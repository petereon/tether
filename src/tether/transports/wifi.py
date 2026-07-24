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

import socket
from typing import Any

DEFAULT_PORT = 8765

_RECV_CHUNK = 65536


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


def connect(host: str, port: int = DEFAULT_PORT, *, timeout: float = 10.0) -> WifiStream:
    """Open a TCP connection to an already-listening on-device runtime."""
    sock = socket.create_connection((host, port), timeout=timeout)
    # This is a synchronous request/response RPC protocol sending small
    # msgpack frames one at a time (DESIGN.md § Wire protocol) - without
    # TCP_NODELAY, Nagle's algorithm can hold a small outgoing frame back
    # waiting to coalesce with more data that never comes, stalling on the
    # peer's delayed-ACK timer (classically ~40ms) on every single call.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    # create_connection's timeout only governs the connect() syscall itself
    # but persists as the socket's ongoing recv() timeout afterwards -
    # switched to blocking for the dispatch phase, matching
    # transports/serial.py's `ser.timeout = None` handoff: the background
    # reader thread's "empty read means disconnected" contract needs reads
    # that block for real data, not ones that wake on an idle timeout with
    # nothing to report.
    sock.settimeout(None)
    return WifiStream(sock)
