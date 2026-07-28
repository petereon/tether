import json

import click
import pytest
from click.testing import CliRunner

from tether.cli import _resolve_port, main


def test_resolve_port_returns_explicit_port_without_listing_devices(monkeypatch):
    def fail_list_devices(list_ports_fn=None, extra_vid_pid=None):
        raise AssertionError("list_devices should not be called when --port is given")

    monkeypatch.setattr("tether.transports.serial.list_devices", fail_list_devices)

    assert _resolve_port("/dev/ttyUSB5") == "/dev/ttyUSB5"


def test_resolve_port_raises_when_no_devices_found(monkeypatch):
    def fake_list_devices(list_ports_fn=None, extra_vid_pid=None):
        return []

    monkeypatch.setattr("tether.transports.serial.list_devices", fake_list_devices)

    with pytest.raises(click.ClickException) as exc_info:
        _resolve_port(None)

    assert "no known" in str(exc_info.value).lower()


def test_resolve_port_returns_single_device_without_prompting(monkeypatch):
    def fake_list_devices(list_ports_fn=None, extra_vid_pid=None):
        return ["/dev/ttyUSB0"]

    def fail_select(options):
        raise AssertionError("beaupy.select should not be called for a single device")

    monkeypatch.setattr("tether.transports.serial.list_devices", fake_list_devices)
    monkeypatch.setattr("beaupy.select", fail_select)

    assert _resolve_port(None) == "/dev/ttyUSB0"


def test_resolve_port_prompts_and_returns_selection_when_multiple_devices(monkeypatch):
    def fake_list_devices(list_ports_fn=None, extra_vid_pid=None):
        return ["/dev/ttyUSB0", "/dev/ttyUSB1"]

    def fake_select(options):
        assert options == ["/dev/ttyUSB0", "/dev/ttyUSB1"]
        return "/dev/ttyUSB1"

    monkeypatch.setattr("tether.transports.serial.list_devices", fake_list_devices)
    monkeypatch.setattr("beaupy.select", fake_select)

    assert _resolve_port(None) == "/dev/ttyUSB1"


def test_resolve_port_raises_when_prompt_selection_is_none(monkeypatch):
    def fake_list_devices(list_ports_fn=None, extra_vid_pid=None):
        return ["/dev/ttyUSB0", "/dev/ttyUSB1"]

    def fake_select(options):
        return None

    monkeypatch.setattr("tether.transports.serial.list_devices", fake_list_devices)
    monkeypatch.setattr("beaupy.select", fake_select)

    with pytest.raises(click.ClickException):
        _resolve_port(None)


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


