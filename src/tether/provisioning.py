"""Provisioning: generates the on-device boot.py (+ config file) that makes
a board reachable over tether's wifi and BLE transports, and the status
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
import textwrap

from tether.connection import PROTOCOL_VERSION
from tether.transports.wifi import DEFAULT_PORT

# Shared between wifi's _BOOT_PY_TEMPLATE and BLE's _BLE_BOOT_TEMPLATE
# (provisioning.py) - the mode-handling logic itself (status/upload/run)
# does not depend on which transport delivered the connection, only on
# the connection object exposing .recv(n)/.send(data) matching a real
# socket's contract (see each template's own _conn adapter). _handle_run
# is the one exception: it takes an extra _make_streams(_conn) parameter
# instead of hardcoding uasyncio.StreamReader/Writer(_conn) directly,
# since that constructor requires a real socket at the native level,
# which a BLE connection fundamentally isn't - wifi's _make_streams wraps
# a real socket (trivial); BLE's constructs a custom async adapter (see
# _BLE_BOOT_TEMPLATE).
#
# Zero-indented here; each template embeds it via textwrap.indent() at
# its own nesting depth, so this text is never duplicated or hand-kept
# in sync between the two call sites.
_MODE_HANDLER_FUNCTIONS_SRC = f"""\
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

def _hexlify(_data):
    return "".join("{{:02x}}".format(_b) for _b in _data)

def _hmac_sha256(_key, _msg):
    # Hand-rolled: MicroPython has hashlib.sha256 but no hmac module.
    # Verified byte-for-byte against CPython's real hmac.new(key, msg,
    # hashlib.sha256).digest() - both locally and on real ESP32 hardware
    # via raw-REPL - before this was written.
    #
    # \\x00 (double backslash), not \x00: this text lives inside
    # _MODE_HANDLER_FUNCTIONS_SRC, itself an f-string - a single \x00 here
    # gets eagerly interpreted by CPython at THIS module's import time into
    # a raw embedded NUL byte baked into the generated boot.py's source
    # text, which MicroPython's own parser then mis-tokenizes (confirmed:
    # a literal NUL byte sitting in source inside a byte-string literal
    # parses to byte value 1, not 0, under the real micropython unix-port
    # interpreter - not the same bug as a normal \x00 escape). Double-
    # escaping here means the GENERATED file's source text carries the
    # literal two-character escape sequence, which MicroPython correctly
    # re-interprets as a single NUL byte when it parses that file.
    _block_size = 64
    if len(_key) > _block_size:
        _key = _hashlib.sha256(_key).digest()
    _key = _key + b"\\x00" * (_block_size - len(_key))
    _o_key_pad = bytes(_b ^ 0x5C for _b in _key)
    _i_key_pad = bytes(_b ^ 0x36 for _b in _key)
    _inner = _hashlib.sha256(_i_key_pad + _msg).digest()
    return _hashlib.sha256(_o_key_pad + _inner).digest()

def _envelope_wrap(_session_key, _counter, _inner, _tag_length):
    _counter_bytes = _counter.to_bytes(4, "big")
    _tag = _hmac_sha256(_session_key, _counter_bytes + _inner)[:_tag_length]
    _body = _counter_bytes + _inner + _tag
    return len(_body).to_bytes(4, "big") + _body

def _envelope_unwrap(_session_key, _expected_counter, _body, _tag_length):
    if len(_body) < 4 + _tag_length:
        raise OSError("authenticated frame too short")
    _counter_bytes = _body[:4]
    _tag = _body[-_tag_length:]
    _inner = _body[4:-_tag_length]
    _counter = int.from_bytes(_counter_bytes, "big")
    if _counter != _expected_counter:
        raise OSError("unexpected frame counter")
    _expected_tag = _hmac_sha256(_session_key, _counter_bytes + _inner)[:_tag_length]
    if _tag != _expected_tag:
        raise OSError("frame authentication tag mismatch")
    return _inner

