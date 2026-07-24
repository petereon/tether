import time

import pytest

from tether.dispatch import Dispatcher
from tether.errors import RemoteError
from tether.transports.mock import MockTransport


def test_call_mcu_reaches_real_sliced_mcu_export_function():
    source = """
from tether import mcu

@mcu.export
def read_temp() -> float:
    return 21.5
"""
    transport = MockTransport(source)
    reader, writer = transport.start()
    pc = Dispatcher(reader, writer)
    pc.start()

    assert pc.call_mcu("read_temp") == 21.5


def test_mcu_export_function_calls_back_into_a_pc_export_stub():
    # Exercises the full round-trip through chunk 4's generated stub: the
    # MCU-side function awaits a PC-side call_pc(...), which chunk 8's
    # namespace-injected `call_pc` routes back through the same wire to a
    # handler registered on the *PC-side* Dispatcher.
    source = """
from tether import mcu, pc

@pc.export
def double(x: int) -> int:
    return x * 2

@mcu.export
async def read_scaled() -> int:
    return await double(21)
"""
    transport = MockTransport(source)
    reader, writer = transport.start()
    pc_dispatcher = Dispatcher(reader, writer)
    pc_dispatcher.register("double", lambda x: x * 2)
    pc_dispatcher.start()

    assert pc_dispatcher.call_mcu("read_scaled") == 42


def test_mcu_export_exception_propagates_as_remote_error():
    source = """
from tether import mcu

@mcu.export
def boom() -> None:
    raise ValueError("bad sensor reading")
"""
    transport = MockTransport(source)
    reader, writer = transport.start()
    pc = Dispatcher(reader, writer)
    pc.start()

    with pytest.raises(RemoteError) as exc_info:
        pc.call_mcu("boom")
    assert exc_info.value.remote_type == "ValueError"
    assert "bad sensor reading" in str(exc_info.value)


def test_mcu_loop_runs_periodically_on_the_real_dispatch_loop():
    source = """
from tether import mcu

counter = {"n": 0}

@mcu.loop(interval_ms=20)
def poll() -> None:
    counter["n"] += 1

@mcu.export
def read_count() -> int:
    return counter["n"]
"""
    transport = MockTransport(source)
    reader, writer = transport.start()
    pc = Dispatcher(reader, writer)
    pc.start()

    time.sleep(0.15)
    count = pc.call_mcu("read_count")
    assert count >= 2, f"expected @mcu.loop to have ticked at least twice, got {count}"
