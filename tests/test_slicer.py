from tether.slicer import slice_mcu_bound


def test_single_decorated_function_with_no_dependencies():
    source = """
from tether import mcu

@mcu.export
def read_temp() -> float:
    return 21.5
"""
    result = slice_mcu_bound(source)

    assert "def read_temp() -> float:" in result.source
    assert "return 21.5" in result.source
    assert result.exported_names == frozenset({"read_temp"})


def test_pulls_in_referenced_module_level_assignment():
    source = """
from tether import mcu

led = Pin(2, Pin.OUT)

@mcu.export
def blink() -> None:
    led.toggle()
"""
    result = slice_mcu_bound(source)

    assert "led = Pin(2, Pin.OUT)" in result.source
    assert "def blink() -> None:" in result.source


def test_pulls_in_helper_function_transitively():
    source = """
from tether import mcu

def _to_celsius(raw: int) -> float:
    return raw / 100

@mcu.export
def read_temp() -> float:
    return _to_celsius(2150)
"""
    result = slice_mcu_bound(source)

    assert "def _to_celsius(raw: int) -> float:" in result.source
    assert "def read_temp() -> float:" in result.source


def test_excludes_unreferenced_module_level_code():
    source = """
from tether import mcu, pc

def unused_helper() -> int:
    return 1

@mcu.export
def read_temp() -> float:
    return 21.5

@pc.export
def log_event(msg: str) -> None:
    print(msg)
"""
    result = slice_mcu_bound(source)

    assert "unused_helper" not in result.source
    assert "log_event" not in result.source
    assert "def read_temp() -> float:" in result.source
    assert result.exported_names == frozenset({"read_temp"})


def test_pulls_in_import_referenced_by_included_assignment():
    source = """
from tether import mcu
from machine import Pin
import time

led = Pin(2, Pin.OUT)

@mcu.export
def blink() -> None:
    led.toggle()
"""
    result = slice_mcu_bound(source)

    assert "from machine import Pin" in result.source
    assert "import time" not in result.source


def test_never_includes_the_pc_side_tether_import():
    # `from tether import mcu` is a PC-only import — `tether` isn't
    # installed on the MCU (only `tether_runtime` is). The decorator's own
    # `mcu` reference (via its decorator_list, which ast.walk also visits)
    # must not pull this import in as if it were a real dependency.
    source = """
from tether import mcu

@mcu.export
def read_temp() -> float:
    return 21.5
"""
    result = slice_mcu_bound(source)

    assert "tether" not in result.source


def test_never_includes_bare_import_tether_or_submodules():
    source = """
import tether
import tether.decorators
from tether_runtime import dispatch
from tether.decorators import mcu

x = dispatch

@mcu.export
def read_temp() -> float:
    return 21.5 if x else 0.0
"""
    result = slice_mcu_bound(source)

    assert "import tether" not in result.source
    assert "from tether.decorators" not in result.source
    # tether_runtime is a distinct, legitimate MCU-side package (not the
    # PC-only `tether` package) and must not get caught by a naive prefix
    # match on the string "tether".
    assert "from tether_runtime import dispatch" in result.source


def test_mcu_loop_functions_are_exported_too():
    source = """
from tether import mcu

@mcu.loop(interval_ms=100)
def poll() -> None:
    pass
"""
    result = slice_mcu_bound(source)

    assert result.exported_names == frozenset({"poll"})
    assert "def poll() -> None:" in result.source


def test_pulls_in_referenced_class_definition():
    source = """
from tether import mcu

class Sensor:
    def read(self) -> float:
        return 1.0

sensor = Sensor()

@mcu.export
def read_temp() -> float:
    return sensor.read()
"""
    result = slice_mcu_bound(source)

    assert "class Sensor:" in result.source
    assert "sensor = Sensor()" in result.source


def test_follows_local_import_and_inlines_referenced_function(tmp_path):
    (tmp_path / "sensors.py").write_text(
        "def to_celsius(raw: int) -> float:\n    return raw / 100\n"
    )
    entry_source = """
from tether import mcu
from sensors import to_celsius

@mcu.export
def read_temp() -> float:
    return to_celsius(2150)
"""
    result = slice_mcu_bound(entry_source, base_dir=tmp_path)

    assert "def to_celsius(raw: int) -> float:" in result.source
    assert "from sensors import to_celsius" not in result.source
    assert "def read_temp() -> float:" in result.source


def test_does_not_follow_non_local_imports(tmp_path):
    entry_source = """
from tether import mcu
from machine import Pin

@mcu.export
def read_temp() -> float:
    return 21.5 if Pin else 0.0
"""
    result = slice_mcu_bound(entry_source, base_dir=tmp_path)

    assert "from machine import Pin" in result.source


def test_cross_file_dependencies_render_in_dependency_order(tmp_path):
    # SCALE's assignment evaluates BASE immediately at module load, so
    # BASE's statement must render before SCALE's, not just "somewhere".
    (tmp_path / "config.py").write_text("BASE = 10\nSCALE = BASE * 2\n")
    entry_source = """
from tether import mcu
from config import SCALE

@mcu.export
def read_scaled() -> int:
    return SCALE
"""
    result = slice_mcu_bound(entry_source, base_dir=tmp_path)

    assert result.source.index("BASE = 10") < result.source.index("SCALE = BASE * 2")


def test_recognizes_decorator_through_import_alias():
    source = """
from tether import mcu as m

@m.export
def read_temp() -> float:
    return 21.5
"""
    result = slice_mcu_bound(source)

    assert result.exported_names == frozenset({"read_temp"})


def test_recognizes_async_mcu_export_function():
    # Async @mcu.export is a real, required case - MCU code that needs to
    # call back into a @pc.export stub (chunk 4) must itself be async to
    # await it (DESIGN.md § Call semantics).
    source = """
from tether import mcu

@mcu.export
async def read_scaled() -> int:
    return 42
"""
    result = slice_mcu_bound(source)

    assert "async def read_scaled() -> int:" in result.source
    assert result.exported_names == frozenset({"read_scaled"})


def test_pulls_in_referenced_async_helper_function():
    source = """
from tether import mcu

async def _slow_read() -> int:
    return 42

@mcu.export
async def read_scaled() -> int:
    return await _slow_read()
"""
    result = slice_mcu_bound(source)

    assert "async def _slow_read() -> int:" in result.source
