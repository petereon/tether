import hashlib
import hmac
import json

from tether.provisioning import STATUS_SCRIPT, generate_ble_boot, generate_wifi_boot


def _send_wifi_preamble(sock, mode, secret, send_json, read_json):
    """Test helper standing in for tether.transports.wifi.send_preamble's
    request half only (not its ack-read-and-raise half) - these
    hand-written test clients want to inspect the raw ack dict directly
    (including auth-failure acks), not have WifiAuthError raised for them.
    Performs the same nonce-challenge round trip the real on-device
    accept-loop now requires (see provisioning.py's _BOOT_PY_TEMPLATE):
    read the nonce the device sends immediately on accept(), then send
    HMAC-SHA256(secret, nonce) as the response (or None if unauthenticated).

    Returns the raw nonce bytes - callers that go on to read a per-frame
    authenticated reply (status/upload, once a secret was accepted) derive
    the session key from it the same way the device does:
    HMAC-SHA256(secret, nonce).digest() (see connection.py/provisioning.py).
    Existing callers that only inspect the ack (or intentionally send a
    wrong/no secret) simply ignore this return value.
    """
    nonce = bytes.fromhex(read_json(sock)["nonce"])
    response = (
        hmac.new(secret.encode(), nonce, hashlib.sha256).hexdigest() if secret is not None else None
    )
    send_json(sock, {"mode": mode, "response": response})
    return nonce


def test_generate_wifi_boot_produces_boot_py_and_config():
    files = generate_wifi_boot("MyNetwork", "hunter2")

    assert set(files.keys()) == {"/boot.py", "/tether_wifi.json"}
    assert isinstance(files["/boot.py"], bytes)
    assert isinstance(files["/tether_wifi.json"], bytes)


def test_generate_wifi_boot_embeds_credentials_only_in_the_config_file():
    files = generate_wifi_boot("MyNetwork", "hunter2")

    config = json.loads(files["/tether_wifi.json"])
    # Exact-membership rather than exact-equality: a shared secret is
    # generated into this same config by default (see
    # test_generate_wifi_boot_includes_a_secret_by_default) - this test's
    # job is only to confirm the credentials themselves are right and
    # nowhere else, not to enumerate every key the config may carry.
    assert config["ssid"] == "MyNetwork"
    assert config["password"] == "hunter2"
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


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mpy_runner import requires_micropython, run_micropython

from tether import mcu
from tether.connection import PROTOCOL_VERSION, generate_bootstrap


# Module-level, deliberately - _capture_caller() (connection.py) collects
# @mcu.export functions from the CALLING file's already-executed module
# globals, so this needs to live at this file's top level (not nested
# inside a test function) for test_real_pc_connect_wifi_against_real_
# on_device_boot_py below to slice/upload/call it through the real
# connect() pipeline, exactly like a real user's script would.
@mcu.export
def e2e_add(a: int, b: int) -> int:
    return a + b


@requires_micropython
def test_status_script_polls_instead_of_checking_isconnected_once():
    # Real-hardware finding: `reset_board()` (RTS/DTR pulse, ~1.3s) returns
    # long before boot.py's own wifi-connect-wait loop finishes - and the
    # raw-REPL entry that follows it sends Ctrl-C, interrupting that loop
    # before association+DHCP (empirically 2-6s) completes. A single
    # up-front `isconnected()` check therefore reports "not connected" on
    # a board that would have connected within another second or two.
    #
    # Fakes a `network.WLAN` whose isconnected() only turns True after a
    # few calls (standing in for "association finishes shortly after
    # STATUS_SCRIPT starts checking") and asserts STATUS_SCRIPT's JSON
    # output reports connected - proving it polls rather than giving up on
    # the first check. Against the pre-fix single-shot STATUS_SCRIPT this
    # fails (reports connected: false), since isconnected() is only ever
    # called once.
    script = f"""
import sys as _sys

class _FakeWLAN:
    _calls = 0
    def __init__(self, *a):
        pass
    def isconnected(self):
        _FakeWLAN._calls += 1
        return _FakeWLAN._calls > 3
    def ifconfig(self):
        return ("10.0.0.5", "255.255.255.0", "10.0.0.1", "10.0.0.1")

class _FakeNetwork:
    STA_IF = 0
    WLAN = _FakeWLAN

_sys.modules["network"] = _FakeNetwork

# Fake uos.listdir to include tether_wifi.json (provisioned board)
import uos as _real_uos
class _FakeUos:
    def listdir(self, path):
        result = list(_real_uos.listdir(path))
        # Ensure tether_wifi.json is in the list (provisioned board)
        if "tether_wifi.json" not in result:
            result.append("tether_wifi.json")
        return result

_fake_uos = _FakeUos()
_sys.modules["uos"] = _fake_uos

{STATUS_SCRIPT.decode()}
"""
    stdout = run_micropython(script, timeout=10.0)
    info = json.loads(stdout.strip())

    assert info["connected"] is True
    assert info["ip"] == "10.0.0.5"


@requires_micropython
def test_status_script_never_provisioned_board_does_not_wait():
    # Regression test for the performance issue: a never-provisioned board
    # (no /tether_wifi.json) should report status quickly without waiting
    # 8 seconds for a wifi connection that will never happen.
    #
    # Fakes a `network.WLAN` where isconnected() tracks call count, and
    # fakes the filesystem so that listdir("/") does NOT contain
    # "tether_wifi.json", causing _provisioned = False. Asserts that
    # isconnected() is called only once or twice (not ~40 times from an
    # 8-second/200ms poll loop).
    script = f"""
import sys as _sys

class _FakeWLAN:
    _calls = 0
    def __init__(self, *a):
        pass
    def isconnected(self):
        _FakeWLAN._calls += 1
        return False
    def ifconfig(self):
        return ("0.0.0.0", "255.255.255.0", "0.0.0.0", "0.0.0.0")

class _FakeNetwork:
    STA_IF = 0
    WLAN = _FakeWLAN

_sys.modules["network"] = _FakeNetwork

# Fake uos with a listdir that excludes tether_wifi.json (never-provisioned board)
import uos as _real_uos
class _FakeUos:
    def listdir(self, path):
        result = list(_real_uos.listdir(path))
        # Remove tether_wifi.json if present, simulating a never-provisioned board
        if "tether_wifi.json" in result:
            result.remove("tether_wifi.json")
        return result

_fake_uos = _FakeUos()
_sys.modules["uos"] = _fake_uos

{STATUS_SCRIPT.decode()}

# Print the call count so we can verify it's low
print("_calls: " + str(_FakeWLAN._calls))
"""
    stdout = run_micropython(script, timeout=10.0)
    lines = stdout.strip().split("\n")
    # Last line should be the call count, second-to-last should be the JSON status
    call_count_line = lines[-1]
    status_line = lines[-2]

    # Extract call count
    call_count = int(call_count_line.split(": ")[1])
    info = json.loads(status_line)

    # Should complete without waiting the full 8 seconds, so isconnected()
    # should be called at most 2-3 times (once to check, maybe one more
    # from other code flow), not ~40 times from 8000ms / 200ms polling.
    assert call_count <= 3, (
        f"Expected isconnected() to be called at most 3 times, but was called {call_count} times (indicating it waited through the poll loop)"
    )
    assert info["provisioned"] is False
    assert info["connected"] is False


@requires_micropython
def test_status_script_exits_early_via_status_code_instead_of_waiting_full_deadline():
    # Real-hardware finding (ESP32, network "Culo"): network.WLAN.status()
    # returns numeric state codes during connection - STAT_CONNECTING
    # (1001) while still associating, STAT_GOT_IP (1010) on success. Once
    # status() leaves the "in progress" range (1000 STAT_IDLE / 1001
    # STAT_CONNECTING), the outcome (success or failure) is already
    # decided - there's no reason to keep polling out the full 8s/200ms
    # deadline. This fakes a WLAN whose status() reports "connecting" for
    # the first couple of calls then flips to "got IP", with isconnected()
    # (the ground truth reported in the final JSON) catching up by the
    # time of the post-loop check - and asserts the loop exits after only
    # a handful of iterations, not the ~40 an 8000ms/200ms deadline would
    # produce. Against the pre-fix STATUS_SCRIPT (status()-blind) this
    # fails: isconnected() never becomes true (nothing ever increments the
    # fake's internal stage without status() being polled), so it waits
    # out the whole 8s deadline and still reports connected: false.
    script = f"""
import sys as _sys

class _FakeWLAN:
    _calls = 0
    _status_calls = 0
    def __init__(self, *a):
        pass
    def isconnected(self):
        _FakeWLAN._calls += 1
        return _FakeWLAN._status_calls >= 3
    def status(self):
        _FakeWLAN._status_calls += 1
        return 1001 if _FakeWLAN._status_calls < 3 else 1010
    def ifconfig(self):
        return ("10.0.0.7", "255.255.255.0", "10.0.0.1", "10.0.0.1")

class _FakeNetwork:
    STA_IF = 0
    STAT_IDLE = 1000
    STAT_CONNECTING = 1001
    STAT_GOT_IP = 1010
    WLAN = _FakeWLAN

_sys.modules["network"] = _FakeNetwork

# Fake uos.listdir to include tether_wifi.json (provisioned board)
import uos as _real_uos
class _FakeUos:
    def listdir(self, path):
        result = list(_real_uos.listdir(path))
        if "tether_wifi.json" not in result:
            result.append("tether_wifi.json")
        return result

_fake_uos = _FakeUos()
_sys.modules["uos"] = _fake_uos

{STATUS_SCRIPT.decode()}

print("_calls: " + str(_FakeWLAN._calls))
"""
    stdout = run_micropython(script, timeout=12.0)
    lines = stdout.strip().split("\n")
    call_count_line = lines[-1]
    status_line = lines[-2]

    call_count = int(call_count_line.split(": ")[1])
    info = json.loads(status_line)

    # An 8000ms/200ms deadline would call isconnected() ~40 times; exiting
    # via the status-code fast path should keep this in the single digits.
    assert call_count <= 6, (
        f"Expected isconnected() to be called at most 6 times (fast exit via "
        f"status()), but was called {call_count} times (indicates it waited "
        f"through the full poll loop)"
    )
    assert info["connected"] is True
    assert info["ip"] == "10.0.0.7"