def _recv_frame(_conn, _session_key, _counter):
    # Reads one frame - authenticated envelope-wrapped if `_session_key`
    # is given, or the plain unauthenticated [4-byte length][bytes] format
    # otherwise (matching _read_json_frame's own wire shape - this is used
    # for the same control channel, just below the JSON layer). Returns
    # (frame_bytes, next_counter); next_counter equals _counter unchanged
    # when _session_key is None.
    #
    # The authenticated branch unwraps ONE extra [4-byte length] layer
    # from the envelope's inner frame before returning - not redundant
    # framing added on a whim, but required to interoperate with the PC
    # side's tether.transports.wifi.send_authenticated_json_frame /
    # send_authenticated_bytes_frame, which each wrap an already
    # length-prefixed blob (_json_frame_bytes()/_bytes_frame_bytes(),
    # matching plain send_json_frame()/send_bytes_frame()'s own wire
    # shape) as the FrameAuthenticator's inner_frame, rather than the bare
    # payload - see read_authenticated_json_frame's matching unwrap on
    # that side. The plain (_session_key is None) branch has no such inner
    # layer, since unauthenticated send_json_frame/read_json_frame only
    # ever carry ONE length prefix total.
    #
    # The two branches bound `_length` differently on purpose. The plain
    # branch's length IS the payload, so _MAX_CTRL_FRAME bounds it
    # directly. The authenticated branch's length is the OUTER envelope,
    # which legitimately runs _ENVELOPE_OVERHEAD bytes larger than the
    # payload it carries (counter + tag + the inner length prefix above) -
    # bounding it at the bare _MAX_CTRL_FRAME would reject a legal
    # max-size upload chunk. Both _TAG_LENGTH and _ENVELOPE_OVERHEAD are
    # defined by BOTH templates that embed this source, with different
    # values (wifi 16/24, BLE 8/16 - see each template's own constants),
    # and BLE reaches the authenticated branch for real, so neither name
    # may be assumed wifi-only here.
    _header = _recv_exact(_conn, 4)
    _length = int.from_bytes(_header, "big")
    if _session_key is None:
        if _length > _MAX_CTRL_FRAME:
            raise OSError("frame too large")
        return _recv_exact(_conn, _length), _counter
    if _length > _MAX_CTRL_FRAME + _ENVELOPE_OVERHEAD:
        raise OSError("frame too large")
    _body = _recv_exact(_conn, _length)
    _inner = _envelope_unwrap(_session_key, _counter, _body, _TAG_LENGTH)
    _inner_length = int.from_bytes(_inner[:4], "big")
    return _inner[4 : 4 + _inner_length], _counter + 1

def _send_frame(_conn, _data, _session_key, _counter):
    # Mirror of _recv_frame for sending. Returns next_counter. See
    # _recv_frame's own comment above for why the authenticated branch
    # adds an extra inner [4-byte length] layer around `_data` before
    # handing it to _envelope_wrap - required to match what
    # read_authenticated_json_frame/read_authenticated_bytes_frame expect
    # to unwrap on the PC side.
    if _session_key is None:
        _conn.send(len(_data).to_bytes(4, "big") + _data)
        return _counter
    _framed = len(_data).to_bytes(4, "big") + _data
    _conn.send(_envelope_wrap(_session_key, _counter, _framed, _TAG_LENGTH))
    return _counter + 1

class _AuthenticatedReader:
    # `_counter` is where this direction's replay counter is PICKED UP, not
    # necessarily 0: on BLE one physical connection carries a whole
    # status/upload/run sequence and the counters run straight through it
    # (see _BLE_BOOT_TEMPLATE's session loop). Wifi passes 0 because a wifi
    # connection only ever serves one mode before closing.
    def __init__(self, _inner, _session_key, _tag_length, _counter):
        self._inner = _inner
        self._session_key = _session_key
        self._tag_length = _tag_length
        self._counter = _counter
        self._pending = b""

    async def readexactly(self, _n):
        while len(self._pending) < _n:
            await self._fill_one_envelope()
        _result = self._pending[:_n]
        self._pending = self._pending[_n:]
        return _result

    async def _fill_one_envelope(self):
        _header = await self._inner.readexactly(4)
        _length = int.from_bytes(_header, "big")
        # + _ENVELOPE_OVERHEAD - see _recv_frame's comment: this bounds
        # the outer envelope, not the payload inside it.
        if _length > _MAX_CTRL_FRAME + _ENVELOPE_OVERHEAD:
            raise OSError("authenticated frame too large")
        _body = await self._inner.readexactly(_length)
        _inner_bytes = _envelope_unwrap(self._session_key, self._counter, _body, self._tag_length)
        self._counter += 1
        self._pending += _inner_bytes

class _AuthenticatedWriter:
    # See _AuthenticatedReader's note on `_counter` above.
    def __init__(self, _inner, _session_key, _tag_length, _counter):
        self._inner = _inner
        self._session_key = _session_key
        self._tag_length = _tag_length
        self._counter = _counter

    def write(self, _data):
        self._inner.write(_envelope_wrap(self._session_key, self._counter, _data, self._tag_length))
        self._counter += 1

    async def drain(self):
        await self._inner.drain()

def _handle_status(_conn, _get_addr, _session_key, _send_counter):
    # Takes the send-direction counter in and hands the advanced one back
    # rather than assuming this call owns the whole connection's counter
    # lifetime. Wifi's caller passes 0 and discards the result (one handler
    # call IS the whole connection there); BLE's caller keeps it, because
    # the very next mode on the same connection continues from it.
    _hash = None
    try:
        with open("/.tether_hash") as _hf:
            _hash = _hf.read()
    except OSError:
        pass
    _reply = {{
        "protocol_version": {PROTOCOL_VERSION},
        "tether_app_hash": _hash,
        "free_heap": _gc.mem_free(),
        "uptime_ms": time.ticks_diff(time.ticks_ms(), _boot_ms),
        "ip": _get_addr(),
    }}
    return _send_frame(_conn, _json.dumps(_reply).encode(), _session_key, _send_counter)

