import queue
import time

import pytest

from tether.dispatch import Dispatcher
from tether.errors import MCUDisconnectedError, MCUTimeoutError, RemoteError


class _Pipe:
    """Thread-safe in-memory duplex channel for wiring two Dispatchers
    together within one test process, without a real transport."""

    def __init__(self):
        self._queue: queue.Queue[bytes] = queue.Queue()

    def write(self, data: bytes) -> None:
        self._queue.put(bytes(data))

    def read(self) -> bytes:
        return self._queue.get()


def _make_pair():
    a_to_b = _Pipe()
    b_to_a = _Pipe()
    return (b_to_a, a_to_b), (a_to_b, b_to_a)


def test_call_mcu_roundtrip_to_a_registered_handler():
    (reader_a, writer_a), (reader_b, writer_b) = _make_pair()
    a = Dispatcher(reader_a, writer_a)
    b = Dispatcher(reader_b, writer_b)

    b.register("add", lambda x, y: x + y)

    a.start()
    b.start()

    assert a.call_mcu("add", 2, 3) == 5


def test_call_mcu_propagates_remote_exception():
    (reader_a, writer_a), (reader_b, writer_b) = _make_pair()
    a = Dispatcher(reader_a, writer_a)
    b = Dispatcher(reader_b, writer_b)

    def boom():
        raise ValueError("bad sensor reading")

    b.register("boom", boom)
    a.start()
    b.start()

    with pytest.raises(RemoteError) as exc_info:
        a.call_mcu("boom")

    assert exc_info.value.remote_type == "ValueError"
    assert "bad sensor reading" in str(exc_info.value)
    assert "ValueError" in exc_info.value.remote_traceback


def test_call_mcu_for_unregistered_handler_raises_not_hangs():
    (reader_a, writer_a), (reader_b, writer_b) = _make_pair()
    a = Dispatcher(reader_a, writer_a)
    b = Dispatcher(reader_b, writer_b)
    # b registers nothing

    a.start()
    b.start()

    with pytest.raises(RemoteError) as exc_info:
        a.call_mcu("nonexistent", timeout=2.0)

    assert exc_info.value.remote_type == "LookupError"


def test_reentrant_nested_call_while_waiting():
    (reader_a, writer_a), (reader_b, writer_b) = _make_pair()
    a = Dispatcher(reader_a, writer_a)
    b = Dispatcher(reader_b, writer_b)

    a.register("inner", lambda x: x * 10)

    def outer():
        return b.call_mcu("inner", 4) + 1

    b.register("outer", outer)

    a.start()
    b.start()

    assert a.call_mcu("outer") == 41


def test_call_mcu_times_out_when_no_response_or_heartbeat_arrives():
    (reader_a, writer_a), _unused = _make_pair()
    # `a` has a reader but nothing on the other end will ever write to it.
    a = Dispatcher(reader_a, writer_a)
    a.start()

    start = time.monotonic()
    with pytest.raises(MCUTimeoutError):
        a.call_mcu("never_answered", timeout=0.2)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, "should time out promptly, not hang"


def test_heartbeat_resets_the_timeout_window_instead_of_completing_the_call():
    # A registered handler slower than the call's own timeout, but emitting
    # heartbeats faster than it - each heartbeat should reset the window, so
    # total duration exceeding the per-window timeout must still succeed.
    (reader_a, writer_a), (reader_b, writer_b) = _make_pair()
    a = Dispatcher(reader_a, writer_a)
    b = Dispatcher(reader_b, writer_b)

    def spin():
        time.sleep(0.4)
        return "done"

    b.register("spin", spin, heartbeat_interval=0.1)
    a.start()
    b.start()

    result = a.call_mcu("spin", timeout=0.2)
    assert result == "done"


def test_reader_thread_death_fails_pending_calls_instead_of_hanging_forever():
    # A caller with no timeout (or a long one) must not hang forever just
    # because the background reader thread died (e.g. transport unplugged).
    # DESIGN.md's full "fail loud + explicit reconnect" story is chunk 11's
    # job, but a dead reader silently stranding in-flight calls is a gap at
    # this chunk's own layer regardless of whether reconnect exists yet.
    class _DyingReader:
        def __init__(self):
            self._read_once = False

        def read(self):
            if not self._read_once:
                self._read_once = True
                # block briefly so call_mcu() is genuinely waiting when
                # the reader dies, not racing it
                time.sleep(0.05)
                raise ConnectionError("device unplugged")
            raise AssertionError("should not be called again after dying")

    a = Dispatcher(_DyingReader(), writer=_Pipe())
    a.start()

    with pytest.raises(MCUDisconnectedError):
        a.call_mcu("anything", timeout=None)
