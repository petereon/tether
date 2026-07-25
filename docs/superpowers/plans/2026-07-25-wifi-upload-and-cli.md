# WiFi Provisioning + CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `tether`'s wifi transport usable against a real board by adding a `boot.py`-based on-device wifi listener, provisioned via a new `tether` CLI.

**Architecture:** A new `boot.py` (uploaded once via `tether provision-wifi`) auto-connects to wifi on every boot and bridges an accepted TCP connection into the *existing, unmodified* `tether_app.py` dispatch loop via a small, backward-compatible injection point in `generate_bootstrap()`. The CLI (`click` + `beaupy`) drives this over the same serial/raw-REPL primitives `connect()` already uses — no new upload mechanism, just new content going through the tested path.

**Tech Stack:** Python 3.10+, `click` (CLI), `beaupy` (interactive prompts), `pyserial` (already a dependency), real `micropython` unix-port interpreter for on-device-logic tests, real ESP32-WROOM-32D hardware (physically connected) for end-to-end verification.

## Global Constraints

- **Strict TDD, no exceptions.** Every step below follows write-failing-test → run it, confirm the failure reason → implement → run it, confirm the pass → commit. Do not write implementation code before its test exists and has been run and confirmed to fail for the right reason.
- Design spec: `docs/superpowers/specs/2026-07-25-wifi-upload-design.md` — read it before starting; every task here traces back to a section of it.
- Python floor: `>=3.10` (pyproject.toml) — no walrus-in-comprehension or other 3.11+-only syntax.
- `ruff check .` and `ruff format --check .` must pass after every task (project convention, enforced by `.github/workflows/lint.yml`).
- `.venv/bin/pytest` must pass in full after every task — never leave the suite red between commits.
- BLE provisioning is explicitly out of scope. Do not add anything BLE-specific "while we're at it."
- Real hardware (ESP32-WROOM-32D) is physically connected via `/dev/cu.wchusbserial*` (exact name varies by session — check `ls /dev/cu.*` before hardware steps) for real-hardware verification at the end.
- After all tasks: run the same 4-parallel-cleanup-agent review (reuse/simplification/efficiency/altitude) + manual security review this project has used for every prior chunk (see `docs/CHUNKS.md`'s entries for the pattern), then do real-hardware end-to-end verification, before considering this done. Neither is a numbered task below — both apply to the whole chunk once Tasks 1–7 are complete, the same way every prior chunk in `docs/CHUNKS.md` was closed out.

---

### Task 1: Injectable stream source in `generate_bootstrap()`

**Files:**
- Modify: `src/tether/connection.py:26-83` (the `generate_bootstrap()` function)
- Test: `tests/test_connection.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: the generated bootstrap script now checks `globals().get("_tether_stream_override")` before falling back to `sys.stdin`/`sys.stdout`. Later tasks (boot.py, Task 3) rely on this exact global name (`_tether_stream_override`), set to a `(reader, writer)` 2-tuple.

This is the one piece of *shared* logic between the existing, hardware-verified serial path and the new wifi path — it must be surgical. When nothing sets the override (every existing serial `connect()` call, unchanged), behavior is byte-for-byte identical to today.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_connection.py` (place near the other `generate_bootstrap` tests, e.g. after `test_generate_bootstrap_wires_streams_from_stdin_stdout`):

```python
def test_generate_bootstrap_checks_for_a_stream_override_before_stdio():
    # boot.py (chunk 19, wifi provisioning) sets a `_tether_stream_override`
    # global to (reader, writer) before exec'ing this generated source, to
    # bridge the dispatch loop onto a socket instead of stdio - the
    # override must be checked FIRST, with stdio as the fallback, so every
    # existing serial connect() (which never sets it) is unaffected.
    script = generate_bootstrap("", "")
    assert "_tether_stream_override" in script
    override_check_index = script.index("_tether_stream_override")
    stdio_wiring_index = script.index("sys.stdin.buffer")
    assert override_check_index < stdio_wiring_index
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_connection.py::test_generate_bootstrap_checks_for_a_stream_override_before_stdio -v`
Expected: FAIL — `assert "_tether_stream_override" in script` fails (`AssertionError: assert '_tether_stream_override' in "..."`), since the string doesn't exist in the generated script yet.

- [ ] **Step 3: Write minimal implementation**

In `src/tether/connection.py`, replace the `async def _tether_main():` block inside `generate_bootstrap()`'s returned f-string. Current code (inside the f-string, right after the `_tether_micropython.kbd_intr(-1)` line and the `{sliced_source}`/`{stubs_source}` interpolations):

```python
async def _tether_main():
    _reader = _tether_asyncio.StreamReader(_tether_sys.stdin.buffer)
    _writer = _tether_asyncio.StreamWriter(_tether_sys.stdout.buffer, {{}})
    _dispatcher = _tether_dispatch.Dispatcher(_reader, _writer)
```

Replace with:

```python
async def _tether_main():
    _override = globals().get("_tether_stream_override")
    if _override is not None:
        _reader, _writer = _override
    else:
        _reader = _tether_asyncio.StreamReader(_tether_sys.stdin.buffer)
        _writer = _tether_asyncio.StreamWriter(_tether_sys.stdout.buffer, {{}})
    _dispatcher = _tether_dispatch.Dispatcher(_reader, _writer)
```

Also update `generate_bootstrap()`'s docstring (currently ends "...wires a Dispatcher over sys.stdin/stdout (wrapped as uasyncio streams), registers...") to:

```python
def generate_bootstrap(sliced_source: str, stubs_source: str) -> str:
    """Assemble the final on-device script: imports the runtime shim +
    dispatch loop, disables MicroPython's Ctrl-C keyboard interrupt on the
    UART (see the module-level comment on `import micropython` below),
    defines the sliced @mcu.export/@mcu.loop functions and the generated
    @pc.export proxy stubs, wires a Dispatcher over a pre-set
    `_tether_stream_override` global if one exists (set by boot.py's wifi
    bridge before exec'ing this script - see provisioning.py), falling
    back to sys.stdin/sys.stdout (wrapped as uasyncio streams) for the
    normal serial path, registers every @mcu.export/@mcu.loop function
    plus the protocol handshake handler, and runs the dispatch loop
    forever.
    """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_connection.py::test_generate_bootstrap_checks_for_a_stream_override_before_stdio -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `.venv/bin/pytest tests/ -q`
Expected: all tests pass (this change is additive/backward-compatible — every existing test that exercises `generate_bootstrap()` output over stdio should be untouched).

- [ ] **Step 6: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: `All checks passed!`; format may reformat the file — re-run `.venv/bin/pytest tests/ -q` after if it does.

- [ ] **Step 7: Commit**

```bash
git add src/tether/connection.py tests/test_connection.py
git commit -m "feat: make generate_bootstrap's stream source injectable

Checks for a _tether_stream_override global before falling back to
sys.stdin/sys.stdout - lets boot.py (wifi provisioning, next tasks)
bridge the dispatch loop onto a socket without touching the serial path.
Backward-compatible: nothing sets this override today, so every existing
connect() call is unaffected."
```

---

### Task 2: Serial-layer primitives for provisioning

**Files:**
- Modify: `src/tether/transports/serial.py`
- Test: `tests/test_serial_transport.py`

**Interfaces:**
- Consumes: `push_raw_repl`, `_enter_raw_repl`, `_exec_raw_start`, `_follow_exec`, `_exit_raw_repl` (all already exist in this file).
- Produces: `list_devices(list_ports_fn=None, extra_vid_pid=None) -> list[str]`, `run_python(serial_obj, code: bytes, *, timeout: float = 10.0) -> tuple[bytes, bytes]`, `remove_file(serial_obj, path: str, *, timeout: float = 10.0) -> None`. Tasks 4–7 (the CLI) call all three directly.

Three small, independent, mechanical additions to the same file — bundled into one task since each is low-risk and a reviewer would accept/reject them together, not piecemeal (per this project's task-sizing convention).

#### 2a: `list_devices()`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_serial_transport.py`, right after `test_discover_raises_when_multiple_known_devices_found` (reuses the existing `_FakePortInfo` fixture already defined in that file):

```python
def test_list_devices_returns_all_known_matches():
    def fake_list_ports():
        return [
            _FakePortInfo("/dev/ttyUSB0", 0x10C4, 0xEA60),  # CP210x - known
            _FakePortInfo("/dev/ttyUSB1", 0x1A86, 0x7523),  # CH340 - also known
            _FakePortInfo("/dev/ttyUSB2", 0x1234, 0x5678),  # unknown chip
        ]

    assert list_devices(list_ports_fn=fake_list_ports) == ["/dev/ttyUSB0", "/dev/ttyUSB1"]


def test_list_devices_returns_empty_list_when_none_found():
    def fake_list_ports():
        return [_FakePortInfo("/dev/ttyUSB0", 0x1234, 0x5678)]

    assert list_devices(list_ports_fn=fake_list_ports) == []
```

Add `list_devices` to the existing import block at the top of `tests/test_serial_transport.py`:

```python
from tether.transports.serial import (
    RawReplError,
    SerialStream,
    discover,
    list_devices,
    push_raw_repl,
    read_file,
    remove_file,
    reset_board,
    run_python,
    write_file,
    write_files,
)
```

(This import will fail until all of Task 2 is done — that's expected; run only the specific tests below until then, not the whole file, or collection will error on the missing names.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_serial_transport.py -k list_devices -v`
Expected: FAIL — `ImportError: cannot import name 'list_devices' from 'tether.transports.serial'` (collection error, not a normal assertion failure — expected at this point since the whole import line was updated up front).

- [ ] **Step 3: Write minimal implementation**

In `src/tether/transports/serial.py`, add `list_devices()` immediately before `discover()`, and refactor `discover()` to use it (removing the duplicated filtering logic):

```python
def list_devices(
    list_ports_fn: Callable[[], Any] | None = None,
    extra_vid_pid: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None,
) -> list[str]:
    """Like `discover()`, but returns every matching port instead of
    requiring exactly one. `discover()` treats ambiguity as an error (the
    right default for the library's `serial:auto` scheme) - the CLI wants
    all matches instead, to let the user pick interactively.
    """
    if list_ports_fn is None:
        from serial.tools import list_ports

        list_ports_fn = list_ports.comports

    known = _KNOWN_VID_PID | extra_vid_pid if extra_vid_pid else _KNOWN_VID_PID
    return [p.device for p in list_ports_fn() if (p.vid, p.pid) in known]


def discover(
    list_ports_fn: Callable[[], Any] | None = None,
    extra_vid_pid: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None,
) -> str:
    """Scan connected USB serial devices for a known MicroPython-capable
    VID/PID, return the matching port path. Raises RawReplError if zero or
    multiple ambiguous matches are found.

    `extra_vid_pid` adds (VID, PID) pairs to the built-in list, for boards
    with a bridge chip not already known - the built-in list is a
    convenience covering common chips, not exhaustive (DESIGN.md's
    "supporting ESP32 and similar" scope), and boards with an unlisted
    chip aren't otherwise locked out of auto-discovery.
    """
    matches = list_devices(list_ports_fn, extra_vid_pid)
    if not matches:
        raise RawReplError(
            "no known MicroPython-capable USB serial device found "
            "(pass an explicit port, or extra_vid_pid=... for an unlisted board)"
        )
    if len(matches) > 1:
        ports = ", ".join(matches)
        raise RawReplError(f"multiple matching devices found ({ports}); specify a port explicitly")
    return matches[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_serial_transport.py -k list_devices -v`
Expected: still FAILS with the same `ImportError` — `run_python` and `remove_file` are imported on the same line and don't exist yet. Continue to 2b/2c before this will pass; this is expected mid-task state, not a bug.

#### 2b: `run_python()`

- [ ] **Step 5: Write the failing test**

Add to `tests/test_serial_transport.py`, after the existing `test_read_file_returns_decoded_content` test:

```python
def test_run_python_returns_stdout_and_stderr():
    fake = _FakeMicroPythonSerial(stdout=b"hello\n", stderr=b"")

    stdout, stderr = run_python(fake, b"print('hello')")

    assert stdout == b"hello\n"
    assert stderr == b""


def test_run_python_returns_stderr_without_raising():
    # Unlike read_file/write_file, run_python is a low-level primitive -
    # it hands back whatever the device printed to stderr rather than
    # raising, so callers (e.g. the CLI's status check) can decide for
    # themselves what a given stderr means instead of it always being a
    # hard error.
    fake = _FakeMicroPythonSerial(stdout=b"", stderr=b"Traceback...\nValueError: boom\n")

    stdout, stderr = run_python(fake, b"raise ValueError('boom')")

    assert stdout == b""
    assert b"ValueError: boom" in stderr
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_serial_transport.py -k run_python -v`
Expected: FAIL — same `ImportError` as before (`remove_file` still missing). Continue to 2c.

#### 2c: `remove_file()`

- [ ] **Step 7: Write the failing test**

Add to `tests/test_serial_transport.py`, after the `write_files` tests:

```python
def test_remove_file_deletes_an_existing_file():
    fake = _FakeMicroPythonSerial()

    remove_file(fake, "/tether_wifi.json")

    sent = bytes(fake.written).decode()
    assert "uos.remove('/tether_wifi.json')" in sent


def test_remove_file_does_not_raise_when_file_is_already_gone():
    # uos.remove() raises OSError for a missing file - remove_file must
    # swallow that (matches ensure_dir's existing try/except OSError
    # pattern for mkdir), since "already removed" and "just removed it"
    # should look the same to the caller.
    fake = _FakeMicroPythonSerial(stderr=b"")  # device-side try/except means stderr stays empty

    remove_file(fake, "/tether_wifi.json")  # must not raise
```

- [ ] **Step 8: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_serial_transport.py -k remove_file -v`
Expected: FAIL — `ImportError: cannot import name 'run_python' from 'tether.transports.serial'` (or `remove_file`, whichever the import resolves first — both are still missing).

- [ ] **Step 9: Write minimal implementation**

In `src/tether/transports/serial.py`, add both functions after `read_file()` (the last function in the file):

```python
def run_python(serial_obj: Any, code: bytes, *, timeout: float = 10.0) -> tuple[bytes, bytes]:
    """Execute arbitrary Python source on-device via raw REPL, returning
    (stdout, stderr) - the same enter/exec/follow/exit sequence read_file
    uses internally, exposed as a public, reusable primitive instead of
    read_file staying the only thing that can run code and get output
    back. Does not raise on non-empty stderr (unlike read_file/write_file)
    - callers decide what a given stderr means for their own use case
    (e.g. the CLI's status check).
    """
    _enter_raw_repl(serial_obj, timeout=timeout)
    try:
        _exec_raw_start(serial_obj, code, timeout=timeout)
        return _follow_exec(serial_obj, timeout=timeout)
    finally:
        _exit_raw_repl(serial_obj)


def remove_file(serial_obj: Any, path: str, *, timeout: float = 10.0) -> None:
    """Delete `path` from the device's filesystem via raw REPL. Silently
    succeeds if the file doesn't exist (matches ensure_dir's existing
    try/except OSError pattern) - used by the CLI's unprovision-wifi to
    remove /tether_wifi.json.
    """
    script = f"import uos\ntry:\n    uos.remove({path!r})\nexcept OSError:\n    pass\n"
    push_raw_repl(serial_obj, script.encode(), timeout=timeout)
```

- [ ] **Step 10: Run all of Task 2's tests to verify they pass**

Run: `.venv/bin/pytest tests/test_serial_transport.py -v`
Expected: all tests in the file PASS, including every test added in this task and every pre-existing test in the file (the `discover()` refactor must not change its observable behavior).

- [ ] **Step 11: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass.

- [ ] **Step 12: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: `All checks passed!`

- [ ] **Step 13: Commit**

```bash
git add src/tether/transports/serial.py tests/test_serial_transport.py
git commit -m "feat: add list_devices, run_python, remove_file to serial transport

Three small primitives the CLI (next tasks) needs: list_devices() for
interactive device selection (discover() deliberately errors on
ambiguity, refactored to share list_devices()'s filtering rather than
duplicating it), run_python() as a general run-code-get-output primitive
(read_file's own enter/exec/follow/exit sequence, generalized), and
remove_file() for unprovision-wifi."
```

---

### Task 3: `boot.py` template + `generate_wifi_boot()`

**Files:**
- Create: `src/tether/provisioning.py`
- Test: `tests/test_provisioning.py` (new)

**Interfaces:**
- Consumes: nothing from earlier tasks directly (this module is standalone), but its *output* depends on Task 1's `_tether_stream_override` contract.
- Produces: `generate_wifi_boot(ssid: str, password: str) -> dict[str, bytes]` (returns `{"/boot.py": ..., "/tether_wifi.json": ...}`), `STATUS_SCRIPT: bytes` (module-level constant). Tasks 5–6 (CLI `provision-wifi`/`status` commands) consume both.

This is the highest-risk task — the on-device logic. Every mechanism below (socket bind via `getaddrinfo`, `uasyncio` stream-wrapping a raw socket, `exec()` with an injected namespace reaching a synchronously-invoked `asyncio.run()`) has already been verified against the real `micropython` unix-port interpreter during design, not assumed — this task encodes that verified behavior into tested, generated code.

- [ ] **Step 1: Write the failing test — `generate_wifi_boot()` produces both files with correct content**

Create `tests/test_provisioning.py`:

```python
import json

from tether.provisioning import STATUS_SCRIPT, generate_wifi_boot


def test_generate_wifi_boot_produces_boot_py_and_config():
    files = generate_wifi_boot("MyNetwork", "hunter2")

    assert set(files.keys()) == {"/boot.py", "/tether_wifi.json"}
    assert isinstance(files["/boot.py"], bytes)
    assert isinstance(files["/tether_wifi.json"], bytes)


def test_generate_wifi_boot_embeds_credentials_only_in_the_config_file():
    files = generate_wifi_boot("MyNetwork", "hunter2")

    config = json.loads(files["/tether_wifi.json"])
    assert config == {"ssid": "MyNetwork", "password": "hunter2"}
    # boot.py itself is a fixed template, never contains credentials -
    # keeps re-provisioning (new SSID) a config-file-only change, and
    # keeps credentials out of anything that might get logged/diffed as
    # "the script", as opposed to "the data".
    assert b"hunter2" not in files["/boot.py"]
    assert b"MyNetwork" not in files["/boot.py"]


def test_generate_wifi_boot_checks_for_wifi_config_before_connecting():
    files = generate_wifi_boot("MyNetwork", "hunter2")
    boot_py = files["/boot.py"].decode()

    assert "tether_wifi.json" in boot_py
    # Never-provisioned boards (file absent) must fall straight through -
    # this is what makes an un-provisioned board behave exactly as it did
    # before this feature existed.
    assert "except OSError" in boot_py


def test_status_script_is_valid_python_source():
    import ast

    ast.parse(STATUS_SCRIPT.decode())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_provisioning.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tether.provisioning'`

- [ ] **Step 3: Write minimal implementation**

Create `src/tether/provisioning.py`:

```python
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
        _cfg = _json.load(_f)
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

            exec(
                _tether_app_src,
                {{
                    "_tether_stream_override": (
                        _asyncio.StreamReader(_conn),
                        _asyncio.StreamWriter(_conn, {{}}),
                    )
                }},
            )
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

    _wlan = network.WLAN(network.STA_IF)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_provisioning.py -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Write the failing test — real MicroPython end-to-end verification of the socket bridge**

This is the test that proves the actual on-device mechanism works, using the real `micropython` unix-port interpreter with `network` faked (no real wifi under the unix port) but a genuinely real TCP socket and a genuinely real dispatch loop underneath. Add to `tests/test_provisioning.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mpy_runner import requires_micropython, run_micropython

from tether.connection import PROTOCOL_VERSION, generate_bootstrap


@requires_micropython
def test_boot_py_bridges_a_real_socket_into_the_dispatch_loop():
    # Fakes `network` (no real wifi under the unix port) and monkeypatches
    # the listen port to something unlikely to collide, then runs the
    # REAL generated boot.py content against a REAL client connection made
    # from a background thread using Python's own socket module - proving
    # the exec()/uasyncio/socket mechanism actually works end-to-end, not
    # just that the generated text looks plausible.
    import socket
    import threading
    import time

    test_port = 18765
    boot_py = (
        generate_wifi_boot("irrelevant", "irrelevant")["/boot.py"]
        .decode()
        .replace(str(8765), str(test_port))
    )

    sliced_source = "@mcu.export\ndef add(a: int, b: int) -> int:\n    return a + b"
    tether_app_source = generate_bootstrap(sliced_source, "")

    client_result: dict[str, bytes] = {}

    def run_client() -> None:
        # Give the micropython process a moment to reach _srv.accept().
        time.sleep(1.0)
        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        # A real MSG_CALL frame for __tether_handshake__: [len:4][type:1][msgpack body]
        import msgpack

        from tether.marshalling import encode_frame

        sock.sendall(encode_frame(1, {"id": 1, "name": "__tether_handshake__", "args": []}))
        raw_len = sock.recv(4)
        body_len = int.from_bytes(raw_len, "big")
        body = sock.recv(body_len)
        client_result["msg_type"] = body[:1]
        client_result["payload"] = msgpack.unpackb(body[1:], raw=False)
        sock.close()

    client_thread = threading.Thread(target=run_client, daemon=True)
    client_thread.start()

    script = f"""
import sys as _sys

class _FakeWLAN:
    def __init__(self, *a):
        pass
    def active(self, *a):
        pass
    def isconnected(self):
        return True
    def connect(self, *a):
        pass

class _FakeNetwork:
    STA_IF = 0
    WLAN = _FakeWLAN

_sys.modules["network"] = _FakeNetwork

# /tether_wifi.json and /tether_app.py would normally be real files on
# the device - stand in with in-memory equivalents via a fake `open`.
_files = {{
    "/tether_wifi.json": '{{"ssid": "irrelevant", "password": "irrelevant"}}',
    "/tether_app.py": {tether_app_source!r},
}}

class _FakeFile:
    def __init__(self, content):
        self._content = content
    def read(self):
        return self._content
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

_real_open = open
def _fake_open(path, *a, **kw):
    if path in _files:
        return _FakeFile(_files[path])
    return _real_open(path, *a, **kw)
import builtins
builtins.open = _fake_open

{boot_py}
"""

    run_micropython(script, timeout=10.0)
    client_thread.join(timeout=10.0)

    assert client_result.get("msg_type") == b"\\x02"  # MSG_RESULT
    assert client_result.get("payload") == {{"id": 1, "value": PROTOCOL_VERSION}}
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_provisioning.py::test_boot_py_bridges_a_real_socket_into_the_dispatch_loop -v`
Expected: at this point it should actually PASS, since Step 3's implementation already contains the verified-correct mechanism — this step exists to catch any transcription mistake between what was manually verified during design and what got written into `_BOOT_PY_TEMPLATE`. If it fails, compare the actual generated `boot.py` content (`generate_wifi_boot(...)['/boot.py'].decode()`) line-by-line against the manually-verified script from the design phase and fix the discrepancy — do not change the test to match broken implementation.

- [ ] **Step 7: Run test to verify it (still) passes**

Run: `.venv/bin/pytest tests/test_provisioning.py -v`
Expected: all tests PASS. If `micropython` isn't installed in this environment, the real-hardware-adjacent test is SKIPPED (not failed) — confirm with `.venv/bin/pytest tests/test_provisioning.py -v` output showing `SKIPPED` rather than absent.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass.

- [ ] **Step 9: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add src/tether/provisioning.py tests/test_provisioning.py
git commit -m "feat: boot.py template and generate_wifi_boot()

boot.py auto-connects to wifi (via /tether_wifi.json, absent = does
nothing = pre-feature behavior unchanged) and bridges one accepted TCP
connection into the existing tether_app.py dispatch loop via the
_tether_stream_override injection point from the previous task. Verified
end-to-end against the real micropython interpreter with a real socket
and a real client, network faked (no real wifi under the unix port)."
```

---

### Task 4: CLI scaffolding + `devices` command

**Files:**
- Create: `src/tether/cli.py`
- Modify: `pyproject.toml`
- Test: `tests/test_cli.py` (new)

**Interfaces:**
- Consumes: `tether.transports.serial.list_devices` (Task 2).
- Produces: `tether.cli.main` (click group, the `[project.scripts]` entry point), `tether.cli._resolve_port(port: str | None) -> str` (used by every later command).

This task adds the `cli` extra and entry point (folded in here since it's the first task that needs `click`/`beaupy` importable at all) and the simplest command, to prove the scaffolding works before building the more complex commands on top of it.

- [ ] **Step 1: Add the `cli` extra and entry point**

In `pyproject.toml`, add a new extra alongside the existing `serial`/`ble`/`dev` ones:

```toml
[project.optional-dependencies]
serial = ["pyserial>=3.5"]
ble = ["bleak>=0.21"]
cli = ["click>=8.1", "beaupy>=3.0", "pyserial>=3.5"]
dev = ["pytest>=8.0"]
```

And add a new top-level table for the console script (place after `[project.optional-dependencies]`, before `[build-system]`):

```toml
[project.scripts]
tether = "tether.cli:main"
```

- [ ] **Step 2: Install the new extra and lock**

Run: `uv sync --extra dev --extra serial --extra cli && uv lock`
Expected: `click` and `beaupy` (and their transitive deps) install cleanly; `uv.lock` updates.

- [ ] **Step 3: Write the failing test**

Create `tests/test_cli.py`:

```python
from click.testing import CliRunner

from tether.cli import main


def test_devices_command_lists_known_devices(monkeypatch):
    def fake_list_devices(list_ports_fn=None, extra_vid_pid=None):
        return ["/dev/ttyUSB0", "/dev/ttyUSB1"]

    monkeypatch.setattr("tether.transports.serial.list_devices", fake_list_devices)

    result = CliRunner().invoke(main, ["devices"])

    assert result.exit_code == 0
    assert "/dev/ttyUSB0" in result.output
    assert "/dev/ttyUSB1" in result.output


def test_devices_command_reports_none_found(monkeypatch):
    def fake_list_devices(list_ports_fn=None, extra_vid_pid=None):
        return []

    monkeypatch.setattr("tether.transports.serial.list_devices", fake_list_devices)

    result = CliRunner().invoke(main, ["devices"])

    assert result.exit_code == 0
    assert "no" in result.output.lower()
```

- [ ] **Step 4: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tether.cli'`

- [ ] **Step 5: Write minimal implementation**

Create `src/tether/cli.py`:

```python
"""`tether` CLI - device discovery and wifi provisioning.

Install via `tether[cli]`. Talks to boards over the same serial/raw-REPL
primitives connect() uses (transports/serial.py) - no separate upload
mechanism, just new content going through the already-tested path. See
docs/superpowers/specs/2026-07-25-wifi-upload-design.md for the design.
"""

from __future__ import annotations

import click


def _resolve_port(port: str | None) -> str:
    """Resolve which serial port to use: explicit --port wins; otherwise
    auto-discover, prompting interactively (beaupy.select) if more than
    one known device is connected.
    """
    from tether.transports import serial as serial_transport

    if port:
        return port

    devices = serial_transport.list_devices()
    if not devices:
        raise click.ClickException(
            "no known MicroPython-capable USB serial device found - pass --port explicitly"
        )
    if len(devices) == 1:
        return devices[0]

    import beaupy

    selected = beaupy.select(devices)
    if selected is None:
        raise click.ClickException("no device selected")
    return selected


@click.group()
def main() -> None:
    """tether: call MicroPython functions from Python and back."""


@main.command("devices")
def devices_command() -> None:
    """List connected MicroPython-capable USB serial devices."""
    from tether.transports import serial as serial_transport

    devices = serial_transport.list_devices()
    if not devices:
        click.echo("No known MicroPython-capable USB serial devices found.")
        return
    for device in devices:
        click.echo(device)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 7: Verify the console script entry point works**

Run: `uv run tether devices`
Expected: runs without a Python traceback — either lists real connected devices (the ESP32 should show up, matching whichever CH340/etc VID:PID it enumerates as) or prints "No known MicroPython-capable USB serial devices found." if none are plugged in at that moment. This proves `[project.scripts]` wiring is correct, not just that the Python function works under `CliRunner`.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass. (Note: from this task onward, running the *full* local suite requires `--extra cli` to have been synced — CI's `test.yml` deliberately does not install `cli` today; Task 4's later step below addresses this.)

- [ ] **Step 9: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: `All checks passed!`

- [ ] **Step 10: Update `.github/workflows/test.yml` to install the `cli` extra**

Without this, `tests/test_cli.py` (and every CLI test in Tasks 5–7) will fail in CI with `ModuleNotFoundError: No module named 'click'`. In `.github/workflows/test.yml`, change:

```yaml
      - run: uv sync --locked --extra dev --extra serial
```

to:

```yaml
      - run: uv sync --locked --extra dev --extra serial --extra cli
```

- [ ] **Step 11: Commit**

```bash
git add src/tether/cli.py tests/test_cli.py pyproject.toml uv.lock .github/workflows/test.yml
git commit -m "feat: CLI scaffolding + devices command

New tether[cli] extra (click, beaupy, pyserial) and console-script entry
point. First command (devices) proves the scaffolding end-to-end,
including the real console script, not just the click group under
CliRunner. Updated test.yml to install the cli extra so CLI tests
actually run there."
```

---

### Task 5: `provision-wifi` command

**Files:**
- Modify: `src/tether/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_resolve_port` (Task 4), `tether.provisioning.generate_wifi_boot` (Task 3), `tether.transports.serial.reset_board`/`write_files` (existing + Task 2).
- Produces: `tether provision-wifi` command.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_provision_wifi_uploads_boot_py_and_config(monkeypatch):
    written = {}

    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            self.port = port

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)

    def fake_write_files(ser, files, **kwargs):
        written.update(files)

    monkeypatch.setattr("tether.transports.serial.write_files", fake_write_files)

    result = CliRunner().invoke(
        main,
        [
            "provision-wifi",
            "--port",
            "/dev/ttyUSB0",
            "--ssid",
            "MyNetwork",
            "--password",
            "hunter2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert set(written.keys()) == {"/boot.py", "/tether_wifi.json"}
    assert b"MyNetwork" in written["/tether_wifi.json"]


def test_provision_wifi_prompts_for_password_when_omitted(monkeypatch):
    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)
    monkeypatch.setattr("tether.transports.serial.write_files", lambda ser, files, **kw: None)

    prompted = {}

    def fake_prompt(message, secure=False):
        prompted["secure"] = secure
        return "prompted-password"

    monkeypatch.setattr("beaupy.prompt", fake_prompt)

    result = CliRunner().invoke(
        main, ["provision-wifi", "--port", "/dev/ttyUSB0", "--ssid", "MyNetwork"]
    )

    assert result.exit_code == 0, result.output
    assert prompted["secure"] is True
```

Add `CliRunner` import at the top of `tests/test_cli.py` if not already present from Task 4 (it is — no change needed there).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -k provision_wifi -v`
Expected: FAIL — `AssertionError: 2 == 0` (or similar) since the `provision-wifi` command doesn't exist yet; `result.exit_code` will be a click "no such command" error (exit code 2).

- [ ] **Step 3: Write minimal implementation**

Add to `src/tether/cli.py`, after `devices_command`:

```python
@main.command("provision-wifi")
@click.option("--port", default=None, help="Serial port (auto-detected if omitted).")
@click.option("--ssid", required=True, help="WiFi network name.")
@click.option("--password", default=None, help="WiFi password (prompted if omitted).")
def provision_wifi_command(port: str | None, ssid: str, password: str | None) -> None:
    """Upload a boot.py that auto-connects to WiFi and makes the board
    reachable over tether's wifi transport on every boot.
    """
    import beaupy
    import serial as pyserial

    from tether import provisioning
    from tether.transports import serial as serial_transport

    resolved_port = _resolve_port(port)
    if password is None:
        password = beaupy.prompt("WiFi password:", secure=True)

    files = provisioning.generate_wifi_boot(ssid, password)

    ser = pyserial.Serial(resolved_port, baudrate=115200, timeout=1.0)
    try:
        serial_transport.reset_board(ser)
        serial_transport.write_files(ser, files)
        serial_transport.reset_board(ser)
    finally:
        ser.close()

    click.echo(f"Provisioned {resolved_port} for wifi network {ssid!r}. Board is restarting.")
    click.echo("Run `tether status` in a few seconds to check connectivity.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -k provision_wifi -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: `All checks passed!`

- [ ] **Step 7: Real-hardware verification**

An ESP32 is physically connected. Find its port first:

Run: `ls /dev/cu.* | grep -i usbserial` (or `uv run tether devices`)

Then run the real command against it (replace `<PORT>` and use a real 2.4GHz wifi network reachable from wherever the board is):

Run: `uv run tether provision-wifi --port <PORT> --ssid "<REAL_SSID>" --password "<REAL_PASSWORD>"`
Expected: exits 0, prints the "Provisioned ... Board is restarting" message. Note any traceback or hang here as a real bug to fix before continuing — do not proceed to Task 6 on an unverified `provision-wifi`.

- [ ] **Step 8: Commit**

```bash
git add src/tether/cli.py tests/test_cli.py
git commit -m "feat: tether provision-wifi command

Uploads boot.py + tether_wifi.json via the existing write_files/
reset_board primitives, resetting again afterward so the board picks up
wifi immediately rather than waiting for a future manual reset.
Interactive password prompt (beaupy, hidden input) when --password is
omitted. Verified against real ESP32 hardware."
```

---

### Task 6: `status` command

**Files:**
- Modify: `src/tether/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_resolve_port` (Task 4), `tether.provisioning.STATUS_SCRIPT` (Task 3), `tether.transports.serial.reset_board`/`run_python` (existing + Task 2).
- Produces: `tether status` command.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
import json


def test_status_command_reports_connected_with_ip(monkeypatch):
    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)

    status = {"provisioned": True, "connected": True, "ip": "192.168.1.42"}

    def fake_run_python(ser, code, timeout=10.0):
        return json.dumps(status).encode() + b"\n", b""

    monkeypatch.setattr("tether.transports.serial.run_python", fake_run_python)

    result = CliRunner().invoke(main, ["status", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert "192.168.1.42" in result.output


def test_status_command_reports_not_provisioned(monkeypatch):
    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)

    status = {"provisioned": False, "connected": False, "ip": None}
    monkeypatch.setattr(
        "tether.transports.serial.run_python",
        lambda ser, code, timeout=10.0: (json.dumps(status).encode(), b""),
    )

    result = CliRunner().invoke(main, ["status", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert "not provisioned" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -k status_command -v`
Expected: FAIL — click "no such command" (exit code 2), `status` doesn't exist yet.

- [ ] **Step 3: Write minimal implementation**

Add to `src/tether/cli.py`, after `provision_wifi_command`:

```python
@main.command("status")
@click.option("--port", default=None, help="Serial port (auto-detected if omitted).")
def status_command(port: str | None) -> None:
    """Check whether a board is wifi-provisioned and currently connected."""
    import json

    import serial as pyserial

    from tether import provisioning
    from tether.transports import serial as serial_transport

    resolved_port = _resolve_port(port)
    ser = pyserial.Serial(resolved_port, baudrate=115200, timeout=1.0)
    try:
        serial_transport.reset_board(ser)
        stdout, stderr = serial_transport.run_python(ser, provisioning.STATUS_SCRIPT, timeout=10.0)
    finally:
        ser.close()

    if stderr:
        raise click.ClickException(f"status check failed: {stderr.decode(errors='replace')}")

    info = json.loads(stdout.decode().strip())
    if not info["provisioned"]:
        click.echo("Not provisioned for wifi. Run `tether provision-wifi` first.")
    elif info["connected"]:
        click.echo(f"Provisioned and connected. IP: {info['ip']}")
    else:
        click.echo("Provisioned but not currently connected to wifi.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -k status_command -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: `All checks passed!`

- [ ] **Step 7: Real-hardware verification**

Using the same board provisioned in Task 5 (wait a few seconds after that provisioning for it to finish connecting):

Run: `uv run tether status --port <PORT>`
Expected: exits 0, reports "Provisioned and connected. IP: ..." with a real IP address on the provisioned network. If it reports "not currently connected," check the wifi credentials used in Task 5 and re-provision before continuing.

- [ ] **Step 8: Commit**

```bash
git add src/tether/cli.py tests/test_cli.py
git commit -m "feat: tether status command

Runs provisioning.STATUS_SCRIPT via run_python (a fresh hardware reset
first, same as every other command - checking status necessarily
interrupts whatever the board is doing, a documented tradeoff from the
design spec). Verified against real, already-provisioned ESP32 hardware."
```

---

### Task 7: `unprovision-wifi` command

**Files:**
- Modify: `src/tether/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_resolve_port` (Task 4), `tether.transports.serial.reset_board`/`remove_file` (existing + Task 2).
- Produces: `tether unprovision-wifi` command.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_unprovision_wifi_removes_config_after_confirmation(monkeypatch):
    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)
    monkeypatch.setattr("beaupy.confirm", lambda question: True)

    removed = {}

    def fake_remove_file(ser, path, **kwargs):
        removed["path"] = path

    monkeypatch.setattr("tether.transports.serial.remove_file", fake_remove_file)

    result = CliRunner().invoke(main, ["unprovision-wifi", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert removed["path"] == "/tether_wifi.json"


def test_unprovision_wifi_does_nothing_without_confirmation(monkeypatch):
    monkeypatch.setattr("beaupy.confirm", lambda question: False)

    removed = {}
    monkeypatch.setattr(
        "tether.transports.serial.remove_file",
        lambda ser, path, **kw: removed.setdefault("called", True),
    )

    result = CliRunner().invoke(main, ["unprovision-wifi", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert "called" not in removed
    assert "cancelled" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -k unprovision_wifi -v`
Expected: FAIL — click "no such command" (exit code 2).

- [ ] **Step 3: Write minimal implementation**

Add to `src/tether/cli.py`, after `status_command`:

```python
@main.command("unprovision-wifi")
@click.option("--port", default=None, help="Serial port (auto-detected if omitted).")
def unprovision_wifi_command(port: str | None) -> None:
    """Remove stored WiFi credentials from a board."""
    import beaupy
    import serial as pyserial

    from tether.transports import serial as serial_transport

    resolved_port = _resolve_port(port)
    if not beaupy.confirm(f"Remove wifi credentials from {resolved_port}?"):
        click.echo("Cancelled.")
        return

    ser = pyserial.Serial(resolved_port, baudrate=115200, timeout=1.0)
    try:
        serial_transport.reset_board(ser)
        serial_transport.remove_file(ser, "/tether_wifi.json")
    finally:
        ser.close()

    click.echo(f"Removed wifi credentials from {resolved_port}.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -k unprovision_wifi -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: `All checks passed!`

- [ ] **Step 7: Real-hardware verification**

Using the same board:

Run: `uv run tether unprovision-wifi --port <PORT>` (answer the confirmation prompt `y`)
Expected: exits 0, prints "Removed wifi credentials from ...". Then verify it actually took effect:

Run: `uv run tether status --port <PORT>`
Expected: "Not provisioned for wifi." — confirms `boot.py` correctly falls through to idle REPL again once `/tether_wifi.json` is gone, and that a plain `mcu.connect("serial:auto")` still works unaffected (optional final check: run `examples/blink_and_log/blink_and_log.py` against this now-unprovisioned board once more, exactly as in chunk 18, to confirm the serial path is completely undisturbed by everything built in this plan).

- [ ] **Step 8: Commit**

```bash
git add src/tether/cli.py tests/test_cli.py
git commit -m "feat: tether unprovision-wifi command

Removes /tether_wifi.json after a beaupy.confirm() prompt (destructive -
kills the board's wifi reachability, unlike the other commands). Verified
against real hardware, including confirming a plain serial connect()
still works completely unaffected afterward."
```

---

## After all tasks

1. **4-angle review + security pass.** Same process as every prior chunk (`docs/CHUNKS.md`): 4 parallel review agents (reuse/simplification/efficiency/altitude) against the full diff since this plan started, plus a manual security review (this touches wifi credentials specifically — check they're never logged, and confirm the plaintext-on-device tradeoff from the spec is still accurately documented somewhere a reader would find it). Apply or explicitly skip-with-reasoning every finding, matching this project's established pattern.
2. **Update `docs/CHUNKS.md`** with a new chunk entry (following the existing numbering and format) summarizing what was built, real findings from implementation (if any — the getaddrinfo/exec() mechanisms were verified during design, but implementation may surface more), and real-hardware verification results.
3. **Update `README.md` and `docs/DESIGN.md`**, same places corrected on 2026-07-25 to say wifi "isn't usable against a real device yet" — they need updating again now that it is. Update the Transports table/status section in both to describe the new `tether provision-wifi` workflow.
4. **Full real-hardware regression check**: run `examples/blink_and_log/blink_and_log.py` (unmodified, serial-only) once more at the very end, after all wifi work is committed, to confirm nothing in this plan regressed the existing working serial path.
