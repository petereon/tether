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
import secrets

from tether.connection import PROTOCOL_VERSION
from tether.transports.wifi import DEFAULT_PORT

# Fixed template - never contains credentials or the shared secret (those
# live in /tether_wifi.json, uploaded separately - see generate_wifi_boot).
# If /tether_wifi.json is missing, this does nothing and falls straight
# through to the idle REPL: a never-provisioned board behaves exactly as
# it did before this feature existed.
#
# Loops indefinitely once wifi is up, accepting connections one at a time
# (never concurrently). Every connection starts with a small preamble
# (JSON, not msgpack - see this module's own note below) selecting a mode
# and presenting the shared secret if one is configured. All three modes
# - "status", "run", and "upload" - are implemented. An unrecognized mode
# gets a clean rejection, not a crash.
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
        import gc as _gc

        _boot_ms = time.ticks_ms()
        _MAX_CTRL_FRAME = 65536

        def _recv_exact(_conn, _n):
            _buf = b""
            while len(_buf) < _n:
                _chunk = _conn.recv(_n - len(_buf))
                if not _chunk:
                    raise OSError("connection closed")
                _buf += _chunk
            return _buf

        def _read_json_frame(_conn):
            _header = _recv_exact(_conn, 4)
            _length = int.from_bytes(_header, "big")
            if _length > _MAX_CTRL_FRAME:
                raise OSError("control frame too large")
            _body = _recv_exact(_conn, _length)
            return _json.loads(_body)

        def _send_json_frame(_conn, _obj):
            _body = _json.dumps(_obj).encode()
            _conn.send(len(_body).to_bytes(4, "big") + _body)

        def _handle_status(_conn):
            _hash = None
            try:
                with open("/.tether_hash") as _hf:
                    _hash = _hf.read()
            except OSError:
                pass
            _send_json_frame(_conn, {{
                "protocol_version": {PROTOCOL_VERSION},
                "tether_app_hash": _hash,
                "free_heap": _gc.mem_free(),
                "uptime_ms": time.ticks_diff(time.ticks_ms(), _boot_ms),
                "ip": _wlan.ifconfig()[0],
            }})

        def _handle_run(_conn):
            try:
                with open("/tether_app.py") as _f:
                    _tether_app_src = _f.read()
            except OSError:
                _tether_app_src = None

            if _tether_app_src is not None:
                import uasyncio as _asyncio

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
                    print(
                        "tether: wifi client disconnected - run session "
                        "ending (reconnect any time, no reset needed)"
                    )
                finally:
                    # The dominant duplication mechanism (found during final
                    # review, distinct from and more serious than the
                    # mcu_decorators registry fix above): MicroPython's
                    # uasyncio task queue is a process-global structure, and
                    # asyncio.run() returning/raising does NOT drain tasks
                    # queued via asyncio.create_task() inside it - which is
                    # exactly what Dispatcher.run() does for every
                    # @mcu.loop function. Without this, a previous run-mode
                    # session's loop task(s) stay alive in the global queue
                    # and get resumed alongside the new session's own task
                    # the next time asyncio.run() is called here - an
                    # accumulating, not replacing, duplicate every
                    # reconnect. new_event_loop() resets that global queue
                    # between sessions, whether this session ended via the
                    # expected disconnect exception or any other way.
                    # Verified against the real interpreter: without this,
                    # three successive sessions' sampled tick counts grew
                    # non-linearly (each session ticking faster than the
                    # last, from N accumulating duplicate loop tasks); with
                    # it, each session's tick count stays roughly constant.
                    _asyncio.new_event_loop()

        def _handle_upload(_conn):
            try:
                _manifest = _read_json_frame(_conn)
                for _d in _manifest.get("dirs", []):
                    try:
                        import uos as _uos

                        _uos.mkdir(_d)
                    except OSError:
                        pass
                for _file_meta in _manifest.get("files", []):
                    _path = _file_meta["path"]
                    _size = _file_meta["size"]
                    _remaining = _size
                    with open(_path, "wb") as _wf:
                        while _remaining > 0:
                            _header = _recv_exact(_conn, 4)
                            _chunk_len = int.from_bytes(_header, "big")
                            if _chunk_len > _MAX_CTRL_FRAME:
                                raise OSError("upload chunk too large")
                            _chunk_data = _recv_exact(_conn, _chunk_len)
                            _wf.write(_chunk_data)
                            _remaining -= len(_chunk_data)
                _send_json_frame(_conn, {{"ok": True}})
            except Exception as _exc:
                try:
                    _send_json_frame(_conn, {{"ok": False, "error": str(_exc)}})
                except OSError:
                    pass

        _addr = _socket.getaddrinfo("0.0.0.0", {DEFAULT_PORT})[0][-1]
        _srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        _srv.bind(_addr)
        _srv.listen(4)

        while True:
            _conn, _client_addr = _srv.accept()
            try:
                _preamble = _read_json_frame(_conn)
                _mode = _preamble.get("mode")
                _secret = _preamble.get("secret")
                _expected_secret = _cfg.get("secret")
                if _expected_secret is not None and _secret != _expected_secret:
                    _send_json_frame(_conn, {{"ok": False, "error": "auth failed"}})
                elif _mode == "status":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_status(_conn)
                elif _mode == "run":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_run(_conn)
                elif _mode == "upload":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_upload(_conn)
                else:
                    _send_json_frame(_conn, {{"ok": False, "error": "unknown mode"}})
            except Exception:
                pass
            finally:
                try:
                    _conn.close()
                except OSError:
                    pass
