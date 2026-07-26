# WiFi upload, auth, and non-interrupting status (design)

Status: approved (via `/grill-me` interview), not yet implemented.
Date: 2026-07-25.

## Problem

The already-shipped wifi provisioning feature (`docs/superpowers/specs/2026-07-25-wifi-upload-design.md`) has three real gaps, all found or confirmed during real-hardware verification, not speculated:

1. **Wifi never pushes code.** `boot.py` only bridges an accepted connection into whatever `/tether_app.py` already exists — that file is only ever written by a prior *serial* `mcu.connect(...)`. Confirmed directly during hardware verification: editing a script and reconnecting over wifi silently ran the *old* version until a fresh serial upload.
2. **The listener is unauthenticated.** Anyone on the same network can be the one connection a boot cycle accepts, and — once this design adds an upload mode — anyone on the network could push and run arbitrary code.
3. **`tether status` kills the very listener it just confirmed is up.** `status` hard-resets the board and interrupts `boot.py` via raw-REPL to query it, then exits back to the plain interactive REPL rather than a fresh hardware reset — `boot.py` only auto-runs on an actual reset, so the TCP listener it had opened doesn't come back. `provision-wifi` → `status` → `mcu.connect(...)` times out on the connect, discovered directly this way.

This design closes all three together, because fixing #3 properly (status without a reset) turns out to require the same underlying architecture change that #1 needs anyway.

## Scope decision

WiFi only — BLE remains out of scope, unchanged from the original design. Builds on the shipped `tether[cli]` provisioning feature; does not touch serial at all.

**This reverses a previously-deferred limitation.** The original wifi design spec explicitly punted on "re-listening after the one accepted connection drops," citing that `_tether_main()` would need to become re-runnable per connection. Making status non-interrupting *requires* `boot.py` to loop and re-accept rather than stay one-shot — so this design solves that general case now (multiple connections per boot cycle, of any mode, including repeated `run` sessions surviving a drop) rather than a status-only special case. This is a bigger change than "add a status mode," and was an explicit, confirmed scope decision (see the `/grill-me` interview this spec is based on) — solving "accept multiple connections per boot" once, generally, rather than solving it narrowly for status and needing to solve it again later for run-mode reconnects.

## Architecture overview

`boot.py` currently: connect wifi → bind/listen once → accept **one** connection → bridge it into the dispatch loop → done, no re-listen.

