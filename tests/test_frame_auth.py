from __future__ import annotations

import pytest

from tether.errors import FrameAuthenticationError
from tether.marshalling.frame_auth import DEFAULT_TAG_LENGTH, FrameAuthenticator


def test_default_tag_length_is_16_bytes():
    assert DEFAULT_TAG_LENGTH == 16


def test_wrap_then_unwrap_round_trips_the_inner_frame():
    sender = FrameAuthenticator(b"a-32-byte-session-key-padded!!!!")
    receiver = FrameAuthenticator(b"a-32-byte-session-key-padded!!!!")

    envelope = sender.wrap(b"hello world")

    assert receiver.unwrap(envelope[4:]) == b"hello world"


def test_wrap_prefixes_a_4_byte_big_endian_outer_length():
    import struct

    sender = FrameAuthenticator(b"key", tag_length=16)
    envelope = sender.wrap(b"payload")

    (declared_length,) = struct.unpack(">I", envelope[:4])
    assert declared_length == len(envelope) - 4


def test_counter_increments_across_successive_wraps():
    sender = FrameAuthenticator(b"key")
    receiver = FrameAuthenticator(b"key")

    first = sender.wrap(b"frame-one")
    second = sender.wrap(b"frame-two")

    assert receiver.unwrap(first[4:]) == b"frame-one"
    assert receiver.unwrap(second[4:]) == b"frame-two"


def test_unwrap_rejects_a_tampered_body():
    sender = FrameAuthenticator(b"key")
    receiver = FrameAuthenticator(b"key")
    envelope = bytearray(sender.wrap(b"hello world"))
    envelope[-1] ^= 0xFF  # flip a bit inside the tag

    with pytest.raises(FrameAuthenticationError):
        receiver.unwrap(bytes(envelope[4:]))


def test_unwrap_rejects_a_replayed_frame():
    sender = FrameAuthenticator(b"key")
    receiver = FrameAuthenticator(b"key")
    envelope = sender.wrap(b"hello world")

    assert receiver.unwrap(envelope[4:]) == b"hello world"
    with pytest.raises(FrameAuthenticationError):
        receiver.unwrap(envelope[4:])  # replay - counter already consumed


def test_unwrap_rejects_an_out_of_order_frame():
    sender = FrameAuthenticator(b"key")
    receiver = FrameAuthenticator(b"key")
    _first = sender.wrap(b"frame-one")  # Advance sender counter; receiver never unwraps
    second = sender.wrap(b"frame-two")

    with pytest.raises(FrameAuthenticationError):
        receiver.unwrap(second[4:])  # counter 1 before counter 0


def test_unwrap_rejects_a_frame_authenticated_with_a_different_key():
    sender = FrameAuthenticator(b"key-a")
    receiver = FrameAuthenticator(b"key-b")
    envelope = sender.wrap(b"hello world")

    with pytest.raises(FrameAuthenticationError):
        receiver.unwrap(envelope[4:])


def test_tag_length_is_configurable():
    sender = FrameAuthenticator(b"key", tag_length=8)
    receiver = FrameAuthenticator(b"key", tag_length=8)
    envelope = sender.wrap(b"x")

    body = envelope[4:]
    # body = 4-byte counter + inner frame (b"x", 1 byte) + 8-byte tag
    assert len(body) == 4 + 1 + 8
    assert receiver.unwrap(body) == b"x"


def test_unwrap_rejects_a_body_too_short_to_contain_counter_and_tag():
    receiver = FrameAuthenticator(b"key", tag_length=16)

    with pytest.raises(FrameAuthenticationError):
        receiver.unwrap(b"short")


def test_wrap_rejects_counter_exhaustion():
    """Counter wraparound past 2^32 must raise FrameAuthenticationError."""
    sender = FrameAuthenticator(b"key")
    sender._counter = 2**32 + 1  # Force counter past the max

    with pytest.raises(FrameAuthenticationError, match="frame counter exhausted"):
        sender.wrap(b"payload")


def test_unwrap_rejects_counter_exhaustion():
    """Counter wraparound past 2^32 must raise FrameAuthenticationError before
    other checks (like too-short or tag mismatch).
    """
    receiver = FrameAuthenticator(b"key")
    receiver._counter = 2**32 + 1  # Force counter past the max

    with pytest.raises(FrameAuthenticationError, match="frame counter exhausted"):
        receiver.unwrap(b"short")
