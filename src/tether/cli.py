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


if __name__ == "__main__":
    main()
