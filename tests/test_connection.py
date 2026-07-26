import socket
import sys
import threading
from pathlib import Path

import pytest

from tether import mcu, pc
from tether._context import current_board
from tether.connection import PROTOCOL_VERSION, _connect_wifi, connect, generate_bootstrap
from tether.dispatch import Dispatcher
from tether.slicer import slice_mcu_bound
from tether.transports.wifi import WifiStream

sys.path.insert(0, str(Path(__file__).parent))
from mpy_runner import PIPE_HARNESS, requires_micropython, run_micropython


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


def test_generate_bootstrap_checks_for_a_stream_override_before_stdio():
    # boot.py (chunk 19, wifi provisioning) sets a `_tether_stream_override`
    # global to (reader, writer) before exec'ing this generated source, to
    # bridge the dispatch loop onto a socket instead of stdio - the
    # override must be checked FIRST, with stdio as the fallback, so every
    # existing serial connect() (which never sets it) is unaffected.
    script = generate_bootstrap("", "")
    assert "_tether_stream_override" in script
    override_check_index = script.index("_tether_stream_override")
    stdio_wiring_index = script.index("sys.stdin.buffer")
    assert override_check_index < stdio_wiring_index


def test_generate_bootstrap_disables_the_ctrl_c_keyboard_interrupt():
    # Found against real ESP32 hardware, not reproducible against the PTY
    # simulation chunk 10 originally verified this with (which only proved
    # the interception was a *host-side* PTY line-discipline artifact under
    # CPython - it never established what real MicroPython firmware does
    # with a raw 0x03 byte arriving mid-stream on a real UART, and it turns
    # out to be different): msgpack routinely encodes small integers as
    # their own literal byte value, so any call argument that happens to
    # produce a raw 0x03 byte anywhere in its frame (e.g. add(2, 3) - 3
    # encodes as byte 0x03) gets intercepted by MicroPython's UART driver
    # as Ctrl-C and raises KeyboardInterrupt *inside the running dispatch
    # loop*, killing it and corrupting the stream for the PC side too.
    # `micropython.kbd_intr(-1)` (a real, documented MicroPython API for
    # exactly this: disabling the built-in keyboard-interrupt character so
    # a UART can carry raw binary protocols) must run before the dispatch
    # loop starts reading real protocol bytes.
    script = generate_bootstrap("", "")
    assert "import micropython" in script
    assert "kbd_intr(-1)" in script
    # Must happen before the dispatch loop starts, not after - the whole
    # point is to disable interception before any real protocol byte
    # (which could contain 0x03) is ever read.
    assert script.index("kbd_intr(-1)") < script.index("_dispatcher.run()")


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


_ambient_local_execution_count = 0


@mcu.export
def _mock_ambient_counter() -> int:
    # MockTransport execs the SAME source text in a SEPARATE namespace to
    # simulate "the device" (see transports/mock.py) - so this global gets
    # incremented ONLY if THIS exact PC-side function object's body somehow
    # ran locally instead of dispatching over the (mock) wire. Needed
    # because a plain return value here (e.g. _mock_read_temp's 21.5)
    # can't distinguish "correctly dispatched" from "buggily fell through
    # to local execution" - both paths would return the same literal,
    # since the mock device re-execs identical source.
    global _ambient_local_execution_count
    # `x = x + 1`, not `x += 1`: the slicer's dependency walk only tracks
    # Name references with Load context (marshalling/__init__.py-adjacent
    # concern, see slicer/__init__.py's _referenced_names) - an AugAssign
    # target has Store context, so `x += 1` would never pull in this
    # global's `= 0` initializer when this function gets sliced for the
    # mock "device" side, and MockTransport would raise NameError there
    # instead of testing anything about ambient dispatch.
    _ambient_local_execution_count = _ambient_local_execution_count + 1
    return 42


@pc.export
def _mock_double(x: int) -> int:
    return x * 2


@mcu.export
async def _mock_read_scaled() -> int:
    return await _mock_double(21)


_reentrant_observed_board: list[object] = []


@pc.export
def _mock_relay() -> float:
    # Runs on a Dispatcher worker thread, triggered by an incoming MCU
    # call (see _mock_trigger_relay below) - _mock_relay is @pc.export, a
    # real PC-side function called directly (not sliced/re-exec'd like
    # @mcu.export functions are for the mock "device"), so it can observe
    # current_board directly and the test can inspect that afterward -
    # sidesteps the "two mock:// boards run identical devices, so return
    # values alone can't distinguish them" problem the multi-board `with`
    # test above works around differently.
    _reentrant_observed_board.append(current_board.get())
    return 0.0


@mcu.export
async def _mock_trigger_relay() -> float:
    return await _mock_relay()


