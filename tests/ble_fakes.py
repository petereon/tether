"""Fake MicroPython `bluetooth` module + the single-process harness that
drives a generated BLE `boot.py` against it under the real `micropython`
unix-port interpreter.

Why a fake at all: unlike wifi (where the on-device listener binds a real
TCP socket a real PC-side client can connect to, so tests exercise a genuine
cross-process channel), GATT has no unfaked local primitive. `bleak` is
central-only and no platform lets you run a real local BLE peripheral. So
both "sides" have to live in one interpreter, sharing the literal same fake
`bluetooth.BLE()` object.

Why threads rather than two uasyncio tasks: the generated BLE session loop
is *synchronous* (`time.sleep_ms` busy-polls waiting for IRQ-delivered
data), deliberately mirroring wifi's synchronous `accept()` loop - see
`_BLE_BOOT_TEMPLATE`'s own comment for why that is the right on-device
shape. Two cooperatively-scheduled tasks in one event loop therefore could
not interleave at all: the boot task would block the whole interpreter the
moment it entered a busy-poll and the driver would never run again. The
MicroPython unix port has real preemptive `_thread`s whose GIL is released
across `time.sleep_ms`, so running boot.py in a background thread and the
scripted "central" driver in the main thread reproduces the real thing far
more closely: on hardware, BLE IRQs are delivered by MicroPython's
scheduler *during* `time.sleep_ms`, which is exactly what a second thread
appending to the shared queue during the poll simulates.

Main-thread exit terminates the process even with the boot thread still
looping (verified against the real interpreter), which is what lets a
driver that runs to completion end the test - boot.py loops forever by
design and never returns.
"""

from __future__ import annotations

import textwrap

# The value handles the fake's `gatts_register_services` hands back, in
# characteristic-declaration order (write first, then notify - matching
# `_BLE_BOOT_TEMPLATE`'s service tuple). Exported so drivers write to the
# *write* characteristic rather than hardcoding a magic number and silently
# addressing the wrong one.
WRITE_VALUE_HANDLE = 100
NOTIFY_VALUE_HANDLE = 101

