"""Opt-in session transcripts for `serverctl connect`.

A transcript needs a terminal proxy: to record what a session displays *and*
what the user types, this process has to sit between the user's terminal and
ssh. On POSIX that means a pty pair; on Windows there is no equivalent in the
standard library, so logging is refused there with a clear message instead of a
half-working approximation.

Two rules shape the implementation:

* **It is opt-in and it says so.** Everything typed into a recorded session ends
  up in the file, including a password typed at a `sudo` prompt that this tool
  never sees as a credential. So the warning is printed before the session
  starts, and the file is mode 600.
* **Redaction is best effort, not a guarantee.** Values in the shapes this tool
  already masks (flags and assignments that carry credentials) are masked
  line by line, and terminal control sequences are stripped so the file diffs
  cleanly. Anything outside those shapes is recorded as typed — which is
  precisely why the warning exists.
"""

from __future__ import annotations

import os
import re
import select
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO

from . import audit
from .db import Database
from .paths import data_dir, ensure_private_dir
from .ssh_runner import SSHError, SSHRunner

# CSI / OSC / single-character escapes. A transcript meant to be read (or
# grepped) should not be full of cursor moves from a full-screen program.
_ESCAPE_RE = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]"      # CSI
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    rb"|\x1b[@-Z\\-_]"               # two-character escapes
)
_CONTROL_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_LINE_BYTES = 64 * 1024


def supported() -> bool:
    return os.name != "nt" and hasattr(os, "openpty")


def require_support() -> None:
    if not supported():
        raise SSHError(
            "session logging needs a POSIX terminal; on Windows run the session "
            "without --log or record it with your terminal emulator"
        )


def sessions_dir() -> Path:
    return ensure_private_dir(data_dir() / "sessions")


def default_log_path(alias: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_alias = re.sub(r"[^A-Za-z0-9._-]", "_", alias)[:40] or "session"
    return sessions_dir() / f"{stamp}-{safe_alias}.log"


def redact_line(raw: bytes) -> str:
    """Strip terminal noise, then mask credential-shaped values.

    The returned text carries no line ending: the writer owns line endings, so a
    line that arrived as CRLF does not end up doubled in the transcript.
    """
    cleaned = _ESCAPE_RE.sub(b"", raw)
    cleaned = _CONTROL_RE.sub(b"", cleaned).replace(b"\r", b"")
    text = cleaned.decode("utf-8", "replace")
    return audit.redact_command(text).rstrip("\n")


class TranscriptWriter:
    """Line-buffered, redacted writer for one direction of a session."""

    def __init__(self, handle: BinaryIO) -> None:
        self.handle = handle
        self.pending = bytearray()

    def feed(self, data: bytes) -> None:
        self.pending.extend(data)
        while True:
            index = self.pending.find(b"\n")
            if index < 0:
                break
            line = bytes(self.pending[: index + 1])
            del self.pending[: index + 1]
            self._write(line)
        if len(self.pending) > MAX_LINE_BYTES:
            # A full-screen program can run without newlines for a long time;
            # flush rather than grow without bound.
            self._write(bytes(self.pending))
            self.pending.clear()

    def flush(self) -> None:
        if self.pending:
            self._write(bytes(self.pending))
            self.pending.clear()

    def _write(self, raw: bytes) -> None:
        text = redact_line(raw)
        if not text:
            return
        self.handle.write(text.encode("utf-8", "replace") + b"\n")
        self.handle.flush()


def _window_size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size()
    except OSError:
        return 80, 24
    return size.columns, size.lines


def run_logged(
    command: list[str],
    *,
    environment: dict[str, str],
    log_path: Path,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    """Run command under a pty, copying both directions and recording the display.

    Only the terminal's *output* stream is recorded. That is deliberate: a
    password typed at a prompt with echo turned off is written to the pty but
    never echoed back, so it never reaches the transcript. Everything the remote
    echoes — including the commands the user types — does.
    """
    require_support()
    import fcntl
    import signal
    import termios
    import tty

    reader = stdin if stdin is not None else sys.stdin.buffer
    writer = stdout if stdout is not None else sys.stdout.buffer
    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())

    descriptor = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    log_handle = os.fdopen(descriptor, "wb")
    try:
        master, slave = os.openpty()
    except OSError as exc:
        # A restricted environment (sandbox, hardened container) can refuse a
        # pty outright; say that instead of failing somewhere deeper, and do not
        # leave an empty file that reads like a finished recording.
        log_handle.close()
        try:
            os.unlink(str(log_path))
        except OSError:
            pass
        raise SSHError(f"this environment does not permit a session pty: {exc}") from exc
    try:
        process = subprocess.Popen(
            command,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        os.close(master)
        os.close(slave)
        log_handle.close()
        raise SSHError(f"could not start the session: {exc}") from exc
    os.close(slave)

    transcript = TranscriptWriter(log_handle)
    restore = None
    input_stream = reader
    try:
        if interactive:
            restore = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())

            def _resize(signum, frame):  # noqa: ARG001 - signal handler signature
                try:
                    columns, rows = _window_size()
                    fcntl.ioctl(
                        master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
                    )
                except OSError:
                    pass

            try:
                signal.signal(signal.SIGWINCH, _resize)
                _resize(signal.SIGWINCH, None)
            except (ValueError, OSError):
                pass
        while True:
            watch: list[int] = [master]
            input_fd: int | None = None
            if input_stream is not None:
                try:
                    input_fd = input_stream.fileno()
                except (AttributeError, OSError, ValueError):
                    input_fd = None
                if input_fd is not None:
                    watch.append(input_fd)
            try:
                readable = select.select(watch, [], [], 0.1)[0]
            except InterruptedError:
                continue
            except OSError:
                break
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except (BlockingIOError, InterruptedError):
                    data = b""
                except OSError:
                    data = b""
                if data:
                    transcript.feed(data)
                    writer.write(data)
                    writer.flush()
                elif process.poll() is not None:
                    break
            if input_fd is not None and input_fd in readable:
                try:
                    chunk = os.read(input_fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    # Input closed (a pipe ended): keep showing the session, but
                    # stop waiting on a stream that will never deliver again.
                    input_stream = None
                else:
                    try:
                        os.write(master, chunk)
                    except OSError:
                        input_stream = None
            if process.poll() is not None and master not in readable:
                # Drain whatever the pty still holds, then stop.
                try:
                    while True:
                        data = os.read(master, 65536)
                        if not data:
                            break
                        transcript.feed(data)
                        writer.write(data)
                        writer.flush()
                except OSError:
                    pass
                break
        try:
            code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            code = process.wait(timeout=5)
    finally:
        if restore is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, restore)
            except (OSError, termios.error):
                pass
        transcript.flush()
        log_handle.close()
        try:
            os.close(master)
        except OSError:
            pass
    return int(code)


def record(database: Database, identifier: str, *, log_path: str | Path | None = None,
           timeout: int = 12) -> dict[str, Any]:
    """Open a logged interactive session; returns where it was recorded."""
    require_support()
    server = database.get_server(identifier)
    target = Path(log_path).expanduser() if log_path else default_log_path(server["alias"])
    if target.parent and not target.parent.is_dir():
        raise SSHError(f"session log directory does not exist: {target.parent}")
    runner = SSHRunner(database)
    command = runner._base_command(server, timeout=timeout)
    environment = runner._environment(server, host_key_confirmation=True)
    audit.record("session_log", alias=server["alias"], path=str(target))
    code = run_logged(command, environment=environment, log_path=target)
    return {"alias": server["alias"], "path": str(target), "exit_code": code}
