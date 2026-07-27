# BLE upload, auth, and status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give BLE full behavioral parity with wifi's `run`/`upload`/`status` modes, shared-secret auth, and hash-check-skip-upload — building BLE's on-device peripheral from scratch (none exists today; `transports/ble.py` is PC-side-only).

**Architecture:** MicroPython's built-in `bluetooth` module (no `aioble` vendoring) drives a GATT peripheral that advertises, accepts one central at a time, and — unlike wifi — reuses **one** BLE connection across `status` → `upload` → `run` (BLE connection setup is too costly to redo per mode). `_handle_status`/`_handle_upload` are shared verbatim with wifi's `boot.py` via a small `.recv(n)`/`.send(data)` adapter matching `socket`'s contract; `_handle_run` shares its body but takes a transport-specific stream-constructor parameter, since `uasyncio.StreamReader/Writer` require a real socket at the native level, which BLE fundamentally isn't.

**Tech Stack:** MicroPython `bluetooth` module (device), `bleak` (PC, existing), `uasyncio`, `click`.

## Global Constraints

- Full parity with wifi: same three modes, same shared-secret auth (`--danger-unauthenticated` opt-out), same hash-check-skip-upload semantics.
- **One BLE connection reused** across `status` → `upload` → `run` — not wifi's "always separate connections." Only `run` (or an auth failure, or an unrecognized mode) ends the session/disconnects.
- On-device: built-in `bluetooth` module only. No vendoring `aioble` (confirmed absent from the real ESP32 firmware this project verifies against; built-in `bluetooth` confirmed present).
- Mutual exclusivity: `provision-ble` warns (does not block) if `/tether_wifi.json` exists; `provision-wifi` gets the symmetric check against `/tether_ble.json`. A board runs one transport's `boot.py` at a time.
- Separate secret/config from wifi: `/tether_ble.json`, `TETHER_BLE_SECRET` env var. `WifiAuthError` is reused as-is for BLE auth failures (no new exception class).
- `SERVICE_UUID`/`WRITE_CHAR_UUID`/`NOTIFY_CHAR_UUID` (already fixed in `transports/ble.py`) are reused unchanged.
- No real BLE peripheral is testable in CI (same limitation `ble.py`'s own docstring already states for the PC-central side). On-device logic is tested via a hand-rolled fake `bluetooth` module driven **within a single `micropython` process** (unlike wifi's tests, which bridge a real TCP socket across two processes — BLE has no such real, unfaked cross-process primitive available).
- Real-hardware verification (final task) is load-bearing, not optional, before this is considered done — matches this project's established discipline (see `docs/superpowers/plans/2026-07-26-wifi-modes-auth.md`'s own Task 8, and its final review finding that no individual task's tests alone caught the real headline bug).
- `ruff format .`/`ruff check .` run unscoped will also touch `docs/superpowers/...` markdown files and can corrupt fenced code examples (pre-existing, repeatedly-hit issue in the wifi plan) — scope format/lint commands to `src/` and `tests/` explicitly.

---

### Task 1: PC-side BLE control-channel wire helpers

**Files:**
- Modify: `src/tether/transports/ble.py`
- Test: `tests/test_transport_ble.py`

**Interfaces:**
- Consumes: `BleStream` (existing, unchanged — `.read()`/`.write()`/`.close()`), `WifiAuthError` (`tether.errors`, existing).
- Produces: `BleControlChannel` class wrapping an already-connected `BleStream`, with methods `send_json_frame(payload)`, `read_json_frame()`, `send_bytes_frame(data)`, `read_bytes_frame()`, `send_preamble(mode, secret)` (raises `WifiAuthError` on nack). `MAX_CONTROL_FRAME_SIZE = 1 << 16` (module constant, matches wifi's).

- [ ] **Step 1: Write the failing tests**

`BleStream.read()` returns whatever bytes one GATT notification happened to carry — not a caller-chosen count, unlike `socket.recv(n)`. A length-prefixed frame reader therefore needs to buffer leftover bytes across calls; that's why this is a stateful class, not free functions like wifi's `transports/wifi.py`. Add to `tests/test_transport_ble.py`:

```python
from tether.transports.ble import BleControlChannel, BleStream
from tether.errors import WifiAuthError


def test_control_channel_reads_a_json_frame_split_across_multiple_notifications():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    body = b'{"ok": true, "ip": "10.0.0.5"}'
    header = len(body).to_bytes(4, "big")
    # Simulate the notification arriving in two separate chunks, neither
    # aligned to the frame boundary - exactly what a real MTU-chunked
    # notification stream can do.
    stream.on_notify(None, bytearray(header + body[:5]))
    stream.on_notify(None, bytearray(body[5:]))

    assert channel.read_json_frame() == {"ok": True, "ip": "10.0.0.5"}


def test_control_channel_leftover_bytes_carry_into_the_next_frame():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    first = b'{"a": 1}'
    second = b'{"b": 2}'
    # Both frames' bytes arrive in one single notification - the reader
    # must not discard the second frame's bytes while parsing the first.
    combined = len(first).to_bytes(4, "big") + first + len(second).to_bytes(4, "big") + second
    stream.on_notify(None, bytearray(combined))

    assert channel.read_json_frame() == {"a": 1}
    assert channel.read_json_frame() == {"b": 2}


def test_control_channel_send_json_frame_writes_length_prefixed_body():
    client = _FakeBleakClient(mtu_size=100)
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    channel.send_json_frame({"mode": "status", "secret": None})

    body = b'{"mode": "status", "secret": null}'
    assert b"".join(client.writes) == len(body).to_bytes(4, "big") + body


def test_control_channel_read_bytes_frame_returns_raw_bytes_not_json_decoded():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    payload = b"\x00\x01\xff not valid json"
    stream.on_notify(None, bytearray(len(payload).to_bytes(4, "big") + payload))

    assert channel.read_bytes_frame() == payload


def test_control_channel_read_json_frame_rejects_oversized_declared_length():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    stream.on_notify(None, bytearray((1 << 20).to_bytes(4, "big")))

    with pytest.raises(OSError, match="too large"):
        channel.read_json_frame()


def test_control_channel_read_json_frame_raises_on_closed_connection():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    stream.signal_closed()

    with pytest.raises(OSError, match="closed"):
        channel.read_json_frame()


def test_send_preamble_raises_wifi_auth_error_on_nack():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    nack = b'{"ok": false, "error": "auth failed"}'
    stream.on_notify(None, bytearray(len(nack).to_bytes(4, "big") + nack))

    with pytest.raises(WifiAuthError, match="auth failed"):
        channel.send_preamble("status", "wrong-secret")


def test_send_preamble_succeeds_on_ack():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    ack = b'{"ok": true}'
    stream.on_notify(None, bytearray(len(ack).to_bytes(4, "big") + ack))

    channel.send_preamble("status", "right-secret")  # must not raise
```

Add `import pytest` at the top of the test file if not already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_transport_ble.py -v`
Expected: FAIL with `ImportError: cannot import name 'BleControlChannel'`.

- [ ] **Step 3: Implement `BleControlChannel`**

Add to `src/tether/transports/ble.py`, after the existing `WriteStream`/`connect` code:

```python
import struct

_LENGTH_PREFIX = struct.Struct(">I")

# Same resource-safety bound as wifi's control channel (transports/wifi.py)
# and the RPC layer's MAX_FRAME_SIZE - a declared length this large would
# risk buffering an unbounded amount of attacker/bug-controlled data before
# anything is validated.
MAX_CONTROL_FRAME_SIZE = 1 << 16  # 64 KiB


class BleControlChannel:
    """Length-prefixed JSON/bytes framing over an already-connected
    BleStream, for the preamble/status/upload control protocol - mirrors
    transports/wifi.py's free-function helpers, but stateful:
    BleStream.read() returns whatever one GATT notification happened to
    carry (not a caller-chosen byte count, unlike socket.recv(n)), so
    leftover bytes from one frame must persist into the next read - this
    class holds that buffer.

    One instance per BleStream, reused across every preamble on that
    connection - status -> upload -> run all share it (see the design's
    one-connection decision: BLE connection setup is too costly to redo
    per mode, unlike wifi's cheap TCP handshake).
    """

    def __init__(self, stream: BleStream) -> None:
        self._stream = stream
        self._buffer = b""

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self._stream.read()
            if not chunk:
                raise OSError("connection closed while reading a frame")
            self._buffer += chunk
        result, self._buffer = self._buffer[:n], self._buffer[n:]
        return result

    def send_json_frame(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._stream.write(_LENGTH_PREFIX.pack(len(body)) + body)

    def read_json_frame(self) -> dict[str, Any]:
        header = self._recv_exact(_LENGTH_PREFIX.size)
        (length,) = _LENGTH_PREFIX.unpack(header)
        if length > MAX_CONTROL_FRAME_SIZE:
            raise OSError(f"control frame too large: declared {length} bytes")
        body = self._recv_exact(length)
        return json.loads(body.decode("utf-8"))

    def send_bytes_frame(self, data: bytes) -> None:
        self._stream.write(_LENGTH_PREFIX.pack(len(data)) + data)

    def read_bytes_frame(self) -> bytes:
        header = self._recv_exact(_LENGTH_PREFIX.size)
        (length,) = _LENGTH_PREFIX.unpack(header)
        if length > MAX_CONTROL_FRAME_SIZE:
            raise OSError(f"control frame too large: declared {length} bytes")
        return self._recv_exact(length)

    def send_preamble(self, mode: str, secret: str | None) -> None:
        """Send the connection preamble (mode + shared secret) and wait
        for the device's ack. Raises WifiAuthError if the device rejects
        it - matches wifi's send_preamble exactly (see transports/wifi.py).
        """
        from tether.errors import WifiAuthError

        self.send_json_frame({"mode": mode, "secret": secret})
        response = self.read_json_frame()
        if not response.get("ok", False):
            raise WifiAuthError(response.get("error") or "connection rejected by device")
```

Add `import json` at the top of `ble.py` alongside the existing `asyncio`/`concurrent.futures`/`queue`/`threading` imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_transport_ble.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite, lint, format**

```bash
.venv/bin/pytest tests/ -q
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/tether/transports/ble.py tests/test_transport_ble.py
git commit -m "feat: BLE control-channel framing (BleControlChannel) for the preamble/status/upload protocol"
```

---

### Task 2: Extract shared mode-handler source from `_BOOT_PY_TEMPLATE` (pure refactor)

**Files:**
- Modify: `src/tether/provisioning.py`
- Test: `tests/test_provisioning.py` (existing wifi tests must pass unchanged — this task adds no new tests, it's a pure refactor)

**Interfaces:**
- Produces: `_MODE_HANDLER_FUNCTIONS_SRC` (module-level string constant, zero-indented, embedded via `textwrap.indent()` at the call site) containing `_recv_exact`, `_read_json_frame`, `_send_json_frame`, `_handle_status`, `_handle_upload` (unchanged logic, now shared) and `_handle_run` (shared body, parameterized by a `_make_streams` callable — see Step 3).
- Consumes: nothing new; `PROTOCOL_VERSION` (already imported).

This task exists so Task 3 can reuse this exact source text for BLE's boot.py instead of duplicating ~120 lines of hard-won mode-handler logic between two templates.

- [ ] **Step 1: Confirm the refactor's success criterion**

Before changing anything, capture the current generated output:

```bash
.venv/bin/python -c "
from tether.provisioning import _BOOT_PY_TEMPLATE
print(_BOOT_PY_TEMPLATE)
" > /tmp/boot_py_before.txt
```

This task is done only when `_BOOT_PY_TEMPLATE`'s generated text (for a fixed set of inputs — it's an f-string closing over only `PROTOCOL_VERSION` and `DEFAULT_PORT`, both constants) is **byte-identical** before and after. No new test is written for this step; the existing wifi test suite (`tests/test_provisioning.py`) staying green, plus this byte-identical diff, is the verification.

- [ ] **Step 2: Extract `_MODE_HANDLER_FUNCTIONS_SRC`**

In `src/tether/provisioning.py`, add this new module-level constant, placed above `_BOOT_PY_TEMPLATE`:

```python
import textwrap

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

def _handle_status(_conn, _get_addr):
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
        "ip": _get_addr(),
    }})

