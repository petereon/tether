import asyncio
import threading

import pytest

from tether.transports.ble import BleStream, connect

_WRITE_CHAR = "write-char-uuid"


class _FakeBleakClient:
    """Hand-written double matching bleak's real async API shape
    (BleakClient.write_gatt_char(char, data, response), .mtu_size,
    .start_notify(char, callback), .disconnect()) - there is no way to
    spin up a real local BLE peripheral to test against (bleak is
    client/central-only), so this fake stands in the same way
    _FakeMicroPythonSerial stands in for real hardware in
    test_serial_transport.py.
    """

    def __init__(self, mtu_size: int = 23):
        self.mtu_size = mtu_size
        self.writes: list[bytes] = []
        self.disconnected = False

    async def write_gatt_char(self, char_uuid: str, data: bytes, response: bool = True) -> None:
        assert char_uuid == _WRITE_CHAR
        self.writes.append(bytes(data))

    async def disconnect(self) -> None:
        self.disconnected = True


def _running_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop


def test_write_splits_a_payload_larger_than_the_mtu_into_multiple_gatt_writes():
    # ATT MTU 23 -> 3-byte header overhead -> 20 usable bytes per write.
    client = _FakeBleakClient(mtu_size=23)
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)

    stream.write(b"x" * 45)

    assert client.writes == [b"x" * 20, b"x" * 20, b"x" * 5]


def test_on_notify_delivers_the_bytes_through_read():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)

    stream.on_notify(None, bytearray(b"from-device"))

    assert stream.read() == b"from-device"


def test_signal_closed_delivers_an_empty_read_matching_transport_closed_contract():
    # Dispatcher._run_reader (dispatch/__init__.py) treats an empty read()
    # as "transport closed" - a BLE disconnect must surface through the
    # exact same signal, not a BLE-specific one.
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)

    stream.signal_closed()

    assert stream.read() == b""


def test_close_disconnects_the_underlying_client():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)

    stream.close()

    assert client.disconnected is True


def test_connect_fails_loud_when_bleak_is_not_installed():
    # This dev environment genuinely has no `bleak` installed (it's an
    # optional extra, tether[ble]) - connect() must fail with the
    # interpreter's own clear ModuleNotFoundError rather than hanging or
    # failing confusingly deeper in, matching serial.py's lazy-import
    # precedent for pyserial.
    with pytest.raises(ModuleNotFoundError, match="bleak"):
        connect("00:11:22:33:44:55", timeout=1.0)
