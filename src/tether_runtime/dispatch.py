"""MicroPython-side uasyncio dispatch loop.

Runs the reentrant request/response loop (request-ID tagged frames), the
periodic @mcu.loop tasks, and the heartbeat-on-yield behavior described in
DESIGN.md § Call semantics and § Wire protocol.

NOTE: this file must run under real MicroPython (uasyncio, not asyncio).
Do not import PC-only stdlib here.
"""
