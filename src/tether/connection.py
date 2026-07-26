"""`mcu.connect(...)` — the wire→probe→use entrypoint. See DESIGN.md § Architecture overview."""

from __future__ import annotations

import hashlib
import inspect
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tether._context import current_board
from tether.dispatch import DEFAULT_TIMEOUT, Dispatcher
from tether.errors import ProtocolVersionError
from tether.slicer import generate_pc_stubs, slice_mcu_bound

# Bumped whenever the wire protocol or dispatch runtime contract changes in
# a way that would break compatibility with an already-uploaded on-device
# bundle. Checked via a handshake call on every connect() (not hash-gated -
# the user's own code hash can be unchanged while the underlying runtime
# still changed between library versions). See DESIGN.md § Wire protocol.
PROTOCOL_VERSION = 1

_HANDSHAKE_NAME = "__tether_handshake__"


def generate_bootstrap(sliced_source: str, stubs_source: str) -> str:
    """Assemble the final on-device script: imports the runtime shim +
    dispatch loop, disables MicroPython's Ctrl-C keyboard interrupt on the
    UART (see the module-level comment on `import micropython` below),
    defines the sliced @mcu.export/@mcu.loop functions and the generated
    @pc.export proxy stubs, wires a Dispatcher over a pre-set
    `_tether_stream_override` global if one exists (set by boot.py's wifi
    bridge before exec'ing this script - see provisioning.py), falling
    back to sys.stdin/sys.stdout (wrapped as uasyncio streams) for the
    normal serial path, registers every @mcu.export/@mcu.loop function
    plus the protocol handshake handler, and runs the dispatch loop
    forever.

    Clears mcu_decorators._registrations at the very start, before the
    sliced @mcu.export/@mcu.loop/@pc.export definitions (and their decorator
    applications) run - without this, repeated exec() of the same generated
    script within one interpreter process (the wifi accept-loop does this;
    serial's hardware-reset-based reconnect never does) would accumulate
    duplicate registrations, harmless for plain handlers (a dict) but not
    for @mcu.loop (a list Dispatcher._loops appends to - duplicates spawn
    duplicate background tasks).
    """
    return f"""\
from mcu_decorators import mcu, pc, registered_mcu_functions
import mcu_decorators as _tether_mcu_decorators
_tether_mcu_decorators._registrations.clear()
import dispatch as _tether_dispatch
import uasyncio as _tether_asyncio
import sys as _tether_sys
import micropython as _tether_micropython

# Found against real ESP32 hardware (not reproducible against the PTY
# simulation this was originally checked with under CPython, which only
# established that the interception it saw was a *host-side* PTY line
# discipline artifact - it never proved anything about what real
# MicroPython firmware does with a raw 0x03 byte on a real UART, and that
# turned out to be different): msgpack routinely encodes small integers as
# their own literal byte value, so a call argument that happens to produce
# a raw 0x03 byte anywhere in its frame (e.g. int 3) gets intercepted by
# MicroPython's UART driver as Ctrl-C and raises KeyboardInterrupt *inside
# the running dispatch loop*, killing it. kbd_intr(-1) disables that
# built-in interception so the UART can carry the wire protocol's raw
# binary frames safely - must run before the dispatch loop starts reading
# any real protocol bytes.
_tether_micropython.kbd_intr(-1)

{sliced_source}

{stubs_source}

async def _tether_main():
    _override = globals().get("_tether_stream_override")
    if _override is not None:
        _reader, _writer = _override
    else:
        _reader = _tether_asyncio.StreamReader(_tether_sys.stdin.buffer)
        _writer = _tether_asyncio.StreamWriter(_tether_sys.stdout.buffer, {{}})
    _dispatcher = _tether_dispatch.Dispatcher(_reader, _writer)
    globals()["call_pc"] = _dispatcher.call_pc
    for _name, _fn, _hb_ms, _interval_ms in registered_mcu_functions():
        if _interval_ms is not None:
            _dispatcher.register_loop(_fn, _interval_ms)
        elif _hb_ms is not None:
            _dispatcher.register(_name, _fn, heartbeat_interval_ms=_hb_ms)
        else:
            _dispatcher.register(_name, _fn)
    # Registered last, deliberately: if a user's own @mcu.export function
    # happened to be named "__tether_handshake__" (unlikely, but not
    # prevented anywhere), this must win and can't be silently shadowed by
    # theirs - the reverse order would let a user function break the
    # protocol-version handshake for every future connect().
    _dispatcher.register({_HANDSHAKE_NAME!r}, lambda: {PROTOCOL_VERSION})
    await _dispatcher.run()

_tether_asyncio.run(_tether_main())
"""


