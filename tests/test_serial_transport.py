import pytest

from tether.transports.serial import (
    RawReplError,
    SerialStream,
    discover,
    list_devices,
    push_raw_repl,
    read_file,
    remove_file,
    reset_board,
    run_python,
    write_file,
    write_files,
)


class _FakeMicroPythonSerial:
    """Scripted fake mimicking a real MicroPython device's raw-REPL
    responses, matching the exact write/read sequence push_raw_repl uses
    (no soft-reset path - push_raw_repl doesn't exercise it).
    """

    def __init__(self, stdout: bytes = b"", stderr: bytes = b""):
        self.written = bytearray()
        self._stdout = stdout
        self._stderr = stderr
        self._buffer = bytearray()
        self.in_waiting = 0

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        if data == b"\r\x01":  # ctrl-A: enter raw REPL
            self._buffer += b"raw REPL; CTRL-B to exit\r\n>"
        elif data == b"\x04":  # ctrl-D: execute
            self._buffer += b"OK" + self._stdout + b"\x04" + self._stderr + b"\x04"
        return len(data)

    def read(self, n: int = 1) -> bytes:
        chunk = bytes(self._buffer[:n])
        del self._buffer[:n]
        return chunk


class _FakePortInfo:
    def __init__(self, device, vid, pid):
        self.device = device
        self.vid = vid
        self.pid = pid


def test_discover_finds_single_known_device():
    def fake_list_ports():
        return [
            _FakePortInfo("/dev/ttyUSB0", 0x10C4, 0xEA60),  # CP210x - known
            _FakePortInfo("/dev/ttyUSB1", 0x1234, 0x5678),  # unknown chip
        ]

    assert discover(list_ports_fn=fake_list_ports) == "/dev/ttyUSB0"


def test_discover_raises_when_no_known_device_found():
    def fake_list_ports():
        return [_FakePortInfo("/dev/ttyUSB0", 0x1234, 0x5678)]

    with pytest.raises(RawReplError, match="no known") as exc_info:
        discover(list_ports_fn=fake_list_ports)
    assert "explicit" in str(exc_info.value).lower()


def test_discover_accepts_extra_vid_pid_pairs():
    def fake_list_ports():
        return [_FakePortInfo("/dev/ttyUSB0", 0x9999, 0x1111)]  # not in the built-in list

    assert (
        discover(list_ports_fn=fake_list_ports, extra_vid_pid={(0x9999, 0x1111)}) == "/dev/ttyUSB0"
    )


def test_discover_raises_when_multiple_known_devices_found():
    def fake_list_ports():
        return [
            _FakePortInfo("/dev/ttyUSB0", 0x10C4, 0xEA60),
            _FakePortInfo("/dev/ttyUSB1", 0x1A86, 0x7523),  # CH340 - also known
        ]

    with pytest.raises(RawReplError, match="multiple"):
        discover(list_ports_fn=fake_list_ports)


def test_list_devices_returns_all_known_matches():
    def fake_list_ports():
        return [
            _FakePortInfo("/dev/ttyUSB0", 0x10C4, 0xEA60),  # CP210x - known
            _FakePortInfo("/dev/ttyUSB1", 0x1A86, 0x7523),  # CH340 - also known
            _FakePortInfo("/dev/ttyUSB2", 0x1234, 0x5678),  # unknown chip
        ]

    assert list_devices(list_ports_fn=fake_list_ports) == ["/dev/ttyUSB0", "/dev/ttyUSB1"]


def test_list_devices_returns_empty_list_when_none_found():
    def fake_list_ports():
        return [_FakePortInfo("/dev/ttyUSB0", 0x1234, 0x5678)]

    assert list_devices(list_ports_fn=fake_list_ports) == []


