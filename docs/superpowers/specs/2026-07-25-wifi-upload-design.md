# WiFi provisioning + a CLI (design)

Status: approved, not yet implemented.
Date: 2026-07-25.

## Problem

`tether`'s wifi transport (`transports/wifi.py`, chunk 12) is PC-side client
code only. It has never been usable against a real board: `tether` has no
way to get a wifi-reachable program *onto* a device in the first place.
`generate_bootstrap()` (the function that builds what gets uploaded)
hardcodes the on-device dispatch loop to `sys.stdin`/`sys.stdout` — there is
no variant that wires it to a socket, and no on-device wifi-connection
management (credentials, reconnect after reset) exists at all. This is
documented as a known, deliberate gap in DESIGN.md and CHUNKS.md's chunk 12
entry.

This design closes that gap for wifi specifically. BLE has a materially
different set of problems (no credentials, but GATT peripheral setup and
BLE's throughput/MTU constraints) and is deliberately out of scope here —
follow-up design, reusing the same CLI shape.

## Scope decision

WiFi only, this pass. The CLI is structured so a `provision-ble` command
(or similar) can slot in the same way later without restructuring anything
built here.

## Command surface

```
tether devices
tether provision-wifi [--port PORT] --ssid SSID [--password PASSWORD]
tether status [--port PORT]
tether unprovision-wifi [--port PORT]
```

- `--port` is optional everywhere it's used. If omitted and more than one
  known-VID/PID serial device is connected, `beaupy.select()` prompts
  interactively instead of erroring the way today's `discover()` does on
  ambiguity.
- `--password` omitted on `provision-wifi` → `beaupy.prompt(secure=True)`
  (hidden input, never touches shell history).
- `unprovision-wifi` goes through `beaupy.confirm()` first — it kills the
  board's wifi reachability, not purely additive like the others.
- `devices` lists connected boards (port + VID/PID), no board interaction.

## On-device architecture

This is the core of the design — the rest is plumbing around it.

### `boot.py` (new, uploaded once by `provision-wifi`)

Auto-run by MicroPython on every boot/reset (standard MicroPython
convention — `boot.py` always runs before anything else). Logic:

1. Check for `/tether_wifi.json`. If it doesn't exist: do nothing, fall
   straight through to idle REPL. A never-provisioned board behaves
   *exactly* as it does today — this file's mere absence is the "not
   opted in" state, no separate flag needed.
2. If it exists, read `{"ssid": ..., "password": ...}` and attempt to join
   wifi (`network.WLAN(network.STA_IF)`) with a bounded timeout (~15s).
   On failure or timeout, give up and fall through to idle REPL — a bad
   password or an unreachable network must never permanently lock out
   serial access. This also means `boot.py` must not swallow
   `KeyboardInterrupt` during the connect-wait loop, so a real ESP32's
   existing Ctrl-C handling (still active at this point —
   `micropython.kbd_intr(-1)` is only called later, inside the dispatch
   loop itself, see below) can interrupt a stuck attempt the same way
   `reset_board()` + raw-REPL entry already does for everything else.
3. Once connected: open a TCP listener on `transports/wifi.py`'s existing
   `DEFAULT_PORT` (8765), accept **one** connection, for the lifetime of
   this boot. If that connection drops, the board does not re-listen —
   see "Explicitly out of scope" below for why, and what that means for
   reconnecting.
4. If `/tether_app.py` exists (uploaded separately, via a normal serial
   `connect()` session — unchanged), import and run it, wired to the
   accepted socket instead of stdio.

Credentials live in their own file, not baked into `boot.py`'s source:
`unprovision-wifi` is then just "delete one file," and re-provisioning
with new credentials doesn't need to re-upload `boot.py`. Plaintext is the
only realistic option — there's no secure credential storage on these
chips; this matches essentially every MicroPython wifi-manager project and
is documented as an accepted constraint, not silently glossed over.

### `generate_bootstrap()` change (small, backward-compatible)

Currently hardcodes `_tether_asyncio.StreamReader(_tether_sys.stdin.buffer)`
/ `StreamWriter(_tether_sys.stdout.buffer, {})` inside `_tether_main()`.
Change: check for a pre-set global (e.g. `_tether_stream_override`) before
falling back to stdio. `boot.py` sets this global to the accepted socket's
reader/writer *before* `import tether_app` when wifi is in play; a normal
serial `connect()` (raw-REPL exec of `tether_app.py` directly, nothing sets
the override) is completely unaffected — same generated code, same
tested/hardware-verified stdio path, zero behavior change for existing
usage.

### Known tradeoff: `status` interrupts the board

`tether status` needs raw-REPL access to ask the board's live state
(connected? what IP?), which means interrupting whatever it's currently
doing — the same `reset_board()` hard-reset-then-raw-REPL dance already
built for serial reconnection. Checking status therefore briefly resets
the board's current wifi session (it reconnects via `boot.py` on the next
boot, same as any other reset). Accepted for v1 rather than building a
second, non-interrupting status channel (e.g. a status file boot.py
updates periodically) - not worth the complexity until it's a real pain
point.

