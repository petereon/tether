"""In-process fake MCU transport for hardware-free testing.

`mcu.connect("mock://")` runs the actual sliced code path (stubs, dispatch
loop, msgpack encode/decode) against a second thread, exercising the real
wire protocol with no board attached. See DESIGN.md § Testing.
"""

from __future__ import annotations