class BoardHandle:
    """Returned by `connect()`. Decorated `@mcu.export` functions are
    directly callable (`read_temp()`) and dispatch through whichever board
    is ambient (see tether/_context.py) - `connect()` sets itself as the
    ambient default, covering the common single-board case with zero extra
    ceremony. They're also exposed as bound attributes here
    (`board.read_temp()`) - explicit, bypasses the ambient lookup entirely,
    useful for one-off calls or when it's clearer to name the board
    directly. Both call the same underlying dispatch.

    `with board:` scopes `board` as the ambient default for the block -
    the multi-board disambiguation story (DESIGN.md): connecting a second
    board would otherwise silently shift the ambient default away from the
    first.
    """

    def __init__(
        self,
        dispatcher: Dispatcher,
        export_specs: dict[str, Any],
        *,
        dial: Callable[[], Dispatcher],
    ) -> None:
        self._dispatcher = dispatcher
        # Lets the dispatcher make `self` the ambient board (tether/_context.py)
        # for the duration of handling an incoming call - see Dispatcher.board's
        # own comment (tether/dispatch/__init__.py). Every _connect_*()
        # function's own dial() closure already passes `board` into
        # _start_and_handshake()/sets dispatcher.board directly BEFORE
        # dispatcher.start() runs (closing the real gap: a reconnected
        # device may already be running @mcu.loop tasks that call back
        # immediately) - this assignment only still matters for dial()'s
        # very first-ever call, when no BoardHandle exists yet for the
        # closure variable to hold. Real devices don't send unsolicited
        # calls before the PC's own handshake completes, so that remaining
        # window isn't reachable in practice - kept for correctness, not
        # because it's been hit.
        dispatcher.board = self
        self._export_specs = export_specs
        # Re-runs the same connect flow (transport-specific) to produce a
        # fresh Dispatcher, for reconnect() below.
        self._dial = dial
        # Per-thread stack of context tokens (threading.local, not a plain
        # list) so `with board:` is correct both for nesting (LIFO within
        # one thread) and for the same BoardHandle being entered
        # concurrently from different threads (tether.dispatch.Dispatcher
        # hands reentrant incoming calls to a worker pool) - a shared list
        # would let one thread's __exit__ pop a token that belongs to a
        # different thread's __enter__.
        self._context_tokens = threading.local()

    def __enter__(self) -> BoardHandle:  # noqa: PYI034 - `Self` needs Python 3.11+, project floor is 3.10
        stack = getattr(self._context_tokens, "stack", None)
        if stack is None:
            stack = []
            self._context_tokens.stack = stack
        stack.append(current_board.set(self))
        return self

    def __exit__(self, *exc_info: object) -> None:
        current_board.reset(self._context_tokens.stack.pop())

    def reconnect(self) -> None:
        """Explicit re-attach after MCUDisconnectedError. Never automatic
        (DESIGN.md § Disconnection) - a reconnected board may be in an
        unknown state, so this is always a deliberate call, never triggered
        internally by a failed call_mcu().

        Cached call closures (see __getattr__) resolve `self._dispatcher` at
        call time, so swapping it here is transparent to already-cached
        closures - no need to re-fetch `board.<name>` after reconnecting.
        """
        old_dispatcher = self._dispatcher
        # self._dial() already sets the new dispatcher's .board correctly
        # (the closure variable it reads was updated to `self` back when
        # this BoardHandle was first constructed) - this reconnect() call
        # is exactly the case that mattered: unlike a fresh connect(), the
        # device here may already be running @mcu.loop tasks that call
        # back immediately, before this line would otherwise run.
        self._dispatcher = self._dial()
        self._dispatcher.board = self  # belt-and-suspenders; see __init__'s comment
        # The old dispatcher's reader thread has no way to be interrupted
        # mid blocking-read (Dispatcher doesn't own the transport's own
        # close()) - it stays blocked until the process exits, same
        # already-documented daemon-thread limitation as chunk 7/10's
        # connect()-failure cleanup. Its thread *pool*, however, is
        # unambiguously abandonable right now - shut it down so a reconnect
        # doesn't leak 4 more live threads per call.
        old_dispatcher.close()

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only runs when normal attribute lookup fails - caching
        # the built closure into __dict__ makes the next access resolve
        # through that, without needing to call this at all. Also gives
        # `board.read_temp is board.read_temp` the expected identity, in
        # case any caller relies on it.
        if name not in self._export_specs:
            raise AttributeError(name)
        spec = self._export_specs[name]
        default_timeout = spec.timeout if spec.timeout is not None else DEFAULT_TIMEOUT

        def call(*args: Any, timeout: float | None = default_timeout) -> Any:
            return self._dispatcher.call_mcu(name, *args, timeout=timeout)

        self.__dict__[name] = call
        return call