FAKE_BLUETOOTH_MODULE_SRC = '''
import _thread
import time


class _FakeBLE:
    """Matches the surface of MicroPython's `bluetooth.BLE` that
    _BLE_BOOT_TEMPLATE uses, including the IRQ event numbering from
    MicroPython's own docs (CENTRAL_CONNECT=1, CENTRAL_DISCONNECT=2,
    GATTS_WRITE=3, MTU_EXCHANGED=21) and the (event, data) callback shape.
    """

    _IRQ_CENTRAL_CONNECT = 1
    _IRQ_CENTRAL_DISCONNECT = 2
    _IRQ_GATTS_WRITE = 3
    _IRQ_MTU_EXCHANGED = 21

    def __init__(self):
        self._irq_handler = None
        self.notifications = []  # [(conn_handle, value_handle, bytes), ...]
        self.disconnects = []  # conn_handles the device itself dropped
        self._values = {}
        self._buffer_sizes = {}
        self._append = {}
        self.advertising = False
        self.adv_payload = None
        self.active_state = False
        # None once the link is genuinely down. Distinct from whatever the
        # device *thinks*, which only updates when the disconnect IRQ lands.
        self.connected_handle = None
        # >0 makes gap_disconnect() asynchronous, like real hardware: the
        # call returns immediately and the CENTRAL_DISCONNECT IRQ arrives
        # this many ms later. 0 delivers it inline.
        self.disconnect_delay_ms = 0
        self.advertise_errors = 0
        # Makes the NEXT gap_disconnect() raise TypeError instead of
        # working, modelling what real MicroPython does when the conn
        # handle it is passed has already been cleared by a disconnect
        # IRQ (`can't convert NoneType to int` - not an OSError, so a
        # too-narrow except clause on the device would let it escape).
        self.fail_next_disconnect = False
        # >0 makes that many upcoming gatts_notify() calls raise an
        # ENOMEM-class OSError before one succeeds - what NimBLE does when
        # its mbuf pool is momentarily exhausted by a burst of
        # notifications. notify_errors counts how many actually fired.
        self.notify_failures = 0
        self.notify_errors = 0
        # While True, every gatts_notify() raises TypeError - the residual
        # post-disconnect case a "is the handle None?" pre-check cannot
        # catch, because the IRQ can land between that check and the call.
        self.notify_type_error = False
        # ticks_ms() of each successful notify / each gap_disconnect call,
        # so a test can assert the device paced them apart rather than
        # racing a queued notification against the link teardown.
        self.notify_ticks = []
        self.disconnect_ticks = []
        self._config = {}

    def active(self, _on=None):
        if _on is not None:
            self.active_state = _on
        return self.active_state

    def irq(self, _handler):
        self._irq_handler = _handler

    def config(self, _key=None, **_kwargs):
        # Real MicroPython bluetooth.BLE.config() is get-or-set, matching
        # its own docs: config('param') to read, config(param=value, ...)
        # to write - a fake supporting only the positional/get form would
        # silently swallow any code that sets gap_name/mtu/etc, since a
        # real TypeError-on-unsupported-kwarg would never surface here.
        if _kwargs:
            self._config.update(_kwargs)
            return None
        if _key == "mac":
            return (0, b"\\xaa\\xbb\\xcc\\xdd\\xee\\xff")
        if _key in self._config:
            return self._config[_key]
        raise ValueError(_key)

    def gap_advertise(self, _interval_us, _adv_data=None):
        # NimBLE refuses to start advertising while a connection is still
        # up. This is the whole reason the device must wait for the
        # disconnect IRQ rather than trusting gap_disconnect()'s return.
        if _interval_us is not None and self.connected_handle is not None:
            self.advertise_errors += 1
            raise OSError("cannot advertise with an active connection")
        self.advertising = _interval_us is not None
        if _adv_data is not None:
            self.adv_payload = bytes(_adv_data)

    def gap_disconnect(self, _conn_handle):
        # The real call only *requests* a disconnect and returns straight
        # away; the link is not down until CENTRAL_DISCONNECT lands. With
        # disconnect_delay_ms set, the fake reproduces that gap on a real
        # thread rather than delivering the IRQ inline.
        self.disconnect_ticks.append(time.ticks_ms())
        if self.fail_next_disconnect:
            self.fail_next_disconnect = False
            raise TypeError("can't convert NoneType to int")
        self.disconnects.append(_conn_handle)
        if self.disconnect_delay_ms:
            _delay = self.disconnect_delay_ms

            def _deliver_later():
                time.sleep_ms(_delay)
                self._deliver_disconnect(_conn_handle)

            _thread.start_new_thread(_deliver_later, ())
        else:
            self._deliver_disconnect(_conn_handle)
        return True

    def _deliver_disconnect(self, _conn_handle):
        self.connected_handle = None
        if self._irq_handler is not None:
            self._irq_handler(self._IRQ_CENTRAL_DISCONNECT, (_conn_handle, 0, b""))

    def gatts_register_services(self, _services):
        # Real API returns one tuple of value handles per service, in
        # characteristic-declaration order. Only a single service with two
        # characteristics is ever registered here, so stable integers the
        # test driver and template agree on ahead of time suffice.
        return ((100, 101),)

    def gatts_read(self, _value_handle):
        _value = self._values.get(_value_handle, b"")
        if self._append.get(_value_handle):
            # In append mode the buffer is drained by the read, so a burst
            # of writes is handed over exactly once, in order.
            self._values[_value_handle] = b""
        return _value

    def gatts_write(self, _value_handle, _data):
        if self._append.get(_value_handle):
            self._values[_value_handle] = self._values.get(_value_handle, b"") + bytes(_data)
        else:
            # Without append, a second write before the first is read
            # simply destroys it - the race gatts_set_buffer(append=True)
            # exists to close.
            self._values[_value_handle] = bytes(_data)

    def gatts_notify(self, _conn_handle, _value_handle, _data):
        if _conn_handle is None or self.notify_type_error:
            # TypeError, not OSError: the real call converts the handle to
            # an int, and None simply is not one. The distinction is the
            # whole point - the device's own except clauses differ.
            raise TypeError("can't convert NoneType to int")
        if self.notify_failures:
            self.notify_failures -= 1
            self.notify_errors += 1
            raise OSError("no mem buffers available")
        self.notify_ticks.append(time.ticks_ms())
        self.notifications.append((_conn_handle, _value_handle, bytes(_data)))

    def gatts_set_buffer(self, _value_handle, _size, _append=False):
        self._buffer_sizes[_value_handle] = _size
        self._append[_value_handle] = _append

    # --- test-driver-only helpers, not part of the real API ---
    def _simulate_connect(self, _conn_handle):
        self.connected_handle = _conn_handle
        self.advertising = False  # a real link stops advertising
        self._irq_handler(self._IRQ_CENTRAL_CONNECT, (_conn_handle, 0, b""))

    def _simulate_disconnect(self, _conn_handle):
        self._deliver_disconnect(_conn_handle)

    def _simulate_mtu(self, _conn_handle, _mtu):
        self._irq_handler(self._IRQ_MTU_EXCHANGED, (_conn_handle, _mtu))

    def _simulate_write(self, _conn_handle, _value_handle, _data):
        # A real central cannot write more than (MTU - 3) bytes in one ATT
        # operation; drivers are expected to respect that themselves, this
        # only mirrors the "peripheral reads the characteristic value the
        # IRQ just announced" ordering.
        self.gatts_write(_value_handle, _data)
        self._irq_handler(self._IRQ_GATTS_WRITE, (_conn_handle, _value_handle))

    def _simulate_write_burst(self, _conn_handle, _value_handle, _chunks):
        """Every ATT write lands before the device's scheduled read handler
        gets to run even once - the multi-write-before-read race that
        gatts_set_buffer(append=True) exists to mitigate. Without append,
        all but the last chunk are destroyed before anything reads them.
        """
        for _chunk in _chunks:
            self.gatts_write(_value_handle, _chunk)
        for _chunk in _chunks:
            self._irq_handler(self._IRQ_GATTS_WRITE, (_conn_handle, _value_handle))


class _FakeUUID:
    def __init__(self, _s):
        self._s = _s

    def __eq__(self, _other):
        return isinstance(_other, _FakeUUID) and self._s == _other._s


_ble_singleton = []


def _ble_factory():
    """`bluetooth.BLE()` is a singleton in real MicroPython - every call
    returns the same object. Faithfully reproducing that is also what lets
    the driver thread reach the very instance the boot thread constructed,
    without boot.py having to expose it.
    """
    if not _ble_singleton:
        _ble_singleton.append(_FakeBLE())
    return _ble_singleton[0]


class _FakeBluetoothModule:
    FLAG_READ = 0x0002
    FLAG_WRITE_NO_RESPONSE = 0x0004
    FLAG_WRITE = 0x0008
    FLAG_NOTIFY = 0x0010
    BLE = staticmethod(_ble_factory)
    UUID = _FakeUUID


import sys as _sys

_sys.modules["bluetooth"] = _FakeBluetoothModule
'''