New shape: connect wifi → bind/listen once (backlog raised from 1 to 4 — cheap insurance against a stray connection attempt colliding with the intended client's own reconnect, not required for correctness under normal single-client sequential usage) → **loop**: accept a connection → read one preamble frame (mode + secret) → check secret → branch on mode → handle to completion → close → loop back to accept the next connection.

Three modes, selected by the preamble:

- **`run`** — today's existing behavior (bridge into the dispatch loop via `_tether_stream_override`), unchanged in mechanism, but now can repeat: a dropped run-mode session no longer needs a physical reset to reconnect.
- **`upload`** — new. Pushes the full bundle (sliced app + `tether_runtime` files), same hash-check-skip-if-unchanged semantics serial already has.
- **`status`** — new. Reports device state over the same authenticated channel, no reset, no interruption.

Sequential only: `boot.py` handles one connection fully (any mode) before looping back to `accept()` the next. No concurrent-connection handling. This means checking status *while* a run-session is actively live isn't possible — only *between* sessions, which is what "status shouldn't need a reset" was actually asking for (confirmed in the interview). Upload and run are always two separate connections, never a same-socket pivot from one mode to another — simpler on-device state machine, and TCP setup on a LAN is cheap enough that this costs nothing meaningful.

## Connection preamble

The first frame on every connection, before any mode-specific handling:

```
{"mode": "run" | "upload" | "status", "secret": "<string, omitted or None if unauthenticated>"}
```

**Correction made while preparing the implementation plan (2026-07-26):** the framing shape (`[4-byte length][body]`) is reused, but the body is **JSON** (`ujson`), not msgpack, and this is not optional. `boot.py` cannot assume `/dispatch.py` or `tether_runtime/umsgpack/` already exist on the filesystem — a board that's only ever been *wifi*-provisioned (never touched serial, never had `upload` mode run) has no runtime bundle yet, and `upload` mode's whole job is to get one there for the first time. Msgpack decoding on-device depends on the *vendored* `umsgpack.py`, which is itself one of the files that may not exist yet — reusing `tether_runtime/dispatch.py`'s `_encode_frame`/`_read_frame` for the preamble would be a circular dependency (the preamble exists partly to negotiate getting the runtime bundle onto a board that doesn't have it). `ujson` has no such problem — it's a MicroPython built-in, always present, nothing to upload first. So: preamble, `status`'s response payload, and `upload`'s per-file metadata and final ok/error response are all length-prefixed JSON text (`[4-byte length][utf-8 json body]`), encoded/decoded with `ujson` (falling back to `json`, matching the existing pattern already used elsewhere in `provisioning.py`). Raw file *content* frames in `upload` mode are NOT JSON-wrapped — those are `[4-byte length][raw bytes]`, no text encoding at all, since JSON can't carry arbitrary binary cleanly and there's no need to make it. On the PC side, this preamble/status/upload-control channel is a small, separate helper (plain stdlib `json` + `struct`, not `tether.marshalling`'s `encode_frame`/`FrameDecoder`, which are msgpack-specific and belong to the `run`-mode RPC layer once bridged) — call it `_wire.py` or similar, shared by both PC and on-device where the *shape* (length-prefix + JSON) matches, even though the two sides can't literally share source.

`run` mode, once selected via the preamble, still bridges into the existing `exec()`-based mechanism entirely unchanged — that layer keeps using real msgpack via `dispatch.py`/`umsgpack`, exactly as today. This is fine and not circular: reaching `run` mode at all implies `/tether_app.py` and `/dispatch.py` already exist (via this new wifi-upload path, or the pre-existing serial path) — only the preamble/status/upload-control layer has the bootstrap problem, and only that layer needed the fix.

Server reads the preamble frame; if a secret is configured on-device (see below) and the client's doesn't match (plain equality — no need for constant-time comparison, the threat model here is "someone on your LAN," not a timing-attack adversary, and that's a deliberate, accepted tradeoff, not an oversight), it replies with `{"ok": false, "error": "auth failed"}` and closes the connection. On success it proceeds directly into the mode-specific handling below (no separate ack needed for `run`, which has its own protocol-version handshake immediately after anyway; `upload`/`status` each define their own first response).

## Mode: `run`

Unchanged bridging mechanism (`exec(_tether_app_src, {"_tether_stream_override": (reader, writer)})`), but now inside the accept loop instead of a one-shot. Two required changes, both on the `boot.py` side:

**1. The `mcu_decorators` registry must be cleared before every fresh `exec()`.** Traced this precisely: `mcu_decorators.py`'s `_registrations` is a *module-level* list, appended to by the `@mcu.export`/`@mcu.loop`/`@pc.export` decorators every time the exec'd bootstrap's top-level code runs. Since `import mcu_decorators` inside the bootstrap only actually executes the module's top-level code on its *first* import (Python/MicroPython caches modules in `sys.modules`), every subsequent `exec()` of the same `tether_app.py` source within the same boot cycle — now possible, since we're looping — reuses the *same*, already-populated `_registrations` list and just keeps appending to it. `Dispatcher.register()` is a dict keyed by name, so this is harmless for plain handler registration (last write wins). It is **not** harmless for `@mcu.loop`: `Dispatcher._loops` is a plain list, and `run()` spawns one `asyncio.create_task` per entry — after N reconnects, an `@mcu.loop`-decorated background task would get spawned N times simultaneously on the Nth connection, an accumulating duplicate-task bug that gets worse with every reconnect. This is a genuinely new risk this design introduces (serial's existing reconnect is unaffected — it always goes through a full hardware `reset_board()` first, which wipes the whole interpreter's `sys.modules`, so `import tether_app` after a serial reset is always a fresh first-time import; wifi's loop, by design, does *not* reset the interpreter between connections).

   Fix: `connection.py`'s `generate_bootstrap()` needs one small addition — clear `mcu_decorators._registrations` at the very start of the generated script, before the sliced `@mcu.export`/`@mcu.loop`/`@pc.export` definitions (and their decorator applications) run:

   ```python
   from mcu_decorators import mcu, pc, registered_mcu_functions
   import mcu_decorators as _tether_mcu_decorators
   _tether_mcu_decorators._registrations.clear()
   ```

   This is the one place this design touches `connection.py` — everything else about `generate_bootstrap()`'s output is unchanged, and this addition is itself harmless on the serial path (clearing an already-empty list on a freshly-reset interpreter is a no-op).

