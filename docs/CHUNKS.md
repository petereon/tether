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

- [ ] **4. AST slicer — reverse stub generation**
  `slicer/` — for every `@pc.export` function, generate a MicroPython proxy
  stub (same name/signature) whose body sends an RPC frame and awaits the
  reply (DESIGN.md step 2). Depends on: 1, 3.

- [ ] **5. MCU runtime — umsgpack**
  `tether_runtime/umsgpack.py` — vendor a real MicroPython-compatible
  msgpack port, wire-compatible with chunk 2's encode/decode. Depends on: 2.

- [ ] **6. MCU runtime — dispatch loop**
  `tether_runtime/dispatch.py` — `uasyncio`-based reentrant dispatch loop:
  request-ID tagged frames, `@mcu.loop` periodic task scheduling, heartbeat
  emission on natural `await` yield points. Depends on: 5.

- [ ] **7. PC-side dispatch**
  `dispatch/` — background reader thread + queue; blocking calls filter by
  request-ID while pumping other in-flight requests to a thread pool
  (reentrant/reverse-call support); per-call timeout + heartbeat-driven
  idle-reset; wraps remote exceptions as `RemoteError`. Depends on: 2.

- [ ] **8. Mock transport**
  `transports/mock.py` — in-process fake MCU: runs the real sliced code path
  (chunks 3, 4, 6) against a second thread, no hardware required. This
  unblocks hardware-free testing for everything downstream. Depends on:
  3, 4, 6, 7.

- [ ] **9. Serial transport**
  `transports/serial.py` — USB VID/PID auto-discovery (`"serial:auto"`),
  raw-REPL code push, `pyserial`-backed byte stream. Depends on: 7.

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
