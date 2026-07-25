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

Targets ESP32 and similar MicroPython-capable boards. Serial and wifi (via
the `tether` CLI's provisioning step) both work against real hardware
today, including `mcu.connect("wifi:<ip>")` itself — a full PC-to-MCU call,
an MCU-to-PC reverse call, and remote-exception propagation were all
verified end-to-end against a real ESP32. BLE is planned but not yet usable
against a real device — see Transports below.

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
| Serial | `"serial:auto"` (USB auto-discovery) or an explicit port | Works today. The only transport that can push code to the board (over MicroPython's raw REPL). |
| Wifi | `"wifi:<ip>"` | **Works today**, verified against real ESP32 hardware, once a board has been provisioned with the `tether` CLI (`tether provision-wifi`, see below). Both the CLI (`provision-wifi`/`status`/`unprovision-wifi`) and an actual `mcu.connect("wifi:<ip>")` session — a PC-to-MCU call, an MCU-to-PC reverse call, and remote-exception propagation — were run end-to-end against a real board. Requires a `tether_app.py` already on the board from a prior serial `mcu.connect(...)` session (wifi never pushes code, only serial does — see the note in "WiFi provisioning CLI" below). A provisioned board opens an **unauthenticated** TCP listener on every boot and accepts exactly one connection per boot cycle — anyone on the same network can connect first; if the connection drops, the board does not re-listen and needs a physical reset or a fresh `tether provision-wifi` run. **Checking `tether status` also drops the current listener** (see "WiFi provisioning CLI" below) — don't check status right before connecting; connect directly after `provision-wifi` instead. Credentials are stored in plaintext on-device (`/tether_wifi.json`) — the only realistic option on this hardware class, no secure storage exists. |
| BLE | `"ble:<addr>"` | **Not usable against a real device yet**, same reason as wifi used to be — no on-device BLE listener exists. Planned. |

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

## WiFi provisioning CLI

Install the `tether[cli]` extra to get the `tether` console script:

```bash
uv pip install -e ".[cli]"
```

```
tether devices                                     # list connected boards
tether provision-wifi --ssid SSID [--password PW]   # upload boot.py + credentials
tether status                                       # check provisioned/connected state
tether unprovision-wifi                             # remove stored credentials
```

`--port` is optional everywhere — if more than one known device is
connected and `--port` is omitted, you're prompted interactively to pick
one. `--password` is prompted for (hidden input) if omitted from
`provision-wifi`. `unprovision-wifi` asks for confirmation first, since it
kills the board's wifi reachability (it only removes the stored
credentials — the uploaded `boot.py` itself stays, harmlessly, and does
nothing without them). `provision-wifi` uploads a small `boot.py` that
auto-connects to wifi on every boot and bridges an accepted connection
into the same dispatch loop serial uses — after it finishes, connect from
Python with `mcu.connect("wifi:<ip>")`, using the IP `tether status`
reports.

**Don't check `tether status` right before connecting — check it, note
the IP, then connect directly next time instead.** `status` works by
hard-resetting the board and interrupting whatever `boot.py` is doing to
ask it questions, then leaves the board sitting at the plain interactive
REPL rather than triggering a fresh reset afterward. Since `boot.py` only
runs automatically on an actual reset (not on a raw-REPL exit), the
listener it had opened is gone once `status` returns — even though
`status` just reported "connected". A `mcu.connect("wifi:<ip>")` attempt
right after a `status` check will time out. This was found by hardware
testing, not anticipated in the original design; workaround for now is to
avoid interleaving `status` checks with connection attempts (re-run
`provision-wifi` to get a fresh, connectable boot cycle if you need to
check status again). Fixing `status` to leave the board connectable
afterward (e.g. resetting again after the query, matching
`provision-wifi`'s own double-reset pattern) is a reasonable follow-up,
not yet done.

**Prerequisite:** wifi never pushes code — only serial does. `boot.py`
bridges an accepted wifi connection into whatever `tether_app.py` is
already on the board, and that file is only ever written by a normal
serial `mcu.connect(...)` session. Run your script once over serial
(`mcu.connect("serial:auto")`) *before* connecting to the same board over
wifi, so there's something on-device for the wifi connection to reach —
otherwise the board accepts the connection and then has nothing to run,
and the PC side just times out waiting for a handshake reply. Also note:
a wifi connection runs whatever `tether_app.py` was uploaded *last* over
serial — there's no hash-check on the wifi path, so editing your script
and reconnecting over wifi without an intervening serial run will
silently run stale code.

## Status

Core functionality (slicing, the wire protocol, dispatch, multi-board and
reconnect handling) is implemented and tested. Serial is fully working and
has been verified against real ESP32 hardware, including reconnecting and
re-running repeatedly.

Wifi now works against real hardware end-to-end: `tether provision-wifi`,
`tether status`, and `tether unprovision-wifi` were all run against a real
ESP32, and a real `mcu.connect("wifi:<ip>")` session was verified too —
a PC-to-MCU call, an MCU-to-PC reverse call, and remote-exception
propagation all confirmed working over the socket bridge. See the
Transports table above and "WiFi provisioning CLI" for wifi's current
limitations (one connection per boot, unauthenticated listener, plaintext
credentials, requires a prior serial run, and `status` dropping the
current listener — found during this hardware verification pass).

BLE is PC-side client code only, covered by automated tests (a fake
matching the BLE library's API) — there is currently no on-device BLE
listener to connect to, so it doesn't work against a real board yet. See
`docs/DESIGN.md` for the full architecture.