**2. `boot.py`'s loop, not `generate_bootstrap()`'s output, is what makes `run` mode repeatable.** No other change to the bootstrap script is needed — `exec()` with a fresh globals dict is already, inherently, "run from scratch" each time; the only shared, persistent state across repeated execs turned out to be the one list above.

A dropped `run`-mode connection (the existing `except (OSError, EOFError)` handling, already correct) now simply means `boot.py`'s loop proceeds to `accept()` again, instead of the script ending. This directly resolves the original design's deferred "no re-listen" limitation — `board.reconnect()` over wifi can now actually succeed, not just fail loud.

## Mode: `upload`

Pushes the **full bundle** — sliced app plus every `tether_runtime` file (dispatch.py, vendored umsgpack, etc.), the same set `_upload_if_needed`/`write_files` already push over serial — not just `tether_app.py`. This makes "wifi never needs serial again after the first provision" actually true: a `tether_runtime` version bump on the PC side propagates over wifi too, not just app-code changes.

**Hash-check reuses `status` mode's own reported hash — no separate hash-request step.** `status` mode (below) already reports whether `/tether_app.py`/`.tether_hash` exist and their content; `mcu.connect("wifi:<ip>")`'s new automatic flow (see PC-side changes) checks status first, compares the reported hash against the locally-computed bundle hash, and only opens a separate `upload` connection if they differ. `upload` mode itself doesn't need its own hash-negotiation exchange — by the time a client opens an `upload` connection, it has already decided (via a prior `status` check) that an upload is needed.

**No base64.** Unlike serial (which pushes files as base64-encoded raw-REPL script text, since raw-REPL is a text/REPL protocol), wifi's file-content frames are plain `[4-byte length][raw bytes]` — not JSON-wrapped like the preamble/control messages (see the preamble section's correction above) — so file bytes transfer directly, no text-encoding overhead. Per-file *metadata* (path, byte count) is a small JSON control frame; the file's own content immediately follows as one or more raw-byte frames.

**Chunking:** an individual file (most likely `tether_app.py`, for a large user script) can exceed `MAX_FRAME_SIZE` (64 KiB). The wire-level chunking mechanism (splitting one file's content across multiple frames, reassembled on-device before writing) is left to implementation — the invariant to preserve is that no single frame ever needs to hold more than `MAX_FRAME_SIZE` bytes, matching the existing bound everywhere else in this protocol.

**Write behavior:** stream each file's bytes to flash as they arrive rather than buffering the whole bundle in memory first (keeps peak RAM low, matching the resource-safety discipline already applied to `MAX_FRAME_SIZE`/`MAX_CONCURRENT_CALLS`). On completion, write `.tether_hash` last (matching serial's own ordering — a hash file present is the signal "this bundle is complete and self-consistent"), reply `{"ok": true}` or `{"ok": false, "error": "..."}`, then close. The client opens a **separate** `run`-mode connection next to actually start using the new code (per the sequential-only, no-same-socket-pivot decision above) — not a special "upload succeeded, now switch this same connection into run mode" transition.