@requires_micropython
def test_status_script_skips_fast_exit_when_status_code_scheme_is_unknown():
    # Guards against reintroducing the exact bug the fast-exit fixes: a
    # board whose network module has a *different* status() numeric scheme
    # (e.g. rp2/Pico W's STAT_LINK_DOWN/STAT_LINK_JOIN/STAT_LINK_UP/etc,
    # not ESP32's STAT_IDLE/STAT_CONNECTING) must not have the fast-exit
    # misinterpret an in-progress code as terminal and report "not
    # connected" before the connection actually finishes. This fakes a
    # WLAN with a status() that returns rp2-style codes (1 = "joining",
    # unrelated to ESP32's 1000/1001) and no STAT_IDLE/STAT_CONNECTING
    # attributes on the network module at all - since neither named
    # constant exists, the fast-exit must not activate, and the script
    # must fall back to the plain isconnected()-only wait until it
    # actually becomes true.
    script = f"""
import sys as _sys

class _FakeWLAN:
    _calls = 0
    def __init__(self, *a):
        pass
    def isconnected(self):
        _FakeWLAN._calls += 1
        return _FakeWLAN._calls > 3
    def status(self):
        # rp2-style STAT_LINK_JOIN == 1 - numerically inside ESP32's old
        # hardcoded 1000/1001 "in progress" range check would have been
        # (wrongly) treated as terminal by a naive `< 1000 or > 1001` test.
        return 1
    def ifconfig(self):
        return ("10.0.0.9", "255.255.255.0", "10.0.0.1", "10.0.0.1")

class _FakeNetwork:
    STA_IF = 0
    STAT_LINK_JOIN = 1
    WLAN = _FakeWLAN

_sys.modules["network"] = _FakeNetwork

# Fake uos.listdir to include tether_wifi.json (provisioned board)
import uos as _real_uos
class _FakeUos:
    def listdir(self, path):
        result = list(_real_uos.listdir(path))
        if "tether_wifi.json" not in result:
            result.append("tether_wifi.json")
        return result

_fake_uos = _FakeUos()
_sys.modules["uos"] = _fake_uos

{STATUS_SCRIPT.decode()}
"""
    stdout = run_micropython(script, timeout=10.0)
    info = json.loads(stdout.strip())

    # Must still reach "connected" via the plain isconnected() poll - not
    # exit early/misreport "not connected" just because status() returned
    # a value outside ESP32's numeric range.
    assert info["connected"] is True
    assert info["ip"] == "10.0.0.9"


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
        _send_wifi_preamble(sock, "status", None, send_json, read_json)
        results["status_ack"] = read_json(sock)
        results["status_payload"] = read_json(sock)
        sock.close()

        sock2 = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        _send_wifi_preamble(sock2, "bogus", None, send_json, read_json)
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
        _send_wifi_preamble(sock, "status", "wrong-one", send_json, read_json)
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


@requires_micropython
def test_boot_py_accepts_correct_secret():
    # Companion to test_boot_py_rejects_wrong_secret: a board configured
    # with a secret must accept a connection that presents the *matching*
    # secret, not just reject a mismatched one. Without this, "wrong
    # secret rejected" alone can't distinguish "auth works" from "auth
    # always fails closed".
    import json as pc_json
    import socket
    import struct
    import threading
    import time

    from mpy_runner import run_micropython_background

    from tether.marshalling.frame_auth import FrameAuthenticator
    from tether.transports.wifi import read_authenticated_json_frame

    test_port = 18769
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
        nonce = _send_wifi_preamble(sock, "status", "the-real-secret", send_json, read_json)
        results["ack"] = read_json(sock)
        # The status reply is per-frame authenticated (Task 4) once a
        # secret was accepted - see
        # test_boot_py_status_reply_is_frame_authenticated_when_secret_configured
        # for the dedicated test of this; this one just needs to read past
        # it correctly to keep verifying the payload contents themselves.
        session_key = hmac.new(b"the-real-secret", nonce, hashlib.sha256).digest()
        results["payload"] = read_authenticated_json_frame(sock, FrameAuthenticator(session_key))
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

    assert results["ack"] == {"ok": True}
    payload = results["payload"]
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["ip"] == "10.0.0.5"


@requires_micropython
def test_boot_py_survives_malformed_preamble_and_serves_next_connection():
    # Regression test for the accept-loop's per-connection exception
    # handling being too narrow (only `except OSError`). Against the real
    # micropython interpreter: ujson.loads() on malformed JSON bytes
    # raises ValueError, and calling .get("mode") on syntactically-valid
    # but non-dict JSON (e.g. a bare int) raises AttributeError. Neither
    # was an OSError, so either one used to propagate out of the
    # `while True:` accept-loop and kill the whole boot.py process - a
    # single malformed/non-JSON preamble (client bug, port scanner, stray
    # TCP probe) would permanently take down the listener, requiring a
    # physical reset. The real proof the loop survives isn't "no
    # exception was visibly raised" - it's that a SUBSEQUENT,
    # well-formed connection is still served afterward.
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

    def send_raw_frame(sock, body):
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

        # 1. Malformed JSON bytes - ujson.loads() raises ValueError.
        sock1 = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        send_raw_frame(sock1, b"{not valid json at all")
        sock1.close()
        time.sleep(0.3)

        # 2. Syntactically valid JSON, but not an object - .get("mode")
        # raises AttributeError on the parsed int.
        sock2 = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        send_json(sock2, 42)
        sock2.close()
        time.sleep(0.3)

        # 3. A subsequent, well-formed connection must still be served -
        # this is the real proof the accept-loop didn't crash.
        sock3 = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        _send_wifi_preamble(sock3, "status", None, send_json, read_json)
        results["status_ack"] = read_json(sock3)
        results["status_payload"] = read_json(sock3)
        sock3.close()

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

    run_micropython_background(script, run_for=4.0)
    client_thread.join(timeout=10.0)

    assert "status_ack" in results, (
        "no response to the well-formed connection sent AFTER the malformed "
        "ones - the accept-loop likely crashed and killed the whole process"
    )
    assert results["status_ack"] == {"ok": True}
    payload = results["status_payload"]
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["ip"] == "10.0.0.5"


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
        _send_wifi_preamble(sock, "run", None, send_json, read_json)
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


