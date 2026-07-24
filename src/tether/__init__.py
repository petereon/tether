"""tether — call MicroPython functions from Python and back.

See docs/DESIGN.md for the locked architecture this package implements.
"""

from tether.connection import connect
from tether.decorators import mcu, pc
from tether.errors import (
    MCUDisconnectedError,
    MCUTimeoutError,
    ProtocolVersionError,
    RemoteError,
    TetherError,
)

# DESIGN.md's own architecture doc (and every example) documents
# `mcu.connect(...)`, not a bare top-level `connect(...)` - found by
# actually running the library as a real user would, against real
# hardware, rather than through tests that all imported `connect` directly
# from tether.connection and never exercised this. Assigned as a PLAIN
# function onto the instance (not defined in _McuNamespace's class body) -
# Python only binds `self` via the descriptor protocol for functions found
# on the class, not ones set directly on an instance, so `mcu.connect(...)`
# invokes this exact function with no `self` injected and no extra stack
# frame - connect()'s own `_capture_caller()` frame-counting is unaffected.
mcu.connect = connect

__all__ = [
    "MCUDisconnectedError",
    "MCUTimeoutError",
    "ProtocolVersionError",
    "RemoteError",
    "TetherError",
    "connect",
    "mcu",
    "pc",
]