Directory creation for nested `tether_runtime` paths reuses the same depth-sort logic already fixed in `write_files` (`serial.py`) — the on-device equivalent (`uos.mkdir`) has the identical parent-must-exist-first constraint.

## Mode: `status`

No reset, no raw-REPL, no interruption of anything. Payload:

```
{
  "protocol_version": <int>,
  "tether_app_hash": "<hex digest>" | null,   # null if /tether_app.py doesn't exist yet
  "free_heap": <int>,                          # gc.mem_free()
  "uptime_ms": <int>,                          # time since this boot, not since provisioning
  "ip": "<string>"
}
```

Dropped fields from the *old* `STATUS_SCRIPT`'s payload: `"provisioned"` and `"connected"` — both are now tautological. Reaching this socket at all already proves the board is provisioned and wifi-connected; there's nothing left to ask.

**`tether status` (CLI) becomes two-tier**, since `status` mode only exists once `boot.py` has actually gotten wifi up and is listening — a board that never associates to wifi at all (bad password, out of range) has no socket to ask anything of:

1. Try connecting to the wifi socket in `status` mode first (fast, non-destructive).
2. If that connection attempt itself fails or times out — meaning wifi never came up in the first place, not that the board is merely busy — fall back to today's existing raw-REPL diagnostic (`STATUS_SCRIPT` via `reset_board()` + `run_python()`), which is the only way to diagnose "why isn't wifi connecting" when there's no listener to ask. `tether status` already requires `--port`, so this fallback needs no new plumbing.

This means the common case (board's fine, just checking) becomes instant and non-destructive; the failure-diagnosis case keeps working exactly as it does today, unchanged.

## Auth: shared secret

- `tether provision-wifi` generates a random secret and writes it into `/tether_wifi.json` alongside the wifi credentials (same file, one more field) — every `provision-wifi` run naturally rotates it, since the file is always regenerated wholesale.
- `--danger-unauthenticated` (a `provision-wifi` flag): no secret is generated or stored — `/tether_wifi.json` simply has no `secret` field. `boot.py`'s check becomes: if the config has a secret, require and check it; if not, skip the check entirely (no "empty secret" edge case to get wrong). Prints a loud, impossible-to-miss warning when used, but does **not** block on an interactive confirmation (unlike `unprovision-wifi`'s `beaupy.confirm()`) — blocking would break scripted/CI provisioning for someone who's deliberately chosen this.
- `provision-wifi` prints the generated secret prominently on success (same moment the user would note the IP).
- PC side: `mcu.connect("wifi:<ip>", secret="...")` takes an explicit kwarg, falling back to a `TETHER_WIFI_SECRET` environment variable if omitted. **Deliberately no local PC-side secret store** (no `~/.tether/secrets.json`, no device registry keyed by IP/MAC) — this project has no PC-side persistent state anywhere today, and a local store raises real unresolved questions (keyed by IP, which DHCP can change? gitignore concerns if project-local?) that aren't worth solving until manual secret-passing actually proves painful in practice — same "not worth the complexity until it's a real pain point" call the original design already made for the non-interrupting status channel (now itself being built, having become that pain point).
- A bad or missing secret raises a new, distinct `WifiAuthError(TetherError)` (added to `errors.py` alongside `RemoteError`/`MCUTimeoutError`/`MCUDisconnectedError`/`ProtocolVersionError`) — not a generic `MCUTimeoutError` or connection error. Immediately obvious as an auth problem, not something that reads like a flaky network.

## PC-side changes