def test_provision_wifi_uploads_boot_py_and_config(monkeypatch):
    written = {}
    calls = []

    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            self.port = port

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: calls.append("reset"))
    monkeypatch.setattr("tether.transports.serial.read_file", lambda ser, path, timeout=10.0: None)

    def fake_write_files(ser, files, **kwargs):
        calls.append("write")
        written.update(files)

    monkeypatch.setattr("tether.transports.serial.write_files", fake_write_files)

    result = CliRunner().invoke(
        main,
        [
            "provision",
            "wifi",
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
    assert calls == ["reset", "write", "reset"], (
        "provision wifi must reset before write (known state) and "
        "reset after write (board picks up new config)"
    )


def test_provision_wifi_prompts_for_password_when_omitted(monkeypatch):
    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)
    monkeypatch.setattr("tether.transports.serial.read_file", lambda ser, path, timeout=10.0: None)

    written = {}

    def fake_write_files(ser, files, **kwargs):
        written.update(files)

    monkeypatch.setattr("tether.transports.serial.write_files", fake_write_files)

    prompted = {}

    def fake_prompt(message, secure=False):
        prompted["secure"] = secure
        return "prompted-password"

    monkeypatch.setattr("beaupy.prompt", fake_prompt)

    result = CliRunner().invoke(
        main, ["provision", "wifi", "--port", "/dev/ttyUSB0", "--ssid", "MyNetwork"]
    )

    assert result.exit_code == 0, result.output
    assert prompted["secure"] is True
    # The prompted value must actually be the one written to the device -
    # not just that beaupy.prompt was called with the right arguments.
    assert b"prompted-password" in written["/tether_wifi.json"]


def test_provision_wifi_prints_the_generated_secret(monkeypatch):
    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: None)
    monkeypatch.setattr("tether.transports.serial.read_file", lambda ser, path, timeout=10.0: None)

    written = {}

    def fake_write_files(ser, files, **kwargs):
        written.update(files)

    monkeypatch.setattr("tether.transports.serial.write_files", fake_write_files)

    result = CliRunner().invoke(
        main,
        [
            "provision",
            "wifi",
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
    monkeypatch.setattr("tether.transports.serial.read_file", lambda ser, path, timeout=10.0: None)

    written = {}

    def fake_write_files(ser, files, **kwargs):
        written.update(files)

    monkeypatch.setattr("tether.transports.serial.write_files", fake_write_files)

    result = CliRunner().invoke(
        main,
        [
            "provision",
            "wifi",
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

    result = CliRunner().invoke(main, ["status", "--port", "/dev/ttyUSB0", "--ip", "192.168.1.50"])

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


def test_status_command_wifi_auth_failure_gives_clean_error_no_fallback(monkeypatch):
    # A wrong/missing shared secret must produce a clear, user-facing
    # click.ClickException - not an unhandled traceback, and not a silent
    # fallback to the raw-REPL path (that would reset a perfectly healthy
    # board just because the wrong secret was supplied).
    from tether.errors import WifiAuthError

    calls = []

    class _FakeSocket:
        def __init__(self, *a, **kw):
            pass

        def close(self):
            pass

    def fake_send_preamble(sock, mode, secret):
        raise WifiAuthError("auth failed")

    monkeypatch.setattr("socket.create_connection", lambda *a, **kw: _FakeSocket())
    monkeypatch.setattr("tether.transports.wifi.send_preamble", fake_send_preamble)
    monkeypatch.setattr(
        "tether.transports.serial.reset_board",
        lambda ser: calls.append(("reset_board",)),
    )

    result = CliRunner().invoke(main, ["status", "--port", "/dev/ttyUSB0", "--ip", "192.168.1.50"])

    assert result.exit_code != 0
    assert isinstance(result.exception, (SystemExit, click.ClickException))
    assert "192.168.1.50" in result.output
    assert "secret" in result.output.lower()
    assert ("reset_board",) not in calls


def test_status_command_reports_connected_with_ip(monkeypatch):
    calls = []

    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: calls.append("reset"))

    status = {"provisioned": True, "connected": True, "ip": "192.168.1.42"}

    def fake_run_python(ser, code, timeout=10.0):
        calls.append("run_python")
        return json.dumps(status).encode() + b"\n", b""

    monkeypatch.setattr("tether.transports.serial.run_python", fake_run_python)

    result = CliRunner().invoke(main, ["status", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert "192.168.1.42" in result.output
    assert calls == ["reset", "run_python"], (
        "status must reset the board (known state) before running the status check script"
    )


def test_unprovision_wifi_removes_config_after_confirmation(monkeypatch):
    calls = []

    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: calls.append("reset"))
    monkeypatch.setattr("beaupy.confirm", lambda question: True)

    removed = {}

    def fake_remove_file(ser, path, **kwargs):
        calls.append("remove")
        removed["path"] = path

    monkeypatch.setattr("tether.transports.serial.remove_file", fake_remove_file)

    result = CliRunner().invoke(main, ["unprovision", "wifi", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert removed["path"] == "/tether_wifi.json"
    assert calls == ["reset", "remove"], (
        "unprovision wifi must reset the board (known state) before removing the wifi config"
    )


def test_unprovision_wifi_does_nothing_without_confirmation(monkeypatch):
    monkeypatch.setattr("beaupy.confirm", lambda question: False)

    removed = {}
    monkeypatch.setattr(
        "tether.transports.serial.remove_file",
        lambda ser, path, **kw: removed.setdefault("called", True),
    )

    result = CliRunner().invoke(main, ["unprovision", "wifi", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert "called" not in removed
    assert "cancelled" in result.output.lower()


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


def _patch_fake_serial(monkeypatch):
    class _FakeSerial:
        def __init__(self, port, baudrate, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setattr("serial.Serial", _FakeSerial)


def test_provision_ble_generates_secret_and_prints_address(monkeypatch):
    _patch_fake_serial(monkeypatch)
    calls = []
    monkeypatch.setattr("tether.transports.serial.reset_board", lambda ser: calls.append("reset"))
    monkeypatch.setattr("tether.transports.serial.read_file", lambda ser, path, timeout=10.0: None)

    written = {}

    def fake_write_files(ser, files, **kwargs):
        calls.append("write")
        written.update(files)

    monkeypatch.setattr("tether.transports.serial.write_files", fake_write_files)

    def fake_run_python(ser, code, timeout=5.0):
        calls.append("run_python")
        return (b"aa:bb:cc:dd:ee:ff\n", b"")

    monkeypatch.setattr("tether.transports.serial.run_python", fake_run_python)

    result = CliRunner().invoke(main, ["provision", "ble", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert set(written.keys()) == {"/boot.py", "/tether_ble.json"}
    assert "Shared secret" in result.output
    assert "aa:bb:cc:dd:ee:ff" in result.output.lower()
    # Final-review finding (F2). run_python enters the raw REPL via a Ctrl-C
    # interrupt, which kills whatever boot.py is running - so the MAC read
    # must happen BEFORE the final reset, leaving reset_board the last
    # serial operation exactly as provision wifi does. Reversed (the
    # original order) the board is left advertising with nothing servicing
    # its BLE session loop, and every later connect attempt hangs until a
    # physical power cycle.
    assert calls == ["reset", "write", "run_python", "reset"], (
        "provision ble must reset before write (known state) and reset LAST, "
        "after every raw-REPL operation, so the new boot.py is left running"
    )


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

    result = CliRunner().invoke(main, ["provision", "ble", "--port", "/dev/ttyUSB0"])

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
        [
            "provision",
            "wifi",
            "--port",
            "/dev/ttyUSB0",
            "--ssid",
            "MyNetwork",
            "--password",
            "hunter2",
        ],
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
        main, ["provision", "ble", "--port", "/dev/ttyUSB0", "--danger-unauthenticated"]
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
        def __init__(self, stream, *, timeout=None):
            # Final-review finding (F1): status must bound its BLE reads -
            # a board that connects but never answers used to hang here.
            calls.append(("channel_timeout", timeout))

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
    timeouts = [call[1] for call in calls if call[0] == "channel_timeout"]
    assert timeouts and all(t is not None for t in timeouts), calls


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


def test_status_ble_addr_without_bleak_installed_fails_loud_without_resetting(monkeypatch):
    # Final-review finding (F9). `bleak` is an optional extra (tether[ble]).
    # Its ModuleNotFoundError used to be swallowed by the same
    # `except Exception` that means "device unreachable, fall back to
    # serial" - so a missing dependency silently took the destructive
    # raw-REPL path, hardware-resetting the board. Avoiding exactly that
    # reset is why the two-tier BLE/serial status design exists.
    calls = []

    def raise_missing_bleak(addr, timeout=5.0):
        raise ModuleNotFoundError("No module named 'bleak'")

    monkeypatch.setattr("tether.transports.ble.connect", raise_missing_bleak)
    _patch_fake_serial(monkeypatch)
    monkeypatch.setattr(
        "tether.transports.serial.reset_board", lambda ser: calls.append("reset_board")
    )
    monkeypatch.setattr(
        "tether.transports.serial.run_python",
        lambda ser, code, timeout=10.0: (
            b'{"provisioned": true, "connected": false, "ip": null}',
            b"",
        ),
    )

    result = CliRunner().invoke(
        main, ["status", "--port", "/dev/ttyUSB0", "--ble-addr", "AA:BB:CC:DD:EE:FF"]
    )

    assert result.exit_code != 0, result.output
    assert "tether[ble]" in result.output
    assert calls == [], "a missing optional dependency must never reset the board"