@requires_micropython
def test_boot_py_run_mode_does_not_accumulate_loop_tasks_across_reconnects():
    # Final-review finding: the dominant duplication mechanism isn't the
    # mcu_decorators registry (Task 2's fix, verified separately) - it's
    # that MicroPython's uasyncio task queue is a process-global structure,
    # and asyncio.run() returning/raising does NOT drain tasks queued via
    # asyncio.create_task() inside it (Dispatcher.run() does exactly that
    # for every @mcu.loop function). So each fresh run-mode exec() leaves
    # the PREVIOUS session's @mcu.loop task(s) still alive in the global
    # queue, resumed alongside the new session's own task the next time
    # asyncio.run() is called - an accumulating, not replacing, duplicate.
    #
    # Observed via a counter stored as an attribute on the `mcu_decorators`
    # module object itself - like `_registrations`, that module is only
    # ever *imported* (not re-exec'd) within one boot.py process, so the
    # attribute persists across successive run-mode exec()s, exactly the
    # same persistence property that causes the underlying bug. Any
    # leftover, not-yet-drained task from an earlier session still holds a
    # reference to this same singleton module object and keeps incrementing
    # the same counter, so accumulation is directly observable from the PC
    # side by calling a plain @mcu.export getter after each session's
    # sampling window - no reliance on reading anything from a closed
    # connection's own dead namespace.
    import socket
    import struct
    import threading
    import time

    import msgpack
    from mpy_runner import run_micropython_background

    from tether.marshalling import encode_frame

    test_port = 18771
    boot_py = (
        generate_wifi_boot("irrelevant", "irrelevant")["/boot.py"]
        .decode()
        .replace(str(8765), str(test_port))
    )

    sliced_source = """\
import mcu_decorators as _test_mod

if not hasattr(_test_mod, "_tick_total"):
    _test_mod._tick_total = 0


@mcu.loop(interval_ms=100)
def tick():
    _test_mod._tick_total += 1


@mcu.export
def get_tick_total() -> int:
    return _test_mod._tick_total
"""
    tether_app_source = generate_bootstrap(sliced_source, "")

    length_prefix = struct.Struct(">I")

    def send_json(sock, obj):
        import json as pc_json

        body = pc_json.dumps(obj).encode()
        sock.sendall(length_prefix.pack(len(body)) + body)

    def read_json(sock):
        import json as pc_json

        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return pc_json.loads(body)

    def read_result_frame(sock):
        raw_len = sock.recv(4)
        body_len = int.from_bytes(raw_len, "big")
        body = b""
        while len(body) < body_len:
            body += sock.recv(body_len - len(body))
        msg_type = body[:1]
        payload = msgpack.unpackb(body[1:], raw=False)
        return msg_type, payload

    sample_window = 1.5  # ~15 ticks/session at interval_ms=100, generous re: jitter

    def do_one_run_session(req_id):
        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        _send_wifi_preamble(sock, "run", None, send_json, read_json)
        ack = read_json(sock)
        assert ack == {"ok": True}, ack

        sock.sendall(encode_frame(1, {"id": req_id, "name": "__tether_handshake__", "args": []}))
        msg_type, payload = read_result_frame(sock)
        assert msg_type == b"\x02", (msg_type, payload)  # MSG_RESULT

        # Let the loop task(s) - however many are actually alive - tick for
        # a fixed sampling window before asking for the running total.
        time.sleep(sample_window)

        sock.sendall(encode_frame(1, {"id": req_id + 1, "name": "get_tick_total", "args": []}))
        msg_type, payload = read_result_frame(sock)
        assert msg_type == b"\x02", (msg_type, payload)  # MSG_RESULT
        sock.close()
        return payload["value"]

    results = {}

    def run_client():
        time.sleep(0.5)
        results["first"] = do_one_run_session(1)
        time.sleep(0.5)
        results["second"] = do_one_run_session(3)
        time.sleep(0.5)
        results["third"] = do_one_run_session(5)

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

    run_micropython_background(script, run_for=9.0)
    client_thread.join(timeout=15.0)

    first, second, third = results["first"], results["second"], results["third"]
    delta1 = first
    delta2 = second - first
    delta3 = third - second

    # Sanity: ticks are actually firing at roughly the expected rate each
    # session (not zero - that would mean the loop never ran at all, a
    # different bug this test isn't about).
    assert delta1 > 0, results
    assert delta2 > 0, results
    assert delta3 > 0, results

    # The actual regression check: broken behavior accumulates one more
    # concurrently-running duplicate task per reconnect, so delta2 ~= 2x
    # delta1 and delta3 ~= 3x delta1 (reviewer's real measurement: 19/58/117,
    # i.e. deltas 19/39/59). Fixed behavior keeps each session's delta
    # roughly constant (reviewer's real measurement: 19/38/57 -> deltas
    # 19/19/19). 1.6x comfortably separates "roughly constant, plus
    # scheduling jitter" from "accumulating an extra duplicate task" without
    # being so tight that timing jitter alone could trip it.
    assert delta3 < delta1 * 1.6, (
        f"loop tick count is accumulating across reconnects, not staying "
        f"constant per session: deltas were {delta1}, {delta2}, {delta3} "
        f"(totals {first}, {second}, {third})"
    )


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
        _send_wifi_preamble(sock, "upload", None, send_json, read_json)
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
        _send_wifi_preamble(sock2, "status", None, send_json, read_json)
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


@requires_micropython
def test_boot_py_upload_mode_rejects_a_chunk_that_overflows_the_declared_file_size():
    # Final-review finding: _handle_upload only checked a chunk against the
    # absolute MAX_CTRL_FRAME bound (64 KiB), never against how many bytes
    # were actually still declared-remaining for the file currently being
    # written. A chunk bigger than what's left of the CURRENT file's
    # declared size used to get written anyway and _remaining would go
    # negative, silently accepting a malformed/oversized chunk instead of
    # failing loud - and in a multi-file upload, would spill into what
    # should have been the next file's own frames. A compliant chunked
    # client (Fix 3's PC-side chunker) never sends a chunk like this; this
    # is a defensive on-device guard proving the check exists regardless.
    import json as pc_json
    import socket
    import struct
    import threading
    import time

    from mpy_runner import run_micropython_background

    test_port = 18772
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

    results = {}

    def run_client():
        time.sleep(0.5)

        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        _send_wifi_preamble(sock, "upload", None, send_json, read_json)
        results["upload_ack"] = read_json(sock)

        # Declares a 5-byte file, then sends a single 10-byte chunk for it -
        # a well-behaved chunker never does this (chunks are always <= the
        # declared remaining size), but a malformed/buggy/malicious client
        # could. The device must reject this immediately, not accept it.
        send_json(
            sock,
            {"dirs": [], "files": [{"path": "/tether_app.py", "size": 5}]},
        )
        send_bytes(sock, b"0123456789")
        results["upload_result"] = read_json(sock)
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

    run_micropython_background(script, run_for=3.0)
    client_thread.join(timeout=10.0)

    assert results["upload_ack"] == {"ok": True}
    assert results["upload_result"]["ok"] is False, results["upload_result"]


@requires_micropython
def test_boot_py_invalidates_stale_hash_when_an_upload_fails_partway():
    # Final-review finding: .tether_hash was written last on a successful
    # upload (correct), but nothing invalidated the OLD one first. If an
    # upload fails partway (a wifi drop, flash exhaustion, Fix 3's oversize
    # case before that fix), the device was left with a partially-written
    # bundle but a hash sentinel still asserting the OLD bundle is intact -
    # so the next connect() would see a hash "match" and skip re-uploading,
    # then run mode would exec a broken/truncated file.
    #
    # Simulates a failed upload using the same chunk-overflow trigger Fix
    # 3's device-side guard test uses (a chunk bigger than the declared
    # remaining size for the file being written), with a pre-existing
    # /.tether_hash already on the fake filesystem standing in for "a
    # previous successful upload happened". Asserts a follow-up status
    # query reports tether_app_hash: None, not the old value - proving the
    # sentinel was invalidated up front, not left stale by the failure.
    import json as pc_json
    import socket
    import struct
    import threading
    import time

    from mpy_runner import run_micropython_background

    test_port = 18773
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

    results = {}

    def run_client():
        time.sleep(0.5)

        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        _send_wifi_preamble(sock, "upload", None, send_json, read_json)
        results["upload_ack"] = read_json(sock)

        # Same failure trigger as Fix 3's device-side guard test: a chunk
        # bigger than the file's own declared size.
        send_json(
            sock,
            {"dirs": [], "files": [{"path": "/tether_app.py", "size": 5}]},
        )
        send_bytes(sock, b"0123456789")
        results["upload_result"] = read_json(sock)
        sock.close()

        time.sleep(0.5)
        sock2 = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        _send_wifi_preamble(sock2, "status", None, send_json, read_json)
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

_files = {{
    "/tether_wifi.json": '{{"ssid": "irrelevant", "password": "irrelevant"}}',
    "/.tether_hash": "old-hash-from-a-previous-successful-upload",
}}

class _FakeUos:
    def remove(self, path):
        if path in _files:
            del _files[path]
        else:
            raise OSError(2, "no such file")
    def mkdir(self, path):
        pass
    def urandom(self, n):
        # This fake replaces the whole uos module, and the accept loop now
        # calls uos.urandom() once per connection for its nonce - a fixed
        # value is fine, nothing here checks its actual randomness.
        return bytes(n)

_sys.modules["uos"] = _FakeUos()

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
    assert results["upload_result"]["ok"] is False, results["upload_result"]
    assert results["status_ack"] == {"ok": True}
    assert results["status_payload"]["tether_app_hash"] is None, results["status_payload"]


