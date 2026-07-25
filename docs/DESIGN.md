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
5. **Upload** (if needed) via raw-REPL over serial. For wifi/BLE, the board
   must already be running a bootstrapped runtime; `tether` does not push
   code over wifi/BLE directly, by design, even once this exists.

   **Current status:** the "board must already be running a bootstrapped
   runtime" precondition above is not achievable today by any means,
   manual or automatic. `generate_bootstrap()` (step 3) always wires the
   dispatch loop to `sys.stdin`/`sys.stdout` - there is no variant that
   wires it to a socket or a BLE GATT service, and no on-device
   wifi-connection or BLE-advertising management exists either. Serial is
   the only transport that works against a real device right now; wifi and
   BLE are PC-side client code with nothing on-device to reach. See §
   Transports.
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
| Wifi | `"wifi:<ip>"` | Not supported directly — board must already run a bootstrapped runtime | **Not usable against a real device yet.** PC-side client (pure stdlib `socket`) exists and is tested, but nothing generates or uploads an on-device program that listens on a socket - `generate_bootstrap()` only ever wires to `sys.stdin`/`sys.stdout`. There is also no on-device wifi-connection-management story (credentials, reconnect after reset) - explicitly out of scope when chunk 12 built the PC-side client, not yet revisited. |
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
