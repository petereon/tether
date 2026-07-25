import pytest

from tether import mcu


def test_mcu_export_accepts_supported_scalar_types():
    @mcu.export
    def read_temp(scale: str, precision: int) -> float:
        return 0.0

    spec = read_temp.__tether_export__
    assert spec.side == "mcu"
    # `read_temp` (post-decoration) is a PC-side dispatch wrapper, not the
    # original function - it must never run the original body locally (see
    # decorators.py's own comment). `spec.func` is the real, undecorated
    # callable instead - used e.g. by transports/mock.py to register the
    # actual handler on the simulated device side. Calling it directly
    # (bypassing the wrapper entirely) proves it's the genuine original,
    # not just an object with the right shape.
    assert spec.func(scale="C", precision=2) == 0.0


def test_mcu_export_rejects_missing_param_type_hint():
    with pytest.raises(TypeError, match="scale"):

        @mcu.export
        def read_temp(scale, precision: int) -> float:
            return 0.0


def test_mcu_export_rejects_missing_return_type_hint():
    with pytest.raises(TypeError, match="return"):

        @mcu.export
        def read_temp(precision: int):
            return 0.0


class _CustomSensor:
    pass


def test_mcu_export_rejects_unsupported_param_type():
    with pytest.raises(TypeError, match="sensor"):

        @mcu.export
        def read_temp(sensor: _CustomSensor) -> float:
            return 0.0


def test_pc_export_accepts_none_return():
    from tether import pc

    @pc.export
    def log_event(msg: str) -> None:
        pass

    assert log_event.__tether_export__.side == "pc"


def test_mcu_export_rejects_var_positional_args():
    with pytest.raises(TypeError, match="args"):

        @mcu.export
        def read_temp(*args: int) -> float:
            return 0.0


def test_mcu_export_rejects_var_keyword_args():
    with pytest.raises(TypeError, match="kwargs"):

        @mcu.export
        def read_temp(**kwargs: int) -> float:
            return 0.0


def test_mcu_export_rejects_keyword_only_args():
    # Fixed positional-or-keyword arity only: the wire protocol has no
    # concept of keyword-vs-positional, and the slicer's @pc.export stub
    # generator forwards args purely positionally (src/tether/slicer) - a
    # keyword-only param would silently never reach the wire.
    with pytest.raises(TypeError, match="y"):

        @mcu.export
        def read_temp(x: int, *, y: int) -> float:
            return 0.0


def test_mcu_export_rejects_positional_only_args():
    with pytest.raises(TypeError, match="x"):

        @mcu.export
        def read_temp(x: int, /) -> float:
            return 0.0


def test_mcu_export_accepts_nested_generic_containers():
    @mcu.export
    def read_samples(labels: list[str]) -> dict[str, int]:
        return {}

    assert read_samples.__tether_export__.side == "mcu"


def test_mcu_export_rejects_generic_container_of_unsupported_type():
    with pytest.raises(TypeError, match="sensors"):

        @mcu.export
        def read_all(sensors: list[_CustomSensor]) -> float:
            return 0.0


def test_mcu_export_rejects_tuple_type():
    with pytest.raises(TypeError, match="coords"):

        @mcu.export
        def read_coords(coords: tuple) -> float:
            return 0.0


def test_mcu_export_with_timeout_and_heartbeat_kwargs():
    @mcu.export(timeout=10, heartbeat_interval=1)
    def spin_motor(duration: int) -> None:
        pass

    spec = spin_motor.__tether_export__
    assert spec.timeout == 10
    assert spec.heartbeat_interval == 1


def test_mcu_loop_sets_interval_ms():
    @mcu.loop(interval_ms=100)
    def poll() -> None:
        pass

    spec = poll.__tether_export__
    assert spec.side == "mcu"
    assert spec.interval_ms == 100


def test_mcu_loop_rejects_unsupported_type():
    with pytest.raises(TypeError, match="sensor"):

        @mcu.loop(interval_ms=100)
        def poll(sensor: _CustomSensor) -> None:
            pass


def test_mcu_export_accepts_none_nested_in_generic_container():
    # typing.get_args() yields a bare `None` (not `type(None)`) for nested
    # positions like this, unlike top-level return annotations — regression
    # guard for that asymmetry in _is_supported_type.
    @mcu.export
    def read_optional_map(data: dict[str, None]) -> int:
        return 0

    assert read_optional_map.__tether_export__.side == "mcu"
