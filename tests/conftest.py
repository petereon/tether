import pytest

from tether._context import current_board


@pytest.fixture(autouse=True)
def _reset_ambient_board():
    """Every test starts and ends with no ambient "current board" set.

    Without this, `connect()` calls in one test would leak their board as
    the ambient default into whichever test happens to run next (pytest
    runs a module's tests in one thread/process by default, and
    contextvars persist across function calls on the same thread unless
    explicitly reset) - a real cross-test pollution hazard now that
    `connect()` sets this.
    """
    token = current_board.set(None)
    yield
    current_board.reset(token)
