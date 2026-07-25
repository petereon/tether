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
