"""tether example: blink an onboard LED over wifi, with progress logged
back from the MCU on every blink, then reconnect without a physical reset
to prove the board stayed reachable.

Setup (one-time, over serial):

    uv pip install -e ".[cli,serial]"
    tether provision wifi --ssid YOUR_SSID
    tether status                       # prints the board's IP once connected

Run (no serial connection needed from here on):

    export TETHER_WIFI_SECRET=<the secret 'provision wifi' printed>
    python wifi_blink.py <board-ip>

This is the same @mcu.export/@pc.export code the serial example
(examples/blink_and_log/blink_and_log.py) uses - only `mcu.connect()`'s
address and how credentials reach it differ. `mcu.connect("wifi:<ip>")`
slices this file, hash-checks the on-device bundle over a fast `status`
query, and uploads it if missing/stale - no prior serial session required,
unlike raw-REPL-only serial. `board.reconnect()` then succeeds without any
physical reset: the board's boot.py accept-loop re-listens after every
connection ends, which is the whole point of provisioning wifi in the
first place (vs. hard-resetting into raw-REPL every time, per DESIGN.md's
own Disconnection section).

Hardware note: LED_PIN=2 is the common onboard LED pin on many ESP32 dev
boards - check your board's pinout and adjust if it doesn't light up.

Design note (see blink_and_log.py's own docstring for the full version):
hardware objects (Pin, ADC, etc.) must be constructed *inside* an
@mcu.export function body, not at module level, since this file also runs
as a plain PC script.
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
    import sys

    if len(sys.argv) != 2:
        print("Usage: python wifi_blink.py <board-ip>")
        print("Run `tether provision wifi` then `tether status` to get the IP.")
        raise SystemExit(1)
    ip = sys.argv[1]

    # secret=None (the default) reads TETHER_WIFI_SECRET - see connection.py's
    # _connect_wifi. Pass secret="..." here directly instead if you'd rather
    # not use an env var.
    board = mcu.connect(f"wifi:{ip}")
    print(f"Connected over wifi to {ip}. Blinking 5 times...")
    blink(5)  # just a function call - mcu.connect() set the ambient board
    print("Done.")

    print("Reconnecting - no physical reset needed over wifi...")
    board.reconnect()
    blink(3)
    print("Done again, still no reset.")
