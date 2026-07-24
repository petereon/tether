"""Placeholder — decoration-time type validation (DESIGN.md § Wire protocol).

Once _validate_signature is implemented, this should assert:
  - supported types (int/float/bool/str/bytes/list/dict) decorate cleanly
  - unsupported types raise at decoration time, naming the offending param
"""

import pytest


def test_placeholder() -> None:
    pytest.skip("decorators.py: _validate_signature not yet implemented")