## PC-side / CLI architecture

- New `src/tether/cli.py` — a `click` command group. Calls straight into
  existing `transports/serial.py` primitives (`write_files`, the raw-REPL
  helpers, `reset_board`) — no new upload mechanism, just new content
  (`boot.py` + `tether_wifi.json`) going through the same tested path.
- One new small addition to `transports/serial.py`: `list_devices()` —
  today's `discover()` deliberately raises on more than one match (chunk
  9's design for the *library's* `serial:auto` scheme, where ambiguity
  should be a hard error); the CLI needs all matches to drive
  `beaupy.select()` instead.
- `generate_wifi_boot(ssid, password) -> dict[str, bytes]`-style function
  (new, likely `src/tether/provisioning.py`) producing the `boot.py` +
  `tether_wifi.json` file contents, analogous in spirit to
  `generate_bootstrap()` but for the provisioning payload instead of the
  per-session app bundle.
- Entry point: `[project.scripts] tether = "tether.cli:main"` in
  `pyproject.toml`.

## Packaging

New `tether[cli]` extra: `click`, `beaupy`, `pyserial` (the CLI inherently
needs serial access, so it pulls that extra's dependency in rather than
requiring `tether[cli,serial]`). Library users who never touch the CLI
don't get click/beaupy forced on them.

## Testing

Same rigor as the rest of this project's serial/hardware work:

- Scripted raw-REPL fakes (reusing what `test_serial_transport.py` already
  has) for the upload logic itself.
- `click.testing.CliRunner` for command-level tests (argument parsing,
  prompting behavior, error messages) with the serial layer faked.
- Real `micropython` unix-port interpreter for `boot.py`'s socket-accept
  and stream-bridging logic — wifi-connect itself stubbed out (no real
  wifi under the unix port), but the "accept a real TCP connection, wire
  it as the dispatch loop's stream, run tether_app.py against it" part is
  fully testable for real, same technique as chunk 10's end-to-end
  bootstrap test.
- Real-hardware verification once implemented — an ESP32 is available for
  this right now.

## Explicitly out of scope here

- BLE provisioning (separate design, later).
- A non-interrupting status channel.
- On-device wifi credential encryption (not feasible on this hardware
  class).
- **Re-listening after the one accepted connection drops.** `boot.py`
  accepts exactly one TCP connection per boot cycle, matching this
  project's now-established "reconnect means restart fresh" philosophy
  (the same reasoning behind serial's `reset_board()` work). Serial can
  force that restart remotely (a hardware reset over RTS/DTR); wifi
  cannot — there's no way to physically reset the board over the network.
  So today, if a wifi connection drops, getting a fresh listener requires
  a physical action (power-cycle / reset button) or a fresh
  `provision-wifi` run. `BoardHandle.reconnect()` over wifi will fail
  loud (`MCUDisconnectedError`, same as any other disconnect) rather than
  hang, but it cannot bring the board back by itself. Making `boot.py`
  loop and accept new connections indefinitely — so a dropped wifi
  session recovers the same way a dropped serial one now does — is real,
  wanted follow-up work, deliberately deferred: it needs `tether_app.py`'s
  one-shot `_tether_main()` to become re-runnable per new connection
  rather than a true one-shot entrypoint, which is its own focused design
  question, not a bolt-on to this one.
