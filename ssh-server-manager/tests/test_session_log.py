"""Opt-in session transcripts.

The proxy itself is exercised with a shell command instead of ssh, so these tests
never open a network connection. What is pinned: the transcript records the
display, masks credential-shaped values, strips terminal control sequences, and
is mode 600.
"""

from __future__ import annotations

import io
import os
import stat
import sys
from pathlib import Path

import pytest

from ssh_server_manager import audit, session_log
from ssh_server_manager.ssh_runner import SSHError

def _pty_available() -> bool:
    """Some sandboxes deny openpty(); skip rather than fail when that is why."""
    if os.name == "nt" or not hasattr(os, "openpty"):
        return False
    try:
        master, slave = os.openpty()
    except OSError:
        return False
    os.close(master)
    os.close(slave)
    return True


POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="session logging needs a POSIX pty"
)
PTY_ONLY = pytest.mark.skipif(
    not _pty_available(),
    reason="this environment does not permit pty allocation",
)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    monkeypatch.setenv("SSM_DATA_DIR", str(directory))
    return directory


def test_support_is_reported_honestly():
    assert session_log.supported() == (os.name != "nt" and hasattr(os, "openpty"))


@POSIX_ONLY
def test_default_log_path_is_private_and_named_for_the_host(data_dir):
    path = session_log.default_log_path("web1.example/../x")
    assert path.parent == data_dir / "sessions"
    assert path.name.endswith("-web1.example_.._x.log")
    assert session_log.sessions_dir().is_dir()
    if os.name != "nt":
        assert stat.S_IMODE(session_log.sessions_dir().stat().st_mode) == 0o700


def test_lines_are_redacted_and_control_sequences_stripped():
    assert session_log.redact_line(b"mysql --password=hunter2\r\n") == "mysql --password=***"

    coloured = b"\x1b[31mwarning\x1b[0m: MYSQL_PWD=hunter2\r\n"
    assert session_log.redact_line(coloured) == "warning: MYSQL_PWD=***"

    assert session_log.redact_line(b"\x1b]0;title\x07hello\r\n") == "hello"


def test_the_writer_buffers_partial_lines_and_flushes_the_rest():
    buffer = io.BytesIO()
    writer = session_log.TranscriptWriter(buffer)
    writer.feed(b"first line\npart")
    assert buffer.getvalue() == b"first line\n"
    writer.feed(b"ial\n")
    assert buffer.getvalue() == b"first line\npartial\n"
    writer.feed(b"no trailing newline")
    writer.flush()
    assert buffer.getvalue().endswith(b"no trailing newline\n")


def test_a_pathological_line_without_newlines_is_flushed(monkeypatch):
    monkeypatch.setattr(session_log, "MAX_LINE_BYTES", 32)
    buffer = io.BytesIO()
    writer = session_log.TranscriptWriter(buffer)
    writer.feed(b"x" * 40)
    assert buffer.getvalue().strip() == b"x" * 40


@POSIX_ONLY
@PTY_ONLY
def test_run_logged_records_output_and_returns_the_exit_code(tmp_path):
    log_path = tmp_path / "session.log"
    code = session_log.run_logged(
        ["/bin/sh", "-c", "echo hello; echo 'app --token=secret-value'; exit 7"],
        environment=dict(os.environ),
        log_path=log_path,
        stdin=io.BytesIO(b""),
        stdout=io.BytesIO(),
    )
    assert code == 7
    text = log_path.read_text(encoding="utf-8")
    assert "hello" in text
    assert "secret-value" not in text
    assert "--token=***" in text
    if os.name != "nt":
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


@POSIX_ONLY
@PTY_ONLY
def test_run_logged_forwards_typed_input_but_does_not_record_it(tmp_path):
    """Input is forwarded to the session; only the display reaches the file.

    The proxy selects on file descriptors, so the test gives it a real pipe with
    a writer thread rather than a BytesIO: a stream without a descriptor would
    forward nothing, and the child would wait on `read` forever.
    """
    import threading

    log_path = tmp_path / "session.log"
    read_fd, write_fd = os.pipe()

    def write_then_close():
        try:
            os.write(write_fd, b"typed-secret\n")
        finally:
            os.close(write_fd)

    with os.fdopen(read_fd, "rb", closefd=True) as reader:
        writer = threading.Thread(target=write_then_close, daemon=True)
        writer.start()
        code = session_log.run_logged(
            ["/bin/sh", "-c", 'read line; echo "got:$line"'],
            environment=dict(os.environ),
            log_path=log_path,
            stdin=reader,
            stdout=io.BytesIO(),
        )
        writer.join(timeout=5)
    assert code == 0
    assert "got:typed-secret" in log_path.read_text(encoding="utf-8")


@POSIX_ONLY
def test_a_missing_log_directory_is_reported_before_starting_a_session(data_dir, tmp_path):
    from ssh_server_manager.db import Database

    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", username="alice")
    with pytest.raises(SSHError, match="does not exist"):
        session_log.record(
            database, "box", log_path=tmp_path / "missing" / "session.log"
        )


@POSIX_ONLY
def test_record_writes_the_transcript_it_announces(data_dir, tmp_path, monkeypatch):
    from ssh_server_manager.db import Database

    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))

    observed = {}

    def fake_run_logged(command, *, environment, log_path, **kwargs):
        observed["command"] = command
        Path(log_path).write_text("transcript\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(session_log, "run_logged", fake_run_logged)
    result = session_log.record(database, "box")
    assert result["exit_code"] == 0
    assert result["path"] == observed["path"] if "path" in observed else True
    assert Path(result["path"]).exists()
    assert observed["command"][-1] == "box"
    # The start of a recording is itself an auditable action.
    events = audit.read_events()
    assert any(event["event"] == "session_log" for event in events)


@POSIX_ONLY
def test_windows_style_refusal_message_is_explicit(monkeypatch):
    monkeypatch.setattr(session_log, "supported", lambda: False)
    with pytest.raises(SSHError, match="needs a POSIX terminal"):
        session_log.require_support()


@POSIX_ONLY
def test_a_denied_pty_is_reported_as_an_error(tmp_path, monkeypatch):
    def refuse():
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "openpty", refuse)
    with pytest.raises(SSHError, match="does not permit a session pty"):
        session_log.run_logged(
            ["/bin/sh", "-c", "true"],
            environment=dict(os.environ),
            log_path=tmp_path / "session.log",
            stdin=io.BytesIO(b""),
            stdout=io.BytesIO(),
        )
    # The transcript file was opened before the pty was requested; it must not be
    # left behind as an empty file that looks like a finished recording.
    assert not (tmp_path / "session.log").exists()


def test_connect_refuses_to_combine_log_and_mosh(tmp_path, monkeypatch, capsys):
    from ssh_server_manager.cli import main

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SSM_DATA_DIR", str(data))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    from ssh_server_manager.db import Database

    Database(data / "manager.db").create_server(
        alias="box", hostname="box.example", username="alice"
    )
    code = main(["connect", "box", "--mosh", "--log"])
    assert code == 2
    assert "--log cannot be combined with --mosh" in capsys.readouterr().err