- `errors.py`: new `WifiAuthError(TetherError)`.
- `transports/wifi.py`: gains the small length-prefixed-JSON control-channel helpers (send preamble, read a JSON response, send/receive raw byte frames for `upload`'s file content) — separate from `WifiStream`, which remains exactly what it is today (the plain read/write duplex wrapper `run` mode's `Dispatcher` uses once bridged). `connect()` itself is unchanged; the new control-channel functions are used by `_connect_wifi`'s `dial()` before it ever constructs a `WifiStream`.
- `connection.py`'s `_connect_wifi` (and its `dial()` closure) gains real work it doesn't do today: slice the calling script (same as `_connect_serial` already does), compute the bundle hash, open a `status`-mode connection to read the device's current hash, compare, and — if different — open an `upload`-mode connection to push the bundle before finally opening the `run`-mode connection that becomes the live `BoardHandle`. This mirrors `_connect_serial`'s existing `upload-if-needed → start fresh → handshake` shape, just with the hash-check split across a `status` connection instead of a single `read_file`.
- `mcu.connect("wifi:<ip>", secret=...)`'s new `secret` parameter threads through the same public API surface `timeout` already uses.
- `generate_bootstrap()` (`connection.py`): one small, additive change — clear `mcu_decorators._registrations` at the top of the generated script (see "Mode: `run`" above). Backward-compatible: harmless on the serial path.

## On-device changes (`provisioning.py`)

`_BOOT_PY_TEMPLATE` is restructured around the new loop:

1. Wifi connect (unchanged from today).
2. Bind + listen once, backlog raised to 4.
3. `while True:` — `accept()` → read preamble frame (self-contained minimal frame reader, not dependent on `/dispatch.py`) → check secret against `/tether_wifi.json`'s `secret` field if present → branch:
   - `run`: existing `exec()`-based bridge, unchanged mechanism.
   - `upload`: receive and write the bundle, streaming to flash, reply, close.
   - `status`: gather the payload (`gc.mem_free()`, uptime via a `time.ticks_ms()` captured at boot, `/tether_app.py` existence + `.tether_hash` content, IP), reply, close.
   - unrecognized mode or auth failure: reply `{"ok": false, ...}`, close.
4. Loop back to step 3's `accept()`.

`STATUS_SCRIPT` (the raw-REPL fallback diagnostic) stays as-is, unchanged — it's still needed for the "wifi never came up at all" fallback case.

## Testing approach

Same rigor as the original wifi feature: real `micropython` unix-port interpreter tests for `boot.py`'s new loop/preamble/mode-dispatch logic (network faked via `sys.modules` injection, real sockets and a real client thread for the actual protocol exchanges — matching the existing `test_boot_py_bridges_a_real_socket_into_the_dispatch_loop` pattern), `click.testing.CliRunner` for CLI-level changes, and real-hardware verification once implemented (the ESP32 used for the original feature's verification is available). Specifically worth a dedicated real-hardware test: reconnecting `run` mode multiple times in a row without a physical reset, and confirming an `@mcu.loop`-decorated function doesn't duplicate across reconnects (the registry-clear fix above) — this is exactly the kind of thing that's easy to get subtly wrong and only shows up after several reconnects, not the first one.

## Explicitly out of scope

- **Concurrent/simultaneous connections.** Sequential only. Checking status *while* a run-session is actively live isn't possible under this design — only *between* sessions.
- **A same-socket upload→run pivot.** Always two separate connections.
- **BLE.** Still fully out of scope, unchanged from the original design.
- **Real cryptography (TLS, challenge-response auth).** The shared secret is a plaintext pre-shared token, sent in cleartext, checked with a plain equality comparison. Matches the existing accepted tradeoff for wifi credentials themselves (plaintext on-device, no secure storage exists on this hardware class) — MicroPython's `ssl` module exists but is heavy for this hardware class and out of scope here. Explicitly a "keep casual LAN snoopers and accidental cross-connects out," not a defense against a sophisticated network adversary.
- **Local PC-side secret storage/device registry.** Explicit secret passing (kwarg or env var) only, for now.
- **A hard cap or rotation policy on how long `boot.py`'s accept-loop can run continuously.** Repeated `exec()` cycles over a long uptime (days/weeks) could in principle accumulate heap fragmentation on a resource-constrained board; not solving this preemptively, noted as a known, accepted operational characteristic rather than a requirement.
