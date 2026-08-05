import asyncio
import hashlib
import hmac
import json
import threading

import pytest

from tether.errors import FrameAuthenticationError, WifiAuthError
from tether.marshalling.frame_auth import FrameAuthenticator
from tether.transports.ble import BLE_TAG_LENGTH, BleControlChannel, BleStream, connect

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


def test_write_raises_oserror_on_timeout_instead_of_hanging_forever():
    # A peripheral that stops acknowledging ATT writes mid-exchange (gone
    # out of range, wedged) must not hang this call - and, transitively,
    # whatever process called it - forever. Regression test: write() used
    # to have no timeout parameter at all, silently blocking on
    # Future.result() with no bound, unlike read()'s already-bounded
    # contract.
    class _HangingBleakClient(_FakeBleakClient):
        async def write_gatt_char(self, char_uuid: str, data: bytes, response: bool = True) -> None:
            await asyncio.sleep(999)

    client = _HangingBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)

    with pytest.raises(OSError, match="timed out"):
        stream.write(b"hello", timeout=0.2)


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

    channel.send_json_frame({"mode": "status", "response": None})

    body = b'{"mode": "status", "response": null}'
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


def _queue_frames(stream: BleStream, *frames: dict) -> None:
    """Feed one notification carrying every frame back-to-back - matches
    how a real device's nonce-then-ack sequence arrives (see
    test_control_channel_leftover_bytes_carry_into_the_next_frame for the
    same multi-frame-in-one-notification shape).
    """
    combined = b""
    for frame in frames:
        body = json.dumps(frame).encode()
        combined += len(body).to_bytes(4, "big") + body
    stream.on_notify(None, bytearray(combined))


def test_send_preamble_raises_wifi_auth_error_on_nack():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    # First send_preamble() call on a channel reads a nonce before sending
    # anything (see send_preamble's own docstring) - queue that, then the
    # device's nack, as two frames on the one connection.
    _queue_frames(
        stream,
        {"nonce": b"0123456789abcdef".hex()},
        {"ok": False, "error": "auth failed"},
    )

    with pytest.raises(WifiAuthError, match="auth failed"):
        channel.send_preamble("status", "wrong-secret")


def test_send_preamble_succeeds_on_ack():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    _queue_frames(stream, {"nonce": b"0123456789abcdef".hex()}, {"ok": True})

    channel.send_preamble("status", "right-secret")  # must not raise


def test_send_preamble_only_reads_a_nonce_on_the_first_call():
    # BLE reuses one connection across every mode (status -> upload -> run)
    # - unlike wifi's fresh-connection-per-mode model, only the FIRST
    # send_preamble() call on a channel should read/answer a nonce; later
    # calls on the same channel just send {mode} (see the class docstring
    # and _BLE_BOOT_TEMPLATE's matching _authenticated device-side state).
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    _queue_frames(stream, {"nonce": b"0123456789abcdef".hex()}, {"ok": True}, {"ok": True})

    channel.send_preamble("status", "right-secret")
    client.writes.clear()
    channel.send_preamble("upload", "right-secret")

    sent = json.loads(b"".join(client.writes)[4:])
    assert sent == {"mode": "upload"}


def test_send_preamble_returns_the_session_key_when_a_secret_is_given():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)
    nonce = b"0123456789abcdef"

    _queue_frames(stream, {"nonce": nonce.hex()}, {"ok": True})

    session_key = channel.send_preamble("status", "right-secret")

    assert (
        session_key
        == hmac.new(b"right-secret", b"tether-frame-key" + nonce, hashlib.sha256).digest()
    )


def test_send_preamble_returns_none_when_secret_is_none():
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    _queue_frames(stream, {"nonce": b"0123456789abcdef".hex()}, {"ok": True})

    session_key = channel.send_preamble("status", None)

    assert session_key is None


def test_send_preamble_returns_the_same_cached_session_key_across_mode_switches():
    # BLE reuses one connection across modes - only the FIRST send_preamble()
    # call derives a session key (it's the only call that reads a nonce at
    # all); every later call on the same channel must return that SAME key,
    # not None and not a freshly re-derived one.
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)

    _queue_frames(stream, {"nonce": b"0123456789abcdef".hex()}, {"ok": True}, {"ok": True})

    first = channel.send_preamble("status", "right-secret")
    second = channel.send_preamble("upload", "right-secret")

    assert first == second
    assert first is not None