# In-memory filesystem standing in for the board's flash, so a generated
# boot.py can read /tether_ble.json and _handle_upload can write files that
# a later status query reads back - same shape as the fake `open` the wifi
# boot.py tests already use, kept here so BLE tests don't each re-declare it.
FAKE_FS_SRC = """
class _FakeReadFile:
    def __init__(self, content):
        self._content = content

    def read(self):
        return self._content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeWriteFile:
    def __init__(self, path):
        self._path = path
        self._buf = b""

    def write(self, data):
        self._buf += data
        return len(data)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        _files[self._path] = self._buf
        return False


def _fake_open(path, mode="r", *a, **kw):
    if "w" in mode:
        return _FakeWriteFile(path)
    if path in _files:
        content = _files[path]
        if isinstance(content, bytes):
            content = content.decode()
        return _FakeReadFile(content)
    raise OSError(2, "no such file")


import builtins as _builtins

_builtins.open = _fake_open


class _FakeUos:
    @staticmethod
    def remove(path):
        if path not in _files:
            raise OSError(2, "no such file")
        del _files[path]

    @staticmethod
    def mkdir(path):
        pass

    @staticmethod
    def listdir(path="/"):
        return [p.lstrip("/") for p in _files]

    @staticmethod
    def urandom(n):
        # _BLE_BOOT_TEMPLATE calls uos.urandom() once per connection for
        # its nonce - a fixed value is fine here, nothing in these tests
        # checks its actual randomness.
        return bytes(n)


_sys.modules["uos"] = _FakeUos
_sys.modules["os"] = _FakeUos
"""


