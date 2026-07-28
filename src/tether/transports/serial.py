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

import base64
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


def list_devices(
    list_ports_fn: Callable[[], Any] | None = None,
    extra_vid_pid: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None,
) -> list[str]:
    """Like `discover()`, but returns every matching port instead of
    requiring exactly one. `discover()` treats ambiguity as an error (the
    right default for the library's `serial:auto` scheme) - the CLI wants
    all matches instead, to let the user pick interactively.
    """
    if list_ports_fn is None:
        from serial.tools import list_ports

        list_ports_fn = list_ports.comports

    known = _KNOWN_VID_PID | extra_vid_pid if extra_vid_pid else _KNOWN_VID_PID
    return [p.device for p in list_ports_fn() if (p.vid, p.pid) in known]


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
    matches = list_devices(list_ports_fn, extra_vid_pid)
    if not matches:
        raise RawReplError(
            "no known MicroPython-capable USB serial device found "
            "(pass an explicit port, or extra_vid_pid=... for an unlisted board)"
        )
    if len(matches) > 1:
        ports = ", ".join(matches)
        raise RawReplError(f"multiple matching devices found ({ports}); specify a port explicitly")
    return matches[0]


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


def reset_board(serial_obj: Any) -> None:
    """Hardware-reset the board via its RTS/DTR-wired EN and GPIO0 pins -
    the classic ESP32 auto-reset circuit.

    Needed before every raw-REPL interaction, not just the first - found
    against real hardware, not anticipated: once a board's dispatch loop is
    running (generate_bootstrap's `micropython.kbd_intr(-1)`, see
    connection.py) plus push_raw_repl's wait=False path deliberately never
    sending the raw-REPL exit sequence anymore (see its own docstring for
    why), there is no software-level way back to a clean raw-REPL prompt
    from a board that's already running a previous connect()'s dispatch
    loop. Without this, a second connect() attempt's Ctrl-C interrupt bytes
    land directly in the OLD dispatch loop's frame parser instead of
    interrupting it, corrupting its declared frame length and crashing it -
    which then leaves `_enter_raw_repl`'s banner wait reading that crash's
    traceback instead of the expected banner.

    Two pulses, not one - found empirically against real hardware, not
    assumed: an RTS-only pulse alone (esptool's `HardReset` strategy,
    normally used for the *final* "resume the running app" step after
    already being in the ROM bootloader) does NOT reliably interrupt a
    program that's actively running right now - repeated real-hardware
    testing showed it silently failing to recover a board stuck in a
    previous dispatch loop, most of the time. The fix: pulse into the ROM
    bootloader first (DTR held low/GPIO0 low during the reset pulse,
    matching esptool's `ClassicReset`) - the bootloader completely takes
    over the chip, so this unconditionally stops whatever was running,
    unlike a plain EN pulse. Then a second, RTS-only pulse boots normally
    out of the bootloader instead of leaving it stuck there waiting for a
    download protocol it'll never receive.

    The trailing sleep matters too, and isn't just a nicety - also found
    empirically: sending raw-REPL bytes too soon after releasing reset
    races the boot banner. Bytes that arrive while the board is still
    mid-boot get silently consumed instead of reaching the idle REPL
    that's actually ready for them, so `_enter_raw_repl` was found to
    still fail even when the reset itself had genuinely succeeded.

    Best-effort: some serial adapters/platforms don't expose RTS/DTR
    control at all (matches esptool's own graceful degrade for exactly
    this) - proceeds without resetting rather than hard-failing a connect()
    attempt that might not have needed it anyway; the raw-REPL work that
    follows still gets its own clear timeout error if the board genuinely
    wasn't in a usable state.
    """
    try:
        serial_obj.dtr = False
        serial_obj.rts = True
        time.sleep(0.1)
        serial_obj.dtr = True
        serial_obj.rts = False
        time.sleep(0.1)
        serial_obj.dtr = False

        serial_obj.rts = True
        time.sleep(0.1)
        serial_obj.rts = False
    except OSError:
        return
    time.sleep(1.0)  # let the boot sequence actually finish, see above
    serial_obj.reset_input_buffer()


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
    raw REPL protocol.

    `wait=True` (default) blocks until `code` finishes executing, raises
    RawReplError if it raised on-device (non-empty stderr), and returns to
    the friendly REPL afterward (ctrl-B) - appropriate for code that's
    expected to return, like a file-write helper.

    `wait=False` returns as soon as the device acks receiving the code (it
    has started running), without waiting for it to finish, and does NOT
    send the raw-REPL exit sequence. Required for code that runs forever -
    e.g. starting chunk 6's dispatch loop, which never returns. `wait=True`
    against such code would block until `timeout` and then raise a
    spurious RawReplError, even though the code is running successfully.

    Found against real ESP32 hardware (not reproducible against the
    scripted fakes chunk 9 was originally verified with, which can't model
    real UART timing): sending the ctrl-B exit sequence after wait=False
    races the just-started program's takeover of stdio. A forever-running
    program (like the dispatch loop) starts reading its own stdin directly
    once running, but there is a real timing window, right after the "OK"
    exec ack, where the interpreter is still transitioning from the raw-REPL
    protocol handler to the user script - ctrl-B landing in that window
    gets handled as a raw-REPL command instead of passing through, and the
    resulting REPL/prompt noise corrupts the very first bytes the program
    (here, the dispatch loop's frame decoder) reads. There is no raw-REPL
    session left to cleanly exit back to once a forever-running program has
    taken over anyway, so for wait=False this is skipped entirely rather
    than raced. Chunk 10 is expected to use wait=False for that call and is
    responsible for confirming the dispatch loop actually came up (e.g. via
    the protocol-version handshake), not this function.
    """
    _enter_raw_repl(serial_obj, timeout=timeout)
    if not wait:
        _exec_raw_start(serial_obj, code, timeout=timeout)
        return
    try:
        _exec_raw_start(serial_obj, code, timeout=timeout)
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


def _file_write_lines(path: str, content: bytes, chunk_size: int) -> list[str]:
    # Base64 encodes in 4-character groups. Slicing the encoded string on a
    # boundary that isn't a multiple of 4 splits a group across two chunks;
    # a2b_base64() on the resulting misaligned chunk raises ValueError
    # on-device (or worse, silently misdecodes). chunk_size=512 (both public
    # callers' default) is already a multiple of 4, so this is currently
    # latent - but chunk_size is a public kwarg on write_file/write_files,
    # so guard it here, the single choke point both funnel through, rather
    # than trust every future caller to remember the constraint. Raising
    # (matching this codebase's existing style for invalid-input-shaped
    # errors - see slicer's unsupported-parameter-kind check and
    # marshalling's oversized-frame check) rather than silently clamping:
    # this is a programmer error in a value the caller explicitly chose,
    # not an environmental condition to degrade gracefully around, so it
    # should fail loudly and immediately instead of quietly using a
    # different chunk size than requested.
    if chunk_size < 4 or chunk_size % 4 != 0:
        raise ValueError(
            f"chunk_size must be a positive multiple of 4 (base64 encodes in "
            f"4-character groups; a misaligned chunk_size corrupts "
            f"a2b_base64 decoding on-device), got {chunk_size}"
        )
    b64 = base64.b64encode(content).decode("ascii")
    lines = [f"_f = open({path!r}, 'wb')"]
    lines.extend(
        f"_f.write(_b64.a2b_base64({b64[i : i + chunk_size]!r}))"
        for i in range(0, len(b64), chunk_size)
    )
    lines.append("_f.close()")
    return lines


def write_file(
    serial_obj: Any, path: str, content: bytes, *, chunk_size: int = 512, timeout: float = 30.0
) -> None:
    """Write `content` to `path` on the device's persistent filesystem via
    raw REPL — needed because `push_raw_repl` alone only executes code
    transiently; `tether_runtime/dispatch.py` (and umsgpack) need to exist
    as real, importable files on-device.

    Content is base64-encoded and embedded as Python source text (binary-
    safe, avoids escaping issues with raw bytes in a source literal), sent
    as ONE combined open/write/close script via a single `push_raw_repl`
    call — not one call per chunk. Raw-REPL state (the open file handle)
    isn't assumed to persist across separate enter/exit cycles without
    real hardware to verify that assumption. For uploading several files
    at once, use `write_files` instead — one raw-REPL round trip for
    everything rather than one per file, since the physical serial upload
    is already the slowest part of the whole system.
    """
    lines = [
        "import ubinascii as _b64",
        *_file_write_lines(path, content, chunk_size),
        "del _f, _b64",
    ]
    push_raw_repl(serial_obj, "\n".join(lines).encode(), timeout=timeout)


def write_files(
    serial_obj: Any,
    files: dict[str, bytes],
    *,
    dirs: tuple[str, ...] = (),
    chunk_size: int = 512,
    timeout: float = 60.0,
) -> None:
    """Write multiple files (and create any needed directories first) in
    ONE combined raw-REPL round trip, rather than one enter/exit cycle per
    file. Order of `files` is preserved (dict iteration order) in case any
    later file depends on an earlier one existing (not currently the case,
    but cheap to guarantee).
    """
    if not files and not dirs:
        return
    lines = ["import ubinascii as _b64", "import uos as _uos"]
    # uos.mkdir() is POSIX-style (not mkdir -p): the parent must already
    # exist, or it raises OSError - silently swallowed below,
    # indistinguishable from "already exists", so a child-before-parent
    # ordering would leave the child directory never actually created and
    # a later file write into it failing. Sort by path depth (number of
    # `/` separators) rather than relying on plain lexicographic sort:
    # sorting by depth guarantees parent-before-child regardless of naming,
    # and is more obviously correct to a future reader than the incidental
    # (if actually-correct, for any depth, given every intermediate
    # ancestor is itself in the set) correctness of a plain string sort.
    # Secondary key on the string itself only for deterministic output
    # among equal-depth entries (creation order among siblings doesn't
    # matter on-device) - makes generated scripts reproducible for tests.
    sorted_dirs = sorted(dirs, key=lambda d: (d.count("/"), d))
    lines.extend(f"try:\n    _uos.mkdir({d!r})\nexcept OSError:\n    pass" for d in sorted_dirs)
    for path, content in files.items():
        lines.extend(_file_write_lines(path, content, chunk_size))
    # `_f` is only ever bound if `files` was non-empty - referencing it in
    # `del` when nothing was written would raise NameError on-device.
    names_to_delete = ["_b64", "_uos"] if not files else ["_f", "_b64", "_uos"]
    lines.append(f"del {', '.join(names_to_delete)}")
    push_raw_repl(serial_obj, "\n".join(lines).encode(), timeout=timeout)


def ensure_dir(serial_obj: Any, path: str, *, timeout: float = 10.0) -> None:
    """Create `path` as a directory on-device if it doesn't already exist."""
    script = f"import uos\ntry:\n    uos.mkdir({path!r})\nexcept OSError:\n    pass\n"
    push_raw_repl(serial_obj, script.encode(), timeout=timeout)


_MISSING_FILE_MARKER = "__TETHER_MISSING__"


def read_file(serial_obj: Any, path: str, *, timeout: float = 10.0) -> bytes | None:
    """Read a file's content from the device's filesystem via raw REPL, or
    `None` if it doesn't exist. Used for chunk 10's hash-check sentinel.
    """
    script = (
        "try:\n"
        "    import ubinascii as _b64\n"
        f"    print(_b64.b2a_base64(open({path!r}, 'rb').read()).decode().strip())\n"
        "except OSError:\n"
        f"    print({_MISSING_FILE_MARKER!r})\n"
    ).encode()

    _enter_raw_repl(serial_obj, timeout=timeout)
    try:
        _exec_raw_start(serial_obj, script, timeout=timeout)
        stdout, stderr = _follow_exec(serial_obj, timeout=timeout)
        if stderr:
            raise RawReplError(f"code raised on device: {stderr.decode(errors='replace')}")
    finally:
        _exit_raw_repl(serial_obj)

    text = stdout.decode().strip()
    if text == _MISSING_FILE_MARKER:
        return None
    return base64.b64decode(text)


def run_python(serial_obj: Any, code: bytes, *, timeout: float = 10.0) -> tuple[bytes, bytes]:
    """Execute arbitrary Python source on-device via raw REPL, returning
    (stdout, stderr) - the same enter/exec/follow/exit sequence read_file
    uses internally, exposed as a public, reusable primitive instead of
    read_file staying the only thing that can run code and get output
    back. Does not raise on non-empty stderr (unlike read_file/write_file)
    - callers decide what a given stderr means for their own use case
    (e.g. the CLI's status check).
    """
    _enter_raw_repl(serial_obj, timeout=timeout)
    try:
        _exec_raw_start(serial_obj, code, timeout=timeout)
        return _follow_exec(serial_obj, timeout=timeout)
    finally:
        _exit_raw_repl(serial_obj)


def remove_file(serial_obj: Any, path: str, *, timeout: float = 10.0) -> None:
    """Delete `path` from the device's filesystem via raw REPL. Silently
    succeeds if the file doesn't exist (matches ensure_dir's existing
    try/except OSError pattern) - used by the CLI's unprovision wifi to
    remove /tether_wifi.json.
    """
    script = f"import uos\ntry:\n    uos.remove({path!r})\nexcept OSError:\n    pass\n"
    push_raw_repl(serial_obj, script.encode(), timeout=timeout)
