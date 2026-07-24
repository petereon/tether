"""AST-based dependency slicer. See DESIGN.md § Architecture overview, step 1.

Given a set of `@mcu.export`/`@mcu.loop` functions, walks referenced names
transitively (module-level assignments, class defs, helper functions, local
imports) to produce the minimal set of source needed on the MCU. Also
generates MCU-side proxy stubs for every `@pc.export` function (step 2).

`ruff` is invoked as a post-slice cleanup pass (unused-import stripping)
after this module produces the bundle — not used for the slicing itself.
"""
