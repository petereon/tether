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

- [x] **8. Mock transport** — done 2026-07-24
  `transports/mock.py` — in-process fake MCU: runs the real sliced code path
  (chunks 3, 4, 6) against a second thread, no hardware required. This
  unblocks hardware-free testing for everything downstream. Depends on:
  3, 4, 6, 7.
  `MockTransport(source, base_dir=None).start()` imports chunk 6's actual
  `tether_runtime/dispatch.py` unmodified and runs it under CPython in a
  background thread, via a `uasyncio` -> `asyncio` compatibility shim +
  a `sys.print_exception` shim (MicroPython-only, used by chunk 6's
  traceback capture) — not a reimplementation of dispatch logic, matching
  DESIGN.md § Testing's explicit intent. Bridges to a real chunk 7
  `Dispatcher` over `asyncio.Queue` (fed via `call_soon_threadsafe` from
  the foreign PC thread) one direction and plain `queue.Queue` the other,
  since the two dispatchers have different reader/writer contracts
  (async `readexactly`/`drain` vs. sync `read`/`write`). 4 tests: basic
  call/response, reverse-call through a generated `@pc.export` stub,
  remote exception propagation, `@mcu.loop` periodic execution.
  Two real bugs surfaced while building this (not by review — by actually
  running the full path end-to-end for the first time):
  - Chunks 3 and 4's AST filtering (`slice_mcu_bound`'s exported-function
    detection, `_top_level_decorated_functions`, and `_bound_names`) only
    recognized `ast.FunctionDef`, never `ast.AsyncFunctionDef` — so `@mcu.export
    async def` was silently invisible to the slicer. This is a real,
    required case (chunk 4's own docstring requires MCU code calling a
    `@pc.export` stub to be async), not an edge case — fixed in
    `src/tether/slicer/__init__.py` with regression tests added to
    `tests/test_slicer.py`.
  - `sys.print_exception` (used by chunk 6's `_format_exception`) is a
    MicroPython-only extension with no CPython equivalent — fixed via a
    shim in this chunk, not a chunk 6 bug (chunk 6 is correct for its real
    target).
  `MICROPYPATH` note from chunk 6 doesn't apply here (that's the real
  MicroPython interpreter's env var); this chunk instead learned that
  `asyncio.to_thread()` wrapping an unbounded blocking `queue.Queue.get()`
  prevents the Python process from exiting (the default executor's
  `atexit` handler waits for it) — discovered when an early prototype hung
  the test process indefinitely. Fixed by using `asyncio.Queue` +
  `call_soon_threadsafe` instead of bridging through a blocking thread pool
  call.
  Reviewed (4-angle + manual security pass): no reuse findings (the
  cross-runtime bridge classes are a genuinely different problem from
  chunk 6/7's own same-runtime test pipes, confirmed not duplicative).
  Simplification: replaced a stringly-keyed `_shared` dict (two fixed
  fields, no static typing) with typed instance attributes; narrowed a
  function param from an entire module reference down to the one constant
  it actually used. Efficiency: no findings (correctly treated as
  non-hot-path test infrastructure, matching chunk 3's precedent).
  Altitude/security: documented (not fixed — reasonable scope boundary for
  a walking-skeleton testing utility) that the shim installs are one-way
  process-global mutations with no teardown, that `exec()` runs whatever
  `source` is passed (trust boundary: test-author-owned fixtures only,
  same as running any code directly), that the slicer's preserved external
  imports (e.g. `from machine import Pin`) will fail under CPython here
  since the mock can't emulate hardware-only modules, and that
  `MockTransport` has no `.stop()` (background thread runs forever, daemon
  so it won't block process exit, but accumulates across a very large test
  suite).

- [x] **9. Serial transport** — done 2026-07-24
  `transports/serial.py` — USB VID/PID auto-discovery (`"serial:auto"`),
  raw-REPL code push, `pyserial`-backed byte stream. Depends on: 7.
  (Chunk 7's read-chunking contract, quoted here previously, is satisfied —
  see `SerialStream` below, not the naive `.read(size)` it warned against.)

  **No physical hardware was available to build or validate this against.**
  Implemented faithfully against the protocol documented in MicroPython's
  own `tools/pyboard.py`, hand-rolled rather than depending on `mpremote`
  (its equivalent internals are explicitly marked unstable/mid-refactor,
  and importing the whole CLI package for one class was poor weight for
  what's needed). Tested against scripted fakes reproducing the exact
  documented byte sequences, plus one sanity check against `pyserial`'s
  real `loop://` virtual port for `SerialStream`. Real-device verification
  remains chunk 15's job (DESIGN.md § Testing) — passing here means
  "faithful to the documented protocol," not "confirmed against hardware."
  10 tests.

  `discover(list_ports_fn=None, extra_vid_pid=None)` — matches
  `serial.tools.list_ports.comports()` against a built-in (VID, PID) table
  covering common ESP32/RP2040 USB-bridge chips (CP210x, CH340, FTDI,
  Espressif native USB, RP2040) — not exhaustive by design (DESIGN.md's
  "supporting ESP32 and similar"), with an `extra_vid_pid` escape hatch and
  an error message pointing at it, so an unlisted board isn't silently
  locked out of ever using auto-discovery.

  `push_raw_repl(serial_obj, code, timeout=10.0, wait=True)` — drives the
  standard (non-raw-paste) raw REPL protocol: interrupt, enter raw REPL,
  send code in 256-byte/10ms-paced chunks (matches the reference pacing
  exactly — deliberately not tuned faster without hardware to validate
  buffer limits against), execute, optionally collect stdout/stderr and
  raise on non-empty stderr, exit raw REPL.

  `SerialStream` — wraps an open `pyserial.Serial` to satisfy chunk 7's
  reader/writer contract: `read(1)` (blocks for the first byte) then drains
  whatever else is immediately available via `in_waiting`, avoiding both
  `pyserial`'s naive 1-byte default *and* the trap of requesting a large
  fixed size directly (which under `timeout=None` blocks until that many
  bytes arrive rather than returning early with whatever's buffered).

  Real bug caught while building, not by review: the initial
  `push_raw_repl` design blocked waiting for the executed code to *return*
  before considering the push complete — correct for a short setup script,
  but the actual on-device bundle (chunk 6's dispatch loop) never returns.
  Every real `connect()` would have hung for the full default timeout and
  then raised a spurious error, even though the code had started running
  successfully. Fixed by splitting send+ack from collect-output, and adding
  `wait=False` for code that's expected to run forever — chunk 10 is
  responsible for using it for that call and confirming the dispatch loop
  actually came up some other way (e.g. the protocol-version handshake),
  not this function.

  Reviewed (4-angle + manual security pass): reuse review confirmed
  hand-rolling over depending on `mpremote` was the right call (documented
  the rationale in the module docstring, since it wasn't recorded
  anywhere). No simplification findings. Efficiency: added a comment
  anchoring `_read_until`'s byte-at-a-time pattern to raw-REPL negotiation
  only (hash-gated per upload) so it doesn't get copied into a hot path
  later, and documented why the chunk-pacing sleep isn't being tuned
  without hardware to validate against. Altitude (real gaps, both fixed):
  the `wait=False` fix above, and the VID/PID escape hatch + clearer error
  message. Security: no findings — this code talks to the user's own
  physically-connected board, same trust tier as their own code, not a
  remote/network peer.

- [x] **10. Connection orchestration** — done 2026-07-24
  `connection.py::connect()` — full wire→probe→use flow: slice, bundle,
  hash-check against on-device sentinel (skip upload if unchanged),
  upload-if-needed, protocol-version handshake (`ProtocolVersionError` on
  mismatch), return ready `BoardHandle`. Depends on: 3, 4, 6, 9.
  The largest chunk yet — the first time chunks 1-9 all actually run
  together. `connect("mock://")` delegates to chunk 8's `MockTransport`
  directly (no upload/hash/handshake — nothing to hash-check against).
  `connect("serial:...")` does the real thing: derives the runtime file
  set from `tether_runtime/` on disk (not hand-listed), batches the whole
  upload into one raw-REPL round trip via a new `write_files` (extends
  chunk 9's `write_file` single-file batching across multiple files - the
  serial upload is already the slowest part of the whole system, so 8
  separate enter/exit cycles avoided down to 1), always restarts the
  on-device app fresh (deliberately not trying to detect "already
  running", since that detection would itself need to read the same
  connection the reader thread is about to own), then hands off to a real
  `Dispatcher` and does the handshake. wifi/ble schemes correctly
  `NotImplementedError` until chunks 12/13 exist.
  New pieces this chunk had to invent, none pinned by any prior chunk:
  - `src/tether_runtime/mcu_decorators.py` — a lightweight on-device
    decorator shim, since sliced `@mcu.export`/`@mcu.loop` source needs
    *something* named `mcu`/`pc` in scope on a device with no `tether`
    package installed. Registers into a module-level list rather than
    tagging function objects — **verified against a real MicroPython
    interpreter that MicroPython function objects don't support arbitrary
    attribute assignment the way CPython's do**, so chunk 1's PC-side
    approach (`fn.__tether_export__ = ...`) doesn't port as-is; this was
    only caught by actually running the shim under `micropython`, not by
    reasoning about it.
  - The protocol-version handshake itself: reuses the existing
    `MSG_CALL`/`MSG_RESULT` machinery with a reserved function name
    (`__tether_handshake__`) rather than adding a new wire message type -
    zero changes needed to chunks 6/7's already-shipped, reviewed dispatch
    code.
  - Wiring `sys.stdin`/`sys.stdout` as the on-device wire transport, via
    `uasyncio.StreamReader`/`StreamWriter` — **empirically verified against
    a real MicroPython interpreter with piped I/O** that raw bytes
    (including `0x03`) reach a running script's own reads correctly. This
    mattered: an initial test showed `0x03` getting intercepted as a
    keyboard interrupt even mid-script, which would have silently corrupted
    the wire protocol every time a msgpack frame happened to contain that
    byte — investigation traced it to the *test harness's* PTY running in
    cooked terminal mode (converting `0x03` to `SIGINT` at the OS line-
    discipline level), not to MicroPython or raw-REPL at all; real UART
    hardware (and `pyserial`, which always opens ports in raw/non-canonical
    mode) has no such line discipline. Re-tested with the PTY in raw mode
    and confirmed clean pass-through — the architecture is sound.
  - The exact MicroPython raw-REPL file-write/read encoding (base64 +
    `ubinascii.a2b_base64`/`b2a_base64`, one combined script per operation)
    — every generated script (write_file, write_files, read_file) was
    extracted from its own test fake and **run for real under
    `micropython`**, not just checked for shape, including a full binary
    round-trip and directory creation.
  Real bugs caught while building (verification-driven, most before any
  review pass ran):
  - `SerialStream`/`Dispatcher` needs *blocking* reads (`timeout=None`) so
    an idle connection with no in-flight calls doesn't spuriously look
    disconnected, but raw-REPL's own `_read_until` needs a *finite* timeout
    or its deadline check never actually engages (a blocked `read(1)` never
    returns to let it check) — caught by the altitude review, fixed by
    opening the port at `timeout=1.0` for the raw-REPL phase and switching
    to `ser.timeout = None` right before handing off to the `Dispatcher`.
  - `_capture_caller()` must NOT re-execute the caller's source to recover
    `@mcu.export` metadata — the caller's module is already running
    (that's how `connect()` got invoked), so re-exec would double any of
    its side effects (prints, connections opened at import time, etc).
    Fixed before ever writing the buggy version, by inspecting the already-
    executed caller frame's globals instead of re-running anything — but
    worth recording since it's the kind of mistake that's easy to make and
    hard to notice (no test failure, just quietly-doubled side effects).
  - A genuine ordering/concurrency bug caught mid-design, before
    implementation: the background `Dispatcher` reader thread and raw-REPL's
    own synchronous reads must never run concurrently against the same
    serial connection, or they'd race for bytes. Fixed by doing ALL raw-REPL
    work (hash-check, upload, start-app) before the `Dispatcher` ever starts,
    which is also *why* the design always restarts fresh rather than trying
    to detect "already running" first (that detection would reintroduce the
    exact same race).
  Reviewed (4-angle + manual security pass on `connection.py`, the highest-
  risk piece; a lighter combined pass on `mcu_decorators.py` and the
  `serial.py` additions): fixed a real bug (altitude finding) where a
  `connect()` failure left the serial port handle and a permanently-blocked
  reader thread leaked with no cleanup, blocking an immediate retry on the
  same port — now closed via `except BaseException: ser.close(); raise`
  around the whole connect sequence. Fixed a second real bug (also
  altitude): `_capture_caller()`'s runtime scan (finds anything with
  `__tether_export__` set, works regardless of control flow) and the AST
  slicer (only recognizes plain top-level `def`/`async def`) can disagree
  for a conditionally-defined decorated function — now cross-checked at
  connect() time with a clear `RuntimeError` instead of a confusing
  `MCUTimeoutError` far from the actual cause. Fixed a real security-
  adjacent bug (manual pass): the reserved `__tether_handshake__` handler
  registered *before* user functions, so a user function accidentally
  sharing that name would silently overwrite it and break the handshake
  for good — reordered so the reserved registration always wins.
  Simplification: derived the uploaded runtime file list from
  `tether_runtime/` on disk instead of hand-maintaining it (a real drift
  bug otherwise — a new file landing there would silently never get
  uploaded); cached `BoardHandle.__getattr__`'s built closures into
  `__dict__` so repeated access doesn't rebuild them and has normal
  identity. `write_files` also fixed to handle an empty-files call cleanly
  (would otherwise reference an unbound variable and raise `NameError`
  on-device). 26 new/changed tests across `test_connection.py` and
  `test_serial_transport.py`, plus 2 regression tests in `test_slicer.py`
  for the async-function AST-recognition bug below.
  One more bug this chunk surfaced in an *already-completed* chunk: the
  AST slicer (chunk 3) and stub generator (chunk 4) only recognized
  `ast.FunctionDef`, never `ast.AsyncFunctionDef` — so `@mcu.export async
  def` was invisible to the slicer, even though async MCU-export functions
  are a real, required case (an MCU function calling a `@pc.export` stub
  must itself be async to await it, per chunk 4's own docstring). Only
  surfaced once this chunk exercised the full pipeline with a genuinely
  async exported function for the first time. Fixed in
  `src/tether/slicer/__init__.py` with regression tests.

- [x] **11. Multi-board + disconnect handling** — done 2026-07-24
  `connection.py` / `dispatch/` — concurrent `BoardHandle`s, per-board method
  routing, `MCUDisconnectedError` on transport loss (fail loud, no silent
  retry), explicit `.reconnect()`. Depends on: 10.

  What was built:
  - Multi-board turned out to already be correct by construction from
    chunk 10's design (each `connect()` call produces its own `BoardHandle`
    closing over its own `Dispatcher`/transport, no shared registry) -
    confirmed with a new test connecting two independent `mock://` boards
    and calling both. No production code change needed for this part.
  - `Dispatcher.call_mcu()` (`dispatch/__init__.py`) now fails immediately
    with `MCUDisconnectedError` if the dispatcher already knows the
    transport is dead, instead of writing into a dead transport and
    hanging out a full timeout waiting for a response that can never
    arrive.
  - `BoardHandle.reconnect()` (`connection.py`) is implemented: both
    `_connect_mock` and `_connect_serial` now build a `dial: Callable[[],
    Dispatcher]` closure (re-running the full transport-specific
    connect flow - for serial: rediscover port if "auto", upload-if-needed,
    restart the on-device app fresh, re-handshake) passed into
    `BoardHandle`. `reconnect()` re-invokes it and swaps in the new
    `Dispatcher`. Cached call closures (`__getattr__`'s `self.__dict__`
    caching from chunk 10) resolve `self._dispatcher` dynamically at call
    time, so already-cached `board.read_temp` etc. keep working
    transparently after a reconnect with no need to re-fetch the attribute.
  - `Dispatcher.close()` (new): shuts down the incoming-call
    `ThreadPoolExecutor` (non-blocking). Called on the *old* dispatcher by
    `reconnect()` before returning, so a reconnect doesn't leak a fresh
    4-worker thread pool on every call. Does not (cannot) stop the reader
    thread - no way to unblock a thread mid blocking `read()` without the
    transport's own `close()`, which `Dispatcher` doesn't own; this remains
    the same accepted daemon-thread limitation from chunk 7/10, now just
    also reachable via the reconnect success path, not only the
    connect()-failure path.

  Real bug found and fixed (via altitude review, not a failing test - see
  below): the disconnected-flag check in `call_mcu()` was originally read
  *outside* `self._pending_lock`, while `_fail_all_pending()` set the flag
  and snapshotted `self._pending` *inside* it. A `call_mcu()` racing the
  reader thread's death could read `self._disconnected` as `None`, then
  insert its `_Pending` into `self._pending` *after* `_fail_all_pending`'s
  snapshot was already taken - that pending call would never be resolved,
  reproducing the exact hang this chunk exists to prevent. Fixed by moving
  the check inside the same `with self._pending_lock:` block used to
  insert the pending call, and moving the flag-set inside the same lock
  `_fail_all_pending` already uses to snapshot - both sides now share one
  critical section, closing the race entirely rather than narrowing it.

  Review (4 parallel cleanup agents + manual security pass, `/security-review`
  again unable to bootstrap - no git remote, same as every prior chunk):
  - Reuse: no findings - no existing "is transport alive" flag or
    reconnect/factory abstraction existed to reuse instead; `_dial_serial`
    logic (later folded into `_connect_serial`'s `dial()` closure per the
    simplification finding below) correctly delegates port rediscovery to
    the existing `serial_transport.discover()` rather than reimplementing
    it.
  - Simplification (2 findings, both applied): (1) the initial
    implementation had `_dial_serial` as a free function with the same
    5-arg signature as `_connect_serial`, which just forwarded into it by
    name - folded into a `dial()` closure nested directly inside
    `_connect_serial`, matching `_connect_mock`'s existing pattern (closes
    over outer variables, no restated parameter list, no misleading
    "shared by two closures" docstring for what's actually one closure
    referenced twice). (2) `BoardHandle.dial` was typed
    `Callable[...] | None = None` with a `NotImplementedError` guard in
    `reconnect()` for a case that never occurs (both real construction
    sites always pass `dial=`) - made it a required keyword-only param and
    deleted the dead guard.
  - Efficiency (1 finding, applied): `reconnect()` originally swapped in
    the new dispatcher without any teardown of the old one - for the mock
    transport in particular, each `dial()` spins up a brand-new
    `MockTransport` with its own thread/event loop/queues, so a reconnect
    (or retry loop) would leak one full thread-set per attempt, unbounded.
    Added `Dispatcher.close()` (see above) and call it on the old
    dispatcher from `reconnect()`.
  - Altitude (2 findings): (1) the pending-lock race described above -
    applied. (2) flagged that `reconnect()` abandons the old dispatcher
    with no teardown at all - already independently caught and fixed via
    the efficiency finding above by the time this landed, so no further
    action needed beyond what's already documented as the accepted
    reader-thread-specific limitation.
  - Security: no findings - this chunk touches no security boundary (no
    tokens, no `FileRef`, no WebView surface; N/A to this Python-only
    project regardless, noted for process consistency with the
    warp-vscode-integration CLAUDE.md's review cadence this project's own
    workflow was modeled on).

  140 tests passing (was 136; +2 connection-level: two-independent-boards,
  reconnect-produces-a-working-fresh-dispatcher; +2 dispatch-level:
  fail-fast-after-disconnect, close()-shuts-down-the-worker-pool).

- [x] **12. Wifi transport** — done 2026-07-24
  `transports/wifi.py` — pure stdlib `socket`. Requires target board already
  running a bootstrapped runtime (no code push over wifi). Depends on: 10.

  Scope decision made before writing code: DESIGN.md says wifi requires
  the board "already running a bootstrapped runtime" and that `tether`
  never pushes code over wifi, but nowhere specifies how the device gets
  there in the first place - there's no wifi-credential API, no on-device
  socket-server bootstrap variant, no port/handshake convention for it
  anywhere in the design. Rather than inventing new unrequested API surface
  to fill that gap (new `connect()` kwargs, a wifi-flavored
  `generate_bootstrap()` variant, etc.), scoped this chunk exactly to what
  CHUNKS.md's own line actually says: the PC-side pure-`socket` client only.
  The on-device-listener story is left as an explicit, documented gap - the
  same treatment this project has given real-hardware validation throughout
  (flagged prominently, not silently pretended away), not an oversight.

  What was built:
  - `WifiStream` (`transports/wifi.py`): duplex wrapper around a connected
    TCP socket, matching the same plain `read()`/`write()`/`close()`
    contract `SerialStream` provides. `read()` is a single `recv()` -
    already blocks for real data and returns `b""` on peer-close exactly
    matching `Dispatcher._run_reader`'s "empty read means disconnected"
    contract, no chunking dance needed (TCP sockets give clean blocking
    semantics directly, unlike serial's `in_waiting` drain).
  - `connect(host, port=DEFAULT_PORT, *, timeout)`: opens the TCP
    connection via `socket.create_connection`, sets `TCP_NODELAY` (see
    efficiency finding below), then hands off from the connect-phase
    timeout to blocking reads for the ongoing dispatch phase - same
    two-phase timeout handoff pattern as `serial.py`'s `ser.timeout = None`.
  - `connection.py`: new `wifi:<ip>[:<port>]` scheme wired into `connect()`.
    `_connect_wifi()` does no slicing/bundling/upload (nothing to push) -
    just builds a `dial()` closure that opens a fresh `WifiStream` and
    performs the handshake, following the same `dial`/`BoardHandle(...,
    dial=dial)` pattern chunk 11 established for mock/serial. Multi-board
    and `reconnect()` therefore work for wifi boards for free, with no
    wifi-specific code needed beyond `dial()` itself.
  - `_start_and_handshake()` (new, `connection.py`): extracted from what
    was originally near-duplicate `dispatcher.start()` +
    `call_mcu(_HANDSHAKE_NAME)` + version-check logic independently
    written for both `_connect_serial` and `_connect_wifi` - see reuse/
    simplification findings below.

  Review (4 parallel cleanup agents + manual security pass, `/security-review`
  again unable to bootstrap - no git remote, same as every prior chunk):
  - Reuse + Simplification (both independently found the same issue,
    applied): the handshake/version-check block was duplicated near-verbatim
    between `_connect_serial`'s and `_connect_wifi`'s `dial()` closures,
    differing only in the remediation hint text. Extracted
    `_start_and_handshake(stream, *, timeout, mismatch_hint)`, called from
    both. Simplification also flagged a test hand-rolling its own
    socket-wrapper class (`_ConnStream` in `test_connection.py`) duplicating
    `WifiStream` - fixed to import and reuse `WifiStream` directly on the
    fake-device side of the test too.
  - Efficiency (1 finding, applied): missing `TCP_NODELAY` - this is a
    synchronous request/response RPC protocol sending small msgpack frames
    one at a time; without it, Nagle's algorithm interacting with the
    peer's delayed-ACK timer classically stalls ~40ms per call. Set
    `socket.IPPROTO_TCP, socket.TCP_NODELAY` right after connecting. Also
    bumped `_RECV_CHUNK` from 4096 to 65536 (minor - fewer syscalls on
    large `bytes` payloads; `FrameDecoder` already buffers/reassembles
    correctly regardless of chunk size, so this was never a correctness
    issue, just avoidable syscall overhead).
  - Altitude (1 finding, applied): `_connect_wifi`'s `dial()` had no
    failure-path cleanup, unlike `_connect_serial`'s `except BaseException:
    ser.close(); raise` - a failed handshake (wrong protocol version, no
    response) would leak the TCP socket and its blocked reader thread with
    no way to close it, since `WifiStream` didn't even expose a `close()`.
    Added `WifiStream.close()` and wrapped `_connect_wifi`'s `dial()` body
    in the same `try/except BaseException: stream.close(); raise` pattern.
    Also confirmed `BoardHandle.reconnect()` (chunk 11) works correctly for
    wifi boards with no wifi-specific code - `dial()` redials a genuinely
    fresh socket each call, no stale closure-variable capture.
  - Security: no findings requiring a code change, but one real property of
    this chunk worth recording explicitly rather than leaving implicit: the
    wifi transport is a plaintext, unauthenticated TCP socket - anyone who
    can reach the device's IP can issue or intercept RPC calls, and this
    was already inherent to the wire protocol as locked in chunk 6 (no
    auth/encryption framing exists at the DESIGN.md level), not something
    newly introduced here. Serial's physical-proximity requirement and the
    in-process mock transport never surfaced this; wifi is the first
    transport that exposes it over a real network. Adding auth/TLS would be
    a DESIGN.md-level architecture decision (what scheme, PSK vs cert,
    bundle-size cost) well beyond a client-transport chunk's scope, so
    flagged here rather than silently fixed or silently ignored.

  145 tests passing (was 140; +4 transport-level in new
  `test_transport_wifi.py`: write/read round-trip over a real
  `socket.socketpair()`, empty-read-on-peer-close, real TCP connect via a
  background-thread listener, connection-refused-when-nothing-listens;
  +1 connection-level end-to-end test using a real TCP socket with a real
  `tether.dispatch.Dispatcher` standing in for the already-running device).

- [x] **13. BLE transport** — done 2026-07-24
  `transports/ble.py` — `bleak`-backed, custom GATT service (write +
  notify characteristic), frame chunking/reassembly across BLE MTU hidden
  in this module. Depends on: 10.

  Same scope decision as chunk 12: DESIGN.md gives BLE "the same bootstrap
  requirement as wifi" (already-running runtime, no code push) but
  specifies no on-device GATT peripheral story - no `bluetooth`/`aioble`
  implementation, no advertising convention. Scoped to the PC-side
  (central/client) half only; the on-device-listener gap is documented
  explicitly, same treatment as wifi's and real-hardware validation
  throughout. `bleak` (the only viable Python BLE library) is
  client/central-only, so unlike wifi (where a real TCP socket pair could
  stand in for "an already-running device" in tests), there is no way to
  run a real local BLE peripheral to test against on any platform, with or
  without hardware - `bleak` itself isn't even installed in this dev
  environment (optional extra, `tether[ble]`).

  What was built:
  - `BleStream` (`transports/ble.py`): bridges bleak's async
    `BleakClient` API to the plain sync `read()`/`write()`/`close()`
    contract `Dispatcher` expects. BLE is push-based (notifications arrive
    via callback) rather than pull-based like serial/wifi, so `read()` is
    backed by a `queue.Queue` fed by `on_notify()` (bleak's notification
    callback - thread-safe `queue.Queue.put`, no `call_soon_threadsafe`
    needed since bleak invokes it on the loop's own thread, unlike
    `MockTransport`'s `asyncio.Queue` bridge which does need it).
    `write()` splits payloads exceeding the negotiated ATT MTU into
    multiple GATT writes, submitted as one coroutine (one cross-thread hop
    per `write()` call) that awaits each chunk in order - BLE allows only
    one outstanding ATT request at a time, so this sequencing is a
    correctness requirement, not just convenient style (see finding
    below). `signal_closed()` pushes the same empty-bytes "transport
    closed" signal `Dispatcher._run_reader` already looks for, so a BLE
    disconnect fails loud through the identical path as a dead serial port
    or closed socket.
  - `connect(address, timeout)`: same async-to-sync bridging shape
    `transports/mock.py` established in chunk 8/10 - a dedicated
    background thread owns its own event loop and the `BleakClient` for
    the connection's lifetime, connects, subscribes to notifications, and
    hands the result back to the calling thread via a
    `concurrent.futures.Future`.
  - `connection.py`: new `ble:<addr>` scheme, `_connect_ble()` mirroring
    chunk 12's `_connect_wifi()` exactly (dial()/`_start_and_handshake()`/
    close-on-failure). BLE MAC addresses contain colons themselves;
    confirmed `address.partition(":")`'s single-split-only semantics
    preserve the full address correctly (a test pins this explicitly).

  Review (4 parallel cleanup agents + manual security pass, `/security-review`
  again unable to bootstrap - no git remote, same as every prior chunk):
  - Reuse (1 finding, skipped with reasoning): `connect()`'s
    thread-owns-an-event-loop bootstrap shape duplicates the same idea
    `MockTransport.start()`/`_run_mcu()` established in chunk 8/10
    (`threading.Event`-style readiness signal + daemon thread). Not
    factored into a shared helper: `MockTransport`'s bootstrap is deeply
    entangled with dispatch-runtime-specific setup (shim installation,
    exported-function registration) while `ble.py`'s is entangled with
    bleak-specific connect/notify/GATT setup - forcing a shared "start a
    thread with an event loop and wait for readiness" abstraction over two
    sufficiently different bodies would be a thin wrapper saving little
    real duplication, and touching the already-shipped, working
    `MockTransport` for a marginal DRY gain elsewhere carries more risk
    than benefit. (The simplification finding below independently made
    `connect()`'s own version of this pattern smaller regardless.)
  - Simplification (1 finding, applied): `connect()`'s hand-rolled
    `threading.Event()` + loosely-typed `outcome: dict[str, Any]` result/
    exception carrier collapses onto `concurrent.futures.Future`, which
    does the same job natively (`.result(timeout=...)` raises the stored
    exception or `concurrent.futures.TimeoutError` automatically, no
    manual dict-key-presence check needed). Applying this also closed the
    efficiency finding below for free via `Future.cancel()`'s built-in
    state machine.
  - Efficiency (2 findings): (1, applied) `write()` originally submitted
    one `run_coroutine_threadsafe` per MTU chunk instead of one coroutine
    looping over all chunks - collapsed to a single cross-thread hop per
    `write()` call (see above), which also gave the altitude finding below
    a natural place to document the real ordering requirement. (2, not
    changed - documented instead) `response=True` on every GATT write
    trades throughput for a per-write delivery guarantee; flagged as an
    "undiscussed default" by the reviewer. Kept deliberately rather than
    flipped to `response=False`: this project has no real BLE hardware to
    validate write-without-response's actual reliability across
    platforms/controllers, and DESIGN.md's wire protocol has no
    independent per-frame ack to fall back on if a chunk were silently
    dropped - added an explicit comment recording this reasoning so it
    reads as a deliberate choice, not an oversight.
  - Altitude (3 findings, all applied): (1) the per-chunk blocking in
    `write()` is an ATT single-outstanding-request correctness requirement
    (out-of-order chunk delivery could corrupt the frame being
    reassembled), not just convenient sequencing - was undocumented as
    such; now has an explicit comment. (2) `connect()` could leak an
    active, unmanaged BLE connection if the calling thread's
    `result.result(timeout=...)` gave up before the background thread's
    connect attempt finished (worse than an idle leaked thread - most
    peripherals accept only one central connection, so this could strand
    the device against every future reconnect attempt) - fixed using
    `Future.cancel()`'s state machine: a cancelled-before-set future makes
    the background thread's later `set_result()`/`set_exception()` raise
    `InvalidStateError`, which it now catches to disconnect and stop its
    loop instead of silently succeeding into a connection nobody holds.
    (3) `BleStream.close()` disconnected the client but never stopped the
    event loop/thread `connect()` created - unlike wifi/serial, no
    blocking syscall justifies keeping that thread alive once
    disconnected, so this was an avoidable leak on every `close()` call,
    not the same accepted "reader thread blocked in `read()`" limitation
    documented elsewhere - fixed with `loop.call_soon_threadsafe(loop.stop)`
    after disconnect completes.
  - Security: no code-level finding, but recorded the same class of note
    as chunk 12's: BLE here uses a plain, unauthenticated GATT connection
    with no pairing/bonding enforced - anyone in range who discovers the
    service UUID can connect and issue/intercept RPC calls. Inherited from
    the wire protocol's chunk-6-locked lack of auth/encryption framing,
    not newly introduced; adding it would be a DESIGN.md-level decision
    beyond a client-transport chunk's scope.

  151 tests passing (was 145; +5 in new `test_transport_ble.py` against a
  hand-written fake matching bleak's documented async API shape: MTU
  chunking, notify-to-read delivery, disconnect-signal delivery,
  close()-disconnects-the-client, and connect()-fails-loud-without-bleak-
  installed (a genuinely true statement in this dev environment, not a
  simulated case); +1 in `test_connection.py` confirming the `ble:` scheme
  routes correctly and preserves a colon-containing MAC address).

- [x] **14. ruff cleanup pass** — done 2026-07-24
  `slicer/` — post-slice unused-import stripping via `ruff`, applied to the
  bundle before upload. Depends on: 3.

  Confirmed this addresses a real, existing gap (not a hypothetical): the
  AST dependency walk (`_bound_names`/`_collect_bindings`) can only
  include/exclude a whole `import`/`from...import` *statement* at a time,
  since every name a multi-name import binds maps to the SAME AST node in
  `bindings`. `from time import monotonic, sleep` where only `sleep` is
  referenced by included code still renders both names into the bundle
  pushed to the device - wasted bytes, and on-device an `ImportError` risk
  if the unused name doesn't even exist on MicroPython. Verified by reading
  the existing code (chunk 3) before writing anything, then pinned with a
  regression test.

  What was built:
  - `_strip_unused_imports(source)` (new, `slicer/__init__.py`): invokes
    the real `ruff` CLI as a subprocess (`ruff check --fix --exit-zero
    --select=F401 --stdin-filename=... -`, source piped via stdin, fixed
    source captured from stdout) - not linked as a library, since ruff has
    no stable Python API for this (DESIGN.md § Dependencies, already
    locked before this chunk started). `slice_mcu_bound()` now calls this
    on its rendered output before returning `SliceResult`.
  - Moved `ruff` from `pyproject.toml`'s `dev` extra into core
    `dependencies` and re-ran `uv lock`: this is no longer just a lint
    tool, it's invoked at `connect()` time by any scheme that slices
    (currently only `serial:`) - DESIGN.md's own § Dependencies section
    already listed it as a real dependency of the slicing pipeline, not an
    optional dev convenience, so this wasn't a new decision, just applying
    an existing one.

  Real correctness finding, self-caught (not from a review agent - see
  process note below): manually verified `ruff`'s actual exit-code
  behavior via `bash` before trusting any flag combination. Without
  `--exit-zero`, `ruff check --fix` exits non-zero whenever a REMAINING
  violation exists after fixing - and ruff's F401 safety heuristics
  deliberately never autofix an import inside a `try`/`except` (can't
  prove removal is safe), which is a common MicroPython compatibility
  idiom (`try: import ujson except ImportError: ...`). Combined with
  `check=True` on `subprocess.run`, that would have made `slice_mcu_bound()`
  raise `CalledProcessError` and crash the whole slice on a completely
  ordinary, safe pattern - not a hypothetical, confirmed by literally
  running that exact source through `ruff check --fix` without
  `--exit-zero` and watching it exit 1. `--exit-zero` was already present
  in the first implementation (written with this failure mode in mind),
  but its reasoning wasn't documented and there was no test pinning it -
  added both: an explicit comment on why `--exit-zero` + `check=True` are
  combined deliberately (one absorbs "ruff still found something it
  couldn't fix", the other still fires loud for a genuine subprocess
  failure), and a regression test asserting `slice_mcu_bound()` doesn't
  raise on exactly that try/except idiom.

  Process note: the simplification review agent for this chunk failed
  mid-run (`"You've hit your monthly spend limit"`), and the reuse agent's
  own output carried a safety-classifier-unavailable warning asking for
  extra verification of its claims (both re-checked manually before
  trusting - the reuse finding of "no existing subprocess pattern to reuse"
  was independently confirmed by grepping `src/tether/` myself). Given the
  spend limit, efficiency and altitude were reviewed manually instead of
  via further parallel agents - the try/except finding above came out of
  that manual pass, specifically from actually running `ruff` against
  several inputs in `bash` rather than reasoning about its CLI flags from
  memory alone.
  - Reuse (1 agent finding, confirmed, no action needed): no existing
    subprocess/CLI-invocation pattern anywhere in `src/tether/` to reuse -
    `_strip_unused_imports` is the first, appropriately scoped to this one
    function.
  - Simplification/Efficiency/Altitude (manual): no issues beyond the
    exit-code finding above, which is really a correctness/altitude-level
    finding surfaced while checking the simplification question ("is
    `--exit-zero` + `check=True` redundant?") - answered definitively by
    testing, not guessing, and documented + regression-tested rather than
    left implicit.
  - Security: no findings. `subprocess.run` is called with an argument
    list (no `shell=True`), and `source` is passed via `input=` (stdin),
    never interpolated into the command line - no injection surface.
    `--stdin-filename` is a fixed constant, not user-controlled.

  153 tests passing (was 152; +1 pinning the multi-name-import stripping
  behavior itself, +1 pinning the try/except-unused-import survival case).

- [x] **15. End-to-end example + docs** — feature-complete 2026-07-24,
  **hardware-verified 2026-07-24** on a real ESP32-WROOM-32D (CH340
  USB-serial bridge). The example ran end-to-end for real: connected over
  `serial:auto`, uploaded and started the bundle, blinked the onboard LED 5
  times (visually confirmed), and logged progress back from MCU to PC after
  each blink. This is the first real-hardware validation anywhere in this
  project's history - see the dedicated section below for the four real
  bugs it found (none of which any prior test, mock or otherwise, could
  have caught) and how each was fixed.
  A real single-file example (e.g. blink/read-sensor over serial) in
  `examples/`, README walkthrough verified against actual hardware. Depends
  on: 9, 10.

  What was built:
  - `examples/blink_and_log/blink_and_log.py`: a real, complete,
    directly-runnable single file. `@mcu.export async def blink(times)`
    blinks an onboard LED and calls back `@pc.export def
    log_progress(blink_number, total)` after each blink; a plain
    `if __name__ == "__main__":` block at the bottom drives it via
    `mcu.connect("serial:auto")`, exactly the "single file, run it
    directly" pitch from `docs/DESIGN.md`'s own opening paragraph.
  - README.md rewritten: fixed the original snippet (which referenced an
    undefined `adc` with no import shown - always illustrative pseudocode,
    never actually runnable), added a full walkthrough section pointing at
    the real example, documented expected output, and added an explicit
    "not run against real hardware" note in the same place a reader would
    look for confirmation it works - not buried in CHUNKS.md where a
    library user would never see it.

  Real design gap found and fixed, by actually trying to write a genuinely
  runnable example rather than another illustrative snippet - exactly the
  kind of thing this chunk exists to catch: `docs/DESIGN.md`'s own § slice
  step uses `led = Pin(2, Pin.OUT)` at *module level* as its illustrative
  example of what the AST slicer captures. That line is true on its own
  (the slicer does capture it) - but a module-level `from machine import
  Pin` / `led = Pin(2, Pin.OUT)` executes under CPython too, the moment the
  single file is run directly as a PC script (that's the whole "single
  file" pitch), and `machine` doesn't exist there - confirmed by actually
  running it and watching it crash with `ModuleNotFoundError`, not by
  inspection alone. This would have made *any* example following
  DESIGN.md's own illustrated pattern literally unrunnable on a PC.
  Fixed by writing the example with hardware objects constructed lazily
  *inside* the `@mcu.export` function body instead (a `_get_led()` helper,
  called from `blink()`) - that code path only ever executes on the MCU,
  never on the PC, so the PC-side run of the file never touches `machine`
  at all. Documented this as a correction directly in `docs/DESIGN.md`'s §
  Architecture overview (dated, explains what changed and why, per
  CLAUDE.md's instruction to amend DESIGN.md explicitly rather than
  silently drift from a decision that turned out to need fixing) - the
  slicer's own capture behavior is unchanged and still correct, only the
  illustrative pattern needed correcting.

  Verified without hardware (`tests/test_examples.py`, 4 tests):
  - The example file parses and slices correctly: exactly `blink` is
    exported, `_get_led`/`_led` (its dependencies) are pulled in, and
    PC-only driver code (`mcu.connect`, the `__main__` guard) is never
    sliced onto the device.
  - `generate_bootstrap()`'s output for the real sliced+stub source is
    syntactically valid Python (`ast.parse` doesn't raise).
  - The generated on-device bundle's registration and dispatch wiring
    actually runs correctly under the real `micropython` unix-port
    interpreter (same rigor as chunk 10's own end-to-end bootstrap test) -
    `machine.Pin` faked out (no real GPIO under the unix port; everything
    else in the generated script is exactly what would be uploaded to a
    real board), driving a real `blink(2)` call through a real `Dispatcher`
    on both sides and asserting the fake LED toggled and progress was
    logged back in the right order.
  - (One test was written and then removed as a design mistake, not a
    product bug: attempted to call `connect("mock://")` from a *different*
    test module than the one defining `blink` - `_capture_caller()`'s
    frame-based caller detection can only ever see decorated functions in
    the module that actually calls `connect()`, so this could never have
    worked regardless of implementation. Removed rather than worked
    around, since the real-MicroPython test above already covers the same
    ground more rigorously.)

  157 tests passing (was 153; +4 in new `tests/test_examples.py`).

  ---

  ## Real hardware validation (2026-07-24)

  A real ESP32-WROOM-32D became available. Flashed with the current stable
  MicroPython (v1.28.0, `ESP32_GENERIC` build, via `esptool`). Getting to
  the point of running anything required installing WCH's CH340 VCP driver
  on macOS (the board's onboard USB-serial bridge chip, VID `0x1a86`/PID
  `0x7523` - already in `discover()`'s known-device list from chunk 9) and
  approving it as a system extension - not a `tether` concern, but the
  reason no earlier session could have done this without a human present
  for the driver's GUI approval step.

  Every one of the four bugs below was invisible to every prior test in
  this project - mock transport, real MicroPython unix-port interpreter,
  scripted raw-REPL fakes, real TCP sockets - because none of them can
  model real UART timing, real MicroPython's actual (as opposed to
  assumed) keyboard-interrupt behavior, or exercise the literal code path
  a real end user's `mcu.connect(...)` call goes through. This is exactly
  why this chunk's own definition of done required real hardware and
  wasn't satisfiable by "verified as thoroughly as possible without it" -
  the gap between those two turned out to be four real, load-bearing bugs.

  **1. `mcu.connect(...)` didn't exist - `connect` was never wired onto the
  `mcu` namespace object.** `docs/DESIGN.md`'s own locked architecture (and
  every example, README snippet, and this very chunk's example) has always
  shown `mcu.connect("serial:auto")`. The actual chunk 10 implementation
  put `connect()` as a bare function in `connection.py` and every test
  imported it directly (`from tether.connection import connect`), so this
  divergence from the locked API was never exercised by anything until the
  first line of real usage: `mcu.connect(...)` raised
  `AttributeError: '_McuNamespace' object has no attribute 'connect'`.
  Fixed in `tether/__init__.py`: `mcu.connect = connect` - assigning the
  SAME function object as a plain instance attribute (not defined in
  `_McuNamespace`'s class body) means Python's descriptor protocol never
  binds `self`, so `mcu.connect(...)` invokes `connect()` with no `self`
  injected and no extra stack frame - `_capture_caller()`'s frame-counting
  needed zero changes. Regression test:
  `test_mcu_connect_is_the_public_api_design_md_documents`.

  **2. `push_raw_repl(..., wait=False)` corrupted the dispatch loop's first
  frame by unconditionally sending the raw-REPL exit sequence into a
  program that had already taken over stdio.** First symptom: `connect()`
  over real serial failed with `MCUDisconnectedError: transport read
  failed: frame too large: declared 72643169 bytes` - a garbage length
  prefix. Decoding those 4 bytes revealed `\x04Tra` - the start of
  "Traceback", meaning the device had actually crashed. `push_raw_repl`'s
  `finally: _exit_raw_repl(serial_obj)` sent `\r\x02` (ctrl-B) regardless
  of `wait=True` or `wait=False` - for `wait=False` (used to start the
  forever-running dispatch loop, chunk 9's own design), this happens
  immediately after the "OK" exec ack, racing the interpreter's transition
  from the raw-REPL protocol handler to the user script's own stdio
  consumption. On real hardware those 2 bytes can land while raw-REPL's
  handler is still in control, producing REPL/prompt noise that corrupts
  the first bytes the dispatch loop's `FrameDecoder` reads. Fixed:
  `wait=False` no longer sends the exit sequence at all - there's no raw-REPL
  session left to cleanly exit back to once a forever-running program has
  taken over stdio anyway, so it's skipped rather than raced. Updated
  `test_push_raw_repl_wait_false_returns_without_waiting_for_completion`
  (previously asserted the buggy behavior - `endswith(b"\r\x02")` - now
  asserts `\x02` is never sent for `wait=False`).

  **3. MicroPython intercepts a raw `0x03` byte as Ctrl-C even when it's
  just a data byte inside a running program's own stdin stream - directly
  contradicting chunk 10's own prior finding.** Second symptom, after
  fixing #2: the handshake succeeded, but the very next real call
  (`add(2, 3)`) crashed the device the same way. Full traceback this time:
  `KeyboardInterrupt` inside `asyncio.core.wait_io_event`. msgpack encodes
  small integers as their own literal byte value, so the argument `3`
  becomes a literal `0x03` byte inside the frame - MicroPython's UART
  driver intercepted it as Ctrl-C and killed the dispatch loop from the
  inside. Chunk 10's own investigation (see its CHUNKS.md entry) concluded
  this was purely a *host-side PTY line-discipline artifact* under CPython
  and not a real MicroPython/hardware behavior, based on a PTY-based test
  under CPython - that conclusion is now known to be wrong for real
  firmware on real hardware; the PTY test only ever proved something about
  the *host's* terminal handling, never about MicroPython's own UART
  driver. Fixed: `generate_bootstrap()` now emits `import micropython as
  _tether_micropython` and calls `_tether_micropython.kbd_intr(-1)` before
  anything else runs - a real, documented MicroPython API that exists
  specifically to disable the built-in keyboard-interrupt character so a
  UART can carry a raw binary protocol safely. Verified directly against
  hardware before implementing (manual raw-REPL script sending
  `micropython.kbd_intr(-1)` then the same calls that previously crashed -
  `add(2, 3) = 5`, `add(10, 20) = 30`, no corruption) and then via the real
  fix: `test_generate_bootstrap_disables_the_ctrl_c_keyboard_interrupt`.

  **4. `@pc.export` functions were never registered as PC-side dispatch
  handlers, for any transport - the "MCU calls PC" half of the pitch was
  never actually wired up in `connect()`, and no test anywhere had ever
  exercised it end-to-end, including under the mock transport.** Third
  symptom, after fixing #2 and #3: `add()` and `greet()` (plain MCU calls)
  both worked correctly over real hardware, but the example's reverse call
  (MCU calling back into a `@pc.export` PC function) failed with
  `RemoteError: no handler named 'log_from_mcu'`. Root cause:
  `_capture_caller()` only ever collected `side == "mcu"` specs into
  `export_specs`; nothing anywhere in `_connect_mock`/`_connect_serial`/
  `_connect_wifi`/`_connect_ble` ever called `dispatcher.register(name,
  fn)` for a `@pc.export` function. This gap existed since chunk 10 and
  survived chunks 11-15 undetected because the one test that could have
  caught it - `test_connection.py`'s `_mock_read_scaled` (defined in chunk
  10 specifically to test the *slicer's* handling of async MCU functions
  calling PC stubs) - was never actually *called* by any test; it only
  needed to exist and slice correctly for its original purpose. Fixed:
  `_capture_caller()` now also collects `side == "pc"` callables into a new
  `pc_handlers` dict, threaded through every `_connect_*`/`dial()` path and
  registered on each dispatcher (via the shared `_start_and_handshake()`
  for serial/wifi/ble, and directly in `_connect_mock`'s `dial()`) before
  `dispatcher.start()`. Regression test (mock, not hardware-dependent -
  this bug was 100% reproducible under mock once actually exercised):
  `test_connect_mock_mcu_can_call_back_into_a_registered_pc_export_function`,
  asserting `board._mock_read_scaled() == 42`.

  Self-reviewed (manual, not via parallel agents, following chunk 14's
  precedent after that session's spend-limit interruption - each fix above
  was TDD'd individually with a RED test against the actual bug before
  changing implementation, then confirmed GREEN against both the fake test
  suite and, for #2-#4, real hardware directly). No further findings.
  Security: no new surface - `micropython.kbd_intr(-1)` only affects which
  byte the interpreter treats as an interrupt signal, doesn't change what
  data the wire protocol carries or who can send it.

  160 tests passing (was 157; +1 `mcu.connect` identity test, +1 reverse
  pc-handler-registration test via mock, `push_raw_repl`'s existing
  wait=False test updated in place rather than added to since it pins the
  same behavior corrected, not new behavior).

- [x] **16. CI: lint workflow** — done 2026-07-24
  GitHub Actions workflow running `ruff check` + `ruff format --check` (via
  `uv`) on push/PR. No hardware/token dependency — safe to build anytime.

  `.github/workflows/lint.yml`: `actions/checkout@v7` +
  `astral-sh/setup-uv@v9` (current major versions, checked via `gh api`
  rather than guessed from memory), `uv sync --locked` (fails loud if
  `uv.lock` has drifted from `pyproject.toml`, rather than silently
  re-resolving - a real CI-correctness gap if omitted), `ruff check .`,
  `ruff format --check .`. Triggers on push to `main` and on every PR.

  Real bug found by actually running the exact commands locally before
  trusting the workflow file, not by reading it: `ruff format --check .`
  covers fenced Python code blocks inside `README.md` too (a newer ruff
  behavior), and README's snippet had drifted out of format - this would
  have made chunk 16's own first CI run fail immediately. Fixed by
  running `ruff format` for real and cleaning up a comment it mangled
  across an import line. Also discovered mid-verification: running the
  lint workflow's exact `uv sync` (no extras) locally strips `pytest`/
  `pyserial` out of the shared dev venv, since `dev`/`serial` are optional
  extras, not core deps - restored via `uv sync --extra dev --extra
  serial` afterward. Neither of these would have been caught by reading
  the YAML alone.

- [x] **17. CI: PyPI release workflow** — done 2026-07-24 (workflow itself
  complete and locally verified end-to-end except the actual PyPI upload,
  which needs the real `PYPI_API_TOKEN` secret the user is providing -
  see below)
  GitHub Actions workflow that builds and publishes to PyPI on release (via
  `uv build` / `uv publish`). Needs a PyPI token supplied by the user as a
  repo secret before this can run for real.

  `.github/workflows/publish.yml`: triggers on `release: types:
  [published]` (not on every tag push - lets a release be drafted/reviewed
  before anything uploads). Two jobs: `test` (full `ruff check` + `ruff
  format --check` + `pytest`, deliberately gating `publish` via `needs:`)
  then `publish` (`uv build` + `uv publish`, reading
  `secrets.PYPI_API_TOKEN` into `UV_PUBLISH_TOKEN`). Expected secret name:
  **`PYPI_API_TOKEN`** — a PyPI API token scoped to this project, added
  under the repo's Settings > Secrets and variables > Actions.

  Judgment call beyond this chunk's literal one-line spec, applied and
  documented rather than silently added: publishing to PyPI is
  irreversible (no unpublishing), so the workflow gates the `publish` job
  behind a full test run rather than trusting that whatever's tagged for
  release already passed CI earlier - a release built from an untested
  commit, or a lockfile that had drifted, would otherwise ship straight to
  PyPI. The `test` job deliberately does NOT install the `ble` extra:
  `test_transport_ble.py` and `test_connection.py` each assert that
  `connect()`/BLE `connect()` fail loud with `ModuleNotFoundError` when
  `bleak` isn't installed - installing it in CI would make those
  assertions false and break them, so this mirrors local dev (`dev` +
  `serial` only) on purpose.

  Locally verified everything short of the actual upload (no real PyPI
  token exists yet to test the last step): `uv sync --locked --extra dev
  --extra serial`, `ruff check .`, `ruff format --check .`, `pytest`
  (157 passing), and `uv build` (produces `dist/tether-0.1.0.tar.gz` +
  `dist/tether-0.1.0-py3-none-any.whl` successfully) all run clean,
  matching exactly what the `test` and `publish` jobs' steps do. The
  `uv publish` step itself is untested - `UV_PUBLISH_TOKEN` isn't set
  locally and shouldn't be faked - once the real secret is added and a
  release is published, this is the one thing left to confirm actually
  works end-to-end.

- [x] **18. Ambient board context + serial reconnect reliability** — done
  2026-07-25. Not part of the original 17-chunk plan - added after the
  user raised a design concern about `board.blink(5)`'s calling
  convention feeling like unexplained magic (a plain top-level function
  with no visible connection to the `board` object it becomes an attribute
  of). Two pieces of work, both driven by continued real-hardware testing
  on the same ESP32-WROOM-32D from chunk 15.

  ## Part A: ambient board context

  Design settled via discussion, not unilaterally: talked through the
  actual tension (decorators run at module-import time, before any board
  object exists, so a decorated function can't syntactically reference
  its board; multi-board support needs *some* explicit disambiguation
  mechanism) and landed on `contextvars`-based ambient state - closer to
  algebraic-effects/dynamic-scoping than the more "FP-pure" Reader-pattern
  alternative also discussed, chosen because it's what actually satisfies
  "call it like normal Python, no board-awareness at the call site" for
  the common single-board case, which is what was asked for. See
  DESIGN.md's dated amendment under § Call semantics for the locked
  writeup.

  What was built:
  - `src/tether/_context.py` (new): `current_board: ContextVar` - split
    into its own tiny module so `decorators.py` (reads it) and
    `connection.py` (sets it) don't need to import each other.
  - `decorators.py`'s `@mcu.export` now returns a dispatch wrapper
    (`functools.wraps`-preserved) instead of the original function -
    calling the decorated name directly looks up `current_board.get()`
    and dispatches through `getattr(board, fn.__name__)(*args, **kwargs)`,
    reusing `BoardHandle.__getattr__`'s already-cached call closure rather
    than reimplementing dispatch. `@mcu.loop` is unchanged (never called
    directly by anyone, PC or otherwise, so it doesn't need this).
  - `connection.py`'s `connect()` sets `current_board` to the
    newly-connected board (most-recent-wins default - simple, and matches
    how e.g. matplotlib's "current figure" works). `BoardHandle` gained
    `__enter__`/`__exit__` (`with board:`) for explicit multi-board
    scoping, using a `threading.local()` stack of context tokens (not a
    plain list/single field) so it's correct both for nesting *and* for
    the same `BoardHandle` being entered concurrently from different
    threads - not just single-threaded convenience.
  - `dispatch/__init__.py`'s `Dispatcher` gained a `board` attribute and
    `_handle_call` now scopes `current_board` to `self.board` for the
    duration of running each incoming-call handler (via a new
    `_board_scoped()` contextmanager, mirroring the existing
    `_heartbeat_ticking` pattern rather than a bespoke inline try/finally)
    - so a reentrant `@pc.export` handler's own ambient calls resolve to
    whichever board actually triggered it, not whatever's ambient on the
    connecting/main thread. Verified empirically, not assumed, that this
    was necessary: neither plain `threading.Thread` nor
    `concurrent.futures.ThreadPoolExecutor` propagate a calling thread's
    `contextvars.Context` to the thread that runs the work (unlike
    `asyncio.Task`, which does) - confirmed with small throwaway scripts
    before writing any implementation.
  - `transports/mock.py` fixed to register `spec.func` (the real
    undecorated callable, always available via `ExportSpec.func`) instead
    of `namespace[name]` (now the PC-side dispatch wrapper) as the mock
    "device"'s own handler - the same `tether.decorators.mcu`/`pc`
    singleton objects get re-exec'd inside `MockTransport` to simulate the
    device side, and the wrapper's ambient-dispatch behavior would be
    wrong there (it would try to look up a board from inside the
    simulated device's own execution).
  - `examples/blink_and_log/blink_and_log.py` updated to demonstrate the
    new style (`blink(5)`, not `board.blink(5)`) and re-verified
    end-to-end against real hardware, LED blink visually confirmed.

  Real correctness gap found via review (altitude angle) and fixed before
  it could bite: `dispatcher.board = self` was originally set only
  *after* `dial()` returns (`BoardHandle.__init__`/`reconnect()`), but
  `dial()` itself calls `dispatcher.start()` before `BoardHandle` exists
  to reference - for the very first-ever `connect()` this window is
  practically unreachable (nothing has been uploaded/started yet for a
  background task to call back through), but for `reconnect()`
  specifically it's real: the device may already be running an
  `@mcu.loop` background task that calls back immediately, before the
  post-hoc assignment would otherwise run. Fixed by threading a mutable
  closure variable (`board: BoardHandle | None`) through each
  `_connect_*()` function that `dial()` itself reads and sets
  `dispatcher.board` from, before `dispatcher.start()` - `None` only for
  the still-unreachable very-first-call case, correctly populated for
  every `reconnect()` afterward since the closure variable gets updated
  once the first `BoardHandle` exists. Regression test:
  `test_reentrant_handler_sees_the_board_correctly_immediately_after_reconnect`.

  Reviewed via 4 parallel cleanup agents (spend limit had recovered from
  chunk 14's interruption) plus a manual security pass (no findings - no
  security boundary touched). Reuse: no findings beyond one cosmetic note
  (applied - see `_board_scoped()` above). Simplification (1 finding,
  applied): a `_TIMEOUT_NOT_GIVEN` sentinel in the dispatch wrapper was
  unnecessary - plain `**kwargs` forwarding to the already-timeout-aware
  cached closure does the identical job with less code. Efficiency: no
  findings - the ContextVar set/reset pair and cached-closure `getattr`
  are both negligible next to the thread-pool submission and (when used)
  heartbeat-ticker thread already on the same path. Altitude (3 findings):
  the `dispatcher.board`/reconnect race above (applied, see above); the
  `mock.py` `spec.func` fix confirmed complete (no other site assumed the
  decorated name was still the original callable, `@mcu.loop` unaffected
  since it's unchanged); confirmed this is the first thing to depend on
  `ExportSpec.func`'s exact identity as "the original, unwrapped
  callable" - not itself a bug, but noted so a future `ExportSpec`/
  `decorators.export` edit doesn't break that invariant unknowingly.

  ## Part B: serial reconnect reliability

  Found by the user simply running the (now ambient-context-using)
  example twice in a row: second run crashed with
  `frame too large: declared <garbage>`. Root cause: two of the SAME
  session's earlier real-hardware fixes (chunk 15's `push_raw_repl`
  `wait=False` no longer sending the raw-REPL exit sequence, and
  `micropython.kbd_intr(-1)` disabling Ctrl-C interception) combined to
  remove the *only* way to recover a board still running a previous
  connection's dispatch loop - a second `connect()`'s interrupt bytes
  were landing directly in the old loop's frame parser as garbage instead
  of interrupting it. Decoded the garbage length value twice during
  investigation (`\x04Tra...` the first time - literally the start of a
  crash traceback; `\r\x03\r\x01` the second time - literally
  `_enter_raw_repl`'s own interrupt+enter-raw-repl bytes) rather than
  guessing, confirming the mechanism precisely before designing a fix.

  Fixed with `reset_board()` (new, `transports/serial.py`), called before
  every raw-REPL interaction in `_connect_serial`'s `dial()`, not just the
  first. Took two more real-hardware findings to get the fix itself
  right, neither assumed:
  - An RTS-only pulse alone (matching esptool's own `HardReset` strategy,
    checked against esptool's actual source rather than guessed) does
    NOT reliably interrupt a program that's actively running right now -
    repeated real-hardware testing showed it silently failing to recover
    a stuck board most of the time. Fixed by pulsing into the ROM
    bootloader first (DTR+RTS, matching esptool's `ClassicReset`) - the
    bootloader unconditionally takes over the chip, so this always stops
    whatever was running - then a second RTS-only pulse boots normally
    out of the bootloader instead of leaving it stuck there.
  - Sending raw-REPL bytes too soon after releasing reset races the boot
    banner - bytes arriving mid-boot get silently consumed instead of
    reaching the idle REPL that's actually ready for them. Confirmed by
    watching `_enter_raw_repl` succeed once a real settle delay was added,
    fail without it, on the identical reset sequence. Fixed with a ~1s
    trailing sleep in `reset_board()` before touching the connection
    again.

  Best-effort, not hard-fail, on platforms where RTS/DTR control isn't
  available at all (`except OSError: return`) - matches esptool's own
  precedent for exactly this (`ResetStrategy.__call__`'s handling of
  `ENOTTY`/`EINVAL`) rather than turning an optional robustness step into
  a hard requirement.

  Verified end-to-end against real hardware repeatedly, not just once:
  ran `blink_and_log.py` 3-4 times back-to-back with no manual
  intervention (multiple separate test batches, all clean), and separately
  exercised `board.reconnect()` explicitly against real hardware
  (`add(2, 3)` then reconnect then `add(10, 20)`, both correct). Unit
  tests use a hand-written fake recording RTS/DTR transitions
  (`test_reset_board_pulses_into_bootloader_then_boots_normally`,
  `test_reset_board_degrades_gracefully_when_rts_is_unsupported`) with
  `time.sleep` patched out via `monkeypatch` so the suite doesn't pay
  `reset_board()`'s real ~1.3s per test run - the first use of
  `monkeypatch` in this test suite (no prior sleep here was ever big
  enough to need it).

  DESIGN.md updated in two places (dated amendments, not silent drift):
  § Call semantics for the ambient-context addition, § Wire protocol +
  the Serial row of § Transports for the `kbd_intr`/hardware-reset
  consequence.

  167 tests passing (was 157 at the end of chunk 17; +2 ambient-context
  mock tests, +1 multi-board `with` test, +2 reentrant-handler tests
  [fresh connect + immediately-after-reconnect], +1 `mcu.connect` identity
  test, +2 `reset_board` tests, +1 existing `test_decorators.py`
  assertion updated for the new wrapper-vs-original-callable distinction,
  +1 existing `push_raw_repl` wait=False test updated for the corrected
  behavior it now pins).

- [x] **19. WiFi provisioning + CLI** — done 2026-07-25. Not part of the
  original 17-chunk plan (like chunk 18) - closes the gap chunk 12 left
  open: `tether`'s wifi transport is now usable against a real board, via
  a new `tether` CLI. Design spec:
  `docs/superpowers/specs/2026-07-25-wifi-upload-design.md`.

  New `tether[cli]` extra (`click`, `beaupy`, `pyserial`) and console-script
  entry point (`[project.scripts] tether = "tether.cli:main"`), with four
  commands: `tether devices` (lists connected MicroPython-capable USB
  serial devices), `tether provision wifi --ssid ... [--password ...]`
  (uploads a `boot.py` + credentials file, prompting for a hidden password
  via `beaupy` if `--password` is omitted), `tether status` (reports
  whether a board is provisioned and currently connected), and `tether
  unprovision wifi` (removes stored credentials, gated behind a
  `beaupy.confirm()` prompt since it's destructive, unlike the others).
  `--port` is optional everywhere - `_resolve_port()` auto-discovers via a
  new `serial.list_devices()` primitive and prompts interactively via
  `beaupy.select()` when more than one device is connected.

  On-device: a new `boot.py` template (`src/tether/provisioning.py`'s
  `generate_wifi_boot()`), uploaded once by `provision wifi`, auto-runs on
  every MicroPython boot. If `/tether_wifi.json` is absent it does
  nothing - a never-provisioned board behaves exactly as it did before
  this feature existed, no separate opt-in flag needed. If present, it
  joins wifi with a bounded ~15s timeout (falling through to idle REPL on
  failure or a bad password, so serial access is never permanently locked
  out), opens a TCP listener on the wifi transport's existing
  `DEFAULT_PORT` (8765), accepts exactly one connection for the lifetime
  of that boot, and - if `/tether_app.py` exists - bridges the accepted
  socket into the existing dispatch loop via a new
  `_tether_stream_override` injection point in `generate_bootstrap()`
  (`src/tether/connection.py`). This override is the mechanism that makes
  everything else possible without touching the existing serial path:
  `generate_bootstrap()` now checks for a pre-set `_tether_stream_override`
  global before falling back to `sys.stdin`/`sys.stdout`, so a normal
  serial `connect()` (nothing ever sets the override there) gets the exact
  same generated code and stdio path as before - zero behavior change for
  existing usage.

  Three small additions to `transports/serial.py`: `list_devices()`
  (today's `discover()` still deliberately errors on ambiguity for the
  library's `serial:auto` scheme; refactored to share `list_devices()`'s
  filtering rather than duplicate it), `run_python()` (a general
  run-code-get-output primitive, generalized from `read_file`'s
  enter/exec/follow/exit sequence - used by `status` to run
  `STATUS_SCRIPT` on the board and parse its one-line JSON result), and
  `remove_file()` (used by `unprovision wifi`).

  Real finding from implementation, not anticipated by the design or plan:
  a status-check race condition, discovered only via real ESP32 hardware
  testing. `tether status` interrupts the board (a `reset_board()`
  hard-reset + raw-REPL entry, the same documented tradeoff every other
  command already accepts) to ask its live state, but the Ctrl-C from that
  interrupt was landing while `boot.py`'s own wifi-connect-wait loop was
  still mid-poll - real association+DHCP takes 2-6s, so a single-shot
  `isconnected()` check in `STATUS_SCRIPT` reliably reported "not
  currently connected" even on a healthy, correctly-provisioned board.
  Fixed across three iterative commits, each re-verified against the real
  board before moving to the next: (1) `STATUS_SCRIPT` polls
  `isconnected()` for up to 8s instead of checking once, mirroring
  `_BOOT_PY_TEMPLATE`'s own `time.ticks_add`/`time.ticks_diff`/
  `time.sleep_ms` poll pattern; (2) that poll is gated behind `if
  _provisioned:` so a never-provisioned board (no `/tether_wifi.json`,
  never called `wlan.connect()` in the first place) reports status
  instantly instead of wasting ~8s waiting for a connection that will
  never happen; (3) the loop exits early via `network.WLAN.status()`'s
  numeric state codes (ESP32-specific, not part of MicroPython's portable
  API - falls back silently to the plain `isconnected()`-only wait if
  `.status()` is unavailable) as soon as the outcome is decided, instead
  of always waiting out the full deadline.

  Real-hardware verification (ESP32, USB serial, network "Culo"):
  `provision wifi`, `status`, and `unprovision wifi` all verified
  end-to-end against the real board, including `status` correctly
  reporting "Provisioned and connected. IP: 192.168.0.197" after the race
  fix, and "Not provisioned for wifi." once `unprovision wifi` removed the
  credentials file. The plain serial `connect()` path
  (`examples/blink_and_log/blink_and_log.py`, unmodified) was re-verified
  clean after unprovisioning, confirming this feature left the existing
  serial path completely undisturbed.

  195 tests passing (was 167 at the end of chunk 18; +28: 1 in
  `test_connection.py` for the `_tether_stream_override` check, 6 new
  `list_devices`/`run_python`/`remove_file` tests in
  `test_serial_transport.py`, and 21 across the two new files
  `test_provisioning.py` and `test_cli.py` - including
  real-`micropython`-unix-port-interpreter tests for `boot.py`'s
  socket-accept/stream-bridging logic and `STATUS_SCRIPT`'s polling
  behavior).

**Addendum (2026-07-25) — `mcu.connect("wifi:<ip>")` verified against real
ESP32 firmware.** Chunk 19's own verification covered the CLI
(`provision wifi`/`status`/`unprovision wifi`) and the socket-bridge
mechanism against the real MicroPython interpreter, but not yet an actual
wifi RPC session against real hardware - this addendum closes that gap.

Verified against the same ESP32 (network "JOZEF-A-BETKA" this time - the
board was disconnected and reconnected to a different network between
sessions): uploaded a small script over serial (`mcu.connect("serial:auto")`,
one `@mcu.export` function calling back into one `@pc.export` function),
provisioned wifi, then `mcu.connect("wifi:<ip>")` from a separate PC
process. Confirmed working: a plain PC-to-MCU call (`add(3, 4) == 7`); an
MCU-to-PC reverse call from inside that same MCU handler (`@pc.export`
invoked via `await` from MCU code, itself dependent on the reverse call's
real return value flowing back correctly - not just "didn't crash");
remote-exception propagation (deliberately triggered a MicroPython-only
`NotImplementedError` - slicing a string with `[::-1]`, unsupported on
MicroPython - and confirmed it surfaced PC-side as a `RemoteError` with
the correct type and message, proving the error path works over wifi too,
not just the happy path).

Two real findings from this pass, both now documented in DESIGN.md's
Wifi transport row and README.md's "WiFi provisioning CLI" section
(not fixed - documented as known limitations, since fixing either would
be new scope beyond a verification pass):

1. **Wifi never pushes code - confirmed the hard way.** Editing the
   verification script's `@mcu.export` function after the initial serial
   upload and testing again over wifi silently ran the *old* code (the
   original, since-reverted `msg[::-1]` version) until a fresh serial
   upload was done. This was already documented as a design consequence
   in chunk 19, but this is now a directly-observed confirmation of the
   exact failure mode, not just an inferred one.
2. **`tether status` leaves the board unable to accept a new wifi
   connection afterward - not anticipated in the original design.**
   `status` hard-resets the board and interrupts `boot.py` via raw-REPL
   entry, then exits raw REPL back to the plain interactive REPL rather
   than triggering a fresh hardware reset - `boot.py` only auto-runs on
   an actual reset, not on raw-REPL exit, so the TCP listener it had
   opened is gone (an unreferenced local variable in the now-interrupted
   script) even though `status` had just reported "connected". The wifi
   *association* itself survives (confirmed: `WLAN.isconnected()` stays
   true across the interrupt, since that's firmware-level state
   independent of the interrupted Python script) - only the *listener*
   doesn't. Practically: `provision wifi` → `status` → `mcu.connect(...)`
   times out on the connect, discovered directly when the first
   verification attempt (which checked `status` first, per the CLI's own
   printed suggestion) timed out; a retry that skipped the `status` check
   worked immediately. A fix (reset again after the status query,
   mirroring `provision_wifi_command`'s existing double-reset pattern) is
   a reasonable, small follow-up - not implemented here, since this was a
   verification pass, not a fix pass.

- [x] **20. WiFi upload modes, shared-secret auth, non-interrupting status**
  — done 2026-07-27 (feature built across 7 tasks 2026-07-26; this entry
  also covers a final whole-branch review pass and its fixes, applied
  2026-07-27 before merge). Not part of the original 17-chunk plan (like
  chunks 18/19) - closes the three real gaps chunk 19's addendum
  documented: wifi never pushed code, the listener was unauthenticated,
  and `tether status` killed the very listener it just confirmed was up.
  Design spec: `docs/superpowers/specs/2026-07-25-wifi-modes-auth-design.md`.
  Plan: `docs/superpowers/plans/2026-07-26-wifi-modes-auth.md`.

  **Architecture:** `boot.py` restructured from a one-shot "accept one
  connection, bridge it" script into a loop that accepts connections
  indefinitely, one at a time (never concurrently). Every connection
  starts with a small preamble - length-prefixed JSON (`ujson`), not
  msgpack, since the vendored `umsgpack.py` may not exist yet on a board
  that's only ever been wifi-provisioned - selecting one of three modes:
  `status` (protocol version, bundle hash or `null`, free heap, uptime,
  IP - no reset, no interruption), `upload` (receives and writes the full
  `tether_runtime` bundle, chunked so no frame exceeds 64 KiB), or `run`
  (the pre-existing `exec()`-based dispatch bridge, unchanged in
  mechanism but now repeatable instead of one-shot). `mcu.connect(
  "wifi:<ip>", secret=...)` drives status → hash-compare →
  upload-if-needed → run automatically, mirroring serial's own shape.
  Auth: a shared secret, generated fresh and printed by every
  `tether provision wifi` run, checked via plain equality (an accepted,
  documented LAN-threat-model tradeoff, not an oversight) -
  `--danger-unauthenticated` opts a board out entirely.

  **Real bugs found during the final whole-branch review (2026-07-27),
  all fixed before merge - documented honestly here rather than folded
  silently into "done", matching this project's established convention
  (see chunk 15's four real-hardware bugs, chunk 19's status-race finding):**

  1. **`@mcu.loop` background tasks still duplicated on every wifi
     reconnect - the literal reason this feature was built, and it was
     still broken.** The already-merged fix (clearing
     `mcu_decorators._registrations` at the top of every generated
     bootstrap, closing the risk the design spec itself had already
     traced during planning - see its "Mode: `run`" section) addressed a
     *secondary* mechanism: harmless for plain handlers (a dict, last
     write wins) but not for `@mcu.loop` (a list `Dispatcher._loops`
     appends to). The *dominant* mechanism, found only once that fix was
     in place and reconnecting was tested for real several times in a
     row: MicroPython's `uasyncio` task queue is itself a process-global
     structure, and `asyncio.run()` returning or raising does **not**
     drain tasks queued via `asyncio.create_task()` inside it - exactly
     what `Dispatcher.run()` does for every `@mcu.loop` function. A
     previous run-mode session's loop task(s) stayed alive in that global
     queue and resumed alongside the new session's own task on the next
     `asyncio.run()` call - an accumulating, not replacing, duplicate
     every reconnect, regardless of the registry fix. Reproduced directly
     against the real interpreter: sampled `@mcu.loop` tick counts across
     3 successive reconnects came out 14/43/87 (deltas 14/29/44 - almost
     exactly 1x/2x/3x, the accumulating-duplicate-task signature). Fixed
     with one line: `_handle_run` (inside `_BOOT_PY_TEMPLATE`) now calls
     `uasyncio.new_event_loop()` in a `finally` clause, resetting the
     global task queue at the end of every run-mode session regardless of
     how it ended. Verified fixed: deltas stayed roughly constant per
     session across repeated real-interpreter runs, comfortably under a
     1.6x tolerance chosen to separate "accumulating" from "scheduling
     jitter." New regression test:
     `test_boot_py_run_mode_does_not_accumulate_loop_tasks_across_reconnects`
     - the counter is stored as an attribute on the `mcu_decorators`
       module object itself (import-cached, not re-exec'd, so it
       persists across sessions the same way `_registrations` does),
       observed via a plain `@mcu.export` getter after each session's
       sampling window.

  2. **Upload chunking was never actually implemented on the PC side** -
     `_upload()` sent one `send_bytes_frame` per file unconditionally,
     even though the whole control channel's invariant (design spec) is
     "no single frame ever needs to hold more than `MAX_FRAME_SIZE`
     bytes" and the device side already looped reading chunks per file.
     A file over 64 KiB failed with "upload chunk too large" and left a
     truncated write. Fixed by splitting each file's content into
     `MAX_CONTROL_FRAME_SIZE`-bounded slices before sending. Related
     device-side gap, promoted from a previously-deferred minor: a
     chunk's length was only checked against the absolute 64 KiB bound,
     never against how much was actually still declared-remaining for
     the file being written, so an oversized chunk got written anyway
     (`_remaining` going negative) instead of rejected - could spill into
     what should have been the next file's own frames in a multi-file
     upload. Fixed with an explicit `_chunk_len > _remaining` check.

  3. **A failed upload left a stale `.tether_hash` lying about a
     truncated bundle.** `.tether_hash` was written last on success
     (correct), but nothing invalidated the OLD one first - an upload
     failing partway (a wifi drop, flash exhaustion, #2's oversize case
     before its fix) left the device with a partially-written bundle but
     a hash sentinel still asserting the OLD bundle was intact, so the
     next `connect()` would see a hash "match" and skip re-uploading,
     then `run` mode would exec a broken file. Fixed: `_handle_upload`
     now removes `/.tether_hash` at the very start, before writing
     anything - a failed upload always leaves the sentinel absent, so a
     follow-up `status` query correctly reports `tether_app_hash: null`.

  4. **`board.reconnect()` on a still-open wifi connection deadlocked the
     device.** `_connect_wifi`'s `dial()` never closed the PREVIOUS
     run-mode connection before opening a new one - since `boot.py`'s
     accept-loop is strictly sequential, this left the device blocked
     forever reading from the stale connection, never getting back to
     `accept()` to serve the new one. Fixed: `dial()`'s closure now
     tracks the most recent run-mode `WifiStream` and closes it at the
     START of the next `dial()` call, before opening any new connection -
     `reconnect()` is now implicitly close-then-reconnect.

  5. **The run-mode preamble ack ignored `timeout=`, blocking forever.**
     `wifi_transport.connect()` switched the socket to blocking (no
     timeout) *before* the preamble's own blocking ack read - a device
     that accepted the TCP connection but never acked (busy, hung, or
     exactly #4's scenario before that fix) hung `mcu.connect(...,
     timeout=N)` forever instead of raising within ~N seconds. Fixed:
     `connect()` gained a `switch_to_blocking` parameter: `dial()` now
     keeps the connection's finite timeout active through the preamble
     exchange and only switches to blocking immediately afterward, right
     before handing the stream to the long-lived `Dispatcher`.

  6. **`WifiAuthError` wasn't exported from the `tether` package** -
     `from tether import WifiAuthError` raised `ImportError` while its
     siblings (`RemoteError`, `MCUTimeoutError`, `MCUDisconnectedError`,
     `ProtocolVersionError`) all worked, despite `connect()`'s own
     docstring telling users to expect it. Fixed by adding it to
     `tether/__init__.py`'s import and `__all__`, matching the existing
     pattern exactly.

  Also added one integration test that had no equivalent anywhere in the
  existing suite and would have caught bugs 1 and 4 far earlier: every
  prior wifi test talked to a hand-rolled fake on one side of the
  connection. `test_real_pc_connect_wifi_against_real_on_device_boot_py`
  drives the REAL public `tether.connection.connect()` against a REAL
  generated `boot.py` running under the real `micropython` interpreter -
  full interop, not two independently-faked halves - covering slice →
  status → upload → run → a real `@mcu.export` call, then
  `board.reconnect()` and a second real call.

  Documentation sweep (this same review pass): `docs/DESIGN.md` (§
  Architecture overview step 5, § Transports' Wifi row, § Disconnection's
  wifi caveat), `CLAUDE.md` (the "wifi never pushes code" constraint
  bullet), and `README.md` (Transports table, "WiFi provisioning CLI"
  section, `Status`) all updated to reflect what's actually true now,
  with dated correction notes rather than silent rewrites, per this
  project's own established convention for amending previously-locked
  claims that real implementation/review has since overtaken.

  Strict TDD throughout the fix wave: every code fix above has a
  dedicated regression test confirmed RED (failing for the diagnosed
  reason, against the pre-fix code) before the fix, then GREEN after.
  238 tests passing (was 230 immediately before this review's fixes; +8:
  one for each of bugs 1/2(PC-side chunking)/2(device-side guard)/3/4/5/6
  above, plus the new real-client-vs-real-device integration test).

- [x] **21. BLE upload modes, shared-secret auth, non-interrupting status**
  — done 2026-07-28 (feature built across 5 tasks 2026-07-27/28 via
  parallel-round subagent-driven development, mirroring chunk 20's own
  execution model; this entry also covers a final whole-branch review pass
  and its fixes, plus real-hardware verification, both 2026-07-28 before
  merge). Full parity with wifi's chunk 20: same three modes
  (`status`/`upload`/`run`), same shared-secret auth model
  (`--danger-unauthenticated`), same hash-check-skip-upload semantics —
  built from scratch, since BLE had no on-device peripheral at all before
  this (`transports/ble.py` was PC-side-only, chunk 13). Design spec:
  `docs/superpowers/specs/2026-07-27-ble-modes-auth-design.md`. Plan:
  `docs/superpowers/plans/2026-07-27-ble-modes-auth.md`.

  **Architecture:** built on MicroPython's built-in `bluetooth` module (no
  `aioble` vendoring — confirmed absent from the generic ESP32 firmware
  this project verifies against, unlike the built-in module). `boot.py`
  advertises a fixed GATT service, accepts one central at a time, and
  **reuses a single connection across any number of `status`/`upload`
  preambles** — a deliberate divergence from wifi's always-separate-
  connections model, since BLE connection setup (advertising discovery,
  link establishment, MTU negotiation) is meaningfully more expensive than
  a TCP handshake; only `run`, an auth failure, or an unrecognized mode
  ends the session. `_handle_status`/`_handle_upload`/`_handle_run` are
  wifi's own shared mode-handler logic (extracted into
  `_MODE_HANDLER_FUNCTIONS_SRC` as a preliminary refactor task, verified
  byte-identical-output on wifi's side before BLE ever used it), reused
  via a `.recv(n)`/`.send(data)` adapter matching a real socket's
  contract; `_handle_run` alone needs a BLE-specific async stream adapter,
  since `uasyncio.StreamReader/Writer` require a real socket at the native
  level, which BLE fundamentally isn't. The on-device session loop itself
  is **synchronous**, not async (a deliberate deviation from the plan's
  own shown design, caught and independently reproduced during task-level
  review: the plan's originally-specified async outer loop segfaults
  MicroPython outright — nesting it under `uasyncio.run()` nests
  `_handle_run`'s own `asyncio.run()`/`new_event_loop()` inside an outer
  event loop and corrupts MicroPython's process-global uasyncio task
  queue, exit 139, reproduced directly against the real interpreter).

  **Real bugs found during task-level review (fixed before merge to
  trunk, same task):** an `_end_session()` TOCTOU could brick the board
  (a scheduler-delivered disconnect IRQ between a None-check and a
  `gap_disconnect()` call could pass `None`, raising an uncaught
  `TypeError` that escaped the outer loop); `gatts_set_buffer` needed
  `append=True` to avoid a real write-buffer race on bursty multi-chunk
  writes.

  **Real bugs found during the final whole-branch review (2026-07-28),
  all fixed before merge — documented honestly here rather than folded
  silently into "done", matching this project's established convention
  (see chunk 15's four real-hardware bugs, chunk 19's status-race finding,
  chunk 20's own two-bug headline):**

  1. **No read timeout anywhere in the BLE control exchange** — reintroduced
     the exact class of hang wifi's own chunk-20 fix (`0cb1c85`) had
     already closed. `BleStream.read()` was an unbounded `queue.get()`;
     a device connected but silent (e.g. right after a fresh, not-yet-
     serviced provisioning) would hang `mcu.connect()`/`tether status`
     forever with no error. Found by a reviewer tracing the PC-side and
     on-device protocols side by side, not by any test — BLE has no real
     local-peripheral testing path at all (unlike wifi, which at least
     had real sockets to test against), so no test could ever have caught
     this. Fixed by threading a timeout through `BleStream`/
     `BleControlChannel`/`_connect_ble`/`status_command`.
  2. **`provision ble` killed the boot.py it had just installed.** Its
     MAC-readback step (a raw-REPL round trip) Ctrl-C'd the freshly
     started BLE session loop, and nothing reset the board again
     afterward (unlike `provision wifi`, where the final reset is always
     last) — every subsequent connect attempt hung against a GATT server
     with no Python servicing it, until a physical power cycle. Fixed by
     reordering: read the MAC before the final reset, not after.

  Also fixed in the same wave: the auth-failure/unknown-mode nack could
  be lost (an immediate `gap_disconnect()` can tear the link down before
  a queued `gatts_notify` transmits — no completion event on notifications,
  unlike wifi's TCP send buffer flushing before close); the sibling
  `_end_session()` TOCTOU from the task-level review recurred at a second
  call site and was hardened the same way; a bounded retry was added
  around device-side `gatts_notify` bursts (real NimBLE can raise
  `OSError`/ENOMEM under burst — invisible to every test, since the fake
  `gatts_notify` cannot fail); stale docstrings in `transports/ble.py`
  still asserting "BLE never pushes code" were corrected.

  **Real-hardware verification (2026-07-28):** `tether provision ble`,
  `tether status --ble-addr`, and a real `mcu.connect("ble:<addr>")`
  session — upload+run with no prior serial upload, script-edit
  propagation, `board.reconnect()` three times in a row with **no
  physical reset**, confirming `@mcu.loop` tasks don't accumulate
  (deltas 14/16/15, flat — the same check that caught chunk 20's own
  headline bug, now passing for BLE too), auth rejection (wrong and
  missing secret), `--danger-unauthenticated`, and the wifi/BLE
  boot.py-conflict warning (both directions) — were all run against a
  real ESP32. Serial (`examples/blink_and_log/blink_and_log.py`) confirmed
  completely unaffected throughout. One real, non-blocking finding
  surfaced only by hardware, not fixed in this chunk since it's a macOS
  platform behavior, not a code defect: macOS's CoreBluetooth hides real
  BLE MAC addresses from apps for privacy, exposing a randomized per-app
  UUID instead — `mcu.connect("ble:<addr>")` on macOS needs that UUID
  (from a scan), not the MAC `provision ble` prints (correct and usable
  as-is on Linux/BlueZ). A second finding (the advertised device name
  showing MicroPython's own default GATT device name, "MPY ESP32",
  instead of this project's "tether" in a scan) was root-caused and fixed
  the same day — see the 2026-07-28 addendum below.

  Documentation sweep (this same review pass): `docs/DESIGN.md` (§
  Architecture overview step 5, § Transports' BLE row), `CLAUDE.md` (the
  "BLE never pushes code" constraint bullet), and `README.md` (Transports
  table, new "BLE" subsection under provisioning, `Status`) all updated
  to reflect what's actually true now, with dated correction notes rather
  than silent rewrites, matching chunk 20's own convention.

  278 tests passing (was 268 immediately before the final review's fixes;
  +10, one per finding above).

**Addendum (2026-07-28) — follow-up fixes from a post-merge gap review.**
Reviewing chunk 20/21's own accepted limitations and parked findings
surfaced four worth acting on immediately rather than leaving parked:

1. **`unprovision-wifi`/`unprovision-ble` asymmetry removed.** A board
   only ever runs one transport's `boot.py` at a time (the mutual-
   exclusivity design chunk 21 already established), so there was no
   real reason to unprovision one transport but not the other. `tether
   unprovision-wifi`/`unprovision-ble` collapse into a single `tether
   unprovision`, which removes whichever of `/tether_wifi.json`/
   `/tether_ble.json` are actually present (checked via `read_file`
   first, so the confirmation prompt and final message are accurate
   about what's really being removed - not a blind "removed everything"
   claim).
2. **`BleStream.write()` gains the same timeout `read()` already had.**
   The final review's F1 fix (chunk 21) bounded reads; writes were left
   with `Future.result()`'s default unbounded wait - a peripheral that
   stops acknowledging ATT writes mid-exchange could still hang the
   whole process forever, on the write side specifically. Same fix
   shape: `concurrent.futures.TimeoutError` caught and converted to
   `OSError`, matching the read-side contract; `BleControlChannel`
   threads its existing `timeout` through both directions now.
3. **BLE gets its own `@mcu.loop`-duplication-across-reconnects
   regression test**, mirroring wifi's own headline test
   (`test_boot_py_run_mode_does_not_accumulate_loop_tasks_across_reconnects`).
   This was a real, named gap in chunk 21's final review (verified once,
   manually, on real hardware, but nothing automated). Reconnects three
   times via the existing `Central` test helper, samples
   `get_tick_total()` via a real RPC call after each session (not just a
   raw counter comparison - a stale reader task racing the live session
   for frames, the specific hazard the final review flagged as
   BLE-specific, would show up as a hung/wrong RPC result here, not just
   an inflated count). Mutation-verified: reverting the underlying fix
   reproduces the exact 1x/2x/3x accumulating signature (15/46/93,
   deltas 15/31/47) chunk 20's own wifi bug had.
4. **The advertised-BLE-name discrepancy (chunk 21's parked finding) was
   root-caused, not just left as a shrug.** Confirmed directly against
   real hardware: MicroPython's `bluetooth` module has its own built-in
   default `gap_name` ("MPY ESP32"), entirely independent of this
   project's custom advertising payload's local name ("tether") -
   nothing in `tether`'s code had ever set it. Fixed with one call,
   `_ble.config(gap_name="tether")`, right after `_ble.active(True)`.
   Verified fixed at the source (reading `gap_name` from inside the
   actual running boot.py setup code, not a fresh disconnected session,
   now returns `b'tether'`). One residual, non-fixable-from-here
   artifact found during verification: a bleak scan on this dev machine
   still occasionally reports "MPY ESP32" for a device it has scanned
   many times before - traced to CoreBluetooth not even exposing the
   standard Generic Access service via `client.services` (a documented
   Apple platform behavior; the OS manages device-name display
   internally and can lag a real device's current `gap_name` behind its
   own cache) - not something more code on tether's side can control.

  4 new/changed test files, 283 tests passing (was 278).

5. **Shared-secret auth upgraded to an HMAC-SHA256 nonce-challenge**, so
   the plaintext secret itself no longer crosses the wire (chunk 20/21's
   own accepted limitation, and this gap review's recommended fix over a
   full TLS stack, which doesn't fit the bundle/complexity budget on this
   hardware class). The device sends a fresh `os.urandom(16)` nonce as the
   very first thing on every accepted connection, before reading anything;
   the client answers with `HMAC-SHA256(secret, nonce)` instead of the
   secret itself. MicroPython has `hashlib.sha256` but no `hmac` module,
   so the device runs a small hand-rolled HMAC-SHA256 (`_hmac_sha256` in
   the shared `_MODE_HANDLER_FUNCTIONS_SRC`) - verified byte-for-byte
   against CPython's real `hmac.new(key, msg, hashlib.sha256)` both
   locally and on real ESP32 hardware; the PC side (`transports/wifi.py`'s
   `send_preamble`, `transports/ble.py`'s `BleControlChannel.send_preamble`)
   uses real stdlib `hmac`. BLE's one-connection-reused-across-modes model
   (chunk 21) needed its own state: only the FIRST preamble on a physical
   connection presents a nonce response (`BleControlChannel._authenticated`
   PC-side, `_authenticated` device-side); later preambles on that same
   already-open connection skip the check, matching the shape the
   one-connection design already established.

   **A real, non-obvious bug found and fixed via this chunk's own testing,
   worth recording:** the hand-rolled HMAC's key-padding literal
   (`b"\x00"`) sat inside `_MODE_HANDLER_FUNCTIONS_SRC`, itself an f-string
   at THIS module's import time - a single backslash there gets eagerly
   interpreted by CPython immediately, baking a raw embedded NUL byte into
   the generated `boot.py`'s own source text (not the two-character escape
   sequence). MicroPython's parser turned out to mis-tokenize a literal
   NUL byte sitting inside a byte-string literal in source (confirmed via
   direct reproduction under the real `micropython` unix-port: the parsed
   byte value came out as `1`, not `0`) - producing a wrong HMAC on every
   single connection, both wifi and BLE, authenticated or not. The
   isolated hand-rolled algorithm itself was correct throughout (verified
   against CPython's `hmac` before this was even wired into the accept
   loop) - the bug was purely in how the literal round-tripped through a
   second layer of Python-source-generating-Python-source, and needed a
   real embedded-context reproduction (not an isolated unit test of the
   function alone) to surface. Fixed with `b"\\x00"` (double backslash) in
   the f-string, so the *generated* file's source text carries the literal
   escape sequence for MicroPython's own parser to interpret. A few
   existing test fakes that fully replace the on-device `uos` module
   (predating this feature) also needed a `urandom()` method added, since
   the accept loop now calls it once per connection.

   Verified against real ESP32 hardware: wifi round-trip (correct secret
   connects and reconnects; wrong secret raises `WifiAuthError`) confirmed
   working end-to-end. 3 changed test files plus `tests/ble_fakes.py` and
   `tests/test_connection.py`'s hand-rolled fake wifi/BLE devices (which
   needed to start sending a nonce before reading a preamble, matching the
   new protocol), 284 tests passing (was 283).

**Addendum (2026-07-28) — wifi/BLE runnable examples, and a real BLE bug
they surfaced.**

Added `examples/wifi_blink/` and `examples/ble_blink/`, wifi/BLE
equivalents of `examples/blink_and_log/` - same `@mcu.export`/`@pc.export`
code, only `mcu.connect()`'s address/credential source differs. Both use
env vars (`TETHER_WIFI_SECRET`/`TETHER_BLE_SECRET`) for the secret, matching
the CLI's own convention, and demonstrate `board.reconnect()` with no
physical reset - the headline reason to provision wifi/BLE over plain
serial in the first place. `tests/test_examples.py` was refactored to
parametrize its existing checks (parses, slices to just the shared
mcu-bound code, generates valid bootstrap, runs correctly under the real
`micropython` interpreter with a faked `Pin`) across all three examples,
plus one new check confirming each example's driver block actually
connects with the right transport scheme and demonstrates reconnect.

Running `examples/ble_blink/` against real ESP32 hardware surfaced a real,
100%-reproducible bug in the HMAC nonce-challenge (addendum item 5 above),
previously merged and shipped: the device sends its one-per-connection
nonce the instant `_IRQ_CENTRAL_CONNECT` fires, but `BleakClient.
start_notify()`'s CCCD subscription write happens strictly *after*
`client.connect()` returns on the PC side - a notify sent before that
subscription completes is silently dropped by the BLE stack, with no
delivery-confirmation mechanism available to the peripheral to detect it.
Confirmed via an isolated bare-bleak reproduction (connect, subscribe to
notifications, wait 5s): zero notifications received, every time, not an
occasional flake - `nc`/`ping`-level reachability checks were actively
misleading during diagnosis, since TCP's listen backlog and ICMP echo both
work independently of whether the actual application (a dead/interrupted
`boot.py`, in wifi's case, or a peripheral mid-race, in BLE's) is
listening. Fixed by resending the nonce on a timer (`_NONCE_RESEND_MS` =
300ms) until the central's first preamble actually lands, bounded by
`_NONCE_MAX_WAIT_MS` (5s) so a central that connects but never subscribes
doesn't wedge the session forever - safe against duplicate delivery, since
a not-yet-subscribed central's earlier notify was truly dropped rather
than queued for later delivery. New regression test
(`test_ble_boot_resends_the_nonce_while_waiting_for_the_first_preamble`)
verifies the resend mechanism directly, since the fake BLE peripheral
can't model "silently dropped" (its `gatts_notify()` always records the
send) - checks that a central slow to respond sees multiple identical
nonce notifications, not just one. Mutation-verified: reverting to a
single one-shot send drops the notification count from ≥2 back to 1,
correctly failing the test.

Also root-caused, in passing, a separate source of "wifi seems flaky"
confusion hit while debugging the above: any raw-REPL-based diagnostic
(`tether status` without `--ip`, or hand-rolled Ctrl-C-based scripts) that
interrupts a board running its wifi/BLE accept-loop kills that loop
outright - it does not auto-resume, and nothing before this addendum
documented that plainly. `nc -zv`/`ping` continuing to "succeed"
afterward is the trap: they only prove OS/TCP/ICMP-level reachability,
not that the on-device Python program is still alive to service the
tether protocol. Not itself a code change (this is inherent to
MicroPython's single-process model, matching DESIGN.md's existing
"sequential-only" limitation) - recorded here since it cost real
debugging time and will again for the next person who hits it. `tether
status --ip <ip>`/`--ble-addr <addr>` (talks to the listener directly,
never touches serial) is the way to check status without this risk.

  3 changed files (`provisioning.py`, `tests/test_examples.py`,
  `tests/test_provisioning.py`) plus 2 new example files, 295 tests
  passing (was 284).

**Addendum (2026-07-28) — actually fixed `tether status`'s
listener-killing side effect, not just documented it.**

The previous addendum's "wifi seems flaky" finding was left as a
documented trap, not a fix. Fixed properly: `status_command`'s raw-REPL
fallback (used whenever `--ip`/`--ble-addr` aren't given, or fail) now
resets the board a SECOND time after running `STATUS_SCRIPT`, restoring
whatever was running before the diagnostic interrupted it - most
importantly, a wifi/BLE `boot.py`'s accept-loop, which does not resume on
its own once Ctrl-C'd. Wrapped in `try`/`finally` so this happens even if
the diagnostic itself raises. `tether unprovision`'s cancel path (`beaupy.
confirm` returns `False`) had the identical bug for a different reason -
its own leading `reset_board()` (needed to read `/tether_wifi.json`/
`/tether_ble.json` to decide what to prompt about) already interrupted a
live listener before the user even answered "no" - fixed the same way:
reset again before reporting "Cancelled." so declining to unprovision is
a genuine no-op.

Verified against real ESP32 hardware: a wifi listener now survives a bare
`tether status` call, and several in a row, with no manual recovery
needed - confirmed by running `examples/wifi_blink/` immediately
afterward each time. One real, expected (not a bug) side effect found
during that verification: a connection attempted *immediately* after
`tether status` returns can hit a brief window (observed: several
seconds) where the board is still rejoining wifi from the reset, same as
right after any boot - documented in `status_command`'s own docstring
rather than papered over with a fixed sleep, since `mcu.connect()`'s own
timeout already handles this correctly.

  1 changed file (`cli.py`) plus 2 updated tests, 295 tests passing (no
  new tests needed - the existing exact-call-sequence assertions on
  `reset_board()` already catch a regression here once updated for the
  new sequence).

**Addendum (2026-07-28) — real `ty` typing fixes (not suppression), a
BLE-example DX fix, and `provision ble` auto-detecting the macOS connect
address.**

1. **Decorator typing was genuinely dishonest, not just unhelpful to
   static analysis.** `@mcu.export`'s declared return type
   (`Callable[[F], F]`) claimed the decorated function keeps its exact
   original signature; at runtime it returns a completely different
   `dispatch(*args, **kwargs)` wrapper that dispatches through the
   ambient board. Running Astral's `ty` against `examples/` surfaced this
   concretely: 26 diagnostics, several of them real consequences of the
   lie (`blink(5)` flagged as "not awaitable" and "too many positional
   arguments" because `ty` believed `blink` was still the original
   `async def blink(times: int) -> None`). Fixed properly:
   - `@mcu.export`'s return type split into a real `@typing.overload`
     pair - bare `@mcu.export` (the common form) genuinely returns
     `Callable[..., Any]` directly; `@mcu.export(timeout=...)` returns a
     decorator (`Callable[[F], Callable[..., Any]]`). A single
     non-overloaded signature can't express "return type depends on
     whether an argument is None," which is exactly what this decorator
     does.
   - The inner `dispatch` function is now returned via
     `typing.cast("Callable[..., Any]", dispatch)`, not a bare `return
     dispatch` - `@functools.wraps(fn)` has special-cased typeshed
     handling that made static analysis infer `dispatch`'s type from
     `fn` regardless of the enclosing function's own declared return
     type; the cast is what actually overrides it at the point that
     matters.
   - `mcu.connect` was a complete blind spot for static analysis - it's
     assigned as a plain instance attribute in `__init__.py` (not a
     class method - avoids a circular import with `connection.py`,
     which imports FROM `decorators.py`), invisible to any type checker.
     Added a `TYPE_CHECKING`-only `connect: Callable[..., Any]`
     declaration on `_McuNamespace` - a bare annotation, not an
     assignment, since assigning the actual function's value there made
     static analysis treat it as a bound method (wrongly self-binding
     the first argument) instead of the plain unbound-function-on-an-
     instance it actually is.

   Result: `examples/` diagnostics dropped from 26 to 6, verified via
   `ty check examples/` before/after. The remaining 6 (unresolved
   `machine`/`time.sleep_ms` imports, and `await`-ing a `@pc.export`
   function from MCU-side code) are structurally different: they depend
   on the slicer's compile-time rewrite of the sliced bundle, which no
   static analysis of the *original* file can ever correctly model -
   exactly the "future language server" territory CLAUDE.md/DESIGN.md
   already flag as out of scope, not something a decorator-signature fix
   can reach.

2. **`examples/ble_blink/` now gives a helpful hint instead of a raw
   traceback for the single most common first-run mistake on macOS**:
   passing the MAC `tether provision ble` printed, which CoreBluetooth
   never exposes to apps. Caught by exception class *name* (not import,
   since `bleak` may not be importable) around `mcu.connect()`.

3. **`tether provision ble` now auto-detects and prints the address this
   machine actually needs**, closing the gap the DX fix above works
   around. A real gap found running `examples/ble_blink/` against real
   hardware and having to hand-write a throwaway `bleak.BleakScanner`
   script to translate the MAC into the macOS-local UUID before it would
   connect - a bad first-run experience for exactly the audience most
   likely to hit it. `_find_mac_local_ble_address()` does a best-effort,
   bounded (8s) `bleak` scan for the just-provisioned board by its
   advertised name after the final reset, and prints the found address
   only when it differs from the MAC already printed (silent, zero-noise
   no-op if `bleak` isn't installed, the scan times out, or - on Linux/
   BlueZ - it finds the same MAC already shown). Never raises; the
   existing MAC + macOS-note output remains the correct fallback either
   way.

   Verified against real ESP32 hardware: `tether provision ble` printed
   `4B05B0A9-D2BF-F915-EEFD-EF8975838091` (this Mac's actual CoreBluetooth
   UUID for the board) unprompted, alongside the existing MAC - confirmed
   directly connectable with `examples/ble_blink/` using exactly that
   printed value, no manual scan needed.

  4 changed files (`decorators.py`, `cli.py`, `ble_blink.py`, plus new
  tests in `test_cli.py`), 299 tests passing (was 295).

**Addendum (2026-07-28) — PyPI package renamed to `tether-mcu`.**

`tether` was already taken on PyPI. Renamed the *distribution* name only
(`pyproject.toml`'s `[project] name`) to `tether-mcu` - the importable
package stays `tether/` (`import tether`, `from tether import mcu, pc`
unchanged), and the installed console script stays `tether`
(`[project.scripts]` untouched), matching the common pattern of a
PyPI-distribution name differing from its import name (e.g.
`beautifulsoup4` distributes `bs4`). Verified by building the wheel
(`uv build`) and confirming its contents are still the unmodified
`tether/` package tree - only the outer distribution metadata name
differs. `uv.lock` regenerated to match (`uv lock`). No other file
references the distribution name (checked - the README's own install
instructions all use local editable installs, `uv pip install -e
".[serial]"`, unaffected either way; `publish.yml` reads the name from
`pyproject.toml`, nothing hardcoded).

  2 changed files (`pyproject.toml`, `uv.lock`), no test/behavior changes
  - 299 tests passing, unchanged.

- [x] **22. Wifi per-frame authentication** — done 2026-08-05
  Closes the gap where only the wifi handshake itself was authenticated -
  every frame after it rode the wire in the clear. Adds a generic,
  transport-agnostic envelope codec (`tether.marshalling.frame_auth.
  FrameAuthenticator`) wrapping every frame post-handshake in
  `[4-byte outer-length][4-byte counter][inner frame][16-byte truncated
  HMAC-SHA256 tag]`, session key reused from the handshake's own HMAC
  output (no extra round trip), per-direction replay-protected counters,
  hard-close-no-retry on any verification failure. Applies uniformly
  across `status`/`upload`/`run` on the PC side (`wifi.py`, `connection.py`)
  and the device side (new helpers in `provisioning.py`'s generated
  boot.py, plus async `_AuthenticatedReader`/`_AuthenticatedWriter` wrapper
  classes for `run` mode's uasyncio-based stream). Skipped entirely under
  `--danger-unauthenticated`. Deliberately wifi-only - BLE explicitly out
  of scope, tracked as a follow-up (the codec's configurable tag length
  exists specifically for BLE's tighter MTU budget when that follow-up
  lands). Breaking change: boards provisioned before this chunk need
  `tether provision wifi` re-run - no negotiation/fallback was added. See
  `docs/DESIGN.md`'s 2026-08-04 "Per-frame authentication" amendment for
  the full design rationale and the grill-me interview that produced it.

  **Remaining, accepted limitation:** this chunk has NOT been verified
  against real ESP32 hardware (unlike chunks 19-21, which explicitly were)
  - only against the `micropython` unix-port subprocess via
  `tests/mpy_runner.py`. Flagging this explicitly rather than silently
  treating subprocess-level verification as equivalent - real-hardware
  verification (timing under the async wrapper classes' extra buffering,
  real TCP behavior under a tampered/dropped connection) should happen
  before this is considered as solid as the modes it's wrapping.

  9 changed files (`errors.py`, new `marshalling/frame_auth.py`,
  `transports/wifi.py`, `connection.py`, `provisioning.py`, plus tests in
  `test_frame_auth.py` (new), `test_transport_wifi.py`, `test_connection.py`,
  `test_provisioning.py`), 323 tests passing (was 299).

---

## Explicitly out of scope for these chunks (see DESIGN.md § Non-goals)

Classic BT, firmware auto-flashing, ISR-driven heartbeats, cross-version
protocol negotiation, pluggable custom-type serializers, VS Code language
server (future project, not part of `tether` core).
