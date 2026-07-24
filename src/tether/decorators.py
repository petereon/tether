"""`@mcu.export`, `@mcu.loop`, `@pc.export` — the user-facing decorator API.

Kept deliberately simple/statically-analyzable (no metaclass or exec-based
magic): a future language server needs to infer signatures and cross-boundary
call targets by walking plain decorator calls. See DESIGN.md § Standing
design constraint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class ExportSpec:
    """Metadata attached to a decorated function, read by the slicer (Q1/Q19)."""

    func: Callable[..., Any]
    side: str  # "mcu" | "pc"
    timeout: float | None = None
    heartbeat_interval: float | None = None
    interval_ms: int | None = None  # set only for @mcu.loop


class _McuNamespace:
    """`@mcu.export` / `@mcu.loop` decorators. See DESIGN.md § Call semantics."""

    def export(
        self,
        func: F | None = None,
        *,
        timeout: float | None = None,
        heartbeat_interval: float | None = None,
    ) -> Callable[[F], F]:
        def decorate(fn: F) -> F:
            _validate_signature(fn)  # raises at decoration time; see DESIGN.md Q4
            fn.__tether_export__ = ExportSpec(  # type: ignore[attr-defined]
                func=fn,
                side="mcu",
                timeout=timeout,
                heartbeat_interval=heartbeat_interval,
            )
            return fn

        return decorate(func) if func is not None else decorate

    def loop(self, *, interval_ms: int) -> Callable[[F], F]:
        def decorate(fn: F) -> F:
            _validate_signature(fn)
            fn.__tether_export__ = ExportSpec(  # type: ignore[attr-defined]
                func=fn, side="mcu", interval_ms=interval_ms
            )
            return fn

        return decorate


class _PcNamespace:
    """`@pc.export` decorator — MCU side gets an auto-generated proxy stub."""

    def export(self, func: F | None = None) -> Callable[[F], F]:
        def decorate(fn: F) -> F:
            _validate_signature(fn)
            fn.__tether_export__ = ExportSpec(func=fn, side="pc")  # type: ignore[attr-defined]
            return fn

        return decorate(func) if func is not None else decorate


def _validate_signature(fn: Callable[..., Any]) -> None:
    """Reject unsupported param/return types at decoration time.

    Supported types (DESIGN.md § Wire protocol): int, float, bool, str,
    bytes, list, dict (recursively, msgpack-safe values only). Registry hook
    for custom types is a placeholder in v1 — always empty.

    TODO: inspect fn's type hints, raise TypeError naming the offending
    param/return annotation if it falls outside the supported set.
    """


mcu = _McuNamespace()
pc = _PcNamespace()
