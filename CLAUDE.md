# CLAUDE.md — tether

Read `docs/DESIGN.md` before writing any code in this project. It's the
locked architecture (wire protocol, call semantics, transports, testing
strategy) reached via a full design-review interview — treat its decisions
as settled, not open for casual re-litigation. If a decision there turns out
to be wrong once real implementation surfaces a problem, fix DESIGN.md
explicitly (note what changed and why) rather than silently drifting from it.

## Chunk tracking — read this before starting or resuming work

`docs/CHUNKS.md` is the implementation roadmap, ordered by dependency.

- Before starting work, check `docs/CHUNKS.md` to see what's done and what's
  next. Work top-to-bottom unless a chunk is explicitly unblocked out of
  order.
- When a chunk is genuinely finished — code written AND tests passing for
  that chunk's scope, not just scaffolded — check its box and append
  `— done YYYY-MM-DD` using the actual current date.
- Don't mark a chunk done on the basis of stub/placeholder code (the kind
  that raises `NotImplementedError`). Scaffolding already exists for every
  module; a chunk is done when the scaffold's `NotImplementedError`s in its
  scope are replaced with real, tested behavior.
- If a chunk turns out to need splitting or reordering once you're in it,
  update `docs/CHUNKS.md` to reflect that rather than leaving it stale.

## Project-specific constraints (also in DESIGN.md, repeated because they're easy to violate by accident)

- **`src/tether/` is PC-side (CPython).** `src/tether_runtime/` is MCU-side
  (MicroPython) — it gets sliced and uploaded, never imported directly by PC
  code. Do not import PC-only stdlib (`threading`, `socket` in its full
  form, etc.) into anything under `tether_runtime/`; MicroPython's stdlib is
  a subset and silently-incompatible imports are a common failure mode here.
- **Decorator API must stay statically analyzable.** No `exec`-based or
  metaclass-driven decorator magic. A future VS Code language server needs
  to infer signatures and cross-boundary call targets by walking plain
  decorator calls — see DESIGN.md § Standing design constraint.
- **v1 type set is fixed:** `int, float, bool, str, bytes, list, dict`
  (recursive, msgpack-safe). Don't quietly widen this while implementing
  chunk 1 — if a real use case needs more, that's a DESIGN.md amendment, not
  a silent scope creep in the validator.
- **BLE only for Bluetooth, no classic BT.** Wifi/BLE transports never push
  code — only serial does (see DESIGN.md § Transports for why: chicken-and-egg
  on an unbootstrapped board).
- **Disconnection fails loud, never silently auto-reconnects** — see
  DESIGN.md § Disconnection for the reasoning (a reconnected board may be in
  an unknown state).

## Testing

Use the mock transport (chunk 8) for all cross-boundary behavior tests —
don't gate PR-worthy test coverage on physical hardware being attached.
