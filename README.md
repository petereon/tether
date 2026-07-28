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

Targets ESP32 and similar MicroPython-capable boards. Serial, wifi, and BLE
(via the `tether` CLI's provisioning step) all work against real hardware
today, including `mcu.connect("wifi:<ip>")`/`mcu.connect("ble:<addr>")`
themselves — a full PC-to-MCU call, an MCU-to-PC reverse call, and
remote-exception propagation were all verified end-to-end against a real
ESP32 over each transport. See Transports below.

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
uv pip install -e ".[serial]"   # or "[ble]" for Bluetooth
# connecting over wifi (mcu.connect("wifi:<ip>")) needs no extra - pure
# stdlib socket. Provisioning a board for wifi needs "[cli]" - see below.
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
| Serial | `"serial:auto"` (USB auto-discovery) or an explicit port | Works today. Pushes code over MicroPython's raw REPL — the only transport that works on a completely unprovisioned board (wifi/BLE need `tether provision-wifi`/`tether provision-ble` first; see below). |
| Wifi | `"wifi:<ip>"` | **Works today**, verified against real ESP32 hardware, once a board has been provisioned with the `tether` CLI (`tether provision-wifi`, see below). **Now pushes code automatically** — `mcu.connect("wifi:<ip>")` slices, hash-checks, and uploads (if needed) before running, the same as serial does, no prior serial session required. **Authenticated by default** — every connection needs a shared secret (`tether provision-wifi` generates and prints one; pass it via `mcu.connect(secret=...)` or the `TETHER_WIFI_SECRET` env var) unless the board was provisioned with `--danger-unauthenticated`. `tether status --ip <ip>` is now fast and non-destructive (no reset). `board.reconnect()` now works over wifi too — sequential only, one connection at a time. Both the CLI (`provision-wifi`/`status`/`unprovision-wifi`) and an actual `mcu.connect("wifi:<ip>")` session — a PC-to-MCU call, an MCU-to-PC reverse call, and remote-exception propagation — were run end-to-end against a real board. Credentials and the shared secret are both stored in plaintext on-device (`/tether_wifi.json`) — the only realistic option on this hardware class, no secure storage exists. See "WiFi provisioning CLI" below for the full picture. |
| BLE | `"ble:<addr>"` | **Works today**, verified against real ESP32 hardware, once a board has been provisioned with `tether provision-ble`. Same code-push/auth/status model as wifi (shared secret, `--danger-unauthenticated`, `tether status --ble-addr <addr>`), but reuses **one** BLE connection across status/upload/run instead of opening a fresh one per mode — connection setup is comparatively expensive over BLE. Wifi and BLE are mutually exclusive on one board (provisioning one warns before overwriting the other's `boot.py` — a board only auto-runs one at a time). **macOS note:** CoreBluetooth hides real BLE MAC addresses from apps for privacy; `mcu.connect("ble:<addr>")` on macOS needs the randomized UUID a BLE scan reports, not the MAC `tether provision-ble` prints (which is correct and usable as-is on Linux/BlueZ). |

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

## WiFi & BLE provisioning CLI

Install the `tether[cli]` extra to get the `tether` console script:

```bash
uv pip install -e ".[cli]"
```

```
tether devices                                       # list connected boards
tether provision-wifi --ssid SSID [--password PW]     # upload boot.py + credentials + a secret
tether provision-ble [--danger-unauthenticated]       # upload boot.py + a secret, advertises the board
tether status [--ip IP] [--secret S] [--ble-addr A] [--ble-secret S]  # check provisioned/connected state
tether unprovision-wifi                               # remove stored credentials
```

`--port` is optional everywhere — if more than one known device is
connected and `--port` is omitted, you're prompted interactively to pick
one. `--password` is prompted for (hidden input) if omitted from
`provision-wifi`. `unprovision-wifi` asks for confirmation first, since it
kills the board's wifi reachability (it only removes the stored
credentials — the uploaded `boot.py` itself stays, harmlessly, and does
nothing without them). `provision-wifi` uploads a small `boot.py` that
auto-connects to wifi on every boot and, once connected, loops
indefinitely accepting connections (one at a time) that push code, run
it, or report status — after it finishes, connect from Python with
`mcu.connect("wifi:<ip>")`, using the IP `tether status` reports.

**`provision-wifi` prints a secret — save it, or you can't connect.** By
default every `provision-wifi` run generates a fresh random shared secret
and prints it once, right after the IP-related output:

```
Provisioned /dev/cu.usbserial-0001 for wifi network 'MyNetwork'. Board is restarting.
Shared secret (save this - needed to connect): 3f9a1c...
Run `tether status` in a few seconds to check connectivity.
```

There is no way to recover this secret later short of re-provisioning
(which rotates it again). Pass it to `mcu.connect("wifi:<ip>",
secret="...")`, or set it once as an environment variable so you don't
have to pass it every time:

```bash
export TETHER_WIFI_SECRET=3f9a1c...
```

A wrong or missing secret raises `tether.WifiAuthError`, not a generic
timeout — if you see that, double-check the secret you saved. If you
deliberately don't want authentication (e.g. a trusted, isolated bench
network), pass `--danger-unauthenticated` to `provision-wifi`: no secret
is generated, and the board's listener accepts any connection from
anyone on the network. This prints a loud warning but does not prompt for
confirmation, so it stays scriptable.