# A scripted stand-in for the PC-side BLE client (transports/ble.py's
# BleStream + BleControlChannel), written in MicroPython so it can run in
# the same process as the device. Chunks its writes at MTU-3 and reassembles
# the notification stream back into length-prefixed frames exactly as the
# real client does - so tests assert on decoded frames rather than on raw
# notification counts, which vary with MTU and payload size.
CENTRAL_HELPER_SRC = """
import ujson as _cjson

try:
    import uhashlib as _chashlib
except ImportError:
    import hashlib as _chashlib


def _c_hexlify(_data):
    return "".join("{:02x}".format(_b) for _b in _data)


def _c_unhexlify(_hex):
    return bytes(int(_hex[_i : _i + 2], 16) for _i in range(0, len(_hex), 2))


def _c_hmac_sha256(_key, _msg):
    # Independent copy of provisioning.py's hand-rolled HMAC-SHA256 - this
    # driver stands in for the real PC-side client (transports/ble.py),
    # which has real stdlib hmac available; this fake, running under the
    # real micropython interpreter alongside the boot.py under test, does
    # not, so it re-implements the same hand-rolled algorithm rather than
    # reaching into boot_py_source's own copy (which is a local name inside
    # _boot_thread(), not visible here - see combine_boot_py_with_driver).
    _block_size = 64
    if len(_key) > _block_size:
        _key = _chashlib.sha256(_key).digest()
    _key = _key + b"\\x00" * (_block_size - len(_key))
    _o_key_pad = bytes(_b ^ 0x5C for _b in _key)
    _i_key_pad = bytes(_b ^ 0x36 for _b in _key)
    _inner = _chashlib.sha256(_i_key_pad + _msg).digest()
    return _chashlib.sha256(_o_key_pad + _inner).digest()


class Central:
    def __init__(self, ble, conn_handle=0, mtu=100):
        self._ble = ble
        self._conn = conn_handle
        self._mtu = mtu
        # Start after whatever a previous connection already produced, so a
        # second Central against the same fake reads only its own replies.
        self._read_pos = len(ble.notifications)
        self._buf = b""
        # Mirrors BleControlChannel's own _authenticated flag: only the
        # first send_preamble() call on this Central reads a nonce and
        # answers it - later calls on the same (simulated) connection just
        # send {mode}. See transports/ble.py's send_preamble docstring.
        self._authenticated = False
        # Set by send_preamble() once a secret is presented, mirroring
        # BleControlChannel's own session-key caching - test driver scripts
        # read this back to drive the authenticated frame helpers below.
        self.session_key = None

    def connect(self):
        self._ble._simulate_connect(self._conn)
        self._ble._simulate_mtu(self._conn, self._mtu)

    def disconnect(self):
        self._ble._simulate_disconnect(self._conn)

    def write(self, data):
        _usable = max(self._mtu - 3, 1)
        for _i in range(0, len(data), _usable):
            self._ble._simulate_write(self._conn, WRITE_VALUE_HANDLE, data[_i : _i + _usable])

    def write_burst(self, data):
        # Same bytes as write(), but every ATT write lands before the
        # device reads any of them - see _FakeBLE._simulate_write_burst.
        _usable = max(self._mtu - 3, 1)
        _chunks = [data[_i : _i + _usable] for _i in range(0, len(data), _usable)]
        self._ble._simulate_write_burst(self._conn, WRITE_VALUE_HANDLE, _chunks)

    def send_json_frame(self, obj):
        _body = _cjson.dumps(obj).encode()
        self.write(len(_body).to_bytes(4, "big") + _body)

    def send_json_frame_burst(self, obj):
        _body = _cjson.dumps(obj).encode()
        self.write_burst(len(_body).to_bytes(4, "big") + _body)

    def send_bytes_frame(self, data):
        self.write(len(data).to_bytes(4, "big") + data)

    def recv_exact(self, n, timeout_ms=3000):
        _deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while True:
            _notes = self._ble.notifications
            while self._read_pos < len(_notes):
                self._buf += _notes[self._read_pos][2]
                self._read_pos += 1
            if len(self._buf) >= n:
                break
            if time.ticks_diff(_deadline, time.ticks_ms()) <= 0:
                raise OSError(
                    "central timed out waiting for {} bytes (have {})".format(n, len(self._buf))
                )
            time.sleep_ms(5)
        _out, self._buf = self._buf[:n], self._buf[n:]
        return _out

    def read_json_frame(self, timeout_ms=3000):
        _length = int.from_bytes(self.recv_exact(4, timeout_ms), "big")
        return _cjson.loads(self.recv_exact(_length, timeout_ms))

    def send_preamble(self, mode, secret):
        if not self._authenticated:
            _nonce_frame = self.read_json_frame()
            _nonce = _c_unhexlify(_nonce_frame["nonce"])
            if secret is not None:
                _response = _c_hexlify(_c_hmac_sha256(secret.encode(), _nonce))
                self.session_key = self._derive_session_key(secret, _nonce)
            else:
                _response = None
                self.session_key = None
            self.send_json_frame({"mode": mode, "response": _response})
            self._authenticated = True
        else:
            self.send_json_frame({"mode": mode})
        return self.read_json_frame()

    def _derive_session_key(self, secret, nonce):
        return _c_hmac_sha256(secret.encode(), b"tether-frame-key" + nonce)

    def send_authenticated_json_frame(self, obj, session_key, counter):
        _body = _cjson.dumps(obj).encode()
        _inner = len(_body).to_bytes(4, "big") + _body
        return self._send_authenticated_frame(_inner, session_key, counter)

    def send_authenticated_bytes_frame(self, data, session_key, counter):
        _inner = len(data).to_bytes(4, "big") + data
        return self._send_authenticated_frame(_inner, session_key, counter)

    def _send_authenticated_frame(self, inner, session_key, counter):
        _counter_bytes = counter.to_bytes(4, "big")
        _tag = _c_hmac_sha256(session_key, _counter_bytes + inner)[:8]
        _envelope = _counter_bytes + inner + _tag
        self.write(len(_envelope).to_bytes(4, "big") + _envelope)
        return counter + 1

    def read_authenticated_json_frame(self, session_key, counter):
        _inner, _next_counter = self._read_authenticated_frame(session_key, counter)
        _length = int.from_bytes(_inner[:4], "big")
        return _cjson.loads(_inner[4 : 4 + _length]), _next_counter

    def _read_authenticated_frame(self, session_key, counter):
        _length = int.from_bytes(self.recv_exact(4), "big")
        _body = self.recv_exact(_length)
        _counter_bytes = _body[:4]
        _tag = _body[-8:]
        _inner = _body[4:-8]
        _got_counter = int.from_bytes(_counter_bytes, "big")
        if _got_counter != counter:
            raise OSError("unexpected frame counter")
        _expected_tag = _c_hmac_sha256(session_key, _counter_bytes + _inner)[:8]
        if _tag != _expected_tag:
            raise OSError("frame authentication tag mismatch")
        return _inner, counter + 1
"""


