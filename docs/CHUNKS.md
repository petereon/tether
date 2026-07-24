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
