"""msgpack framing + the v1 type contract. See DESIGN.md § Wire protocol.

Frame: [4-byte length][msg-type][msgpack body]. PC side uses the `msgpack`
package directly; the MCU-side counterpart is the vendored umsgpack.py in
tether_runtime, uploaded with every deploy.
"""

from __future__ import annotations

import struct
from typing import Any

import msgpack

_LENGTH_PREFIX = struct.Struct(">I")
_MSG_TYPE_SIZE = 1

# Bound on the msg-type+body length a length prefix may declare. Exists so a
# corrupted or hostile length field can't make FrameDecoder buffer unbounded
# bytes before anything is validated. Sized for the MCU side of this bound
# (tether_runtime/dispatch.MAX_FRAME_SIZE, which this mirrors), not this
# process's own memory budget: a stock ESP32-WROOM board has no PSRAM and
# only ~100-200KB typical free heap under MicroPython, so the old 1 MiB
# value let a single readexactly() attempt an allocation that would
# MemoryError well before reaching the nominal cap - reachable remotely now
# that an unauthenticated wifi transport exists (see provisioning.py). 64
# KiB comfortably fits a stock board's free heap with headroom for the rest
# of the runtime, while still leaving real room for legitimate payloads
# (small file contents, lists) - see MAX_FRAME_SIZE in
# tether_runtime/dispatch.py, which MUST be updated together with this one
# or the two sides of the wire protocol disagree on what's rejected.
MAX_FRAME_SIZE = 1 << 16  # 64 KiB


def encode_frame(msg_type: int, payload: Any) -> bytes:
    """Pack `payload` as msgpack and prepend [4-byte length][msg-type].

    `length` covers the msg-type byte plus the msgpack body, not itself.
    """
    body = msgpack.packb(payload, use_bin_type=True)
    return _LENGTH_PREFIX.pack(_MSG_TYPE_SIZE + len(body)) + bytes([msg_type]) + body


class FrameDecoder:
    """Incremental frame decoder. Feed raw bytes as they arrive from any
    transport (they won't respect frame boundaries); pull complete frames
    out whenever enough bytes have accumulated.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)

    def pull_frames(self) -> list[tuple[int, Any]]:
        # Parse against a zero-copy view and only trim the real buffer once,
        # after the loop — repeated del-slicing inside the loop would memmove
        # the remaining tail on every frame (O(n) per frame, so O(n*frames)
        # per call) on what's a hot path once a transport is live.
        frames: list[tuple[int, Any]] = []
        buffer_len = len(self._buffer)
        offset = 0

        # `view` (and any slice taken from it, e.g. `body`) must be released
        # before self._buffer can be resized — including on the exception
        # path. A caller that catches and holds a decode error (logging it,
        # retrying, ...) keeps this frame alive via the traceback, which
        # would otherwise leave the buffer permanently unresizable. The `with`
        # guarantees `view` releases on every exit path (normal or raised);
        # the outer `finally` then trims only after that release has
        # happened, and covers both paths too.
        try:
            with memoryview(self._buffer) as view:
                while buffer_len - offset >= _LENGTH_PREFIX.size:
                    (length,) = _LENGTH_PREFIX.unpack_from(view, offset)
                    if length > MAX_FRAME_SIZE:
                        raise ValueError(
                            f"frame too large: declared {length} bytes, max is {MAX_FRAME_SIZE}"
                        )
                    header_end = offset + _LENGTH_PREFIX.size
                    total_end = header_end + length
                    if buffer_len < total_end:
                        break

                    msg_type = view[header_end]
                    body = view[header_end + _MSG_TYPE_SIZE : total_end]
                    try:
                        payload = msgpack.unpackb(body, raw=False)
                    finally:
                        del body
                    frames.append((msg_type, payload))
                    offset = total_end
        finally:
            if offset:
                del self._buffer[:offset]

        return frames
