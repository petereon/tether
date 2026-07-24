"""Chunk 15: validates the examples/ walkthrough as thoroughly as possible
without real hardware. No physical ESP32/MicroPython board has been
available at any point during this project's development - these tests
verify the example slices correctly, produces a syntactically valid
on-device bundle, and that bundle's registration/dispatch wiring actually
works under a real MicroPython interpreter (machine.Pin faked out, since
the unix-port interpreter has no real GPIO - see conftest-less inline fake
below). They do NOT verify that an LED actually blinks on a real board.
See docs/CHUNKS.md's entry for chunk 15 for the full, explicit limitation.
"""

import ast
import sys
from pathlib import Path

from tether.connection import generate_bootstrap
from tether.slicer import generate_pc_stubs, slice_mcu_bound

sys.path.insert(0, str(Path(__file__).parent))
from mpy_runner import PIPE_HARNESS, requires_micropython, run_micropython

_EXAMPLE_PATH = Path(__file__).parent.parent / "examples" / "blink_and_log" / "blink_and_log.py"


def _example_source() -> str:
    return _EXAMPLE_PATH.read_text()


def test_example_file_exists_and_parses():
    ast.parse(_example_source())


def test_slices_out_only_the_mcu_bound_code():
    result = slice_mcu_bound(_example_source(), base_dir=_EXAMPLE_PATH.parent)

    assert result.exported_names == {"blink"}
    assert "def blink" in result.source
    assert "def _get_led" in result.source
    assert "_led = None" in result.source
    # PC-only driver code must never be sliced onto the device.
    assert "mcu.connect" not in result.source
    assert '__name__ == "__main__"' not in result.source


def test_generated_bootstrap_is_syntactically_valid_python():
    sliced = slice_mcu_bound(_example_source(), base_dir=_EXAMPLE_PATH.parent)
    stubs = generate_pc_stubs(_example_source())
    bootstrap = generate_bootstrap(sliced.source, stubs.source)

    ast.parse(bootstrap)  # must not raise


@requires_micropython
def test_generated_bootstrap_runs_under_real_micropython_with_a_fake_pin():
    # Registration + dispatch wiring verified against the real interpreter,
    # same rigor as chunk 10's end-to-end bootstrap test - only `machine`
    # is faked (no real GPIO under the unix port), everything else in the
    # generated script is exactly what would be uploaded to a real board.
    sliced = slice_mcu_bound(_example_source(), base_dir=_EXAMPLE_PATH.parent)
    stubs = generate_pc_stubs(_example_source())
    bootstrap = generate_bootstrap(sliced.source, stubs.source)

    patched = bootstrap.replace(
        "    _reader = _tether_asyncio.StreamReader(_tether_sys.stdin.buffer)\n"
        "    _writer = _tether_asyncio.StreamWriter(_tether_sys.stdout.buffer, {})\n",
        "",
    ).replace("\n_tether_asyncio.run(_tether_main())\n", "\n")

    fake_machine = """
class _FakePin:
    OUT = 1
    def __init__(self, num, mode):
        pass
    def value(self, v):
        print("led:", v)

import sys as _sys
class _FakeMachine:
    Pin = _FakePin
_sys.modules["machine"] = _FakeMachine
"""

    out = run_micropython(
        fake_machine
        + PIPE_HARNESS
        + patched
        + """

import dispatch as _driver_dispatch

async def _run_test():
    asyncio.create_task(_tether_main())
    driver = _driver_dispatch.Dispatcher(reader_a, writer_a)
    asyncio.create_task(driver.run())

    async def _log_progress(blink_number, total):
        print("progress:", blink_number, total)
    driver.register("log_progress", _log_progress)

    await driver.call_pc("__tether_handshake__")
    await driver.call_pc("blink", 2)

asyncio.run(_run_test())
"""
    )

    assert "led: 1" in out
    assert "led: 0" in out
    assert "progress: 1 2" in out
    assert "progress: 2 2" in out
