"""`@mcu.export`, `@mcu.loop`, `@pc.export` — the user-facing decorator API.

Kept deliberately simple/statically-analyzable (no metaclass or exec-based
magic): a future language server needs to infer signatures and cross-boundary
call targets by walking plain decorator calls. See DESIGN.md § Standing
design constraint.

`src/tether_runtime/mcu_decorators.py` mirrors this API's shape (`.export`,
`.loop`, the same kwargs) for the on-device side — the two can never share
an import (different runtimes: CPython here, MicroPython there; MicroPython
function objects also don't support the attribute-tagging this file uses).
If you change this file's decorator signatures, check whether that file
needs the same change - there's no automated check tying them together.
"""

from __future__ import annotations

import functools
import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from tether._context import current_board

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
            spec = ExportSpec(
                func=fn,
                side="mcu",
                timeout=timeout,
                heartbeat_interval=heartbeat_interval,
            )

            # The returned callable is NOT `fn` - `fn`'s body only ever
            # runs on the MCU (the slicer works from source text, so this
            # substitution doesn't affect what gets uploaded), and must
            # never execute here on the PC. Calling the decorated name
            # directly (no `board.<name>()`) dispatches through whichever
            # BoardHandle is ambient (see tether/_context.py) - the "call
            # it like a normal Python function, with no awareness of where
            # it runs" story. `board.<name>()` still works too (explicit,
            # bypasses ambient lookup) - this is additive, not a
            # replacement.
            @functools.wraps(fn)
            def dispatch(*args: Any, **kwargs: Any) -> Any:
                board = current_board.get()
                if board is None:
                    raise RuntimeError(
                        f"{fn.__name__}() called with no board in context - call "
                        f"mcu.connect(...) first, or wrap the call in `with board:` "
                        f"if you're working with more than one board"
                    )
                # Reuses BoardHandle's own cached call closure, which
                # already accepts an optional `timeout=` override
                # (connection.py's __getattr__). No timeout kwarg here
                # means none in `kwargs` either, so the closure's own
                # default (spec.timeout, or DEFAULT_TIMEOUT) applies
                # untouched - forwarding kwargs plainly, rather than
                # defaulting timeout ourselves, is what avoids silently
                # passing timeout=None ("wait forever") on every ambient
                # call that didn't ask for it.
                return getattr(board, fn.__name__)(*args, **kwargs)

            dispatch.__tether_export__ = spec  # type: ignore[attr-defined]
            return dispatch  # type: ignore[return-value]

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


_SCALAR_TYPES = (int, float, bool, str, bytes)
_CONTAINER_ORIGINS = (list, dict)


def _is_supported_type(annotation: Any) -> bool:
    """Recursively check an annotation against the v1 wire type set.

    See DESIGN.md § Wire protocol: int, float, bool, str, bytes, list, dict,
    recursively, plus None for functions with no return value.
    """
    if annotation is None or annotation is type(None):
        return True
    if annotation in _SCALAR_TYPES:
        return True
    origin = typing.get_origin(annotation)
    if annotation in _CONTAINER_ORIGINS or origin in _CONTAINER_ORIGINS:
        args = typing.get_args(annotation)
        return all(_is_supported_type(a) for a in args)
    return False


def _validate_signature(fn: Callable[..., Any]) -> None:
    """Reject unsupported param/return types at decoration time.

    Supported types (DESIGN.md § Wire protocol): int, float, bool, str,
    bytes, list, dict (recursively, msgpack-safe values only). Registry hook
    for custom types is a placeholder in v1 — always empty.
    """
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)

    _UNSUPPORTED_PARAM_KINDS = (
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_ONLY,
    )
    for name, param in sig.parameters.items():
        if param.kind in _UNSUPPORTED_PARAM_KINDS:
            raise TypeError(
                f"{fn.__qualname__}: parameter {name!r} has unsupported kind "
                f"{param.kind.name} (fixed positional-or-keyword arity only)"
            )
        if name not in hints:
            raise TypeError(f"{fn.__qualname__}: parameter {name!r} has no type hint")
        if not _is_supported_type(hints[name]):
            raise TypeError(
                f"{fn.__qualname__}: parameter {name!r} has unsupported type {hints[name]!r}"
            )

    if "return" not in hints:
        raise TypeError(f"{fn.__qualname__}: missing return type hint")
    if not _is_supported_type(hints["return"]):
        raise TypeError(f"{fn.__qualname__}: unsupported return type {hints['return']!r}")


mcu = _McuNamespace()
pc = _PcNamespace()