def _handle_run(_conn, _make_streams, _session_key, _recv_counter, _send_counter):
    try:
        with open("/tether_app.py") as _f:
            _tether_app_src = _f.read()
    except OSError:
        _tether_app_src = None

    if _tether_app_src is not None:
        import uasyncio as _asyncio

        _reader, _writer = _make_streams(_conn, _session_key, _recv_counter, _send_counter)
        try:
            exec(
                _tether_app_src,
                {{"_tether_stream_override": (_reader, _writer)}},
            )
        except (OSError, EOFError):
            print(
                "tether: client disconnected - run session ending "
                "(reconnect any time, no reset needed)"
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

def _handle_upload(_conn, _session_key, _recv_counter, _send_counter):
    # Both directions' counters come in as parameters and go back out
    # advanced - see _handle_status's own note above for why they are no
    # longer assumed to start fresh at 0 on every call.
    try:
        # Invalidate the OLD hash sentinel FIRST, before writing any
        # file - .tether_hash is written last on success (matching
        # serial's own ordering: a hash file present means "this
        # bundle is complete and self-consistent"), but nothing
        # previously invalidated the old one if an upload failed
        # partway (wifi drop, flash exhaustion, an oversized/
        # malformed chunk). Left stale, the old hash would still
        # "match" on a future status-based hash-check, so a broken/
        # truncated bundle would silently never get re-uploaded.
        # With this, a failed upload always leaves the sentinel
        # absent - a subsequent status query correctly reports
        # tether_app_hash: None, signaling "needs upload" - rather
        # than stale.
        try:
            import uos as _uos

            _uos.remove("/.tether_hash")
        except OSError:
            pass

        _manifest_bytes, _recv_counter = _recv_frame(_conn, _session_key, _recv_counter)
        _manifest = _json.loads(_manifest_bytes)

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
                # A chunk bigger than what's still declared-remaining for
                # THIS file must be rejected, not written anyway -
                # otherwise _remaining goes negative and, in a multi-file
                # upload, an oversized chunk would spill into what should
                # have been the next file's own frames. The body is read
                # in full by _recv_frame either way (needed to verify the
                # envelope when authenticated), so this check happens
                # after the read rather than before it - a behavior-
                # neutral reordering from the prior unauthenticated-only
                # code, still bounded by _MAX_CTRL_FRAME regardless. A
                # compliant chunked client (PC-side _upload()) never
                # sends a chunk like this.
                while _remaining > 0:
                    _chunk_data, _recv_counter = _recv_frame(_conn, _session_key, _recv_counter)
                    if len(_chunk_data) > _remaining:
                        raise OSError("upload chunk exceeds declared file size")
                    _wf.write(_chunk_data)
                    _remaining -= len(_chunk_data)
        _reply = {{"ok": True}}
    except Exception as _exc:
        _reply = {{"ok": False, "error": str(_exc)}}
    try:
        _send_counter = _send_frame(_conn, _json.dumps(_reply).encode(), _session_key, _send_counter)
    except OSError:
        # Deliberately leaves _send_counter where it was: the reply never
        # made it onto the wire, so the peer's own recv-direction counter
        # did not advance either, and a later mode on this same (BLE)
        # connection must continue from the unadvanced value or every
        # subsequent frame is off by one.
        pass
    return _recv_counter, _send_counter
"""

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
    import uhashlib as _hashlib
except ImportError:
    import hashlib as _hashlib

try:
    with open("/tether_wifi.json") as _f:
        _cfg = _json.loads(_f.read())
except OSError:
    _cfg = None

if _cfg is not None:
    import network
    import time
    import uos as _uos

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
        _TAG_LENGTH = 16  # must match tether.marshalling.frame_auth.DEFAULT_TAG_LENGTH
        _ENVELOPE_OVERHEAD = 24  # must match tether.marshalling.frame_auth.ENVELOPE_OVERHEAD

{textwrap.indent(_MODE_HANDLER_FUNCTIONS_SRC, "        ")}
        def _wifi_make_streams(_conn, _session_key, _recv_counter, _send_counter):
            import uasyncio as _asyncio

            _reader = _asyncio.StreamReader(_conn)
            _writer = _asyncio.StreamWriter(_conn, {{}})
            if _session_key is None:
                return _reader, _writer
            return (
                _AuthenticatedReader(_reader, _session_key, _TAG_LENGTH, _recv_counter),
                _AuthenticatedWriter(_writer, _session_key, _TAG_LENGTH, _send_counter),
            )

        _addr = _socket.getaddrinfo("0.0.0.0", {DEFAULT_PORT})[0][-1]
        _srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        _srv.bind(_addr)
        _srv.listen(4)

        while True:
            _conn, _client_addr = _srv.accept()
            try:
                _expected_secret = _cfg.get("secret")
                _nonce = _uos.urandom(16)
                _send_json_frame(_conn, {{"nonce": _hexlify(_nonce)}})
                _preamble = _read_json_frame(_conn)
                _mode = _preamble.get("mode")
                _response = _preamble.get("response")
                # Two SEPARATE HMACs over the same secret and nonce, not
                # one reused twice. _expected_response goes out on the
                # wire in the clear; if _session_key were the same digest
                # (as it originally was) any passive observer of this
                # handshake could hex-decode the response into the frame
                # key and forge/replay authenticated frames. The
                # b"tether-frame-key" prefix domain-separates the two.
                # Mirrors tether.transports.wifi.send_preamble exactly -
                # the two derivations must stay identical.
                if _expected_secret is not None:
                    _expected_response = _hexlify(
                        _hmac_sha256(_expected_secret.encode(), _nonce)
                    )
                    _session_key = _hmac_sha256(
                        _expected_secret.encode(), b"tether-frame-key" + _nonce
                    )
                else:
                    _session_key = None
                    _expected_response = None
                if _expected_secret is not None and _response != _expected_response:
                    _send_json_frame(_conn, {{"ok": False, "error": "auth failed"}})
                # Literal 0s, and the returned counters discarded: this loop
                # serves exactly ONE mode per accepted connection and then
                # closes it (below), so every handler call legitimately owns
                # a fresh counter pair - unlike BLE, which reuses one
                # connection across modes and must thread them through (see
                # _BLE_BOOT_TEMPLATE's session loop). Passing 0 explicitly
                # only makes visible what these handlers used to hardcode
                # internally; wifi's wire behavior is unchanged.
                elif _mode == "status":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_status(_conn, lambda: _wlan.ifconfig()[0], _session_key, 0)
                elif _mode == "run":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_run(_conn, _wifi_make_streams, _session_key, 0, 0)
                elif _mode == "upload":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_upload(_conn, _session_key, 0, 0)
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

# Hardcoded rather than imported from transports/ble.py, matching how
# _BOOT_PY_TEMPLATE above hardcodes DEFAULT_PORT: this text is executed on
# the MCU, where `tether.transports.ble` does not exist. The two must stay
# in sync by hand - see test_ble_boot_uuids_match_the_pc_side_transport.
_BLE_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
_BLE_WRITE_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
_BLE_NOTIFY_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

_BLE_ADV_NAME = "tether"


def _ble_adv_payload(name: str, service_uuid: str) -> bytes:
    """Build the 31-byte-max GAP advertising payload the board advertises
    with. Assembled here (PC side, at generation time) rather than on the
    MCU so the template carries one opaque bytes literal instead of packing
    logic - nothing about it varies per board.

    Three AD structures, each `len, type, value`: flags (LE General
    Discoverable, BR/EDR unsupported), the complete local name, and the
    complete list of 128-bit service UUIDs. The UUID goes out
    little-endian, per the Bluetooth Core spec's byte ordering for
    advertised UUIDs.
    """
    uuid_le = bytes.fromhex(service_uuid.replace("-", ""))[::-1]
    name_bytes = name.encode()
    payload = (
        b"\x02\x01\x06"
        + bytes([len(name_bytes) + 1, 0x09])
        + name_bytes
        + bytes([len(uuid_le) + 1, 0x07])
        + uuid_le
    )
    if len(payload) > 31:
        raise ValueError(f"advertising payload is {len(payload)} bytes, max 31")
    return payload


_BLE_ADV_PAYLOAD = _ble_adv_payload(_BLE_ADV_NAME, _BLE_SERVICE_UUID)


# BLE's counterpart to _BOOT_PY_TEMPLATE above, deliberately the same shape:
# a fixed template holding no secret (that lives in /tether_ble.json), which
# does nothing at all if that config file is missing, so a never-provisioned
# board falls straight through to the idle REPL.
#
# Structural note - why the session loop is synchronous, like wifi's:
# MicroPython delivers `bluetooth` IRQ events through its scheduler, which
# runs during `time.sleep_ms()`. A busy-poll waiting for IRQ-delivered bytes
# is therefore the exact BLE analogue of wifi's blocking `accept()`/`recv()`
# - it yields to the only thing that needs to run while a status/upload
# exchange is in flight (the BLE stack itself). Nothing else is scheduled
# on-device outside of run mode, so there is nothing to starve, and keeping
# this loop synchronous keeps _handle_run's `asyncio.run()` (inside the
# exec'd app) and its `new_event_loop()` reset non-nested, exactly as wifi
# already relies on.
#
# Run mode is the one place that genuinely needs async: `@mcu.loop` tasks
# are scheduled in the same event loop the app runs in, so a busy-poll while
# waiting for the next inbound frame *would* starve them. That is what
# _ble_make_streams' async reader below exists for - it awaits rather than
# blocks, so loop tasks keep progressing between frames.
#
# One BLE connection is reused across as many status/upload exchanges as the
# client wants; `run`, an auth failure, and an unrecognized mode each end the
# session (disconnect + resume advertising).
_BLE_BOOT_TEMPLATE = f"""\
try:
    import ujson as _json
except ImportError:
    import json as _json

try:
    import uhashlib as _hashlib
except ImportError:
    import hashlib as _hashlib

try:
    with open("/tether_ble.json") as _f:
        _cfg = _json.loads(_f.read())
except OSError:
    _cfg = None

if _cfg is not None:
    import bluetooth as _bluetooth
    import time
    import gc as _gc
    import uos as _uos

    # Module-scope (unlike wifi's _wifi_make_streams, which imports it
    # lazily inside itself): _ble_make_streams' nested reader class body
    # needs the name resolvable, and a frozen uasyncio import at boot costs
    # nothing.
    import uasyncio as _asyncio

    _boot_ms = time.ticks_ms()
    _MAX_CTRL_FRAME = 65536
    _TAG_LENGTH = 8  # must match tether.transports.ble.BLE_TAG_LENGTH
    _ENVELOPE_OVERHEAD = 16  # must match ble.py's own constant (see Task 1)
    _ADV_PAYLOAD = {_BLE_ADV_PAYLOAD!r}
    _ADV_INTERVAL_US = 100000
    # How long to wait for a requested disconnect's IRQ to confirm the link
    # is down before falling back on the advertise-retry loop.
    _DISCONNECT_WAIT_MS = 2000
    # gatts_notify() can fail transiently on a real stack (NimBLE raises an
    # ENOMEM-class OSError when its mbuf pool is momentarily exhausted by a
    # burst of notifications - a multi-chunk status payload or upload
    # result is exactly such a burst). Retry a few times with a short pause
    # so one transient failure doesn't fail the whole session; see
    # _BleConn.send.
    _NOTIFY_ATTEMPTS = 4
    _NOTIFY_RETRY_MS = 20
    # Give a nack a moment to actually go out before dropping the link.
    # gatts_notify() only *queues* an ATT notification and there is no
    # completion event for notifications (unlike indications), so an
    # immediate gap_disconnect() can tear the link down before the bytes
    # transmit - the client would then see a bare connection-closed OSError
    # instead of the intended WifiAuthError.
    _NACK_FLUSH_MS = 100
    # How often to resend the one-per-connection nonce while waiting for
    # the client's first preamble, and how long to keep resending before
    # giving up on a connection that never responds at all. Needed because
    # the CENTRAL_CONNECT IRQ (which triggers the first nonce send) fires
    # as soon as the BLE link forms - before the central has necessarily
    # finished subscribing to notifications (BleakClient.start_notify()'s
    # CCCD write happens strictly after client.connect() returns on the PC
    # side). A notify() sent before that subscription completes is silently
    # dropped by the BLE stack - gatts_notify() has no delivery
    # confirmation, so the device can't detect this any other way.
    # Confirmed via direct real-hardware reproduction (bare bleak
    # subscribe-then-wait): a single one-shot nonce send is reliably lost
    # to this race, not just occasionally. Resending is safe against
    # duplicate delivery: once the central *is* subscribed, a not-yet-
    # subscribed central's earlier notify() was truly dropped, not queued,
    # so only the resend sent after subscription completes ever reaches
    # `_queue`.
    _NONCE_RESEND_MS = 300
    _NONCE_MAX_WAIT_MS = 5000

{textwrap.indent(_MODE_HANDLER_FUNCTIONS_SRC, "    ")}
    _ble = _bluetooth.BLE()
    _ble.active(True)
    # MicroPython's bluetooth module has its own default gap_name
    # ("MPY ESP32") baked in, populating the standard Generic Access GATT
    # service's Device Name characteristic every peripheral exposes
    # automatically - separate from _BLE_ADV_PAYLOAD's custom advertising
    # local name below. Left unset, the two disagree, and some OS
    # Bluetooth stacks (observed on macOS CoreBluetooth) report whichever
    # one they happen to have cached rather than always the live
    # advertisement's name - a real device can show up as "tether" in one
    # scan and "MPY ESP32" in the next. Setting both to the same value
    # removes the discrepancy at the source instead of leaving it to
    # chance which one a given OS/scan happens to surface.
    try:
        _ble.config(gap_name="{_BLE_ADV_NAME}")
    except Exception:
        pass
    _ble_addr_str = ":".join("{{:02x}}".format(_b) for _b in _ble.config("mac")[1])

    # One-element lists as mutable boxes: _bt_irq and the stream adapters
    # below only ever *close over* these, and a MicroPython closure cannot
    # rebind a plain enclosing-scope name.
    _conn_handle = [None]
    _mtu = [23]  # BLE's pre-negotiation default
    _queue = []
    _write_handle = [None]
    _notify_handle = [None]

    def _bt_irq(_event, _data):
        # Runs in IRQ/scheduler context: routes bytes and records
        # connection state, never blocks and never does I/O.
        if _event == 1:  # _IRQ_CENTRAL_CONNECT
            _conn_handle[0] = _data[0]
        elif _event == 2:  # _IRQ_CENTRAL_DISCONNECT
            _conn_handle[0] = None
            _mtu[0] = 23
            # Drop anything the departed central left half-sent, so the
            # next connection never parses a truncated frame as its
            # preamble.
            del _queue[:]
        elif _event == 3:  # _IRQ_GATTS_WRITE
            if _data[1] == _write_handle[0]:
                _queue.append(_ble.gatts_read(_write_handle[0]))
        elif _event == 21:  # _IRQ_MTU_EXCHANGED
            _mtu[0] = _data[1]

    _ble.irq(_bt_irq)

    _write_char = (_bluetooth.UUID("{_BLE_WRITE_CHAR_UUID}"), _bluetooth.FLAG_WRITE)
    _notify_char = (_bluetooth.UUID("{_BLE_NOTIFY_CHAR_UUID}"), _bluetooth.FLAG_NOTIFY)
    _service = (_bluetooth.UUID("{_BLE_SERVICE_UUID}"), (_write_char, _notify_char))
    ((_write_handle[0], _notify_handle[0]),) = _ble.gatts_register_services((_service,))
    # The default GATT characteristic buffer is 20 bytes - anything larger
    # a central writes would be silently truncated, corrupting every frame
    # once the MTU is negotiated up.
    #
    # This size and the MTU are ONE coupled decision, not two independent
    # ones: 512 does not in fact cover the largest ATT MTU a central can
    # negotiate (517, i.e. a 514-byte single-write payload). It is safe
    # only because nothing here ever calls _ble.config(mtu=...), so the
    # on-device MTU stays at its low default and no single write can come
    # close. If a future change raises the MTU (to make bundle uploads
    # faster, say), this buffer size must be re-reviewed in the same
    # change - with append=True below, a write that overflows the buffer
    # truncates silently rather than erroring.
    #
    # append=True is MicroPython's documented mitigation for the
    # multi-write-before-read race: _bt_irq only runs when the scheduler
    # gets round to it, so a fast central can land a second write first.
    # Without append that second write overwrites the first and the frame
    # is silently corrupted; with it, writes accumulate and gatts_read
    # drains them in order. A burst then produces one IRQ per write but
    # only the first read returns data - the rest return b"", which both
    # queue consumers below treat as a harmless no-op.
    _ble.gatts_set_buffer(_write_handle[0], 512, True)

    class _BleConn:
        \"\"\"Adapts an IRQ-fed byte queue + gatts_notify() to the plain
        recv(n)/send(data) socket contract the shared mode handlers use.
        \"\"\"

        def __init__(self):
            self._buffer = b""

        def recv(self, _n):
            while len(self._buffer) < _n:
                while not _queue:
                    if _conn_handle[0] is None:
                        raise OSError("ble disconnected")
                    time.sleep_ms(10)
                self._buffer += _queue.pop(0)
            _result, self._buffer = self._buffer[:_n], self._buffer[_n:]
            return _result

        def send(self, _data):
            _usable = max(_mtu[0] - 3, 1)
            for _i in range(0, len(_data), _usable):
                _chunk = _data[_i : _i + _usable]
                _attempt = 0
                while True:
                    _h = _conn_handle[0]
                    if _h is None:
                        # The central is already gone. Raise the same
                        # OSError a dead socket's send() would, so the
                        # shared mode handlers (which catch OSError, not
                        # TypeError) treat a BLE disconnect exactly as
                        # they treat wifi's - notably _handle_run, whose
                        # friendly "reconnect any time" message is
                        # otherwise lost when an @mcu.loop task tries to
                        # notify after the central has left.
                        raise OSError("ble disconnected")
                    try:
                        _ble.gatts_notify(_h, _notify_handle[0], _chunk)
                        break
                    except OSError:
                        # Transient (ENOMEM-class) - back off briefly and
                        # retry rather than failing the whole session on
                        # the first hiccup of a notification burst. The
                        # sleep also lets the scheduler drain the stack's
                        # own queued work, which is what frees the buffers.
                        _attempt += 1
                        if _attempt >= _NOTIFY_ATTEMPTS:
                            raise
                        time.sleep_ms(_NOTIFY_RETRY_MS)
                    except Exception:
                        # Anything that isn't an OSError - e.g. the
                        # TypeError gatts_notify raises if the handle was
                        # cleared by a disconnect IRQ landing between the
                        # check above and this call - is translated to the
                        # OSError every caller above is written against.
                        raise OSError("ble notify failed")

        def take_buffer(self):
            _result, self._buffer = self._buffer, b""
            return _result

    def _ble_make_streams(_conn, _session_key, _recv_counter, _send_counter):
        # Seeded with whatever the synchronous preamble read over-consumed,
        # so no byte is lost handing this connection over to run mode.
        _leftover = [_conn.take_buffer()]

        class _BleAsyncReader:
            async def readexactly(self, _n):
                while len(_leftover[0]) < _n:
                    while not _queue:
                        if _conn_handle[0] is None:
                            raise EOFError("ble disconnected")
                        # Awaits rather than blocks: this is what keeps
                        # @mcu.loop tasks running between inbound frames.
                        await _asyncio.sleep_ms(5)
                    _leftover[0] += _queue.pop(0)
                _result, _leftover[0] = _leftover[0][:_n], _leftover[0][_n:]
                return _result

        class _BleAsyncWriter:
            def write(self, _data):
                _conn.send(_data)

            async def drain(self):
                await _asyncio.sleep_ms(0)

        if _session_key is None:
            return _BleAsyncReader(), _BleAsyncWriter()
        # Seeded with the counters this connection has already reached
        # across any preceding status/upload exchanges - run mode continues
        # the same two counter sequences, it does not restart them. The PC
        # side does exactly this too: connection.py's _connect_ble hands
        # BleStream.enable_authentication() the SAME FrameAuthenticator
        # pair BleControlChannel has been using since the handshake.
        return (
            _AuthenticatedReader(_BleAsyncReader(), _session_key, _TAG_LENGTH, _recv_counter),
            _AuthenticatedWriter(_BleAsyncWriter(), _session_key, _TAG_LENGTH, _send_counter),
        )

    def _start_advertising():
        \"\"\"True once the board is actually advertising again. NimBLE
        refuses to start advertising while a connection is still up, so
        this can legitimately fail for a few ms after a disconnect has
        been requested but before its IRQ lands. The caller retries
        instead of treating it as fatal: an unguarded raise here would
        escape the per-session try/except below and kill the whole accept
        loop, leaving the board unreachable until a physical reset.
        \"\"\"
        try:
            _ble.gap_advertise(_ADV_INTERVAL_US, _ADV_PAYLOAD)
            return True
        except OSError:
            return False

    def _end_session():
        \"\"\"Drop the current link and wait for the CENTRAL_DISCONNECT IRQ
        to confirm it is really down. gap_disconnect() only *requests* a
        disconnect and returns immediately; treating the link as gone the
        moment it returns is what makes the re-advertise above fail. The
        wait is bounded - if the IRQ never arrives, the advertise retry
        loop is the backstop rather than hanging here forever.

        Nothing in here may raise: this runs OUTSIDE the per-session
        try/except below, so an escaping exception kills the whole session
        loop and leaves the board unreachable until a physical reset. Two
        things guard that. The handle is read ONCE, into a local: reading
        _conn_handle[0] a second time for the gap_disconnect() call leaves
        a window where a scheduler-delivered CENTRAL_DISCONNECT IRQ (which
        clears it) turns that call into gap_disconnect(None) - a TypeError,
        which `except OSError` would not catch. And the except is
        Exception, not OSError, so a stack that rejects a stale handle some
        other way is equally survivable - the same discipline
        _start_advertising above already applies.
        \"\"\"
        _handle = _conn_handle[0]
        if _handle is None:
            return
        try:
            _ble.gap_disconnect(_handle)
        except Exception:
            pass
        _deadline = time.ticks_add(time.ticks_ms(), _DISCONNECT_WAIT_MS)
        while _conn_handle[0] is not None and time.ticks_diff(_deadline, time.ticks_ms()) > 0:
            time.sleep_ms(10)

    while True:
        # Re-establish a known-good advertising state before serving
        # anything. Also the guard against starting a spurious session on
        # a link that is still tearing down: while the old connection is
        # up this keeps retrying rather than falling through to the
        # wait-for-connection loop, which _conn_handle[0] alone would let
        # pass the instant a stale handle was still set.
        while not _start_advertising():
            time.sleep_ms(50)
        while _conn_handle[0] is None:
            time.sleep_ms(20)
        _conn = _BleConn()
        try:
            # One nonce per physical connection, sent before the first
            # preamble is even read - not per preamble like wifi's fresh-
            # connection-per-mode model. Only the FIRST preamble on this
            # connection needs to present a response to it; status/upload
            # exchanges that follow on the SAME already-open connection
            # skip the check entirely (see _BLE_BOOT_TEMPLATE's own module
            # docstring on the one-connection-reused model).
            _expected_secret = _cfg.get("secret")
            _nonce = _uos.urandom(16)
            _authenticated = _expected_secret is None
            _session_key = (
                None
                if _expected_secret is None
                else _hmac_sha256(_expected_secret.encode(), b"tether-frame-key" + _nonce)
            )
            # Connection-scoped, NOT per-mode: one physical BLE connection
            # carries a whole status/upload/run sequence, and both the
            # session key and these two counters must live for its entire
            # lifetime (DESIGN.md, BLE row, per-frame authentication). The
            # PC side's BleControlChannel keeps one _send_auth/_recv_auth
            # pair per connection for exactly this reason; a handler that
            # restarted at 0 on its second call would desync the two sides
            # on the very next frame, and the hard-close-no-retry policy
            # turns that straight into a dead connection.
            _recv_counter = 0
            _send_counter = 0
            _send_json_frame(_conn, {{"nonce": _hexlify(_nonce)}})
            # Resend on a timer until the central's first preamble actually
            # lands in _queue - see _NONCE_RESEND_MS's own comment above for
            # why a single send isn't reliable. Bounded by _NONCE_MAX_WAIT_MS
            # so a central that connects but never subscribes at all doesn't
            # wedge this session forever.
            _nonce_deadline = time.ticks_add(time.ticks_ms(), _NONCE_RESEND_MS)
            _nonce_giveup = time.ticks_add(time.ticks_ms(), _NONCE_MAX_WAIT_MS)
            while not _queue:
                if _conn_handle[0] is None:
                    raise OSError("ble disconnected")
                if time.ticks_diff(_nonce_giveup, time.ticks_ms()) <= 0:
                    raise OSError("central never subscribed to notifications")
                if time.ticks_diff(_nonce_deadline, time.ticks_ms()) <= 0:
                    _send_json_frame(_conn, {{"nonce": _hexlify(_nonce)}})
                    _nonce_deadline = time.ticks_add(time.ticks_ms(), _NONCE_RESEND_MS)
                time.sleep_ms(10)
            while True:
                _preamble = _read_json_frame(_conn)
                _mode = _preamble.get("mode")
                if not _authenticated:
                    _response = _preamble.get("response")
                    _expected_response = _hexlify(_hmac_sha256(_expected_secret.encode(), _nonce))
                    if _response != _expected_response:
                        _send_json_frame(_conn, {{"ok": False, "error": "auth failed"}})
                        # Let the nack actually transmit before the disconnect
                        # below tears the link down - see _NACK_FLUSH_MS.
                        time.sleep_ms(_NACK_FLUSH_MS)
                        break
                    _authenticated = True
                if _mode == "status":
                    _send_json_frame(_conn, {{"ok": True}})
                    _send_counter = _handle_status(
                        _conn, lambda: _ble_addr_str, _session_key, _send_counter
                    )
                elif _mode == "upload":
                    _send_json_frame(_conn, {{"ok": True}})
                    _recv_counter, _send_counter = _handle_upload(
                        _conn, _session_key, _recv_counter, _send_counter
                    )
                elif _mode == "run":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_run(_conn, _ble_make_streams, _session_key, _recv_counter, _send_counter)
                    break
                else:
                    _send_json_frame(_conn, {{"ok": False, "error": "unknown mode"}})
                    # Same flush-before-disconnect reasoning as the auth
                    # nack above.
                    time.sleep_ms(_NACK_FLUSH_MS)
                    break
        except Exception:
            pass
        # status/upload fall through the if/elif chain and loop naturally to
        # read the next preamble on this same connection; getting here means
        # the session is over (run finished, auth failed, unknown mode, or
        # the central vanished). Re-advertising is the next iteration's
        # first act, once this confirms the link is actually down.
        #
        # Belt and braces around _end_session's own internal hardening
        # (see its docstring): this call site sits outside the per-session
        # try/except above, and NOTHING here is allowed to kill the loop -
        # an unreachable board needs a physical reset to recover.
        try:
            _end_session()
        except Exception:
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
    for `tether provision wifi` to upload. `boot.py` is a fixed template
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


def generate_ble_boot(
    secret: str | None = None, *, danger_unauthenticated: bool = False
) -> dict[str, bytes]:
    """Return `{"/boot.py": ..., "/tether_ble.json": ...}` file contents for
    `tether provision ble` to upload. Mirrors `generate_wifi_boot` exactly -
    see its docstring - minus credentials, since BLE has no network to join:
    the config file carries only the shared secret.
    """
    config: dict[str, str] = {}
    if not danger_unauthenticated:
        config["secret"] = secret if secret is not None else secrets.token_hex(16)
    return {
        "/boot.py": _BLE_BOOT_TEMPLATE.encode(),
        "/tether_ble.json": json.dumps(config).encode(),
    }