def _handle_run(_conn, _make_streams):
    try:
        with open("/tether_app.py") as _f:
            _tether_app_src = _f.read()
    except OSError:
        _tether_app_src = None

    if _tether_app_src is not None:
        import uasyncio as _asyncio

        _reader, _writer = _make_streams(_conn)
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
            # See boot.py's own historical comment (design spec, Mode:
            # run) for the full mechanism - MicroPython's uasyncio task
            # queue is process-global; this resets it between sessions so
            # @mcu.loop tasks don't accumulate across reconnects,
            # regardless of transport.
            _asyncio.new_event_loop()

def _handle_upload(_conn):
    try:
        try:
            import uos as _uos

            _uos.remove("/.tether_hash")
        except OSError:
            pass

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
                    if _chunk_len > _remaining:
                        raise OSError("upload chunk exceeds declared file size")
                    _chunk_data = _recv_exact(_conn, _chunk_len)
                    _wf.write(_chunk_data)
                    _remaining -= len(_chunk_data)
        _send_json_frame(_conn, {{"ok": True}})
    except Exception as _exc:
        try:
            _send_json_frame(_conn, {{"ok": False, "error": str(_exc)}})
        except OSError:
            pass
"""
```

Note the two behavioral changes from the original inlined text, both required for sharing and both no-ops for wifi:
- `_handle_status` now takes `_get_addr` (a zero-arg callable) instead of hardcoding `_wlan.ifconfig()[0]` — wifi's call site passes `lambda: _wlan.ifconfig()[0]`, producing byte-identical *output* even though the template text differs.
- `_handle_run` now takes `_make_streams` instead of hardcoding `_asyncio.StreamReader(_conn), _asyncio.StreamWriter(_conn, {{}})` — wifi's call site passes an equivalent lambda (Step 3).
- The disconnect print message drops wifi's literal word "wifi" (now "tether: client disconnected...", transport-neutral) — this is the one **visible** text change; it only appears in server-side console output over a connection that's already ending, never asserted on by any existing test (confirmed by grep below before proceeding).

Before proceeding, confirm no existing test asserts on that exact string:
```bash
grep -rn "wifi client disconnected" tests/
```
Expected: no matches (or if any exist, update them to the new text as part of this step — they'd be asserting on now-stale wording, not behavior).

- [ ] **Step 3: Rewrite `_BOOT_PY_TEMPLATE` to compose from the shared constant**

Replace `_BOOT_PY_TEMPLATE`'s body (the `if _wlan.isconnected():` block's function definitions) with an embed of `_MODE_HANDLER_FUNCTIONS_SRC`, plus wifi's own `_conn`-adapter lambdas passed at each call site. The full new `_BOOT_PY_TEMPLATE`:

```python
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

{textwrap.indent(_MODE_HANDLER_FUNCTIONS_SRC, "        ")}
        def _wifi_make_streams(_conn):
            import uasyncio as _asyncio

            return _asyncio.StreamReader(_conn), _asyncio.StreamWriter(_conn, {{}})

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
                    _handle_status(_conn, lambda: _wlan.ifconfig()[0])
                elif _mode == "run":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_run(_conn, _wifi_make_streams)
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
```

- [ ] **Step 4: Verify byte-identical output modulo the two documented changes**

```bash
.venv/bin/python -c "
from tether.provisioning import _BOOT_PY_TEMPLATE
print(_BOOT_PY_TEMPLATE)
" > /tmp/boot_py_after.txt
diff /tmp/boot_py_before.txt /tmp/boot_py_after.txt
```
Expected: the diff shows *only* the `_handle_status`/`_handle_run` signature changes, their call sites' new lambda arguments, and the disconnect message wording — no other semantic change. If anything else differs, stop and fix before proceeding.

- [ ] **Step 5: Run the full existing test suite**

```bash
.venv/bin/pytest tests/ -q
```
Expected: all existing tests pass unchanged — this is the real proof the refactor preserved behavior. If any wifi test fails, the refactor introduced a regression; do not proceed to Task 3 until this is green.

- [ ] **Step 6: Lint, format, commit**

```bash
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
git add src/tether/provisioning.py
git commit -m "refactor: extract shared mode-handler source (_MODE_HANDLER_FUNCTIONS_SRC) for reuse by BLE's boot.py"
```

---

### Task 3: On-device BLE peripheral — advertising, session loop, `_handle_run`/`_handle_upload`/`_handle_status` reuse

**Files:**
- Modify: `src/tether/provisioning.py`
- Test: `tests/test_provisioning.py`

**Interfaces:**
- Consumes: `_MODE_HANDLER_FUNCTIONS_SRC` (Task 2), `SERVICE_UUID`/`WRITE_CHAR_UUID`/`NOTIFY_CHAR_UUID` (already in `transports/ble.py` — hardcode the same three UUID string literals into the template, not an import, matching how `_BOOT_PY_TEMPLATE` already hardcodes `DEFAULT_PORT` as a literal rather than importing on-device).
- Produces: `_BLE_BOOT_TEMPLATE` (module-level string constant, same shape as `_BOOT_PY_TEMPLATE`).

**This is the largest task in the plan.** It has no real BLE hardware to test against until Task 6; correctness here rests entirely on (a) a faithful fake `bluetooth` module matching MicroPython's real IRQ/GATT API shape, and (b) driving that fake **within one `micropython` process** (no real cross-process channel like wifi's raw sockets exists for GATT), verified via the real `micropython` unix-port interpreter throughout.

#### On-device design, precisely

**The `_conn` adapter (`_BleConn`).** `_handle_status`/`_handle_upload` (shared, Task 2) call `_conn.recv(n)`/`_conn.send(data)` expecting socket semantics. `_BleConn` adapts a BLE connection (an IRQ-fed byte queue for reads, `gatts_notify()` for writes) to that exact contract:

```python
class _BleConn:
    def __init__(self, _ble, _conn_handle, _notify_handle, _mtu, _queue):
        self._ble = _ble
        self._conn_handle = _conn_handle
        self._notify_handle = _notify_handle
        self._mtu = _mtu
        self._queue = _queue  # shared with the IRQ handler - see below
        self._buffer = b""

    def recv(self, _n):
        while len(self._buffer) < _n:
            while not self._queue:
                time.sleep_ms(10)
            self._buffer += self._queue.pop(0)
        _result, self._buffer = self._buffer[:_n], self._buffer[_n:]
        return _result

    def send(self, _data):
        _usable = max(self._mtu - 3, 1)
        for _i in range(0, len(_data), _usable):
            self._ble.gatts_notify(self._conn_handle, self._notify_handle, _data[_i : _i + _usable])
```

`recv()` busy-polls (`sleep_ms`) rather than genuinely blocking — correct because MicroPython's `bluetooth` IRQ callbacks run via the same cooperative scheduler `time.sleep_ms` yields to, and because `_handle_status`/`_handle_upload` are bounded, quick operations that don't need to interleave with other concurrently-scheduled tasks (matching wifi's own `_conn.recv()`, which blocks the single thread for the same reason, just via a real blocking syscall instead of a poll loop — same outcome, different primitive).

**Run mode needs real async, not busy-poll.** While a `run` session is live, `@mcu.loop` background tasks are scheduled via `asyncio.create_task` in the *same* event loop `_handle_run`'s `exec()` runs in — a busy-poll `sleep_ms` loop would stall the scheduler and starve those tasks entirely, silently breaking `@mcu.loop` support specifically for BLE. `_make_streams` for BLE therefore builds a small async Pipe-style adapter (same proven shape as `tests/mpy_runner.py`'s own `PIPE_HARNESS.Pipe`, which already validates this pattern works under the real interpreter):

```python
def _ble_make_streams(_conn_state):
    _ble, _conn_handle, _notify_handle, _mtu, _queue, _leftover = _conn_state

    class _BleAsyncReader:
        def __init__(self):
            self._event = _asyncio.Event()

        async def readexactly(self, _n):
            while len(_leftover[0]) < _n:
                while not _queue:
                    self._event.clear()
                    await self._event.wait()
                _leftover[0] += _queue.pop(0)
                self._event.set()
            _result, _leftover[0] = _leftover[0][:_n], _leftover[0][_n:]
            return _result

    class _BleAsyncWriter:
        def write(self, _data):
            _usable = max(_mtu - 3, 1)
            for _i in range(0, len(_data), _usable):
                _ble.gatts_notify(_conn_handle, _notify_handle, _data[_i : _i + _usable])

        async def drain(self):
            await _asyncio.sleep_ms(0)

    return _BleAsyncReader(), _BleAsyncWriter()
```

Both the sync `_BleConn` and the async reader pull from the **same underlying `_queue` list** (appended to by the IRQ handler) — never concurrently, since a connection is always in exactly one phase (reading a preamble / running `_handle_status`/`_handle_upload` synchronously, or fully inside `_handle_run`'s async world) at a time. `_leftover` is a one-element list used as a mutable box so the async reader's leftover buffer survives across `readexactly()` calls without needing a class instance shared by reference into a closure (MicroPython closures over a plain variable would not observe reassignment from inside the method; the one-element-list-as-box idiom sidesteps this, and is not novel — the shared `PIPE_HARNESS.Pipe` class already sidesteps the same issue by using `self.buf` on an instance instead; using a plain box here rather than a class is an equally valid, slightly more compact choice, kept consistent with how `_BleConn` above already holds its own `self._buffer`).

**IRQ handler and the session loop.** The `bluetooth` module delivers all events (central connect/disconnect, characteristic writes, MTU negotiation) through one callback registered via `ble.irq(handler)`. The handler's only job is to route data into `_queue` and record connection-lifecycle state — no blocking, no I/O, matching MicroPython's own guidance that IRQ callbacks should do minimal work:

```python
_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_MTU_EXCHANGED = 21

_conn_handle = [None]
_mtu = [23]  # BLE's default, pre-negotiation
_queue = []

def _bt_irq(_event, _data):
    if _event == _IRQ_CENTRAL_CONNECT:
        _conn_handle[0] = _data[0]
    elif _event == _IRQ_CENTRAL_DISCONNECT:
        _conn_handle[0] = None
    elif _event == _IRQ_GATTS_WRITE:
        _conn_h, _value_handle = _data[0], _data[1]
        if _value_handle == _write_handle:
            _queue.append(_ble.gatts_read(_value_handle))
    elif _event == _IRQ_MTU_EXCHANGED:
        _mtu[0] = _data[1]
```

Session loop (the BLE analogue of wifi's `while True: accept()`):

```python
while True:
    while _conn_handle[0] is None:
        time.sleep_ms(20)
    _leftover = [b""]
    _conn = _BleConn(_ble, _conn_handle[0], _notify_handle, _mtu[0], _queue)
    try:
        while True:
            _preamble = _read_json_frame(_conn)
            _mode = _preamble.get("mode")
            _secret = _preamble.get("secret")
            _expected_secret = _cfg.get("secret")
            if _expected_secret is not None and _secret != _expected_secret:
                _send_json_frame(_conn, {"ok": False, "error": "auth failed"})
                break
            elif _mode == "status":
                _send_json_frame(_conn, {"ok": True})
                _handle_status(_conn, lambda: _ble_addr_str)
                continue
            elif _mode == "upload":
                _send_json_frame(_conn, {"ok": True})
                _handle_upload(_conn)
                continue
            elif _mode == "run":
                _send_json_frame(_conn, {"ok": True})
                _handle_run(
                    _conn,
                    lambda _c: _ble_make_streams((_ble, _conn_handle[0], _notify_handle, _mtu[0], _queue, _leftover)),
                )
                break
            else:
                _send_json_frame(_conn, {"ok": False, "error": "unknown mode"})
                break
    except Exception:
        pass
    _ble.gap_advertise(100_000, _adv_payload)  # resume advertising
```

Note the loop only reads a *next* preamble after `status`/`upload` (`continue`); `run`, an auth failure, or an unrecognized mode all `break` out to disconnect and re-advertise — exactly the "one connection reused, but `run`/failure/unknown always end it" shape the corrected design spec settled on.

- [ ] **Step 1: Write the fake `bluetooth` module test harness**

Add to `tests/test_provisioning.py` (or a new `tests/ble_fakes.py` imported from it — prefer the latter if this grows past ~60 lines, matching the project's file-size discipline; check current `test_provisioning.py` length first with `wc -l tests/test_provisioning.py` and decide):

```python
FAKE_BLUETOOTH_MODULE_SRC = """
class _FakeBLE:
    _IRQ_CENTRAL_CONNECT = 1
    _IRQ_CENTRAL_DISCONNECT = 2
    _IRQ_GATTS_WRITE = 3
    _IRQ_MTU_EXCHANGED = 21

    def __init__(self):
        self._irq_handler = None
        self.notifications = []  # [(conn_handle, value_handle, bytes), ...]
        self._values = {}
        self.advertising = False
        self.active_state = False

    def active(self, _on):
        self.active_state = _on

    def irq(self, _handler):
        self._irq_handler = _handler

    def config(self, _key):
        if _key == "mac":
            return (0, b"\\xaa\\xbb\\xcc\\xdd\\xee\\xff")
        raise ValueError(_key)

    def gap_advertise(self, _interval_us, _adv_data=None):
        self.advertising = _interval_us is not None

    def gatts_register_services(self, _services):
        # Real API returns nested handle tuples per service/characteristic;
        # the fake only needs to hand back stable integers the test driver
        # and template agree on ahead of time - see test setup.
        return [(100, 101)]

    def gatts_read(self, _value_handle):
        return self._values.get(_value_handle, b"")

    def gatts_write(self, _value_handle, _data):
        self._values[_value_handle] = bytes(_data)

    def gatts_notify(self, _conn_handle, _value_handle, _data):
        self.notifications.append((_conn_handle, _value_handle, bytes(_data)))

    def gatts_set_buffer(self, _value_handle, _size):
        pass

    # --- test-driver-only helpers, not part of the real API ---
    def _simulate_connect(self, _conn_handle):
        self._irq_handler(self._IRQ_CENTRAL_CONNECT, (_conn_handle, 0, b""))

    def _simulate_disconnect(self, _conn_handle):
        self._irq_handler(self._IRQ_CENTRAL_DISCONNECT, (_conn_handle, 0, b""))

    def _simulate_mtu(self, _conn_handle, _mtu):
        self._irq_handler(self._IRQ_MTU_EXCHANGED, (_conn_handle, _mtu))

    def _simulate_write(self, _conn_handle, _value_handle, _data):
        self.gatts_write(_value_handle, _data)
        self._irq_handler(self._IRQ_GATTS_WRITE, (_conn_handle, _value_handle))