"""

# Run via serial.run_python() by the CLI's `status` command. Structured
# output (one JSON line) rather than freeform prints - robust to parse on
# the PC side, no fragile string matching.
STATUS_SCRIPT = b"""\
try:
    import ujson as _json
except ImportError:
    import json as _json
try:
    import uos as _uos
except ImportError:
    import os as _uos

_provisioned = "tether_wifi.json" in _uos.listdir("/")
_connected = False
_ip = None
try:
    import network
    import time

    _wlan = network.WLAN(network.STA_IF)
    if _provisioned:
        # WLAN.status() and its numeric codes are ESP32-specific, not part
        # of MicroPython's portable network.WLAN API - only isconnected()
        # is portable. Only attempt the status()-based fast-exit when the
        # named STAT_IDLE/STAT_CONNECTING constants actually exist on this
        # port - guessing a numeric "in progress" range (e.g. 1000/1001)
        # would misfire on a port with a different scheme (falsely
        # treating an in-progress state as terminal and reporting "not
        # connected" too early, the exact bug this poll exists to avoid).
        # If either constant is missing, skip the fast-exit and fall back
        # to the plain isconnected()-only wait for the full deadline.
        # isconnected() stays the ground truth reported below either way;
        # status() (when usable) only decides when to stop polling early.
        _idle = getattr(network, "STAT_IDLE", None)
        _connecting = getattr(network, "STAT_CONNECTING", None)
        # 8000ms here must stay comfortably under cli.py's status_command
        # run_python(..., timeout=10.0) - that timeout also has to cover
        # reset_board() and the raw-REPL round trip on top of this wait,
        # so don't raise one without checking the other has headroom left.
        _deadline = time.ticks_add(time.ticks_ms(), 8000)
        while not _wlan.isconnected() and time.ticks_diff(_deadline, time.ticks_ms()) > 0:
            if _idle is not None and _connecting is not None:
                try:
                    _st = _wlan.status()
                    if _st != _idle and _st != _connecting:
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


def generate_wifi_boot(
    ssid: str, password: str, *, secret: str | None = None, danger_unauthenticated: bool = False
) -> dict[str, bytes]:
    """Return `{"/boot.py": ..., "/tether_wifi.json": ...}` file contents
    for `tether provision-wifi` to upload. `boot.py` is a fixed template
    (see `_BOOT_PY_TEMPLATE`'s own docstring) - only the config file
    contains credentials and the shared secret, so re-provisioning with
    new ones is a config-file-only re-upload.

    `secret`: explicit secret to use (mainly for tests - real callers
    should let this generate one). `danger_unauthenticated`: when True,
    no secret is stored at all, regardless of `secret` - the on-device
    listener accepts any connection with no auth check. Every call
    generates a fresh random secret by default (unless overridden or
    unauthenticated), so re-provisioning naturally rotates it.
    """
    config: dict[str, str] = {"ssid": ssid, "password": password}
    if not danger_unauthenticated:
        config["secret"] = secret if secret is not None else secrets.token_hex(16)
    files = {
        "/boot.py": _BOOT_PY_TEMPLATE.encode(),
        "/tether_wifi.json": json.dumps(config).encode(),
    }
    return files
