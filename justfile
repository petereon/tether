# Hardware helpers for ESP32 dev boards. Requires `esptool` (pip install
# esptool) and `uv` on PATH; run from the repo root.

firmware_url := "https://micropython.org/resources/firmware/ESP32_GENERIC-20260406-v1.28.0.bin"
firmware_file := ".cache/firmware/ESP32_GENERIC-20260406-v1.28.0.bin"

# Erase flash and write a fresh generic-ESP32 MicroPython image. Wipes
# everything on the board's filesystem, including any tether wifi
# provisioning. Re-run `tether provision wifi` afterwards.
reflash port="/dev/cu.wchusbserial110":
    mkdir -p .cache/firmware
    [ -f {{firmware_file}} ] || curl -fL -o {{firmware_file}} {{firmware_url}}
    esptool --port {{port}} erase-flash
    esptool --port {{port}} write-flash 0x1000 {{firmware_file}}
