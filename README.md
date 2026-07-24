# tether

Write MicroPython and Python in one file. Call MCU functions from your PC
script, and PC functions from your MCU code, like ordinary Python calls —
`tether` handles slicing, upload, and wire marshalling over serial, wifi, or
BLE.

```python
from tether import mcu, pc

@mcu.export
def read_temp() -> float:
    return adc.read() / 100

@pc.export
def log_event(msg: str) -> None:
    print("MCU says:", msg)

board = mcu.connect("serial:auto")
print(board.read_temp())
```

Status: pre-implementation scaffold. See `docs/DESIGN.md` for the full
locked architecture.