class _FakeResettableSerial:
    """Records RTS/DTR transitions - reset_board() drives a two-pulse
    sequence (bootloader entry via DTR+RTS, then a normal-boot RTS-only
    pulse) found necessary against real hardware: an RTS-only pulse alone
    does not reliably interrupt a program that's actively running right
    now (see reset_board's own docstring for the full story).
    """

    def __init__(self):
        self.rts_history: list[bool] = []
        self.dtr_history: list[bool] = []
        self._rts = False
        self._dtr = False
        self.reset_input_buffer_called = False

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, value: bool) -> None:
        self._rts = value
        self.rts_history.append(value)

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self._dtr = value
        self.dtr_history.append(value)

    def reset_input_buffer(self) -> None:
        self.reset_input_buffer_called = True


def test_reset_board_pulses_into_bootloader_then_boots_normally(monkeypatch):
    # No need for reset_board's real ~1.3s of settle time in a unit test -
    # only the pin-toggle sequence itself is under test here.
    monkeypatch.setattr("tether.transports.serial.time.sleep", lambda seconds: None)
    fake = _FakeResettableSerial()

    reset_board(fake)

    # Bootloader-entry pulse: DTR low then high while RTS pulses low then
    # high (GPIO0 held low during the reset pulse), then DTR released -
    # matches esptool's ClassicReset shape.
    assert fake.dtr_history == [False, True, False]
    # Two RTS pulses total: the bootloader-entry one above, plus the
    # trailing RTS-only pulse that boots normally out of the bootloader.
    assert fake.rts_history == [True, False, True, False]
    assert fake.reset_input_buffer_called is True


def test_reset_board_degrades_gracefully_when_rts_is_unsupported():
    # Matches esptool's own precedent (reset.py's ResetStrategy.__call__):
    # some serial adapters/platforms don't expose RTS/DTR control at all.
    # A connect() attempt on such an adapter shouldn't hard-fail just
    # because this best-effort robustness step isn't available - the
    # subsequent raw-REPL work still gets its own clear timeout error if
    # the board genuinely wasn't in a usable state.
    class _NoRtsSerial:
        @property
        def rts(self) -> bool:
            raise OSError("Setting RTS/DTR lines is not supported")

        @rts.setter
        def rts(self, value: bool) -> None:
            raise OSError("Setting RTS/DTR lines is not supported")

        def reset_input_buffer(self) -> None:
            pass

    reset_board(_NoRtsSerial())  # must not raise


def test_push_raw_repl_sends_code_and_enters_exits_raw_repl():
    fake = _FakeMicroPythonSerial()

    push_raw_repl(fake, b"print('hi')")

    assert b"\r\x03" in bytes(fake.written)  # interrupt
    assert b"\r\x01" in bytes(fake.written)  # enter raw repl
    assert b"print('hi')" in bytes(fake.written)
    assert bytes(fake.written).endswith(b"\r\x02")  # exit raw repl is the last thing sent


def test_push_raw_repl_wait_false_returns_without_waiting_for_completion():
    # The actual on-device bundle (chunk 6's dispatch loop) never returns -
    # it runs forever. wait=True would hang until timeout waiting for a
    # closing \x04 that never comes, even though the code started fine.
    # Simulate "OK" ack arriving but no closing \x04 ever - the fake's
    # default write() handler already only queues "OK" + stdout + \x04 +
    # stderr + \x04 all at once, so to model "never finishes" we need a
    # variant that only ever provides the OK ack.

    class _NeverFinishingSerial(_FakeMicroPythonSerial):
        def write(self, data: bytes) -> int:
            self.written.extend(data)
            if data == b"\r\x01":
                self._buffer += b"raw REPL; CTRL-B to exit\r\n>"
            elif data == b"\x04":
                self._buffer += b"OK"  # ack only - code keeps running, no \x04 ever
            return len(data)

    never_finishing = _NeverFinishingSerial()
    push_raw_repl(never_finishing, b"run_forever()", wait=False, timeout=0.3)

    # Found against real ESP32 hardware: the started program (a
    # forever-running dispatch loop, in real use) takes over stdio
    # directly once running - there is no "raw REPL session" left to
    # cleanly exit back to, and sending the ctrl-B exit sequence anyway
    # races the program's takeover of stdin on real hardware, corrupting
    # the first bytes it reads. wait=False must NOT send the raw-REPL exit
    # sequence at all - it only makes sense (and is only safe) for wait=True,
    # where the code has already finished and control has genuinely
    # returned to the raw-REPL handler.
    assert b"\x02" not in bytes(never_finishing.written)


