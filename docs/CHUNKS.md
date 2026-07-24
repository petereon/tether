# tether — Implementation Chunks

Tracks build progress against `docs/DESIGN.md`. Chunks are ordered by
dependency (later chunks build on earlier ones) — work top to bottom unless
a chunk is explicitly marked parallelizable.

**Protocol:** when a chunk is finished (code written, tests passing for that
chunk's scope), check its box and append `— done YYYY-MM-DD`. Don't mark a
chunk done based on scaffolding/stubs alone — it means the behavior actually
works.

---

- [x] **1. Type contract & decorator validation** — done 2026-07-24
  `decorators.py::_validate_signature` — inspect type hints against the v1
  set (`int/float/bool/str/bytes/list/dict`, recursive), raise `TypeError`
  naming the offending param/return at decoration time. Unit tests in
  `tests/test_decorators.py`. Reviewed (simplify + security passes): no
  actionable findings — one simplify-agent claim (dead `annotation is None`
  branch) was checked empirically and found to be a false positive (nested
  generic args, e.g. `dict[str, None]`, are not normalized to `type(None)`
  the way top-level `get_type_hints()` return values are); kept the branch
  and added a regression test for it instead.

- [x] **2. Marshalling (PC side)** — done 2026-07-24
  `marshalling/` — length-prefixed msgpack framing (`[4-byte length][msg-type][msgpack body]`)
  using the `msgpack` package. Encode/decode for the v1 type set. No
  transport dependency — pure bytes-in, bytes-out. Also: incremental
  `FrameDecoder` (feed/pull_frames) for parsing off a live byte stream,
  needed by chunk 7's dispatch loop. Reviewed (4-angle simplify + manual
  security pass): fixed an O(n·frames) buffer-trim efficiency issue via
  zero-copy memoryview parsing, fixed an exception-safety gap where a
  caught-and-held decode error left the buffer permanently unresizable
  (memoryview export outliving the frame via the traceback), and added a
  `MAX_FRAME_SIZE` (1 MiB) bound on the wire length prefix to close an
  unbounded-memory DoS vector on corrupted/hostile input. 22 tests.

