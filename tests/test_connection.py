import socket
import sys
import threading
from pathlib import Path

import pytest

from tether import mcu, pc
from tether._context import current_board
from tether.connection import (
    PROTOCOL_VERSION,
    _connect_wifi,
    _hint_if_frame_auth_failure,
    connect,
    generate_bootstrap,
)
from tether.dispatch import Dispatcher
from tether.errors import FrameAuthenticationError, MCUDisconnectedError
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


def test_wifi_auth_error_is_importable_from_the_top_level_tether_package():
    # Final-review finding: `from tether import WifiAuthError` raised
    # ImportError, while RemoteError, MCUTimeoutError,
    # MCUDisconnectedError, and ProtocolVersionError all work fine this
    # way (tether/__init__.py re-exports them). connect()'s own docstring
    # tells users to expect WifiAuthError on a bad/missing secret, so it
    # needs to be a properly public, importable exception like its
    # siblings, not something reachable only via tether.errors directly.
    from tether import WifiAuthError as top_level_wifi_auth_error
    from tether.errors import WifiAuthError as errors_module_wifi_auth_error

    assert top_level_wifi_auth_error is errors_module_wifi_auth_error


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

    # Every connection now starts with the device sending a nonce before
    # reading anything - see wifi.py's send_preamble docstring. This fake
    # device doesn't simulate real auth (no secret is configured on the
    # client side in these tests), so a fixed placeholder nonce is fine:
    # the client only reads it and echoes back response=None.
    fake_nonce = {"nonce": "00" * 16}

    # 1. status connection - reports no hash, so the client must upload.
    conn, _addr = sock.accept()
    send_json(conn, fake_nonce)
    preamble = read_json(conn)
    assert preamble["mode"] == "status"
    send_json(conn, {"ok": True})
    send_json(conn, {"tether_app_hash": None})
    conn.close()

    # 2. upload connection - receive and discard the manifest + file bytes.
    conn2, _addr = sock.accept()
    send_json(conn2, fake_nonce)
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
    send_json(conn3, fake_nonce)
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


def _serve_one_authenticated_device_connection(
    sock: socket.socket, *, handshake_version: int, secret: str, corrupt_status_reply: bool = False
) -> None:
    """Authenticated counterpart to _serve_one_device_connection (see its
    own docstring above) - requires the real nonce-challenge response and
    wraps every device->PC reply in a FrameAuthenticator envelope, proving
    connection.py's session-key derivation and Task 2's authenticated
    frame helpers round-trip against a well-behaved authenticated peer.
    `corrupt_status_reply=True` flips a byte in the status reply's
    envelope, for the rejection test below.
    """
    import hashlib
    import hmac
    import json
    import struct

    from tether.marshalling.frame_auth import FrameAuthenticator
    from tether.transports.wifi import (
        read_authenticated_bytes_frame,
        read_authenticated_json_frame,
        send_authenticated_json_frame,
    )

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

    def accept_authenticated(conn, mode):
        nonce = b"0123456789abcdef"
        send_json(conn, {"nonce": nonce.hex()})
        preamble = read_json(conn)
        assert preamble["mode"] == mode
        expected_response = hmac.new(secret.encode(), nonce, hashlib.sha256).hexdigest()
        assert preamble["response"] == expected_response
        send_json(conn, {"ok": True})
        return hmac.new(secret.encode(), b"tether-frame-key" + nonce, hashlib.sha256).digest()

    # 1. status connection.
    conn, _addr = sock.accept()
    session_key = accept_authenticated(conn, "status")
    auth = FrameAuthenticator(session_key)
    if corrupt_status_reply:
        envelope = bytearray(auth.wrap(b'{"tether_app_hash": null}'))
        envelope[-1] ^= 0xFF
        conn.sendall(bytes(envelope))
    else:
        send_authenticated_json_frame(conn, auth, {"tether_app_hash": None})
    conn.close()
    if corrupt_status_reply:
        return  # PC side is expected to raise and never reach upload/run

    # 2. upload connection - receive and discard the manifest + file bytes.
    conn2, _addr = sock.accept()
    session_key2 = accept_authenticated(conn2, "upload")
    recv_auth = FrameAuthenticator(session_key2)
    send_auth = FrameAuthenticator(session_key2)
    manifest = read_authenticated_json_frame(conn2, recv_auth)
    for file_meta in manifest["files"]:
        remaining = file_meta["size"]
        while remaining > 0:
            remaining -= len(read_authenticated_bytes_frame(conn2, recv_auth))
    send_authenticated_json_frame(conn2, send_auth, {"ok": True})
    conn2.close()

    # 3. run connection - real WifiStream + Dispatcher, authenticated.
    conn3, _addr = sock.accept()
    session_key3 = accept_authenticated(conn3, "run")
    stream = WifiStream(conn3)
    stream.enable_authentication(session_key3)
    dispatcher = Dispatcher(stream, stream)
    dispatcher.register("__tether_handshake__", lambda: handshake_version)
    dispatcher.register("_mock_read_temp", lambda: 21.5)
    dispatcher.start()


