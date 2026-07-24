"""Lightweight on-device decorator shim.

Sliced `@mcu.export`/`@mcu.loop` source (chunk 3) preserves decorator
syntax as-is - it needs *something* named `mcu`/`pc` in scope on-device to
resolve, since the real `tether` package (chunk 1's decorators, with full
type/arity validation) is PC-only and never installed on the MCU. That
validation already ran on the PC side before the code was ever sliced -
this shim only needs to record enough metadata for chunk 10's generated
bootstrap to register functions with a Dispatcher (chunk 6).

Registers into a module-level list rather than tagging the function object
itself (e.g. `fn.__tether_export__ = ...`, chunk 1's PC-side approach) -
MicroPython function objects don't support arbitrary attribute assignment
the way CPython's do (verified against a real interpreter; this is not a
CPython-only quirk to work around, it's a hard on-device constraint).

Not imported by PC code. Bundled onto the device alongside dispatch.py and
umsgpack/ by chunk 10's connect().
"""

_registrations = []  # list of (side, name, func, heartbeat_interval_ms, interval_ms)


class _McuNamespace:
    def export(self, func=None, *, timeout=None, heartbeat_interval=None):
        heartbeat_ms = None
        if heartbeat_interval is not None:
            heartbeat_ms = int(heartbeat_interval * 1000)

        def decorate(fn):
            _registrations.append(("mcu", fn.__name__, fn, heartbeat_ms, None))
            return fn

        return decorate(func) if func is not None else decorate

    def loop(self, *, interval_ms):
        def decorate(fn):
            _registrations.append(("mcu", fn.__name__, fn, None, interval_ms))
            return fn

        return decorate


class _PcNamespace:
    def export(self, func=None):
        def decorate(fn):
            _registrations.append(("pc", fn.__name__, fn, None, None))
            return fn

        return decorate(func) if func is not None else decorate


mcu = _McuNamespace()
pc = _PcNamespace()


def registered_mcu_functions():
    """Yields (name, func, heartbeat_interval_ms, interval_ms) for every
    @mcu.export/@mcu.loop-tagged function, in decoration order.
    """
    for side, name, fn, heartbeat_ms, interval_ms in _registrations:
        if side == "mcu":
            yield name, fn, heartbeat_ms, interval_ms
