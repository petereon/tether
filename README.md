# tether

Write MicroPython and Python in one file, and call across the PC/MCU
boundary like it's a normal function call.

Building a project that pairs a microcontroller with a PC-side app usually
means two separate codebases, a hand-rolled wire protocol, and manual
marshalling every time you add a feature. `tether` collapses that into one
file: decorate a function `@mcu.export` and it runs on the board; decorate
one `@pc.export` and it runs on your PC. Call either one from the other
side exactly like a local function call — `tether` figures out which parts
of the file belong on the MCU, uploads just that code, and handles framing,
serialization, and routing calls over the wire.

Targets ESP32 and similar MicroPython-capable boards, over serial, wifi, or
BLE.

## How it works

- **One file, two runtimes.** The same `.py` file runs as a normal script
  on your PC and (in sliced form) as the program running on the MCU.
  `@mcu.export`/`@mcu.loop` mark MCU-bound functions; `@pc.export` marks
  PC-bound ones.
- **AST slicing, not manual splitting.** At connect time, `tether` walks
  the file's syntax tree from every `@mcu.export`/`@mcu.loop` function,
  pulling in whatever helper functions, module-level assignments, and
  local imports it actually depends on. Only that subset gets uploaded —
  you don't maintain a separate MCU-only file by hand.
- **Calls cross the boundary transparently.** Every exported function gets
  a matching stub generated on the other side. Calling an MCU function from
  the PC (or a PC function from the MCU) sends a request over the wire,
  waits for the result, and returns it like any other function call —
  including calls made *from inside* a call already in flight, in either
  direction.
- **Type-checked at the boundary.** Only `int`, `float`, `bool`, `str`,
  `bytes`, `list`, and `dict` (recursively) can cross — enforced from type
  hints at decoration time, so an unsupported type is a definition-time
  error, not a surprise mid-call.

## Install

```bash
uv pip install -e ".[serial]"   # or "[ble]" for Bluetooth; wifi needs no extra
```

## Example

```python
from tether import mcu, pc


@pc.export
def log_event(msg: str) -> None:
    print("MCU says:", msg)


@mcu.export
def read_temp() -> float:
    # Hardware imports go INSIDE the function body: this file also runs
    # directly on your PC, where `machine` doesn't exist.
    from machine import ADC, Pin

    return ADC(Pin(4)).read_u16() / 65535


mcu.connect("serial:auto")
print(read_temp())
```

`connect()` sets itself as the ambient "current board" — call an
`@mcu.export` function like any other Python function, no board-awareness
needed at the call site. `board = mcu.connect(...)` still works if you want
to be explicit (`board.read_temp()`), or need to juggle more than one board
at once (`with board:` scopes which one is ambient for a block).

## Transports

| Transport | Address | Notes |
|---|---|---|
| Serial | `"serial:auto"` (USB auto-discovery) or an explicit port | The only transport that can push code to the board (over MicroPython's raw REPL). |
| Wifi | `"wifi:<ip>"` | Pure stdlib socket, no extra install. Board must already be running a `tether`-uploaded program (get it there once over serial first). |
| BLE | `"ble:<addr>"` | Same requirement as wifi — connects to an already-running board, doesn't push code. |

## Walkthrough: blink an LED

A complete, runnable example lives in `examples/blink_and_log/`. It blinks
an onboard LED a set number of times from a PC script, with the MCU logging
progress back after each blink — exercising both `@mcu.export` (PC calls
MCU) and `@pc.export` (MCU calls PC) in one file.

```bash
uv pip install -e ".[serial]"
cd examples/blink_and_log
python blink_and_log.py
```

Expected output:

```
Connected. Blinking 5 times...
  blink 1/5
  blink 2/5
  blink 3/5
  blink 4/5
  blink 5/5
Done.
```

The example hardcodes `Pin(2, Pin.OUT)` — pin 2 is the common onboard LED
pin on many ESP32 dev boards; check yours and adjust if it doesn't light
up.

## Status

Core functionality (slicing, the wire protocol, all three transports,
multi-board and reconnect handling) is implemented and tested. Serial has
been verified against real ESP32 hardware, including reconnecting and
re-running repeatedly. Wifi and BLE are covered by automated tests (real
TCP sockets, and a fake matching the BLE library's API) but not yet
verified against real hardware. See `docs/DESIGN.md` for the full
architecture.
