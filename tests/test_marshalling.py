import pytest

from tether.marshalling import FrameDecoder, encode_frame


def test_roundtrip_single_frame():
    frame = encode_frame(msg_type=1, payload={"a": 1})

    decoder = FrameDecoder()
    decoder.feed(frame)
    frames = decoder.pull_frames()

    assert frames == [(1, {"a": 1})]


def test_frame_split_across_multiple_feeds_yields_no_frame_until_complete():
    frame = encode_frame(msg_type=2, payload={"b": 2})
    midpoint = len(frame) // 2

    decoder = FrameDecoder()
    decoder.feed(frame[:midpoint])
    assert decoder.pull_frames() == []

    decoder.feed(frame[midpoint:])
    assert decoder.pull_frames() == [(2, {"b": 2})]


def test_multiple_frames_in_one_feed_are_pulled_in_order():
    frame_a = encode_frame(msg_type=1, payload="first")
    frame_b = encode_frame(msg_type=1, payload="second")

    decoder = FrameDecoder()
    decoder.feed(frame_a + frame_b)

    assert decoder.pull_frames() == [(1, "first"), (1, "second")]


def test_complete_frame_plus_partial_next_frame_only_yields_complete_one():
    frame_a = encode_frame(msg_type=1, payload="whole")
    frame_b = encode_frame(msg_type=1, payload="partial")

    decoder = FrameDecoder()
    decoder.feed(frame_a + frame_b[:3])

    assert decoder.pull_frames() == [(1, "whole")]

    decoder.feed(frame_b[3:])
    assert decoder.pull_frames() == [(1, "partial")]


def test_roundtrips_full_v1_type_set_including_nested_containers():
    payload = {
        "int": 7,
        "float": 3.5,
        "bool": True,
        "str": "hello",
        "bytes": b"\x00\x01\xff",
        "list": [1, 2, 3],
        "nested": {"labels": ["a", "b"], "count": None},
    }

    decoder = FrameDecoder()
    decoder.feed(encode_frame(msg_type=9, payload=payload))

    assert decoder.pull_frames() == [(9, payload)]


def test_encode_frame_rejects_msg_type_outside_byte_range():
    with pytest.raises(ValueError):
        encode_frame(msg_type=256, payload=None)


def test_corrupt_frame_does_not_leave_buffer_unresizable_when_caller_holds_the_exception():
    import struct

    corrupt_body = b"\xc1\xc1\xc1"  # 0xc1 is reserved/never-used in msgpack: guaranteed invalid
    corrupt_frame = struct.pack(">I", 1 + len(corrupt_body)) + bytes([1]) + corrupt_body

    decoder = FrameDecoder()
    decoder.feed(corrupt_frame)

    caught = None
    try:
        decoder.pull_frames()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, mimics a real caller
        caught = exc

    assert caught is not None
    assert not isinstance(caught, BufferError)

    # The very next feed() (e.g. more bytes arriving off the transport after
    # the caller decided to keep going) must not explode with an unrelated
    # BufferError from a memoryview export the first (failed) call left
    # dangling via the still-referenced traceback.
    decoder.feed(encode_frame(msg_type=2, payload="ok"))


def test_oversized_length_prefix_raises_immediately_without_buffering():
    import struct

    # Claims a ~100 MiB frame while only sending 5 bytes. A real attacker or
    # corrupted transport could send this; the decoder must reject it as
    # soon as the length header is read, not silently wait/accumulate up to
    # that many bytes (unbounded memory growth).
    hostile_prefix = struct.pack(">I", 100 * 1024 * 1024) + b"\x01\x00\x00\x00"

    decoder = FrameDecoder()
    decoder.feed(hostile_prefix)

    with pytest.raises(ValueError, match="frame too large"):
        decoder.pull_frames()