def combine_boot_py_with_driver(
    boot_py_source: str, driver_source: str, *, files: dict[str, str] | None = None
) -> str:
    """Build one micropython script that runs `boot_py_source` (a generated
    BLE boot.py, verbatim - never rewritten) on a background thread and
    `driver_source` (a scripted "central") on the main thread, both sharing
    one fake `bluetooth` module instance and one fake filesystem.

    `files` seeds that filesystem; `/tether_ble.json` must be among them or
    boot.py falls straight through to the REPL and does nothing.

    The driver runs to completion and its prints are the script's output;
    the boot thread loops forever by design and is simply abandoned when the
    main thread returns.
    """
    files = files if files is not None else {}
    return f"""
import _thread
import time

WRITE_VALUE_HANDLE = {WRITE_VALUE_HANDLE}
NOTIFY_VALUE_HANDLE = {NOTIFY_VALUE_HANDLE}

{FAKE_BLUETOOTH_MODULE_SRC}

_files = {files!r}

{FAKE_FS_SRC}

{CENTRAL_HELPER_SRC}

def _boot_thread():
    try:
{textwrap.indent(boot_py_source, " " * 8)}
    except Exception as _exc:
        print("BOOT THREAD DIED:", repr(_exc))


_thread.start_new_thread(_boot_thread, ())
# Let the boot thread finish registering its GATT service and reach its
# wait-for-connection poll before the driver simulates a central.
time.sleep_ms(100)

{driver_source}
"""