def test_reentrant_handler_sees_the_triggering_board_as_ambient_not_the_connecting_threads_default():
    # A @pc.export handler running because board_a's MCU called it must see
    # board_a as ambient for any further ambient MCU calls it makes itself
    # - NOT board_b, even though board_b is what's ambient on the
    # connecting/main thread (the "most recent connect() wins" default from
    # the test above). Otherwise a reentrant handler on one board could
    # accidentally dispatch a "local" ambient call to a completely
    # different board.
    board_a = connect("mock://")
    connect("mock://")  # board_b - becomes ambient on THIS thread, deliberately not asserted on
    _reentrant_observed_board.clear()

    board_a._mock_trigger_relay()

    assert _reentrant_observed_board == [board_a]


def test_reentrant_handler_sees_the_board_correctly_immediately_after_reconnect():
    # The gap this closes: unlike a fresh connect(), a reconnected device
    # may already be running @mcu.loop background tasks that call back
    # immediately - Dispatcher.board must be correct from dial()'s very
    # first moment (set via the closure variable each _connect_*() function
    # threads into _start_and_handshake/dial), not just "eventually" set by
    # BoardHandle.__init__ after dial() has already returned and started
    # the reader thread.
    board = connect("mock://")
    board.reconnect()
    _reentrant_observed_board.clear()

    board._mock_trigger_relay()

    assert _reentrant_observed_board == [board]


def test_connect_mock_mcu_can_call_back_into_a_registered_pc_export_function():
    # Found against REAL hardware, not caught by any existing test: nothing
    # in connect() ever registered @pc.export functions as PC-side dispatch
    # handlers, for any transport, including mock - _mock_read_scaled was
    # defined all the way back in chunk 10 to test the *slicer's* handling
    # of async MCU functions calling PC stubs, but nothing ever actually
    # CALLED it, so the runtime reverse-call path (MCU -> call_pc() -> PC
    # dispatcher -> a real registered Python handler) was never exercised
    # end-to-end anywhere until real hardware caught it.
    board = connect("mock://")
    assert board._mock_read_scaled() == 42


def test_connect_mock_reaches_a_registered_mcu_export_function():
    board = connect("mock://")
    assert board._mock_read_temp() == 21.5


def test_calling_an_exported_function_directly_with_no_board_connected_raises_clearly():
    # No connect() has happened in this test - _mock_read_temp() called
    # directly (not via board.<name>()) must fail loud and clear, not
    # silently run its own body locally (which would happen to return the
    # same 21.5 either way here, masking the bug) or hang.
    with pytest.raises(RuntimeError, match="_mock_read_temp"):
        _mock_read_temp()


def test_calling_an_exported_function_directly_dispatches_through_the_ambient_board():
    connect("mock://")  # sets itself as the ambient "current" board
    assert _mock_ambient_counter() == 42
    # Proves the call actually crossed the (mock) wire rather than falling
    # through to running the PC-side function object's own body locally -
    # see _mock_ambient_counter's own comment for why the return value
    # alone can't distinguish the two.
    assert _ambient_local_execution_count == 0


def test_with_board_scopes_the_ambient_board_for_multi_board_disambiguation():
    board_a = connect("mock://")
    board_b = connect("mock://")  # most-recently-connected wins by default
    assert current_board.get() is board_b

    with board_a:
        assert current_board.get() is board_a
        assert _mock_read_temp() == 21.5  # dispatches through board_a specifically

    # Restores the PREVIOUS ambient value (board_b), not just clears it -
    # proper token-based reset, not a naive "set to None".
    assert current_board.get() is board_b
    assert _mock_read_temp() == 21.5  # dispatches through board_b now


