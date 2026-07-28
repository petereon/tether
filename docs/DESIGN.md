# tether — Design Doc

Status: locked (v1 scope), pre-implementation.
Date: 2026-07-24.

## Pitch

Write MicroPython and Python in one file. Decorate a function `@mcu.export` and
call it from your PC code like any normal Python function — `tether` uploads
it to the board, marshals the call over the wire, and returns the result.
Decorate a PC function `@pc.export` and call it back from the MCU side the
same way. Protocol-agnostic: serial, wifi, BLE. Targets ESP32 and similar
MicroPython boards.

## Non-goals (v1)

- Classic Bluetooth (SPP) — MicroPython support on target boards is too
  inconsistent. BLE only.
- Full zero-touch firmware flashing from a blank board (esptool, wifi
  credential injection). v1 assumes MicroPython is already flashed; `tether`
  handles code upload, not firmware provisioning.
- ISR-driven heartbeats that survive a genuinely blocking (non-cooperative)
  MCU call. Long-running MCU functions are expected to be written
  cooperatively (`await asyncio.sleep(...)` in a loop, not `time.sleep(20)`).
- Cross-version protocol negotiation. A protocol mismatch is a hard error,
  not a negotiated downgrade.
- Pluggable custom-type serializers. The registry hook exists in the type
  system but ships empty in v1.

## Architecture overview

```
user_script.py
├── @mcu.export def read_temp() -> float: ...      # MCU-bound
├── @mcu.loop(interval_ms=100) def poll(): ...      # MCU-bound, periodic
├── @pc.export def log_event(msg: str) -> None: ... # PC-bound
└── board = mcu.connect("serial:auto")
    board.read_temp()                               # sync call, PC -> MCU
```

At `connect()` time:

