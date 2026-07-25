"""MicroPython-side uasyncio dispatch loop.

Runs the reentrant request/response loop (request-ID tagged frames), the
periodic @mcu.loop tasks, and the heartbeat-on-yield behavior described in
DESIGN.md § Call semantics and § Wire protocol.

NOTE: this file must run under real MicroPython (uasyncio, not asyncio).
Do not import PC-only stdlib here.

Wire message types. Must exactly match the PC-side dispatch (chunk 7) - no
shared import is possible between tether (PC) and tether_runtime (MCU),
they never share a runtime. If these change, chunk 7 must change to match.
"""

import io
import sys

import uasyncio as asyncio
import umsgpack

# NOTE: `import umsgpack`, not `from tether_runtime import umsgpack` - this
# module runs on-device where tether_runtime/'s contents are deployed flat
# (dispatch.py and umsgpack/ as siblings), not as a Python package. The
# `tether_runtime` wrapper only exists in this repo for our own PC-side
# source organization; it's stripped by chunk 10's bundling step.

MSG_CALL = 1
MSG_RESULT = 2
MSG_ERROR = 3
MSG_HEARTBEAT = 4

_LENGTH_SIZE = 4
_MSG_TYPE_SIZE = 1

# Mirrors tether/marshalling.MAX_FRAME_SIZE (chunk 2) - see
# tether_runtime/umsgpack/VENDORED.md's security note for why this bound
# must exist before umsgpack ever touches the frame body. Sized for real
# hardware: a stock ESP32-WROOM board has no PSRAM and only ~100-200KB
# typical free heap under MicroPython, so the old 1 MiB value let a single
# readexactly() attempt an allocation that would MemoryError well before
# reaching the nominal cap - reachable remotely now that an unauthenticated
# wifi transport exists. 64 KiB comfortably fits a stock board's free heap
# with headroom for the rest of the runtime, while still leaving real room
# for legitimate payloads (small file contents, lists). MUST be updated
# together with tether/marshalling.MAX_FRAME_SIZE or the two sides of the
# wire protocol disagree on what's rejected.
MAX_FRAME_SIZE = 1 << 16  # 64 KiB

# Default heartbeat cadence for a handler registered without an explicit
# interval - DESIGN.md's heartbeat design is "automatic by default", not
# opt-in per function.
DEFAULT_HEARTBEAT_INTERVAL_MS = 1000

# Cap on concurrently in-flight MSG_CALL handler tasks. Each one is a
# uasyncio task holding its own stack/locals until it finishes, so with no
# bound a peer sending CALL frames faster than handlers complete (malicious
# or buggy, now reachable over the unauthenticated wifi listener - same
# security context as MAX_FRAME_SIZE above) can exhaust the board's RAM.
# 8 is generous for interactive RPC use (far more concurrent calls than a
# typical script issues) while still being a meaningful, small bound
# relative to what a stock board's RAM can hold in flight.
MAX_CONCURRENT_CALLS = 8


class RemoteError(Exception):
    """Raised in call_pc() when the other side's handler raised, or when
    the other side had no handler registered for the called name at all.
    Wraps rather than re-raising the original exception type, since the
    raising side's exception classes aren't guaranteed to exist or match
    constructor signatures on this side. Mirrors tether.errors.RemoteError
    on the PC side (DESIGN.md § Wire protocol) - kept as a separate class
    since PC and MCU code never share a runtime to import a common base from.
    """

    def __init__(self, remote_type, message, remote_traceback=""):
        super().__init__(message)
        self.remote_type = remote_type
        self.remote_traceback = remote_traceback


class _Pending:
    """State for one in-flight call_pc() request. A plain dict here would
    let the two outcome shapes (result vs. error) mix ambiguously with no
    static check that they're mutually exclusive - this makes the three
    states (waiting / done-with-result / done-with-error) explicit.
    """

    __slots__ = (
        "done",
        "error_message",
        "error_traceback",
        "error_type",
        "event",
        "is_error",
        "result",
    )

    def __init__(self):
        self.event = asyncio.Event()
        self.done = False
        self.is_error = False
        self.error_type = None
        self.error_message = None
        self.error_traceback = None
        self.result = None

    def resolve_result(self, value):
        self.done = True
        self.result = value
        self.event.set()

    def resolve_error(self, error_type, error_message, error_traceback):
        self.done = True
        self.is_error = True
        self.error_type = error_type
        self.error_message = error_message
        self.error_traceback = error_traceback
        self.event.set()

    def heartbeat(self):
        self.event.set()  # wake the waiter to reset its wait; not done


def _format_exception(exc):
    buf = io.StringIO()
    sys.print_exception(exc, buf)
    return buf.getvalue()


async def _maybe_await(result):
    """`fn()` for an `async def fn` returns a generator rather than running
    the body - await it; a plain sync function's return value passes
    through unchanged. Used for both handler results and periodic loop
    functions, which may be either.
    """
    if hasattr(result, "send"):
        return await result
    return result


def _encode_frame(msg_type, payload):
    body = umsgpack.dumps(payload)
    total = _MSG_TYPE_SIZE + len(body)
    frame = bytearray(_LENGTH_SIZE + total)
    frame[0:_LENGTH_SIZE] = total.to_bytes(_LENGTH_SIZE, "big")
    frame[_LENGTH_SIZE] = msg_type
    frame[_LENGTH_SIZE + _MSG_TYPE_SIZE :] = body
    return frame


