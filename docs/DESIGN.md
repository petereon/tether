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
2. **Stub.** For every `@pc.export` function, generate a matching MCU-side
   proxy stub (same name, same signature) whose body sends an RPC frame and
   awaits the reply. From MCU code, calling a PC function looks identical to
   calling a local one.
3. **Bundle.** Package the sliced script + generated stubs + vendored
   `umsgpack.py` + the `uasyncio`-based dispatch-loop runtime.
4. **Hash-check.** Compare a hash of the bundle against a sentinel already on
   the board. Skip upload + reset if unchanged (fast dev-loop iteration).
5. **Upload** (if needed) via raw-REPL over serial. For wifi/BLE, the board
   must already be running a bootstrapped runtime (pushed once over serial);
   `tether` does not push code over wifi/BLE directly.
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

## Transports

| Transport | Discovery | Upload | Notes |
|---|---|---|---|
| Serial | `"serial:auto"` (USB VID/PID scan) or explicit port | Raw-REPL push | Primary/first transport, always code-push capable |
| Wifi | `"wifi:<ip>"` | Not supported directly — board must already run a bootstrapped runtime | Pure stdlib `socket` on PC side |
| BLE | `"ble:<addr>"` | Same bootstrap requirement as wifi | Custom GATT service (one write characteristic, one notify characteristic); frame chunking/reassembly handled transparently in the transport adapter (BLE MTU is small) |

Multiple boards are supported concurrently — `connect()` returns a handle,
decorated functions are accessed as attributes on that handle
(`board.read_temp()`), never as ambiguous global calls.

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
