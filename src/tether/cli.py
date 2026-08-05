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


def _check_other_transport_provisioned(ser: Any, other_config_path: str, other_name: str) -> None:
    """Print a warning (does not block) if `other_config_path` already
    exists on the board - provisioning this transport will overwrite the
    board's single boot.py slot, silently orphaning the other transport's
    credentials file. Matches --danger-unauthenticated's non-blocking
    precedent - scripted provisioning shouldn't hang on a prompt.
    """
    from tether.transports import serial as serial_transport

    existing = serial_transport.read_file(ser, other_config_path, timeout=5.0)
    if existing is not None:
        click.echo(
            f"WARNING: {other_name} is currently provisioned on this board - "
            f"this will overwrite its boot.py and disable {other_name} connectivity "
            f"(its credentials file is left in place but nothing will read it anymore)."
        )


@main.group("provision")
def provision_group() -> None:
    """Upload a boot.py that makes the board reachable over wifi or BLE."""


@provision_group.command("wifi")
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
        _check_other_transport_provisioned(ser, "/tether_ble.json", "BLE")
        serial_transport.write_files(ser, files)
        serial_transport.reset_board(ser)

    click.echo(f"Provisioned {resolved_port} for wifi network {ssid!r}. Board is restarting.")
    if not danger_unauthenticated:
        import json

        config = json.loads(files["/tether_wifi.json"])
        click.echo(f"Shared secret (save this - needed to connect): {config['secret']}")
    click.echo("Run `tether status` in a few seconds to check connectivity.")


def _find_mac_local_ble_address(timeout: float = 8.0) -> str | None:
    """Best-effort: scan for the just-provisioned board by its advertised
    name and return whatever address THIS machine's Bluetooth stack
    actually needs to connect to it - on macOS, CoreBluetooth hides real
    BLE MAC addresses from apps for privacy and hands out a randomized
    per-app UUID instead (see DESIGN.md's BLE row), so the MAC this
    command already read straight from the board over serial is unusable
    for `mcu.connect("ble:<addr>")` there. Making the user run their own
    throwaway scan script to translate one into the other is a real DX
    gap - found by watching a real first run hit exactly this. Requires
    the optional `ble` extra; returns None (never raises) if `bleak`
    isn't installed, the scan times out, or the board isn't found within
    it - `--ble-addr <address>` and `provision ble`'s existing MAC-based
    output/macOS note are both still correct fallbacks either way, this
    is a bonus autodetection on top of them, not a replacement.
    """
    try:
        import asyncio

        import bleak

        from tether.provisioning import _BLE_ADV_NAME
    except ImportError:
        return None

    async def _scan() -> str | None:
        devices = await bleak.BleakScanner.discover(timeout=timeout)
        for device in devices:
            if device.name == _BLE_ADV_NAME:
                return device.address
        return None

    try:
        return asyncio.run(_scan())
    except Exception:  # noqa: BLE001 - any scan failure (permissions, adapter
        # off, timeout) means "couldn't autodetect," not a bug to surface -
        # the caller already has a correct fallback.
        return None