def test_connect_wifi_authenticates_status_upload_and_run_with_a_shared_secret():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    device_thread = threading.Thread(
        target=_serve_one_authenticated_device_connection,
        kwargs={"sock": listener, "handshake_version": PROTOCOL_VERSION, "secret": "the-secret"},
        daemon=True,
    )
    device_thread.start()

    board = connect(f"wifi:127.0.0.1:{port}", secret="the-secret", timeout=5.0)

    assert board._mock_read_temp() == 21.5
    device_thread.join(timeout=5.0)
    listener.close()


def test_connect_wifi_rejects_a_tampered_authenticated_status_reply():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    device_thread = threading.Thread(
        target=_serve_one_authenticated_device_connection,
        kwargs={
            "sock": listener,
            "handshake_version": PROTOCOL_VERSION,
            "secret": "the-secret",
            "corrupt_status_reply": True,
        },
        daemon=True,
    )
    device_thread.start()

    with pytest.raises(FrameAuthenticationError):
        connect(f"wifi:127.0.0.1:{port}", secret="the-secret", timeout=5.0)

    device_thread.join(timeout=5.0)
    listener.close()


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

    fake_nonce = {"nonce": "00" * 16}

    def fake_device():
        # 1. status connection - reports no hash, so the client must upload.
        conn, _ = server_sock.accept()
        send_json(conn, fake_nonce)
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
        send_json(conn2, fake_nonce)
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
        send_json(conn3, fake_nonce)
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


