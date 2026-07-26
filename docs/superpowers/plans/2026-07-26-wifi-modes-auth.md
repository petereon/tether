# WiFi Upload, Auth, and Non-Interrupting Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make wifi actually push code (closing the "wifi never uploads" gap), add shared-secret authentication to the wifi listener, and make `tether status` non-destructive (no reset, no interrupted listener).

**Architecture:** Restructure `boot.py` from a one-shot "accept one connection, bridge it" script into a loop that repeatedly accepts connections, each starting with a small authenticated preamble (mode: `run`/`upload`/`status`) using a self-contained, dependency-free length-prefixed JSON control channel (not msgpack — the vendored `umsgpack.py` may not exist yet on a board that's only ever been wifi-provisioned). `run` mode's existing `exec()`-based dispatch bridge is unchanged in mechanism but now repeatable; `upload` and `status` are new. `mcu.connect("wifi:<ip>")` gains the same slice → hash-check → upload-if-needed → run flow serial already has, with the hash-check piggybacked on a `status`-mode query instead of a separate step.

**Tech Stack:** Python 3.10+, real MicroPython (`micropython` unix-port binary) for on-device logic tests, real ESP32 hardware for final verification.

## Global Constraints

- **Strict TDD, no exceptions.** Write failing test → run it, confirm the failure reason → implement → run it, confirm it passes → commit.
- Design spec: `docs/superpowers/specs/2026-07-25-wifi-modes-auth-design.md` — read it before starting; every task here traces back to a section of it. Note its 2026-07-26 correction: the preamble/status/upload control channel is length-prefixed **JSON** via `ujson`, not msgpack.
- `ruff check .` and `ruff format --check .` must pass after every task.
- `.venv/bin/pytest` must pass in full after every task — never leave the suite red between commits.
- Every control-channel frame (preamble, status payload, upload manifest, per-chunk byte frames) is bounded the same way the RPC layer's `MAX_FRAME_SIZE` already is — declared lengths over 64 KiB (`65536`) must be rejected, not trusted.
- The preamble's secret comparison is plain equality, not constant-time — an accepted, documented tradeoff (see spec), not an oversight.
- `--danger-unauthenticated` prints a warning; it does not block on an interactive confirmation.
- Real hardware (the ESP32 used for the original wifi feature) is available for the final verification task.

---

### Task 1: `WifiAuthError` + PC-side wire helpers

**Files:**
- Modify: `src/tether/errors.py`
- Modify: `src/tether/transports/wifi.py`
- Test: `tests/test_transport_wifi.py`

**Interfaces:**
- Produces: `tether.errors.WifiAuthError(TetherError)`; `tether.transports.wifi.send_json_frame(sock, payload: dict) -> None`; `read_json_frame(sock) -> dict`; `send_bytes_frame(sock, data: bytes) -> None`; `read_bytes_frame(sock) -> bytes`; `send_preamble(sock, mode: str, secret: str | None) -> None` (sends the preamble, reads the device's ack, raises `WifiAuthError` on rejection).
- Consumes: nothing new (pure additions to an existing small module).

- [ ] **Step 1: Write the failing test for the JSON frame round trip**

Add to `tests/test_transport_wifi.py`:

```python
def test_send_json_frame_and_read_json_frame_round_trip():
    a, b = socket.socketpair()

    send_json_frame(a, {"mode": "status", "secret": "abc123"})

    assert read_json_frame(b) == {"mode": "status", "secret": "abc123"}


def test_send_bytes_frame_and_read_bytes_frame_round_trip():
    a, b = socket.socketpair()

    send_bytes_frame(a, b"some raw content, not text")

    assert read_bytes_frame(b) == b"some raw content, not text"
```

Add to the import line at the top of the file:

```python
from tether.transports.wifi import (
    WifiStream,
    connect,
    read_bytes_frame,
    read_json_frame,
    send_bytes_frame,
    send_json_frame,
    send_preamble,
)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_transport_wifi.py -v`
Expected: FAIL — `ImportError: cannot import name 'send_json_frame' from 'tether.transports.wifi'` (collection error, expected since none of these names exist yet — `send_preamble` is imported too and will fail collection until Step 5 lands, same as this project's established pattern of adding all needed imports up front and treating collection failures as expected mid-task state).

- [ ] **Step 3: Write minimal implementation for the frame helpers**

Add to `src/tether/transports/wifi.py`, after the existing imports (`from __future__ import annotations`, `import socket`, `from typing import Any`):

```python
import json
import struct

_LENGTH_PREFIX = struct.Struct(">I")

# Same resource-safety bound as the RPC layer's MAX_FRAME_SIZE
# (tether/marshalling) - a declared length this large would risk buffering
# an unbounded amount of attacker/bug-controlled data before anything is
# validated, same risk shape regardless of what's inside the frame.
MAX_CONTROL_FRAME_SIZE = 1 << 16  # 64 KiB
```

Add these functions after `WifiStream`'s class definition, before `connect()`:

```python
def _recv_exact(sock: Any, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("connection closed while reading a frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_json_frame(sock: Any, payload: dict[str, Any]) -> None:
    """Send `[4-byte length][utf-8 json body]`. Used for the wifi
    preamble/status/upload control channel - deliberately not the msgpack
    frame format the RPC layer (tether.marshalling) uses, since msgpack
    decoding depends on the vendored umsgpack.py, which may not exist yet
    on a board this control channel is partly responsible for provisioning
    in the first place (see the design spec's 2026-07-26 correction).
    """
    body = json.dumps(payload).encode("utf-8")
    sock.sendall(_LENGTH_PREFIX.pack(len(body)) + body)


def read_json_frame(sock: Any) -> dict[str, Any]:
    header = _recv_exact(sock, _LENGTH_PREFIX.size)
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAX_CONTROL_FRAME_SIZE:
        raise OSError(f"control frame too large: declared {length} bytes")
    body = _recv_exact(sock, length)
    return json.loads(body.decode("utf-8"))


def send_bytes_frame(sock: Any, data: bytes) -> None:
    """Send `[4-byte length][raw bytes]` - used for upload mode's file
    content, which is not JSON-wrapped (JSON can't carry arbitrary binary
    cleanly, and there's no need to make it).
    """
    sock.sendall(_LENGTH_PREFIX.pack(len(data)) + data)


def read_bytes_frame(sock: Any) -> bytes:
    header = _recv_exact(sock, _LENGTH_PREFIX.size)
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAX_CONTROL_FRAME_SIZE:
        raise OSError(f"control frame too large: declared {length} bytes")
    return _recv_exact(sock, length)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_transport_wifi.py -k "round_trip" -v`
Expected: PASS (2 tests). The full file still fails to collect (`send_preamble` doesn't exist yet) — that's expected, continue to Step 5.

- [ ] **Step 5: Write the failing tests for `send_preamble`**

Add to `tests/test_transport_wifi.py`:

```python
def test_send_preamble_succeeds_when_device_acks():
    a, b = socket.socketpair()

    def fake_device():
        preamble = read_json_frame(b)
        assert preamble == {"mode": "status", "secret": "right-secret"}
        send_json_frame(b, {"ok": True})

    device_thread = threading.Thread(target=fake_device, daemon=True)
    device_thread.start()

    send_preamble(a, "status", "right-secret")  # must not raise

    device_thread.join(timeout=2.0)


def test_send_preamble_raises_wifi_auth_error_when_device_rejects():
    a, b = socket.socketpair()

    def fake_device():
        read_json_frame(b)
        send_json_frame(b, {"ok": False, "error": "auth failed"})

    device_thread = threading.Thread(target=fake_device, daemon=True)
    device_thread.start()

    with pytest.raises(WifiAuthError, match="auth failed"):
        send_preamble(a, "status", "wrong-secret")

    device_thread.join(timeout=2.0)
```

Update the imports at the top of `tests/test_transport_wifi.py`:

```python
import threading

import pytest

from tether.errors import WifiAuthError
```

(add `threading` next to the existing `import socket`; add `pytest` and `from tether.errors import WifiAuthError` as new top-level imports).

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_transport_wifi.py -v`
Expected: FAIL — `ImportError: cannot import name 'send_preamble' from 'tether.transports.wifi'` (and `WifiAuthError` doesn't exist in `tether.errors` yet either).

- [ ] **Step 7: Write minimal implementation**

Add to `src/tether/errors.py`, after the existing `ProtocolVersionError` class:

```python
class WifiAuthError(TetherError):
    """The wifi listener rejected this connection's shared secret (or its
    absence) during the mode-selection preamble.
    """
```

Add to `src/tether/transports/wifi.py`, after `read_bytes_frame`:

```python
def send_preamble(sock: Any, mode: str, secret: str | None) -> None:
    """Send the connection preamble (mode + shared secret) and wait for the
    device's ack. Raises WifiAuthError if the device rejects it - every
    mode gets an explicit ack/nack before any mode-specific work begins,
    including `run` (one extra round trip, worth it so a bad secret always
    surfaces as a clear WifiAuthError rather than a confusing downstream
    failure specific to whichever mode was requested).
    """
    from tether.errors import WifiAuthError

    send_json_frame(sock, {"mode": mode, "secret": secret})
    response = read_json_frame(sock)
    if not response.get("ok", False):
        raise WifiAuthError(response.get("error") or "connection rejected by device")
```

- [ ] **Step 8: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_transport_wifi.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 9: Run the full suite, lint, format**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: all pass; `ruff format` may reformat, re-run pytest after if so.

- [ ] **Step 10: Commit**

```bash
git add src/tether/errors.py src/tether/transports/wifi.py tests/test_transport_wifi.py
git commit -m "feat: WifiAuthError + PC-side wifi control-channel wire helpers

Length-prefixed JSON frame send/receive (send_json_frame/read_json_frame)
and raw-byte frame send/receive (send_bytes_frame/read_bytes_frame), plus
send_preamble() which sends the mode+secret preamble and raises
WifiAuthError on rejection. Deliberately JSON via ujson, not msgpack -
see the design spec's correction on why reusing the RPC layer's msgpack
framing here would be circular."
```

---

### Task 2: Fix the `mcu_decorators` registry accumulation bug in `generate_bootstrap()`

**Files:**
- Modify: `src/tether/connection.py`
- Test: `tests/test_connection.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `generate_bootstrap()`'s output now clears `mcu_decorators._registrations` at the top of the generated script, before any `@mcu.export`/`@mcu.loop`/`@pc.export` decorator applications run. No signature change.

This is the real, non-obvious bug traced in the design spec: `mcu_decorators._registrations` is module-level state that accumulates across repeated `exec()` of the same generated bootstrap within one MicroPython process — harmless for plain handlers (a dict, last write wins) but not for `@mcu.loop` (a list `Dispatcher._loops` appends to, so a second `exec()` within the same boot cycle would register the same loop function twice, spawning a duplicate background task). Serial's existing reconnect never hits this — it always does a full hardware reset first, wiping `sys.modules` entirely. Wifi's new accept-loop (Tasks 3-5) deliberately does not reset between connections, so this must be fixed before `run` mode becomes repeatable in Task 4.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_connection.py`:

```python
@requires_micropython
def test_generate_bootstrap_clears_mcu_decorators_registry_before_re_exec():
    # Regression test for a bug the wifi accept-loop (Tasks 3-5) would
    # otherwise introduce: mcu_decorators._registrations is module-level
    # state that accumulates across repeated exec() of the same generated
    # bootstrap within one interpreter process. Runs the exact same
    # generated bootstrap TWICE in one micropython process (a fake reader
    # that immediately raises EOFError lets _tether_main() exit quickly
    # each time, standing in for "the wifi connection just closed") and
    # asserts mcu_decorators._registrations never grows past what ONE
    # exec's own decorator applications produce - it must be exactly 1
    # after both the first AND the second exec, not 1 then 2.
    sliced_source = "@mcu.loop(interval_ms=50)\ndef tick():\n    pass\n"
    bootstrap = generate_bootstrap(sliced_source, "")

    script = f"""
import uasyncio as asyncio
import mcu_decorators

class _EOFReader:
    async def readexactly(self, n):
        raise EOFError("closed")

class _NullWriter:
    def write(self, data):
        pass
    async def drain(self):
        pass

async def run_once():
    ns = {{"_tether_stream_override": (_EOFReader(), _NullWriter())}}
    try:
        exec({bootstrap!r}, ns)
    except EOFError:
        pass

async def main():
    await run_once()
    print("after_first:", len(mcu_decorators._registrations))
    await run_once()
    print("after_second:", len(mcu_decorators._registrations))

asyncio.run(main())
"""
    out = run_micropython(script, timeout=10.0)

    assert "after_first: 1" in out
    assert "after_second: 1" in out
```

Check `tests/test_connection.py`'s existing imports already include `requires_micropython`/`run_micropython` from `mpy_runner` and `generate_bootstrap` from `tether.connection` (both used by existing tests in this file per this session's earlier work) — add them if for some reason they're missing.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_connection.py::test_generate_bootstrap_clears_mcu_decorators_registry_before_re_exec -v`
Expected: FAIL — `AssertionError: assert 'after_second: 1' in '...after_first: 1\nafter_second: 2\n'` (the registry grew to 2 entries on the second exec, confirming the bug is real before the fix).

- [ ] **Step 3: Write minimal implementation**

In `src/tether/connection.py`, find `generate_bootstrap()`'s returned f-string. It currently starts:

```python
from mcu_decorators import mcu, pc, registered_mcu_functions
import dispatch as _tether_dispatch
```

Change to:

```python
from mcu_decorators import mcu, pc, registered_mcu_functions
import mcu_decorators as _tether_mcu_decorators
_tether_mcu_decorators._registrations.clear()
import dispatch as _tether_dispatch
```

Also update `generate_bootstrap()`'s docstring to mention this — find the line ending "...registers every @mcu.export/@mcu.loop function plus the protocol handshake handler, and runs the dispatch loop forever." and add, in the same docstring:

```
Clears mcu_decorators._registrations at the very start, before the
sliced @mcu.export/@mcu.loop/@pc.export definitions (and their decorator
applications) run - without this, repeated exec() of the same generated
script within one interpreter process (the wifi accept-loop does this;
serial's hardware-reset-based reconnect never does) would accumulate
duplicate registrations, harmless for plain handlers (a dict) but not
for @mcu.loop (a list Dispatcher._loops appends to - duplicates spawn
duplicate background tasks).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_connection.py::test_generate_bootstrap_clears_mcu_decorators_registry_before_re_exec -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass — this change is additive/backward-compatible (clearing an already-empty list on a freshly-reset serial connection is a no-op), and the existing end-to-end bootstrap tests (`test_generated_bootstrap_actually_works_end_to_end`, `test_generated_bootstrap_runs_under_real_micropython_with_a_fake_pin`) should be unaffected.

- [ ] **Step 6: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`

- [ ] **Step 7: Commit**

```bash
git add src/tether/connection.py tests/test_connection.py
git commit -m "fix: clear mcu_decorators registry at the top of every generated bootstrap

Module-level _registrations list accumulates across repeated exec() of
the same generated script within one interpreter process - harmless for
plain handlers (a dict) but spawns duplicate background tasks for
@mcu.loop (a list). Serial's reconnect never hits this (always a full
hardware reset first); the upcoming wifi accept-loop (Tasks 3-5)
deliberately doesn't reset between connections, so this needs fixing
first. Verified against the real interpreter: registry stayed at exactly
1 entry across two successive exec()s of the same bootstrap, where
before the fix it grew to 2."
```

---

### Task 3: `boot.py` preamble, auth, accept-loop skeleton, and `status` mode

**Files:**
- Modify: `src/tether/provisioning.py`
- Modify: `tests/mpy_runner.py`
- Test: `tests/test_provisioning.py`

**Interfaces:**
- Consumes: `PROTOCOL_VERSION` (`from tether.connection import PROTOCOL_VERSION`), `DEFAULT_PORT` (`from tether.transports.wifi import DEFAULT_PORT`, already imported).
- Produces: `_BOOT_PY_TEMPLATE` restructured with the accept-loop, preamble/auth check, and `status` mode. `run`/`upload` modes don't exist yet in this task's version — an unrecognized mode gets a clean `{"ok": false, "error": "unknown mode"}` response, which is complete, correct behavior for this task's scope, not a placeholder. Also produces a new test helper: `tests/mpy_runner.py`'s `run_micropython_background(script, *, run_for: float = 3.0) -> None`.

`boot.py`'s new accept-loop runs forever by design (a persistent listener) — the existing `run_micropython()` helper treats a timeout as a test failure (`AssertionError`), which is wrong for a script that's *supposed* to loop forever. This task adds a sibling helper built for that case.

- [ ] **Step 1: Add `run_micropython_background` to `mpy_runner.py`**

This is test infrastructure needed by this task's own test (and by Tasks 4-5), not a separate TDD cycle on its own — add it now, verify it works as part of this task's real test in Step 5.

Add to `tests/mpy_runner.py`, after `run_micropython`:

```python
def run_micropython_background(script: str, *, run_for: float = 3.0) -> None:
    """Start `script` under micropython as a background process, let it
    run for `run_for` seconds, then terminate it - for scripts that loop
    forever by design (e.g. boot.py's accept loop), where a timeout is the
    expected way the test ends, not a failure. Does not return stdout: a
    forcibly-terminated process's buffered output isn't reliably
    available, so tests using this observe behavior through a real side
    channel (e.g. a socket a client thread connects to during `run_for`),
    not through captured process output.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        proc = subprocess.Popen(
            ["micropython", script_path],
            env={
                **os.environ,
                "MICROPYPATH": ":".join([*_DEFAULT_MICROPY_PATHS, str(_TETHER_RUNTIME_SRC)]),
            },
        )
        try:
            proc.wait(timeout=run_for)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
    finally:
        Path(script_path).unlink(missing_ok=True)
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_provisioning.py`:

```python
@requires_micropython
def test_boot_py_status_mode_and_preamble_auth():
    # Verifies the new preamble+accept-loop mechanism end-to-end. run and
    # upload modes don't exist yet at this point in the plan (Tasks 4-5
    # add them) - this test also exercises the "unknown mode" branch to
    # prove the skeleton correctly rejects a mode it doesn't support yet,
    # and the auth-failure branch, before anything is built on top of it.
    import json as pc_json
    import socket
    import struct
    import threading
    import time

    from mpy_runner import run_micropython_background

    test_port = 18767
    boot_py = (
        generate_wifi_boot("irrelevant", "irrelevant")["/boot.py"]
        .decode()
        .replace(str(8765), str(test_port))
    )

    length_prefix = struct.Struct(">I")

    def send_json(sock, obj):
        body = pc_json.dumps(obj).encode()
        sock.sendall(length_prefix.pack(len(body)) + body)

    def read_json(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return pc_json.loads(body)

    results = {}

    def run_client():
        time.sleep(0.5)

        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        send_json(sock, {"mode": "status", "secret": None})
        results["status_ack"] = read_json(sock)
        results["status_payload"] = read_json(sock)
        sock.close()

        sock2 = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        send_json(sock2, {"mode": "bogus", "secret": None})
        results["unknown_mode_ack"] = read_json(sock2)
        sock2.close()

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
    def ifconfig(self):
        return ("10.0.0.5", "255.255.255.0", "10.0.0.1", "10.0.0.1")

class _FakeNetwork:
    STA_IF = 0
    WLAN = _FakeWLAN

_sys.modules["network"] = _FakeNetwork

_files = {{"/tether_wifi.json": '{{"ssid": "irrelevant", "password": "irrelevant"}}'}}

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
def _fake_open(path, mode="r", *a, **kw):
    if path in _files:
        return _FakeFile(_files[path])
    raise OSError(2, "no such file")
import builtins
builtins.open = _fake_open

{boot_py}
"""

    run_micropython_background(script, run_for=3.0)
    client_thread.join(timeout=10.0)

    assert results["status_ack"] == {"ok": True}
    payload = results["status_payload"]
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["tether_app_hash"] is None
    assert isinstance(payload["free_heap"], int)
    assert isinstance(payload["uptime_ms"], int)
    assert payload["ip"] == "10.0.0.5"
    assert results["unknown_mode_ack"] == {"ok": False, "error": "unknown mode"}


@requires_micropython
def test_boot_py_rejects_wrong_secret():
    import json as pc_json
    import socket
    import struct
    import threading
    import time

    from mpy_runner import run_micropython_background

    test_port = 18768
    boot_py = (
        generate_wifi_boot("irrelevant", "irrelevant")["/boot.py"]
        .decode()
        .replace(str(8765), str(test_port))
    )

    length_prefix = struct.Struct(">I")

    def send_json(sock, obj):
        body = pc_json.dumps(obj).encode()
        sock.sendall(length_prefix.pack(len(body)) + body)

    def read_json(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return pc_json.loads(body)

    results = {}

    def run_client():
        time.sleep(0.5)
        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        send_json(sock, {"mode": "status", "secret": "wrong-one"})
        results["ack"] = read_json(sock)
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
    def ifconfig(self):
        return ("10.0.0.5", "255.255.255.0", "10.0.0.1", "10.0.0.1")

class _FakeNetwork:
    STA_IF = 0
    WLAN = _FakeWLAN

_sys.modules["network"] = _FakeNetwork

_files = {{"/tether_wifi.json": '{{"ssid": "irrelevant", "password": "irrelevant", "secret": "the-real-secret"}}'}}

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
def _fake_open(path, mode="r", *a, **kw):
    if path in _files:
        return _FakeFile(_files[path])
    raise OSError(2, "no such file")
import builtins
builtins.open = _fake_open

{boot_py}
"""

    run_micropython_background(script, run_for=3.0)
    client_thread.join(timeout=10.0)

    assert results["ack"] == {"ok": False, "error": "auth failed"}
```

Add `PROTOCOL_VERSION` to `tests/test_provisioning.py`'s existing `from tether.connection import PROTOCOL_VERSION, generate_bootstrap` import line (it likely already imports `PROTOCOL_VERSION` for the existing bridging test — verify and add if missing).

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_provisioning.py -k "preamble_auth or rejects_wrong_secret" -v`
Expected: FAIL — `results["status_ack"]` (and similar) never gets populated / the client's `read_json` call hangs or errors, because the current `_BOOT_PY_TEMPLATE` doesn't speak this protocol at all yet (it still does the old one-shot accept-and-bridge-directly-into-tether_app.py flow, with no preamble). Depending on timing this may show as a `KeyError` on `results["status_ack"]` (client thread never got a response) rather than a clean assertion failure — either way, confirms current behavior doesn't match.

- [ ] **Step 4: Write minimal implementation**

In `src/tether/provisioning.py`, add the import:

```python
from tether.connection import PROTOCOL_VERSION
```

Replace `_BOOT_PY_TEMPLATE` entirely with:

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
                else:
                    _send_json_frame(_conn, {{"ok": False, "error": "unknown mode"}})
            except OSError:
                pass
            finally:
                try:
                    _conn.close()
                except OSError:
                    pass
"""
```

Update the module docstring at the top of `provisioning.py` and the comment above `_BOOT_PY_TEMPLATE` to reflect the new loop/preamble/multi-mode design — replace the existing "Fixed template... If /tether_wifi.json is missing, this does nothing..." comment block with:

```python
# Fixed template - never contains credentials or the shared secret (those
# live in /tether_wifi.json, uploaded separately - see generate_wifi_boot).
# If /tether_wifi.json is missing, this does nothing and falls straight
# through to the idle REPL: a never-provisioned board behaves exactly as
# it did before this feature existed.
#
# Loops indefinitely once wifi is up, accepting connections one at a time
# (never concurrently). Every connection starts with a small preamble
# (JSON, not msgpack - see this module's own note below) selecting a mode
# and presenting the shared secret if one is configured. As of this
# version only "status" is implemented; "run" and "upload" are added by
# later work. An unrecognized mode gets a clean rejection, not a crash.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_provisioning.py -k "preamble_auth or rejects_wrong_secret" -v`
Expected: PASS (both tests).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass. Note: `test_status_script_*` tests (which exercise `STATUS_SCRIPT`, the separate raw-REPL fallback diagnostic — unchanged by this task) and the boot-py-bridging test from the original feature will need checking — the ORIGINAL `test_boot_py_bridges_a_real_socket_into_the_dispatch_loop` test exercised the *old* one-shot, no-preamble boot.py directly with a raw connection and no preamble frame. Since this task changes `boot.py` to require a preamble first, that old test will now fail (the raw connection it makes gets read as a malformed preamble, not a direct dispatch bridge). This is expected and correct — that test is superseded by Task 4's new run-mode test, which drives the *same* mechanism through the new preamble. Update the old test now: delete `test_boot_py_bridges_a_real_socket_into_the_dispatch_loop` from `tests/test_provisioning.py` (its coverage is superseded by Task 4's test, which will restore equivalent coverage plus the preamble).

- [ ] **Step 7: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`

- [ ] **Step 8: Commit**

```bash
git add src/tether/provisioning.py tests/test_provisioning.py tests/mpy_runner.py
git commit -m "feat: boot.py accept-loop, preamble auth, and status mode

Restructures _BOOT_PY_TEMPLATE from a one-shot accept-and-bridge into a
loop that accepts connections indefinitely, each starting with a
length-prefixed JSON preamble (mode + shared secret) checked against
/tether_wifi.json's optional secret field. status mode is fully
implemented (protocol version, tether_app.py hash, free heap, uptime,
IP - no reset, no interruption); run and upload modes land in the next
two tasks. An unrecognized mode gets a clean {ok: false} rejection.

Deleted the old one-shot bridging test (test_boot_py_bridges_a_real_
socket_into_the_dispatch_loop) - it drove boot.py without a preamble,
which the new design requires; superseded by Task 4's run-mode test,
which exercises the same underlying bridge mechanism through the new
preamble.

Added run_micropython_background() to mpy_runner.py: boot.py's loop now
runs forever by design, so tests exercising it need a helper that treats
a timeout as the expected way to end the test, not a failure - observing
behavior through a real client socket during a bounded run window rather
than through captured process output."
```

---

### Task 4: `run` mode wired into the accept-loop, reconnect verified

**Files:**
- Modify: `src/tether/provisioning.py`
- Test: `tests/test_provisioning.py`

**Interfaces:**
- Consumes: `generate_bootstrap` (from `tether.connection`, for building a real `tether_app.py` bundle in the test), `PROTOCOL_VERSION`, `encode_frame` (from `tether.marshalling`, for the test's client to speak the real RPC handshake).
- Produces: `_BOOT_PY_TEMPLATE` gains `run` mode, wired into the accept-loop. No new PC-side or public interface — this is purely the on-device mechanism.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_provisioning.py`:

```python
@requires_micropython
def test_boot_py_run_mode_survives_a_reconnect_without_a_reset():
    # The core proof this task exists for: two SUCCESSIVE run-mode
    # connections against the same boot.py process, neither needing a
    # physical reset - this directly resolves the original wifi design's
    # deferred "no re-listen" limitation. (The mcu_decorators registry
    # duplication risk this reconnect pattern would otherwise hit is
    # separately, directly covered by Task 2's dedicated unit test - this
    # test's job is proving the reconnect itself works end-to-end, not
    # re-proving that specific mechanism.)
    import json as pc_json
    import socket
    import struct
    import threading
    import time

    import msgpack

    from mpy_runner import run_micropython_background
    from tether.marshalling import encode_frame

    test_port = 18769
    boot_py = (
        generate_wifi_boot("irrelevant", "irrelevant")["/boot.py"]
        .decode()
        .replace(str(8765), str(test_port))
    )

    sliced_source = "@mcu.export\ndef add(a: int, b: int) -> int:\n    return a + b"
    tether_app_source = generate_bootstrap(sliced_source, "")

    length_prefix = struct.Struct(">I")

    def send_json(sock, obj):
        body = pc_json.dumps(obj).encode()
        sock.sendall(length_prefix.pack(len(body)) + body)

    def read_json(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return pc_json.loads(body)

    def do_one_run_session():
        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        send_json(sock, {"mode": "run", "secret": None})
        ack = read_json(sock)
        assert ack == {"ok": True}, ack

        sock.sendall(encode_frame(1, {"id": 1, "name": "__tether_handshake__", "args": []}))
        raw_len = sock.recv(4)
        body_len = int.from_bytes(raw_len, "big")
        body = sock.recv(body_len)
        msg_type = body[:1]
        payload = msgpack.unpackb(body[1:], raw=False)
        sock.close()
        return msg_type, payload

    results = {}

    def run_client():
        time.sleep(0.5)
        results["first"] = do_one_run_session()
        # boot.py's loop needs a moment to close the first connection and
        # get back to accept() - a short pause avoids a spurious connection
        # refusal racing that turnaround.
        time.sleep(0.5)
        results["second"] = do_one_run_session()

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
    def ifconfig(self):
        return ("10.0.0.5", "255.255.255.0", "10.0.0.1", "10.0.0.1")

class _FakeNetwork:
    STA_IF = 0
    WLAN = _FakeWLAN

_sys.modules["network"] = _FakeNetwork

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
def _fake_open(path, mode="r", *a, **kw):
    if path in _files:
        return _FakeFile(_files[path])
    raise OSError(2, "no such file")
import builtins
builtins.open = _fake_open

{boot_py}
"""

    run_micropython_background(script, run_for=4.0)
    client_thread.join(timeout=10.0)

    for label in ("first", "second"):
        msg_type, payload = results[label]
        assert msg_type == b"\x02", (label, msg_type)  # MSG_RESULT
        assert payload == {"id": 1, "value": PROTOCOL_VERSION}, (label, payload)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_provisioning.py::test_boot_py_run_mode_survives_a_reconnect_without_a_reset -v`
Expected: FAIL — `KeyError: 'first'` (or similar), since `run` mode isn't wired into the accept-loop yet — the current template replies `{"ok": false, "error": "unknown mode"}` for `mode: "run"`, so `do_one_run_session()`'s `assert ack == {"ok": True}` fails.

- [ ] **Step 3: Write minimal implementation**

In `src/tether/provisioning.py`, add `_handle_run` inside `_BOOT_PY_TEMPLATE`'s f-string, right after `_handle_status`'s definition (before the `_addr = _socket.getaddrinfo(...)` line):

```python
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
```

Change the accept-loop's branch from:

```python
                elif _mode == "status":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_status(_conn)
                else:
                    _send_json_frame(_conn, {{"ok": False, "error": "unknown mode"}})
```

to:

```python
                elif _mode == "status":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_status(_conn)
                elif _mode == "run":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_run(_conn)
                else:
                    _send_json_frame(_conn, {{"ok": False, "error": "unknown mode"}})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_provisioning.py::test_boot_py_run_mode_survives_a_reconnect_without_a_reset -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite, lint, format**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format .`

- [ ] **Step 6: Commit**

```bash
git add src/tether/provisioning.py tests/test_provisioning.py
git commit -m "feat: run mode wired into boot.py's accept-loop, reconnect verified

Moves the existing exec()-based dispatch bridge (unchanged in mechanism)
into the new accept-loop's run branch. Verified against the real
interpreter: two successive run-mode connections against the same
boot.py process both complete a full protocol-version handshake
correctly, with no physical reset between them - resolves the original
wifi design's deferred 're-listening after the one accepted connection
drops' limitation."
```

---

### Task 5: `upload` mode

**Files:**
- Modify: `src/tether/provisioning.py`
- Test: `tests/test_provisioning.py`

**Interfaces:**
- Consumes: nothing new from earlier tasks beyond what Task 3/4 already established.
- Produces: `_BOOT_PY_TEMPLATE` gains `upload` mode. Wire shape (on-device side, mirrored by Task 6's PC-side implementation): after the preamble ack, the client sends one JSON manifest frame `{"dirs": [...], "files": [{"path": ..., "size": ...}, ...]}`, then for each file in `files` order, raw byte frames whose total length equals that file's declared `size`; device replies `{"ok": true}` or `{"ok": false, "error": "..."}` once all files are written, then closes.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_provisioning.py`:

```python
@requires_micropython
def test_boot_py_upload_mode_writes_files_verified_via_status():
    # Verifies upload mode via the same channel a real client would use to
    # confirm it worked: upload a file + its hash, then reconnect in
    # status mode and check the reported hash matches - proving the round
    # trip through the actual on-device write path, not just "the ok
    # response came back".
    import json as pc_json
    import socket
    import struct
    import threading
    import time

    from mpy_runner import run_micropython_background

    test_port = 18770
    boot_py = (
        generate_wifi_boot("irrelevant", "irrelevant")["/boot.py"]
        .decode()
        .replace(str(8765), str(test_port))
    )

    length_prefix = struct.Struct(">I")

    def send_json(sock, obj):
        body = pc_json.dumps(obj).encode()
        sock.sendall(length_prefix.pack(len(body)) + body)

    def read_json(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return pc_json.loads(body)

    def send_bytes(sock, data):
        sock.sendall(length_prefix.pack(len(data)) + data)

    app_content = b"print('this is the uploaded tether_app.py')\n"
    hash_content = b"deadbeef-fake-hash"

    results = {}

    def run_client():
        time.sleep(0.5)

        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        send_json(sock, {"mode": "upload", "secret": None})
        results["upload_ack"] = read_json(sock)

        send_json(
            sock,
            {
                "dirs": [],
                "files": [
                    {"path": "/tether_app.py", "size": len(app_content)},
                    {"path": "/.tether_hash", "size": len(hash_content)},
                ],
            },
        )
        send_bytes(sock, app_content)
        send_bytes(sock, hash_content)
        results["upload_result"] = read_json(sock)
        sock.close()

        time.sleep(0.5)
        sock2 = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        send_json(sock2, {"mode": "status", "secret": None})
        results["status_ack"] = read_json(sock2)
        results["status_payload"] = read_json(sock2)
        sock2.close()

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
    def ifconfig(self):
        return ("10.0.0.5", "255.255.255.0", "10.0.0.1", "10.0.0.1")

class _FakeNetwork:
    STA_IF = 0
    WLAN = _FakeWLAN

_sys.modules["network"] = _FakeNetwork

_files = {{"/tether_wifi.json": '{{"ssid": "irrelevant", "password": "irrelevant"}}'}}

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

_real_open = open
def _fake_open(path, mode="r", *a, **kw):
    if mode == "wb":
        return _FakeWriteFile(path)
    if path in _files:
        content = _files[path]
        if isinstance(content, bytes):
            content = content.decode()
        return _FakeReadFile(content)
    raise OSError(2, "no such file")
import builtins
builtins.open = _fake_open

{boot_py}
"""

    run_micropython_background(script, run_for=4.0)
    client_thread.join(timeout=10.0)

    assert results["upload_ack"] == {"ok": True}
    assert results["upload_result"] == {"ok": True}, results["upload_result"]
    assert results["status_ack"] == {"ok": True}
    assert results["status_payload"]["tether_app_hash"] == hash_content.decode()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_provisioning.py::test_boot_py_upload_mode_writes_files_verified_via_status -v`
Expected: FAIL — `assert {'ok': False, 'error': 'unknown mode'} == {'ok': True}` on `results["upload_ack"]`, since `upload` mode isn't wired into the accept-loop yet.

- [ ] **Step 3: Write minimal implementation**

In `src/tether/provisioning.py`, add `_handle_upload` inside `_BOOT_PY_TEMPLATE`'s f-string, right after `_handle_run`'s definition:

```python
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
```

Change the accept-loop's branch from:

```python
                elif _mode == "run":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_run(_conn)
                else:
                    _send_json_frame(_conn, {{"ok": False, "error": "unknown mode"}})
```

to:

```python
                elif _mode == "run":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_run(_conn)
                elif _mode == "upload":
                    _send_json_frame(_conn, {{"ok": True}})
                    _handle_upload(_conn)
                else:
                    _send_json_frame(_conn, {{"ok": False, "error": "unknown mode"}})
```

Note: directories are created in whatever order the manifest lists them — unlike `write_files` (serial), which depth-sorts internally, this task deliberately leaves depth-ordering as the CLIENT's responsibility (Task 6 must send `dirs` pre-sorted by depth, exactly matching the existing convention `connection.py`'s `_upload_if_needed` already uses for serial). Not re-litigating that division of responsibility here — the on-device side just creates whatever it's told, in order, same as `write_files`'s own mkdir loop does.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_provisioning.py::test_boot_py_upload_mode_writes_files_verified_via_status -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite, lint, format**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format .`

- [ ] **Step 6: Commit**

```bash
git add src/tether/provisioning.py tests/test_provisioning.py
git commit -m "feat: upload mode - boot.py can now receive and write a full bundle

Manifest (dirs + per-file path/size) as one JSON control frame, then
each file's content as one or more raw length-prefixed byte frames (no
base64 - wifi's frames carry binary directly, unlike serial's
raw-REPL-text-console constraint). Streams each file straight to flash
rather than buffering the whole bundle in memory. Verified against the
real interpreter via the same channel a real client would use to check
it worked: upload a file + its hash, reconnect in status mode, confirm
the reported hash matches what was just written."
```

---

### Task 6: PC-side `_connect_wifi` — slice, hash-check via status, upload-if-needed, run

**Files:**
- Modify: `src/tether/connection.py`
- Test: `tests/test_connection.py`

**Interfaces:**
- Consumes: `send_preamble`, `send_json_frame`, `read_json_frame`, `send_bytes_frame` (from `tether.transports.wifi`, Task 1); `slice_mcu_bound`, `generate_pc_stubs` (from `tether.slicer`, already used by `connect()`'s serial path); `_hash_bundle` (already in `connection.py`).
- Produces: `connect(address, *, timeout=10.0, secret=None)` — new `secret` kwarg on the public entrypoint. `_connect_wifi`'s signature changes from `_connect_wifi(rest, export_specs, pc_handlers, *, timeout)` to `_connect_wifi(rest, bootstrap, export_specs, exported_names, pc_handlers, *, timeout, secret=None)`, matching `_connect_serial`'s shape. `connect()` itself now slices for the `wifi` scheme too, not just `serial`.

- [ ] **Step 1: Write the failing test**

This test needs a fake on-device server speaking the full protocol (status → upload if needed → run + handshake) over a real socket, since there's no PC-side-only way to fake `_connect_wifi`'s new multi-step flow otherwise. Add to `tests/test_connection.py`:

```python
def test_connect_wifi_uploads_when_hash_differs_then_runs():
    import json
    import socket
    import struct
    import threading

    length_prefix = struct.Struct(">I")

    def read_json(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return json.loads(body)

    def send_json(sock, obj):
        body = json.dumps(obj).encode()
        sock.sendall(length_prefix.pack(len(body)) + body)

    def recv_exact(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("closed")
            buf += chunk
        return buf

    def read_bytes_frame(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        return recv_exact(sock, length)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(4)
    port = server_sock.getsockname()[1]

    received_files: dict[str, bytes] = {}

    def fake_device():
        # 1. status connection - reports no hash, so the client must upload.
        conn, _ = server_sock.accept()
        preamble = read_json(conn)
        assert preamble["mode"] == "status"
        send_json(conn, {"ok": True})
        send_json(
            conn,
            {
                "protocol_version": 1,
                "tether_app_hash": None,
                "free_heap": 100000,
                "uptime_ms": 500,
                "ip": "127.0.0.1",
            },
        )
        conn.close()

        # 2. upload connection - receive the manifest, then each file's bytes.
        conn2, _ = server_sock.accept()
        preamble2 = read_json(conn2)
        assert preamble2["mode"] == "upload"
        send_json(conn2, {"ok": True})
        manifest = read_json(conn2)
        for file_meta in manifest["files"]:
            remaining = file_meta["size"]
            content = b""
            while remaining > 0:
                chunk = read_bytes_frame(conn2)
                content += chunk
                remaining -= len(chunk)
            received_files[file_meta["path"]] = content
        send_json(conn2, {"ok": True})
        conn2.close()

        # 3. run connection - ack the preamble, then answer the handshake.
        conn3, _ = server_sock.accept()
        preamble3 = read_json(conn3)
        assert preamble3["mode"] == "run"
        send_json(conn3, {"ok": True})

        import msgpack

        header = conn3.recv(4)
        body_len = int.from_bytes(header, "big")
        body = conn3.recv(body_len)
        request = msgpack.unpackb(body[1:], raw=False)
        assert request["name"] == "__tether_handshake__"

        from tether.marshalling import encode_frame

        conn3.sendall(encode_frame(2, {"id": request["id"], "value": PROTOCOL_VERSION}))
        # Keep the connection open briefly so BoardHandle construction
        # completes before the test tears down - the reader thread inside
        # Dispatcher.start() needs a live socket to not immediately see EOF.
        conn3.settimeout(2.0)
        try:
            conn3.recv(1)
        except OSError:
            pass
        conn3.close()

    server_thread = threading.Thread(target=fake_device, daemon=True)
    server_thread.start()

    source = (
        "from tether import mcu, pc\n\n"
        "@mcu.export\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
    )
    export_specs = {"add": object()}  # exact value unused by this path; presence is what matters
    pc_handlers: dict = {}

    sliced = slice_mcu_bound(source)
    bootstrap = generate_bootstrap(sliced.source, "")

    board = _connect_wifi(
        f"127.0.0.1:{port}",
        bootstrap,
        export_specs,
        sliced.exported_names,
        pc_handlers,
        timeout=5.0,
    )

    server_thread.join(timeout=5.0)
    # Full bundle, not just tether_app.py - the design spec explicitly
    # requires wifi upload to push the whole tether_runtime library too
    # (dispatch.py, mcu_decorators.py, vendored umsgpack), same as serial's
    # _upload_if_needed already does, so "wifi never needs serial again
    # after the first provision" is actually true.
    assert received_files["/tether_app.py"] == bootstrap.encode()
    assert "/.tether_hash" in received_files
    assert "/dispatch.py" in received_files
    assert "/mcu_decorators.py" in received_files
    assert "/umsgpack/__init__.py" in received_files
    assert received_dirs == ["/umsgpack"]
    assert board is not None
```

Add the needed imports at the top of `tests/test_connection.py` if not already present: `from tether.connection import _connect_wifi, generate_bootstrap` and `from tether.slicer import slice_mcu_bound` (check the file's existing imports first — `generate_bootstrap` is very likely already imported given Task 2's test uses it; `_connect_wifi` and `slice_mcu_bound` are new for this test).

Add `received_dirs: list = []` next to `received_files: dict[str, bytes] = {}`'s declaration (before `fake_device` is defined), and inside `fake_device`'s upload-handling section, right after `manifest = read_json(conn2)`, add `received_dirs.extend(manifest["dirs"])` — this captures what the client sent so the dirs assertion above has something to check.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_connection.py::test_connect_wifi_uploads_when_hash_differs_then_runs -v`
Expected: FAIL — `TypeError: _connect_wifi() takes 3 positional arguments but 5 were given` (current signature is `_connect_wifi(rest, export_specs, pc_handlers, *, timeout)`).

- [ ] **Step 3: Write minimal implementation**

First, extract the runtime-bundle-gathering logic `_upload_if_needed` already has into a shared helper, so wifi's `_upload` (which needs the exact same file set) doesn't duplicate it. In `src/tether/connection.py`, find `_upload_if_needed`:

```python
def _upload_if_needed(
    ser: Any, serial_transport: Any, bootstrap: str, bundle_hash: str, *, timeout: float
) -> None:
    existing_hash = serial_transport.read_file(ser, "/.tether_hash", timeout=timeout)
    if existing_hash is not None and existing_hash.decode() == bundle_hash:
        return

    runtime_dir = Path(__file__).resolve().parents[1] / "tether_runtime"
    # Derived from disk rather than hand-listed - a new file landing in
    # tether_runtime/ (e.g. a future umsgpack helper) is picked up
    # automatically instead of silently missing from the upload until an
    # on-device ImportError surfaces it. __init__.py is the one PC-side-only
    # marker file (see its own docstring) and is excluded.
    runtime_paths = [
        p for p in sorted(runtime_dir.rglob("*.py")) if p != runtime_dir / "__init__.py"
    ]
    runtime_files = {
        f"/{p.relative_to(runtime_dir).as_posix()}": p.read_bytes() for p in runtime_paths
    }
    runtime_dirs = tuple(
        sorted(
            {
                f"/{p.parent.relative_to(runtime_dir).as_posix()}"
                for p in runtime_paths
                if p.parent != runtime_dir
            }
        )
    )
    serial_transport.write_files(
        ser,
        {
            **runtime_files,
            "/tether_app.py": bootstrap.encode(),
            "/.tether_hash": bundle_hash.encode(),
        },
        dirs=runtime_dirs,
        timeout=timeout,
    )
```

Replace it with a shared helper plus a thinner `_upload_if_needed` that calls it:

```python
def _gather_runtime_bundle(bootstrap: str, bundle_hash: str) -> tuple[dict[str, bytes], tuple[str, ...]]:
    """Every file a fresh board needs: the whole tether_runtime library
    (dispatch.py, mcu_decorators.py, vendored umsgpack) plus this
    connection's own sliced app and hash sentinel. Shared between serial's
    _upload_if_needed and wifi's upload mode (_connect_wifi) - both need
    the exact same file set, so this is the one place that gathers it.

    Derived from disk rather than hand-listed - a new file landing in
    tether_runtime/ (e.g. a future umsgpack helper) is picked up
    automatically instead of silently missing from the upload until an
    on-device ImportError surfaces it. __init__.py is the one PC-side-only
    marker file (see its own docstring) and is excluded.
    """
    runtime_dir = Path(__file__).resolve().parents[1] / "tether_runtime"
    runtime_paths = [
        p for p in sorted(runtime_dir.rglob("*.py")) if p != runtime_dir / "__init__.py"
    ]
    runtime_files = {
        f"/{p.relative_to(runtime_dir).as_posix()}": p.read_bytes() for p in runtime_paths
    }
    runtime_dirs = tuple(
        sorted(
            {
                f"/{p.parent.relative_to(runtime_dir).as_posix()}"
                for p in runtime_paths
                if p.parent != runtime_dir
            }
        )
    )
    files = {
        **runtime_files,
        "/tether_app.py": bootstrap.encode(),
        "/.tether_hash": bundle_hash.encode(),
    }
    return files, runtime_dirs


def _upload_if_needed(
    ser: Any, serial_transport: Any, bootstrap: str, bundle_hash: str, *, timeout: float
) -> None:
    existing_hash = serial_transport.read_file(ser, "/.tether_hash", timeout=timeout)
    if existing_hash is not None and existing_hash.decode() == bundle_hash:
        return

    files, runtime_dirs = _gather_runtime_bundle(bootstrap, bundle_hash)
    serial_transport.write_files(ser, files, dirs=runtime_dirs, timeout=timeout)
```

This is a pure refactor of already-tested, already-hardware-verified code — `_upload_if_needed`'s own observable behavior (what it writes, when) is unchanged, only *where* the file-gathering logic lives. Run `.venv/bin/pytest tests/ -q` right after this refactor, before writing anything wifi-specific, to confirm nothing broke — every existing serial upload test should still pass unmodified.

Now add `_connect_wifi` (replacing the current 3-argument version entirely):

```python
def _connect_wifi(
    rest: str,
    bootstrap: str,
    export_specs: dict[str, Any],
    exported_names: frozenset[str],
    pc_handlers: dict[str, Callable[..., Any]],
    *,
    timeout: float,
    secret: str | None = None,
) -> BoardHandle:
    """Connect over TCP to a boot.py-managed wifi listener. Unlike the
    original design, this now mirrors _connect_serial's shape: slice,
    hash-check, upload-if-needed, then run - just with the hash-check
    piggybacked on a `status`-mode query instead of a direct file read,
    and upload/run as two separate connections instead of one persistent
    raw-REPL session (see the design spec for why).
    """
    import os

    from tether.transports import wifi as wifi_transport

    host, _, port_str = rest.partition(":")
    port = int(port_str) if port_str else wifi_transport.DEFAULT_PORT
    resolved_secret = secret if secret is not None else os.environ.get("TETHER_WIFI_SECRET")

    unsliced = export_specs.keys() - exported_names
    if unsliced:
        raise RuntimeError(
            f"{sorted(unsliced)} are decorated with @mcu.export/@mcu.loop but weren't "
            "found by static analysis of the source - decorated functions must be plain "
            "top-level `def`/`async def` statements, not conditionally defined "
            "(DESIGN.md § Standing design constraint)"
        )

    bundle_hash = _hash_bundle(bootstrap)
    # See _connect_mock's matching comment.
    board: BoardHandle | None = None

    def _query_status(sock: Any) -> dict[str, Any]:
        wifi_transport.send_preamble(sock, "status", resolved_secret)
        return wifi_transport.read_json_frame(sock)

    def _upload(sock: Any) -> None:
        wifi_transport.send_preamble(sock, "upload", resolved_secret)
        # Full bundle (tether_runtime library + app + hash), same set and
        # same dirs-sorted-by-depth-by-the-client convention
        # _upload_if_needed uses for serial - see _gather_runtime_bundle.
        files, dirs = _gather_runtime_bundle(bootstrap, bundle_hash)
        manifest = {
            "dirs": list(dirs),
            "files": [{"path": path, "size": len(content)} for path, content in files.items()],
        }
        wifi_transport.send_json_frame(sock, manifest)
        for content in files.values():
            wifi_transport.send_bytes_frame(sock, content)
        result = wifi_transport.read_json_frame(sock)
        if not result.get("ok", False):
            raise RuntimeError(f"wifi upload failed: {result.get('error')}")

    def dial() -> Dispatcher:
        import socket as socket_module

        status_sock = socket_module.create_connection((host, port), timeout=timeout)
        try:
            status = _query_status(status_sock)
        finally:
            status_sock.close()

        if status.get("tether_app_hash") != bundle_hash:
            upload_sock = socket_module.create_connection((host, port), timeout=timeout)
            try:
                _upload(upload_sock)
            finally:
                upload_sock.close()

        # wifi_transport.connect() (not a bare socket_module.create_connection,
        # unlike status/upload above) - reuses its existing TCP_NODELAY +
        # blocking-timeout-handoff setup, already correct and
        # hardware-verified for the long-lived RPC stream this becomes.
        # The short-lived status/upload connections above don't need that
        # treatment, so a plain socket is simplest for those.
        stream = wifi_transport.connect(host, port, timeout=timeout)
        try:
            wifi_transport.send_preamble(stream._sock, "run", resolved_secret)
            return _start_and_handshake(
                stream,
                timeout=timeout,
                mismatch_hint="update tether or the on-device runtime",
                pc_handlers=pc_handlers,
                board=board,
            )
        except BaseException:
            stream.close()
            raise

    board = BoardHandle(dial(), export_specs, dial=dial)
    return board
```

Now update `connect()` (the top-level public function) to slice for the `wifi` scheme too. Find:

```python
    elif scheme == "wifi":
        board = _connect_wifi(rest, export_specs, pc_handlers, timeout=timeout)
```

Replace with:

```python
    elif scheme == "wifi":
        sliced = slice_mcu_bound(source, base_dir=base_dir)
        stubs = generate_pc_stubs(source)
        bootstrap = generate_bootstrap(sliced.source, stubs.source)
        board = _connect_wifi(
            rest,
            bootstrap,
            export_specs,
            sliced.exported_names,
            pc_handlers,
            timeout=timeout,
            secret=secret,
        )
```

Update `connect()`'s own signature and docstring:

```python
def connect(address: str, *, timeout: float = 10.0, secret: str | None = None) -> BoardHandle:
    """Slice -> stub -> bundle -> hash-check -> upload -> handshake -> ready.

    `address` scheme selects transport:
      "serial:auto" | "serial:/dev/ttyUSB0" | "wifi:<ip>" | "ble:<addr>" | "mock://"

    `secret` (wifi only): the shared secret configured during
    `tether provision-wifi`. Falls back to the TETHER_WIFI_SECRET
    environment variable if omitted. Required when the device has one
    configured (raises WifiAuthError if missing/wrong); ignored otherwise.

    Auto-detects the calling file's source (must be called from a real .py
    file) - matches the "single file" pitch: no need to pass your own
    source to connect() explicitly.
    """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_connection.py::test_connect_wifi_uploads_when_hash_differs_then_runs -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass. Watch specifically for: any existing wifi-related test in `test_connection.py` that called `_connect_wifi` with the OLD 3-positional-argument signature — update any such call site to the new signature (bootstrap/exported_names now required); and every existing serial-upload test that exercises `_upload_if_needed` (real-hardware-verified code from the original feature) — confirm the `_gather_runtime_bundle` extraction in Step 3 didn't change its observable behavior (same files, same dirs, same hash-check-skip logic).

- [ ] **Step 6: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`

- [ ] **Step 7: Commit**

```bash
git add src/tether/connection.py tests/test_connection.py
git commit -m "feat: mcu.connect(\"wifi:<ip>\") now uploads code, matching serial

_connect_wifi gains the same slice -> hash-check -> upload-if-needed ->
run flow _connect_serial already has, closing the 'wifi silently runs
stale code' gotcha for good rather than just documenting it. Hash-check
is piggybacked on a status-mode query instead of a separate step -
status already reports the on-device hash. New secret kwarg on the
public connect()/mcu.connect(), falling back to TETHER_WIFI_SECRET.

Extracted _gather_runtime_bundle() out of _upload_if_needed (pure
refactor, no behavior change) so wifi's upload mode pushes the exact
same full bundle serial does - the whole tether_runtime library, not
just tether_app.py - instead of duplicating the file-gathering logic.

Verified against a hand-rolled fake device speaking the full
status->upload->run protocol over a real socket, asserting the full
runtime file set (dispatch.py, mcu_decorators.py, vendored umsgpack)
and its directory structure actually get sent, not just the app file."
```

---

### Task 7: Secret generation + `provision-wifi`/`status` CLI changes

**Files:**
- Modify: `src/tether/provisioning.py`
- Modify: `src/tether/cli.py`
- Test: `tests/test_provisioning.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `send_preamble`, `read_json_frame` (from `tether.transports.wifi`, Task 1).
- Produces: `generate_wifi_boot(ssid: str, password: str, *, secret: str | None = None) -> dict[str, bytes]` — `secret` now threads into the on-device config. `provision-wifi` CLI command gains `--danger-unauthenticated`; prints the generated secret. `status` CLI command becomes two-tier (wifi socket first, raw-REPL fallback).

- [ ] **Step 1: Write the failing test for secret generation**

Add to `tests/test_provisioning.py`:

```python
def test_generate_wifi_boot_includes_a_secret_by_default():
    files = generate_wifi_boot("MyNetwork", "hunter2")

    config = json.loads(files["/tether_wifi.json"])
    assert "secret" in config
    assert isinstance(config["secret"], str)
    assert len(config["secret"]) >= 16  # not a trivially guessable short token


def test_generate_wifi_boot_omits_secret_when_explicitly_none():
    files = generate_wifi_boot("MyNetwork", "hunter2", secret=None, danger_unauthenticated=True)

    config = json.loads(files["/tether_wifi.json"])
    assert "secret" not in config


def test_generate_wifi_boot_two_calls_produce_different_secrets():
    files_a = generate_wifi_boot("MyNetwork", "hunter2")
    files_b = generate_wifi_boot("MyNetwork", "hunter2")

    config_a = json.loads(files_a["/tether_wifi.json"])
    config_b = json.loads(files_b["/tether_wifi.json"])
    assert config_a["secret"] != config_b["secret"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_provisioning.py -k "generate_wifi_boot_includes_a_secret or omits_secret or two_calls_produce" -v`
Expected: FAIL — `TypeError: generate_wifi_boot() got an unexpected keyword argument 'danger_unauthenticated'` (and the first test fails with `KeyError`/`AssertionError` since no secret is generated today).

- [ ] **Step 3: Write minimal implementation**

In `src/tether/provisioning.py`, add the import:

```python
import secrets
```

Replace `generate_wifi_boot`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_provisioning.py -k "generate_wifi_boot_includes_a_secret or omits_secret or two_calls_produce" -v`
Expected: PASS.

- [ ] **Step 5: Run the previously-existing `generate_wifi_boot` tests to confirm no regression**

Run: `.venv/bin/pytest tests/test_provisioning.py -k generate_wifi_boot -v`
Expected: PASS — `test_generate_wifi_boot_embeds_credentials_only_in_the_config_file` and `test_generate_wifi_boot_checks_for_wifi_config_before_connecting` should be unaffected (they check `ssid`/`password` presence/absence in `boot.py`, not `secret`).

- [ ] **Step 6: Write the failing test for `provision-wifi`'s CLI changes**

Add to `tests/test_cli.py`:

```python
def test_provision_wifi_prints_the_generated_secret(monkeypatch):
    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)

    written = {}

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
    config = json.loads(written["/tether_wifi.json"])
    assert config["secret"] in result.output


def test_provision_wifi_danger_unauthenticated_omits_secret_and_warns(monkeypatch):
    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)

    written = {}

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
            "--danger-unauthenticated",
        ],
    )

    assert result.exit_code == 0, result.output
    config = json.loads(written["/tether_wifi.json"])
    assert "secret" not in config
    assert "unauthenticated" in result.output.lower()
```

Add `import json` to the top of `tests/test_cli.py` if not already present (it likely is, given the existing `status` command tests use it).

- [ ] **Step 7: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -k "prints_the_generated_secret or danger_unauthenticated" -v`
Expected: FAIL — `click.exceptions.NoSuchOption: No such option: --danger-unauthenticated` (and the first test's secret assertion fails since nothing is printed yet).

- [ ] **Step 8: Write minimal implementation**

In `src/tether/cli.py`, replace `provision_wifi_command`:

```python
@main.command("provision-wifi")
@click.option("--port", default=None, help="Serial port (auto-detected if omitted).")
@click.option("--ssid", required=True, help="WiFi network name.")
@click.option("--password", default=None, help="WiFi password (prompted if omitted).")
@click.option(
    "--danger-unauthenticated",
    is_flag=True,
    default=False,
    help="Skip generating a shared secret - the wifi listener accepts any connection.",
)
def provision_wifi_command(
    port: str | None, ssid: str, password: str | None, danger_unauthenticated: bool
) -> None:
    """Upload a boot.py that auto-connects to WiFi and makes the board
    reachable over tether's wifi transport on every boot.

    Note: this uploads boot.py + wifi credentials only - the program that
    actually runs when a wifi client connects (tether_app.py) is uploaded
    separately, over wifi itself, the first time `mcu.connect("wifi:<ip>")`
    is called (or over a normal serial `mcu.connect(...)` session, which
    still works too).
    """
    import beaupy

    from tether import provisioning
    from tether.transports import serial as serial_transport

    resolved_port = _resolve_port(port)
    if password is None:
        password = beaupy.prompt("WiFi password:", secure=True)

    if danger_unauthenticated:
        click.echo(
            "WARNING: --danger-unauthenticated - this board's wifi listener will "
            "accept connections from anyone on the network, no secret required."
        )

    files = provisioning.generate_wifi_boot(
        ssid, password, danger_unauthenticated=danger_unauthenticated
    )

    with _open_board(resolved_port) as ser:
        serial_transport.reset_board(ser)
        serial_transport.write_files(ser, files)
        serial_transport.reset_board(ser)

    click.echo(f"Provisioned {resolved_port} for wifi network {ssid!r}. Board is restarting.")
    if not danger_unauthenticated:
        import json

        config = json.loads(files["/tether_wifi.json"])
        click.echo(f"Shared secret (save this - needed to connect): {config['secret']}")
    click.echo("Run `tether status` in a few seconds to check connectivity.")
```

- [ ] **Step 9: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -k "prints_the_generated_secret or danger_unauthenticated" -v`
Expected: PASS.

- [ ] **Step 10: Write the failing test for `status`'s two-tier fallback**

Add to `tests/test_cli.py`:

```python
def test_status_command_tries_wifi_socket_first(monkeypatch):
    # When the wifi socket answers, status must use it directly - no
    # reset_board() call at all (that's the whole point of this feature).
    calls = []

    class _FakeSocket:
        def __init__(self, *a, **kw):
            pass

        def close(self):
            pass

    def fake_send_preamble(sock, mode, secret):
        calls.append(("preamble", mode))

    def fake_read_json_frame(sock):
        calls.append(("status_payload",))
        return {
            "protocol_version": 1,
            "tether_app_hash": "abc123",
            "free_heap": 50000,
            "uptime_ms": 1234,
            "ip": "192.168.1.50",
        }

    monkeypatch.setattr("socket.create_connection", lambda *a, **kw: _FakeSocket())
    monkeypatch.setattr("tether.transports.wifi.send_preamble", fake_send_preamble)
    monkeypatch.setattr("tether.transports.wifi.read_json_frame", fake_read_json_frame)
    monkeypatch.setattr(
        "tether.transports.serial.reset_board",
        lambda ser: calls.append(("reset_board",)),
    )

    result = CliRunner().invoke(main, ["status", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert "192.168.1.50" in result.output
    assert ("reset_board",) not in calls


def test_status_command_falls_back_to_raw_repl_when_wifi_socket_unreachable(monkeypatch):
    import json

    def raise_connection_refused(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr("socket.create_connection", raise_connection_refused)

    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)

    status = {"provisioned": True, "connected": False, "ip": None}
    monkeypatch.setattr(
        "tether.transports.serial.run_python",
        lambda ser, code, timeout=10.0: (json.dumps(status).encode(), b""),
    )

    result = CliRunner().invoke(main, ["status", "--port", "/dev/ttyUSB0", "--ip", "10.0.0.5"])

    assert result.exit_code == 0, result.output
    assert "not currently connected" in result.output.lower()
```

- [ ] **Step 11: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -k "tries_wifi_socket_first or falls_back_to_raw_repl" -v`
Expected: FAIL — `click.exceptions.NoSuchOption: No such option: --ip` (the current `status` command doesn't know a device's IP at all, and always goes straight to `reset_board()` + raw-REPL) — confirms the current one-tier behavior.

- [ ] **Step 12: Write minimal implementation**

In `src/tether/cli.py`, replace `status_command`:

```python
@main.command("status")
@click.option("--port", default=None, help="Serial port (auto-detected if omitted).")
@click.option(
    "--ip",
    default=None,
    help="Device IP (if known) - tried first, over wifi, before falling back to serial.",
)
@click.option("--secret", default=None, help="Shared secret, if the device requires one.")
def status_command(port: str | None, ip: str | None, secret: str | None) -> None:
    """Check whether a board is wifi-provisioned and currently connected.

    Tries a direct, non-destructive wifi query first if --ip is given (no
    reset, no interruption). Falls back to the existing raw-REPL
    diagnostic (which does reset the board) only if the wifi socket
    itself is unreachable - meaning wifi never came up in the first
    place, not that the board is merely busy.
    """
    import json
    import os
    import socket

    from tether import provisioning
    from tether.transports import serial as serial_transport
    from tether.transports import wifi as wifi_transport

    if ip:
        resolved_secret = secret if secret is not None else os.environ.get("TETHER_WIFI_SECRET")
        try:
            sock = socket.create_connection((ip, wifi_transport.DEFAULT_PORT), timeout=3.0)
        except OSError:
            sock = None
        if sock is not None:
            try:
                wifi_transport.send_preamble(sock, "status", resolved_secret)
                payload = wifi_transport.read_json_frame(sock)
            finally:
                sock.close()
            click.echo(f"Provisioned and connected. IP: {payload['ip']}")
            return

    resolved_port = _resolve_port(port)
    with _open_board(resolved_port) as ser:
        serial_transport.reset_board(ser)
        # timeout=10.0 must stay comfortably above STATUS_SCRIPT's own
        # internal 8s wifi-connect poll (provisioning.py) - it also has
        # to cover the raw-REPL round trip on top of that wait.
        stdout, stderr = serial_transport.run_python(ser, provisioning.STATUS_SCRIPT, timeout=10.0)

    if stderr:
        raise click.ClickException(f"status check failed: {stderr.decode(errors='replace')}")

    try:
        info = json.loads(stdout.decode(errors="replace").strip())
        provisioned, connected, ip_from_serial = info["provisioned"], info["connected"], info["ip"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise click.ClickException(f"could not parse status response: {exc}") from None

    if not provisioned:
        click.echo("Not provisioned for wifi. Run `tether provision-wifi` first.")
    elif connected:
        click.echo(f"Provisioned and connected. IP: {ip_from_serial}")
    else:
        click.echo("Provisioned but not currently connected to wifi.")
```

Note the `--ip`-not-given case (the common one today, since nothing currently tells the user their board's IP before a first successful connection) falls straight through to the existing raw-REPL path unchanged — this CLI change is additive, not a breaking change to today's `tether status --port ...` usage.

- [ ] **Step 13: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -k "tries_wifi_socket_first or falls_back_to_raw_repl" -v`
Expected: PASS.

- [ ] **Step 14: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass — check the two pre-existing `status_command` tests (`test_status_command_reports_connected_with_ip`, `test_status_command_reports_not_provisioned`) still pass unmodified, since they don't pass `--ip` and so exercise the unchanged raw-REPL-only path.

- [ ] **Step 15: Lint and format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`

- [ ] **Step 16: Commit**

```bash
git add src/tether/provisioning.py src/tether/cli.py tests/test_provisioning.py tests/test_cli.py
git commit -m "feat: shared secret generation + provision-wifi/status CLI changes

generate_wifi_boot() generates a fresh random secret (secrets.token_hex)
by default, storing it in /tether_wifi.json alongside credentials -
every provision-wifi run naturally rotates it. --danger-unauthenticated
skips secret generation entirely and prints a warning (not a blocking
prompt, so scripted provisioning still works). provision-wifi prints the
secret once, on success.

status gains an optional --ip: when given, tries a direct wifi-socket
status query first (no reset, no interruption) and only falls back to
the existing raw-REPL diagnostic if that connection itself fails -
meaning wifi never came up, not that the board's merely busy. Omitting
--ip (today's only usage) is unaffected - falls straight to the existing
path, unchanged."
```

---

### Task 8: Real-hardware verification

Not TDD-shaped — this is the final verification pass against the physical ESP32, following this project's established discipline (every prior wifi-related chunk closed with real hardware, not just interpreter tests).

- [ ] **Step 1: Confirm the board is reachable**

Run: `ls /dev/cu.* | grep -i usbserial` (or `uv run tether devices`) to find the current port. Port names can shift between sessions.

- [ ] **Step 2: Provision with auth (default)**

Run: `uv run tether provision-wifi --port <PORT> --ssid "<REAL_SSID>" --password "<REAL_PASSWORD>"`
Expected: exits 0, prints the secret. Note the secret and the network.

- [ ] **Step 3: Non-destructive status check**

Wait a few seconds for wifi to associate, then run: `uv run tether status --ip <IP-you-expect-or-find-via-router>` (if the IP isn't known yet, fall back once to `uv run tether status --port <PORT>` to learn it via the raw-REPL path, then use `--ip` from here on).
Expected: exits 0, reports "Provisioned and connected. IP: ...", **no visible board reset** (compare against the old behavior: no multi-second pause, and a `tether status` run immediately afterward should show the SAME session still alive, not a freshly-rebooted one — this is the core promise of this whole feature).

- [ ] **Step 4: Real `mcu.connect("wifi:<ip>", secret=...)` end-to-end, including upload**

Write a small script (a variant of `examples/blink_and_log/blink_and_log.py`, or reuse a scratch script) with one `@mcu.export` function. Run it once with `mcu.connect("wifi:<ip>", secret="<the secret from Step 2>")` directly — **without any prior serial upload** — confirming wifi itself now pushes the code (the gap this whole feature exists to close). Call the function, confirm the correct result.

- [ ] **Step 5: Confirm a script edit propagates over wifi without touching serial**

Edit the script's `@mcu.export` function (change its return value), re-run the same wifi `mcu.connect(...)` call.
Expected: the NEW behavior takes effect — confirms upload-if-needed's hash-check correctly detects the change and re-uploads, entirely over wifi.

- [ ] **Step 6: Reconnect without a reset**

Run the same script twice in a row (two separate `mcu.connect("wifi:<ip>", secret=...)` calls, two separate Python processes or two calls in one process with `board.reconnect()` between them). Confirm both succeed with no physical reset needed — the actual, hardware-verified proof of Task 4's reconnect fix.

- [ ] **Step 7: Auth rejection**

Run `mcu.connect("wifi:<ip>", secret="wrong-secret")` (or omit `secret` entirely, with no `TETHER_WIFI_SECRET` env var set). Confirm it raises `WifiAuthError` promptly, not a timeout.

- [ ] **Step 8: `--danger-unauthenticated`**

Run `uv run tether provision-wifi --port <PORT> --ssid "<SSID>" --password "<PASSWORD>" --danger-unauthenticated`. Confirm the warning prints, no secret is shown, and `mcu.connect("wifi:<ip>")` (no `secret` at all) now succeeds.

- [ ] **Step 9: Confirm serial is completely undisturbed**

Run `examples/blink_and_log/blink_and_log.py` (unmodified) over serial one final time.
Expected: clean, unaffected by everything built in this plan.

- [ ] **Step 10: Update documentation**

Update `README.md`, `docs/DESIGN.md`, and `docs/CHUNKS.md` to describe the new modes, auth, and non-destructive status — matching this project's established pattern (see the original wifi feature's own doc updates and the follow-up hardware-verification doc PR) — with a real, hardware-verified status, not aspirational language. Commit these doc updates separately from the code (matching this project's established convention of a dedicated `docs:` closeout commit).