def _capture_caller() -> tuple[str, Path, dict[str, Any], dict[str, Callable[..., Any]]]:
    """Read the calling file's source (for slicing) and its already-executed
    module globals (for @mcu.export metadata, and @pc.export callables) -
    NOT by re-executing the source (the caller's module is already running;
    connect() is being called from within it), which would double any of
    its side effects (prints, connections opened at import time, etc).
    """
    # inspect.stack()[2]: [0]=this frame, [1]=connect()'s frame, [2]=the
    # real caller - more robust to a future extra layer of indirection here
    # than counting .f_back.f_back links by hand.
    caller_frame = inspect.stack()[2].frame
    filename = caller_frame.f_code.co_filename
    path = Path(filename)
    if not path.is_file():
        raise RuntimeError(
            f"connect() could not read the calling file's source from {filename!r} - "
            "it must be called from a real .py file, not a REPL/exec'd string"
        )

    export_specs = {}
    pc_handlers: dict[str, Callable[..., Any]] = {}
    for name, obj in caller_frame.f_globals.items():
        spec = getattr(obj, "__tether_export__", None)
        if spec is None:
            continue
        if spec.side == "mcu":
            export_specs[name] = spec
        elif spec.side == "pc":
            # The @pc.export function itself (not just its ExportSpec) -
            # registered as a real Dispatcher handler below so an incoming
            # call from the MCU (via its generated proxy stub) actually
            # reaches this live callable, not just a name the slicer/stub
            # generator knew about at bundle-build time.
            pc_handlers[name] = obj
    return path.read_text(encoding="utf-8"), path.parent, export_specs, pc_handlers


def _hash_bundle(bootstrap: str) -> str:
    return hashlib.sha256(bootstrap.encode()).hexdigest()


def _start_and_handshake(
    stream: Any,
    *,
    timeout: float,
    mismatch_hint: str,
    pc_handlers: dict[str, Callable[..., Any]],
    board: BoardHandle | None,
) -> Dispatcher:
    """Start a Dispatcher over an already-connected stream and perform the
    protocol-version handshake - shared by every transport's dial() closure
    once it has a live stream, so the version-check logic exists in exactly
    one place. `mismatch_hint` is the transport-specific remediation text
    appended to a version-mismatch error (e.g. serial can suggest clearing
    the on-device hash sentinel to force a re-upload; wifi/BLE can't, since
    they never upload).

    `pc_handlers` (@pc.export functions, from _capture_caller()) are
    registered before start() - registration only touches a plain dict, no
    race with the reader thread either way, but doing it first keeps the
    "fully ready before this dispatcher does anything" ordering simple to
    reason about.

    `board`: the owning BoardHandle if it already exists (reconnect()'s
    dial() calls pass it), `None` on a fresh connect()'s very first dial()
    call (BoardHandle can't exist before its own constructor has run at
    least once) - see _connect_mock's matching comment for why closing this
    gap for reconnect() specifically matters.
    """
    dispatcher = Dispatcher(stream, stream)
    dispatcher.board = board
    for name, handler in pc_handlers.items():
        dispatcher.register(name, handler)
    dispatcher.start()

    version = dispatcher.call_mcu(_HANDSHAKE_NAME, timeout=timeout)
    if version != PROTOCOL_VERSION:
        raise ProtocolVersionError(
            f"on-device runtime speaks protocol version {version}, "
            f"this library expects {PROTOCOL_VERSION} - {mismatch_hint}"
        )
    return dispatcher


