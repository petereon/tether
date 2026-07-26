"""Run a snippet of MCU-side code under a real MicroPython interpreter.

`tether_runtime/` code (uasyncio, umsgpack) can't be exercised under CPython
- `uasyncio` doesn't exist there, and even where APIs look similar, behavior
can differ. Tests that need real MicroPython semantics use `run_micropython`
below rather than importing tether_runtime modules directly into the
CPython-based pytest process.

Requires the `micropython` unix-port binary on PATH (`brew install
micropython` on macOS). Tests using this are skipped, not failed, if it's
not installed - see `requires_micropython`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_TETHER_RUNTIME_SRC = Path(__file__).parent.parent / "src" / "tether_runtime"

# MICROPYPATH replaces sys.path entirely rather than extending it, dropping
# the frozen stdlib (uasyncio et al) unless we list those default locations
# ourselves alongside our own path.
_DEFAULT_MICROPY_PATHS = [
    ".frozen",
    str(Path.home() / ".micropython" / "lib"),
    "/usr/lib/micropython",
]

requires_micropython = pytest.mark.skipif(
    shutil.which("micropython") is None,
    reason="micropython interpreter not installed (brew install micropython)",
)

# In-memory duplex Pipe pair for driving a generated bootstrap's
# _tether_main() as a task inside a single micropython process, standing in
# for real stdin/stdout wiring (already verified separately that
# uasyncio.StreamReader/Writer over real piped stdio works - these tests are
# about the registration/dispatch wiring, not the stream plumbing itself).
# Defines module-level `reader_a`/`writer_a` (driver-side) and `_reader`/
# `_writer` (the names a bootstrap's stdio-wiring lines get patched to
# reference instead). Shared across every "drive a real generated bootstrap
# end-to-end" test (test_connection.py, test_examples.py) rather than
# duplicated per test file - a bug fix here would otherwise need applying
# in each copy independently.
PIPE_HARNESS = """
import uasyncio as asyncio

class Pipe:
    def __init__(self):
        self.buf = b""
        self.event = asyncio.Event()

    def write(self, data):
        self.buf += data
        if not self.event.is_set():
            self.event.set()

    async def drain(self):
        pass

    async def readexactly(self, n):
        while len(self.buf) < n:
            self.event.clear()
            await self.event.wait()
        chunk = self.buf[:n]
        self.buf = self.buf[n:]
        return chunk


def make_pair():
    a_to_b = Pipe()
    b_to_a = Pipe()
    return (b_to_a, a_to_b), (a_to_b, b_to_a)

(reader_a, writer_a), (_reader, _writer) = make_pair()
"""


def run_micropython(script: str, timeout: float = 5.0) -> str:
    """Run `script` under `micropython`, with tether_runtime/ importable.
    Returns stdout. Raises AssertionError (with stderr attached) on a
    non-zero exit or timeout, so failures show up clearly in test output.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        result = subprocess.run(
            ["micropython", script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={
                **os.environ,
                "MICROPYPATH": ":".join([*_DEFAULT_MICROPY_PATHS, str(_TETHER_RUNTIME_SRC)]),
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"micropython script timed out after {timeout}s.\nscript:\n{script}"
        ) from exc
    finally:
        Path(script_path).unlink(missing_ok=True)

    if result.returncode != 0:
        raise AssertionError(
            f"micropython script exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\nscript:\n{script}"
        )
    return result.stdout


def run_micropython_background(script: str, *, run_for: float = 3.0) -> None:
    """Start `script` under micropython as a background process, let it
    run for `run_for` seconds, then terminate it - for scripts that loop
    forever by design (e.g. boot.py's accept loop), where a timeout is the
    expected way the test ends, not a failure. Does not return stdout: a
    forcibly-terminated process's buffered output isn't reliably
    available, so tests using this observe behavior through a real side
    channel (e.g. a socket a client thread connects to during `run_for`),
    not through captured process output.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        proc = subprocess.Popen(
            ["micropython", script_path],
            env={
                **os.environ,
                "MICROPYPATH": ":".join([*_DEFAULT_MICROPY_PATHS, str(_TETHER_RUNTIME_SRC)]),
            },
        )
        try:
            proc.wait(timeout=run_for)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
    finally:
        Path(script_path).unlink(missing_ok=True)
