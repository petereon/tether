"""tether_runtime.dispatch, exercised under a real MicroPython interpreter.

See tests/mpy_runner.py for why: uasyncio doesn't exist under CPython, and
even where APIs look similar, real interpreter behavior is what matters
here (this module is inherently MicroPython/uasyncio-specific).
"""

from mpy_runner import requires_micropython, run_micropython

# In-memory bidirectional pipe pair so two Dispatchers can talk to each
# other within a single MicroPython process, without a real transport.
# Prepended to test scripts that need a full duplex connection.
_PIPE_HELPER = """
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
    # (reader, writer) for each end
    return (b_to_a, a_to_b), (a_to_b, b_to_a)
"""


@requires_micropython
def test_frame_roundtrip():
    out = run_micropython("""
import dispatch
frame = dispatch._encode_frame(1, {"a": 1})
print(len(frame))
print(frame[4])
""")
    lines = out.strip().split("\n")
    assert int(lines[0]) > 0
    assert int(lines[1]) == 1


@requires_micropython
def test_read_frame_roundtrip():
    out = run_micropython("""
import uasyncio as asyncio
import dispatch

class FakeReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
    async def readexactly(self, n):
        chunk = self.data[self.pos:self.pos+n]
        self.pos += n
        return chunk

async def main():
    frame = dispatch._encode_frame(3, {"hello": "world"})
    reader = FakeReader(frame)
    msg_type, payload = await dispatch._read_frame(reader)
    print(msg_type)
    print(payload)

asyncio.run(main())
""")
    lines = out.strip().split("\n")
    assert lines[0] == "3"
    assert "hello" in lines[1] and "world" in lines[1]


@requires_micropython
def test_read_frame_rejects_oversized_length():
    out = run_micropython("""
import uasyncio as asyncio
import dispatch

class FakeReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
    async def readexactly(self, n):
        chunk = self.data[self.pos:self.pos+n]
        self.pos += n
        return chunk

async def main():
    hostile = (100 * 1024 * 1024).to_bytes(4, "big") + b"\\x01\\x00\\x00\\x00"
    reader = FakeReader(hostile)
    try:
        await dispatch._read_frame(reader)
        print("NO ERROR RAISED")
    except ValueError as e:
        print("raised:", e)

asyncio.run(main())
""")
    assert "raised:" in out
    assert "NO ERROR" not in out


@requires_micropython
def test_read_frame_accepts_frame_just_under_max_size():
    # Generous headroom below MAX_FRAME_SIZE absorbs umsgpack's per-value
    # header overhead so the *declared* frame length (not just the string
    # payload) lands comfortably under the bound.
    out = run_micropython("""
import uasyncio as asyncio
import dispatch

class FakeReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
    async def readexactly(self, n):
        chunk = self.data[self.pos:self.pos+n]
        self.pos += n
        return chunk

async def main():
    payload = {"data": "x" * (dispatch.MAX_FRAME_SIZE - 4096)}
    frame = dispatch._encode_frame(1, payload)
    declared_length = int.from_bytes(frame[0:4], "big")
    print("within bound:", declared_length <= dispatch.MAX_FRAME_SIZE)
    reader = FakeReader(frame)
    msg_type, decoded = await dispatch._read_frame(reader)
    print("msg_type:", msg_type)
    print("ok:", decoded["data"] == payload["data"])

asyncio.run(main())
""")
    assert "within bound: True" in out
    assert "msg_type: 1" in out
    assert "ok: True" in out


@requires_micropython
def test_read_frame_rejects_frame_just_over_max_size():
    out = run_micropython("""
import uasyncio as asyncio
import dispatch

class FakeReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
    async def readexactly(self, n):
        chunk = self.data[self.pos:self.pos+n]
        self.pos += n
        return chunk

async def main():
    payload = {"data": "x" * (dispatch.MAX_FRAME_SIZE + 4096)}
    frame = dispatch._encode_frame(1, payload)
    declared_length = int.from_bytes(frame[0:4], "big")
    print("over bound:", declared_length > dispatch.MAX_FRAME_SIZE)
    # Only the 4-byte length header is needed for rejection - _read_frame
    # bounds-checks before ever calling readexactly() for the body.
    reader = FakeReader(frame[:4])
    try:
        await dispatch._read_frame(reader)
        print("NO ERROR RAISED")
    except ValueError as e:
        print("raised:", e)

asyncio.run(main())
""")
    assert "over bound: True" in out
    assert "raised:" in out
    assert "NO ERROR" not in out


