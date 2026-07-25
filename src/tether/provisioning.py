"""WiFi provisioning: generates the on-device boot.py + credentials file
that make a board reachable over tether's wifi transport, and the status
diagnostic script the CLI's `status` command runs.

See docs/superpowers/specs/2026-07-25-wifi-upload-design.md for the full
design. Every mechanism `_BOOT_PY_TEMPLATE` relies on (socket bind via
getaddrinfo, uasyncio wrapping a raw accepted socket, exec() with an
injected namespace reaching a synchronously-invoked asyncio.run()) was
verified against the real `micropython` unix-port interpreter before this
was written, not assumed - see this task's test for the same mechanism
exercised for real.
"""

from __future__ import annotations

import json

from tether.transports.wifi import DEFAULT_PORT

# Fixed template - never contains credentials (those live in
# /tether_wifi.json, uploaded separately - see generate_wifi_boot). If
# /tether_wifi.json is missing, this does nothing and falls straight
# through to the idle REPL: a never-provisioned board behaves exactly as
# it did before this feature existed.
_BOOT_PY_TEMPLATE = f"""\
try:
    import ujson as _json
except ImportError:
    import json as _json

try:
    with open("/tether_wifi.json") as _f:
        _cfg = _json.loads(_f.read())
except OSError:
    _cfg = None

if _cfg is not None:
    import network
    import time

    _wlan = network.WLAN(network.STA_IF)
    _wlan.active(True)
    if not _wlan.isconnected():
        _wlan.connect(_cfg["ssid"], _cfg["password"])
        _deadline = time.ticks_add(time.ticks_ms(), 15000)
        while not _wlan.isconnected() and time.ticks_diff(_deadline, time.ticks_ms()) > 0:
            time.sleep_ms(100)

    if _wlan.isconnected():
        import usocket as _socket

        _addr = _socket.getaddrinfo("0.0.0.0", {DEFAULT_PORT})[0][-1]
        _srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        _srv.bind(_addr)
        _srv.listen(1)
        _conn, _client_addr = _srv.accept()
        _srv.close()

        try:
            with open("/tether_app.py") as _f:
                _tether_app_src = _f.read()
        except OSError:
            _tether_app_src = None

        if _tether_app_src is not None:
            import uasyncio as _asyncio

            # The accepted connection is this boot's only listener - by
            # design (see docs/superpowers/specs, "Explicitly out of
            # scope") there is no re-listen once it drops; getting a fresh
            # one needs a physical reset or a new provision-wifi run. So
            # once the dispatch loop's stream hits EOF (peer disconnected)
            # it raises up through here - let that end this boot.py run
            # quietly rather than as an unhandled exception, since a
            # dropped wifi client is an expected, not exceptional, way for
            # a single-shot listener's one connection to end.
            try:
                exec(
                    _tether_app_src,
                    {{
                        "_tether_stream_override": (
                            _asyncio.StreamReader(_conn),
                            _asyncio.StreamWriter(_conn, {{}}),
                        )
                    }},
                )
            except (OSError, EOFError):
                pass
"""

# Run via serial.run_python() by the CLI's `status` command. Structured
# output (one JSON line) rather than freeform prints - robust to parse on
# the PC side, no fragile string matching.
STATUS_SCRIPT = b"""\
import ujson as _json
import uos as _uos

_provisioned = "tether_wifi.json" in _uos.listdir("/")
_connected = False
_ip = None
try:
    import network
    import time

    _wlan = network.WLAN(network.STA_IF)
    if _provisioned:
        _deadline = time.ticks_add(time.ticks_ms(), 8000)
        while not _wlan.isconnected() and time.ticks_diff(_deadline, time.ticks_ms()) > 0:
            # WLAN.status() and its numeric codes are ESP32-specific, not
            # part of MicroPython's portable network.WLAN API - only
            # isconnected() is portable. Where status() is available it
            # reaches a terminal value (anything other than "idle"/1000 or
            # "connecting"/1001) as soon as the outcome - success or
            # failure - is decided, well before isconnected() would time
            # out waiting the full deadline. isconnected() stays the
            # ground truth reported below either way; status() is only
            # used to stop polling early. If .status() doesn't exist or
            # raises, fall back to the plain isconnected()-only wait.
            try:
                _st = _wlan.status()
                if _st < 1000 or _st > 1001:
                    break
            except Exception:
                pass
            time.sleep_ms(200)
    _connected = _wlan.isconnected()
    _ip = _wlan.ifconfig()[0] if _connected else None
except Exception:
    pass
print(_json.dumps({"provisioned": _provisioned, "connected": _connected, "ip": _ip}))
"""


def generate_wifi_boot(ssid: str, password: str) -> dict[str, bytes]:
    """Return `{"/boot.py": ..., "/tether_wifi.json": ...}` file contents
    for `tether provision-wifi` to upload. `boot.py` is a fixed template
    (see `_BOOT_PY_TEMPLATE`'s own docstring) - only the config file
    contains credentials, so re-provisioning with new ones is a
    config-file-only re-upload.
    """
    config = json.dumps({"ssid": ssid, "password": password}).encode()
    return {
        "/boot.py": _BOOT_PY_TEMPLATE.encode(),
        "/tether_wifi.json": config,
    }
