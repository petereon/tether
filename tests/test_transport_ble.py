import asyncio
import threading

import pytest

from tether.errors import WifiAuthError
from tether.transports.ble import BleControlChannel, BleStream, connect

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


def test_control_channel_reads_a_json_frame_split_across_multiple_notifications():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    body = b'{"ok": true, "ip": "10.0.0.5"}'
    header = len(body).to_bytes(4, "big")
    # Simulate the notification arriving in two separate chunks, neither
    # aligned to the frame boundary - exactly what a real MTU-chunked
    # notification stream can do.
    stream.on_notify(None, bytearray(header + body[:5]))
    stream.on_notify(None, bytearray(body[5:]))

    assert channel.read_json_frame() == {"ok": True, "ip": "10.0.0.5"}


def test_control_channel_leftover_bytes_carry_into_the_next_frame():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    first = b'{"a": 1}'
    second = b'{"b": 2}'
    # Both frames' bytes arrive in one single notification - the reader
    # must not discard the second frame's bytes while parsing the first.
    combined = len(first).to_bytes(4, "big") + first + len(second).to_bytes(4, "big") + second
    stream.on_notify(None, bytearray(combined))

    assert channel.read_json_frame() == {"a": 1}
    assert channel.read_json_frame() == {"b": 2}


def test_control_channel_send_json_frame_writes_length_prefixed_body():
    client = _FakeBleakClient(mtu_size=100)
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    channel.send_json_frame({"mode": "status", "secret": None})

    body = b'{"mode": "status", "secret": null}'
    assert b"".join(client.writes) == len(body).to_bytes(4, "big") + body


def test_control_channel_read_bytes_frame_returns_raw_bytes_not_json_decoded():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    payload = b"\x00\x01\xff not valid json"
    stream.on_notify(None, bytearray(len(payload).to_bytes(4, "big") + payload))

    assert channel.read_bytes_frame() == payload


def test_control_channel_read_json_frame_rejects_oversized_declared_length():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    stream.on_notify(None, bytearray((1 << 20).to_bytes(4, "big")))

    with pytest.raises(OSError, match="too large"):
        channel.read_json_frame()


def test_control_channel_read_json_frame_raises_on_closed_connection():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    stream.signal_closed()

    with pytest.raises(OSError, match="closed"):
        channel.read_json_frame()


def test_send_preamble_raises_wifi_auth_error_on_nack():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    nack = b'{"ok": false, "error": "auth failed"}'
    stream.on_notify(None, bytearray(len(nack).to_bytes(4, "big") + nack))

    with pytest.raises(WifiAuthError, match="auth failed"):
        channel.send_preamble("status", "wrong-secret")


def test_send_preamble_succeeds_on_ack():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    ack = b'{"ok": true}'
    stream.on_notify(None, bytearray(len(ack).to_bytes(4, "big") + ack))

    channel.send_preamble("status", "right-secret")  # must not raise


def test_stream_read_with_a_timeout_raises_oserror_not_queue_empty():
    # Final-review finding (F1). Callers above this module (BleControlChannel,
    # Dispatcher) are written against the OSError/empty-bytes contract the
    # serial and wifi streams already use - letting queue.Empty escape would
    # sail straight through every `except OSError` in the stack.
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)

    with pytest.raises(OSError, match="timed out"):
        stream.read(0.01)


def test_control_channel_read_times_out_instead_of_hanging_forever():
    # Final-review finding (F1). Every control-exchange read used to be an
    # unbounded queue.get(): a device that stayed connected but never
    # answered hung send_preamble/read_json_frame/read_bytes_frame forever -
    # the same hang wifi's own preamble ack read was already fixed for.
    import time

    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream, timeout=0.05)

    started = time.monotonic()
    with pytest.raises(OSError, match="timed out"):
        channel.read_json_frame()
    assert time.monotonic() - started < 2.0


def test_stream_read_without_a_timeout_still_blocks_for_the_dispatcher():
    # The other half of F1's fix: the bound belongs to the control channel,
    # not the stream. Once the control exchange is over the same stream
    # becomes the long-lived RPC stream, where "nothing for N seconds" is a
    # normal idle session and only an empty read means disconnected (the
    # contract Dispatcher._run_reader relies on).
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    BleControlChannel(stream, timeout=0.01)  # a bounded channel over it

    received: list[bytes] = []
    reader = threading.Thread(target=lambda: received.append(stream.read()), daemon=True)
    reader.start()
    reader.join(timeout=0.3)
    assert reader.is_alive(), "read() must keep blocking, not inherit the channel's timeout"

    stream.on_notify(None, bytearray(b"late-but-real"))
    reader.join(timeout=2.0)
    assert received == [b"late-but-real"]
