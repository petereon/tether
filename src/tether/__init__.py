"""tether — call MicroPython functions from Python and back.

See docs/DESIGN.md for the locked architecture this package implements.
"""

from tether.decorators import mcu, pc
from tether.errors import (
    MCUDisconnectedError,
    MCUTimeoutError,
    ProtocolVersionError,
    RemoteError,
    TetherError,
)

__all__ = [
    "mcu",
    "pc",
    "TetherError",
    "RemoteError",
    "MCUTimeoutError",
    "MCUDisconnectedError",
    "ProtocolVersionError",
]
