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

    def fake_write_files(ser, files, **kwargs):
        calls.append("write")
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
    assert calls == ["reset", "write", "reset"], (
        "provision-wifi must reset before write (known state) and "
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

    result = CliRunner().invoke(main, ["unprovision-wifi", "--port", "/dev/ttyUSB0"])

    assert result.exit_code == 0, result.output
    assert removed["path"] == "/tether_wifi.json"
    assert calls == ["reset", "remove"], (
        "unprovision-wifi must reset the board (known state) before removing the wifi config"
    )


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
