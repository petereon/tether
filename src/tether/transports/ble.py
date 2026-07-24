"""BLE transport via `bleak` (install via `tether[ble]`).

Custom GATT service: one write characteristic (PC->MCU), one notify
characteristic (MCU->PC). Frame chunking/reassembly across the small BLE MTU
is handled entirely in this module — invisible above the dispatch layer.
See DESIGN.md § Transports.

Scope note (chunk 13, same shape as chunk 12's wifi gap): DESIGN.md gives
BLE "the same bootstrap requirement as wifi" - the board must already be
running a bootstrapped runtime, tether never pushes code over BLE. Nothing
in the design specifies how a device comes to advertise tether's custom
GATT service in the first place (no on-device `bluetooth`/`aioble`
peripheral implementation exists or is asked for anywhere). This module is
the PC-side (central/client) half only - the same explicit, documented gap
as wifi's on-device listener, not an oversight. See CHUNKS.md's entry for
this chunk.

`bleak` is client/central-only - there is no way to run a real local BLE
peripheral to test against, on any platform, with or without hardware.
`BleStream`'s bridging logic (MTU chunking, push-to-pull notification
bridging) is unit tested against a hand-written fake matching bleak's
documented async API shape (see tests/test_transport_ble.py). `connect()`
itself - the real bleak wiring - is not exercised by any test; there is no
way to do so without real BLE hardware and a real peripheral to connect to.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from typing import Any

# Custom 128-bit UUIDs for tether's GATT service - arbitrary but fixed, so
# a device's on-device peripheral implementation (out of this chunk's
# scope, see module docstring) and this PC-side client agree on what to
# look for. Generated once, never to be changed without bumping
# PROTOCOL_VERSION (connection.py) - an unrecognized UUID looks identical
# to "wrong device" from this module's point of view either way.
SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
WRITE_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

DEFAULT_TIMEOUT = 10.0

# BLE ATT header: 1-byte opcode + 2-byte handle - subtracted from the
# negotiated MTU to get the usable payload size per GATT write.
_ATT_HEADER_OVERHEAD = 3


class BleStream:
    """Duplex stream bridging a connected `bleak.BleakClient` (async,
    foreign event loop) to the plain synchronous read()/write() contract
    tether.dispatch.Dispatcher expects - same shape as SerialStream/
    WifiStream, but BLE is push-based (notifications arrive via a callback)
    rather than pull-based (a blocking read() call), so read() is backed by
    a thread-safe queue fed by `on_notify` instead of calling into the
    client directly.
    """

    def __init__(self, client: Any, loop: asyncio.AbstractEventLoop, write_char_uuid: str) -> None:
        self._client = client
        self._loop = loop
        self._write_char_uuid = write_char_uuid
        self._queue: queue.Queue[bytes] = queue.Queue()

    def read(self) -> bytes:
        return self._queue.get()

    def write(self, data: bytes) -> None:
        # GATT writes are capped by the negotiated ATT MTU - split anything
        # larger into multiple writes. FrameDecoder (marshalling/__init__.py)
        # already reassembles arbitrarily-chunked reads into whole frames on
        # the Dispatcher side, so read()/on_notify below need no equivalent
        # reassembly logic - only the write side needs to actively chunk,
        # since BLE (unlike a TCP socket or serial port) will reject or
        # truncate a write larger than the MTU rather than accepting and
        # buffering it.
        usable = max(self._client.mtu_size - _ATT_HEADER_OVERHEAD, 1)
        chunks = [data[i : i + usable] for i in range(0, len(data), usable)]

        async def _write_all() -> None:
            # BLE allows only one outstanding ATT request at a time - each
            # chunk must be awaited (not just sent) before the next is
            # written, or an out-of-order ack (or a controller/OS BLE stack
            # that doesn't strictly serialize concurrent requests) could
            # corrupt the frame being reassembled on the far side. This is a
            # correctness requirement, not just a convenient way to get
            # sequencing - do not parallelize these writes.
            #
            # response=True (write-with-response) trades throughput for a
            # per-write delivery guarantee from the peripheral. Kept
            # deliberately: this project has no real BLE hardware to
            # validate write-without-response's actual reliability
            # characteristics across platforms/controllers, and DESIGN.md's
            # wire protocol has no independent per-frame ack of its own to
            # fall back on if a written chunk were silently dropped.
            for chunk in chunks:
                await self._client.write_gatt_char(self._write_char_uuid, chunk, response=True)

        # One cross-thread hop for the whole write() call (one Future, one
        # event-loop wakeup) rather than one per chunk - the sequencing
        # above already happens entirely within this single coroutine on
        # the loop's own thread.
        asyncio.run_coroutine_threadsafe(_write_all(), self._loop).result()

    def on_notify(self, _sender: Any, data: bytearray) -> None:
        """Registered as the notify characteristic's callback - matches
        bleak's `Callable[[BleakGATTCharacteristic, bytearray], None]`
        signature. bleak invokes this on its own event-loop thread;
        queue.Queue.put is thread-safe so no call_soon_threadsafe/marshalling
        is needed here (unlike MockTransport's asyncio.Queue bridge in
        transports/mock.py, which needs it because asyncio.Queue isn't
        thread-safe - queue.Queue is a different tool for exactly this
        reason).
        """
        self._queue.put(bytes(data))

    def signal_closed(self) -> None:
        """Called from bleak's `disconnected_callback` - pushes the same
        "transport closed" signal Dispatcher._run_reader already looks for
        (an empty read()), so a BLE disconnect fails loud through the exact
        same path as a dead serial port or closed socket, rather than
        needing BLE-specific disconnect handling anywhere above this
        module.
        """
        self._queue.put(b"")

    def close(self) -> None:
        asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop).result()
        # connect() gave this stream's event loop its own dedicated thread
        # purely to run bleak's async API (unlike wifi/serial, no blocking
        # syscall justifies leaving that thread alive once disconnected) -
        # stop it so close() doesn't leak a thread on every call.
        self._loop.call_soon_threadsafe(self._loop.stop)


def connect(address: str, *, timeout: float = DEFAULT_TIMEOUT) -> BleStream:
    """Connect to an already-running on-device runtime advertising tether's
    custom GATT service (SERVICE_UUID/WRITE_CHAR_UUID/NOTIFY_CHAR_UUID
    above). No slicing, bundling, or upload here - DESIGN.md gives BLE "the
    same bootstrap requirement as wifi": tether never pushes code over it.

    bleak's client API is async; this bridges it to BleStream's plain
    synchronous contract the same way transports/mock.py bridges chunk 6's
    real asyncio-based dispatch loop to CPython - a dedicated background
    thread owns its own event loop and the BleakClient for this
    connection's whole lifetime.
    """
    import bleak  # optional extra (tether[ble]) - fails loud with the
    # interpreter's own clear ModuleNotFoundError if not installed, same as
    # transports/serial.py's lazy `from serial.tools import list_ports`.

    result: concurrent.futures.Future[BleStream] = concurrent.futures.Future()

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _setup() -> None:
            stream: BleStream | None = None

            def _on_disconnect(_client: Any) -> None:
                if stream is not None:
                    stream.signal_closed()

            try:
                client = bleak.BleakClient(address, disconnected_callback=_on_disconnect)
                await client.connect(timeout=timeout)
                stream = BleStream(client, loop, WRITE_CHAR_UUID)
                await client.start_notify(NOTIFY_CHAR_UUID, stream.on_notify)
            except BaseException as exc:  # noqa: BLE001 - propagated via the future below, never swallowed
                try:
                    result.set_exception(exc)
                except concurrent.futures.InvalidStateError:
                    pass  # caller already gave up (see the `except TimeoutError` below)
                loop.call_soon_threadsafe(loop.stop)
                return

            try:
                result.set_result(stream)
            except concurrent.futures.InvalidStateError:
                # The caller's result(timeout=...) below already gave up
                # before this connect attempt finished - don't strand a
                # live, unmanaged BLE connection nothing will ever close.
                # Most peripherals accept only one central connection at a
                # time, so leaving this open could block every future
                # (re)connect attempt, not just leak a thread.
                await client.disconnect()
                loop.call_soon_threadsafe(loop.stop)

        loop.create_task(_setup())
        loop.run_forever()

    threading.Thread(target=_run, daemon=True).start()
    try:
        return result.result(timeout=timeout + 5.0)
    except concurrent.futures.TimeoutError:
        # Future.cancel() on a still-pending future transitions it to
        # CANCELLED, so _setup()'s later set_result()/set_exception() call
        # (above) observes InvalidStateError and cleans up instead of
        # silently succeeding into a connection nobody holds a reference
        # to - see its own comment. No-op (returns False) if _setup()
        # already finished first; that race is exactly what the
        # InvalidStateError check on the other side is for.
        result.cancel()
        raise
