"""PC-side dispatch: background reader thread + queue, reentrant call routing.

See DESIGN.md § Call semantics. Owns:
  - the reader thread draining the transport into a queue
  - filtering "blocking" calls by request-ID while pumping other in-flight
    requests to a thread pool (reverse-call support)
  - timeout + heartbeat bookkeeping per in-flight request
  - wraps remote exceptions as RemoteError

Wire message types. Must exactly match the MCU-side dispatch
(tether_runtime/dispatch.py, chunk 6) - no shared import is possible
between tether (PC) and tether_runtime (MCU), they never share a runtime.
If these change, chunk 6 must change to match.
"""

from __future__ import annotations

import contextlib
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tether.errors import MCUDisconnectedError, MCUTimeoutError, RemoteError
from tether.marshalling import FrameDecoder, encode_frame

MSG_CALL = 1
MSG_RESULT = 2
MSG_ERROR = 3
MSG_HEARTBEAT = 4

DEFAULT_TIMEOUT = 10.0

# Default heartbeat cadence for a handler registered without an explicit
# interval - mirrors tether_runtime.dispatch.DEFAULT_HEARTBEAT_INTERVAL_MS
# (chunk 6): automatic by default, not opt-in per function.
DEFAULT_HEARTBEAT_INTERVAL = 1.0


class _Pending:
    """State for one in-flight call_mcu() request.

    `outcome` holds either the plain result value or an `Exception`
    instance (`RemoteError` for a wire-reported handler failure,
    `MCUDisconnectedError` if the transport died mid-wait) - the v1 wire
    type set (DESIGN.md § Wire protocol) never includes exception objects,
    so `isinstance(outcome, Exception)` unambiguously distinguishes the two
    without a separate `is_error` flag plus parallel fields.
    """

    __slots__ = ("done", "event", "outcome")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.done = False
        self.outcome: Any = None

    def resolve(self, outcome: Any) -> None:
        # Idempotent: if a real response already resolved this call, a
        # later disconnect-triggered _fail_all_pending() must not clobber
        # it with a spurious error before the caller has woken up and
        # cleaned up self._pending (see Dispatcher._fail_all_pending).
        if self.done:
            return
        self.done = True
        self.outcome = outcome
        self.event.set()

    def heartbeat(self) -> None:
        self.event.set()  # wake the waiter to reset its wait; not done