def test_connect_ble_uploads_when_hash_differs_then_runs_over_one_connection():
    import json
    import sys
    import types
    from unittest.mock import patch

    from tether.connection import _connect_ble
    from tether.slicer import slice_mcu_bound

    source = """
from tether import mcu

@mcu.export
def add(a: int, b: int) -> int:
    return a + b
"""
    sliced = slice_mcu_bound(source, base_dir=Path("."))
    bootstrap = generate_bootstrap(sliced.source, "")
    # SliceResult (slicer/__init__.py) has no `export_specs` field - only
    # `source`/`exported_names` - matching the existing
    # test_connect_wifi_uploads_when_hash_differs_then_runs test's own
    # pattern above: `_connect_ble`'s unsliced-decorator check only reads
    # export_specs.keys(), so a minimal hand-built dict is sufficient here.
    export_specs = {"add": object()}  # exact value unused by this path; presence is what matters

    received_modes = []

    class _FakeDevice:
        """Stateful fake device: parses length-prefixed JSON/byte frames
        accumulated across write_gatt_char calls and pushes responses
        back via the client's registered notify callback - mirrors the
        real on-device BLE session loop's mode dispatch (one connection,
        sequential preambles). Driven synchronously: write_gatt_char
        calls happen on the fake client's own event-loop thread, the
        same thread queue.Queue.put (inside on_notify) is safe to call
        from directly, no cross-thread handoff needed here.

        Simplification versus the real device: each file's content is
        assumed to arrive in exactly one bytes-frame (true for this
        test's tiny single-function source) - multi-frame chunking
        fidelity is already covered by Task 1's BleControlChannel tests
        and Task 3's on-device tests, not re-verified here.
        """

        def __init__(self, client, received_modes):
            self._client = client
            self._received_modes = received_modes
            self._buffer = b""
            self._state = "await_preamble"
            self._pending_files: list[dict] = []

        def feed(self, data: bytes) -> None:
            self._buffer += bytes(data)
            self._drain()

        def _notify(self, payload: dict) -> None:
            body = json.dumps(payload).encode()
            frame = len(body).to_bytes(4, "big") + body
            self._client._pending_notify_cb(None, bytearray(frame))

        def _notify_raw(self, data: bytes) -> None:
            self._client._pending_notify_cb(None, bytearray(data))

        def send_nonce(self) -> None:
            # Real device sends its one-per-connection nonce proactively,
            # before reading anything - see provisioning.py's
            # _BLE_BOOT_TEMPLATE. This fake doesn't simulate real auth (the
            # response is never checked below), so a fixed placeholder is
            # fine - the client only needs something to read and respond
            # to before its first send_preamble() call proceeds.
            self._notify({"nonce": "00" * 16})

        def _drain(self) -> None:
            while len(self._buffer) >= 4:
                length = int.from_bytes(self._buffer[:4], "big")
                if len(self._buffer) < 4 + length:
                    return
                body = self._buffer[4 : 4 + length]
                self._buffer = self._buffer[4 + length :]
                self._handle_frame(body)

        def _handle_frame(self, body: bytes) -> None:
            if self._state == "await_preamble":
                preamble = json.loads(body)
                mode = preamble["mode"]
                self._received_modes.append(mode)
                self._notify({"ok": True})
                if mode == "status":
                    self._notify(
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "tether_app_hash": None,
                            "free_heap": 100000,
                            "uptime_ms": 500,
                            "ip": "aa:bb:cc:dd:ee:ff",
                        }
                    )
                elif mode == "upload":
                    self._state = "await_manifest"
                elif mode == "run":
                    self._state = "await_handshake"
            elif self._state == "await_manifest":
                manifest = json.loads(body)
                self._pending_files = list(manifest["files"])
                self._state = "await_file_content" if self._pending_files else "await_preamble"
                if not self._pending_files:
                    self._notify({"ok": True})
            elif self._state == "await_file_content":
                self._pending_files.pop(0)  # one frame == one file, see class docstring
                if self._pending_files:
                    return  # still expecting more files' content frames
                self._notify({"ok": True})
                self._state = "await_preamble"
            elif self._state == "await_handshake":
                import msgpack

                request = msgpack.unpackb(body[1:], raw=False)
                assert request["name"] == "__tether_handshake__"
                from tether.marshalling import encode_frame

                self._notify_raw(encode_frame(2, {"id": request["id"], "value": PROTOCOL_VERSION}))
                self._state = "done"

    class _FakeBleakClient:
        mtu_size = 200

        def __init__(self, address, disconnected_callback=None):
            self.address = address
            self._disconnected_callback = disconnected_callback
            self._pending_notify_cb = None
            self.connected = False
            self._device = _FakeDevice(self, received_modes)

        async def connect(self, timeout=10.0):
            self.connected = True

        async def start_notify(self, char_uuid, callback):
            self._pending_notify_cb = callback
            self._device.send_nonce()

        async def write_gatt_char(self, char_uuid, data, response=True):
            self._device.feed(data)

        async def disconnect(self):
            self.connected = False

    # This dev environment/CI genuinely has no `bleak` installed (see
    # test_transport_ble.py's test_connect_fails_loud_when_bleak_is_not_installed)
    # - patch("bleak.BleakClient", ...) can't traverse into a module that
    # doesn't exist. Inject a fake `bleak` module into sys.modules instead,
    # so transports/ble.py's lazy `import bleak` (inside its connect())
    # resolves to it, matching the brief's Step 2 hint to adjust the patch
    # target to wherever that import actually happens.
    fake_bleak = types.ModuleType("bleak")
    fake_bleak.BleakClient = _FakeBleakClient
    with patch.dict(sys.modules, {"bleak": fake_bleak}):
        board = _connect_ble(
            "AA:BB:CC:DD:EE:FF",
            bootstrap,
            export_specs,
            sliced.exported_names,
            {},
            timeout=5.0,
            secret="test-secret",
        )

    assert received_modes == ["status", "upload", "run"]
    assert board is not None


