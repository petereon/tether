"""In-process fake MCU transport for hardware-free testing.

`mcu.connect("mock://")` (chunk 10) will use this transport; for now
(chunk 10 not yet built) construct `MockTransport` directly and `.start()`
it in tests. Runs the actual sliced/generated code path (chunks 3, 4)
against chunk 6's real dispatch runtime, executing under CPython in a
background thread - not a reimplementation of dispatch logic. See
DESIGN.md § Testing.

How: `tether_runtime/dispatch.py` imports `uasyncio` and `umsgpack` as bare
top-level names (matching on-device flat deployment - see that file's own
header comment). To import and run it unmodified under CPython, we (1)
install a small `uasyncio` -> `asyncio` compatibility shim into
`sys.modules` before the first import, and (2) put `src/tether_runtime` on
`sys.path` so `umsgpack` and `dispatch` resolve as bare top-level modules,
exactly as they would on-device.

Caveats:
- The shim installs are one-way, process-global mutations (`sys.modules`,
  `sys.path`, `sys.print_exception`) with no teardown - importing
  `MockTransport` anywhere in a pytest session makes bare `import dispatch`
  resolve to chunk 6's module for the rest of that session, in any test
  module. There's no current collision (PC-side dispatch is `tether.dispatch`,
  not bare `dispatch`), but this isn't isolated between test modules.
- `source` is executed via `exec()` - only pass source you trust (your own
  test fixtures), same trust level as any other code you'd run directly.
- The slicer preserves genuine external imports it can't resolve locally
  (e.g. `from machine import Pin`) verbatim in its output (see
  tests/test_slicer.py) - such imports will fail under CPython here, since
  this mock can't emulate hardware-only modules. Keep mock-transport test
  fixtures free of real hardware imports.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
import traceback
import types
from pathlib import Path
from typing import Any

from tether import decorators
from tether.slicer import generate_pc_stubs, slice_mcu_bound

_TETHER_RUNTIME_SRC = Path(__file__).resolve().parents[2] / "tether_runtime"


def _install_micropython_shims() -> None:
    """Patch in the MicroPython-only surface tether_runtime/dispatch.py
    legitimately relies on, so the real (unmodified) module runs correctly
    under CPython. Not a chunk 6 bug - chunk 6 targets real MicroPython;
    this compatibility layer belongs to the mock transport, not to it.
    """
    if "uasyncio" not in sys.modules:
        shim = types.ModuleType("uasyncio")
        shim.Event = asyncio.Event
        shim.Lock = asyncio.Lock
        shim.sleep = asyncio.sleep
        shim.create_task = asyncio.create_task
        shim.run = asyncio.run
        shim.CancelledError = asyncio.CancelledError

        async def sleep_ms(ms: float) -> None:
            await asyncio.sleep(ms / 1000)

        shim.sleep_ms = sleep_ms
        sys.modules["uasyncio"] = shim

    if not hasattr(sys, "print_exception"):
        # sys.print_exception(exc, file) is a MicroPython-only extension
        # (used by dispatch.py's _format_exception for RemoteError
        # tracebacks); CPython's sys module has no such attribute.
        def _print_exception(exc: BaseException, file: Any = sys.stdout) -> None:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=file)

        sys.print_exception = _print_exception  # type: ignore[attr-defined]


def _import_runtime_dispatch() -> Any:
    """Import the real chunk 6 dispatch module, unmodified."""
    _install_micropython_shims()
    runtime_path = str(_TETHER_RUNTIME_SRC)
    if runtime_path not in sys.path:
        sys.path.insert(0, runtime_path)
    import dispatch as tether_runtime_dispatch  # the real tether_runtime/dispatch.py

    return tether_runtime_dispatch


class _McuReader:
    """Async side: readexactly(n), fed by an asyncio.Queue only ever
    touched via call_soon_threadsafe from the (foreign) PC writer thread.
    """

    def __init__(self, in_queue: asyncio.Queue[bytes]) -> None:
        self._in_queue = in_queue
        self._buffer = bytearray()

    async def readexactly(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = await self._in_queue.get()
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:n])
        del self._buffer[:n]
        return result


class _McuWriter:
    """Sync side: write()/drain(), matching uasyncio.StreamWriter's shape.
    Feeds a plain thread-safe queue.Queue the PC-side sync reader consumes.
    """

    def __init__(self, out_thread_queue: queue.Queue[bytes]) -> None:
        self._out_thread_queue = out_thread_queue

    def write(self, data: bytes) -> None:
        self._out_thread_queue.put(bytes(data))

    async def drain(self) -> None:
        return


class _PcReader:
    """Sync side: plain blocking read(), matches tether.dispatch's contract."""

    def __init__(self, in_thread_queue: queue.Queue[bytes]) -> None:
        self._in_thread_queue = in_thread_queue

    def read(self) -> bytes:
        return self._in_thread_queue.get()