def test_push_raw_repl_raises_on_device_side_exception():
    fake = _FakeMicroPythonSerial(stderr=b"Traceback...\nValueError: boom\n")

    with pytest.raises(RawReplError, match="ValueError: boom"):
        push_raw_repl(fake, b"raise ValueError('boom')")


def test_push_raw_repl_raises_on_timeout_waiting_for_raw_repl_banner():
    class _SilentSerial:
        in_waiting = 0

        def write(self, data: bytes) -> int:
            return len(data)

        def read(self, n: int = 1) -> bytes:
            return b""  # device never responds

    with pytest.raises(RawReplError, match="timed out"):
        push_raw_repl(_SilentSerial(), b"pass", timeout=0.2)


class _FakePyserial:
    """Mimics pyserial's Serial.read(n) semantics with timeout=None:
    blocks until at least 1 byte, never returns more than what's buffered.
    """

    def __init__(self):
        self._buffer = bytearray()
        self.written = bytearray()

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    @property
    def in_waiting(self) -> int:
        return len(self._buffer)

    def read(self, size: int = 1) -> bytes:
        # Real pyserial with timeout=None blocks until `size` bytes are
        # available - it does NOT return early with a partial chunk. This
        # fake models that faithfully so SerialStream.read()'s "1 byte
        # blocking + drain in_waiting" strategy is tested against the real
        # constraint it exists to work around.
        while len(self._buffer) < size:
            pass  # test only ever calls this after feed(), so no real spin
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk


def test_serial_stream_read_blocks_for_one_byte_then_drains_available():
    fake = _FakePyserial()
    fake.feed(b"hello world")

    stream = SerialStream(fake)
    assert stream.read() == b"hello world"


def test_serial_stream_write_passes_through():
    fake = _FakePyserial()
    stream = SerialStream(fake)

    stream.write(b"data")

    assert bytes(fake.written) == b"data"


def test_write_file_sends_open_write_close_as_one_script():
    # One combined raw-repl exec, not multiple separate calls - state
    # (the open file handle) can't be assumed to persist across separate
    # raw-repl enter/exit cycles without real hardware to verify that
    # assumption, so everything goes in a single script.
    fake = _FakeMicroPythonSerial()

    write_file(fake, "/dispatch.py", b"print('hello')\n")

    sent = bytes(fake.written).decode()
    assert "open('/dispatch.py', 'wb')" in sent
    assert "a2b_base64(" in sent
    assert sent.count("\x04") == 1  # exactly one raw-repl exec (one ctrl-D)


def test_write_file_round_trips_binary_content_through_base64():
    import base64

    fake = _FakeMicroPythonSerial()
    content = b"\x00\x01\x02binary\xff\xfe"

    write_file(fake, "/x.bin", content)

    sent = bytes(fake.written).decode()
    # Extract the base64 chunk(s) embedded in the generated script and
    # confirm they actually decode back to the original content - not just
    # that *some* base64-looking text was sent.
    import re

    chunks = re.findall(r"a2b_base64\('([^']*)'\)", sent)
    assert chunks, "expected at least one a2b_base64(...) call in the generated script"
    decoded = b"".join(base64.b64decode(c) for c in chunks)
    assert decoded == content


def test_write_file_raises_on_device_side_error():
    fake = _FakeMicroPythonSerial(stderr=b"OSError: [Errno 28] ENOSPC\n")

    with pytest.raises(RawReplError, match="ENOSPC"):
        write_file(fake, "/dispatch.py", b"content")