class Dispatcher:
    """Reentrant request/response dispatch over a (reader, writer) pair.

    `call_mcu` is the plain-sync-feeling blocking call PC code makes into
    MCU-exported functions. `register` wires up a local handler so incoming
    calls for `name` (from the MCU, e.g. `@pc.export` functions) get routed
    and answered on a background thread pool - never on the reader thread,
    so a slow/reentrant handler can't stall reading.
    """

    def __init__(self, reader: Any, writer: Any, *, max_workers: int = 4) -> None:
        self._reader = reader
        self._writer = writer
        self._decoder = FrameDecoder()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._reader_thread = threading.Thread(target=self._run_reader, daemon=True)
        # Set once the reader thread observes the transport is gone - lets
        # any *subsequent* call_mcu() fail immediately instead of writing
        # into a dead transport and waiting out a full timeout for a
        # response that can never arrive. DESIGN.md § Disconnection: fail
        # loud, no silent retry.
        self._disconnected: MCUDisconnectedError | None = None

    def start(self) -> None:
        self._reader_thread.start()

    def close(self) -> None:
        """Shut down this dispatcher's incoming-call worker pool.

        Does NOT stop the reader thread - there's no way to unblock a
        thread mid blocking `reader.read()` without the underlying
        transport's own close(), which this class doesn't own (see
        `_run_reader`'s docstring and connection.py's reconnect()). Safe to
        call on a dispatcher whose reader has already died.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)

    def register(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        heartbeat_interval: float | None = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self._handlers[name] = (handler, heartbeat_interval)

    def _new_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _send(self, msg_type: int, payload: dict[str, Any]) -> None:
        frame = encode_frame(msg_type, payload)
        with self._write_lock:
            self._writer.write(frame)

    def call_mcu(self, name: str, *args: Any, timeout: float | None = DEFAULT_TIMEOUT) -> Any:
        req_id = self._new_id()
        pending = _Pending()
        with self._pending_lock:
            # Checked and inserted under the same lock _fail_all_pending
            # uses to set self._disconnected and snapshot self._pending: a
            # call racing the reader thread's death either inserts before
            # the snapshot (and gets swept up and resolved below) or runs
            # this check after self._disconnected is already set (and
            # raises here, before ever being inserted) - no window where a
            # pending call is registered after the sweep but never resolved.
            if self._disconnected is not None:
                raise self._disconnected
            self._pending[req_id] = pending
        try:
            self._send(MSG_CALL, {"id": req_id, "name": name, "args": list(args)})
            while True:
                if not pending.event.wait(timeout=timeout):
                    raise MCUTimeoutError(
                        f"{name!r} timed out after {timeout}s with no response or heartbeat"
                    )
                pending.event.clear()
                if pending.done:
                    break
                # else: was a heartbeat - loop back with a full fresh
                # timeout window (idle-reset, DESIGN.md § Wire protocol).
        finally:
            with self._pending_lock:
                del self._pending[req_id]
        if isinstance(pending.outcome, Exception):
            raise pending.outcome
        return pending.outcome

    def _run_reader(self) -> None:
        # `reader.read()` contract: blocking, no size argument, returns
        # whatever's readily available (empty bytes means the transport
        # closed). This is the transport adapter's responsibility to chunk
        # reasonably - e.g. a naive pyserial wrapper defaults `.read()` to
        # size=1, which would turn this into a byte-at-a-time loop and
        # amortize nothing. Adapters (chunk 9 serial, 12 wifi, 13 BLE) must
        # read in reasonably-sized chunks internally, not rely on this loop
        # to request a size - see CHUNKS.md chunk 9's entry.
        #
        # If the transport dies (raises, or reports EOF), every currently
        # in-flight call_mcu() must be unblocked immediately rather than
        # left to hang until its own timeout (or forever, for timeout=None)
        # with zero signal anything went wrong. Full "fail loud + explicit
        # reconnect" board-lifecycle handling (DESIGN.md § Disconnection) is
        # chunk 11's job, not this one's - but a dead reader silently
        # stranding in-flight calls is a gap at this chunk's own layer
        # regardless of whether reconnect exists yet.
        try:
            while True:
                data = self._reader.read()
                if not data:
                    break
                self._decoder.feed(data)
                for msg_type, payload in self._decoder.pull_frames():
                    self._route_frame(msg_type, payload)
        except Exception as exc:  # noqa: BLE001 - any transport failure must not strand callers
            self._fail_all_pending(f"transport read failed: {exc}")
            return
        self._fail_all_pending("transport closed")

    def _fail_all_pending(self, message: str) -> None:
        # self._disconnected is set and self._pending is snapshotted under
        # one critical section, matching call_mcu's own check-and-insert -
        # see the comment there for why both sides need the same lock, not
        # just this one.
        error = MCUDisconnectedError(message)
        with self._pending_lock:
            self._disconnected = error
            pending_calls = list(self._pending.values())
        for pending in pending_calls:
            pending.resolve(error)

    def _route_frame(self, msg_type: int, payload: dict[str, Any]) -> None:
        if msg_type == MSG_CALL:
            self._executor.submit(self._handle_call, payload)
            return
        with self._pending_lock:
            pending = self._pending.get(payload.get("id"))
        if pending is None:
            # Unknown/stale request id - most plausibly a response arriving
            # after the caller's own timeout already fired and cleaned up
            # self._pending. Silently dropping is intentional, not a gap.
            return
        if msg_type == MSG_HEARTBEAT:
            pending.heartbeat()
        elif msg_type == MSG_ERROR:
            error = RemoteError(
                payload.get("type"), payload.get("message"), payload.get("traceback", "")
            )
            pending.resolve(error)
        else:
            pending.resolve(payload.get("value"))

    def _send_error(self, req_id: int, error_type: str, message: str, tb: str = "") -> None:
        self._send(
            MSG_ERROR, {"id": req_id, "type": error_type, "message": message, "traceback": tb}
        )

    def _handle_call(self, payload: dict[str, Any]) -> None:
        req_id = payload["id"]
        name = payload["name"]
        args = payload.get("args", [])

        entry = self._handlers.get(name)
        if entry is None:
            self._send_error(req_id, "LookupError", f"no handler named {name!r}")
            return
        handler, heartbeat_interval = entry

        with self._heartbeat_ticking(req_id, heartbeat_interval):
            try:
                result = handler(*args)
            except Exception as exc:  # noqa: BLE001 - any handler exception must become MSG_ERROR
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                self._send_error(req_id, type(exc).__name__, str(exc), tb)
                return
        self._send(MSG_RESULT, {"id": req_id, "value": result})

    @contextlib.contextmanager
    def _heartbeat_ticking(self, req_id: int, interval: float | None):
        if not interval:
            yield
            return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_ticker, args=(req_id, interval, stop_event), daemon=True
        )
        thread.start()
        try:
            yield
        finally:
            stop_event.set()
            thread.join()

    def _heartbeat_ticker(self, req_id: int, interval: float, stop_event: threading.Event) -> None:
        # A dedicated thread ticking on a timer, not interleaved into the
        # handler's own execution - unlike chunk 6's asyncio ticker (which
        # naturally starves alongside a non-yielding handler, since both
        # share one event loop), a Python thread keeps ticking regardless
        # of what the handler thread is doing, CPU-bound or not. Heartbeats
        # here mean "the handler thread is still alive", not "still
        # cooperatively yielding" - a deliberate difference from chunk 6,
        # not an oversight; there's no equivalent of asyncio yield points
        # for a plain OS thread running arbitrary (possibly CPU-bound) code.
        while not stop_event.wait(timeout=interval):
            self._send(MSG_HEARTBEAT, {"id": req_id})
