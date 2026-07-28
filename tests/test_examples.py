"""Chunk 15: validates the examples/ walkthrough as thoroughly as possible
without real hardware. These tests verify each example slices correctly,
produces a syntactically valid on-device bundle, and that bundle's
registration/dispatch wiring actually works under a real MicroPython
interpreter (machine.Pin faked out, since the unix-port interpreter has no
real GPIO - see conftest-less inline fake below). They do NOT verify that
an LED actually blinks on a real board - see docs/CHUNKS.md's entry for
chunk 15, and the wifi/BLE examples' own real-hardware verification notes
in docs/CHUNKS.md's follow-up addendum, for that.

wifi_blink.py and ble_blink.py (examples/wifi_blink/,
examples/ble_blink/) share the exact same @mcu.export/@pc.export code as
blink_and_log.py - only the driver block's `mcu.connect()` address and
credential source differ, and slicing strips the driver block entirely -
so all three examples are exercised through the same parametrized checks
below.
"""

import ast
import sys
from pathlib import Path

import pytest

from tether.connection import generate_bootstrap
from tether.slicer import generate_pc_stubs, slice_mcu_bound

sys.path.insert(0, str(Path(__file__).parent))
from mpy_runner import PIPE_HARNESS, requires_micropython, run_micropython

_EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
_EXAMPLE_PATHS = [
    _EXAMPLES_DIR / "blink_and_log" / "blink_and_log.py",
    _EXAMPLES_DIR / "wifi_blink" / "wifi_blink.py",
    _EXAMPLES_DIR / "ble_blink" / "ble_blink.py",
]


def _example_source(path: Path) -> str:
    return path.read_text()


@pytest.mark.parametrize("example_path", _EXAMPLE_PATHS, ids=lambda p: p.stem)
def test_example_file_exists_and_parses(example_path: Path):
    ast.parse(_example_source(example_path))


@pytest.mark.parametrize("example_path", _EXAMPLE_PATHS, ids=lambda p: p.stem)
def test_slices_out_only_the_mcu_bound_code(example_path: Path):
    result = slice_mcu_bound(_example_source(example_path), base_dir=example_path.parent)

    assert result.exported_names == {"blink"}
    assert "def blink" in result.source
    assert "def _get_led" in result.source
    assert "_led = None" in result.source
    # PC-only driver code must never be sliced onto the device.
    assert "mcu.connect" not in result.source
    assert '__name__ == "__main__"' not in result.source


@pytest.mark.parametrize("example_path", _EXAMPLE_PATHS, ids=lambda p: p.stem)
def test_generated_bootstrap_is_syntactically_valid_python(example_path: Path):
    source = _example_source(example_path)
    sliced = slice_mcu_bound(source, base_dir=example_path.parent)
    stubs = generate_pc_stubs(source)
    bootstrap = generate_bootstrap(sliced.source, stubs.source)

    ast.parse(bootstrap)  # must not raise


@requires_micropython
@pytest.mark.parametrize("example_path", _EXAMPLE_PATHS, ids=lambda p: p.stem)
def test_generated_bootstrap_runs_under_real_micropython_with_a_fake_pin(example_path: Path):
    # Registration + dispatch wiring verified against the real interpreter,
    # same rigor as chunk 10's end-to-end bootstrap test - only `machine`
    # is faked (no real GPIO under the unix port), everything else in the
    # generated script is exactly what would be uploaded to a real board.
    source = _example_source(example_path)
    sliced = slice_mcu_bound(source, base_dir=example_path.parent)
    stubs = generate_pc_stubs(source)
    bootstrap = generate_bootstrap(sliced.source, stubs.source)

    patched = bootstrap.replace(
        '    _override = globals().get("_tether_stream_override")\n'
        "    if _override is not None:\n"
        "        _reader, _writer = _override\n"
        "    else:\n"
        "        _reader = _tether_asyncio.StreamReader(_tether_sys.stdin.buffer)\n"
        "        _writer = _tether_asyncio.StreamWriter(_tether_sys.stdout.buffer, {})\n",
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


@pytest.mark.parametrize(
    "example_path,expected_scheme",
    [
        (_EXAMPLES_DIR / "wifi_blink" / "wifi_blink.py", 'mcu.connect(f"wifi:{ip}")'),
        (_EXAMPLES_DIR / "ble_blink" / "ble_blink.py", 'mcu.connect(f"ble:{address}")'),
    ],
    ids=lambda v: v if isinstance(v, str) else v.stem,
)
def test_driver_block_connects_with_the_right_transport_scheme(
    example_path: Path, expected_scheme: str
):
    source = _example_source(example_path)
    assert expected_scheme in source
    # Both examples demonstrate reconnect() without a physical reset - the
    # whole reason to provision wifi/BLE over plain serial in the first
    # place (see each file's own docstring).
    assert "board.reconnect()" in source