1. **Slice.** AST-walk the entry file (and local imports it references,
   transitively) starting from every `@mcu.export`/`@mcu.loop` function.
   Follow referenced names into module-level assignments, class defs, and
   helper functions so peripheral setup (`led = Pin(2, Pin.OUT)`) is captured
   automatically — no separate annotation needed.

   **Correction (found building chunk 15's example, 2026-07-24):** the
   slicer genuinely does capture module-level peripheral setup like
   `led = Pin(2, Pin.OUT)` as shown above — but that pattern only works if
   the entry file is never executed directly by the PC. Since the whole
   pitch is a single file runnable as a normal PC script too, a *top-level*
   `from machine import Pin` / `led = Pin(2, Pin.OUT)` executes under
   CPython as well (where `machine` doesn't exist) and crashes before
   `connect()` is ever reached — confirmed by actually running it, not by
   inspection. The example in `examples/blink_and_log/` shows the pattern
   that actually works instead: construct hardware objects *inside* the
   `@mcu.export` function body (lazily, on first call) so that code path
   never executes on the PC side. Module-level assignments are still
   captured and useful for MCU-only constants/config that don't touch
   hardware modules — the caution is specifically about anything importing
   or constructing from a hardware-only module (`machine`, etc.) at module
   scope.
2. **Stub.** For every `@pc.export` function, generate a matching MCU-side
   proxy stub (same name, same signature) whose body sends an RPC frame and
   awaits the reply. From MCU code, calling a PC function looks identical to
   calling a local one.
3. **Bundle.** Package the sliced script + generated stubs + vendored
   `umsgpack.py` + the `uasyncio`-based dispatch-loop runtime.
4. **Hash-check.** Compare a hash of the bundle against a sentinel already on
   the board. Skip upload + reset if unchanged (fast dev-loop iteration).
5. **Upload** (if needed) via raw-REPL over serial, or, as of 2026-07-26,
   over wifi via its own authenticated status/upload/run protocol (see the
   correction below) — and, as of 2026-07-28, over BLE via the same
   status/upload/run protocol, reused over one physical BLE connection
   instead of wifi's separate-connection-per-mode shape (see § Transports'
   BLE row).

   **Correction (2026-07-26):** the original framing above ("`tether` does
   not push code over wifi/BLE directly, by design, even once this
   exists") turned out to be wrong for wifi specifically, once a wifi-side
   upload mechanism was actually designed and built (see
   `docs/superpowers/specs/2026-07-25-wifi-modes-auth-design.md`).
   `mcu.connect("wifi:<ip>")` now performs the same slice → hash-check →
   upload-if-needed → run flow serial always has — just split across
   short-lived `status` and `upload` connections instead of a single
   raw-REPL session: a `status`-mode connection reports the device's
   current bundle hash (`null` if nothing's uploaded yet), the PC compares
   it against the hash of the freshly-sliced bundle, and only opens a
   separate `upload`-mode connection if they differ, before finally
   opening the long-lived `run`-mode connection that becomes the live
   `BoardHandle`. Every wifi connection (any mode) is now also
   authenticated by a shared secret by default — see § Transports' Wifi
   row for the full mechanism. BLE's "no code push" limitation is
   unaffected by this — it only closes the gap for wifi.

   **Current status (updated 2026-07-25, wifi upload/auth/status pieces
   superseded by the 2026-07-26 correction above):** the "board must already be
   running a bootstrapped runtime" precondition is now achievable for
   wifi via the `tether` CLI's `provision wifi` command (`tether[cli]`
   extra; `src/tether/cli.py` + `src/tether/provisioning.py`) - a small,
   uploaded-once `boot.py` handles on-device wifi-connection management
   (credentials, reconnect-on-boot) and TCP listening that
   `generate_bootstrap()` itself still knows nothing about.
   `generate_bootstrap()` (step 3) still always wires the dispatch loop's
   `_tether_main()` the same way it always has, checking a
   `_tether_stream_override` global before falling back to
   `sys.stdin`/`sys.stdout`; nothing on the serial path ever sets that
   global, so serial's stdio wiring is unchanged. `boot.py` is the one
   thing that *does* set it, to the accepted socket's reader/writer,
   before exec'ing `tether_app.py` - see § Transports' Wifi row for the
   mechanism and its limitations. As of 2026-07-28, BLE has the same shape
   via `tether provision ble` and its own on-device GATT peripheral - see
   § Transports' BLE row.
6. **Handshake.** Exchange a protocol-version frame. Hard error
   (`ProtocolVersionError`) on mismatch.
7. **Ready.** Board handle returned; calls now dispatch over the transport.

## Call semantics

- **Sync by default.** `board.read_temp()` blocks the calling thread and
  returns the value. Async available via `await board.read_temp.async_call()`
  for callers already in an event loop.
- **Reentrant / bidirectional.** Every request carries a request-ID. While a
  call is in flight, the dispatch loop on both sides keeps pumping incoming
  frames — so MCU code can call back into PC code (or vice versa) from inside
  a handler without deadlocking, to arbitrary nesting depth.
- **PC-side concurrency.** A background reader thread continuously drains the
  transport into a queue. A "blocking" call filters that queue for its own
  request-ID, dispatching any other in-flight request it observes to a small
  thread pool (so a nested reverse-call doesn't stall the reader). This keeps
  the PC-facing API plain synchronous Python — no `asyncio` required in user
  code on the PC side.
- **MCU-side concurrency.** Dispatch loop is a `uasyncio` task. User's own
  MCU background logic also runs as `uasyncio` tasks — either their own, or
  via `@mcu.loop(interval_ms=...)` for periodic work. Both coexist under one
  `asyncio.run()`.

## Wire protocol

- **Framing:** length-prefixed, `[4-byte length][msg-type][msgpack body]`.
- **Serialization:** msgpack. PC side uses the standard `msgpack` package;
  MCU side uses a vendored `umsgpack.py` (not stdlib in MicroPython),
  auto-uploaded alongside every deploy.
- **Type set (v1):** `int, float, bool, str, bytes, list, dict`, recursively,
  msgpack-safe values only. Enforced by inspecting type hints at decoration
  time — an unsupported parameter/return type is a definition-time error, not
  a surprise at call time. A registry hook for custom-type encode/decode
  exists but is unimplemented in v1.
- **Errors:** a remote exception is caught, serialized (type name + message +
  traceback string), and re-raised on the caller side wrapped as
  `RemoteError` (subclasses the appropriate category where cheap to do so;
  always carries `.remote_type`, `.remote_traceback`).
- **Timeouts:** configurable globally and per-call
  (`@mcu.export(timeout=10)`). Distinct from `RemoteError` —
  `MCUTimeoutError` means "no answer," not "answer was a failure."
- **Heartbeats:** automatic, piggybacked on the MCU function's natural
  `await` yield points — the runtime opportunistically emits a lightweight
  "still alive" frame during long calls if enough time has passed since the
  last one. The PC-side idle-timeout clock resets on each heartbeat, not just
  on the final response. Optional manual progress reporting
  (`await mcu.heartbeat("40% done")`) layers richer status onto the same
  frame type. A function that blocks synchronously (no `await` points) gets
  no heartbeat — an accurate reflection of the board being genuinely
  unresponsive during that window.
- **Ctrl-C is disabled on-device** (`micropython.kbd_intr(-1)`, set before
  the dispatch loop starts reading any protocol bytes) — added 2026-07-25
  after real-hardware testing (not the CPython/PTY simulation this was
  originally checked with) showed MicroPython intercepts a raw `0x03` byte
  as a keyboard interrupt even when it's just a data byte inside a running
  program's own stdin stream, not a terminal-level signal. msgpack encodes
  small integers as their own literal byte value, so any call argument that
  happens to produce `0x03` (e.g. the integer `3`) would otherwise kill the
  dispatch loop from the inside. This is why a stuck board can no longer be
  recovered via Ctrl-C alone — see § Transports' Serial row for the
  hardware-reset consequence.

## Transports

| Transport | Discovery | Upload | Notes |
|---|---|---|---|
| Serial | `"serial:auto"` (USB VID/PID scan) or explicit port | Raw-REPL push | Verified against real hardware. Primary/first transport, always code-push capable. Every connect (including reconnect) hardware-resets the board first (RTS/DTR toggle, ~1.3s) - added 2026-07-25 after real-hardware testing showed Ctrl-C alone can no longer recover a board already running a previous connect()'s dispatch loop, once `micropython.kbd_intr(-1)` is active on-device (see § Wire protocol). Harmless on an already-idle board. |
| Wifi | `"wifi:<ip>"` | **Full code-push support**, via its own authenticated status/upload/run protocol, reachable via `tether provision wifi` | Verified end-to-end against real ESP32 hardware. Added 2026-07-25 (see `docs/superpowers/specs/2026-07-25-wifi-upload-design.md`); code push, shared-secret auth, and non-interrupting status added 2026-07-26 (see `docs/superpowers/specs/2026-07-25-wifi-modes-auth-design.md`) — a `boot.py` uploaded once by `tether provision wifi` (`src/tether/provisioning.py`'s `generate_wifi_boot()`) auto-runs on every MicroPython boot: reads credentials and (by default) a random shared secret from `/tether_wifi.json`, joins wifi with a bounded ~15s timeout, opens a TCP listener on `DEFAULT_PORT` (8765, backlog 4), then loops indefinitely, sequentially accepting **one connection at a time** (never concurrently, by design — see § Explicitly-out-of-scope in the design spec). Every connection starts with a small authenticated preamble (length-prefixed JSON, not msgpack — the vendored `umsgpack.py` may not exist yet on a board that's only ever been wifi-provisioned) selecting one of three modes: `status` (protocol version, current bundle hash or `null`, free heap, uptime, IP — no reset, no interruption of anything), `upload` (receives and writes the full runtime bundle — sliced app plus every `tether_runtime` file — chunked so no single frame exceeds 64 KiB), or `run` (bridges the connection into the same generated dispatch loop serial uses, via the `_tether_stream_override` global `generate_bootstrap()` checks before falling back to stdio). `mcu.connect("wifi:<ip>", secret=...)` drives all three automatically — status query → hash comparison → upload-if-needed → run — mirroring serial's own upload-if-needed → start fresh → handshake shape. `board.reconnect()` succeeds over wifi too (see § Disconnection) since the accept-loop re-listens after each connection ends — still sequential-only, one connection at a time. **Auth:** every connection is authenticated by a shared secret by default, freshly generated and printed once by every `tether provision wifi` run (save it — there's no way to recover it later short of re-provisioning); `mcu.connect(secret=...)` or the `TETHER_WIFI_SECRET` env var supplies it PC-side, raising `WifiAuthError` on a bad/missing one. `--danger-unauthenticated` (a `provision wifi` flag) disables the check for a board, printing a loud warning but not blocking on confirmation. **HMAC nonce-challenge (2026-07-28):** the shared secret itself never crosses the wire. The device sends a fresh random nonce (`os.urandom(16)`) as the very first thing on every accepted connection, before reading anything; the client responds with `HMAC-SHA256(secret, nonce)` instead of the secret itself, so a passive LAN observer of the connection learns neither the secret nor a value that lets it replay onto a future (different-nonce) connection — a real upgrade over the earlier plain-equality-on-the-secret-itself design, still short of TLS (no confidentiality for the rest of the exchange, no protection against an active MITM). MicroPython has `hashlib.sha256` but no `hmac` module, so the device runs a small hand-rolled HMAC-SHA256 (verified byte-for-byte against CPython's real `hmac` both locally and on real ESP32 hardware); the PC side uses real stdlib `hmac`. `tether status` is now two-tier: tries the fast, non-destructive wifi `status`-mode query first; only falls back to the older raw-REPL diagnostic (hard reset required) if wifi itself never came up at all (bad password, out of range) — the common case (board's fine, just checking) is now instant and non-destructive. A real `mcu.connect("wifi:<ip>")` session was verified against a real ESP32: a PC-to-MCU call (`add(3, 4)`), an MCU-to-PC reverse call from inside that same handler, and remote-exception propagation all confirmed working over the real socket bridge. **Real bugs found during final whole-branch review (2026-07-27), both fixed before merge:** (1) the on-device `mcu_decorators` module-level registry accumulated duplicate registrations across repeated `exec()`s of the same generated bootstrap within one boot cycle — harmless for plain handlers (a dict) but spawned duplicate `@mcu.loop` background tasks; fixed by clearing the registry at the top of every generated bootstrap. (2) The dominant, deeper mechanism — found only after (1) was fixed and reconnecting was tested for real several times in a row, not caught by (1)'s own fix or its test: MicroPython's `uasyncio` task queue is itself a process-global structure, and `asyncio.run()` returning or raising does not drain tasks queued via `asyncio.create_task()` inside it, so a previous run-mode session's `@mcu.loop` task(s) stayed alive and kept accumulating across every reconnect regardless of (1)'s fix. Fixed by resetting the event loop (`uasyncio.new_event_loop()`) at the end of every run-mode session — verified directly against the real interpreter: sampled `@mcu.loop` tick counts across 3 successive reconnects went from a broken, accumulating 14/43/87 (deltas 14/29/44 — roughly 1x/2x/3x, the accumulating-duplicate-task signature) before the fix, to staying roughly constant per session after it. **Remaining, accepted limitations:** sequential-only (no concurrent connections — checking status while a run session is live isn't possible, only between sessions); credentials and the shared secret are both stored in plaintext on-device (`/tether_wifi.json`) — no secure storage exists on this hardware class; no TLS — the rest of the exchange (mode selection, uploaded code, RPC traffic) is unencrypted even though the secret itself no longer crosses the wire (see the HMAC nonce-challenge above), an explicit "keep casual LAN snoopers out, and stop them replaying a captured secret" tradeoff, not a defense against a sophisticated active network adversary. |
| BLE | `"ble:<addr>"` (macOS: the address bleak/CoreBluetooth reports is a randomized per-app UUID, not the device's real BLE MAC — see note below) | **Full code-push support**, via the same status/upload/run protocol wifi uses, reused over **one** physical BLE connection (not wifi's separate-connection-per-mode shape — BLE connection setup is too costly to redo per mode), reachable via `tether provision ble` | Verified end-to-end against real ESP32 hardware (2026-07-28, see `docs/superpowers/specs/2026-07-27-ble-modes-auth-design.md`). Built on MicroPython's built-in `bluetooth` module (no `aioble` vendoring — confirmed absent from the generic ESP32 firmware this project verifies against). A `boot.py` uploaded once by `tether provision ble` (`generate_ble_boot()`) auto-runs on every boot: advertises a fixed 128-bit GATT service (one write characteristic, one notify characteristic; frame chunking/reassembly matches wifi's length-prefixed-JSON control channel, split across the negotiated ATT MTU), accepts one central at a time, and — the one deliberate divergence from wifi — reuses that **single connection** across any number of `status`/`upload` preambles in sequence; only `run`, an auth failure, or an unrecognized mode ends the session and resumes advertising. `_handle_status`/`_handle_upload`/`_handle_run` are the exact same shared logic wifi's `boot.py` uses (`_MODE_HANDLER_FUNCTIONS_SRC`), reused via a `.recv(n)`/`.send(data)` adapter matching a real socket's contract; `_handle_run` alone gets a BLE-specific async stream adapter, since `uasyncio.StreamReader/Writer` require a real socket at the native level, which a BLE connection isn't — the on-device session loop itself is deliberately **synchronous** (matching wifi's own accept-loop shape), not async, because nesting it under `uasyncio.run()` would nest `_handle_run`'s own `asyncio.run()`/`new_event_loop()` inside an outer event loop and corrupt MicroPython's process-global uasyncio task queue (confirmed via real-interpreter reproduction during final review — the brief's originally-proposed async outer loop segfaults MicroPython outright, exit 139). **Auth:** same shared-secret model as wifi, independent secret/file (`/tether_ble.json`, `TETHER_BLE_SECRET` env var) — `WifiAuthError` is reused as-is for BLE auth failures too. `--danger-unauthenticated` supported identically. Same HMAC nonce-challenge as wifi (2026-07-28, see wifi's own row above) — with one BLE-specific difference: since BLE reuses **one** physical connection across every mode, only the FIRST preamble on a connection presents a nonce response; later preambles on that same (already-authenticated) connection skip the check, matching the one-connection-reused model above. `tether status --ble-addr` mirrors wifi's two-tier `--ip` behavior. **boot.py conflict:** MicroPython auto-runs exactly one `/boot.py`; wifi and BLE are mutually exclusive on one board — `provision ble`/`provision wifi` each warn (not block) before overwriting the other's boot.py, leaving the other's credentials file orphaned but harmless. A real `mcu.connect("ble:<addr>")` session was verified against a real ESP32: full upload-and-run with no prior serial upload, script-edit propagation (hash-check correctly re-uploads), auth rejection (wrong and missing secret), `--danger-unauthenticated`, and — the headline check — `board.reconnect()` three times in a row with no physical reset, confirming `@mcu.loop` tasks don't accumulate (deltas 14/16/15, flat) on real BLE hardware, matching wifi's own fix. **Real bugs found during final whole-branch review (2026-07-28), fixed before merge:** an unguarded `gap_advertise()` call right after an async `gap_disconnect()` could brick the board's BLE reachability until a physical reset (NimBLE errors advertising with an active connection if the disconnect IRQ hasn't landed yet — fixed by waiting for it, bounded, with a non-raising retry); `gatts_set_buffer` needed `append=True` to avoid a real write-buffer race on bursty multi-chunk writes; the control channel had no read timeout at all (reintroducing the exact class of hang wifi's own `0cb1c85` fix closed); and `provision ble`'s MAC-readback step was Ctrl-C'ing the BLE `boot.py` it had just installed, with nothing resetting the board again afterward, bricking every subsequent connect attempt. **Follow-up fixes (2026-07-28, see CHUNKS.md chunk 21's addendum for the full list):** `BleStream.write()` gained the same timeout `read()` already had (was still unbounded); a dedicated `@mcu.loop`-duplication-across-reconnects regression test now covers BLE the way wifi's own headline test does; on-device `bluetooth.BLE()` now explicitly sets `gap_name` to match the advertised local name, closing a real discrepancy (MicroPython's own default `gap_name`, "MPY ESP32", disagreed with this project's advertised "tether" — confirmed and fixed at the source; a residual per-scan display lag on some OS Bluetooth stacks, from their own device-name caching, is outside tether's control). **macOS-specific note:** CoreBluetooth hides real BLE MAC addresses from apps for privacy, exposing a randomized per-app UUID instead — `mcu.connect("ble:<addr>")` on macOS needs that UUID (obtained via a BLE scan), not the MAC `tether provision ble` prints (which is correct and usable as-is on Linux/BlueZ). **Real bug found running `examples/ble_blink/` against real hardware (2026-07-28), fixed same-day:** the HMAC nonce-challenge (see wifi's row above) made the device send its one-per-connection nonce the instant the BLE link forms (`_IRQ_CENTRAL_CONNECT`) — but `BleakClient.start_notify()`'s CCCD subscription write happens strictly *after* `client.connect()` returns on the PC side, so a notify sent before that subscription completes is silently dropped by the BLE stack (no delivery confirmation exists to detect this from the device side). Confirmed via an isolated bare-bleak reproduction (connect, subscribe, wait 5s): zero notifications received, every time, not an occasional flake. Fixed by resending the nonce on a timer (`_NONCE_RESEND_MS` = 300ms) until the central's first preamble actually arrives, bounded by `_NONCE_MAX_WAIT_MS` (5s) so a central that connects but never subscribes doesn't wedge the session forever — safe against duplicate delivery since a not-yet-subscribed central's earlier notify was truly dropped, not queued for later. Regression test (`test_ble_boot_resends_the_nonce_while_waiting_for_the_first_preamble`) verifies the resend mechanism itself (the fake BLE peripheral can't model "silently dropped," so it checks that a slow-to-respond central sees multiple identical nonce notifications, not just one) — mutation-verified: reverting to a single one-shot send drops the notification count from ≥2 back to 1, correctly failing the test. Re-verified end-to-end against real ESP32 hardware after the fix (`examples/ble_blink/`, full run including `board.reconnect()`). **Remaining, accepted limitations:** same as wifi — sequential-only (one central at a time), the secret is still stored in plaintext on-device and no TLS-equivalent covers the rest of the exchange (see wifi's own row above for the HMAC nonce-challenge that DOES now protect the secret itself in transit), no BLE pairing/bonding (deliberately not used - see NOTES.local.md for why), no scanning/discovery command (`provision ble` printing the address is the only discovery mechanism), no concurrent wifi+BLE on one board. |

Multiple boards are supported concurrently — `connect()` returns a handle,
decorated functions are accessed as attributes on that handle
(`board.read_temp()`), never as ambiguous global calls.

**Amendment (2026-07-25):** decorated functions are now *also* directly
callable by name (`read_temp()`, no `board.` prefix) - this doesn't
contradict the "never as ambiguous global calls" line above, it resolves
the ambiguity differently. `connect()` sets itself as the ambient "current
board" (a `contextvars.ContextVar`, thread/task-safe - the same mechanism
`asyncio`/`decimal` use for this kind of state), so a directly-called
export dispatches through whichever board is ambient. For the common
single-board case this needs zero extra syntax - the original design's own
goal ("calling a local one" - see this section's opening pitch) applies
more literally than `board.read_temp()` ever did. Multi-board
disambiguation is still fully supported, just via an explicit `with
board:` scope instead of always requiring the attribute form:
```python
with board_a:
    read_temp()  # -> board_a
with board_b:
    read_temp()  # -> board_b
```
`board.read_temp()` still works unchanged (explicit, bypasses the ambient
lookup) - this was purely additive, not a breaking change, driven by
wanting PC-side calling code to read like normal Python with no
boundary-awareness at the call site, outside the decorators themselves.
A reentrant `@pc.export` handler (running because some board's MCU called
it) sees *that* board as ambient for any further ambient calls it makes
itself, not whatever's ambient on the connecting/main thread - contextvars
don't propagate across `Dispatcher`'s worker-thread pool on their own
(confirmed empirically, not assumed - neither plain `threading.Thread` nor
`concurrent.futures.ThreadPoolExecutor` copy a calling thread's context by
default), so `Dispatcher` sets the ambient board fresh, per handled call,
to the board it itself belongs to.

## Disconnection

Disconnection during a call fails loud (`MCUDisconnectedError`) — no silent
auto-reconnect, since a board coming back may be in an unknown state (reset,
lost in-progress work, or a physically different device). `board.reconnect()`
is available as an explicit, deliberate re-attach.

**Wifi-specific note (updated 2026-07-26 — corrects the prior claim
below):** `board.reconnect()` **can** now succeed over wifi. The original
claim here — "`board.reconnect()` cannot succeed over wifi" — was true
against the original one-shot `boot.py` (accepted exactly one TCP
connection per boot cycle, never re-listened), but is no longer accurate:
`boot.py`'s accept-loop (see § Transports' Wifi row) now loops
indefinitely, re-listening after every connection ends, so a dropped
run-mode session reconnects without a physical reset or a fresh
`provision wifi` run. The design's own sequential-only constraint still
holds, though: only **one connection at a time** — reconnecting closes
the previous connection first (`_connect_wifi`'s `dial()` now does this
explicitly, closing the prior `WifiStream` before opening a new one, so a
caller doesn't have to remember to close it themselves), and there is
still no concurrent-connections support (e.g. checking `status` while a
`run` session is actively live). `reconnect()` over wifi fails loud
(`MCUDisconnectedError`/`WifiAuthError`/timeout, as appropriate) the same
way any other disconnect does if the device is genuinely unreachable —
it just no longer *always* fails as a matter of protocol design.

## Testing

`mcu.connect("mock://")` spins up an in-process fake MCU that runs the actual
sliced code path (stubs, dispatch loop, msgpack encode/decode included)
against a second thread, so full round-trip behavior is testable in plain
`pytest` with no hardware and no MicroPython firmware/emulator dependency.
Known limitation: won't catch MicroPython-specific runtime quirks that differ
from CPython.

## Dependencies

- PC side: `pyserial` and `bleak` as optional extras (`tether[serial]`,
  `tether[ble]`); wifi transport is pure stdlib `socket`. Core install has no
  hard transport dependency.
- Slicing: stdlib `ast` for the dependency walk; `ruff` invoked as a
  post-slice cleanup pass (unused-import stripping) — not used for the core
  slicing/tree-shaking logic itself, which `ruff` has no stable API for.

## Standing design constraint

A language server (VS Code integration) is planned as future work. The
decorator API is kept statically analyzable from day one — no dynamic
decorator generation, no metaprogramming that would prevent an LSP from
inferring cross-boundary call signatures, hovering docs, or resolving
"go to MCU implementation" from a PC-side call site. This constrains *all*
future API additions, not just what's covered above.
