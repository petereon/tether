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
        send_json(sock, {"mode": "status", "secret": "the-real-secret"})
        results["ack"] = read_json(sock)
        results["payload"] = read_json(sock)
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
        send_json(sock3, {"mode": "status", "secret": None})
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
        send_json(sock, {"mode": "run", "secret": None})
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
        send_json(sock, {"mode": "upload", "secret": None})
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
        send_json(sock, {"mode": "upload", "secret": None})
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
