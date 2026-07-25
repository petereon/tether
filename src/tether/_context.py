"""Ambient "current board" context for directly-callable `@mcu.export`
functions - lets PC code call an exported function like a normal function
(`read_temp()`) with no awareness of where it executes, instead of only
via `board.read_temp()`.

A `contextvars.ContextVar`, not a plain module-level global: it's
thread/async-task safe by construction (the same mechanism `asyncio` and
`decimal` use for exactly this kind of ambient state), which matters here
since `tether.dispatch.Dispatcher` handles incoming calls on a worker
thread pool.

Split into its own tiny module so `decorators.py` (reads this when a
decorated function is called directly) and `connection.py` (sets this on
`connect()`, and via `BoardHandle`'s `with board:` support) don't need to
import each other.
"""

from __future__ import annotations

import contextvars
from typing import Any

# Typed `Any` rather than importing `connection.BoardHandle` for real: that
# would create decorators.py -> connection.py -> (nothing back), which is
# fine on its own, but this module is also imported *by* connection.py, so
# importing BoardHandle here for typing only would need a TYPE_CHECKING
# guard regardless - not worth it for one attribute's type annotation.
current_board: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "tether_current_board", default=None
)
