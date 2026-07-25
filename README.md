# tether

Write MicroPython and Python in one file. Call MCU functions from your PC
script, and PC functions from your MCU code, like ordinary Python calls —
`tether` handles slicing, upload, and wire marshalling over serial, wifi, or
BLE.

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

**This walkthrough has been run against a real board** (ESP32-WROOM-32D,
2026-07-24 and 2026-07-25) — connected over `serial:auto`, uploaded and
started, blinked the onboard LED 5 times, and logged progress back from MCU
to PC after each blink, exactly as shown above, including reconnecting and
re-running repeatedly with no manual reset in between. Real-hardware runs
across these two sessions found and fixed real bugs that no amount of
testing without hardware had caught: a missing `mcu.connect` API wiring, a
raw-REPL protocol race, a MicroPython Ctrl-C interception issue, a missing
PC-side handler registration for `@pc.export` functions, and — found only
once running the same script back-to-back multiple times — a board already
running a previous connection's dispatch loop could no longer be recovered
by Ctrl-C alone, needing an explicit hardware reset before every connect.
See `docs/CHUNKS.md`'s chunk 15 and chunk 18 entries for the full account
of each. Serial is the one transport verified this way so far; wifi and BLE
are still verified only as thoroughly as possible without hardware (real
TCP sockets / a hand-written fake matching bleak's API, respectively) — the
same category of gap this note used to describe for all three.

## Status

All 17 originally-planned chunks of `docs/CHUNKS.md`'s roadmap are
implemented and tested (slicing, marshalling, dispatch, all three
transports, connection orchestration, multi-board/reconnect handling, a
real-hardware-verified example, CI lint, and a PyPI release workflow), plus
chunk 18: an additional ambient-board calling convention (`read_temp()`,
not just `board.read_temp()`) and the serial reconnect-reliability fixes
found while exercising it against real hardware. See `docs/DESIGN.md` for
the full locked architecture and `docs/CHUNKS.md` for exactly what's been
built, what's been found and fixed along the way (including the real
hardware findings above), and what's still open (wifi/BLE hardware
verification; the PyPI release workflow's actual upload step, pending a
real token).