def test_connect_wifi_chunks_a_file_larger_than_max_control_frame_size():
    # Final-review finding: _upload() sent one send_bytes_frame per file
    # unconditionally, even though the whole control channel's invariant
    # (design spec) is "no single frame ever needs to hold more than
    # MAX_FRAME_SIZE bytes" and the device side already loops reading
    # chunks per file. A ~64KiB+ app file used to fail with "upload chunk
    # too large" on the device side (its own MAX_CONTROL_FRAME_SIZE guard
    # correctly rejecting an oversized single frame) and left a truncated
    # write behind. This test uses a `bootstrap` string bigger than
    # MAX_CONTROL_FRAME_SIZE directly (bypassing the slicer - the size of
    # the *content*, not how it was produced, is what this test is about)
    # and asserts the client actually splits it into multiple frames, none
    # of which exceeds the bound, while the reassembled content on the
    # device side is still byte-for-byte correct.
    import json
    import socket
    import struct
    import threading

    from tether.transports.wifi import MAX_CONTROL_FRAME_SIZE

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

    def read_bytes_frame_capturing_chunk_sizes(sock, chunk_sizes):
        header = sock.recv(4)
        (length,) = length_prefix.unpack(header)
        chunk_sizes.append(length)
        return recv_exact(sock, length)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(4)
    port = server_sock.getsockname()[1]

    received_files: dict[str, bytes] = {}
    chunk_sizes: list[int] = []

    fake_nonce = {"nonce": "00" * 16}

    def fake_device():
        conn, _ = server_sock.accept()
        send_json(conn, fake_nonce)
        assert read_json(conn)["mode"] == "status"
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

        conn2, _ = server_sock.accept()
        send_json(conn2, fake_nonce)
        assert read_json(conn2)["mode"] == "upload"
        send_json(conn2, {"ok": True})
        manifest = read_json(conn2)
        for file_meta in manifest["files"]:
            remaining = file_meta["size"]
            content = b""
            while remaining > 0:
                chunk = read_bytes_frame_capturing_chunk_sizes(conn2, chunk_sizes)
                content += chunk
                remaining -= len(chunk)
            received_files[file_meta["path"]] = content
        send_json(conn2, {"ok": True})
        conn2.close()

        conn3, _ = server_sock.accept()
        send_json(conn3, fake_nonce)
        assert read_json(conn3)["mode"] == "run"
        send_json(conn3, {"ok": True})

        import msgpack

        header = conn3.recv(4)
        body_len = int.from_bytes(header, "big")
        body = conn3.recv(body_len)
        request = msgpack.unpackb(body[1:], raw=False)
        assert request["name"] == "__tether_handshake__"

        from tether.marshalling import encode_frame

        conn3.sendall(encode_frame(2, {"id": request["id"], "value": PROTOCOL_VERSION}))
        conn3.settimeout(2.0)
        try:
            conn3.recv(1)
        except OSError:
            pass
        conn3.close()

    server_thread = threading.Thread(target=fake_device, daemon=True)
    server_thread.start()

    # Bigger than MAX_CONTROL_FRAME_SIZE (65536) by a non-round amount, so a
    # correct chunker must emit at least 2 chunks and the final chunk must
    # be a partial, non-65536-sized remainder - exercising both the "full
    # chunk" and "last partial chunk" cases in one file.
    big_bootstrap = "x" * (MAX_CONTROL_FRAME_SIZE + 12345)

    board = _connect_wifi(
        f"127.0.0.1:{port}",
        big_bootstrap,
        {},
        frozenset(),
        {},
        timeout=5.0,
    )

    server_thread.join(timeout=5.0)

    assert received_files["/tether_app.py"] == big_bootstrap.encode()
    assert chunk_sizes, "expected at least one bytes-frame chunk to have been sent"
    assert all(size <= MAX_CONTROL_FRAME_SIZE for size in chunk_sizes), chunk_sizes
    assert len(chunk_sizes) >= 2, (
        "expected the oversized file to be split into multiple frames, "
        f"got chunk sizes {chunk_sizes}"
    )
    assert board is not None


