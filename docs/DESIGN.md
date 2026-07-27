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
   correction below). BLE still requires the board to already be running a
   bootstrapped runtime; `tether` does not push code over BLE directly —
   no on-device BLE listener exists yet.

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
   wifi via the `tether` CLI's `provision-wifi` command (`tether[cli]`
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
   mechanism and its limitations. BLE has no equivalent yet: no on-device
   BLE-advertising management or GATT listener exists, so BLE remains
   PC-side client code with nothing on-device to reach. See § Transports.
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
| Serial | `"serial:auto"` (USB VID/PID scan) or explicit port | Raw-REPL push | **Works today**, verified against real hardware. Primary/first transport, always code-push capable. Every connect (including reconnect) hardware-resets the board first (RTS/DTR toggle, ~1.3s) - added 2026-07-25 after real-hardware testing showed Ctrl-C alone can no longer recover a board already running a previous connect()'s dispatch loop, once `micropython.kbd_intr(-1)` is active on-device (see § Wire protocol). Harmless on an already-idle board. |
| Wifi | `"wifi:<ip>"` | **Full code-push support**, via its own authenticated status/upload/run protocol, reachable via `tether provision-wifi` | **Works today**, verified end-to-end against real ESP32 hardware. Added 2026-07-25 (see `docs/superpowers/specs/2026-07-25-wifi-upload-design.md`); code push, shared-secret auth, and non-interrupting status added 2026-07-26 (see `docs/superpowers/specs/2026-07-25-wifi-modes-auth-design.md`) — a `boot.py` uploaded once by `tether provision-wifi` (`src/tether/provisioning.py`'s `generate_wifi_boot()`) auto-runs on every MicroPython boot: reads credentials and (by default) a random shared secret from `/tether_wifi.json`, joins wifi with a bounded ~15s timeout, opens a TCP listener on `DEFAULT_PORT` (8765, backlog 4), then loops indefinitely, sequentially accepting **one connection at a time** (never concurrently, by design — see § Explicitly-out-of-scope in the design spec). Every connection starts with a small authenticated preamble (length-prefixed JSON, not msgpack — the vendored `umsgpack.py` may not exist yet on a board that's only ever been wifi-provisioned) selecting one of three modes: `status` (protocol version, current bundle hash or `null`, free heap, uptime, IP — no reset, no interruption of anything), `upload` (receives and writes the full runtime bundle — sliced app plus every `tether_runtime` file — chunked so no single frame exceeds 64 KiB), or `run` (bridges the connection into the same generated dispatch loop serial uses, via the `_tether_stream_override` global `generate_bootstrap()` checks before falling back to stdio). `mcu.connect("wifi:<ip>", secret=...)` drives all three automatically — status query → hash comparison → upload-if-needed → run — mirroring serial's own upload-if-needed → start fresh → handshake shape. `board.reconnect()` now succeeds over wifi too (see § Disconnection) since the accept-loop re-listens after each connection ends — still sequential-only, one connection at a time. **Auth:** every connection is authenticated by a shared secret by default, freshly generated and printed once by every `tether provision-wifi` run (save it — there's no way to recover it later short of re-provisioning); `mcu.connect(secret=...)` or the `TETHER_WIFI_SECRET` env var supplies it PC-side, raising `WifiAuthError` on a bad/missing one. `--danger-unauthenticated` (a `provision-wifi` flag) disables the check for a board, printing a loud warning but not blocking on confirmation. The comparison is plain equality, not constant-time — an accepted LAN-snooper-threat-model tradeoff, not an oversight. `tether status` is now two-tier: tries the fast, non-destructive wifi `status`-mode query first; only falls back to the older raw-REPL diagnostic (hard reset required) if wifi itself never came up at all (bad password, out of range) — the common case (board's fine, just checking) is now instant and non-destructive. A real `mcu.connect("wifi:<ip>")` session was verified against a real ESP32: a PC-to-MCU call (`add(3, 4)`), an MCU-to-PC reverse call from inside that same handler, and remote-exception propagation all confirmed working over the real socket bridge. **Real bugs found during final whole-branch review (2026-07-27), both fixed before merge:** (1) the on-device `mcu_decorators` module-level registry accumulated duplicate registrations across repeated `exec()`s of the same generated bootstrap within one boot cycle — harmless for plain handlers (a dict) but spawned duplicate `@mcu.loop` background tasks; fixed by clearing the registry at the top of every generated bootstrap. (2) The dominant, deeper mechanism — found only after (1) was fixed and reconnecting was tested for real several times in a row, not caught by (1)'s own fix or its test: MicroPython's `uasyncio` task queue is itself a process-global structure, and `asyncio.run()` returning or raising does not drain tasks queued via `asyncio.create_task()` inside it, so a previous run-mode session's `@mcu.loop` task(s) stayed alive and kept accumulating across every reconnect regardless of (1)'s fix. Fixed by resetting the event loop (`uasyncio.new_event_loop()`) at the end of every run-mode session — verified directly against the real interpreter: sampled `@mcu.loop` tick counts across 3 successive reconnects went from a broken, accumulating 14/43/87 (deltas 14/29/44 — roughly 1x/2x/3x, the accumulating-duplicate-task signature) before the fix, to staying roughly constant per session after it. **Remaining, accepted limitations:** sequential-only (no concurrent connections — checking status while a run session is live isn't possible, only between sessions); credentials and the shared secret are both stored in plaintext on-device (`/tether_wifi.json`) — no secure storage exists on this hardware class; no real cryptography (TLS, challenge-response) — the shared secret is a plaintext pre-shared token, an explicit "keep casual LAN snoopers out" tradeoff, not a defense against a sophisticated network adversary. |
| BLE | `"ble:<addr>"` | Same bootstrap requirement as wifi | **Not usable against a real device yet**, same reason as wifi. Custom GATT service design (one write characteristic, one notify characteristic; frame chunking/reassembly in the transport adapter for BLE's small MTU) is implemented on the PC side and unit-tested against a hand-written fake, but there is no on-device GATT peripheral implementation, and BLE hardware has never been tested against this project at all. |

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
`provision-wifi` run. The design's own sequential-only constraint still
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