async def _read_frame(reader):
    """Read one length-prefixed frame from `reader` (anything with an async
    `readexactly(n)`, e.g. uasyncio.StreamReader). Always bounds `length`
    before reading the body, and always decodes via umsgpack.loads() on the
    resulting complete bytes - never umsgpack.load() on the stream directly.
    See umsgpack/VENDORED.md's security note for why that distinction matters.
    """
    header = await reader.readexactly(_LENGTH_SIZE)
    length = int.from_bytes(header, "big")
    if length > MAX_FRAME_SIZE:
        raise ValueError(f"frame too large: declared {length} bytes, max is {MAX_FRAME_SIZE}")
    body = await reader.readexactly(length)
    msg_type = body[0]
    payload = umsgpack.loads(body[_MSG_TYPE_SIZE:])
    return msg_type, payload


class Dispatcher:
    """Reentrant request/response dispatch loop over a (reader, writer) pair.

    `call_pc` is the contract chunk 4's generated stubs call
    (`call_pc(name, *args)` - see CHUNKS.md chunk 6's contract note).
    `register` wires up a local handler so incoming calls for `name` (from
    the other side, e.g. `@mcu.export` functions) get routed and answered.
    """

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer
        self._next_id = 0
        self._pending = {}  # request id -> _Pending
        self._handlers = {}  # name -> (callable, heartbeat_interval_ms)
        self._loops = []  # list of (fn, interval_ms)
        self._in_flight_calls = 0
        self._call_slot_free = asyncio.Event()
        self._call_slot_free.set()

    def register(self, name, handler, heartbeat_interval_ms=DEFAULT_HEARTBEAT_INTERVAL_MS):
        self._handlers[name] = (handler, heartbeat_interval_ms)

    def register_loop(self, fn, interval_ms):
        self._loops.append((fn, interval_ms))

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    async def _send(self, msg_type, payload):
        self._writer.write(_encode_frame(msg_type, payload))
        await self._writer.drain()

    async def call_pc(self, name, *args):
        req_id = self._new_id()
        pending = _Pending()
        self._pending[req_id] = pending
        try:
            await self._send(MSG_CALL, {"id": req_id, "name": name, "args": list(args)})
            while True:
                await pending.event.wait()
                pending.event.clear()
                if pending.done:
                    break
                # else: was a heartbeat - loop back and keep waiting. (No
                # timeout enforcement here yet: that's paired with the
                # caller providing a timeout value, not this chunk's scope -
                # see CHUNKS.md chunk 7, which owns per-call timeouts.)
        finally:
            del self._pending[req_id]
        if pending.is_error:
            raise RemoteError(pending.error_type, pending.error_message, pending.error_traceback)
        return pending.result

    async def run(self):
        for fn, interval_ms in self._loops:
            asyncio.create_task(self._run_loop(fn, interval_ms))
        while True:
            msg_type, payload = await _read_frame(self._reader)
            if msg_type == MSG_CALL:
                # Backpressure at the read loop itself: hold off reading (and
                # spawning) further calls once MAX_CONCURRENT_CALLS handler
                # tasks are already outstanding, rather than spawning without
                # bound. Simpler than a semaphore-style primitive and needs
                # nothing beyond the Event already used elsewhere here.
                while self._in_flight_calls >= MAX_CONCURRENT_CALLS:
                    self._call_slot_free.clear()
                    await self._call_slot_free.wait()
                self._in_flight_calls += 1
                asyncio.create_task(self._handle_call(payload))
            elif msg_type in (MSG_RESULT, MSG_ERROR, MSG_HEARTBEAT):
                self._route_response(msg_type, payload)

    def _route_response(self, msg_type, payload):
        pending = self._pending.get(payload.get("id"))
        if pending is None:
            return
        if msg_type == MSG_HEARTBEAT:
            pending.heartbeat()
        elif msg_type == MSG_ERROR:
            pending.resolve_error(
                payload.get("type"), payload.get("message"), payload.get("traceback", "")
            )
        else:
            pending.resolve_result(payload.get("value"))

    async def _handle_call(self, payload):
        # Outer try/finally always releases this task's MAX_CONCURRENT_CALLS
        # slot (incremented by run() before spawning this task), even if a
        # handler raises or the frame references an unregistered name -
        # otherwise a slot would leak per such call, same failure shape as
        # the pending-request leak this file also fixes elsewhere.
        try:
            req_id = payload["id"]
            name = payload["name"]
            args = payload.get("args", [])

            entry = self._handlers.get(name)
            if entry is None:
                await self._send(
                    MSG_ERROR,
                    {
                        "id": req_id,
                        "type": "LookupError",
                        "message": f"no handler named {name!r}",
                        "traceback": "",
                    },
                )
                return
            handler, heartbeat_interval_ms = entry

            heartbeat_task = None
            if heartbeat_interval_ms:
                heartbeat_task = asyncio.create_task(
                    self._heartbeat_ticker(req_id, heartbeat_interval_ms)
                )
            try:
                result = await _maybe_await(handler(*args))
            except Exception as exc:  # noqa: BLE001 - any handler exception must become MSG_ERROR
                await self._send(
                    MSG_ERROR,
                    {
                        "id": req_id,
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": _format_exception(exc),
                    },
                )
                return
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
            await self._send(MSG_RESULT, {"id": req_id, "value": result})
        finally:
            self._in_flight_calls -= 1
            self._call_slot_free.set()

    async def _heartbeat_ticker(self, req_id, interval_ms):
        # Runs as a task alongside the handler, not interleaved into it -
        # see DESIGN.md's heartbeat design: a handler that never yields
        # (blocks synchronously) starves this task too, same as it starves
        # everything else, which correctly means it gets no heartbeat.
        while True:
            await asyncio.sleep_ms(interval_ms)
            await self._send(MSG_HEARTBEAT, {"id": req_id})

    async def _run_loop(self, fn, interval_ms):
        while True:
            await asyncio.sleep_ms(interval_ms)
            await _maybe_await(fn())