def _connect_mock(
    source: str,
    base_dir: Path,
    export_specs: dict[str, Any],
    pc_handlers: dict[str, Callable[..., Any]],
) -> BoardHandle:
    from tether.transports.mock import MockTransport

    # Read by dial() below, assigned once the BoardHandle exists - `None`
    # only for dial()'s very first-ever call (BoardHandle can't exist
    # before its own constructor has run at least once). Closing this gap
    # for reconnect() specifically matters: unlike the first connect(), a
    # reconnected device may already be running @mcu.loop background tasks
    # that call back immediately, before BoardHandle.__init__'s own
    # post-hoc `dispatcher.board = self` (connection.py) would otherwise
    # run - setting it here, inside dial() itself and before
    # dispatcher.start(), closes that window instead of just documenting
    # it as unreachable.
    board: BoardHandle | None = None

    def dial() -> Dispatcher:
        # A fresh MockTransport per dial (including on reconnect) - each
        # instance owns its own thread/event loop/queues (see mock.py), so
        # boards never share dispatch state, and reconnecting one board
        # never touches another's.
        transport = MockTransport(source, base_dir=base_dir)
        reader, writer = transport.start()
        dispatcher = Dispatcher(reader, writer)
        dispatcher.board = board
        for name, handler in pc_handlers.items():
            dispatcher.register(name, handler)
        dispatcher.start()
        return dispatcher

    board = BoardHandle(dial(), export_specs, dial=dial)
    return board


def _gather_runtime_bundle(
    bootstrap: str, bundle_hash: str
) -> tuple[dict[str, bytes], tuple[str, ...]]:
    """Every file a fresh board needs: the whole tether_runtime library
    (dispatch.py, mcu_decorators.py, vendored umsgpack) plus this
    connection's own sliced app and hash sentinel. Shared between serial's
    _upload_if_needed and wifi's upload mode (_connect_wifi) - both need
    the exact same file set, so this is the one place that gathers it.

    Derived from disk rather than hand-listed - a new file landing in
    tether_runtime/ (e.g. a future umsgpack helper) is picked up
    automatically instead of silently missing from the upload until an
    on-device ImportError surfaces it. __init__.py is the one PC-side-only
    marker file (see its own docstring) and is excluded.
    """
    runtime_dir = Path(__file__).resolve().parents[1] / "tether_runtime"
    runtime_paths = [
        p for p in sorted(runtime_dir.rglob("*.py")) if p != runtime_dir / "__init__.py"
    ]
    runtime_files = {
        f"/{p.relative_to(runtime_dir).as_posix()}": p.read_bytes() for p in runtime_paths
    }
    runtime_dirs = tuple(
        sorted(
            {
                f"/{p.parent.relative_to(runtime_dir).as_posix()}"
                for p in runtime_paths
                if p.parent != runtime_dir
            }
        )
    )
    files = {
        **runtime_files,
        "/tether_app.py": bootstrap.encode(),
        "/.tether_hash": bundle_hash.encode(),
    }
    return files, runtime_dirs


def _upload_if_needed(
    ser: Any, serial_transport: Any, bootstrap: str, bundle_hash: str, *, timeout: float
) -> None:
    existing_hash = serial_transport.read_file(ser, "/.tether_hash", timeout=timeout)
    if existing_hash is not None and existing_hash.decode() == bundle_hash:
        return

    files, runtime_dirs = _gather_runtime_bundle(bootstrap, bundle_hash)
    serial_transport.write_files(ser, files, dirs=runtime_dirs, timeout=timeout)


