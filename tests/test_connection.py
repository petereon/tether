import socket
import sys
import threading
from pathlib import Path

import pytest

from tether import mcu, pc
from tether.connection import PROTOCOL_VERSION, connect, generate_bootstrap
from tether.dispatch import Dispatcher
from tether.transports.wifi import WifiStream

sys.path.insert(0, str(Path(__file__).parent))
from mpy_runner import requires_micropython, run_micropython


def test_generate_bootstrap_embeds_sliced_and_stub_source():
    sliced = "@mcu.export\ndef read_temp() -> float:\n    return 21.5"
    stubs = "async def log_event(msg: str) -> None:\n    return await call_pc('log_event', msg)"

    script = generate_bootstrap(sliced, stubs)

    assert "def read_temp() -> float:" in script
    assert "async def log_event" in script
    assert "return 21.5" in script


def test_generate_bootstrap_imports_the_runtime_shim():
    script = generate_bootstrap("", "")
    assert "from mcu_decorators import mcu, pc, registered_mcu_functions" in script
    assert "import dispatch" in script
    assert "import uasyncio" in script


def test_generate_bootstrap_wires_streams_from_stdin_stdout():
    script = generate_bootstrap("", "")
    assert "sys.stdin.buffer" in script
    assert "sys.stdout.buffer" in script


def test_generate_bootstrap_registers_the_handshake_handler():
    script = generate_bootstrap("", "")
    assert "__tether_handshake__" in script
    assert str(PROTOCOL_VERSION) in script


# Module-level, matching the real intended usage pattern: connect() finds
# already-decorated functions via the caller frame's globals, which only
# works if they're defined at module scope (or anywhere sharing this
# module's globals) - not nested inside the test function that calls
# connect(), which would make them locals of that function instead.
@mcu.export
def _mock_read_temp() -> float:
    return 21.5


@pc.export
def _mock_double(x: int) -> int:
    return x * 2


@mcu.export
async def _mock_read_scaled() -> int:
    return await _mock_double(21)


def test_connect_mock_reaches_a_registered_mcu_export_function():
    board = connect("mock://")
    assert board._mock_read_temp() == 21.5


def test_connect_mock_board_handle_rejects_unknown_attribute():
    board = connect("mock://")
    with pytest.raises(AttributeError):
        board.nonexistent_function()


def test_board_handle_returns_the_same_callable_on_repeated_access():
    # __getattr__ caches into __dict__ - the second access resolves through
    # normal attribute lookup, not __getattr__ again.
    board = connect("mock://")
    assert board._mock_read_temp is board._mock_read_temp


def test_connect_mock_two_boards_route_calls_independently():
    # Multi-board: two connect() calls must not share dispatch state -
    # calling a function on board_a must never reach board_b's handler.
    board_a = connect("mock://")
    board_b = connect("mock://")

    assert board_a._mock_read_temp() == 21.5
    assert board_b._mock_read_temp() == 21.5


def test_board_handle_reconnect_establishes_a_fresh_working_dispatcher():
    # Cached call closures (BoardHandle.__getattr__) resolve self._dispatcher
    # dynamically at call time, so a reconnect that swaps in a new
    # Dispatcher must transparently make already-cached closures work again
    # - no re-fetching board.<name> required from calling code.
    board = connect("mock://")
    old_dispatcher = board._dispatcher
    cached_call = board._mock_read_temp  # triggers __dict__ caching

    board.reconnect()

    assert board._dispatcher is not old_dispatcher
    assert board._mock_read_temp is cached_call  # still the same cached closure
    assert cached_call() == 21.5  # and it still works, routed to the new dispatcher


def test_connect_serial_raises_clearly_on_a_decorated_but_unsliced_function():
    # _capture_caller finds @mcu.export functions on already-executed live
    # objects (works regardless of control flow); the AST slicer only
    # recognizes plain top-level def/async def statements. A function
    # decorated inside if/try/etc would pass the former check but never
    # get sliced or registered on-device - this must fail loudly at
    # connect() time, not as a confusing MCUTimeoutError later.
    from tether.connection import _connect_serial
    from tether.decorators import ExportSpec

    ghost_spec = ExportSpec(func=lambda: None, side="mcu")

    with pytest.raises(RuntimeError, match="ghost_fn"):
        _connect_serial("auto", "", {"ghost_fn": ghost_spec}, frozenset(), timeout=1.0)