def test_board_reconnect_over_wifi_closes_the_previous_connection_first():
    # Final-review finding: _connect_wifi's dial() never closed the
    # PREVIOUS run-mode WifiStream before opening a new one. boot.py's
    # accept-loop is strictly sequential (one connection fully handled
    # before the next accept()) - calling board.reconnect() while the old
    # connection is still open (not explicitly closed by the caller first)
    # left the device's _handle_run blocked forever reading from the stale
    # connection, so it never got back to accept() to serve the new one -
    # a hang/timeout on the reconnect attempt.
    #
    # Hand-rolled fake device over a real socket (matching this file's
    # other wifi tests), tracking accept() count and whether the FIRST
    # run-mode connection was ever actually seen to close - a still-broken
    # PC side would leave it open forever, so the device's own wait for
    # that close is itself bounded (not an indefinite block) so a
    # regression here produces a clean test failure, not a hung test
    # process. Answers RPC calls manually (no real server-side Dispatcher)
    # so the same function that answers calls also owns the socket for the
    # close-detection read that follows - no second reader thread to race.
    import json
    import struct
    import threading

    import msgpack

    from tether.marshalling import encode_frame

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

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    results: dict = {"accepted": 0, "first_run_conn_closed": None}

    fake_nonce = {"nonce": "00" * 16}

    def serve_status_and_upload_then_open_run_conn():
        conn, _addr = listener.accept()
        results["accepted"] += 1
        send_json(conn, fake_nonce)
        assert read_json(conn)["mode"] == "status"
        send_json(conn, {"ok": True})
        send_json(conn, {"tether_app_hash": None})  # always force a re-upload, keeps this simple
        conn.close()

        conn2, _addr = listener.accept()
        results["accepted"] += 1
        send_json(conn2, fake_nonce)
        assert read_json(conn2)["mode"] == "upload"
        send_json(conn2, {"ok": True})
        manifest = read_json(conn2)
        for file_meta in manifest["files"]:
            remaining = file_meta["size"]
            while remaining > 0:
                remaining -= len(read_bytes_frame(conn2))
        send_json(conn2, {"ok": True})
        conn2.close()

        conn3, _addr = listener.accept()
        results["accepted"] += 1
        send_json(conn3, fake_nonce)
        assert read_json(conn3)["mode"] == "run"
        send_json(conn3, {"ok": True})
        return conn3

    def serve_run_connection_until_closed(conn, *, wait_for_close_timeout):
        """Answers any RPC calls (handshake, _mock_read_temp) that arrive,
        then, once the peer stops sending anything, waits (bounded) to see
        the connection actually close. Returns True if it did, False if the
        wait timed out first - a still-broken PC side never closes this
        connection, so this is exactly how the bug would manifest.
        """
        conn.settimeout(wait_for_close_timeout)
        while True:
            try:
                header = conn.recv(4)
            except OSError:
                return False
            if not header:
                return True
            body_len = int.from_bytes(header, "big")
            body = conn.recv(body_len)
            request = msgpack.unpackb(body[1:], raw=False)
            value = {"__tether_handshake__": PROTOCOL_VERSION, "_mock_read_temp": 21.5}.get(
                request["name"]
            )
            conn.sendall(encode_frame(2, {"id": request["id"], "value": value}))

    def fake_device():
        first_run_conn = serve_status_and_upload_then_open_run_conn()
        closed = serve_run_connection_until_closed(first_run_conn, wait_for_close_timeout=6.0)
        results["first_run_conn_closed"] = closed
        first_run_conn.close()

        if not closed:
            # A real device would be stuck here forever - nothing more to
            # prove; the main thread's board.reconnect() call already
            # timed out/failed by this point.
            return

        second_run_conn = serve_status_and_upload_then_open_run_conn()
        serve_run_connection_until_closed(second_run_conn, wait_for_close_timeout=2.0)
        second_run_conn.close()

    device_thread = threading.Thread(target=fake_device, daemon=True)
    device_thread.start()

    board = connect(f"wifi:127.0.0.1:{port}", timeout=3.0)
    assert board._mock_read_temp() == 21.5

    board.reconnect()  # must not hang/timeout - this is the fix under test
    assert board._mock_read_temp() == 21.5

    device_thread.join(timeout=15.0)
    assert results["first_run_conn_closed"] is True, results
    assert results["accepted"] == 6, results