- [x] **3. AST slicer — MCU-bound extraction** — done 2026-07-24
  `slicer/` — walk `@mcu.export`/`@mcu.loop` functions, transitively pull in
  referenced module-level assignments, class defs, helper functions, and
  local imports (DESIGN.md § Architecture overview step 1). Depends on: 1.
  Scoped deliberately: extracts source subset + preserves decorator syntax
  as-is; making decorators resolve on-device and `@pc.export` stub
  generation are chunk 4/6/10's job, not this one's. 13 tests, TDD'd.
  Reviewed (4-angle simplify + manual security pass): fixed a real bug
  where the PC-only `from tether import mcu` import leaked into MCU-bound
  output (would fail on-device — `tether` isn't installed there), fixed a
  cross-file ordering bug where dependency-of-a-dependency could render
  after its dependent (topological sort, not discovery order, now used),
  and fixed decorator recognition to resolve through import aliasing
  (`from tether import mcu as m`) instead of a hardcoded literal name.
  Known, documented limitation carried forward (not fixed — judged
  disproportionate for now): tuple/list-unpacking assignment targets
  (`x, y = ...`) at module level aren't tracked as dependencies.

- [x] **4. AST slicer — reverse stub generation** — done 2026-07-24
  `slicer/` — for every `@pc.export` function, generate a MicroPython proxy
  stub (same name/signature) whose body sends an RPC frame and awaits the
  reply (DESIGN.md step 2). Depends on: 1, 3. `generate_pc_stubs()`, stubs
  are `async def` forwarding args positionally to a forward-declared
  `call_pc(name, *args)` runtime hook — see chunk 6's contract note above.
  7 tests, TDD'd. Reviewed (4-angle simplify + manual security pass): fixed
  a real bug where keyword-only/positional-only params were silently
  dropped from the forwarded call (never sent over RPC, no error) — closed
  it in two places: rejected at decoration time (chunk 1's
  `_validate_signature`, matching the existing `*args`/`**kwargs`
  rejection) AND defensively in the slicer itself, since `generate_pc_stubs`
  parses raw source text with no guarantee decoration ever ran first.
  Extracted a shared `_top_level_decorated_functions` helper (was
  duplicated between `slice_mcu_bound` and `generate_pc_stubs`).

- [x] **5. MCU runtime — umsgpack** — done 2026-07-24
  `tether_runtime/umsgpack/` — vendored `peterhinch/micropython-msgpack`
  (MIT, commit `31d512d`), core `__init__.py`/`mp_dump.py`/`mp_load.py`
  only (extension-type modules and the async stream loader weren't needed
  for the v1 type set). Provenance + update instructions in
  `umsgpack/VENDORED.md`; excluded from `ruff` formatting so it stays
  byte-diffable against upstream. Depends on: 2.
  Verification (not TDD in the usual sense — integration-testing a vendored
  component, not designing new behavior): 39 round-trip tests in
  `tests/test_umsgpack_compat.py` confirming wire compatibility both
  directions with the PC-side `msgpack` package from chunk 2, including the
  longer-form (str8/16, bin16, array16, map16) wire encodings a
  small-values-only test would have missed — caught by review, fixed by
  expanding the test data. Retroactively also confirmed against a real
  MicroPython interpreter (`brew install micropython`, unix port, installed
  while starting chunk 6) — round-trips correctly under the actual target
  runtime, not just under CPython.
  Reviewed (combined quality pass + manual security pass): no reuse/
  simplification/altitude findings on our own code (vendored library
  itself deliberately out of scope for critique — see VENDORED.md).
  Security pass found a real forward-looking hazard: the vendored
  `umsgpack.load(fp)` trusts a wire length field with no upper bound —
  safe via `loads()` on an already-bounded `bytes` object, unsafe if ever
  called directly on a live stream (would reintroduce, on far more
  RAM-constrained hardware, the exact unbounded-memory DoS chunk 2's
  `MAX_FRAME_SIZE` closed on the PC side). Not fixable in the vendored file
  without breaking upstream-diffability, and the actual call site doesn't
  exist yet — documented as a hard, explicit constraint on chunk 6 (see
  its entry below) and in `VENDORED.md`, rather than left as scattered
  awareness.

- [x] **6. MCU runtime — dispatch loop** — done 2026-07-24
  `tether_runtime/dispatch.py` — `uasyncio`-based reentrant dispatch loop:
  request-ID tagged frames, `@mcu.loop` periodic task scheduling, heartbeat
  emission on natural `await` yield points. Depends on: 5.
  `Dispatcher(reader, writer)` with `register(name, handler,
  heartbeat_interval_ms=1000)`, `register_loop(fn, interval_ms)`, `call_pc
  (name, *args)`, `run()`. Satisfies both contracts pinned above: `call_pc`
  matches chunk 4's generated stubs exactly, and frame reading always
  bounds `length` (mirrors chunk 2's `MAX_FRAME_SIZE`) before ever calling
  `umsgpack.loads()` on the resulting bytes.
  Installed a real MicroPython interpreter (`brew install micropython`,
  unix port) since this module is inherently `uasyncio`-specific and can't
  be meaningfully exercised under CPython — 13 tests in
  `tests/test_dispatch_mcu.py` run against the actual interpreter via a
  subprocess harness (`tests/mpy_runner.py`, skips gracefully if
  micropython isn't installed), covering frame roundtrip, oversized-length
  rejection, call/response, error propagation (incl. traceback), async
  handlers, bidirectional reentrancy (a handler calling back into its own
  caller while that caller is still waiting — the core DESIGN.md guarantee),
  periodic loops, and heartbeat emission/non-interference with the final
  response.
  Two real bugs caught mid-build by the harness itself (not by review):
  `from tether_runtime import umsgpack` failed under real MicroPython
  because the on-device bundle deploys flat (no package wrapper) — fixed to
  `import umsgpack`; and `MICROPYPATH` replaces `sys.path` entirely rather
  than extending it, dropping the frozen stdlib — fixed the test harness to
  include the default search paths alongside the project path.
  Reviewed (4-angle + manual security pass): fixed a real bug (altitude
  finding) where a CALL frame for an unregistered handler name raised an
  uncaught `KeyError` inside a fire-and-forget task, permanently hanging
  the caller with no response ever sent — now returns a proper `MSG_ERROR`
  (`LookupError`). Applied a `_Pending` value class (was an ad-hoc dict with
  implicit mutual-exclusion rules — simplification + efficiency agents both
  flagged this independently), a single-allocation `_encode_frame`
  (efficiency), `try/finally` heartbeat-task cleanup (simplification), and
  a shared `_maybe_await` helper (reuse — was duplicated between
  `_handle_call` and `_run_loop`). Security pass found a real gap against
  DESIGN.md's own locked contract: `RemoteError` was only carrying
  type+message, not the traceback DESIGN.md § Wire protocol specifies —
  fixed using `sys.print_exception`/`io.StringIO` (both available on
  MicroPython). Also found and documented (not fixed — needs a symmetric
  decision with chunk 7, not yet made) that `run()` spawns an unbounded
  task per incoming CALL frame with no concurrency cap or backpressure;
  noted as a constraint on chunk 7 below rather than left as scattered
  awareness.
  (Both contracts previously pinned here — the `call_pc` shape owed to
  chunk 4, and the length-bound-before-`loads()` security constraint owed
  by chunk 5's review — are now satisfied; see the summary above.)

- [x] **7. PC-side dispatch** — done 2026-07-24
  `dispatch/` — background reader thread + queue; blocking calls filter by
  request-ID while pumping other in-flight requests to a thread pool
  (reentrant/reverse-call support); per-call timeout + heartbeat-driven
  idle-reset; wraps remote exceptions as `RemoteError`. Depends on: 2.
  `Dispatcher(reader, writer, max_workers=4)` — threading mirror of chunk
  6's asyncio `Dispatcher`: `register(name, handler, heartbeat_interval=1.0)`,
  `call_mcu(name, *args, timeout=10.0)`, `start()`. Reuses chunk 2's
  `FrameDecoder`/`encode_frame` directly (no PC-side reimplementation
  needed) and the existing `tether.errors.RemoteError`/`MCUTimeoutError`/
  `MCUDisconnectedError` from the original scaffold — their field shapes
  already matched what chunk 6 sends over the wire without any changes.
  Heartbeat emission for handled calls uses a cancellable ticker thread
  (`Event.wait(timeout=...)` loop) rather than chunk 6's parallel-task
  approach — ticks on a fixed timer regardless of what the handler thread
  is doing (CPU-bound or not), a deliberate difference from chunk 6's
  yield-point-based ticker, not an oversight (there's no equivalent of
  asyncio yield points for a plain OS thread). 7 tests, TDD'd, covering
  call/response, remote exceptions (with traceback), unregistered-handler
  error, bidirectional reentrancy, timeout enforcement, heartbeat-driven
  idle-reset, and reader-thread-death handling.
  Reviewed (4-angle + manual security pass): no reuse findings (PC/MCU
  structural similarity is intentional and can't be shared — confirmed no
  accidental `tether_runtime` import). Simplification: collapsed `_Pending`'s
  four parallel error fields into one `outcome` field holding either the
  result or an `Exception` instance; extracted a `_send_error` helper and a
  `_heartbeat_ticking` context manager for the paired start/stop/join
  lifecycle. Efficiency: documented (not fixed here — belongs to the
  transport adapter) that `reader.read()` must be chunked reasonably by
  whichever adapter implements it, since a naive `pyserial` wrapper
  defaults `.read()` to 1 byte — noted as a constraint on chunk 9 below.
  Altitude/security (real bug, not just a finding): the reader thread
  dying (transport error or clean EOF) left every in-flight `call_mcu()`
  call hanging forever for `timeout=None` with zero signal — fixed to fail
  all pending calls with `MCUDisconnectedError` immediately. This is a
  chunk-7-layer fix (in-flight call bookkeeping), not chunk 11's full
  reconnect/board-lifecycle scope. Fixing this surfaced a second latent
  race: a call that resolved normally right before the transport died could
  have its correct result clobbered by the disconnect sweep before the
  caller thread woke up to clean up — fixed by making `_Pending.resolve()`
  idempotent (first resolution wins). The unbounded-concurrent-calls
  concern chunk 6's review flagged is confirmed present here too (partially
  mitigated by the bounded 4-worker pool vs. chunk 6's fully unbounded task
  spawn, but the executor's internal queue is still unbounded) — still an
  open, deliberately deferred decision: whichever future chunk designs real
  backpressure must do it symmetrically for both sides, not independently
  invent different schemes per side.

- [ ] **8. Mock transport**
  `transports/mock.py` — in-process fake MCU: runs the real sliced code path
  (chunks 3, 4, 6) against a second thread, no hardware required. This
  unblocks hardware-free testing for everything downstream. Depends on:
  3, 4, 6, 7.

- [ ] **9. Serial transport**
  `transports/serial.py` — USB VID/PID auto-discovery (`"serial:auto"`),
  raw-REPL code push, `pyserial`-backed byte stream. Depends on: 7.
  **Contract owed to chunk 7 (from chunk 7's review):** `Dispatcher._run_reader`
  calls `reader.read()` with no size argument and expects a reasonably-sized
  chunk back, blocking until at least one byte arrives. `pyserial`'s
  `Serial.read()` defaults to `size=1` — this adapter's `read()` must
  explicitly request a real chunk size (e.g. `self._serial.read(4096)`
  or similar), not pass through the naive default, or the reader loop
  degrades to processing one byte at a time.

- [ ] **10. Connection orchestration**
  `connection.py::connect()` — full wire→probe→use flow: slice, bundle,
  hash-check against on-device sentinel (skip upload if unchanged),
  upload-if-needed, protocol-version handshake (`ProtocolVersionError` on
  mismatch), return ready `BoardHandle`. Depends on: 3, 4, 6, 9.

- [ ] **11. Multi-board + disconnect handling**
  `connection.py` / `dispatch/` — concurrent `BoardHandle`s, per-board method
  routing (`board.read_temp()`), `MCUDisconnectedError` on transport loss
  (fail loud, no silent retry), explicit `.reconnect()`. Depends on: 10.

- [ ] **12. Wifi transport**
  `transports/wifi.py` — pure stdlib `socket`. Requires target board already
  running a bootstrapped runtime (no code push over wifi). Depends on: 10.

- [ ] **13. BLE transport**
  `transports/ble.py` — `bleak`-backed, custom GATT service (write +
  notify characteristic), frame chunking/reassembly across BLE MTU hidden
  in this module. Depends on: 10.

- [ ] **14. ruff cleanup pass**
  `slicer/` — post-slice unused-import stripping via `ruff`, applied to the
  bundle before upload. Depends on: 3.

- [ ] **15. End-to-end example + docs**
  A real single-file example (e.g. blink/read-sensor over serial) in
  `examples/`, README walkthrough verified against actual hardware. Depends
  on: 9, 10.

- [ ] **16. CI: lint workflow**
  GitHub Actions workflow running `ruff check` + `ruff format --check` (via
  `uv`) on push/PR. No hardware/token dependency — safe to build anytime.
  Not started; queued per explicit user request, do not start until told.

- [ ] **17. CI: PyPI release workflow**
  GitHub Actions workflow that builds and publishes to PyPI on release (via
  `uv build` / `uv publish`). Needs a PyPI token supplied by the user as a
  repo secret before this can run for real. **Do not start this chunk until
  the user explicitly says to** — flagged that they'll provide the token
  when ready.

---

## Explicitly out of scope for these chunks (see DESIGN.md § Non-goals)

Classic BT, firmware auto-flashing, ISR-driven heartbeats, cross-version
protocol negotiation, pluggable custom-type serializers, VS Code language
server (future project, not part of `tether` core).
