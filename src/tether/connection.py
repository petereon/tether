"""`mcu.connect(...)` — the wire→probe→use entrypoint. See DESIGN.md § Architecture overview."""

from __future__ import annotations

from typing import Any


class BoardHandle:
    """Returned by `connect()`. Decorated `@mcu.export` functions are exposed
    as bound attributes here — never as ambiguous global calls (DESIGN.md,
    multi-board routing).
    """

    def __init__(self, address: str) -> None:
        self.address = address

    def reconnect(self) -> None:
        """Explicit re-attach after MCUDisconnectedError. Never automatic."""
        raise NotImplementedError

    def __getattr__(self, name: str) -> Any:
        raise NotImplementedError(f"call dispatch for {name!r} not yet implemented")


def connect(address: str) -> BoardHandle:
    """Slice -> stub -> bundle -> hash-check -> upload -> handshake -> ready.

    `address` scheme selects transport:
      "serial:auto" | "serial:/dev/ttyUSB0" | "wifi:<ip>" | "ble:<addr>" | "mock://"
    """
    raise NotImplementedError