def test_connect_wifi_respects_timeout_when_run_mode_preamble_is_never_acked():
    # Final-review finding: wifi_transport.connect() sets
    # sock.settimeout(None) (correct for the live Dispatcher phase) BEFORE
    # send_preamble() does its own blocking ack read. A device that accepts
    # the TCP connection but never answers the preamble (busy, hung, or
    # exactly the Fix-5 deadlock scenario before that fix landed) made
    # mcu.connect() hang forever instead of respecting the `timeout`
    # parameter.
    #
    # Fake device answers status/upload normally (reporting a hash that
    # already matches, so the client skips straight to the run connection)
    # but then, on the run connection, reads the preamble frame and simply
    # never responds - exactly "accepts the connection but never acks".
    # Bounded via pytest's own default test timeout behavior: if this
    # regresses back to an unbounded hang, this test will just hang too -
    # that's the point (an actual regression here SHOULD make this test
    # visibly stuck, not silently pass) - but the assertion below is what
    # proves correctness when the fix is in place: a small timeout raises
    # within a bounded time, comfortably inside any reasonable suite
    # timeout.
    import json
    import struct
    import threading
    import time

    from tether.connection import _hash_bundle

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

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    bootstrap = "irrelevant bootstrap content"
    bundle_hash = _hash_bundle(bootstrap)

    def fake_device():
        # status - report the matching hash so the client skips upload
        # entirely and goes straight to the run connection.
        conn, _addr = listener.accept()
        send_json(conn, {"nonce": "00" * 16})
        assert read_json(conn)["mode"] == "status"
        send_json(conn, {"ok": True})
        send_json(conn, {"tether_app_hash": bundle_hash})
        conn.close()

        # run - accept the connection, then do nothing: no nonce, no ack,
        # no close. Exactly the scenario the fix must bound - the client's
        # send_preamble() blocks reading the nonce it never gets, same as
        # it would previously have blocked reading the ack.
        conn2, _addr = listener.accept()
        time.sleep(10.0)  # outlives the test's own bounded timeout below
        conn2.close()

    device_thread = threading.Thread(target=fake_device, daemon=True)
    device_thread.start()

    started = time.monotonic()
    with pytest.raises(Exception):  # noqa: B017 - any exception is correct; a hang is the failure
        _connect_wifi(
            f"127.0.0.1:{port}",
            bootstrap,
            {},
            frozenset(),
            {},
            timeout=1.5,
        )
    elapsed = time.monotonic() - started

    # Generous upper bound (well under the fake device's own 10s sleep, and
    # comfortably above the 1.5s timeout to absorb scheduling slack) -
    # proves this raised because the timeout fired, not because the device
    # eventually responded or the test got lucky on timing.
    assert elapsed < 5.0, f"mcu.connect() took {elapsed:.1f}s to raise - timeout wasn't respected"


def test_connect_ble_fails_fast_when_the_device_connects_but_never_answers():
    # Final-review finding (F1). A board that accepts the BLE link and then
    # goes silent used to hang _connect_ble forever (BleStream.read was an
    # unbounded queue.get with no timeout anywhere above it) - the exact
    # hang wifi's own preamble ack read was already fixed for. `timeout`
    # must reach the control exchange.
    import sys
    import time
    import types
    from unittest.mock import patch

    from tether.connection import _connect_ble

    class _SilentBleakClient:
        """Connects, subscribes, accepts writes - and never notifies
        anything back. The "device is up but its session loop is wedged"
        case, which no connect-level timeout alone can catch.
        """

        mtu_size = 200

        def __init__(self, address, disconnected_callback=None):
            self.address = address

        async def connect(self, timeout=10.0):
            pass

        async def start_notify(self, char_uuid, callback):
            pass

        async def write_gatt_char(self, char_uuid, data, response=True):
            pass

        async def disconnect(self):
            pass

    fake_bleak = types.ModuleType("bleak")
    fake_bleak.BleakClient = _SilentBleakClient

    started = time.monotonic()
    with (
        patch.dict(sys.modules, {"bleak": fake_bleak}),
        pytest.raises(OSError, match="timed out"),
    ):
        _connect_ble("AA:BB:CC:DD:EE:FF", "print('hi')\n", {}, frozenset(), {}, timeout=0.3)
    assert time.monotonic() - started < 5.0, "connect() hung well past its own timeout"


