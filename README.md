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
    from machine import ADC, Pin  # hardware imports go INSIDE the function
                                   # body - this file also runs directly on
                                   # your PC, where `machine` doesn't exist
    return ADC(Pin(4)).read_u16() / 65535

board = mcu.connect("serial:auto")
print(board.read_temp())
```

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

**This walkthrough has not been run against a real board.** No physical
ESP32 or other MicroPython-capable hardware has been available at any point
during this project's development (stated here for the same reason it's
stated throughout `docs/CHUNKS.md`: every chunk touching serial/wifi/BLE has
been verified as thoroughly as possible without hardware — the real
`micropython` unix-port interpreter, scripted fakes matching MicroPython's
own documented raw-REPL protocol, real TCP sockets, real `pyserial`
`loop://` ports — but none of that is a substitute for a real device). What
*has* been verified: the example's source slices correctly, the generated
on-device bundle is syntactically valid, and its registration/dispatch
wiring runs correctly under the real MicroPython interpreter with only
`machine.Pin` faked out (see `tests/test_examples.py`). If you try this on
real hardware and something in this walkthrough is wrong, that's the
expected failure mode this note exists to warn about — please open an
issue.

## Status

Chunks 1–14 of `docs/CHUNKS.md`'s roadmap are implemented and tested
(slicing, marshalling, dispatch, all three transports, connection
orchestration, multi-board/reconnect handling). Chunk 15 (this walkthrough)
is feature-complete but hardware-unverified, per above. CI and a PyPI
release workflow (chunks 16–17) are not yet built. See `docs/DESIGN.md` for
the full locked architecture and `docs/CHUNKS.md` for exactly what's been
built, what's been found and fixed along the way, and what's still open.