@requires_micropython
def test_call_pc_removes_pending_entry_when_send_raises():
    # Regression test for a pending-request memory leak: call_pc() used to
    # register self._pending[req_id] and then await self._send(...) OUTSIDE
    # the try/finally that deletes it. If _send() raised (e.g. OSError from
    # a dropped connection), the finally never ran and the entry leaked in
    # self._pending forever - unbounded growth on every connection drop that
    # happens mid-call_pc, on a board with very little RAM to spare.
    out = run_micropython("""
import uasyncio as asyncio
import dispatch

class BoomWriter:
    def write(self, data):
        raise OSError("connection dropped")
    async def drain(self):
        pass

class NeverReader:
    async def readexactly(self, n):
        # call_pc() fails inside _send(), before it would ever wait on a
        # read - this just needs to never resolve on its own.
        await asyncio.sleep(1000)

async def main():
    d = dispatch.Dispatcher(NeverReader(), BoomWriter())
    try:
        await d.call_pc("whatever")
        print("NO ERROR RAISED")
    except OSError as e:
        print("raised:", e)
    print("pending count:", len(d._pending))

asyncio.run(main())
""")
    assert "raised:" in out
    assert "NO ERROR" not in out
    assert "pending count: 0" in out


@requires_micropython
def test_call_pc_roundtrip_to_a_registered_handler():
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)
    b = dispatch.Dispatcher(reader_b, writer_b)

    def add(x, y):
        return x + y
    b.register("add", add)

    asyncio.create_task(a.run())
    asyncio.create_task(b.run())

    result = await a.call_pc("add", 2, 3)
    print("result:", result)

asyncio.run(main())
"""
    )
    assert "result: 5" in out


@requires_micropython
def test_call_pc_propagates_handler_exception():
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)
    b = dispatch.Dispatcher(reader_b, writer_b)

    def boom():
        raise ValueError("bad sensor reading")
    b.register("boom", boom)

    asyncio.create_task(a.run())
    asyncio.create_task(b.run())

    try:
        await a.call_pc("boom")
        print("NO ERROR RAISED")
    except dispatch.RemoteError as e:
        print("remote_type:", e.remote_type)
        print("message:", e.args[0])

asyncio.run(main())
"""
    )
    assert "remote_type: ValueError" in out
    assert "bad sensor reading" in out


@requires_micropython
def test_remote_error_includes_traceback():
    # DESIGN.md § Wire protocol: RemoteError carries type + message +
    # traceback - not just type + message.
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)
    b = dispatch.Dispatcher(reader_b, writer_b)

    def boom():
        raise ValueError("bad sensor reading")
    b.register("boom", boom)

    asyncio.create_task(a.run())
    asyncio.create_task(b.run())

    try:
        await a.call_pc("boom")
    except dispatch.RemoteError as e:
        print("traceback:", repr(e.remote_traceback))

asyncio.run(main())
"""
    )
    assert "traceback:" in out
    assert "ValueError" in out
    assert "traceback: ''" not in out


@requires_micropython
def test_call_pc_works_with_async_handler():
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)
    b = dispatch.Dispatcher(reader_b, writer_b)

    async def read_temp():
        await asyncio.sleep_ms(10)
        return 21.5
    b.register("read_temp", read_temp)

    asyncio.create_task(a.run())
    asyncio.create_task(b.run())

    result = await a.call_pc("read_temp")
    print("result:", result)

asyncio.run(main())
"""
    )
    assert "result: 21.5" in out


@requires_micropython
def test_reentrant_nested_call_while_waiting():
    # B's "outer" handler calls back into A's "inner" handler while A is
    # still awaiting B's response to "outer" - A's run() loop must keep
    # servicing incoming frames concurrently with A's own pending call_pc(),
    # not block on it. This is the core bidirectional-reentrancy guarantee
    # DESIGN.md's call semantics commit to.
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)
    b = dispatch.Dispatcher(reader_b, writer_b)

    def inner(x):
        return x * 10
    a.register("inner", inner)

    async def outer():
        value = await b.call_pc("inner", 4)
        return value + 1
    b.register("outer", outer)

    asyncio.create_task(a.run())
    asyncio.create_task(b.run())

    result = await a.call_pc("outer")
    print("result:", result)

asyncio.run(main())
"""
    )
    assert "result: 41" in out


@requires_micropython
def test_register_loop_runs_periodically():
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)

    ticks = []
    def poll():
        ticks.append(1)
    a.register_loop(poll, interval_ms=20)

    task = asyncio.create_task(a.run())
    await asyncio.sleep_ms(90)
    task.cancel()
    print("ticks:", len(ticks))

asyncio.run(main())
"""
    )
    # ~90ms at 20ms intervals should yield roughly 4 ticks; allow slack for
    # scheduler jitter without being so loose the test can't catch a bug.
    count = int(out.strip().split("ticks:")[1])
    assert 2 <= count <= 6, f"expected ~4 ticks in 90ms at 20ms interval, got {count}"


@requires_micropython
def test_register_loop_supports_async_function():
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)

    ticks = []
    async def poll():
        await asyncio.sleep_ms(1)
        ticks.append(1)
    a.register_loop(poll, interval_ms=20)

    task = asyncio.create_task(a.run())
    await asyncio.sleep_ms(90)
    task.cancel()
    print("ticks:", len(ticks))

