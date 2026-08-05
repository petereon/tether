"""Error hierarchy for cross-boundary calls. See DESIGN.md § Wire protocol."""

from __future__ import annotations


class TetherError(Exception):
    """Base class for all tether-raised errors."""


class RemoteError(TetherError):
    """A remote (MCU- or PC-side) function raised during a cross-boundary call.

    Wraps the original exception rather than re-raising its exact type, since
    the raising side's exception classes aren't guaranteed to exist or match
    constructor signatures on the receiving side.
    """

    def __init__(self, remote_type: str, message: str, remote_traceback: str) -> None:
        super().__init__(f"{remote_type}: {message}")
        self.remote_type = remote_type
        self.remote_traceback = remote_traceback


class MCUTimeoutError(TetherError):
    """No response and no heartbeat within the configured timeout window."""


class MCUDisconnectedError(TetherError):
    """The transport connection was lost during or before a call."""


class ProtocolVersionError(TetherError):
    """PC-side library and on-device runtime speak incompatible protocol versions."""


class WifiAuthError(TetherError):
    """The wifi listener rejected this connection's shared secret (or its
    absence) during the mode-selection preamble.
    """


class FrameAuthenticationError(TetherError):
    """A per-frame HMAC tag or replay counter failed verification on an
    authenticated wifi (or future BLE) connection.

    Always fatal: the connection is closed immediately after this is
    raised, never retried on the same frame - see DESIGN.md § Transports'
    Wifi row, "Per-frame authentication".
    """
