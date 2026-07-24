"""Serial transport: USB VID/PID auto-discovery + raw-REPL code push.

Depends on `pyserial` (install via `tether[serial]`). See DESIGN.md § Transports.

Raw-REPL protocol implemented against the reference behavior documented in
MicroPython's own tools/pyboard.py
(https://github.com/micropython/micropython/blob/master/tools/pyboard.py).
Uses the standard (non-raw-paste) protocol — universally supported across
MicroPython versions, sufficient for this chunk's "push a script" scope.
Raw-paste mode (a newer, optional throughput optimization) is not
implemented — not needed for correctness, only speed.

Hand-rolled rather than depending on `mpremote` (the official MicroPython
tool, which has an equivalent `SerialTransport` class): that module's own
header states its internals are mid-refactor with no stability guarantee,
it's published as a CLI with no supported import surface, and importing it
would pull in the whole `mpremote` package (console/repl/mip/romfs) for one
class. A small, self-contained, test-covered implementation of the
documented protocol was the better tradeoff here.

No real hardware was available to validate this against while building it
— behavior is implemented faithfully against the documented protocol and
tested against scripted fakes, but real-device verification is chunk 15's
job (DESIGN.md § Testing: "README walkthrough verified against actual
hardware").
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

# Known (VID, PID) pairs for common MicroPython-capable USB-serial chips.
# Not exhaustive — covers common ESP32/RP2040 dev-board bridge chips, per
# DESIGN.md's "supporting ESP32 and similar" scope.
_KNOWN_VID_PID = frozenset(
    {
        (0x10C4, 0xEA60),  # Silicon Labs CP210x
        (0x1A86, 0x7523),  # QinHeng CH340
        (0x0403, 0x6001),  # FTDI FT232R
        (0x303A, 0x1001),  # Espressif native USB (ESP32-S2/S3/C3 USB-JTAG/Serial)
        (0x2E8A, 0x0005),  # Raspberry Pi RP2040 (bootloader)
        (0x2E8A, 0x000A),  # Raspberry Pi RP2040 (MicroPython/CircuitPython)
    }
)


class RawReplError(Exception):
    """Raised on any serial-discovery or raw-REPL protocol failure:
    unexpected response, timeout, ambiguous/missing device, or the
    executed code itself raising on-device (non-empty stderr).
    """


def discover(
    list_ports_fn: Callable[[], Any] | None = None,
    extra_vid_pid: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None,
) -> str:
    """Scan connected USB serial devices for a known MicroPython-capable
    VID/PID, return the matching port path. Raises RawReplError if zero or
    multiple ambiguous matches are found.

    `extra_vid_pid` adds (VID, PID) pairs to the built-in list, for boards
    with a bridge chip not already known - the built-in list is a
    convenience covering common chips, not exhaustive (DESIGN.md's
    "supporting ESP32 and similar" scope), and boards with an unlisted
    chip aren't otherwise locked out of auto-discovery.
    """
    if list_ports_fn is None:
        from serial.tools import list_ports

        list_ports_fn = list_ports.comports

    known = _KNOWN_VID_PID | extra_vid_pid if extra_vid_pid else _KNOWN_VID_PID
    matches = [p for p in list_ports_fn() if (p.vid, p.pid) in known]
    if not matches:
        raise RawReplError(
            "no known MicroPython-capable USB serial device found "
            "(pass an explicit port, or extra_vid_pid=... for an unlisted board)"
        )
    if len(matches) > 1:
        ports = ", ".join(p.device for p in matches)
        raise RawReplError(f"multiple matching devices found ({ports}); specify a port explicitly")
    return matches[0].device


def _read_until(serial_obj: Any, ending: bytes, timeout: float) -> bytes:
    # Byte-at-a-time is fine here: only used during raw-REPL negotiation
    # (short, hash-gated-per-upload responses), never on the live
    # connection's hot path. Don't reuse this pattern for that — see
    # SerialStream.read() for the hot-path-appropriate approach.
    data = bytearray()
    deadline = time.monotonic() + timeout
    while not bytes(data).endswith(ending):
        if time.monotonic() >= deadline:
            raise RawReplError(f"timed out waiting for {ending!r}, got {bytes(data)!r}")
        chunk = serial_obj.read(1)
        if chunk:
            data.extend(chunk)
    return bytes(data)


def _enter_raw_repl(serial_obj: Any, timeout: float) -> None:
    serial_obj.write(b"\r\x03")  # ctrl-C: interrupt any running program

    while serial_obj.in_waiting:
        serial_obj.read(serial_obj.in_waiting)

    serial_obj.write(b"\r\x01")  # ctrl-A: enter raw REPL
    _read_until(serial_obj, b"raw REPL; CTRL-B to exit\r\n", timeout=timeout)


def _exit_raw_repl(serial_obj: Any) -> None:
    serial_obj.write(b"\r\x02")  # ctrl-B: back to friendly REPL


def _exec_raw_start(serial_obj: Any, code: bytes, timeout: float) -> None:
    """Send `code` for execution and wait for the "OK" ack confirming the
    device received and started it. Does not wait for it to finish -
    splitting this from reading the output is what makes wait=False
    (fire-and-forget, for code that runs forever - see push_raw_repl)
    possible: after this returns, the code is running on-device regardless
    of whether the caller goes on to collect its output.
    """
    _read_until(serial_obj, b">", timeout=timeout)

    # 256 bytes / 10ms matches MicroPython's own tools/pyboard.py exactly -
    # deliberately not tuned faster. Scales linearly with code size (~400ms
    # per 10KB), but with no real device to validate a faster pace against,
    # keeping the reference implementation's conservative, well-tested
    # pacing is the safer choice over risking device input-buffer overruns.
    for i in range(0, len(code), 256):
        serial_obj.write(code[i : i + 256])
        time.sleep(0.01)
    serial_obj.write(b"\x04")  # ctrl-D: execute

    ack = serial_obj.read(2)
    if ack != b"OK":
        raise RawReplError(f"could not exec code (response: {ack!r})")


def _follow_exec(serial_obj: Any, timeout: float) -> tuple[bytes, bytes]:
    """Wait for `code`'s stdout/stderr, which only arrive once it *returns*.
    Only call this for code expected to finish - see push_raw_repl(wait=).
    """
    stdout = _read_until(serial_obj, b"\x04", timeout=timeout)[:-1]
    stderr = _read_until(serial_obj, b"\x04", timeout=timeout)[:-1]
    return stdout, stderr


def push_raw_repl(
    serial_obj: Any, code: bytes, *, timeout: float = 10.0, wait: bool = True
) -> None:
    """Upload and execute `code` on the connected MicroPython board via the
    raw REPL protocol, then return to the friendly REPL.

    `wait=True` (default) blocks until `code` finishes executing and raises
    RawReplError if it raised on-device (non-empty stderr) - appropriate
    for code that's expected to return, like a file-write helper.

    `wait=False` returns as soon as the device acks receiving the code (it
    has started running), without waiting for it to finish. Required for
    code that runs forever - e.g. starting chunk 6's dispatch loop, which
    never returns. `wait=True` against such code would block until
    `timeout` and then raise a spurious RawReplError, even though the code
    is running successfully; exiting raw REPL (ctrl-B) does not interrupt
    it. Chunk 10 is expected to use wait=False for that call and is
    responsible for confirming the dispatch loop actually came up (e.g. via
    the protocol-version handshake), not this function.
    """
    _enter_raw_repl(serial_obj, timeout=timeout)
    try:
        _exec_raw_start(serial_obj, code, timeout=timeout)
        if wait:
            _stdout, stderr = _follow_exec(serial_obj, timeout=timeout)
            if stderr:
                raise RawReplError(f"code raised on device: {stderr.decode(errors='replace')}")
    finally:
        _exit_raw_repl(serial_obj)


class SerialStream:
    """Wraps an open pyserial `Serial` object to satisfy
    `tether.dispatch.Dispatcher`'s reader/writer contract: blocking
    `read()` with no size argument, returning a reasonably-sized chunk of
    whatever's readily available — not `pyserial`'s naive `read()` default
    of `size=1`, and not a large fixed-size `read(N)` either, since with
    `timeout=None` (the expected configuration — see below) `Serial.read(N)`
    blocks until all N bytes arrive rather than returning early with
    whatever's already buffered. See CHUNKS.md chunk 9's pinned constraint
    from chunk 7's review.

    Expects `serial_obj` to have `timeout=None` (pyserial's default) —
    blocking reads, not the non-blocking/timeout-tuple variants.
    """

    def __init__(self, serial_obj: Any) -> None:
        self._serial = serial_obj

    def read(self) -> bytes:
        first = self._serial.read(1)  # blocks until at least 1 byte arrives
        if not first:
            return first
        waiting = self._serial.in_waiting
        if waiting:
            first += self._serial.read(waiting)
        return first

    def write(self, data: bytes) -> None:
        self._serial.write(data)