def test_connect_ble_refuses_to_strand_control_bytes_at_the_run_handover():
    # Final-review finding (F8). The device side explicitly seeds its
    # run-mode reader with _conn.take_buffer() (provisioning.py) so nothing
    # it over-read during the synchronous preamble exchange is lost. The PC
    # side has no equivalent drain - it hands the raw stream to the
    # Dispatcher, so anything left in the control channel's buffer would be
    # silently dropped and the handshake would hang with no diagnosis. The
    # buffer is provably empty today; the assertion is what keeps it that
    # way if either side's chunking ever changes.
    import json
    import sys
    import types
    from unittest.mock import patch

    from tether.connection import _connect_ble, _hash_bundle

    bootstrap = "print('hi')\n"
    bundle_hash = _hash_bundle(bootstrap)

    class _CoalescingDevice:
        """Answers status normally (hash already matches, so no upload),
        then packs the run-mode ack and trailing bytes into ONE
        notification - exactly the shape a chunking change could produce,
        and the shape that strands bytes in the channel's buffer.
        """

        def __init__(self, client):
            self._client = client
            self._buffer = b""

        @staticmethod
        def _frame(obj):
            body = json.dumps(obj).encode()
            return len(body).to_bytes(4, "big") + body

        def send_nonce(self):
            # The real device sends its one-per-connection nonce
            # proactively, before reading anything - not in response to a
            # write. This fake mirrors that from start_notify (BLE's
            # closest analogue to "connection established").
            self._client._notify_cb(None, bytearray(self._frame({"nonce": "00" * 16})))

        def feed(self, data):
            self._buffer += bytes(data)
            while len(self._buffer) >= 4:
                length = int.from_bytes(self._buffer[:4], "big")
                if len(self._buffer) < 4 + length:
                    return
                body = self._buffer[4 : 4 + length]
                self._buffer = self._buffer[4 + length :]
                self._handle(json.loads(body))

        def _handle(self, preamble):
            notify = self._client._notify_cb
            ack = self._frame({"ok": True})
            if preamble["mode"] == "run":
                # ONE notification carrying the ack AND bytes that belong
                # to the Dispatcher - the control channel reads the ack out
                # of it and strands the rest in its own buffer.
                notify(None, bytearray(ack + b"stray-rpc-bytes"))
                return
            notify(None, bytearray(ack))
            if preamble["mode"] == "status":
                notify(
                    None,
                    bytearray(
                        self._frame(
                            {
                                "protocol_version": PROTOCOL_VERSION,
                                "tether_app_hash": bundle_hash,
                                "free_heap": 100000,
                                "uptime_ms": 500,
                                "ip": "aa:bb:cc:dd:ee:ff",
                            }
                        )
                    ),
                )

    class _FakeBleakClient:
        mtu_size = 200

        def __init__(self, address, disconnected_callback=None):
            self._notify_cb = None
            self._device = _CoalescingDevice(self)

        async def connect(self, timeout=10.0):
            pass

        async def start_notify(self, char_uuid, callback):
            self._notify_cb = callback
            self._device.send_nonce()

        async def write_gatt_char(self, char_uuid, data, response=True):
            self._device.feed(data)

        async def disconnect(self):
            pass

    fake_bleak = types.ModuleType("bleak")
    fake_bleak.BleakClient = _FakeBleakClient

    with (
        patch.dict(sys.modules, {"bleak": fake_bleak}),
        pytest.raises(AssertionError, match="run-mode handover"),
    ):
        _connect_ble("AA:BB:CC:DD:EE:FF", bootstrap, {}, frozenset(), {}, timeout=2.0)


def test_frame_auth_failure_gets_a_re_provision_hint():
    # A board provisioned before per-frame authentication shipped fails
    # with a bare FrameAuthenticationError, which says nothing about the
    # actual cause (a breaking wire change) or the fix.
    hinted = _hint_if_frame_auth_failure(FrameAuthenticationError("tag mismatch"))

    assert isinstance(hinted, FrameAuthenticationError)
    assert "tag mismatch" in str(hinted)
    assert "tether provision wifi" in str(hinted)


def test_frame_auth_failure_hint_survives_the_run_mode_disconnect_wrapping():
    # Run mode never sees the FrameAuthenticationError itself: Dispatcher's
    # reader thread collapses any read failure into MCUDisconnectedError,
    # keeping only the type name and message as text.
    wrapped = MCUDisconnectedError("transport read failed: FrameAuthenticationError: tag mismatch")

    hinted = _hint_if_frame_auth_failure(wrapped)

    assert isinstance(hinted, MCUDisconnectedError)
    assert "tether provision wifi" in str(hinted)


def test_unrelated_exceptions_are_returned_untouched():
    disconnect = MCUDisconnectedError("transport closed")
    other = ValueError("nothing to do with framing")

    assert _hint_if_frame_auth_failure(disconnect) is disconnect
    assert _hint_if_frame_auth_failure(other) is other
