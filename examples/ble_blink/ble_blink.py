"""tether example: blink an onboard LED over BLE, with progress logged
back from the MCU on every blink, then reconnect without a physical reset
to prove the board stayed reachable.

Setup (one-time, over serial):

    uv pip install -e ".[cli,serial,ble]"
    tether provision ble
    # prints the board's BLE MAC address and a shared secret - save both.

Run (no serial connection needed from here on):

    export TETHER_BLE_SECRET=<the secret 'provision ble' printed>
    python ble_blink.py <board-address>

**macOS note:** CoreBluetooth hides a device's real BLE MAC address from
apps for privacy, exposing a randomized per-app UUID instead - on macOS,
<board-address> is the UUID a BLE scan reports (e.g.
`bleak.BleakScanner.discover()`), not the MAC `tether provision ble`
printed. On Linux/BlueZ, the printed MAC works as-is.

This is the same @mcu.export/@pc.export code the serial example
(examples/blink_and_log/blink_and_log.py) uses - only `mcu.connect()`'s
address and how credentials reach it differ. `mcu.connect("ble:<addr>")`
slices this file, hash-checks the on-device bundle, and uploads it if
missing/stale - no prior serial session required. Unlike wifi (a fresh
connection per mode), BLE connection setup is comparatively expensive, so
status/upload/run here all reuse a **single** BLE connection instead - see
DESIGN.md's Transports table. `board.reconnect()` still works without any
physical reset: the board's boot.py resumes advertising after the
connection ends, which is the whole point of provisioning BLE in the
first place (vs. hard-resetting into raw-REPL every time).

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
        print("Usage: python ble_blink.py <board-address>")
        print("Run `tether provision ble` to get the address (see macOS note above).")
        raise SystemExit(1)
    address = sys.argv[1]

    # secret=None (the default) reads TETHER_BLE_SECRET - see connection.py's
    # _connect_ble. Pass secret="..." here directly instead if you'd rather
    # not use an env var.
    board = mcu.connect(f"ble:{address}")
    print(f"Connected over BLE to {address}. Blinking 5 times...")
    blink(5)  # just a function call - mcu.connect() set the ambient board
    print("Done.")

    print("Reconnecting - no physical reset needed over BLE...")
    board.reconnect()
    blink(3)
    print("Done again, still no reset.")