class _FakeUUID:
    def __init__(self, _s):
        self._s = _s

    def __eq__(self, _other):
        return isinstance(_other, _FakeUUID) and self._s == _other._s


class _FakeBluetoothModule:
    FLAG_WRITE = 0x0008
    FLAG_NOTIFY = 0x0010
    BLE = _FakeBLE
    UUID = _FakeUUID


import sys as _sys
_sys.modules["bluetooth"] = _FakeBluetoothModule
"""
```

- [ ] **Step 2: Run to confirm the fake loads cleanly under real micropython**

```python
def test_fake_bluetooth_module_loads_and_simulates_connect_write_notify():
    from mpy_runner import run_micropython

    script = FAKE_BLUETOOTH_MODULE_SRC + """
import bluetooth
_ble = bluetooth.BLE()
_ble.active(True)
_ble.irq(lambda e, d: print("irq", e, d))
_ble._simulate_connect(0)
_ble._simulate_write(0, 101, b"hello")
_ble.gatts_notify(0, 100, b"world")
print("notifications:", _ble.notifications)
"""
    out = run_micropython(script)
    assert "irq 1" in out
    assert "irq 3" in out
    assert "notifications: [(0, 100, b'world')]" in out
```

Run: `.venv/bin/pytest tests/test_provisioning.py -k fake_bluetooth -v`
Expected: PASS. This step is pure infrastructure validation before building `_BLE_BOOT_TEMPLATE` against it — if the fake itself doesn't behave, every later test in this task is unreliable.

- [ ] **Step 3: Write the failing test for a full status-mode round trip over the fake, single connection**

```python
def test_ble_boot_status_mode_reports_hash_and_reuses_the_connection_for_a_second_preamble():
    from mpy_runner import run_micropython

    boot_py = generate_ble_boot("irrelevant-secret")["/boot.py"].decode()

    driver = """