def _connect_serial(
    port_spec: str,
    bootstrap: str,
    export_specs: dict[str, Any],
    exported_names: frozenset[str],
    pc_handlers: dict[str, Callable[..., Any]],
    *,
    timeout: float = 10.0,
) -> BoardHandle:
    # See _connect_mock's matching comment: None only for dial()'s very
    # first-ever call, assigned once the BoardHandle exists so subsequent
    # dial() calls (from reconnect()) can pass it into
    # _start_and_handshake before dispatcher.start() runs.
    board: BoardHandle | None = None

    def dial() -> Dispatcher:
        """One full connect-or-reconnect attempt: rediscover the port (if
        "auto"), upload-if-needed, restart the app fresh, handshake. Called
        both for the initial connect below and, later, from
        BoardHandle.reconnect() - reconnecting re-runs exactly this, nothing
        less (DESIGN.md § Disconnection: a reconnected board may be in an
        unknown state, so "restart fresh" is the only safe assumption
        either way).
        """
        import serial as pyserial

        from tether.transports import serial as serial_transport

        # Fail fast, at connect() time, on a decorated-but-never-sliced
        # function: _capture_caller() finds @mcu.export functions by
        # scanning already-executed live objects (works for any control
        # flow, since it runs after the module has finished executing),
        # while the AST slicer only recognizes plain top-level
        # `def`/`async def` statements (matches DESIGN.md's
        # statically-analyzable decorator API constraint). A function
        # decorated inside `if`/`try`/etc would pass the former check but
        # never get sliced or registered on-device - without this check,
        # calling it would just hang until MCUTimeoutError, an
        # unrelated-looking error far from the actual cause.
        unsliced = export_specs.keys() - exported_names
        if unsliced:
            raise RuntimeError(
                f"{sorted(unsliced)} are decorated with @mcu.export/@mcu.loop but weren't "
                "found by static analysis of the source - decorated functions must be plain "
                "top-level `def`/`async def` statements, not conditionally defined "
                "(DESIGN.md § Standing design constraint)"
            )

        port = serial_transport.discover() if port_spec in ("", "auto") else port_spec
        # Finite read timeout for the raw-REPL phase - `_read_until`'s
        # deadline check only fires *between* reads, so a blocking
        # (timeout=None) read would make every timeout= parameter downstream
        # cosmetic if the device never responds (mid-boot, wrong baud,
        # crashed). Switched to blocking (timeout=None) below, once the
        # Dispatcher takes over: SerialStream's "empty read means
        # disconnected" contract depends on reads that block for real data
        # rather than waking on an idle timeout with nothing to report,
        # which finite timeouts would otherwise trigger spuriously during
        # any normal idle period with no in-flight calls.
        ser = pyserial.Serial(port, baudrate=115200, timeout=1.0)
        try:
            # Hardware-reset before any raw-REPL interaction, every time -
            # found against real hardware: a board already running a
            # PREVIOUS connect()'s dispatch loop can no longer be recovered
            # via Ctrl-C alone once micropython.kbd_intr(-1) is active
            # on-device and push_raw_repl's wait=False stopped sending the
            # raw-REPL exit sequence (see reset_board's own docstring for
            # the full mechanism). Harmless on a board that's already idle -
            # just reboots it again into the same ready state.
            serial_transport.reset_board(ser)

            # All raw-REPL work happens BEFORE the Dispatcher's reader
            # thread ever starts - the two must never run concurrently
            # against the same serial connection, or they'd race each other
            # for bytes (the reader thread doesn't know about raw-REPL
            # framing, and raw-REPL's own reads aren't coordinated with
            # it). Once the app is confirmed started, raw-REPL is never
            # touched again for this connection.
            bundle_hash = _hash_bundle(bootstrap)
            _upload_if_needed(ser, serial_transport, bootstrap, bundle_hash, timeout=timeout)

            # Always (re)start fresh rather than trying to detect "is it
            # already running" - that detection would itself need to read
            # the connection, which is exactly the race this ordering
            # avoids. A previously-running instance from an earlier
            # connect()/reconnect() in this session gets interrupted and
            # replaced; DESIGN.md's "fail loud, no silent retry" disconnect
            # philosophy already treats device-side state as not something
            # to preserve across reconnects.
            serial_transport.push_raw_repl(ser, b"import tether_app\n", wait=False, timeout=timeout)

            ser.timeout = None
            stream = serial_transport.SerialStream(ser)
            dispatcher = _start_and_handshake(
                stream,
                timeout=timeout,
                mismatch_hint="re-upload by clearing /.tether_hash on the device, or update tether",
                pc_handlers=pc_handlers,
                board=board,
            )
        except BaseException:
            # No Dispatcher.stop() exists yet (chunk 7's known limitation) -
            # if the reader thread already started, it stays blocked on ser
            # until the process exits (a daemon thread, so at least that
            # doesn't hang the process). Closing ser here still matters: it
            # releases the OS handle so an immediate connect()/reconnect()
            # retry on the same port doesn't fail (exclusive access on some
            # platforms) or race a second reader against the first on
            # others.
            ser.close()
            raise

        return dispatcher

    board = BoardHandle(dial(), export_specs, dial=dial)
    return board