class _PcWriter:
    """Sync side (called from a foreign PC thread): hands data across into
    the MCU's asyncio.Queue via call_soon_threadsafe - the only safe way to
    push into an asyncio.Queue from outside its own event loop's thread.
    """

    def __init__(
        self, loop: asyncio.AbstractEventLoop, out_async_queue: asyncio.Queue[bytes]
    ) -> None:
        self._loop = loop
        self._out_async_queue = out_async_queue

    def write(self, data: bytes) -> None:
        self._loop.call_soon_threadsafe(self._out_async_queue.put_nowait, bytes(data))


class MockTransport:
    """In-process fake MCU. `.start()` runs it in a background thread and
    returns (reader, writer) for a `tether.dispatch.Dispatcher` to use.

    No `.stop()` - the background thread (daemon, so it won't block process
    exit) runs the dispatch loop forever. Fine for typical test-suite sizes;
    a very large suite creating many `MockTransport`s will accumulate
    lingering threads/event loops until the process exits. Building proper
    teardown (canceling the dispatch loop, closing the event loop, joining
    the thread) is out of scope for this walking-skeleton testing utility.
    """

    def __init__(self, source: str, *, base_dir: Path | None = None) -> None:
        self._source = source
        self._base_dir = base_dir
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._in_queue: asyncio.Queue[bytes] | None = None
        self._mcu_to_pc_queue: queue.Queue[bytes] = queue.Queue()

    def start(self) -> tuple[Any, Any]:
        thread = threading.Thread(target=self._run_mcu, daemon=True)
        thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("mock MCU failed to start within 5s")
        pc_reader = _PcReader(self._mcu_to_pc_queue)
        pc_writer = _PcWriter(self._loop, self._in_queue)
        return pc_reader, pc_writer

    def _run_mcu(self) -> None:
        runtime_dispatch = _import_runtime_dispatch()

        async def main() -> None:
            self._loop = asyncio.get_running_loop()
            self._in_queue = asyncio.Queue()

            mcu_reader = _McuReader(self._in_queue)
            mcu_writer = _McuWriter(self._mcu_to_pc_queue)
            dispatcher = runtime_dispatch.Dispatcher(mcu_reader, mcu_writer)
            self._register_exported_functions(
                dispatcher, default_heartbeat_ms=runtime_dispatch.DEFAULT_HEARTBEAT_INTERVAL_MS
            )

            self._ready.set()
            await dispatcher.run()

        asyncio.run(main())

    def _register_exported_functions(self, dispatcher: Any, *, default_heartbeat_ms: int) -> None:
        sliced = slice_mcu_bound(self._source, base_dir=self._base_dir)
        stubs = generate_pc_stubs(self._source)
        combined = sliced.source + "\n\n" + stubs.source

        namespace: dict[str, Any] = {
            "mcu": decorators.mcu,
            "pc": decorators.pc,
            "call_pc": dispatcher.call_pc,
        }
        exec(compile(combined, "<mock-mcu>", "exec"), namespace)  # noqa: S102

        for name in sliced.exported_names:
            spec = namespace[name].__tether_export__
            if spec.interval_ms is not None:
                dispatcher.register_loop(namespace[name], spec.interval_ms)
                continue
            heartbeat_ms = (
                int(spec.heartbeat_interval * 1000)
                if spec.heartbeat_interval is not None
                else default_heartbeat_ms
            )
            dispatcher.register(name, namespace[name], heartbeat_interval_ms=heartbeat_ms)