@provision_group.command("ble")
@click.option("--port", default=None, help="Serial port (auto-detected if omitted).")
@click.option(
    "--danger-unauthenticated",
    is_flag=True,
    default=False,
    help="Skip generating a shared secret - the BLE listener accepts any connection.",
)
def provision_ble_command(port: str | None, danger_unauthenticated: bool) -> None:
    """Upload a boot.py that advertises tether's BLE service and makes
    the board reachable over tether's BLE transport on every boot.

    Note: this uploads boot.py + BLE config only - the program that
    actually runs when a BLE client connects (tether_app.py) is uploaded
    separately, over BLE itself, the first time `mcu.connect("ble:<addr>")`
    is called (or over a normal serial `mcu.connect(...)` session, which
    still works too).
    """
    from tether import provisioning
    from tether.transports import serial as serial_transport

    resolved_port = _resolve_port(port)

    if danger_unauthenticated:
        click.echo(
            "WARNING: --danger-unauthenticated - this board's BLE listener will "
            "accept connections from anyone in range, no secret required."
        )

    files = provisioning.generate_ble_boot(danger_unauthenticated=danger_unauthenticated)

    with _open_board(resolved_port) as ser:
        serial_transport.reset_board(ser)
        _check_other_transport_provisioned(ser, "/tether_wifi.json", "wifi")
        serial_transport.write_files(ser, files)
        # Read the MAC back BEFORE the final reset, not after: run_python
        # enters the raw REPL via a Ctrl-C interrupt, which would kill a
        # just-started BLE session loop outright (KeyboardInterrupt is a
        # BaseException in MicroPython, so the loop's own `except
        # Exception` never sees it) and leave the board advertising with
        # nothing servicing it. The MAC is a hardware property, unaffected
        # by which boot.py is running, so reading it here costs nothing -
        # and this keeps reset_board() the LAST serial operation, exactly
        # as provision wifi does.
        addr_stdout, _ = serial_transport.run_python(
            ser,
            b"import bluetooth\nb=bluetooth.BLE()\nb.active(True)\n"
            b'print(":".join("{:02x}".format(x) for x in b.config("mac")[1]))\n',
            timeout=5.0,
        )
        serial_transport.reset_board(ser)

    mac = addr_stdout.decode().strip()
    click.echo(f"Provisioned {resolved_port} for BLE. Board is restarting.")
    click.echo(f"BLE address: {mac}")
    if not danger_unauthenticated:
        import json

        config = json.loads(files["/tether_ble.json"])
        click.echo(f"Shared secret (save this - needed to connect): {config['secret']}")

    # Best-effort autodetection of the address THIS machine actually needs
    # (see _find_mac_local_ble_address's own docstring for why the MAC
    # above isn't always it) - silently skipped, never adding noise, if
    # bleak isn't installed or the scan just doesn't find anything; the
    # MAC above and the macOS note remain correct either way.
    local_addr = _find_mac_local_ble_address()
    if local_addr is not None and local_addr.lower() != mac.lower():
        click.echo(
            f"On this machine, connect with this address instead: {local_addr} "
            "(not the MAC above - see the macOS note in DESIGN.md's Transports table)"
        )

    click.echo("Run `tether status --ble-addr <address>` in a few seconds to check connectivity.")