**`tether status --ip <ip>` is now the fast, non-destructive path.**
Given `--ip` (and `--secret`/`TETHER_WIFI_SECRET` if the board is
authenticated), `status` talks directly to the board's wifi listener —
no reset, no interruption of anything it's doing. It only falls back to
the older, serial-based raw-REPL diagnostic (which does reset the board)
if the wifi connection attempt itself fails — meaning wifi never came up
in the first place (bad password, out of range), not that the board is
merely busy. Without `--ip`, `status` always uses the serial fallback
directly, same as before.

**Code push works automatically now.** `mcu.connect("wifi:<ip>")` slices
your script, checks the on-device bundle hash (via a fast `status`-mode
query), and uploads the full bundle first if it's missing or stale —
the same slice → hash-check → upload-if-needed → run flow serial has
always done. A prior serial `mcu.connect(...)` session is no longer a
prerequisite for connecting over wifi.

**Reconnecting works too.** `board.reconnect()` now succeeds over wifi
(the board's accept-loop re-listens after each connection ends) — it's
still sequential-only, one connection at a time, so you can't check
`status` while a `run` session is actively live, only in between.

### BLE

`tether provision-ble [--danger-unauthenticated]` is the BLE equivalent of
`provision-wifi` — no network credentials needed (BLE just advertises),
but everything else matches: a fresh shared secret generated and printed
by default (`mcu.connect(secret=...)` or `TETHER_BLE_SECRET` env var;
`tether.WifiAuthError` on a bad/missing one, reused as-is for BLE — same
exception class, both transports), `tether status --ble-addr <addr>
[--ble-secret ...]` as the fast non-destructive path, and full code push
(`mcu.connect("ble:<addr>")` slices/hash-checks/uploads exactly like wifi).

**One real difference from wifi:** BLE connection setup is comparatively
expensive, so `status`/`upload`/`run` all reuse a **single** BLE
connection instead of wifi's one-connection-per-mode — only `run`
finishing, an auth failure, or an unrecognized mode ends the session.

**Wifi and BLE are mutually exclusive on one board** — MicroPython only
auto-runs one `/boot.py`, so provisioning one warns (doesn't block) before
overwriting the other's; the other's credentials file is left in place but
nothing reads it anymore.

**macOS-specific:** CoreBluetooth hides a device's real BLE MAC address
from apps for privacy, exposing a randomized per-app UUID instead.
`provision-ble` prints the board's real MAC (correct and directly usable
on Linux/BlueZ) — on macOS, get the UUID to connect with from a BLE scan
(e.g. `bleak.BleakScanner.discover()`) instead.

## Status

Core functionality (slicing, the wire protocol, dispatch, multi-board and
reconnect handling) is implemented and tested. Serial is fully working and
has been verified against real ESP32 hardware, including reconnecting and
re-running repeatedly.

Wifi now works against real hardware end-to-end, including code push,
shared-secret auth, non-interrupting status, and reconnect:
`tether provision-wifi`, `tether status`, and `tether unprovision-wifi`
were all run against a real ESP32, and a real `mcu.connect("wifi:<ip>")`
session was verified too — a PC-to-MCU call, an MCU-to-PC reverse call,
and remote-exception propagation all confirmed working over the socket
bridge. See the Transports table above and "WiFi provisioning CLI" for
the full picture and wifi's remaining, accepted limitations (sequential
connections only, plaintext credentials/secret on-device, no
TLS/challenge-response auth).

BLE now works against real hardware end-to-end too, including code push,
shared-secret auth, non-interrupting status, and reconnect:
`tether provision-ble` and `tether status --ble-addr` were run against a
real ESP32, and a real `mcu.connect("ble:<addr>")` session was verified —
upload+run, script-edit propagation, `board.reconnect()` three times in a
row with no physical reset (`@mcu.loop` tasks confirmed not to accumulate),
auth rejection, and `--danger-unauthenticated`. Unlike wifi, BLE reuses one
physical connection across status/upload/run rather than opening a fresh
one per mode. See the Transports table above and `docs/DESIGN.md` for the
full architecture and BLE's remaining, accepted limitations (sequential
connections only, plaintext shared secret, no BLE pairing/bonding, no
concurrent wifi+BLE on one board).