def _connect_wifi(
    rest: str,
    bootstrap: str,
    export_specs: dict[str, Any],
    exported_names: frozenset[str],
    pc_handlers: dict[str, Callable[..., Any]],
    *,
    timeout: float,
    secret: str | None = None,
) -> BoardHandle:
    """Connect over TCP to a boot.py-managed wifi listener. Unlike the
    original design, this now mirrors _connect_serial's shape: slice,
    hash-check, upload-if-needed, then run - just with the hash-check
    piggybacked on a `status`-mode query instead of a direct file read,
    and upload/run as two separate connections instead of one persistent
    raw-REPL session (see the design spec for why).
    """
    import os

    from tether.transports import wifi as wifi_transport

    host, _, port_str = rest.partition(":")
    port = int(port_str) if port_str else wifi_transport.DEFAULT_PORT
    resolved_secret = secret if secret is not None else os.environ.get("TETHER_WIFI_SECRET")

    unsliced = export_specs.keys() - exported_names
    if unsliced:
        raise RuntimeError(
            f"{sorted(unsliced)} are decorated with @mcu.export/@mcu.loop but weren't "
            "found by static analysis of the source - decorated functions must be plain "
            "top-level `def`/`async def` statements, not conditionally defined "
            "(DESIGN.md § Standing design constraint)"
        )

    bundle_hash = _hash_bundle(bootstrap)
    # See _connect_mock's matching comment.
    board: BoardHandle | None = None

    def _query_status(sock: Any) -> dict[str, Any]:
        wifi_transport.send_preamble(sock, "status", resolved_secret)
        return wifi_transport.read_json_frame(sock)

    def _upload(sock: Any) -> None:
        wifi_transport.send_preamble(sock, "upload", resolved_secret)
        # Full bundle (tether_runtime library + app + hash), same set and
        # same dirs-sorted-by-depth-by-the-client convention
        # _upload_if_needed uses for serial - see _gather_runtime_bundle.
        files, dirs = _gather_runtime_bundle(bootstrap, bundle_hash)
        manifest = {
            "dirs": list(dirs),
            "files": [{"path": path, "size": len(content)} for path, content in files.items()],
        }
        wifi_transport.send_json_frame(sock, manifest)
        for content in files.values():
            wifi_transport.send_bytes_frame(sock, content)
        result = wifi_transport.read_json_frame(sock)
        if not result.get("ok", False):
            raise RuntimeError(f"wifi upload failed: {result.get('error')}")

    def dial() -> Dispatcher:
        import socket as socket_module

        status_sock = socket_module.create_connection((host, port), timeout=timeout)
        try:
            status = _query_status(status_sock)
        finally:
            status_sock.close()

        if status.get("tether_app_hash") != bundle_hash:
            upload_sock = socket_module.create_connection((host, port), timeout=timeout)
            try:
                _upload(upload_sock)
            finally:
                upload_sock.close()

        # wifi_transport.connect() (not a bare socket_module.create_connection,
        # unlike status/upload above) - reuses its existing TCP_NODELAY +
        # blocking-timeout-handoff setup, already correct and
        # hardware-verified for the long-lived RPC stream this becomes.
        # The short-lived status/upload connections above don't need that
        # treatment, so a plain socket is simplest for those.
        stream = wifi_transport.connect(host, port, timeout=timeout)
        try:
            wifi_transport.send_preamble(stream._sock, "run", resolved_secret)
            return _start_and_handshake(
                stream,
                timeout=timeout,
                mismatch_hint="update tether or the on-device runtime",
                pc_handlers=pc_handlers,
                board=board,
            )
        except BaseException:
            stream.close()
            raise

    board = BoardHandle(dial(), export_specs, dial=dial)
    return board


