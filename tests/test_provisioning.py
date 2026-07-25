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

from tether.connection import PROTOCOL_VERSION, generate_bootstrap


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

{STATUS_SCRIPT.decode()}
"""
    stdout = run_micropython(script, timeout=10.0)
    info = json.loads(stdout.strip())

    assert info["connected"] is True
    assert info["ip"] == "10.0.0.5"


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

    assert client_result.get("msg_type") == b"\x02"  # MSG_RESULT
    assert client_result.get("payload") == {"id": 1, "value": PROTOCOL_VERSION}
