import pytest

from tether.slicer import generate_pc_stubs


def test_generates_async_stub_for_pc_export_function():
    source = """
from tether import pc

@pc.export
def log_event(msg: str) -> None:
    print(msg)
"""
    result = generate_pc_stubs(source)

    assert "async def log_event(msg: str) -> None:" in result.source
    assert "call_pc(" in result.source
    assert "log_event" in result.source.split("call_pc(", 1)[1]
    assert result.stubbed_names == frozenset({"log_event"})


def test_stub_preserves_full_signature():
    source = """
from tether import pc

@pc.export
def record(label: str, value: int) -> bool:
    return True
"""
    result = generate_pc_stubs(source)

    assert "async def record(label: str, value: int) -> bool:" in result.source
    call_args = result.source.split("call_pc(", 1)[1].split(")")[0]
    assert "label" in call_args
    assert "value" in call_args


def test_ignores_mcu_export_functions():
    source = """
from tether import mcu, pc

@mcu.export
def read_temp() -> float:
    return 21.5

@pc.export
def log_event(msg: str) -> None:
    print(msg)
"""
    result = generate_pc_stubs(source)

    assert result.stubbed_names == frozenset({"log_event"})
    assert "read_temp" not in result.source


def test_recognizes_pc_decorator_through_import_alias():
    source = """
from tether import pc as p

@p.export
def log_event(msg: str) -> None:
    print(msg)
"""
    result = generate_pc_stubs(source)

    assert result.stubbed_names == frozenset({"log_event"})


def test_stub_for_no_arg_function():
    source = """
from tether import pc

@pc.export
def ping() -> None:
    pass
"""
    result = generate_pc_stubs(source)

    assert "async def ping() -> None:" in result.source
    assert "call_pc('ping')" in result.source


def test_rejects_keyword_only_param_instead_of_silently_dropping_it():
    # generate_pc_stubs parses raw source text - it doesn't run
    # decorators.py's _validate_signature, so it can't assume decoration-time
    # validation already ran (e.g. called directly, or on source that hasn't
    # been imported yet). A keyword-only param must fail loudly here too,
    # not silently vanish from the forwarded call_pc(...) args.
    source = """
from tether import pc

@pc.export
def record(label: str, *, value: int) -> None:
    pass
"""
    with pytest.raises(ValueError, match="value"):
        generate_pc_stubs(source)