import bluetooth, ujson, time

_ble = bluetooth.BLE()

def _frame(obj):
    body = ujson.dumps(obj).encode()
    return len(body).to_bytes(4, "big") + body

# Simulate a central: connect, then send TWO preambles on the same
# connection (status, then status again) - proves the session loop
# reuses one connection rather than requiring a reconnect per mode.
_ble._simulate_connect(0)
time.sleep_ms(50)
_ble._simulate_mtu(0, 100)
_ble._simulate_write(0, 101, _frame({"mode": "status", "secret": "irrelevant-secret"}))
time.sleep_ms(50)
_ble._simulate_write(0, 101, _frame({"mode": "status", "secret": "irrelevant-secret"}))
time.sleep_ms(50)
print("notify_count:", len(_ble.notifications))
"""

    # boot_py runs forever (advertise loop) - run it as the "background"
    # actor and the driver as foreground within ONE combined script, since
    # there is no real cross-process channel for GATT (unlike wifi's real
    # sockets). Structure: boot_py's loop body is extracted and run as an
    # asyncio task alongside the driver coroutine in one process, both
    # sharing the same fake bluetooth module instance.
    combined = _combine_boot_py_with_driver(boot_py, driver)
    out = run_micropython(combined, timeout=5.0)
    assert "notify_count: 4" in out  # 2x ack + 2x status payload
```

This test references a `_combine_boot_py_with_driver` helper — write it now, in `tests/test_provisioning.py` (or alongside the fake, wherever Step 1 landed it):

```python
def _combine_boot_py_with_driver(boot_py_source: str, driver_source: str) -> str:
    """Run boot.py's BLE session loop and a scripted "central" driver as
    two concurrently-scheduled uasyncio tasks in ONE micropython process.
    Necessary because GATT has no real, unfaked cross-process primitive
    like wifi's raw TCP sockets - the fake bluetooth module instance must
    be the literal same Python object on both "sides", which only works
    within a single interpreter.

    Splices boot_py_source's top-level `while True:` session loop body
    into an async function so it can run as a task, sharing FAKE_BLUETOOTH_MODULE_SRC's
    injected `bluetooth` module with the driver. The driver runs to
    completion (it's a scripted, finite sequence) and its final print()
    is this combined script's output.
    """
    return f"""
import uasyncio as _asyncio

{FAKE_BLUETOOTH_MODULE_SRC}

async def _boot_task():
{textwrap.indent(boot_py_source, "    ")}

async def _driver_task():
{textwrap.indent(driver_source, "    ")}

async def _main():
    _t = _asyncio.create_task(_boot_task())
    await _driver_task()

_asyncio.run(_main())
"""
```

Note: `boot_py_source`'s session loop as generated is **synchronous** Python (busy-poll `time.sleep_ms`, not `await`) — running it as a plain function body inside an `async def` works fine under `uasyncio.create_task` *only* if its blocking waits actually yield to the scheduler. `time.sleep_ms()` under MicroPython's uasyncio does **not** yield — it's a real blocking call. This means `_boot_task()` would starve `_driver_task()` entirely once it reaches its own busy-poll loops.

**Fix, applied in Step 4 below (not this test file):** every busy-poll wait in the on-device BLE code (`_BleConn.recv`'s inner loop, the "wait for a connection" loop) must use `await _asyncio.sleep_ms(10)` instead of `time.sleep_ms(10)`, and the surrounding functions (`_BleConn.recv`, the session loop itself) must therefore be `async def`, called with `await`, **except** `_handle_status`/`_handle_upload`/`_recv_exact`/`_send_json_frame` (Task 2's shared, synchronous handlers, which call `_conn.recv`/`_conn.send` as plain synchronous methods — those signatures do not change). Resolve this by making `_BleConn.recv`'s wait loop `await`-based internally via a small synchronous-looking wrapper that is *itself* not a coroutine but is only ever called from within a context that's already inside an async task and can tolerate `asyncio.sleep_ms`'s cooperative yield — concretely: **run the whole session loop, including its calls into the shared synchronous `_handle_status`/`_handle_upload`, as one `async def`,** and give `_BleConn.recv` an async wait primitive it calls via a module-level helper, not `time.sleep_ms` directly:

```python
async def _async_wait_ms(_ms):
    await _asyncio.sleep_ms(_ms)
```

`_BleConn.recv` cannot itself be `async def` (it's called from *synchronous* `_recv_exact`, which Task 2 fixed as shared, non-negotiable text). Resolve the mismatch by having `_BleConn.recv`'s inner wait call a **non-async** yield that still cooperates with uasyncio's scheduler: MicroPython's `uasyncio` schedules via an internal ready-queue serviced by its own run loop; a plain synchronous function has no way to yield into that loop mid-call. This is the one genuine architectural wrinkle `_handle_status`/`_handle_upload`'s reuse costs: **while a synchronous `_handle_status`/`_handle_upload` call is in progress, this micropython process cannot service any other concurrently-scheduled task (including the driver in this test, and, on real hardware, nothing else needs to run anyway during a status/upload exchange — no `@mcu.loop` task is active outside of `run` mode).** This is not a real problem for the actual feature (status/upload are bounded, quick, and no other work needs to happen on-device during them) — it only affects this *test's* ability to interleave a scripted driver with the boot task during status/upload specifically. Resolve it in the test harness, not the production code: drive status/upload preambles from a **separate real thread** if `_thread` is available in this micropython build, or — simpler and avoiding a new dependency — restructure this specific test to run the driver and boot task **sequentially within the busy-poll's own bounded wait window** (the busy-poll loop is only ever a few iterations of 10-20ms for a synchronous exchange that completes in well under `run_micropython`'s timeout), accepting that `_boot_task` blocking the process for tens of milliseconds during a status/upload exchange is fine because the driver's *next* `_simulate_write` call doesn't need to run concurrently — it needs to run **after** the previous exchange fully completes, which is exactly what happens when `_boot_task` returns control at its next `await` (the "wait for a connection"/preamble-read loop, both of which **do** need to be `await _asyncio.sleep_ms(...)`-based for the *session* loop to cooperate at all — only the innermost `_BleConn.recv` busy-wait inside a single synchronous status/upload exchange is briefly non-yielding, and that's acceptable).

Simplify Step 3's assertion accordingly: this test verifies the *sequential* two-preamble exchange completes correctly and produces the right notification count — it does not need genuine interleaving with the driver mid-status-handling, only before/after each preamble.

- [ ] **Step 4: Implement `_BLE_BOOT_TEMPLATE` and `generate_ble_boot()`**

Add to `src/tether/provisioning.py`, following the design above (the `_BleConn`/`_ble_make_streams`/IRQ-handler/session-loop code shown in this task's design section, composed via f-string with `_MODE_HANDLER_FUNCTIONS_SRC` embedded the same way `_BOOT_PY_TEMPLATE` embeds it — same `textwrap.indent()` pattern from Task 2 Step 3). Use the session loop's `await`-based waits exactly as resolved in Step 3 above (the "wait for connection" and preamble-read outer loop are `async def`/`await`-based; `_BleConn.recv`'s inner busy-poll stays a plain synchronous `time.sleep_ms`, accepted as bounded and harmless per Step 3's reasoning).

```python
_BLE_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
_BLE_WRITE_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
_BLE_NOTIFY_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

_BLE_BOOT_TEMPLATE = f"""\
try:
    import ujson as _json
except ImportError:
    import json as _json

try:
    with open("/tether_ble.json") as _f:
        _cfg = _json.loads(_f.read())
except OSError:
    _cfg = None

if _cfg is not None:
    import bluetooth as _bluetooth
    import time
    import uasyncio as _asyncio
    import gc as _gc

    _boot_ms = time.ticks_ms()
    _MAX_CTRL_FRAME = 65536

{textwrap.indent(_MODE_HANDLER_FUNCTIONS_SRC, "    ")}
    _ble = _bluetooth.BLE()
    _ble.active(True)
    _mac = _ble.config("mac")[1]
    _ble_addr_str = ":".join("{{:02x}}".format(_b) for _b in _mac)

    _conn_handle = [None]
    _mtu = [23]
    _queue = []
    _write_handle_box = [None]
    _notify_handle_box = [None]

    def _bt_irq(_event, _data):
        if _event == 1:  # _IRQ_CENTRAL_CONNECT
            _conn_handle[0] = _data[0]
        elif _event == 2:  # _IRQ_CENTRAL_DISCONNECT
            _conn_handle[0] = None
        elif _event == 3:  # _IRQ_GATTS_WRITE
            _conn_h, _value_handle = _data[0], _data[1]
            if _value_handle == _write_handle_box[0]:
                _queue.append(_ble.gatts_read(_value_handle))
        elif _event == 21:  # _IRQ_MTU_EXCHANGED
            _mtu[0] = _data[1]

    _ble.irq(_bt_irq)

    _write_char = (_bluetooth.UUID("{_BLE_WRITE_CHAR_UUID}"), _bluetooth.FLAG_WRITE)
    _notify_char = (_bluetooth.UUID("{_BLE_NOTIFY_CHAR_UUID}"), _bluetooth.FLAG_NOTIFY)
    _service = (_bluetooth.UUID("{_BLE_SERVICE_UUID}"), (_write_char, _notify_char))
    ((_write_handle_box[0], _notify_handle_box[0]),) = _ble.gatts_register_services((_service,))

    class _BleConn:
        def __init__(self):
            self._buffer = b""

        def recv(self, _n):
            while len(self._buffer) < _n:
                while not _queue:
                    time.sleep_ms(10)
                self._buffer += _queue.pop(0)
            _result, self._buffer = self._buffer[:_n], self._buffer[_n:]
            return _result

        def send(self, _data):
            _usable = max(_mtu[0] - 3, 1)
            for _i in range(0, len(_data), _usable):
                _ble.gatts_notify(_conn_handle[0], _notify_handle_box[0], _data[_i : _i + _usable])

    def _ble_make_streams(_conn):
        _leftover = [b""]

        class _BleAsyncReader:
            def __init__(self):
                self._event = _asyncio.Event()

            async def readexactly(self, _n):
                while len(_leftover[0]) < _n:
                    while not _queue:
                        await _asyncio.sleep_ms(10)
                    _leftover[0] += _queue.pop(0)
                _result, _leftover[0] = _leftover[0][:_n], _leftover[0][_n:]
                return _result

        class _BleAsyncWriter:
            def write(self, _data):
                _conn.send(_data)

            async def drain(self):
                await _asyncio.sleep_ms(0)

        return _BleAsyncReader(), _BleAsyncWriter()

    async def _session_loop():
        while True:
            while _conn_handle[0] is None:
                await _asyncio.sleep_ms(20)
            _conn = _BleConn()
            try:
                while True:
                    _preamble = _read_json_frame(_conn)
                    _mode = _preamble.get("mode")
                    _secret = _preamble.get("secret")
                    _expected_secret = _cfg.get("secret")
                    if _expected_secret is not None and _secret != _expected_secret:
                        _send_json_frame(_conn, {{"ok": False, "error": "auth failed"}})
                        break
                    elif _mode == "status":
                        _send_json_frame(_conn, {{"ok": True}})
                        _handle_status(_conn, lambda: _ble_addr_str)
                    elif _mode == "upload":
                        _send_json_frame(_conn, {{"ok": True}})
                        _handle_upload(_conn)
                    elif _mode == "run":
                        _send_json_frame(_conn, {{"ok": True}})
                        _handle_run(_conn, _ble_make_streams)
                        break
                    else:
                        _send_json_frame(_conn, {{"ok": False, "error": "unknown mode"}})
                        break
            except Exception:
                pass
            _ble.gap_advertise(100000, None)

    _ble.gap_advertise(100000, None)
    _asyncio.run(_session_loop())
"""


def generate_ble_boot(
    secret: str | None = None, *, danger_unauthenticated: bool = False
) -> dict[str, bytes]:
    """Return `{"/boot.py": ..., "/tether_ble.json": ...}` file contents
    for `tether provision-ble` to upload. Mirrors `generate_wifi_boot`
    exactly - see its docstring.
    """
    config: dict[str, str] = {}
    if not danger_unauthenticated:
        config["secret"] = secret if secret is not None else secrets.token_hex(16)
    return {
        "/boot.py": _BLE_BOOT_TEMPLATE.encode(),
        "/tether_ble.json": json.dumps(config).encode(),
    }
```

Note the `elif _mode == "status": ... _handle_status(...)` / `elif _mode == "upload": ... _handle_upload(...)` branches deliberately have **no `continue`/explicit loop-back** — they simply fall through the `if/elif` chain to the bottom of the `while True:` body and loop naturally, which is equivalent to `continue` here and slightly simpler; only `run`, auth failure, and unknown-mode explicitly `break`.

- [ ] **Step 5: Run the Step 3 test, iterate until it passes**

```bash
.venv/bin/pytest tests/test_provisioning.py -k ble_boot -v
```
Fix any interpreter-level errors surfaced (e.g., MicroPython syntax restrictions, `gatts_register_services` return-shape mismatches against the fake) by adjusting either the fake or the template — whichever is actually wrong relative to MicroPython's real documented API (check against the fake's own comments, which cite the real API shape).

- [ ] **Step 6: Add a test for the upload → run sequence on one connection**

```python
def test_ble_boot_upload_then_run_reuses_the_same_connection():
    from mpy_runner import run_micropython

    boot_py = generate_ble_boot("s3cr3t")["/boot.py"].decode()

    # A tiny valid tether_app.py bundle (matches what a real PC-side
    # upload would send: a single-file "app" that just registers one
    # @mcu.export-shaped handler via a bare-bones handshake response) -
    # reuse test_provisioning.py's existing `generate_bootstrap`-produced
    # source for a trivial exported function, same as
    # test_boot_py_upload_mode_writes_files_verified_via_status already
    # does for wifi (see that test for the exact pattern to copy).
    tether_app_source = generate_bootstrap("", "")

    driver = f"""
import bluetooth, ujson, time

_ble = bluetooth.BLE()

def _frame(obj):
    body = ujson.dumps(obj).encode()
    return len(body).to_bytes(4, "big") + body

def _bytes_frame(data):
    return len(data).to_bytes(4, "big") + data

_ble._simulate_connect(0)
time.sleep_ms(50)
_ble._simulate_mtu(0, 200)

_ble._simulate_write(0, 101, _frame({{"mode": "upload", "secret": "s3cr3t"}}))
time.sleep_ms(50)
_content = {tether_app_source!r}.encode()
_manifest = _frame({{"dirs": [], "files": [{{"path": "/tether_app.py", "size": len(_content)}}]}})
_ble._simulate_write(0, 101, _manifest)
time.sleep_ms(20)
_ble._simulate_write(0, 101, _bytes_frame(_content))
time.sleep_ms(100)

_ble._simulate_write(0, 101, _frame({{"mode": "status", "secret": "s3cr3t"}}))
time.sleep_ms(50)

print("notify_count:", len(_ble.notifications))
print("last_notify_is_status_ack_or_payload:", _ble.notifications[-1][2][:20])
"""

    combined = _combine_boot_py_with_driver(boot_py, driver)
    out = run_micropython(combined, timeout=5.0)
    assert "notify_count: 4" in out  # upload-ack, upload-result, status-ack, status-payload
```

Run: `.venv/bin/pytest tests/test_provisioning.py -k ble_boot -v`
Expected: PASS once Step 4's implementation is correct. Debug via the interpreter's real error output if not (`run_micropython` surfaces stderr in its `AssertionError` on failure).

- [ ] **Step 7: Add the auth-rejection test**

```python
def test_ble_boot_rejects_wrong_secret_and_disconnects():
    from mpy_runner import run_micropython

    boot_py = generate_ble_boot("correct-secret")["/boot.py"].decode()

    driver = """
import bluetooth, ujson, time

_ble = bluetooth.BLE()

def _frame(obj):
    body = ujson.dumps(obj).encode()
    return len(body).to_bytes(4, "big") + body

_ble._simulate_connect(0)
time.sleep_ms(50)
_ble._simulate_write(0, 101, _frame({"mode": "status", "secret": "wrong-secret"}))
time.sleep_ms(50)
_reply = ujson.loads(_ble.notifications[-1][2])
print("rejected:", _reply["ok"] is False and "auth failed" in _reply.get("error", ""))
print("notify_count:", len(_ble.notifications))
"""

    combined = _combine_boot_py_with_driver(boot_py, driver)
    out = run_micropython(combined, timeout=5.0)
    assert "rejected: True" in out
    # Exactly one notification (the nack) - no status payload follows a
    # rejected preamble, confirming the session ended rather than looping
    # back to read another preamble.
    assert "notify_count: 1" in out
```

- [ ] **Step 8: Run the full suite, lint, format**

```bash
.venv/bin/pytest tests/ -q
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
```

- [ ] **Step 9: Commit**

```bash
git add src/tether/provisioning.py tests/test_provisioning.py
git commit -m "feat: on-device BLE peripheral - advertising, one-connection session loop, run/upload/status reuse"
```

---

### Task 4: PC-side `_connect_ble` — one connection, status → upload-if-needed → run

**Files:**
- Modify: `src/tether/connection.py`
- Test: `tests/test_connection.py`

**Interfaces:**
- Consumes: `BleControlChannel` (Task 1), `_gather_runtime_bundle`, `_hash_bundle`, `_start_and_handshake` (all existing, transport-agnostic already).
- Produces: `_connect_ble(rest, bootstrap, export_specs, exported_names, pc_handlers, *, timeout, secret=None) -> BoardHandle` (new signature — was `_connect_ble(rest, export_specs, pc_handlers, *, timeout)`, matching the *old* pre-upload wifi shape). `connect()`'s `ble` branch updated to slice/bundle like the `wifi` branch, and thread `secret` through.

- [ ] **Step 1: Write the failing test**

Model directly on `tests/test_connection.py`'s existing `test_connect_wifi_uploads_when_hash_differs_then_runs`, adapted for BLE's one-connection model (a single fake `BleakClient` connection carrying all three preambles in sequence, not three separate connections):

```python
def test_connect_ble_uploads_when_hash_differs_then_runs_over_one_connection():
    import asyncio as _asyncio
    import threading

    from tether.connection import _connect_ble
    from tether.slicer import slice_mcu_bound

    source = '''
from tether import mcu

@mcu.export
def add(a: int, b: int) -> int:
    return a + b
'''
    sliced = slice_mcu_bound(source, base_dir=Path("."))
    bootstrap = generate_bootstrap(sliced.source, "")

    received_modes = []

    class _FakeDevice:
        """Stateful fake device: parses length-prefixed JSON/byte frames
        accumulated across write_gatt_char calls and pushes responses
        back via the client's registered notify callback - mirrors the
        real on-device BLE session loop's mode dispatch (one connection,
        sequential preambles). Driven synchronously: write_gatt_char
        calls happen on the fake client's own event-loop thread, the
        same thread queue.Queue.put (inside on_notify) is safe to call
        from directly, no cross-thread handoff needed here.

        Simplification versus the real device: each file's content is
        assumed to arrive in exactly one bytes-frame (true for this
        test's tiny single-function source) - multi-frame chunking
        fidelity is already covered by Task 1's BleControlChannel tests
        and Task 3's on-device tests, not re-verified here.
        """

        def __init__(self, client, received_modes):
            self._client = client
            self._received_modes = received_modes
            self._buffer = b""
            self._state = "await_preamble"
            self._pending_files: list[dict] = []

        def feed(self, data: bytes) -> None:
            self._buffer += bytes(data)
            self._drain()

        def _notify(self, payload: dict) -> None:
            body = json.dumps(payload).encode()
            frame = len(body).to_bytes(4, "big") + body
            self._client._pending_notify_cb(None, bytearray(frame))

        def _notify_raw(self, data: bytes) -> None:
            self._client._pending_notify_cb(None, bytearray(data))

        def _drain(self) -> None:
            while len(self._buffer) >= 4:
                length = int.from_bytes(self._buffer[:4], "big")
                if len(self._buffer) < 4 + length:
                    return
                body = self._buffer[4 : 4 + length]
                self._buffer = self._buffer[4 + length :]
                self._handle_frame(body)

        def _handle_frame(self, body: bytes) -> None:
            if self._state == "await_preamble":
                preamble = json.loads(body)
                mode = preamble["mode"]
                self._received_modes.append(mode)
                self._notify({"ok": True})
                if mode == "status":
                    self._notify(
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "tether_app_hash": None,
                            "free_heap": 100000,
                            "uptime_ms": 500,
                            "ip": "aa:bb:cc:dd:ee:ff",
                        }
                    )
                elif mode == "upload":
                    self._state = "await_manifest"
                elif mode == "run":
                    self._state = "await_handshake"
            elif self._state == "await_manifest":
                manifest = json.loads(body)
                self._pending_files = list(manifest["files"])
                self._state = "await_file_content" if self._pending_files else "await_preamble"
                if not self._pending_files:
                    self._notify({"ok": True})
            elif self._state == "await_file_content":
                self._pending_files.pop(0)  # one frame == one file, see class docstring
                if self._pending_files:
                    return  # still expecting more files' content frames
                self._notify({"ok": True})
                self._state = "await_preamble"
            elif self._state == "await_handshake":
                import msgpack

                request = msgpack.unpackb(body[1:], raw=False)
                assert request["name"] == "__tether_handshake__"
                from tether.marshalling import encode_frame

                self._notify_raw(encode_frame(2, {"id": request["id"], "value": PROTOCOL_VERSION}))
                self._state = "done"

    class _FakeBleakClient:
        mtu_size = 200

        def __init__(self, address, disconnected_callback=None):
            self.address = address
            self._disconnected_callback = disconnected_callback
            self._pending_notify_cb = None
            self.connected = False
            self._device = _FakeDevice(self, received_modes)

        async def connect(self, timeout=10.0):
            self.connected = True

        async def start_notify(self, char_uuid, callback):
            self._pending_notify_cb = callback

        async def write_gatt_char(self, char_uuid, data, response=True):
            self._device.feed(data)

        async def disconnect(self):
            self.connected = False

    with patch("bleak.BleakClient", _FakeBleakClient):
        board = _connect_ble(
            "AA:BB:CC:DD:EE:FF",
            bootstrap,
            sliced.export_specs,
            sliced.exported_names,
            {},
            timeout=5.0,
            secret="test-secret",
        )

    assert received_modes == ["status", "upload", "run"]
    assert board is not None
```

Add `import json` at the top of the test if not already present. The handshake-response construction (`msgpack.unpackb`, `encode_frame(2, {"id": ..., "value": PROTOCOL_VERSION})`) is copied verbatim from `tests/test_connection.py`'s existing `test_connect_wifi_uploads_when_hash_differs_then_runs` (lines ~619-629 as of this plan's writing) — `_start_and_handshake` doesn't care which transport delivered the bytes, so the exact same response construction applies unchanged.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_connection.py -k connect_ble_uploads -v`
Expected: FAIL (`_connect_ble`'s current signature doesn't match, or `bleak` isn't patched at the right import site — adjust the `patch()` target to wherever `transports/ble.py` actually imports `bleak` from, matching its existing lazy `import bleak` inside `connect()`).

- [ ] **Step 3: Rewrite `_connect_ble`**

```python
def _connect_ble(
    rest: str,
    bootstrap: str,
    export_specs: dict[str, Any],
    exported_names: frozenset[str],
    pc_handlers: dict[str, Callable[..., Any]],
    *,
    timeout: float,
    secret: str | None = None,
) -> BoardHandle:
    """Connect over BLE to a boot.py-managed peripheral. Mirrors
    _connect_wifi's slice -> hash-check -> upload-if-needed -> run shape,
    but reuses ONE BLE connection across all three (see the design's
    one-connection decision - BLE connection setup is too costly to redo
    per mode, unlike wifi's cheap TCP handshake). transports/ble.py's
    BleControlChannel provides the length-prefixed framing over that one
    connection's BleStream.
    """
    import os

    from tether.transports import ble as ble_transport

    resolved_secret = secret if secret is not None else os.environ.get("TETHER_BLE_SECRET")

    unsliced = export_specs.keys() - exported_names
    if unsliced:
        raise RuntimeError(
            f"{sorted(unsliced)} are decorated with @mcu.export/@mcu.loop but weren't "
            "found by static analysis of the source - decorated functions must be plain "
            "top-level `def`/`async def` statements, not conditionally defined "
            "(DESIGN.md § Standing design constraint)"
        )

    bundle_hash = _hash_bundle(bootstrap)
    board: BoardHandle | None = None
    # Unlike wifi's per-mode connections, BLE's dial() opens exactly ONE
    # BleStream per call and drives status -> upload-if-needed -> run
    # entirely over it. On reconnect(), the OLD stream must still be
    # closed first - same reasoning as wifi's last_stream (see
    # _connect_wifi), just simpler here since there's only ever one
    # stream per dial() call, not up to three.
    last_stream: Any | None = None

    def dial() -> Dispatcher:
        nonlocal last_stream
        if last_stream is not None:
            last_stream.close()
            last_stream = None

        stream = ble_transport.connect(rest, timeout=timeout)
        last_stream = stream
        try:
            channel = ble_transport.BleControlChannel(stream)

            channel.send_preamble("status", resolved_secret)
            status = channel.read_json_frame()

            if status.get("tether_app_hash") != bundle_hash:
                channel.send_preamble("upload", resolved_secret)
                files, dirs = _gather_runtime_bundle(bootstrap, bundle_hash)
                manifest = {
                    "dirs": list(dirs),
                    "files": [
                        {"path": path, "size": len(content)} for path, content in files.items()
                    ],
                }
                channel.send_json_frame(manifest)
                max_chunk = ble_transport.MAX_CONTROL_FRAME_SIZE
                for content in files.values():
                    for offset in range(0, len(content), max_chunk):
                        channel.send_bytes_frame(content[offset : offset + max_chunk])
                result = channel.read_json_frame()
                if not result.get("ok", False):
                    raise RuntimeError(f"BLE upload failed: {result.get('error')}")

            channel.send_preamble("run", resolved_secret)
            return _start_and_handshake(
                stream,
                timeout=timeout,
                mismatch_hint="update tether or the on-device runtime",
                pc_handlers=pc_handlers,
                board=board,
            )
        except BaseException:
            stream.close()
            last_stream = None
            raise

    board = BoardHandle(dial(), export_specs, dial=dial)
    return board
```

- [ ] **Step 4: Update `connect()`'s `ble` branch**

In `connection.py`'s `connect()`:

```python
elif scheme == "ble":
    sliced = slice_mcu_bound(source, base_dir=base_dir)
    stubs = generate_pc_stubs(source)
    bootstrap = generate_bootstrap(sliced.source, stubs.source)
    board = _connect_ble(
        rest,
        bootstrap,
        export_specs,
        sliced.exported_names,
        pc_handlers,
        timeout=timeout,
        secret=secret,
    )
```

Update `connect()`'s docstring: the `secret` parameter's doc currently says "(wifi only)" — change to "(wifi and BLE)", and note it falls back to `TETHER_WIFI_SECRET` or `TETHER_BLE_SECRET` depending on which transport `address` selects.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_connection.py -k connect_ble_uploads -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/pytest tests/ -q
```
Expected: all green — in particular, confirm no existing BLE test (the old `_connect_ble(rest, export_specs, pc_handlers, timeout=...)`-shaped tests, if any exist under the old signature) broke. Search first:

```bash
grep -n "_connect_ble(" tests/test_connection.py
```
Update any call site still using the old 3-positional-argument signature to the new one, matching the brief's own signature above — same category of fix wifi's Task 6 needed for its old wifi test.

- [ ] **Step 7: Lint, format, commit**

```bash
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
git add src/tether/connection.py tests/test_connection.py
git commit -m "feat: mcu.connect(\"ble:<addr>\") now uploads code over one reused BLE connection"
```

---

### Task 5: `provision-ble` CLI, secret/address printing, `status --ble-addr`, boot.py conflict warnings

**Files:**
- Modify: `src/tether/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `provision_ble_command` (new `tether provision-ble` command), `status_command` gains `--ble-addr`/`--ble-secret` options, both `provision_wifi_command` and the new `provision_ble_command` gain a boot.py-conflict warning check.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py` uses `monkeypatch.setattr(...)` per test and `CliRunner().invoke(main, [...])` directly — no shared fixtures exist in this file (confirmed by reading it in full before writing these). Match that convention exactly, mirroring `test_provision_wifi_uploads_boot_py_and_config`/`test_provision_wifi_prints_the_generated_secret`/`test_status_command_tries_wifi_socket_first` (already implemented, real, passing — read them before writing these) rather than inventing fixtures:

```python
def _patch_fake_serial(monkeypatch):
    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)


def test_provision_ble_generates_secret_and_prints_address(monkeypatch):
    _patch_fake_serial(monkeypatch)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)
    monkeypatch.setattr("tether.transports.serial.read_file", lambda ser, path, timeout=10.0: None)

    written = {}

    def fake_write_files(ser, files, **kwargs):
        written.update(files)

    monkeypatch.setattr("tether.transports.serial.write_files", fake_write_files)
    monkeypatch.setattr(
        "tether.transports.serial.run_python",
        lambda ser, code, timeout=5.0: (b"aa:bb:cc:dd:ee:ff\n", b""),
    )

    result = CliRunner().invoke(main, ["provision-ble", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert set(written.keys()) == {"/boot.py", "/tether_ble.json"}
    assert "Shared secret" in result.output
    assert "aa:bb:cc:dd:ee:ff" in result.output.lower()


def test_provision_ble_warns_if_wifi_already_provisioned(monkeypatch):
    _patch_fake_serial(monkeypatch)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)
    monkeypatch.setattr("tether.transports.serial.write_files", lambda ser, files, **kw: None)
    monkeypatch.setattr(
        "tether.transports.serial.run_python",
        lambda ser, code, timeout=5.0: (b"aa:bb:cc:dd:ee:ff\n", b""),
    )

    def fake_read_file(ser, path, timeout=10.0):
        return b'{"ssid": "x", "password": "y"}' if path == "/tether_wifi.json" else None

    monkeypatch.setattr("tether.transports.serial.read_file", fake_read_file)

    result = CliRunner().invoke(main, ["provision-ble", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert "overwrite" in result.output.lower()
    assert "wifi" in result.output.lower()


def test_provision_wifi_warns_if_ble_already_provisioned(monkeypatch):
    _patch_fake_serial(monkeypatch)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)
    monkeypatch.setattr("tether.transports.serial.write_files", lambda ser, files, **kw: None)

    def fake_read_file(ser, path, timeout=10.0):
        return b'{"secret": "x"}' if path == "/tether_ble.json" else None

    monkeypatch.setattr("tether.transports.serial.read_file", fake_read_file)

    result = CliRunner().invoke(
        main,
        ["provision-wifi", "--port", "/dev/ttyUSB0", "--ssid", "MyNetwork", "--password", "hunter2"],
    )

    assert result.exit_code == 0, result.output
    assert "overwrite" in result.output.lower()
    assert "ble" in result.output.lower()


def test_provision_ble_danger_unauthenticated_prints_no_secret(monkeypatch):
    _patch_fake_serial(monkeypatch)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)
    monkeypatch.setattr("tether.transports.serial.write_files", lambda ser, files, **kw: None)
    monkeypatch.setattr("tether.transports.serial.read_file", lambda ser, path, timeout=10.0: None)
    monkeypatch.setattr(
        "tether.transports.serial.run_python",
        lambda ser, code, timeout=5.0: (b"aa:bb:cc:dd:ee:ff\n", b""),
    )

    result = CliRunner().invoke(
        main, ["provision-ble", "--port", "/dev/ttyUSB0", "--danger-unauthenticated"]
    )

    assert result.exit_code == 0, result.output
    assert "Shared secret" not in result.output
    assert "WARNING" in result.output


def test_status_ble_addr_tries_ble_first_non_destructively(monkeypatch):
    calls = []

    class _FakeStream:
        def close(self):
            calls.append(("close",))

    class _FakeChannel:
        def __init__(self, stream):
            pass

        def send_preamble(self, mode, secret):
            calls.append(("preamble", mode))

        def read_json_frame(self):
            return {
                "protocol_version": 1,
                "tether_app_hash": "abc123",
                "free_heap": 50000,
                "uptime_ms": 1234,
                "ip": "aa:bb:cc:dd:ee:ff",
            }

    monkeypatch.setattr("tether.transports.ble.connect", lambda addr, timeout=5.0: _FakeStream())
    monkeypatch.setattr("tether.transports.ble.BleControlChannel", _FakeChannel)
    monkeypatch.setattr(
        "tether.transports.serial.reset_board", lambda ser: calls.append(("reset_board",))
    )

    result = CliRunner().invoke(
        main, ["status", "--ble-addr", "AA:BB:CC:DD:EE:FF", "--ble-secret", "s3cr3t"]
    )

    assert result.exit_code == 0, result.output
    assert "aa:bb:cc:dd:ee:ff" in result.output.lower()
    assert ("reset_board",) not in calls


def test_status_ble_addr_falls_back_to_serial_when_ble_unreachable(monkeypatch):
    def raise_unreachable(addr, timeout=5.0):
        raise OSError("connection refused")

    monkeypatch.setattr("tether.transports.ble.connect", raise_unreachable)

    _patch_fake_serial(monkeypatch)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)

    status = {"provisioned": True, "connected": False, "ip": None}
    monkeypatch.setattr(
        "tether.transports.serial.run_python",
        lambda ser, code, timeout=10.0: (json.dumps(status).encode(), b""),
    )

    result = CliRunner().invoke(
        main, ["status", "--port", "/dev/ttyUSB0", "--ble-addr", "AA:BB:CC:DD:EE:FF"]
    )

    assert result.exit_code == 0, result.output
    assert "not currently connected" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py -k ble -v`
Expected: FAIL (`No such command 'provision-ble'`, then progressively other failures as each is added).

- [ ] **Step 3: Add a boot.py-conflict check helper**

In `cli.py`:

```python
def _check_other_transport_provisioned(ser: Any, other_config_path: str, other_name: str) -> None:
    """Print a warning (does not block) if `other_config_path` already
    exists on the board - provisioning this transport will overwrite the
    board's single boot.py slot, silently orphaning the other transport's
    credentials file. Matches --danger-unauthenticated's non-blocking
    precedent - scripted provisioning shouldn't hang on a prompt.
    """
    from tether.transports import serial as serial_transport

    existing = serial_transport.read_file(ser, other_config_path, timeout=5.0)
    if existing is not None:
        click.echo(
            f"WARNING: {other_name} is currently provisioned on this board - "
            f"this will overwrite its boot.py and disable {other_name} connectivity "
            f"(its credentials file is left in place but nothing will read it anymore)."
        )
```

- [ ] **Step 4: Add `provision_ble_command`**

```python
@main.command("provision-ble")
@click.option("--port", default=None, help="Serial port (auto-detected if omitted).")
@click.option(
    "--danger-unauthenticated",
    is_flag=True,
    default=False,
    help="Skip generating a shared secret - the BLE listener accepts any connection.",
)
def provision_ble_command(port: str | None, danger_unauthenticated: bool) -> None:
    """Upload a boot.py that advertises tether's BLE service and makes
    the board reachable over tether's BLE transport on every boot.

    Note: this uploads boot.py + BLE config only - the program that
    actually runs when a BLE client connects (tether_app.py) is uploaded
    separately, over BLE itself, the first time `mcu.connect("ble:<addr>")`
    is called (or over a normal serial `mcu.connect(...)` session, which
    still works too).
    """
    from tether import provisioning
    from tether.transports import serial as serial_transport

    resolved_port = _resolve_port(port)

    if danger_unauthenticated:
        click.echo(
            "WARNING: --danger-unauthenticated - this board's BLE listener will "
            "accept connections from anyone in range, no secret required."
        )

    files = provisioning.generate_ble_boot(danger_unauthenticated=danger_unauthenticated)

    with _open_board(resolved_port) as ser:
        serial_transport.reset_board(ser)
        _check_other_transport_provisioned(ser, "/tether_wifi.json", "wifi")
        serial_transport.write_files(ser, files)
        serial_transport.reset_board(ser)
        addr_stdout, _ = serial_transport.run_python(
            ser,
            b'import bluetooth\nb=bluetooth.BLE()\nb.active(True)\n'
            b'print(":".join("{:02x}".format(x) for x in b.config("mac")[1]))\n',
            timeout=5.0,
        )

    click.echo(f"Provisioned {resolved_port} for BLE. Board is restarting.")
    click.echo(f"BLE address: {addr_stdout.decode().strip()}")
    if not danger_unauthenticated:
        import json

        config = json.loads(files["/tether_ble.json"])
        click.echo(f"Shared secret (save this - needed to connect): {config['secret']}")
    click.echo("Run `tether status --ble-addr <address>` in a few seconds to check connectivity.")
```

- [ ] **Step 5: Add the symmetric check to `provision_wifi_command`**

In the existing `provision_wifi_command`, right after `serial_transport.reset_board(ser)` and before `serial_transport.write_files(ser, files)`:

```python
_check_other_transport_provisioned(ser, "/tether_ble.json", "BLE")
```

- [ ] **Step 6: Extend `status_command` with `--ble-addr`/`--ble-secret`**

Add the two new options, and — before the existing `if ip:` block — an analogous `if ble_addr:` block using `ble_transport`/`BleControlChannel`:

```python
@click.option("--ble-addr", default=None, help="Device BLE address (if known) - tried first, over BLE, before falling back to serial.")
@click.option("--ble-secret", default=None, help="BLE shared secret, if the device requires one.")
def status_command(
    port: str | None, ip: str | None, secret: str | None, ble_addr: str | None, ble_secret: str | None
) -> None:
    # existing --port/--ip/--secret click.option decorators and the
    # docstring stay exactly as they are today - only the two new options
    # above and the new `ble_addr`/`ble_secret` parameters are added.
    # `import json`, `import os`, `from tether import provisioning`,
    # `from tether.errors import WifiAuthError`, `from tether.transports
    # import serial as serial_transport`, `from tether.transports import
    # wifi as wifi_transport` at the top of the function body are
    # unchanged - only the block below is new, inserted immediately
    # before the existing `if ip:` line.
    if ble_addr:
        import os as _os

        from tether.errors import WifiAuthError
        from tether.transports import ble as ble_transport

        resolved_ble_secret = (
            ble_secret if ble_secret is not None else _os.environ.get("TETHER_BLE_SECRET")
        )
        payload = None
        stream = None
        try:
            stream = ble_transport.connect(ble_addr, timeout=5.0)
        except Exception:
            stream = None
        if stream is not None:
            try:
                try:
                    channel = ble_transport.BleControlChannel(stream)
                    channel.send_preamble("status", resolved_ble_secret)
                    payload = channel.read_json_frame()
                except WifiAuthError:
                    raise click.ClickException(
                        f"BLE auth failed for {ble_addr} - check --ble-secret/TETHER_BLE_SECRET"
                    ) from None
                except OSError:
                    payload = None
            finally:
                stream.close()
            if payload is not None:
                click.echo(f"Provisioned and connected. Address: {payload['ip']}")
                return

    if ip:
        ...  # existing wifi block, unchanged
```

Note `payload['ip']` is reused as the field name (Task 2's `_handle_status(_conn, _get_addr)` writes whatever the address getter returns into the same `"ip"` JSON key regardless of transport, per the design's decision to keep the payload shape identical across transports) — the CLI just labels it "Address" instead of "IP" in its own echo text for BLE. Confirm precedence: if both `--ip` and `--ble-addr` are given, BLE is tried first (matches this being inserted before the existing `if ip:` block) — document this in the docstring.

- [ ] **Step 7: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_cli.py -k ble -v
```

- [ ] **Step 8: Run the full suite, lint, format**

```bash
.venv/bin/pytest tests/ -q
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
```

- [ ] **Step 9: Commit**

```bash
git add src/tether/cli.py tests/test_cli.py
git commit -m "feat: tether provision-ble, status --ble-addr, and wifi/BLE boot.py conflict warnings"
```

---

### Task 6: Real-hardware verification

**No files changed** — this task is a verification pass against the physically-connected ESP32, exactly mirroring `docs/superpowers/plans/2026-07-26-wifi-modes-auth.md`'s own Task 8. Its outcome is what actually decides whether this feature is done, not the test suite alone: **the wifi plan's final review found a real, hardware-relevant duplication bug that survived seven clean per-task reviews specifically because no test anywhere exercised the real PC client against the real on-device boot.py** — BLE's on-device logic is *more*, not less, exposed to this risk, since even the single-process fake-`bluetooth`-module tests in Task 3 can't exercise real BLE stack behavior (real MTU negotiation quirks, real advertising/connection timing, a real `bleak` central against a real MicroPython peripheral) the way wifi's tests could at least exercise real sockets.

- [ ] **Step 1: Confirm the board and dependencies**

```bash
ls /dev/cu.* 2>&1
.venv/bin/tether devices
uv sync --extra dev --extra serial --extra ble --extra cli
.venv/bin/pytest tests/ -q
```
Expected: board visible, full suite green (baseline before touching hardware).

- [ ] **Step 2: Provision BLE with auth (default)**

```bash
.venv/bin/tether provision-ble --port <port>
```
Expected: secret printed, BLE address printed, no crash. Note the address and secret for later steps.

- [ ] **Step 3: Non-destructive status check**

```bash
.venv/bin/tether status --ble-addr <address> --ble-secret <secret>
```
Expected: `Provisioned and connected. Address: ...`. Confirm this does **not** reset the board (matches wifi's own Task 8 Step 3 — check via a marker, e.g. an `@mcu.loop` counter from a prior run session, if one exists at this point, or simply confirm no reset banner appears on a concurrently-open serial monitor).

- [ ] **Step 4: Real `mcu.connect("ble:<addr>")` end-to-end upload + run**

Write a small script (matching the wifi plan's own `wifi_hw_test.py` pattern) with a trivial `@mcu.export` function, run it, confirm the upload happens (no prior serial upload of `tether_app.py`) and the call succeeds.

- [ ] **Step 5: Script-edit propagation over BLE**

Edit the script's exported function, re-run, confirm the new code took effect (hash-check correctly detects the change and re-uploads) — no serial touched.

- [ ] **Step 6: Reconnect without a reset, `@mcu.loop` duplication check**

The single most important check in this task, mirroring wifi's own headline verification: add an `@mcu.loop`-decorated counter (same pattern as the wifi plan's `wifi_loop_test.py`, using the `mcu_decorators`-module-attribute trick so the counter persists correctly across `exec()` sessions), connect, sample the counter, call `board.reconnect()` twice more (no physical reset), sample after each. Expect roughly flat deltas across all three sessions, not growing — confirms the transport-agnostic `_asyncio.new_event_loop()` fix (already verified for wifi) also holds for BLE's on-device `_handle_run` reuse.

- [ ] **Step 7: Auth rejection**

Attempt `mcu.connect("ble:<addr>", secret="wrong")` and with no secret at all (unset `TETHER_BLE_SECRET`); confirm both raise `WifiAuthError`.

- [ ] **Step 8: `--danger-unauthenticated` end-to-end**

Re-provision with `--danger-unauthenticated`, confirm `mcu.connect("ble:<addr>")` with no secret succeeds.

- [ ] **Step 9: boot.py conflict warning, live**

With BLE currently provisioned, run `tether provision-wifi` against the same board; confirm the warning about overwriting BLE's boot.py appears, and confirm afterward that BLE is indeed no longer reachable (boot.py is now wifi's) while `/tether_ble.json` is still present on the filesystem (confirms the "orphaned, not deleted" behavior the warning describes) — then re-provision BLE to leave the board in a known state afterward.

- [ ] **Step 10: Confirm serial remains undisturbed**

```bash
.venv/bin/python examples/blink_and_log/blink_and_log.py
```
Expected: unaffected by any of the above.

- [ ] **Step 11: Record results**

If all steps pass: update `docs/DESIGN.md`/`CLAUDE.md`/`README.md`/`docs/CHUNKS.md` to reflect BLE's new capability (mirroring the wifi plan's own closing documentation sweep — check whether those docs currently say anything like "BLE never pushes code" and correct it), commit. If any step fails: that's real, load-bearing information this plan's design got wrong somewhere — fix the specific issue (likely in Task 3's on-device logic, given it's the least-tested-by-construction part of this plan), re-run the full suite, and re-verify the failed step before considering this task complete.
