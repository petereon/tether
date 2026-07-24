"""Serial transport: USB VID/PID auto-discovery + raw-REPL code push.

Depends on `pyserial` (install via `tether[serial]`). See DESIGN.md § Transports.
"""

from __future__ import annotations


def discover() -> str:
    """Scan connected USB serial devices for a known MicroPython board
    VID/PID, return the matching port path. Raises if zero or multiple
    ambiguous matches."""
    raise NotImplementedError


def push_raw_repl(port: str, bundle: bytes) -> None:
    """Upload the sliced+bundled script via raw-REPL, then soft-reset."""
    raise NotImplementedError
