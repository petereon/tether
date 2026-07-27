"""`tether` CLI - device discovery and wifi provisioning.

Install via `tether[cli]`. Talks to boards over the same serial/raw-REPL
primitives connect() uses (transports/serial.py) - no separate upload
mechanism, just new content going through the already-tested path. See
docs/superpowers/specs/2026-07-25-wifi-upload-design.md for the design.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import click


@contextmanager
def _open_board(port: str) -> Iterator[Any]:
    """Open a serial connection to the board for the duration of the
    `with` block, translating connection and raw-REPL failures into a
    clean click.ClickException instead of a raw traceback - the common
    first-run failures (port busy, permission denied, board unplugged
    mid-operation, board that won't enter raw REPL) all had no CLI-level
    handling before this helper existed.
    """
    import serial as pyserial

    from tether.transports.serial import RawReplError

    try:
        ser = pyserial.Serial(port, baudrate=115200, timeout=1.0)
    except pyserial.SerialException as exc:
        raise click.ClickException(f"could not open {port}: {exc}") from None
    try:
        yield ser
    except (pyserial.SerialException, RawReplError) as exc:
        raise click.ClickException(f"communication with {port} failed: {exc}") from None
    finally:
        ser.close()


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


@main.command("unprovision-wifi")
@click.option("--port", default=None, help="Serial port (auto-detected if omitted).")
def unprovision_wifi_command(port: str | None) -> None:
    """Remove stored WiFi credentials from a board.

    Note: this only removes /tether_wifi.json - the auto-run boot.py
    itself is left in place (harmless without credentials: it does
    nothing and falls through to the idle REPL, same as a never-
    provisioned board). Re-run `provision-wifi` to provision again.
    """
    import beaupy

    from tether.transports import serial as serial_transport

    resolved_port = _resolve_port(port)
    if not beaupy.confirm(f"Remove wifi credentials from {resolved_port}?"):
        click.echo("Cancelled.")
        return

    with _open_board(resolved_port) as ser:
        serial_transport.reset_board(ser)
        serial_transport.remove_file(ser, "/tether_wifi.json")

    click.echo(f"Removed wifi credentials from {resolved_port}.")


if __name__ == "__main__":
    main()