@main.command("status")
@click.option("--port", default=None, help="Serial port (auto-detected if omitted).")
@click.option(
    "--ip",
    default=None,
    help="Device IP (if known) - tried first, over wifi, before falling back to serial.",
)
@click.option("--secret", default=None, help="Shared secret, if the device requires one.")
@click.option(
    "--ble-addr",
    default=None,
    help="Device BLE address (if known) - tried first, over BLE, before falling back to serial.",
)
@click.option("--ble-secret", default=None, help="BLE shared secret, if the device requires one.")
def status_command(
    port: str | None,
    ip: str | None,
    secret: str | None,
    ble_addr: str | None,
    ble_secret: str | None,
) -> None:
    """Check whether a board is wifi- or BLE-provisioned and currently connected.

    Tries a direct, non-destructive query first if --ble-addr or --ip is
    given (no reset, no interruption) - BLE is tried first if both are
    given. Falls back to the raw-REPL diagnostic only if neither socket/BLE
    connection is reachable - meaning the transport never came up in the
    first place, not that the board is merely busy.

    The raw-REPL fallback resets the board TWICE, not once: entering raw
    REPL to run STATUS_SCRIPT necessarily interrupts whatever was already
    running - including, on a wifi/BLE-provisioned board, boot.py's own
    accept-loop, which does not resume on its own once interrupted (a
    plain Ctrl-C stops the program; nothing about entering/leaving raw
    REPL re-runs boot.py). Without the second reset, a bare `tether
    status` would silently and permanently kill a working wifi/BLE
    listener - a real, previously-unfixed trap: `nc`/`ping` keep
    "succeeding" against the board afterward regardless (TCP's listen
    backlog and ICMP echo don't require the interrupted application to
    still be running), which is exactly what made this hard to diagnose.
    The second reset restores the board to the same live-and-listening
    state this command found it in, so `tether status` is genuinely
    non-destructive from the caller's point of view even on this slower
    path - it just costs an extra reset/reconnect cycle to get there.
    Verified against real ESP32 hardware: a wifi listener survives a bare
    `tether status` call (and several in a row) with no manual recovery
    needed, whereas before this fix the same sequence permanently killed
    it. That reconnect cycle isn't instant, though - a wifi/BLE connection
    attempted immediately after this command returns can hit a brief
    window (observed: several seconds) where the board is still rejoining
    the network, same as right after any boot; mcu.connect()'s own
    timeout/retry is the right way to ride that out, not a fixed sleep
    here.
    """
    import json
    import os
    import socket

    from tether import provisioning
    from tether.errors import WifiAuthError
    from tether.transports import serial as serial_transport
    from tether.transports import wifi as wifi_transport

    if ble_addr:
        from tether.transports import ble as ble_transport

        resolved_ble_secret = (
            ble_secret if ble_secret is not None else os.environ.get("TETHER_BLE_SECRET")
        )
        payload = None
        stream = None
        # Bounds both the connect attempt and every read of the status
        # exchange below - a board that is connected but silent must not
        # hang this command forever (matches the wifi branch's own 3s
        # create_connection timeout just below).
        ble_timeout = 5.0
        try:
            stream = ble_transport.connect(ble_addr, timeout=ble_timeout)
        except ModuleNotFoundError as exc:
            # `bleak` is an optional extra. Letting this fall into the
            # general handler below would silently drop through to the
            # raw-REPL fallback, which RESETS the board - the exact
            # destructive behaviour --ble-addr exists to avoid. A missing
            # dependency is a user setup problem, not an unreachable
            # device.
            raise click.ClickException(
                f"--ble-addr needs the optional BLE dependency: {exc}. "
                "Install it with `pip install 'tether[ble]'`."
            ) from None
        except Exception:  # noqa: BLE001 - bleak/connect() can raise a range of
            # exception types (BleakError, OSError, TimeoutError, ...) for an
            # unreachable/absent device; any of them means "fall back to
            # serial", not a bug to surface.
            stream = None
        if stream is not None:
            from tether.connection import _hint_if_frame_auth_failure
            from tether.errors import FrameAuthenticationError

            try:
                try:
                    channel = ble_transport.BleControlChannel(stream, timeout=ble_timeout)
                    session_key = channel.send_preamble("status", resolved_ble_secret)
                    if session_key is None:
                        payload = channel.read_json_frame()
                    else:
                        payload = channel.read_authenticated_json_frame()
                except WifiAuthError:
                    raise click.ClickException(
                        f"BLE auth failed for {ble_addr} - check --ble-secret/TETHER_BLE_SECRET"
                    ) from None
                except FrameAuthenticationError as exc:
                    raise click.ClickException(str(_hint_if_frame_auth_failure(exc))) from None
                except OSError:
                    # device became unreachable mid-exchange - fall through
                    # to the raw-REPL fallback below, same as a failed
                    # connect.
                    payload = None
            finally:
                stream.close()
            if payload is not None:
                click.echo(f"Provisioned and connected. Address: {payload.get('ip')}")
                return

    if ip:
        from tether.connection import _hint_if_frame_auth_failure
        from tether.errors import FrameAuthenticationError
        from tether.marshalling.frame_auth import FrameAuthenticator

        resolved_secret = secret if secret is not None else os.environ.get("TETHER_WIFI_SECRET")
        payload = None
        try:
            sock = socket.create_connection((ip, wifi_transport.DEFAULT_PORT), timeout=3.0)
        except OSError:
            sock = None
        if sock is not None:
            try:
                try:
                    session_key = wifi_transport.send_preamble(sock, "status", resolved_secret)
                    if session_key is None:
                        payload = wifi_transport.read_json_frame(sock)
                    else:
                        payload = wifi_transport.read_authenticated_json_frame(
                            sock, FrameAuthenticator(session_key)
                        )
                except WifiAuthError:
                    raise click.ClickException(
                        f"wifi auth failed for {ip} - check --secret/TETHER_WIFI_SECRET"
                    ) from None
                except FrameAuthenticationError as exc:
                    raise click.ClickException(str(_hint_if_frame_auth_failure(exc))) from None
                except OSError:
                    # device became unreachable mid-exchange - fall through
                    # to the raw-REPL fallback below, same as a failed
                    # connect.
                    payload = None
            finally:
                sock.close()
            if payload is not None:
                click.echo(f"Provisioned and connected. IP: {payload['ip']}")
                return

    resolved_port = _resolve_port(port)
    with _open_board(resolved_port) as ser:
        serial_transport.reset_board(ser)
        try:
            # timeout=10.0 must stay comfortably above STATUS_SCRIPT's own
            # internal 8s wifi-connect poll (provisioning.py) - it also
            # has to cover the raw-REPL round trip on top of that wait.
            stdout, stderr = serial_transport.run_python(
                ser, provisioning.STATUS_SCRIPT, timeout=10.0
            )
        finally:
            # Restore whatever was running before this command interrupted
            # it (most importantly, a wifi/BLE boot.py's accept-loop) -
            # see this function's own docstring. Runs even if run_python
            # itself raised, so a status check that fails partway still
            # leaves the board in its normal running state rather than
            # stuck at an idle REPL prompt.
            serial_transport.reset_board(ser)

    if stderr:
        raise click.ClickException(f"status check failed: {stderr.decode(errors='replace')}")

    try:
        info = json.loads(stdout.decode(errors="replace").strip())
        provisioned, connected, ip_from_serial = info["provisioned"], info["connected"], info["ip"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise click.ClickException(f"could not parse status response: {exc}") from None

    if not provisioned:
        click.echo("Not provisioned for wifi. Run `tether provision wifi` first.")
    elif connected:
        click.echo(f"Provisioned and connected. IP: {ip_from_serial}")
    else:
        click.echo("Provisioned but not currently connected to wifi.")


@main.command("unprovision")
@click.option("--port", default=None, help="Serial port (auto-detected if omitted).")
def unprovision_command(port: str | None) -> None:
    """Remove all stored wifi/BLE credentials from a board.

    Removes both /tether_wifi.json and /tether_ble.json, whichever are
    actually present - a board only ever runs one transport's boot.py at
    a time (see `provision wifi`/`provision ble`'s own conflict warning),
    so there is no reason to unprovision one transport but not the
    other. The uploaded boot.py itself is left in place either way
    (harmless without credentials: it does nothing and falls through to
    the idle REPL, same as a never-provisioned board). Re-run
    `provision wifi`/`provision ble` to provision again.
    """
    import beaupy

    from tether.transports import serial as serial_transport

    resolved_port = _resolve_port(port)

    with _open_board(resolved_port) as ser:
        serial_transport.reset_board(ser)
        has_wifi = serial_transport.read_file(ser, "/tether_wifi.json", timeout=5.0) is not None
        has_ble = serial_transport.read_file(ser, "/tether_ble.json", timeout=5.0) is not None

        if not has_wifi and not has_ble:
            click.echo(f"{resolved_port} has no stored wifi or BLE credentials.")
            return

        present = [name for name, found in (("wifi", has_wifi), ("BLE", has_ble)) if found]
        if not beaupy.confirm(f"Remove {' and '.join(present)} credentials from {resolved_port}?"):
            # Nothing changed on-device, but the reset_board() above already
            # interrupted whatever was running (a wifi/BLE boot.py's
            # accept-loop, if either transport is actually provisioned -
            # see status_command's docstring for the same trap) - restore
            # it before reporting "cancelled", so a cancelled unprovision
            # is a genuine no-op, not a silent way to kill a working
            # listener.
            serial_transport.reset_board(ser)
            click.echo("Cancelled.")
            return

        if has_wifi:
            serial_transport.remove_file(ser, "/tether_wifi.json")
        if has_ble:
            serial_transport.remove_file(ser, "/tether_ble.json")

    click.echo(f"Removed {' and '.join(present)} credentials from {resolved_port}.")


if __name__ == "__main__":
    main()
