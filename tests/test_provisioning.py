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


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mpy_runner import requires_micropython, run_micropython

from tether.connection import PROTOCOL_VERSION


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
