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
