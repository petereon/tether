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

import hashlib
import hmac
import json
import socket
import struct
from typing import Any

from tether.errors import FrameAuthenticationError
from tether.marshalling.frame_auth import (
    DEFAULT_TAG_LENGTH,
    ENVELOPE_OVERHEAD,
    FrameAuthenticator,
)

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

    Call `enable_authentication()` once, right after a successful
    handshake and before handing the stream to Dispatcher, to switch every
    subsequent read()/write() to per-frame HMAC authentication (see
    tether.marshalling.frame_auth and DESIGN.md's Per-frame authentication
    note). Unauthenticated (--danger-unauthenticated boards) streams never
    call this and behave exactly as before.
    """

    def __init__(self, sock: Any) -> None:
        self._sock = sock
        self._send_auth: FrameAuthenticator | None = None
        self._recv_auth: FrameAuthenticator | None = None

    def enable_authentication(
        self, session_key: bytes, tag_length: int = DEFAULT_TAG_LENGTH
    ) -> None:
        self._send_auth = FrameAuthenticator(session_key, tag_length)
        self._recv_auth = FrameAuthenticator(session_key, tag_length)

    def read(self) -> bytes:
        if self._recv_auth is None:
            return self._sock.recv(_RECV_CHUNK)
        return self._read_authenticated_chunk()

    def _read_authenticated_chunk(self) -> bytes:
        # Reads and unwraps exactly one outer envelope, returning its inner
        # bytes. FrameDecoder (tether.marshalling) doesn't care about
        # read()'s chunk boundaries, only about the accumulated logical
        # byte stream, so returning one envelope's worth per call is fine.
        try:
            header = self._recv_exact_or_empty(_LENGTH_PREFIX.size)
            if not header:
                return b""
            (length,) = _LENGTH_PREFIX.unpack(header)
            # + ENVELOPE_OVERHEAD: `length` is the OUTER envelope's body,
            # which legitimately exceeds a max-size payload by the
            # counter/tag (and, on the control channel, the inner length
            # prefix). Bounding it at the bare payload limit would reject
            # a legal 64 KiB frame. See frame_auth.ENVELOPE_OVERHEAD.
            if length > MAX_CONTROL_FRAME_SIZE + ENVELOPE_OVERHEAD:
                raise OSError(f"authenticated frame too large: declared {length} bytes")
            body = self._recv_exact_or_empty(length)
            if not body:
                return b""
            assert self._recv_auth is not None
            return self._recv_auth.unwrap(body)
        except FrameAuthenticationError:
            self.close()
            raise

    def _recv_exact_or_empty(self, n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                return b""
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def write(self, data: bytes) -> None:
        if self._send_auth is None:
            self._sock.sendall(data)
        else:
            self._sock.sendall(self._send_auth.wrap(data))

    def close(self) -> None:
        # shutdown() before close() - not redundant. This process's own
        # Dispatcher reader thread is typically still blocked in recv() on
        # this exact socket when close() is called (no Dispatcher.stop()
        # exists yet - see connection.py's own comments on this), and
        # POSIX leaves close()-while-another-thread-is-blocked-on-the-same-
        # fd unspecified. Confirmed against real Linux (not just reasoned
        # about): close() alone leaves the peer never observing the
        # connection end at all - board.reconnect() over wifi hangs/times
        # out waiting for the OLD device-side connection to close, because
        # no FIN or RST ever reaches it, only on Linux (harmless on macOS,
        # where this was developed and had passed before). shutdown()
        # operates at the protocol level, is specified to affect every
        # thread with a blocking call on the socket, and reliably causes
        # the peer to see the connection end regardless of what else has
        # this fd open. OSError is possible if the socket is already
        # disconnected (e.g. the peer already closed it) - not an error
        # worth surfacing here, close() below still needs to run either way.
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
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


def _json_frame_bytes(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return _LENGTH_PREFIX.pack(len(body)) + body


def send_json_frame(sock: Any, payload: dict[str, Any]) -> None:
    """Send `[4-byte length][utf-8 json body]`. Used for the wifi
    preamble/status/upload control channel - deliberately not the msgpack
    frame format the RPC layer (tether.marshalling) uses, since msgpack
    decoding depends on the vendored umsgpack.py, which may not exist yet
    on a board this control channel is partly responsible for provisioning
    in the first place (see the design spec's 2026-07-26 correction).
    """
    sock.sendall(_json_frame_bytes(payload))


def read_json_frame(sock: Any) -> dict[str, Any]:
    header = _recv_exact(sock, _LENGTH_PREFIX.size)
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAX_CONTROL_FRAME_SIZE:
        raise OSError(f"control frame too large: declared {length} bytes")
    body = _recv_exact(sock, length)
    return json.loads(body.decode("utf-8"))


def _bytes_frame_bytes(data: bytes) -> bytes:
    return _LENGTH_PREFIX.pack(len(data)) + data


def send_bytes_frame(sock: Any, data: bytes) -> None:
    """Send `[4-byte length][raw bytes]` - used for upload mode's file
    content, which is not JSON-wrapped (JSON can't carry arbitrary binary
    cleanly, and there's no need to make it).
    """
    sock.sendall(_bytes_frame_bytes(data))


def read_bytes_frame(sock: Any) -> bytes:
    header = _recv_exact(sock, _LENGTH_PREFIX.size)
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAX_CONTROL_FRAME_SIZE:
        raise OSError(f"control frame too large: declared {length} bytes")
    return _recv_exact(sock, length)


def _read_authenticated_body(sock: Any) -> bytes:
    header = _recv_exact(sock, _LENGTH_PREFIX.size)
    (length,) = _LENGTH_PREFIX.unpack(header)
    # + ENVELOPE_OVERHEAD - see _read_authenticated_chunk's matching
    # comment: this bounds the outer envelope, not the payload inside it.
    if length > MAX_CONTROL_FRAME_SIZE + ENVELOPE_OVERHEAD:
        raise OSError(f"authenticated frame too large: declared {length} bytes")
    return _recv_exact(sock, length)


def send_authenticated_json_frame(sock: Any, authenticator: Any, payload: dict[str, Any]) -> None:
    """Like send_json_frame, but wrapped in a per-frame HMAC envelope (see
    tether.marshalling.frame_auth). Used for status/upload control-channel
    traffic once a session key exists - never for the handshake itself
    (send_preamble/send_json_frame/read_json_frame stay unauthenticated,
    since no session key exists until the handshake completes).
    """
    sock.sendall(authenticator.wrap(_json_frame_bytes(payload)))


def read_authenticated_json_frame(sock: Any, authenticator: Any) -> dict[str, Any]:
    inner = authenticator.unwrap(_read_authenticated_body(sock))
    (length,) = _LENGTH_PREFIX.unpack_from(inner, 0)
    body = inner[_LENGTH_PREFIX.size : _LENGTH_PREFIX.size + length]
    return json.loads(body.decode("utf-8"))


def send_authenticated_bytes_frame(sock: Any, authenticator: Any, data: bytes) -> None:
    sock.sendall(authenticator.wrap(_bytes_frame_bytes(data)))


def read_authenticated_bytes_frame(sock: Any, authenticator: Any) -> bytes:
    inner = authenticator.unwrap(_read_authenticated_body(sock))
    (length,) = _LENGTH_PREFIX.unpack_from(inner, 0)
    return inner[_LENGTH_PREFIX.size : _LENGTH_PREFIX.size + length]


def send_preamble(sock: Any, mode: str, secret: str | None) -> bytes | None:
    """Send the connection preamble (mode + a nonce-challenge response) and
    wait for the device's ack. Raises WifiAuthError if the device rejects
    it - every mode gets an explicit ack/nack before any mode-specific work
    begins, including `run` (one extra round trip, worth it so a bad secret
    always surfaces as a clear WifiAuthError rather than a confusing
    downstream failure specific to whichever mode was requested).

    The device sends a fresh nonce as the very first thing on every new
    connection (wifi never reuses a connection across modes - see
    connection.py's _connect_wifi), before this function sends anything.
    The plaintext shared secret itself never crosses the wire: `response`
    is HMAC-SHA256(secret, nonce), so a passive observer of the connection
    learns neither the secret nor a value that lets it replay onto a future
    (different-nonce) connection. `secret=None` (unauthenticated boards)
    sends `response: None` - the device only checks it when a secret is
    actually configured on its side (see _BOOT_PY_TEMPLATE).

    Returns the derived session key (see tether.marshalling.frame_auth) for
    per-frame authentication of everything sent after this handshake, or
    None when `secret` is None - matching the handshake's own "no secret,
    no check" behavior (see DESIGN.md's Per-frame authentication note).

    The session key is a SECOND, domain-separated HMAC over the same
    secret and nonce - `HMAC-SHA256(secret, b"tether-frame-key" + nonce)`,
    not the same computation as `response`. This separation is
    load-bearing, not stylistic: `response` is sent over the wire in the
    clear, so if the session key were the same HMAC's raw digest (as it
    originally was), any passive observer of the handshake could read
    `response`, hex-decode it, and hold the exact key needed to forge,
    tamper with, inject, or replay authenticated frames - defeating the
    entire point of per-frame authentication. Prefixing a distinct,
    constant label before the nonce makes the two HMAC inputs disjoint, so
    the wire-visible response reveals nothing about the session key.
    Costs one extra local hash, no extra round trip.
    """
    from tether.errors import WifiAuthError

    nonce_frame = read_json_frame(sock)
    nonce = bytes.fromhex(nonce_frame["nonce"])
    if secret is not None:
        response = hmac.new(secret.encode(), nonce, hashlib.sha256).hexdigest()
        session_key = hmac.new(
            secret.encode(), b"tether-frame-key" + nonce, hashlib.sha256
        ).digest()
    else:
        response = None
        session_key = None
    send_json_frame(sock, {"mode": mode, "response": response})
    ack = read_json_frame(sock)
    if not ack.get("ok", False):
        raise WifiAuthError(ack.get("error") or "connection rejected by device")
    return session_key


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
