# BLE upload, auth, and status — full parity with wifi (design)

Status: approved (via brainstorming interview), not yet implemented.
Date: 2026-07-27.

## Problem

The wifi transport just gained three connection modes (`run`/`upload`/`status`), shared-secret auth, and hash-check-skip-upload (`docs/superpowers/specs/2026-07-25-wifi-modes-auth-design.md`). BLE was explicitly out of scope for that work and remains, today, in the state wifi was in *before* that design: `transports/ble.py` is a PC-side-only `bleak` client (chunk 13) that can dial an already-running on-device runtime, but **no on-device BLE peripheral exists at all** — no advertising, no GATT server, no listener of any kind. `tether` cannot push code over BLE, cannot check status over BLE, and there is no `provision-ble` step to even make a board reachable over BLE in the first place.

This design builds BLE's on-device half from scratch and gives it full behavioral parity with wifi's mode/auth/hash-check model, adapted to BLE's connection-oriented GATT transport instead of a raw TCP socket.

## Scope decision

Full parity with wifi: same three modes (`run`/`upload`/`status`), same shared-secret auth model (with `--danger-unauthenticated`), same hash-check-skip-upload semantics, same "loop forever, one connection at a time, no reset needed to reconnect" shape. The parts that differ are strictly the ones BLE's transport model forces to differ: framing (MTU-chunked GATT writes/notifications instead of a raw socket stream), provisioning UX (no network credentials to configure — provisioning still exists as an explicit step, but only to write the shared secret and put boot.py into BLE-peripheral mode), and address discovery (the board's own BLE MAC, printed at provision time, instead of a DHCP-assigned IP).

Wifi and BLE are independent: separate secret, separate provisioning command, separate on-device config file. Provisioning one does not touch or require the other.

## On-device architecture