def test_authenticated_json_frame_round_trips():
    client_a = _FakeBleakClient()
    stream_a = BleStream(client_a, _running_loop(), _WRITE_CHAR)
    channel_a = BleControlChannel(stream_a)
    client_b = _FakeBleakClient()
    stream_b = BleStream(client_b, _running_loop(), _WRITE_CHAR)
    channel_b = BleControlChannel(stream_b)

    key = b"shared-session-key"
    channel_a._send_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)
    channel_b._recv_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)

    channel_a.send_authenticated_json_frame({"hello": "world"})
    # BleStream.write() sent via client_a; feed the same bytes into
    # stream_b as if they'd arrived over the air, matching how every other
    # test_transport_ble.py test bridges two independent fake clients.
    stream_b.on_notify(None, bytearray(b"".join(client_a.writes)))

    assert channel_b.read_authenticated_json_frame() == {"hello": "world"}


def test_authenticated_bytes_frame_round_trips():
    client_a = _FakeBleakClient()
    stream_a = BleStream(client_a, _running_loop(), _WRITE_CHAR)
    channel_a = BleControlChannel(stream_a)
    client_b = _FakeBleakClient()
    stream_b = BleStream(client_b, _running_loop(), _WRITE_CHAR)
    channel_b = BleControlChannel(stream_b)

    key = b"shared-session-key"
    channel_a._send_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)
    channel_b._recv_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)

    channel_a.send_authenticated_bytes_frame(b"some file content")
    stream_b.on_notify(None, bytearray(b"".join(client_a.writes)))

    assert channel_b.read_authenticated_bytes_frame() == b"some file content"


def test_ble_stream_enable_authentication_uses_the_given_authenticator_pair():
    # enable_authentication must use the EXACT instances it's given, not
    # construct fresh ones - proven by pre-advancing send_auth's counter
    # before calling enable_authentication, then checking the wrapped
    # write's counter matches (i.e. picks up where it left off).
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    key = b"shared-session-key"
    send_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)
    recv_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)
    send_auth.wrap(b"already sent one frame via the control channel")  # counter -> 1

    stream.enable_authentication(send_auth, recv_auth)
    stream.write(b"first run-mode frame")

    envelope = b"".join(client.writes)
    verify_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)
    verify_auth.wrap(b"placeholder")  # advance verify_auth's own counter to 1 too
    # unwrap() checks the counter matches its OWN internal expectation, so
    # rebuild one at counter=1 to confirm the envelope really is counter 1,
    # not counter 0 (which would mean enable_authentication started fresh).
    assert verify_auth.unwrap(envelope[4:]) == b"first run-mode frame"


def test_ble_stream_read_raises_and_closes_on_a_tampered_frame():
    # Fail-loud policy (DESIGN.md's BLE row, frame_auth.py's own docstring):
    # a tag/counter mismatch must close the connection immediately, never
    # retry - mirrors WifiStream's already-shipped
    # test_wifi_stream_read_raises_and_closes_on_a_tampered_frame
    # (tests/test_transport_wifi.py). For run mode, this close() is the
    # ONLY thing that tears down the BLE link on tampering - Dispatcher's
    # reader thread never calls it itself.
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    key = b"shared-session-key"
    send_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)
    recv_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)
    stream.enable_authentication(send_auth, recv_auth)

    good_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)
    tampered = bytearray(good_auth.wrap(b"payload"))
    tampered[-1] ^= 0xFF
    stream.on_notify(None, tampered)

    with pytest.raises(FrameAuthenticationError):
        stream.read()

    assert client.disconnected is True


def test_control_channel_read_authenticated_json_frame_raises_and_closes_on_a_tampered_frame():
    # Same fail-loud requirement as above, but for the status/upload
    # control-channel path (BleControlChannel), which owns its own
    # authenticator pair independent of BleStream's (run mode only enables
    # authentication on the stream once control-channel work is done - see
    # enable_authentication's docstring). The channel doesn't own the
    # physical connection, so it must close via self._stream.close().
    client = _FakeBleakClient()
    stream = BleStream(client, _running_loop(), _WRITE_CHAR)
    channel = BleControlChannel(stream)
    key = b"shared-session-key"
    channel._recv_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)

    good_auth = FrameAuthenticator(key, BLE_TAG_LENGTH)
    inner = len(b"hello").to_bytes(4, "big") + b"hello"
    tampered = bytearray(good_auth.wrap(inner))
    tampered[-1] ^= 0xFF
    stream.on_notify(None, tampered)

    with pytest.raises(FrameAuthenticationError):
        channel.read_authenticated_json_frame()

    assert client.disconnected is True


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