asyncio.run(main())
"""
    )
    count = int(out.strip().split("ticks:")[1])
    assert count >= 2, f"expected async loop to tick at least twice in 90ms, got {count}"


@requires_micropython
def test_long_handler_emits_heartbeats_while_running():
    # DESIGN.md § Wire protocol: heartbeats piggyback on the handler's own
    # await yield points - a cooperative long-running handler should get
    # periodic MSG_HEARTBEAT frames sent on its behalf while it's still
    # executing, distinct from (and before) its final MSG_RESULT.
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)
    b = dispatch.Dispatcher(reader_b, writer_b)

    async def spin():
        for _ in range(5):
            await asyncio.sleep_ms(20)
        return "done"
    b.register("spin", spin, heartbeat_interval_ms=15)

    asyncio.create_task(a.run())
    asyncio.create_task(b.run())

    seen = []
    orig_route = a._route_response
    def spy(msg_type, payload):
        seen.append(msg_type)
        orig_route(msg_type, payload)
    a._route_response = spy

    result = await a.call_pc("spin")
    print("result:", result)
    print("heartbeats:", seen.count(dispatch.MSG_HEARTBEAT))

asyncio.run(main())
"""
    )
    assert "result: done" in out
    heartbeat_count = int(out.strip().split("heartbeats:")[1])
    assert heartbeat_count >= 2, (
        f"expected multiple heartbeats during a 100ms call, got {heartbeat_count}"
    )


@requires_micropython
def test_heartbeat_does_not_complete_the_pending_call():
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)
    b = dispatch.Dispatcher(reader_b, writer_b)

    async def spin():
        await asyncio.sleep_ms(60)
        return "final"
    b.register("spin", spin, heartbeat_interval_ms=15)

    asyncio.create_task(a.run())
    asyncio.create_task(b.run())

    result = await a.call_pc("spin")
    print("result:", result)

asyncio.run(main())
"""
    )
    # If a heartbeat were mistakenly treated as a final response, the
    # awaited result would resolve early with no "value" (None) instead of
    # waiting for the real "final" MSG_RESULT.
    assert "result: final" in out


@requires_micropython
def test_call_for_unregistered_handler_returns_error_not_hang():
    # A CALL frame for a name nobody registered (stale/mismatched deploy,
    # or a protocol bug) must not silently hang the caller forever - the
    # handling task is fire-and-forget (asyncio.create_task in run()),
    # nothing else awaits or inspects it, so an uncaught exception there
    # would strand the caller's pending request with no response ever sent.
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)
    b = dispatch.Dispatcher(reader_b, writer_b)
    # b registers nothing at all

    asyncio.create_task(a.run())
    asyncio.create_task(b.run())

    try:
        await a.call_pc("nonexistent")
        print("NO ERROR RAISED")
    except dispatch.RemoteError as e:
        print("remote_type:", e.remote_type)

asyncio.run(main())
"""
    )
    assert "remote_type:" in out
    assert "NO ERROR" not in out


@requires_micropython
def test_run_bounds_concurrent_handler_tasks():
    # Bug: run()'s MSG_CALL handling did asyncio.create_task(self._handle_call(...))
    # with no cap at all - a peer sending many CALL frames in quick
    # succession (now reachable over the unauthenticated wifi listener)
    # could spawn unbounded concurrent tasks, each holding its own
    # stack/locals, exhausting the board's RAM. This drives more concurrent
    # requests than the cap and asserts the number of *simultaneously
    # running* handler bodies never exceeds it - not just "it doesn't
    # crash".
    out = run_micropython(
        _PIPE_HELPER
        + """
import dispatch

async def main():
    (reader_a, writer_a), (reader_b, writer_b) = make_pair()
    a = dispatch.Dispatcher(reader_a, writer_a)
    b = dispatch.Dispatcher(reader_b, writer_b)

    running = 0
    max_running = 0
    async def slow(i):
        nonlocal running, max_running
        running += 1
        if running > max_running:
            max_running = running
        await asyncio.sleep_ms(30)
        running -= 1
        return i
    b.register("slow", slow)

    asyncio.create_task(a.run())
    asyncio.create_task(b.run())

    tasks = [asyncio.create_task(a.call_pc("slow", i)) for i in range(20)]
    results = []
    for t in tasks:
        results.append(await t)

    print("max_running:", max_running)
    print("num_results:", len(results))
    print("results_ok:", sorted(results) == list(range(20)))

asyncio.run(main())
"""
    )
    max_running = int(out.strip().split("max_running:")[1].split("\n")[0])
    assert max_running <= 8, f"expected at most 8 concurrent handlers in flight, got {max_running}"
    assert "num_results: 20" in out
    assert "results_ok: True" in out