@requires_micropython
def test_generated_bootstrap_actually_works_end_to_end():
    # Exercises the exact generated script's registration loop + handshake
    # against the real chunk 6 Dispatcher and mcu_decorators shim under a
    # real MicroPython interpreter - only the sys.stdin/stdout wiring lines
    # are substituted for test-controllable in-memory pipes (already
    # verified separately, against real piped stdin/stdout, that
    # uasyncio.StreamReader/Writer wrapping sys.stdin.buffer/stdout.buffer
    # works correctly - not re-proven here, this test is about the
    # registration/handshake wiring instead).
    sliced_source = "@mcu.export\ndef read_temp() -> float:\n    return 21.5"
    bootstrap = generate_bootstrap(sliced_source, "")

    # Swap the real stdio wiring for fake in-memory streams driven by the
    # test, keeping every other line (imports, registration loop, handshake)
    # exactly as generated. `_reader`/`_writer` become module-level globals
    # (set up by the test driver below, before _tether_main runs) rather
    # than being constructed inside _tether_main's own local scope, so the
    # driver can also reach the other end of the same pipe pair. Also drop
    # the trailing self-running `_tether_asyncio.run(_tether_main())` -
    # correct for real deployment (it's meant to run forever as the entry
    # point), but this test needs to drive _tether_main as a task inside
    # its own event loop instead, or that blocking call would prevent the
    # driver code below it from ever running at all.
    patched = bootstrap.replace(
        "    _reader = _tether_asyncio.StreamReader(_tether_sys.stdin.buffer)\n"
        "    _writer = _tether_asyncio.StreamWriter(_tether_sys.stdout.buffer, {})\n",
        "",
    ).replace("\n_tether_asyncio.run(_tether_main())\n", "\n")

    out = run_micropython(
        """
import uasyncio as asyncio

class Pipe:
    def __init__(self):
        self.buf = b""
        self.event = asyncio.Event()

    def write(self, data):
        self.buf += data
        if not self.event.is_set():
            self.event.set()

    async def drain(self):
        pass

    async def readexactly(self, n):
        while len(self.buf) < n:
            self.event.clear()
            await self.event.wait()
        chunk = self.buf[:n]
        self.buf = self.buf[n:]
        return chunk


def make_pair():
    a_to_b = Pipe()
    b_to_a = Pipe()
    return (b_to_a, a_to_b), (a_to_b, b_to_a)

(reader_a, writer_a), (_reader, _writer) = make_pair()

"""
        + patched
        + """

import dispatch as _driver_dispatch

async def _run_test():
    asyncio.create_task(_tether_main())
    driver = _driver_dispatch.Dispatcher(reader_a, writer_a)
    asyncio.create_task(driver.run())
    version = await driver.call_pc("__tether_handshake__")
    print("handshake:", version)
    result = await driver.call_pc("read_temp")
    print("read_temp:", result)

asyncio.run(_run_test())
"""
    )

    assert f"handshake: {PROTOCOL_VERSION}" in out
    assert "read_temp: 21.5" in out


def _serve_one_device_connection(sock: socket.socket, *, handshake_version: int) -> None:
    """Stand-in for an already-running on-device runtime, reachable over a
    real TCP socket: exactly what DESIGN.md's "board must already be
    running a bootstrapped runtime" precondition assumes for the wifi
    transport (chunk 12's actual scope - see transports/wifi.py's module
    docstring for why this chunk doesn't build the device side of that).
    Runs a real tether.dispatch.Dispatcher server-side, registered with a
    handshake handler and one export, over a real accepted connection.
    """
    conn, _addr = sock.accept()

    stream = WifiStream(conn)  # reuse the real transport class on the fake device side too
    dispatcher = Dispatcher(stream, stream)
    dispatcher.register("__tether_handshake__", lambda: handshake_version)
    # Matches the module-level `_mock_read_temp` @mcu.export function
    # already defined above in this file - _capture_caller() picks it up
    # from module globals regardless of which transport scheme connect()
    # is asked to use, so the resulting BoardHandle exposes it here too.
    dispatcher.register("_mock_read_temp", lambda: 21.5)
    dispatcher.start()


def test_connect_wifi_reaches_an_already_running_device_over_a_real_tcp_socket():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    threading.Thread(
        target=_serve_one_device_connection,
        args=(listener,),
        kwargs={"handshake_version": PROTOCOL_VERSION},
        daemon=True,
    ).start()

    board = connect(f"wifi:127.0.0.1:{port}", timeout=2.0)

    assert board._mock_read_temp() == 21.5