Built on MicroPython's built-in `bluetooth` module (confirmed present on the real ESP32 hardware this project verifies against; `aioble` is not, and vendoring it was rejected — see "Alternatives considered"). This module is event/IRQ-callback based, not `asyncio`-based like wifi's socket API, so a small bridge is needed: the IRQ handler (registered via `BLE().irq(...)`) does nothing but push `(event, data)` tuples onto a plain list/queue; the actual mode-handling code (`_handle_run`/`_handle_upload`/`_handle_status`, reused near-verbatim from wifi's `boot.py`) stays written in the same synchronous, blocking-read style as today, pulling off that queue instead of calling `socket.recv()`.

New on-device shape, mirroring wifi's `boot.py` loop exactly:

1. Bring up the BLE radio, register tether's GATT service (`SERVICE_UUID`/`WRITE_CHAR_UUID`/`NOTIFY_CHAR_UUID` — already fixed by the PC-side `ble.py`, reused unchanged so the two sides agree on what to look for).
2. Start advertising.
3. `while True:` — wait for a central to connect (blocking read off the IRQ queue) → read one preamble frame off the write characteristic (same length-prefixed JSON shape as wifi: `{"mode": ..., "secret": ...}`) → check secret → branch on mode → handle to completion → on disconnect, resume advertising → loop back to step 3.

Sequential only, same as wifi: one central at a time, matching both the wifi design's decision and ESP32 BLE peripherals' own practical limit (`ble.py`'s PC-side docstring already notes this). No same-connection mode pivot — `upload` and `run` are always separate connections, exactly like wifi.

## Protocol reuse

The preamble, the three modes' semantics, and hash-check-skip-upload are identical to wifi's design — see that spec's "Connection preamble," "Mode: `run`," "Mode: `upload`," and "Mode: `status`" sections; nothing about the mode logic itself changes for BLE. The only thing that changes is framing:

- **Wifi:** `[4-byte length][body]` written directly to a raw socket, one write per frame.
- **BLE:** the same `[4-byte length][body]` logical frame, but the write characteristic's payload is capped by the negotiated ATT MTU, so each frame gets split into multiple GATT writes on the way out (mirroring `BleStream.write()`'s existing PC-side chunking) and reassembled on-device before being handed to the same frame-parsing code wifi already uses. Responses/notifications flow the same way in reverse, via `gatts_notify()` chunked to MTU, mirroring how the PC side's `on_notify` callback already feeds a reassembly-free read queue (reassembly happens above this layer, in the length-prefix frame reader — same as wifi).

This means `_handle_run`, `_handle_upload`, and `_handle_status` (from wifi's `boot.py`) are reused with only their I/O primitives swapped — `_recv_exact`/`_send_json_frame` get a BLE-backed implementation instead of a socket-backed one, not a rewrite of the mode logic itself.

`mcu_decorators._registrations.clear()` (wifi's fix for `@mcu.loop` duplication across `exec()`s within one boot cycle) and the `asyncio.new_event_loop()` fix (for the process-global uasyncio task queue) both apply identically here — `run` mode's `exec()`-based bridge mechanism is unchanged, transport-agnostic, and already fixed. No new duplication risk is introduced by BLE.

## Provisioning CLI

New `tether provision-ble` command, serial-only, mirroring `provision-wifi`'s shape:

- Generates a random secret (`secrets.token_hex(16)`) and writes it, along with a BLE-mode marker, into a new `/tether_ble.json` (separate file from wifi's `/tether_wifi.json` — independent lifecycles, independent rotation).
- Writes the BLE boot.py template, resets the board.
- Reads back the board's own BLE MAC address (`bluetooth.BLE().config('mac')`) over the same serial session used to provision it, and prints it alongside the secret — the same "prints what you need to connect" UX wifi's `provision-wifi` already has for the IP.
- `--danger-unauthenticated`: same semantics as wifi's flag — no secret generated or stored, loud warning printed, no interactive confirmation block (scriptable).

`mcu.connect("ble:<addr>", secret=...)` gains the same `secret` kwarg wifi's `connect()` has, falling back to a `TETHER_BLE_SECRET` environment variable (separate from `TETHER_WIFI_SECRET`) if omitted. Same "no PC-side persistent secret store" decision as wifi, for the same reasons.

`tether status` gains a `--ble-addr` option mirroring `--ip`: tries a non-destructive `status`-mode BLE connection first, falls back to the existing raw-REPL serial diagnostic only if the BLE connection itself can't be made (mirrors wifi's two-tier status exactly).

**Explicitly decided against:** BLE scanning/discovery in `tether devices`. `provision-ble` printing the address is the only address-discovery mechanism this design adds. If rediscovering a previously-provisioned board's address (without re-provisioning) turns out to be a real pain point in practice, that's a follow-up, not part of this scope.

## PC-side changes

- `errors.py`: `WifiAuthError` gets reused as-is for BLE auth failures too — same failure semantics (bad/missing shared secret), not worth a BLE-specific exception class for an identical failure mode. The name stays `WifiAuthError` even though it now also covers BLE; renaming it is a bigger, unrelated churn (every existing wifi call site, test, and doc reference) for a cosmetic gain, not part of this design's scope.
- `transports/ble.py`: gains the same length-prefixed-JSON control-channel helpers wifi's `transports/wifi.py` has (send preamble, read a JSON response, send/receive raw byte frames for `upload`'s file content), adapted to write via `client.write_gatt_char`/read via the existing notify-backed queue instead of a raw socket. `BleStream` itself is unchanged — these are used by `_connect_ble`'s `dial()` before a `BleStream` becomes the live `run`-mode connection, same relationship wifi's control-channel helpers have to `WifiStream`.
- `connection.py`'s `_connect_ble` currently mirrors the *old*, pre-upload wifi shape (dial-and-handshake an already-running runtime, no slicing/upload at all). This gets rewritten to mirror the *current* `_connect_wifi`: slice the calling script → open a `status`-mode BLE connection to read the device's current hash → compare → open an `upload`-mode connection if different → open the `run`-mode connection that becomes the live `BoardHandle`. Same `_gather_runtime_bundle` helper wifi's `_connect_wifi` already uses, reused as-is (transport-agnostic).
- `connect()`'s `ble:<addr>` branch starts slicing/bundling like the wifi branch does, instead of skipping straight to a bare handshake.

## Testing approach

Same rigor and same structure as the wifi plan:

- On-device logic (preamble parsing, mode dispatch, hash-check, the IRQ-to-queue bridge) driven through the real `micropython` unix-port interpreter, with a hand-rolled fake `bluetooth` module standing in for the real IRQ/GATT API — mirroring exactly how wifi's tests fake `network.WLAN`. The fake needs to support: registering a services table, advertising start/stop, delivering fake `_IRQ_CENTRAL_CONNECT`/`_IRQ_GATTS_WRITE`/`_IRQ_CENTRAL_DISCONNECT`/`_IRQ_MTU_EXCHANGED` events, and `gatts_notify`.
- `click.testing.CliRunner` for `provision-ble`/`status --ble-addr` CLI-level changes.
- Real BLE peripheral behavior cannot be tested in CI — same limitation `ble.py`'s own module docstring already documents for the PC-central side (`bleak` is client-only; there is no way to run a real local BLE peripheral to test against, on any platform, with or without hardware). This makes real-hardware verification (a Task-8-equivalent pass) load-bearing for BLE in a way it wasn't quite as acutely for wifi — wifi at least had a real socket to test the on-device accept loop against in the unix-port interpreter; BLE's on-device GATT server logic is entirely faked until it runs on real silicon.
- Specifically worth a dedicated real-hardware check, mirroring wifi's own headline verification: reconnecting `run` mode multiple times without a physical reset, confirming an `@mcu.loop`-decorated function doesn't duplicate across reconnects. Expected to already be fixed (same underlying mechanism, already fixed for wifi and confirmed transport-agnostic above) — worth confirming empirically on BLE specifically rather than assuming.

## Alternatives considered

**`aioble` instead of the built-in `bluetooth` module.** Would produce nicer-looking on-device code (async, matching `boot.py`'s existing `uasyncio` style in `_handle_run` more directly, no IRQ-to-queue bridge needed). Rejected: `aioble` is not present in the generic ESP32 firmware build this project verifies against (confirmed directly against real hardware — `import aioble` fails, `import bluetooth` succeeds), so it would need to be vendored into `tether_runtime`, the same kind of dependency this project already carries deliberate weight for with `umsgpack.py` (see its own `VENDORED.md`). A second vendored dependency, whose MicroPython-version compatibility this project has not evaluated, was judged not worth it against the built-in module's zero-new-maintenance-surface tradeoff.

**Always-on BLE advertising (no explicit provisioning step).** Rejected: removes the natural place to configure/rotate the shared secret and makes BLE peripheral mode non-opt-in on every board by default — a bigger default security/battery surface than this project wants to force on every generated `boot.py`.

## Explicitly out of scope

- **BLE pairing/bonding** (OS/platform-level Bluetooth security, encrypted links, MITM protection). This design reuses the same preamble-shared-secret model wifi uses instead — deliberately not touching the platform pairing story. Same accepted tradeoff wifi's spec already made explicit for its own auth: "keep casual snoopers and accidental cross-connects out," not a defense against a sophisticated adversary.
- **BLE scanning/discovery** via `tether devices` or otherwise. `provision-ble` printing the address is the only discovery mechanism.
- **Concurrent/simultaneous BLE connections.** Sequential only, same as wifi.
- **A same-connection upload→run pivot.** Always two separate connections, same as wifi.
- **Real cryptography** (encrypted GATT, challenge-response auth). Same plaintext-preshared-token tradeoff as wifi, for the same reasons.
- **Sharing a secret/config file between wifi and BLE.** Independent provisioning, independent secrets, independent files.