def test_mcu_connect_is_the_public_api_design_md_documents():
    # DESIGN.md's own architecture doc (and every example/README snippet)
    # shows `mcu.connect(...)`, never a bare top-level `connect(...)` -
    # `connect` must be reachable as an attribute of the `mcu` namespace
    # object, not just importable from tether.connection. Assigning the
    # SAME function object (not a wrapper) is what makes this work without
    # adding a stack frame _capture_caller()'s frame-counting depends on -
    # see tether/__init__.py's own comment on this.
    assert mcu.connect is connect
    board = mcu.connect("mock://")
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
        _connect_serial("auto", "", {"ghost_fn": ghost_spec}, frozenset(), {}, timeout=1.0)


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
    # driver can also reach the other end of the same pipe pair. The
    # override-checking logic must be stripped to use the test-provided
    # globals. Also drop the trailing self-running `_tether_asyncio.run(_tether_main())` -
    # correct for real deployment (it's meant to run forever as the entry
    # point), but this test needs to drive _tether_main as a task inside
    # its own event loop instead, or that blocking call would prevent the
    # driver code below it from ever running at all.
    patched = bootstrap.replace(
        '    _override = globals().get("_tether_stream_override")\n'
        "    if _override is not None:\n"
        "        _reader, _writer = _override\n"
        "    else:\n"
        "        _reader = _tether_asyncio.StreamReader(_tether_sys.stdin.buffer)\n"
        "        _writer = _tether_asyncio.StreamWriter(_tether_sys.stdout.buffer, {})\n",
        "",
    ).replace("\n_tether_asyncio.run(_tether_main())\n", "\n")

    out = run_micropython(
        PIPE_HARNESS
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


@requires_micropython
def test_generate_bootstrap_clears_mcu_decorators_registry_before_re_exec():
    # Regression test for a bug the wifi accept-loop (Tasks 3-5) would
    # otherwise introduce: mcu_decorators._registrations is module-level
    # state that accumulates across repeated exec() of the same generated
    # bootstrap within one interpreter process. Runs the exact same
    # generated bootstrap TWICE in one micropython process (a fake reader
    # that immediately raises EOFError lets _tether_main() exit quickly
    # each time, standing in for "the wifi connection just closed") and
    # asserts mcu_decorators._registrations never grows past what ONE
    # exec's own decorator applications produce - it must be exactly 1
    # after both the first AND the second exec, not 1 then 2.
    sliced_source = "@mcu.loop(interval_ms=50)\ndef tick():\n    pass\n"
    bootstrap = generate_bootstrap(sliced_source, "")

    script = f"""
import uasyncio as asyncio
import mcu_decorators

class _EOFReader:
    async def readexactly(self, n):
        raise EOFError("closed")

class _NullWriter:
    def write(self, data):
        pass
    async def drain(self):
        pass

async def run_once():
    ns = {{"_tether_stream_override": (_EOFReader(), _NullWriter())}}
    try:
        exec({bootstrap!r}, ns)
    except EOFError:
        pass

async def main():
    await run_once()
    print("after_first:", len(mcu_decorators._registrations))
    await run_once()
    print("after_second:", len(mcu_decorators._registrations))

asyncio.run(main())
"""
    out = run_micropython(script, timeout=10.0)

    assert "after_first: 1" in out
    assert "after_second: 1" in out


def _serve_one_device_connection(sock: socket.socket, *, handshake_version: int) -> None:
    """Stand-in for an already-running on-device runtime, reachable over a
    real TCP socket: exactly what DESIGN.md's "board must already be
    running a bootstrapped runtime" precondition assumes for the wifi
    transport. Since Task 6, connect("wifi:...") always does
    status -> upload-if-needed -> run (see _connect_wifi), so this fake
    device speaks all three steps - it always reports no on-device hash
    (forcing an upload) and discards whatever gets uploaded, since this
    test's only concern is "connect() reaches a live device end-to-end",
    not upload content (that's
    test_connect_wifi_uploads_when_hash_differs_then_runs's job). Runs a
    real tether.dispatch.Dispatcher server-side for the run connection,
    registered with a handshake handler and one export.
    """
    import json
    import struct

    length_prefix = struct.Struct(">I")

    def read_json(conn):
        header = conn.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += conn.recv(length - len(body))
        return json.loads(body)

    def send_json(conn, obj):
        body = json.dumps(obj).encode()
        conn.sendall(length_prefix.pack(len(body)) + body)

    def read_bytes_frame(conn):
        header = conn.recv(4)
        (length,) = length_prefix.unpack(header)
        buf = b""
        while len(buf) < length:
            buf += conn.recv(length - len(buf))
        return buf

    # 1. status connection - reports no hash, so the client must upload.
    conn, _addr = sock.accept()
    preamble = read_json(conn)
    assert preamble["mode"] == "status"
    send_json(conn, {"ok": True})
    send_json(conn, {"tether_app_hash": None})
    conn.close()

    # 2. upload connection - receive and discard the manifest + file bytes.
    conn2, _addr = sock.accept()
    preamble2 = read_json(conn2)
    assert preamble2["mode"] == "upload"
    send_json(conn2, {"ok": True})
    manifest = read_json(conn2)
    for file_meta in manifest["files"]:
        remaining = file_meta["size"]
        while remaining > 0:
            remaining -= len(read_bytes_frame(conn2))
    send_json(conn2, {"ok": True})
    conn2.close()

    # 3. run connection - ack the preamble, then serve a real Dispatcher.
    conn3, _addr = sock.accept()
    preamble3 = read_json(conn3)
    assert preamble3["mode"] == "run"
    send_json(conn3, {"ok": True})

    stream = WifiStream(conn3)  # reuse the real transport class on the fake device side too
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
    listener.listen(4)
    port = listener.getsockname()[1]
    threading.Thread(
        target=_serve_one_device_connection,
        args=(listener,),
        kwargs={"handshake_version": PROTOCOL_VERSION},
        daemon=True,
    ).start()

    board = connect(f"wifi:127.0.0.1:{port}", timeout=2.0)

    assert board._mock_read_temp() == 21.5


def test_connect_ble_reaches_the_ble_transport_and_fails_loud_without_bleak_installed():
    # bleak (tether[ble]) genuinely isn't installed in this dev environment
    # - confirms the "ble:" scheme actually routes into
    # transports/ble.py's connect() (address preserved past the first ":",
    # matters since BLE MAC addresses contain colons themselves) rather
    # than silently no-op'ing or hitting the wrong branch.
    with pytest.raises(ModuleNotFoundError, match="bleak"):
        connect("ble:00:11:22:33:44:55", timeout=1.0)


def test_connect_wifi_uploads_when_hash_differs_then_runs():
    import json
    import socket
    import struct
    import threading

    length_prefix = struct.Struct(">I")

    def read_json(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        body = b""
        while len(body) < length:
            body += sock.recv(length - len(body))
        return json.loads(body)

    def send_json(sock, obj):
        body = json.dumps(obj).encode()
        sock.sendall(length_prefix.pack(len(body)) + body)

    def recv_exact(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("closed")
            buf += chunk
        return buf

    def read_bytes_frame(sock):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        return recv_exact(sock, length)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(4)
    port = server_sock.getsockname()[1]

    received_files: dict[str, bytes] = {}
    received_dirs: list = []

    def fake_device():
        # 1. status connection - reports no hash, so the client must upload.
        conn, _ = server_sock.accept()
        preamble = read_json(conn)
        assert preamble["mode"] == "status"
        send_json(conn, {"ok": True})
        send_json(
            conn,
            {
                "protocol_version": 1,
                "tether_app_hash": None,
                "free_heap": 100000,
                "uptime_ms": 500,
                "ip": "127.0.0.1",
            },
        )
        conn.close()

        # 2. upload connection - receive the manifest, then each file's bytes.
        conn2, _ = server_sock.accept()
        preamble2 = read_json(conn2)
        assert preamble2["mode"] == "upload"
        send_json(conn2, {"ok": True})
        manifest = read_json(conn2)
        received_dirs.extend(manifest["dirs"])
        for file_meta in manifest["files"]:
            remaining = file_meta["size"]
            content = b""
            while remaining > 0:
                chunk = read_bytes_frame(conn2)
                content += chunk
                remaining -= len(chunk)
            received_files[file_meta["path"]] = content
        send_json(conn2, {"ok": True})
        conn2.close()

        # 3. run connection - ack the preamble, then answer the handshake.
        conn3, _ = server_sock.accept()
        preamble3 = read_json(conn3)
        assert preamble3["mode"] == "run"
        send_json(conn3, {"ok": True})

        import msgpack

        header = conn3.recv(4)
        body_len = int.from_bytes(header, "big")
        body = conn3.recv(body_len)
        request = msgpack.unpackb(body[1:], raw=False)
        assert request["name"] == "__tether_handshake__"

        from tether.marshalling import encode_frame

        conn3.sendall(encode_frame(2, {"id": request["id"], "value": PROTOCOL_VERSION}))
        # Keep the connection open briefly so BoardHandle construction
        # completes before the test tears down - the reader thread inside
        # Dispatcher.start() needs a live socket to not immediately see EOF.
        conn3.settimeout(2.0)
        try:
            conn3.recv(1)
        except OSError:
            pass
        conn3.close()

    server_thread = threading.Thread(target=fake_device, daemon=True)
    server_thread.start()

    source = (
        "from tether import mcu, pc\n\n"
        "@mcu.export\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
    )
    export_specs = {"add": object()}  # exact value unused by this path; presence is what matters
    pc_handlers: dict = {}

    sliced = slice_mcu_bound(source)
    bootstrap = generate_bootstrap(sliced.source, "")

    board = _connect_wifi(
        f"127.0.0.1:{port}",
        bootstrap,
        export_specs,
        sliced.exported_names,
        pc_handlers,
        timeout=5.0,
    )

    server_thread.join(timeout=5.0)
    # Full bundle, not just tether_app.py - the design spec explicitly
    # requires wifi upload to push the whole tether_runtime library too
    # (dispatch.py, mcu_decorators.py, vendored umsgpack), same as serial's
    # _upload_if_needed already does, so "wifi never needs serial again
    # after the first provision" is actually true.
    assert received_files["/tether_app.py"] == bootstrap.encode()
    assert "/.tether_hash" in received_files
    assert "/dispatch.py" in received_files
    assert "/mcu_decorators.py" in received_files
    assert "/umsgpack/__init__.py" in received_files
    assert received_dirs == ["/umsgpack"]
    assert board is not None