@requires_micropython
def test_boot_py_status_reply_is_frame_authenticated_when_secret_configured():
    import hashlib
    import hmac
    import json as pc_json
    import socket
    import struct
    import threading
    import time

    from mpy_runner import run_micropython_background

    from tether.marshalling.frame_auth import FrameAuthenticator
    from tether.transports.wifi import read_authenticated_json_frame

    test_port = 18775  # next unused port after the existing 18767-18774
    secret = "the-secret"
    boot_py = (
        generate_wifi_boot("irrelevant", "irrelevant")["/boot.py"]
        .decode()
        .replace(str(8765), str(test_port))
    )

    length_prefix = struct.Struct(">I")

    def read_json(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return pc_json.loads(body)

    def send_json(sock, obj):
        body = pc_json.dumps(obj).encode()
        sock.sendall(length_prefix.pack(len(body)) + body)

    results = {}

    def run_client():
        time.sleep(0.5)
        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        nonce_frame = read_json(sock)
        nonce = bytes.fromhex(nonce_frame["nonce"])
        response = hmac.new(secret.encode(), nonce, hashlib.sha256).hexdigest()
        send_json(sock, {"mode": "status", "response": response})
        ack = read_json(sock)
        assert ack == {"ok": True}, ack
        session_key = hmac.new(secret.encode(), nonce, hashlib.sha256).digest()
        results["status"] = read_authenticated_json_frame(sock, FrameAuthenticator(session_key))
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

_files = {{
    "/tether_wifi.json": '{{"ssid": "irrelevant", "password": "irrelevant", "secret": "{secret}"}}',
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

def _fake_open(path, mode="r", *a, **kw):
    if path in _files:
        return _FakeFile(_files[path])
    raise OSError(2, "no such file")
import builtins
builtins.open = _fake_open

{boot_py}
"""

    run_micropython_background(script, run_for=3.0)
    client_thread.join(timeout=8.0)

    status = results.get("status")
    assert status is not None, results
    assert set(status.keys()) == {
        "protocol_version",
        "tether_app_hash",
        "free_heap",
        "uptime_ms",
        "ip",
    }


@requires_micropython
def test_boot_py_upload_mode_authenticates_the_manifest_and_chunks():
    import hashlib
    import hmac
    import json as pc_json
    import socket
    import struct
    import threading
    import time

    from mpy_runner import run_micropython_background

    from tether.marshalling.frame_auth import FrameAuthenticator
    from tether.transports.wifi import (
        read_authenticated_json_frame,
        send_authenticated_bytes_frame,
        send_authenticated_json_frame,
    )

    test_port = 18776
    secret = "the-secret"
    boot_py = (
        generate_wifi_boot("irrelevant", "irrelevant")["/boot.py"]
        .decode()
        .replace(str(8765), str(test_port))
    )
    file_content = b"print('hello')\n"

    length_prefix = struct.Struct(">I")

    def read_json(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return pc_json.loads(body)

    def send_json(sock, obj):
        body = pc_json.dumps(obj).encode()
        sock.sendall(length_prefix.pack(len(body)) + body)

    results = {}

    def run_client():
        time.sleep(0.5)
        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        nonce_frame = read_json(sock)
        nonce = bytes.fromhex(nonce_frame["nonce"])
        response = hmac.new(secret.encode(), nonce, hashlib.sha256).hexdigest()
        send_json(sock, {"mode": "upload", "response": response})
        ack = read_json(sock)
        assert ack == {"ok": True}, ack
        session_key = hmac.new(secret.encode(), nonce, hashlib.sha256).digest()
        send_auth = FrameAuthenticator(session_key)
        recv_auth = FrameAuthenticator(session_key)

        manifest = {"dirs": [], "files": [{"path": "/uploaded.py", "size": len(file_content)}]}
        send_authenticated_json_frame(sock, send_auth, manifest)
        send_authenticated_bytes_frame(sock, send_auth, file_content)
        results["result"] = read_authenticated_json_frame(sock, recv_auth)
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

_written = {{}}

_files = {{
    "/tether_wifi.json": '{{"ssid": "irrelevant", "password": "irrelevant", "secret": "{secret}"}}',
}}

class _FakeFile:
    def __init__(self, content):
        self._content = content
    def read(self):
        return self._content
    def write(self, data):
        _written[self._path] = _written.get(self._path, b"") + data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def _fake_open(path, mode="r", *a, **kw):
    if "w" in mode:
        f = _FakeFile(b"")
        f._path = path
        _written[path] = b""
        return f
    if path in _files:
        return _FakeFile(_files[path])
    raise OSError(2, "no such file")
import builtins
builtins.open = _fake_open

class _FakeUos:
    def mkdir(self, *a):
        pass
    def remove(self, *a):
        raise OSError(2, "no such file")
    def urandom(self, n):
        return bytes(n)

_sys.modules["uos"] = _FakeUos()

{boot_py}
"""

    # run_micropython_background() never returns captured stdout (see its
    # own docstring - a forcibly-terminated background process's buffered
    # output isn't reliably available), so success is verified entirely
    # through the real side channel: the authenticated "ok" reply the
    # client thread reads back over the socket, which _handle_upload only
    # sends after every declared chunk for /uploaded.py was written
    # without error.
    run_micropython_background(script, run_for=3.0)
    client_thread.join(timeout=8.0)

    assert results.get("result") == {"ok": True}, results


@requires_micropython
def test_boot_py_upload_mode_rejects_a_tampered_chunk():
    import hashlib
    import hmac
    import json as pc_json
    import socket
    import struct
    import threading
    import time

    from mpy_runner import run_micropython_background

    from tether.marshalling.frame_auth import FrameAuthenticator
    from tether.transports.wifi import read_authenticated_json_frame, send_authenticated_json_frame

    test_port = 18777
    secret = "the-secret"
    boot_py = (
        generate_wifi_boot("irrelevant", "irrelevant")["/boot.py"]
        .decode()
        .replace(str(8765), str(test_port))
    )
    file_content = b"print('hello')\n"

    length_prefix = struct.Struct(">I")

    def read_json(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return pc_json.loads(body)

    def send_json(sock, obj):
        body = pc_json.dumps(obj).encode()
        sock.sendall(length_prefix.pack(len(body)) + body)

    results = {}

    def run_client():
        time.sleep(0.5)
        sock = socket.create_connection(("127.0.0.1", test_port), timeout=5.0)
        nonce_frame = read_json(sock)
        nonce = bytes.fromhex(nonce_frame["nonce"])
        response = hmac.new(secret.encode(), nonce, hashlib.sha256).hexdigest()
        send_json(sock, {"mode": "upload", "response": response})
        ack = read_json(sock)
        assert ack == {"ok": True}, ack
        session_key = hmac.new(secret.encode(), nonce, hashlib.sha256).digest()
        send_auth = FrameAuthenticator(session_key)
        recv_auth = FrameAuthenticator(session_key)

        manifest = {"dirs": [], "files": [{"path": "/uploaded.py", "size": len(file_content)}]}
        send_authenticated_json_frame(sock, send_auth, manifest)

        tampered = bytearray(send_auth.wrap(file_content))
        tampered[-1] ^= 0xFF
        sock.sendall(bytes(tampered))

        results["result"] = read_authenticated_json_frame(sock, recv_auth)
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

_files = {{
    "/tether_wifi.json": '{{"ssid": "irrelevant", "password": "irrelevant", "secret": "{secret}"}}',
}}

class _FakeFile:
    def __init__(self, content):
        self._content = content
    def read(self):
        return self._content
    def write(self, data):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def _fake_open(path, mode="r", *a, **kw):
    if "w" in mode:
        return _FakeFile(b"")
    if path in _files:
        return _FakeFile(_files[path])
    raise OSError(2, "no such file")
import builtins
builtins.open = _fake_open

class _FakeUos:
    def mkdir(self, *a):
        pass
    def remove(self, *a):
        raise OSError(2, "no such file")
    def urandom(self, n):
        return bytes(n)

_sys.modules["uos"] = _FakeUos()

{boot_py}
"""

    run_micropython_background(script, run_for=3.0)
    client_thread.join(timeout=8.0)

    result = results.get("result")
    assert result is not None and result.get("ok") is False, results


@requires_micropython
def test_real_pc_connect_wifi_against_real_on_device_boot_py():
    # The single test that would have caught Fix 1 (asyncio task-queue
    # accumulation across reconnects) and Fix 5 (reconnect deadlock) far
    # earlier: every other wifi test on the PC side talks to a hand-rolled
    # fake device, and every other wifi test on the device side talks to a
    # hand-rolled fake PC client. This drives the REAL, public
    # tether.connection.connect() against a REAL generated boot.py running
    # under the real micropython interpreter - full interop between the
    # actual client and the actual device, not two halves each
    # individually faked.
    #
    # Exercises the real end-to-end flow: slice (this file) -> status
    # query (nothing on-device yet) -> upload (the full runtime bundle,
    # real _gather_runtime_bundle reading real files off disk) -> run ->
    # a real @mcu.export function call (e2e_add, defined at this file's
    # module level above) returns the correct value over the real wire
    # protocol. Then, since Fix 5 should make it work now, exercises
    # board.reconnect() and a second real call - this time the status
    # query should report a matching hash (no re-upload needed).
    import threading
    import time

    from mpy_runner import run_micropython_background

    from tether.connection import connect

    test_port = 18774
    boot_py = (
        generate_wifi_boot("irrelevant", "irrelevant")["/boot.py"]
        .decode()
        .replace(str(8765), str(test_port))
    )

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

class _FakeUos:
    def mkdir(self, path):
        pass
    def remove(self, path):
        if path in _files:
            del _files[path]
        else:
            raise OSError(2, "no such file")
    def urandom(self, n):
        return bytes(n)

_sys.modules["uos"] = _FakeUos()

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

    device_thread = threading.Thread(
        target=run_micropython_background,
        args=(script,),
        kwargs={"run_for": 12.0},
        daemon=True,
    )
    device_thread.start()
    time.sleep(0.5)  # let the device get through wifi-connect + listen setup

    board = connect(f"wifi:127.0.0.1:{test_port}", timeout=5.0)
    assert board.e2e_add(3, 4) == 7

    board.reconnect()
    assert board.e2e_add(10, 20) == 30

    device_thread.join(timeout=15.0)


# --- BLE peripheral (generate_ble_boot / _BLE_BOOT_TEMPLATE) -----------
#
# Every test below drives the *real* generated boot.py under the real
# micropython interpreter against a fake `bluetooth` module (tests/
# ble_fakes.py - see its docstring for why a fake, and why threads).


@requires_micropython
def test_fake_bluetooth_module_loads_and_simulates_connect_write_notify():
    # Pure infrastructure validation, run before anything depends on it:
    # if the fake itself doesn't behave like MicroPython's `bluetooth`
    # module, every later BLE test here is unreliable rather than wrong in
    # an obvious way.
    from ble_fakes import FAKE_BLUETOOTH_MODULE_SRC

    script = (
        FAKE_BLUETOOTH_MODULE_SRC
        + """
import bluetooth

_ble = bluetooth.BLE()
_ble.active(True)
_ble.irq(lambda e, d: print("irq", e, d))
handles = _ble.gatts_register_services(("service-tuple-goes-here",))
print("handles:", handles)
_ble._simulate_connect(0)
_ble._simulate_mtu(0, 100)
_ble._simulate_write(0, 100, b"hello")
print("gatts_read:", _ble.gatts_read(100))
_ble.gatts_notify(0, 101, b"world")
print("notifications:", _ble.notifications)
_ble._simulate_disconnect(0)
"""
    )
    out = run_micropython(script)

    assert "handles: ((100, 101),)" in out
    assert "irq 1 (0, 0, b'')" in out
    assert "irq 21 (0, 100)" in out
    assert "irq 3 (0, 100)" in out
    assert "gatts_read: b'hello'" in out
    assert "notifications: [(0, 101, b'world')]" in out
    assert "irq 2 (0, 0, b'')" in out


@requires_micropython
def test_ble_boot_status_mode_reports_hash_and_reuses_one_connection():
    # Two status exchanges over a SINGLE connection - the behavior that
    # distinguishes BLE's session loop from wifi's (where every mode costs
    # a fresh TCP connection). A reconnect per mode is expensive over BLE.
    from ble_fakes import combine_boot_py_with_driver

    boot_py = generate_ble_boot("s3cr3t")["/boot.py"].decode()

    driver = """
import bluetooth

_ble = bluetooth.BLE()  # singleton - the very instance boot.py registered
_c = Central(_ble)
_c.connect()

_ack1 = _c.send_preamble("status", "s3cr3t")
_status1 = _c.read_json_frame()
_ack2 = _c.send_preamble("status", "s3cr3t")
_status2 = _c.read_json_frame()

print("ack1:", _ack1)
print("hash1:", _status1["tether_app_hash"])
print("protocol_version:", _status1["protocol_version"])
print("addr:", _status1["ip"])
print("ack2:", _ack2)
print("hash2:", _status2["tether_app_hash"])
print("still_one_connection:", _ble.notifications[0][0] == 0)
"""

    combined = combine_boot_py_with_driver(
        boot_py,
        driver,
        files={"/tether_ble.json": '{"secret": "s3cr3t"}', "/.tether_hash": "cafef00d"},
    )
    out = run_micropython(combined, timeout=10.0)

    assert "BOOT THREAD DIED" not in out, out
    assert "ack1: {'ok': True}" in out, out
    assert "hash1: cafef00d" in out, out
    assert f"protocol_version: {PROTOCOL_VERSION}" in out, out
    assert "addr: aa:bb:cc:dd:ee:ff" in out, out
    assert "ack2: {'ok': True}" in out, out
    assert "hash2: cafef00d" in out, out


@requires_micropython
def test_ble_boot_upload_then_status_reuses_the_same_connection():
    # Upload a bundle, then - without reconnecting - query status on the
    # same connection and confirm the reported hash matches what was just
    # written. Proves the round trip through the real on-device write path,
    # not merely that an {"ok": True} came back.
    from ble_fakes import combine_boot_py_with_driver

    boot_py = generate_ble_boot("s3cr3t")["/boot.py"].decode()

    driver = """
import bluetooth

_ble = bluetooth.BLE()
_c = Central(_ble)
_c.connect()

_app = b"print('uploaded over ble')\\n"
_hash = b"0badcafe"

print("upload_ack:", _c.send_preamble("upload", "s3cr3t"))
_c.send_json_frame({"dirs": ["/lib"], "files": [
    {"path": "/tether_app.py", "size": len(_app)},
    {"path": "/.tether_hash", "size": len(_hash)},
]})
_c.send_bytes_frame(_app)
_c.send_bytes_frame(_hash)
print("upload_result:", _c.read_json_frame())

print("status_ack:", _c.send_preamble("status", "s3cr3t"))
print("status_hash:", _c.read_json_frame()["tether_app_hash"])
print("written_app:", _files["/tether_app.py"])
"""

    combined = combine_boot_py_with_driver(
        boot_py, driver, files={"/tether_ble.json": '{"secret": "s3cr3t"}'}
    )
    out = run_micropython(combined, timeout=10.0)

    assert "BOOT THREAD DIED" not in out, out
    assert "upload_ack: {'ok': True}" in out, out
    assert "upload_result: {'ok': True}" in out, out
    assert "status_ack: {'ok': True}" in out, out
    assert "status_hash: 0badcafe" in out, out
    assert "written_app: b\"print('uploaded over ble')\\n\"" in out, out


@requires_micropython
def test_ble_boot_resends_the_nonce_while_waiting_for_the_first_preamble():
    # Real-hardware regression: BleakClient.start_notify()'s CCCD
    # subscription write happens strictly AFTER client.connect() returns,
    # but the device's CENTRAL_CONNECT IRQ (which triggers the very first
    # nonce send) fires the instant the link forms - a notify() sent before
    # that subscription completes is silently dropped by the BLE stack,
    # with no delivery confirmation available to detect it. Confirmed via
    # direct real ESP32 + bleak reproduction: a single one-shot nonce send
    # was reliably lost every time, not just occasionally (see
    # provisioning.py's _NONCE_RESEND_MS for the full writeup).
    #
    # This fake can't model "silently dropped, not queued" - gatts_notify()
    # here always records the send - so this test verifies the RESEND
    # mechanism directly: a central slow to respond should see multiple
    # identical nonce notifications queued up over time, not just one,
    # proving the device kept trying rather than sending once and then
    # blocking forever waiting for a response that was never going to
    # arrive.
    from ble_fakes import combine_boot_py_with_driver

    boot_py = generate_ble_boot("s3cr3t")["/boot.py"].decode()

    driver = """
import bluetooth
import time

_ble = bluetooth.BLE()
_c = Central(_ble)
_c.connect()

# Longer than one _NONCE_RESEND_MS (300ms) interval, well short of
# _NONCE_MAX_WAIT_MS (5000ms) - long enough to observe at least one
# resend, nowhere near the give-up timeout.
time.sleep_ms(700)

_nonce_notifies = [n for n in _ble.notifications if b'"nonce"' in n[2]]
print("nonce_notify_count:", len(_nonce_notifies))
"""

    combined = combine_boot_py_with_driver(
        boot_py, driver, files={"/tether_ble.json": '{"secret": "s3cr3t"}'}
    )
    out = run_micropython(combined, timeout=10.0)

    assert "BOOT THREAD DIED" not in out, out
    count_line = next(line for line in out.splitlines() if line.startswith("nonce_notify_count:"))
    count = int(count_line.split(":")[1].strip())
    assert count >= 2, (
        f"expected at least 2 resent nonce notifications during a 700ms wait "
        f"(a single one-shot send is the exact bug this test guards against), got {count}: {out}"
    )


@requires_micropython
def test_ble_boot_rejects_wrong_secret_and_ends_the_session():
    from ble_fakes import combine_boot_py_with_driver

    boot_py = generate_ble_boot("correct-secret")["/boot.py"].decode()

    driver = """
import bluetooth

_ble = bluetooth.BLE()
_c = Central(_ble)
_c.connect()

_reply = _c.send_preamble("status", "wrong-secret")
print("reply:", _reply)
# No status payload may follow a rejected preamble, and the device must
# drop the link rather than loop back for another preamble - otherwise a
# wrong secret would cost nothing to retry over one connection.
time.sleep_ms(300)
print("total_notify_chunks:", len(_ble.notifications))
print("device_dropped_link:", _ble.disconnects)
print("readvertising:", _ble.advertising)
print("nack_flush_ms:", time.ticks_diff(_ble.disconnect_ticks[0], _ble.notify_ticks[-1]))
"""

    combined = combine_boot_py_with_driver(
        boot_py,
        driver,
        files={"/tether_ble.json": '{"secret": "correct-secret"}'},
    )
    out = run_micropython(combined, timeout=10.0)

    assert "BOOT THREAD DIED" not in out, out
    assert "reply: {'ok': False, 'error': 'auth failed'}" in out, out
    # 2, not 1: the device's one-per-connection nonce is its own
    # notification, sent before Central.send_preamble() even presents a
    # response - so a rejected preamble is nonce (1) + nack (2), not just
    # the nack alone.
    assert "total_notify_chunks: 2" in out, out
    assert "device_dropped_link: [0]" in out, out
    assert "readvertising: True" in out, out

    # Final-review finding (F3). gatts_notify() only *queues* an ATT
    # notification and there is no completion event for notifications, so
    # disconnecting the instant after sending the nack can tear the link
    # down before those bytes ever transmit - the client then sees a bare
    # connection-closed OSError instead of the WifiAuthError this branch
    # exists to produce. The device must pace the two apart.
    flush_ms = int(out.split("nack_flush_ms:")[1].split()[0])
    assert flush_ms >= 50, f"nack was not given time to flush before the disconnect: {flush_ms}ms"


@requires_micropython
def test_ble_boot_run_mode_serves_calls_and_keeps_loop_tasks_ticking():
    # The payoff test for _ble_make_streams' async reader. Run mode is the
    # one phase where the device has other work scheduled alongside the
    # connection (@mcu.loop tasks share the event loop the app runs in), so
    # the reader must *await* between inbound frames rather than busy-poll.
    # A busy-poll here would still answer calls correctly - it would only
    # silently starve every @mcu.loop function, which is exactly the kind
    # of failure no round-trip assertion alone would catch. Hence the
    # tick-count assertion.
    from ble_fakes import combine_boot_py_with_driver

    from tether.marshalling import encode_frame

    boot_py = generate_ble_boot(danger_unauthenticated=True)["/boot.py"].decode()

    sliced_source = """\
import mcu_decorators as _test_mod

if not hasattr(_test_mod, "_tick_total"):
    _test_mod._tick_total = 0


@mcu.loop(interval_ms=20)
def tick():
    _test_mod._tick_total += 1


@mcu.export
def get_tick_total() -> int:
    return _test_mod._tick_total
"""
    tether_app_source = generate_bootstrap(sliced_source, "")

    handshake = encode_frame(1, {"id": 1, "name": "__tether_handshake__", "args": []})
    # msg_type 1 == MSG_CALL (2 is MSG_RESULT - encoding a call as one
    # makes the dispatcher treat it as a reply and silently never answer).
    get_ticks = encode_frame(1, {"id": 2, "name": "get_tick_total", "args": []})

    driver = f"""
import bluetooth
import umsgpack

_ble = bluetooth.BLE()
_c = Central(_ble)
_c.connect()

print("run_ack:", _c.send_preamble("run", None))


def _read_result():
    _len = int.from_bytes(_c.recv_exact(4), "big")
    _body = _c.recv_exact(_len)
    return _body[:1], umsgpack.loads(_body[1:])


_c.write({handshake!r})
_type1, _payload1 = _read_result()
print("handshake_type:", _type1)
print("handshake_payload:", _payload1)

# Idle on a live run session. Nothing is inbound, so the device sits in
# _BleAsyncReader.readexactly the whole time - if that awaited nothing, the
# @mcu.loop task below would never get scheduled and the count stays 0.
time.sleep_ms(600)

_c.write({get_ticks!r})
_type2, _payload2 = _read_result()
print("ticks_type:", _type2)
print("ticks_while_idle:", _payload2["value"])
"""

    combined = combine_boot_py_with_driver(
        boot_py,
        driver,
        files={"/tether_ble.json": "{}", "/tether_app.py": tether_app_source},
    )
    out = run_micropython(combined, timeout=15.0)

    assert "BOOT THREAD DIED" not in out, out
    assert "run_ack: {'ok': True}" in out, out
    assert "handshake_type: b'\\x02'" in out, out  # MSG_RESULT
    assert f"handshake_payload: {{'id': 1, 'value': {PROTOCOL_VERSION}}}" in out, out
    assert "ticks_type: b'\\x02'" in out, out

    ticks = int(out.split("ticks_while_idle:")[1].split()[0])
    # ~30 expected at interval_ms=20 over 600ms; assert generously - the
    # point is "the loop ran at all while the reader waited", not the rate.
    assert ticks >= 5, f"@mcu.loop starved during a live BLE run session: {ticks} ticks\n{out}"


@requires_micropython
def test_ble_boot_run_mode_does_not_accumulate_loop_tasks_across_reconnects():
    # BLE's own version of the wifi headline regression test
    # (test_boot_py_run_mode_does_not_accumulate_loop_tasks_across_reconnects,
    # test_connection.py). The underlying fix (mcu_decorators._registrations
    # cleared per exec(), uasyncio.new_event_loop() reset in _handle_run's
    # finally - both in the shared _MODE_HANDLER_FUNCTIONS_SRC) is
    # transport-agnostic and already covered for wifi, but nothing
    # previously exercised it for BLE specifically - a real, named gap
    # noted (not fixed) in this feature's own final review. `_BleAsyncReader`
    # closes over the module-global `_queue` (not a per-connection object
    # the way wifi's per-socket StreamReader is), which the final review
    # flagged as a slightly different-shaped hazard: a stale reader task
    # surviving a reconnect wouldn't just inflate a counter, it would race
    # the live session's reader for `_queue.pop(0)` and silently steal
    # frames. Sampling `get_tick_total()` via a real RPC call after each
    # reconnect (not just comparing raw counters) means a corrupted/hung
    # session would show up as this test hanging or asserting garbage, not
    # just a wrong number - the same class of evidence a stale-reader race
    # would actually produce.
    from ble_fakes import combine_boot_py_with_driver

    from tether.marshalling import encode_frame

    boot_py = generate_ble_boot(danger_unauthenticated=True)["/boot.py"].decode()

    sliced_source = """\
import mcu_decorators as _test_mod

if not hasattr(_test_mod, "_tick_total"):
    _test_mod._tick_total = 0


@mcu.loop(interval_ms=20)
def tick():
    _test_mod._tick_total += 1


@mcu.export
def get_tick_total() -> int:
    return _test_mod._tick_total
"""
    tether_app_source = generate_bootstrap(sliced_source, "")

    def _session_driver(req_id: int) -> str:
        handshake = encode_frame(1, {"id": req_id, "name": "__tether_handshake__", "args": []})
        get_ticks = encode_frame(1, {"id": req_id + 1, "name": "get_tick_total", "args": []})
        return f"""
_c = Central(_ble)
_c.connect()
print("session ack:", _c.send_preamble("run", None))

def _read_result():
    _len = int.from_bytes(_c.recv_exact(4), "big")
    _body = _c.recv_exact(_len)
    return _body[:1], umsgpack.loads(_body[1:])

_c.write({handshake!r})
_t1, _p1 = _read_result()
print("session handshake:", _t1, _p1)

time.sleep_ms(300)

_c.write({get_ticks!r})
_t2, _p2 = _read_result()
print("session total:", _p2["value"])

_c.disconnect()
time.sleep_ms(300)  # let boot.py's own bounded disconnect-wait finish and re-advertise
"""

    driver = f"""
import bluetooth
import umsgpack

_ble = bluetooth.BLE()

{_session_driver(1)}
{_session_driver(3)}
{_session_driver(5)}
"""

    combined = combine_boot_py_with_driver(
        boot_py,
        driver,
        files={"/tether_ble.json": "{}", "/tether_app.py": tether_app_source},
    )
    out = run_micropython(combined, timeout=20.0)

    assert "BOOT THREAD DIED" not in out, out
    assert out.count("session ack: {'ok': True}") == 3, out

    totals = [
        int(line.split("session total:")[1].strip())
        for line in out.splitlines()
        if "session total:" in line
    ]
    assert len(totals) == 3, out

    first, second, third = totals
    delta1 = first
    delta2 = second - first
    delta3 = third - second

    # Sanity: ticks actually fired every session (not zero - a different
    # bug this test isn't about).
    assert delta1 > 0, totals
    assert delta2 > 0, totals
    assert delta3 > 0, totals

    # The real check: an accumulating-duplicate-task regression would show
    # delta2 ~= 2x delta1 and delta3 ~= 3x delta1 (matches the exact
    # signature the wifi headline bug produced: 14/29/44, i.e. deltas
    # roughly 1x/2x/3x). 1.6x separates "roughly constant plus scheduling
    # jitter" from "accumulating an extra duplicate task" without being so
    # tight that timing jitter alone could trip it - same tolerance wifi's
    # own regression test uses.
    assert delta2 < delta1 * 1.6, (
        f"BLE @mcu.loop tick count is accumulating across reconnects: "
        f"deltas were {delta1}, {delta2}, {delta3} (totals {totals})"
    )
    assert delta3 < delta1 * 1.6, (
        f"BLE @mcu.loop tick count is accumulating across reconnects: "
        f"deltas were {delta1}, {delta2}, {delta3} (totals {totals})"
    )


def test_generate_ble_boot_produces_boot_py_and_config():
    files = generate_ble_boot()

    assert set(files.keys()) == {"/boot.py", "/tether_ble.json"}
    assert isinstance(files["/boot.py"], bytes)
    assert isinstance(files["/tether_ble.json"], bytes)
    # BLE has no network to join, so unlike wifi's config the only thing
    # here is the shared secret.
    assert set(json.loads(files["/tether_ble.json"])) == {"secret"}


def test_generate_ble_boot_keeps_the_secret_out_of_boot_py():
    files = generate_ble_boot("hunter2")

    assert json.loads(files["/tether_ble.json"])["secret"] == "hunter2"
    # Same rationale as wifi: boot.py is a fixed template, so rotating the
    # secret is a config-file-only re-upload and the secret never lands in
    # anything that gets logged or diffed as "the script".
    assert b"hunter2" not in files["/boot.py"]


def test_generate_ble_boot_includes_a_secret_by_default():
    config = json.loads(generate_ble_boot()["/tether_ble.json"])

    assert isinstance(config["secret"], str)
    assert len(config["secret"]) >= 16


def test_generate_ble_boot_omits_secret_when_unauthenticated():
    config = json.loads(generate_ble_boot(danger_unauthenticated=True)["/tether_ble.json"])

    assert config == {}


def test_generate_ble_boot_two_calls_produce_different_secrets():
    first = json.loads(generate_ble_boot()["/tether_ble.json"])["secret"]
    second = json.loads(generate_ble_boot()["/tether_ble.json"])["secret"]

    assert first != second


def test_generate_ble_boot_falls_through_when_never_provisioned():
    boot_py = generate_ble_boot()["/boot.py"].decode()

    assert "tether_ble.json" in boot_py
    # Config absent -> the whole peripheral block is skipped and the board
    # behaves exactly as it did before it was ever provisioned.
    assert "except OSError" in boot_py


def test_ble_boot_uuids_match_the_pc_side_transport():
    # The template hardcodes these (it runs on the MCU, where
    # tether.transports.ble does not exist), so nothing but this test stops
    # the two halves from silently drifting apart - a mismatch looks
    # identical to "wrong device" from the client's side.
    from tether.transports.ble import NOTIFY_CHAR_UUID, SERVICE_UUID, WRITE_CHAR_UUID

    boot_py = generate_ble_boot()["/boot.py"].decode()

    assert SERVICE_UUID in boot_py
    assert WRITE_CHAR_UUID in boot_py
    assert NOTIFY_CHAR_UUID in boot_py


def test_ble_advertising_payload_is_a_legal_gap_payload():
    from tether.provisioning import _BLE_ADV_PAYLOAD

    # GAP caps a legacy advertising payload at 31 bytes; overflowing it
    # makes the board silently un-discoverable rather than erroring.
    assert len(_BLE_ADV_PAYLOAD) <= 31
    assert b"tether" in _BLE_ADV_PAYLOAD

    # Walk the length-prefixed AD structures - the payload must consume
    # exactly, with no trailing slack that would mean a bad length byte.
    offset, ad_types = 0, []
    while offset < len(_BLE_ADV_PAYLOAD):
        length = _BLE_ADV_PAYLOAD[offset]
        ad_types.append(_BLE_ADV_PAYLOAD[offset + 1])
        offset += 1 + length
    assert offset == len(_BLE_ADV_PAYLOAD)
    assert ad_types == [0x01, 0x09, 0x07]  # flags, complete local name, 128-bit UUIDs


@requires_micropython
def test_ble_boot_sets_gap_name_to_match_the_advertised_local_name():
    # Real-hardware finding: MicroPython's bluetooth module has its own
    # default gap_name ("MPY ESP32") independent of _BLE_ADV_PAYLOAD's
    # custom advertising local name - left unset, the standard Generic
    # Access GATT service's Device Name characteristic (which every BLE
    # peripheral exposes automatically) disagrees with the live
    # advertisement, and some OS Bluetooth stacks report one or the other
    # depending on what they've cached, not consistently the live ad.
    # Confirmed directly against real hardware: `bluetooth.BLE().config
    # ("gap_name")` returned b'MPY ESP32' before this fix, unprompted by
    # any code in this project.
    from ble_fakes import FAKE_BLUETOOTH_MODULE_SRC, FAKE_FS_SRC
    from mpy_runner import run_micropython

    boot_py = generate_ble_boot(danger_unauthenticated=True)["/boot.py"].decode()
    # Same "run setup only, stop before the infinite session loop" idea as
    # the setup-succeeds check elsewhere in this file - gap_name is set
    # once, early, well before the loop.
    idx = boot_py.index("    while True:\n        # Re-establish")
    setup_src = boot_py[:idx]

    script = f"""
{FAKE_BLUETOOTH_MODULE_SRC}

_files = {{"/tether_ble.json": "{{}}"}}

{FAKE_FS_SRC}

{setup_src}
    print("gap_name:", _ble.config("gap_name"))
"""
    out = run_micropython(script, timeout=8.0)
    assert "gap_name: tether" in out, out


@requires_micropython
def test_ble_boot_survives_a_slow_disconnect_and_serves_the_next_connection():
    # Review finding (Important 1). gap_disconnect() is asynchronous on
    # real hardware: it only *requests* a disconnect. Re-advertising before
    # the CENTRAL_DISCONNECT IRQ confirms the link is down makes NimBLE
    # raise OSError - and that call sits outside the per-session
    # try/except, so an unguarded raise there kills the whole accept loop
    # and the board is unreachable over BLE until a physical reset.
    #
    # The fake models this: gap_advertise() raises while a link is up, and
    # disconnect_delay_ms delivers the disconnect IRQ late, on a real
    # thread, exactly as hardware would.
    from ble_fakes import combine_boot_py_with_driver

    boot_py = generate_ble_boot("s3cr3t")["/boot.py"].decode()

    driver = """
import bluetooth

_ble = bluetooth.BLE()
_ble.disconnect_delay_ms = 300  # the IRQ lands well after gap_disconnect()

_c = Central(_ble)
_c.connect()
print("rejected:", _c.send_preamble("status", "wrong-secret"))

# The board must get itself back to advertising on its own.
_deadline = time.ticks_add(time.ticks_ms(), 3000)
while not _ble.advertising and time.ticks_diff(_deadline, time.ticks_ms()) > 0:
    time.sleep_ms(20)
print("readvertising:", _ble.advertising)
print("advertise_errors:", _ble.advertise_errors)

# A second, independent connection is only servable if the accept loop
# actually survived the first session's teardown.
_c2 = Central(_ble, conn_handle=1)
_c2.connect()
print("second_ack:", _c2.send_preamble("status", "s3cr3t"))
print("second_hash:", _c2.read_json_frame()["tether_app_hash"])
"""

    combined = combine_boot_py_with_driver(
        boot_py,
        driver,
        files={"/tether_ble.json": '{"secret": "s3cr3t"}', "/.tether_hash": "still-alive"},
    )
    out = run_micropython(combined, timeout=15.0)

    assert "BOOT THREAD DIED" not in out, out
    assert "rejected: {'ok': False, 'error': 'auth failed'}" in out, out
    assert "readvertising: True" in out, out
    # The device waited for the disconnect IRQ rather than racing it, so it
    # never even attempted an advertise that NimBLE would have rejected.
    assert "advertise_errors: 0" in out, out
    assert "second_ack: {'ok': True}" in out, out
    assert "second_hash: still-alive" in out, out


@requires_micropython
def test_ble_boot_reassembles_writes_that_all_land_before_the_read_handler():
    # Review finding (Important 2). _bt_irq runs when MicroPython's
    # scheduler gets round to it, so a fast central can land several ATT
    # writes before the device reads the first. gatts_set_buffer(...,
    # append=True) is MicroPython's documented mitigation; without it every
    # write but the last is destroyed unread and the frame is silently
    # corrupted. A tiny MTU makes even a short preamble span three writes.
    from ble_fakes import combine_boot_py_with_driver

    boot_py = generate_ble_boot("s3cr3t")["/boot.py"].decode()

    driver = """
import bluetooth

_ble = bluetooth.BLE()
_c = Central(_ble, mtu=23)  # BLE's pre-negotiation minimum: 20 usable bytes
_c.connect()

# Consume the device's one-per-connection nonce normally (not burst-written
# - the device sends it, this doesn't exercise the write-burst path) before
# hand-computing the response for the burst-written preamble below.
_nonce = _c_unhexlify(_c.read_json_frame()["nonce"])
_response = _c_hexlify(_c_hmac_sha256(b"s3cr3t", _nonce))

# Every chunk lands before the device's read handler runs even once.
_c.send_json_frame_burst({"mode": "status", "response": _response})
print("append_mode:", _ble._append[WRITE_VALUE_HANDLE])
print("ack:", _c.read_json_frame())
print("hash:", _c.read_json_frame()["tether_app_hash"])
"""

    combined = combine_boot_py_with_driver(
        boot_py,
        driver,
        files={"/tether_ble.json": '{"secret": "s3cr3t"}', "/.tether_hash": "burst-ok"},
    )
    out = run_micropython(combined, timeout=15.0)

    assert "BOOT THREAD DIED" not in out, out
    assert "append_mode: True" in out, out
    assert "ack: {'ok': True}" in out, out
    assert "hash: burst-ok" in out, out


@requires_micropython
def test_ble_boot_unknown_mode_nack_flushes_before_ending_the_session():
    # Final-review finding (F3), the second nack branch. Same hazard as the
    # auth-failure nack above: an unrecognized mode is answered with an
    # {"ok": False} frame the client is meant to read as a clean rejection,
    # which is lost if the link drops before the queued notification goes
    # out. This branch had no test of its own at all before.
    from ble_fakes import combine_boot_py_with_driver

    boot_py = generate_ble_boot(danger_unauthenticated=True)["/boot.py"].decode()

    driver = """
import bluetooth

_ble = bluetooth.BLE()
_c = Central(_ble)
_c.connect()

print("reply:", _c.send_preamble("teleport", None))
time.sleep_ms(300)
print("device_dropped_link:", _ble.disconnects)
print("nack_flush_ms:", time.ticks_diff(_ble.disconnect_ticks[0], _ble.notify_ticks[-1]))
"""

    combined = combine_boot_py_with_driver(boot_py, driver, files={"/tether_ble.json": "{}"})
    out = run_micropython(combined, timeout=10.0)

    assert "BOOT THREAD DIED" not in out, out
    assert "reply: {'ok': False, 'error': 'unknown mode'}" in out, out
    assert "device_dropped_link: [0]" in out, out
    flush_ms = int(out.split("nack_flush_ms:")[1].split()[0])
    assert flush_ms >= 50, f"nack was not given time to flush before the disconnect: {flush_ms}ms"


@requires_micropython
def test_ble_boot_survives_a_gap_disconnect_that_raises_a_non_oserror():
    # Final-review finding (F4). _end_session() runs OUTSIDE the per-session
    # try/except, so anything it lets escape kills the whole session loop and
    # the board is unreachable over BLE until a physical power cycle. The
    # concrete route: a scheduler-delivered CENTRAL_DISCONNECT IRQ landing
    # between _end_session's None-check and its gap_disconnect() call turns
    # that call into gap_disconnect(None), which raises TypeError - not
    # caught by the old `except OSError`.
    #
    # The fake raises that exact TypeError on the next gap_disconnect. The
    # link therefore stays up from the stack's point of view, so the central
    # drops it itself afterwards (the realistic recovery: the board failed to
    # hang up, the peer left anyway) and the board must get back to serving.
    from ble_fakes import combine_boot_py_with_driver

    boot_py = generate_ble_boot("s3cr3t")["/boot.py"].decode()

    driver = """
import bluetooth

_ble = bluetooth.BLE()
_c = Central(_ble)
_c.connect()
_ble.fail_next_disconnect = True  # the device's own hang-up will raise

print("rejected:", _c.send_preamble("status", "wrong-secret"))
time.sleep_ms(300)
_c.disconnect()  # the central goes away on its own

_deadline = time.ticks_add(time.ticks_ms(), 3000)
while not _ble.advertising and time.ticks_diff(_deadline, time.ticks_ms()) > 0:
    time.sleep_ms(20)
print("readvertising:", _ble.advertising)

_c2 = Central(_ble, conn_handle=1)
_c2.connect()
print("second_ack:", _c2.send_preamble("status", "s3cr3t"))
print("second_hash:", _c2.read_json_frame()["tether_app_hash"])
"""

    combined = combine_boot_py_with_driver(
        boot_py,
        driver,
        files={"/tether_ble.json": '{"secret": "s3cr3t"}', "/.tether_hash": "still-alive"},
    )
    out = run_micropython(combined, timeout=15.0)

    assert "BOOT THREAD DIED" not in out, out
    assert "rejected: {'ok': False, 'error': 'auth failed'}" in out, out
    assert "readvertising: True" in out, out
    assert "second_ack: {'ok': True}" in out, out
    assert "second_hash: still-alive" in out, out


@requires_micropython
def test_ble_boot_retries_a_transient_notify_failure_instead_of_dropping_the_session():
    # Final-review finding (F5). Real ESP32/NimBLE raises an ENOMEM-class
    # OSError from gatts_notify() when its mbuf pool is momentarily exhausted
    # by a burst of notifications - which a multi-chunk status payload or
    # upload result is. Without a retry, one transient failure aborts the
    # whole session (the exception escapes to the loop's silent
    # `except Exception: pass`) and the client just sees the link drop.
    from ble_fakes import combine_boot_py_with_driver

    boot_py = generate_ble_boot("s3cr3t")["/boot.py"].decode()

    driver = """
import bluetooth

_ble = bluetooth.BLE()
_c = Central(_ble)
_c.connect()
_ble.notify_failures = 2  # the next two notifications fail, then recover

print("ack:", _c.send_preamble("status", "s3cr3t"))
print("hash:", _c.read_json_frame()["tether_app_hash"])
print("notify_errors:", _ble.notify_errors)
"""

    combined = combine_boot_py_with_driver(
        boot_py,
        driver,
        files={"/tether_ble.json": '{"secret": "s3cr3t"}', "/.tether_hash": "survived-enomem"},
    )
    out = run_micropython(combined, timeout=15.0)

    assert "BOOT THREAD DIED" not in out, out
    assert "ack: {'ok': True}" in out, out
    assert "hash: survived-enomem" in out, out
    # The failures really happened - the test would pass vacuously if the
    # fake had never raised.
    assert "notify_errors: 2" in out, out


@requires_micropython
def test_ble_boot_translates_a_non_oserror_notify_failure_into_an_oserror():
    # Final-review finding (F10). The shared mode handlers
    # (_MODE_HANDLER_FUNCTIONS_SRC, byte-identical between wifi and BLE)
    # are written against a socket's contract: a send to a dead peer raises
    # OSError. BLE breaks that - gatts_notify against a conn handle the
    # stack has already invalidated raises TypeError, which their
    # `except OSError` clauses do not catch. Rather than widen a clause
    # wifi also depends on, _BleConn.send translates non-OSError notify
    # failures to OSError, so BLE presents the same contract wifi does.
    #
    # Observable here through _handle_upload, which reports a failed upload
    # over the same connection and swallows OSError from that report (there
    # is nothing useful left to do). With the translation it returns
    # normally and the session survives to serve the next preamble on the
    # SAME connection; without it, TypeError escapes into the session
    # loop's `except Exception: pass` and the link is dropped instead.
    from ble_fakes import combine_boot_py_with_driver

    boot_py = generate_ble_boot("s3cr3t")["/boot.py"].decode()

    driver = """
import bluetooth

_ble = bluetooth.BLE()
_c = Central(_ble)
_c.connect()

print("upload_ack:", _c.send_preamble("upload", "s3cr3t"))

# Every notification from here on raises TypeError, as it would against a
# handle the stack invalidated. The device is about to need one: its
# end-of-upload reply.
_ble.notify_type_error = True
_c.send_json_frame({"dirs": [], "files": [{"path": "/tether_app.py", "size": 5}]})
_c.send_bytes_frame(b"hello")
time.sleep_ms(300)
_ble.notify_type_error = False

# Only reachable if the failed reply left the session intact.
print("later_ack:", _c.send_preamble("status", "s3cr3t"))
print("later_hash:", _c.read_json_frame()["tether_app_hash"])
print("written_app:", _files["/tether_app.py"])
"""

    combined = combine_boot_py_with_driver(
        boot_py, driver, files={"/tether_ble.json": '{"secret": "s3cr3t"}'}
    )
    out = run_micropython(combined, timeout=15.0)

    assert "BOOT THREAD DIED" not in out, out
    assert "upload_ack: {'ok': True}" in out, out
    # The session lived on, over the same connection, after a notify failure
    # the shared handler was only ever written to tolerate as an OSError.
    assert "later_ack: {'ok': True}" in out, out
    # The upload itself really happened (and its hash sentinel is correctly
    # left absent, since the client never got a success reply).
    assert "written_app: b'hello'" in out, out
    assert "later_hash: None" in out, out
