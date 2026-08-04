"""Per-frame HMAC authentication envelope. See DESIGN.md § Transports,
Wifi row, "Per-frame authentication" - wraps an already-framed blob of
bytes (an RPC frame from `encode_frame()`, or an existing control-channel
frame like `[4-byte length][JSON body]`) in an outer envelope providing
integrity, authenticity, and replay protection, without needing to
understand the inner format at all. Deliberately transport-agnostic (no
socket/I-O here) so wifi.py's integration and any future BLE integration
can both build on it - see DESIGN.md's "Future: BLE" note.

Envelope: [4-byte outer-length][4-byte counter][inner-frame bytes][tag].
`outer-length` covers everything after itself (counter + inner-frame +
tag). `tag = HMAC-SHA256(session_key, counter-bytes + inner-frame)[:tag_length]`
(16 bytes by default - 128-bit margin, standard truncation per RFC 2104 /
IPsec's HMAC-SHA-256-128; the full 32-byte tag buys nothing further for a
transport like wifi with no payload-budget pressure).

A `FrameAuthenticator` instance tracks ONE direction's monotonic counter
only (folded into the MAC input for replay protection) - a connection
needs two instances per side (one for its own outgoing counter, one for
the peer's incoming counter), both built from the same `session_key`. See
wifi.py's `WifiStream.enable_authentication()` for the pairing.
"""

from __future__ import annotations

import hashlib
import hmac
import struct

from tether.errors import FrameAuthenticationError

_OUTER_LENGTH = struct.Struct(">I")
_COUNTER = struct.Struct(">I")
_MAX_COUNTER = 2**32 - 1

DEFAULT_TAG_LENGTH = 16


class FrameAuthenticator:
    """One direction of a per-frame HMAC envelope. Pure/no I/O - callers
    read/write the actual bytes over whatever transport they have.
    """

    def __init__(self, session_key: bytes, tag_length: int = DEFAULT_TAG_LENGTH) -> None:
        self._session_key = session_key
        self._tag_length = tag_length
        self._counter = 0

    def wrap(self, inner_frame: bytes) -> bytes:
        """Build one complete outer envelope (including its own 4-byte
        length prefix) around `inner_frame`, an already-fully-formed frame
        in whatever format the caller uses (RPC or JSON/control).
        """
        if self._counter > _MAX_COUNTER:
            raise FrameAuthenticationError("frame counter exhausted - reconnect required")
        counter_bytes = _COUNTER.pack(self._counter)
        self._counter += 1
        tag = self._tag(counter_bytes, inner_frame)
        body = counter_bytes + inner_frame + tag
        return _OUTER_LENGTH.pack(len(body)) + body

    def unwrap(self, body: bytes) -> bytes:
        """`body` is everything after the outer length prefix (counter +
        inner-frame + tag), already read off the wire in full. Verifies
        the tag and the replay counter and returns the inner frame bytes.

        Raises FrameAuthenticationError on any mismatch - the caller MUST
        close the connection immediately after this (see DESIGN.md's
        fail-loud per-frame-auth policy), never retry on the same frame.
        """
        if self._counter > _MAX_COUNTER:
            raise FrameAuthenticationError("frame counter exhausted - reconnect required")
        if len(body) < _COUNTER.size + self._tag_length:
            raise FrameAuthenticationError("authenticated frame too short")
        counter_bytes = body[: _COUNTER.size]
        tag = body[-self._tag_length :]
        inner_frame = body[_COUNTER.size : -self._tag_length]
        (counter,) = _COUNTER.unpack(counter_bytes)
        if counter != self._counter:
            raise FrameAuthenticationError(
                f"unexpected frame counter: got {counter}, expected {self._counter}"
            )
        expected_tag = self._tag(counter_bytes, inner_frame)
        if not hmac.compare_digest(tag, expected_tag):
            raise FrameAuthenticationError("frame authentication tag mismatch")
        self._counter += 1
        return inner_frame

    def _tag(self, counter_bytes: bytes, inner_frame: bytes) -> bytes:
        mac = hmac.new(self._session_key, counter_bytes + inner_frame, hashlib.sha256).digest()
        return mac[: self._tag_length]
