import socket
import threading

import pytest

from tether.transports.wifi import WifiStream, connect


def test_wifi_stream_write_sends_bytes_and_read_receives_them():
    a, b = socket.socketpair()
    stream_a = WifiStream(a)
    stream_b = WifiStream(b)

    stream_a.write(b"hello")

    assert stream_b.read() == b"hello"


def test_wifi_stream_read_returns_empty_bytes_when_peer_closes():
    # Dispatcher._run_reader treats an empty read() as "transport closed"
    # (chunk 7's disconnect handling) - a real closed TCP socket's recv()
    # already returns b"" naturally, no special-casing needed here.
    a, b = socket.socketpair()
    stream_a = WifiStream(a)

    b.close()

    assert stream_a.read() == b""


class _ListeningServer:
    """A real TCP server bound to localhost on an OS-assigned port -
    accepts exactly one connection in a background thread, matching how a
    real already-running on-device listener would behave from the PC's
    point of view.
    """

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.accepted: socket.socket | None = None
        self._accepted_event = threading.Event()

    def accept_in_background(self) -> None:
        def _accept():
            conn, _addr = self._sock.accept()
            self.accepted = conn
            self._accepted_event.set()

        threading.Thread(target=_accept, daemon=True).start()

    def wait_for_accept(self, timeout: float = 2.0) -> socket.socket:
        if not self._accepted_event.wait(timeout=timeout):
            raise AssertionError("server never accepted a connection")
        return self.accepted


def test_connect_opens_a_real_tcp_connection_to_the_given_host_and_port():
    server = _ListeningServer()
    server.accept_in_background()

    client_stream = connect("127.0.0.1", server.port, timeout=2.0)

    server_conn = server.wait_for_accept()
    server_conn.sendall(b"from device")
    assert client_stream.read() == b"from device"


def test_connect_raises_when_nothing_is_listening_on_the_port():
    # A real closed port refuses the connection immediately - fail loud,
    # no silent retry (DESIGN.md § Disconnection philosophy extends to
    # connect-time failures too).
    unused_port = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    unused_port.bind(("127.0.0.1", 0))
    port = unused_port.getsockname()[1]
    unused_port.close()  # port is free again, guaranteed nothing listens on it

    with pytest.raises(OSError):
        connect("127.0.0.1", port, timeout=2.0)
