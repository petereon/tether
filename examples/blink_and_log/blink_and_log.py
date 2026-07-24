"""tether example: blink an onboard LED from the PC, with progress logged
back from the MCU on every blink.

Run directly on your PC:

    python blink_and_log.py

tether slices out the @mcu.export-decorated code below, uploads it to the
board over serial, and this same file's `if __name__ == "__main__":` block
then drives it as a normal PC script.

Hardware note: LED_PIN=2 is the common onboard LED pin on many ESP32 dev
boards - check your board's pinout and adjust if it doesn't light up.

Design note (important if you're writing your own MCU-side functions):
hardware objects (Pin, ADC, etc.) must be constructed *inside* an
@mcu.export function body, not at module level. This file runs as a plain
script on your PC too (that's the whole "single file" pitch) - a top-level
`from machine import Pin` or `led = Pin(2, Pin.OUT)` would execute on your
PC as well, where `machine` doesn't exist, and would crash immediately,
before mcu.connect() is ever reached. Constructing hardware objects lazily
inside the function body (as below) sidesteps this entirely: the function
body only ever actually runs on the MCU, never on the PC.
"""

import time

from tether import mcu, pc

_led = None


def _get_led():
    global _led
    if _led is None:
        from machine import Pin

        _led = Pin(2, Pin.OUT)
    return _led


@pc.export
def log_progress(blink_number: int, total: int) -> None:
    print(f"  blink {blink_number}/{total}")


@mcu.export
async def blink(times: int) -> None:
    led = _get_led()
    for i in range(times):
        led.value(1)
        time.sleep_ms(150)
        led.value(0)
        time.sleep_ms(150)
        await log_progress(i + 1, times)


if __name__ == "__main__":
    board = mcu.connect("serial:auto")
    print("Connected. Blinking 5 times...")
    board.blink(5)
    print("Done.")
