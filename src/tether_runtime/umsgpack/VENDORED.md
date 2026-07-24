# Vendored: micropython-msgpack

Source: https://github.com/peterhinch/micropython-msgpack
Commit: `31d512dfc0e84af280b94c60ae0ebbe8e0853914` (2026-05-25)
License: MIT (see `LICENSE` in this directory)

Files vendored: `__init__.py`, `mp_dump.py`, `mp_load.py` — the core
encode/decode. Deliberately **not** vendored: `as_loader.py` (async
StreamReader loader — chunk 6's dispatch loop can adopt it later if useful,
not needed just to vendor the codec) and the `mpk_*.py` extension-type
modules (`bytearray`, `complex`, `odict`, `set`, `tuple`) — none are needed
for tether's v1 wire type set (`int, float, bool, str, bytes, list, dict,
None` — see DESIGN.md § Wire protocol).

These files are copied verbatim from upstream (not reformatted by `ruff` —
see the `[tool.ruff] extend-exclude` entry in `pyproject.toml`) so future
updates can be diffed cleanly against the source. To update: re-fetch the
three files above from a newer commit, update the commit hash here, and
rerun `tests/test_umsgpack_compat.py`.

## Security note: `load()` vs `loads()`

`load(fp)` reads directly from whatever file-like object `fp` is, and its
internal `_read_except` trusts a length field taken straight from the wire
with no upper bound. That's fine when `fp` is an `io.BytesIO` wrapping an
already-complete, already-bounded `bytes` object (which is what `loads(s)`
does) — reading past the end just runs out of buffer. It is **not** fine if
`load()` is ever called directly on a live stream (UART, socket): a
corrupted or hostile length field would make it block/buffer indefinitely
trying to satisfy an unbounded read, on a device with far less RAM than a
PC. Always bound the frame length yourself first (mirror chunk 2's
`MAX_FRAME_SIZE`) and call `loads()` on the resulting bytes — never
`load()` on a raw transport stream. See CHUNKS.md chunk 6's entry.