def _connect_ble(
    rest: str,
    export_specs: dict[str, Any],
    pc_handlers: dict[str, Callable[..., Any]],
    *,
    timeout: float,
) -> BoardHandle:
    """Connect to an already-running on-device runtime over BLE. Same shape
    as _connect_wifi (no slicing/bundling/upload - DESIGN.md gives BLE "the
    same bootstrap requirement as wifi").
    """
    from tether.transports import ble as ble_transport

    # See _connect_mock's matching comment.
    board: BoardHandle | None = None

    def dial() -> Dispatcher:
        stream = ble_transport.connect(rest, timeout=timeout)
        try:
            return _start_and_handshake(
                stream,
                timeout=timeout,
                mismatch_hint="update tether or the on-device runtime",
                pc_handlers=pc_handlers,
                board=board,
            )
        except BaseException:
            # Matches _connect_serial/_connect_wifi's dial(): a failed
            # handshake must not leak the connection.
            stream.close()
            raise

    board = BoardHandle(dial(), export_specs, dial=dial)
    return board


def connect(address: str, *, timeout: float = 10.0, secret: str | None = None) -> BoardHandle:
    """Slice -> stub -> bundle -> hash-check -> upload -> handshake -> ready.

    `address` scheme selects transport:
      "serial:auto" | "serial:/dev/ttyUSB0" | "wifi:<ip>" | "ble:<addr>" | "mock://"

    `secret` (wifi only): the shared secret configured during
    `tether provision-wifi`. Falls back to the TETHER_WIFI_SECRET
    environment variable if omitted. Required when the device has one
    configured (raises WifiAuthError if missing/wrong); ignored otherwise.

    Auto-detects the calling file's source (must be called from a real .py
    file) - matches the "single file" pitch: no need to pass your own
    source to connect() explicitly.
    """
    scheme, _, rest = address.partition(":")
    source, base_dir, export_specs, pc_handlers = _capture_caller()

    if scheme == "mock":
        board = _connect_mock(source, base_dir, export_specs, pc_handlers)
    elif scheme == "serial":
        sliced = slice_mcu_bound(source, base_dir=base_dir)
        stubs = generate_pc_stubs(source)
        bootstrap = generate_bootstrap(sliced.source, stubs.source)
        port_spec = rest.removeprefix("//")
        board = _connect_serial(
            port_spec,
            bootstrap,
            export_specs,
            sliced.exported_names,
            pc_handlers,
            timeout=timeout,
        )
    elif scheme == "wifi":
        sliced = slice_mcu_bound(source, base_dir=base_dir)
        stubs = generate_pc_stubs(source)
        bootstrap = generate_bootstrap(sliced.source, stubs.source)
        board = _connect_wifi(
            rest,
            bootstrap,
            export_specs,
            sliced.exported_names,
            pc_handlers,
            timeout=timeout,
            secret=secret,
        )
    elif scheme == "ble":
        board = _connect_ble(rest, export_specs, pc_handlers, timeout=timeout)
    else:
        raise NotImplementedError(f"transport scheme {scheme!r} is not implemented yet")

    # Most-recently-connected board becomes the ambient default for
    # directly-callable @mcu.export functions (decorators.py) - a plain
    # `read_temp()` after this just works for the common single-board
    # case, with `with board:` (BoardHandle.__enter__/__exit__ below)
    # available to disambiguate when more than one board is connected.
    current_board.set(board)
    return board
