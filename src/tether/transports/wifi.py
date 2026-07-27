"""Wifi transport: pure stdlib `socket`, no code push (board must already be
running a bootstrapped runtime — see DESIGN.md § Transports).

Scope note (chunk 12): this module is only the PC-side TCP client. DESIGN.md
says the board "must already be running a bootstrapped runtime" reachable
over wifi, but nowhere specifies how it gets there — there is no wifi
credential/on-device-listener story anywhere in the design, and inventing
one (new connect() kwargs, an on-device socket-server bootstrap variant,
a wire convention for it) would be new, unrequested API surface rather than
what this chunk actually asks for. Treated the same way real-hardware
validation is treated throughout this project: an explicitly flagged,
deliberate gap, not silently pretended away. See CHUNKS.md's entry for this
chunk.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any

DEFAULT_PORT = 8765

_RECV_CHUNK = 65536

_LENGTH_PREFIX = struct.Struct(">I")

# Same resource-safety bound as the RPC layer's MAX_FRAME_SIZE
# (tether/marshalling) - a declared length this large would risk buffering
# an unbounded amount of attacker/bug-controlled data before anything is
# validated, same risk shape regardless of what's inside the frame.
MAX_CONTROL_FRAME_SIZE = 1 << 16  # 64 KiB


class WifiStream:
    """Duplex stream wrapping a connected TCP socket, matching the plain
    read()/write() contract tether.dispatch.Dispatcher expects (see
    SerialStream in transports/serial.py for the same shape over a
    different transport). `.read()` blocks for at least one chunk of real
    data and returns b"" once the peer closes - exactly the "transport
    closed" signal Dispatcher._run_reader already relies on.
    """

    def __init__(self, sock: Any) -> None:
        self._sock = sock

    def read(self) -> bytes:
        return self._sock.recv(_RECV_CHUNK)

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        self._sock.close()


def _recv_exact(sock: Any, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("connection closed while reading a frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_json_frame(sock: Any, payload: dict[str, Any]) -> None:
    """Send `[4-byte length][utf-8 json body]`. Used for the wifi
    preamble/status/upload control channel - deliberately not the msgpack
    frame format the RPC layer (tether.marshalling) uses, since msgpack
    decoding depends on the vendored umsgpack.py, which may not exist yet
    on a board this control channel is partly responsible for provisioning
    in the first place (see the design spec's 2026-07-26 correction).
    """
    body = json.dumps(payload).encode("utf-8")
    sock.sendall(_LENGTH_PREFIX.pack(len(body)) + body)


def read_json_frame(sock: Any) -> dict[str, Any]:
    header = _recv_exact(sock, _LENGTH_PREFIX.size)
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAX_CONTROL_FRAME_SIZE:
        raise OSError(f"control frame too large: declared {length} bytes")
    body = _recv_exact(sock, length)
    return json.loads(body.decode("utf-8"))


def send_bytes_frame(sock: Any, data: bytes) -> None:
    """Send `[4-byte length][raw bytes]` - used for upload mode's file
    content, which is not JSON-wrapped (JSON can't carry arbitrary binary
    cleanly, and there's no need to make it).
    """
    sock.sendall(_LENGTH_PREFIX.pack(len(data)) + data)


def read_bytes_frame(sock: Any) -> bytes:
    header = _recv_exact(sock, _LENGTH_PREFIX.size)
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAX_CONTROL_FRAME_SIZE:
        raise OSError(f"control frame too large: declared {length} bytes")
    return _recv_exact(sock, length)


def send_preamble(sock: Any, mode: str, secret: str | None) -> None:
    """Send the connection preamble (mode + shared secret) and wait for the
    device's ack. Raises WifiAuthError if the device rejects it - every
    mode gets an explicit ack/nack before any mode-specific work begins,
    including `run` (one extra round trip, worth it so a bad secret always
    surfaces as a clear WifiAuthError rather than a confusing downstream
    failure specific to whichever mode was requested).
    """
    from tether.errors import WifiAuthError

    send_json_frame(sock, {"mode": mode, "secret": secret})
    response = read_json_frame(sock)
    if not response.get("ok", False):
        raise WifiAuthError(response.get("error") or "connection rejected by device")


def connect(
    host: str, port: int = DEFAULT_PORT, *, timeout: float = 10.0, switch_to_blocking: bool = True
) -> WifiStream:
    """Open a TCP connection to an already-listening on-device runtime.

    `switch_to_blocking`: when True (the default), the socket is switched
    to blocking (no timeout) before this returns - correct once the caller
    is about to hand the stream to the long-lived Dispatcher, matching
    transports/serial.py's `ser.timeout = None` handoff: the background
    reader thread's "empty read means disconnected" contract needs reads
    that block for real data, not ones that wake on an idle timeout with
    nothing to report.

    Pass False when the caller still has synchronous, bounded work to do
    on this socket before that handoff (e.g. wifi's run-mode preamble
    exchange - send_preamble()'s own blocking ack read must still respect
    `timeout`, not block forever on a device that accepts the connection
    but never acks) - the socket keeps `timeout` as its active timeout
    until the caller explicitly switches it to blocking itself (see
    connection.py's _connect_wifi dial(), which does exactly that,
    immediately before the stream is handed to _start_and_handshake).
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    # This is a synchronous request/response RPC protocol sending small
    # msgpack frames one at a time (DESIGN.md § Wire protocol) - without
    # TCP_NODELAY, Nagle's algorithm can hold a small outgoing frame back
    # waiting to coalesce with more data that never comes, stalling on the
    # peer's delayed-ACK timer (classically ~40ms) on every single call.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    # create_connection's timeout only governs the connect() syscall itself
    # but persists as the socket's ongoing recv() timeout afterwards.
    if switch_to_blocking:
        sock.settimeout(None)
    return WifiStream(sock)