def test_read_file_returns_none_when_file_missing():
    fake = _FakeMicroPythonSerial(stdout=b"__TETHER_MISSING__\n")

    assert read_file(fake, "/.tether_hash") is None


def test_read_file_returns_decoded_content():
    import base64

    content = b"deadbeef"
    fake = _FakeMicroPythonSerial(stdout=base64.b64encode(content) + b"\n")

    assert read_file(fake, "/.tether_hash") == content


def test_run_python_returns_stdout_and_stderr():
    fake = _FakeMicroPythonSerial(stdout=b"hello\n", stderr=b"")

    stdout, stderr = run_python(fake, b"print('hello')")

    assert stdout == b"hello\n"
    assert stderr == b""


def test_run_python_returns_stderr_without_raising():
    # Unlike read_file/write_file, run_python is a low-level primitive -
    # it hands back whatever the device printed to stderr rather than
    # raising, so callers (e.g. the CLI's status check) can decide for
    # themselves what a given stderr means instead of it always being a
    # hard error.
    fake = _FakeMicroPythonSerial(stdout=b"", stderr=b"Traceback...\nValueError: boom\n")

    stdout, stderr = run_python(fake, b"raise ValueError('boom')")

    assert stdout == b""
    assert b"ValueError: boom" in stderr


def test_write_files_sends_everything_as_one_round_trip():
    fake = _FakeMicroPythonSerial()

    write_files(
        fake,
        {"/a.py": b"print(1)", "/b/c.py": b"print(2)"},
        dirs=("/b",),
    )

    sent = bytes(fake.written).decode()
    assert sent.count("\x04") == 1  # one raw-repl exec for everything
    assert "mkdir('/b')" in sent
    assert "open('/a.py', 'wb')" in sent
    assert "open('/b/c.py', 'wb')" in sent


def test_write_files_round_trips_all_content_correctly():
    import base64
    import re

    fake = _FakeMicroPythonSerial()
    files = {"/a.py": b"hello", "/b.py": b"\x00\x01world\xff"}

    write_files(fake, files)

    sent = bytes(fake.written).decode()
    chunks = re.findall(r"a2b_base64\('([^']*)'\)", sent)
    decoded = b"".join(base64.b64decode(c) for c in chunks)
    assert decoded == b"".join(files.values())


def test_write_files_with_only_dirs_does_not_reference_unbound_file_handle():
    # `_f` is only bound inside the per-file write blocks - if `files` is
    # empty, the generated script's cleanup `del` must not reference it,
    # or the script would raise NameError on-device.
    fake = _FakeMicroPythonSerial()

    write_files(fake, {}, dirs=("/only_a_dir",))

    sent = bytes(fake.written).decode()
    assert "mkdir('/only_a_dir')" in sent
    assert "_f" not in sent


def test_write_files_with_nothing_to_do_sends_no_script():
    fake = _FakeMicroPythonSerial()

    write_files(fake, {})

    assert bytes(fake.written) == b""


def test_remove_file_deletes_an_existing_file():
    fake = _FakeMicroPythonSerial()

    remove_file(fake, "/tether_wifi.json")

    sent = bytes(fake.written).decode()
    assert "uos.remove('/tether_wifi.json')" in sent


def test_remove_file_does_not_raise_when_file_is_already_gone():
    # uos.remove() raises OSError for a missing file - remove_file must
    # swallow that (matches ensure_dir's existing try/except OSError
    # pattern for mkdir), since "already removed" and "just removed it"
    # should look the same to the caller. The fake always returns empty
    # stderr regardless of what's sent, so it can't simulate the device
    # actually raising OSError - the real guarantee this test protects is
    # that remove_file's generated script itself contains the swallow, not
    # just that this particular fake happens not to raise.
    fake = _FakeMicroPythonSerial(stderr=b"")

    remove_file(fake, "/tether_wifi.json")  # must not raise

    sent = bytes(fake.written).decode()
    assert "except OSError" in sent
