"""tether_runtime — the MicroPython-side package.

Not imported by user PC code. Its contents (dispatch loop, umsgpack.py,
generated proxy stubs) are what the slicer bundles and uploads to the board.
Written to run under MicroPython, not CPython — avoid PC-only stdlib.
"""
