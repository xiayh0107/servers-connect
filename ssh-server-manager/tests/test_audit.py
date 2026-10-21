"""The local audit log: what is recorded, what is never recorded, and reading it.

The invariant that matters most is negative: a secret that reaches a command line
or a reveal must not appear in the log. Everything else here is about the log
staying trustworthy — one line per event, corrupt lines skipped rather than
hiding the rest, newest first, and filters that mean what they say.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from ssh_server_manager import audit


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    # The module warns once per process on write failure; reset so tests that
    # provoke a failure see their own warning.
    monkeypatch.setattr(audit, "_warned", False)
    return tmp_path / "data"


def test_events_are_appended_as_json_lines(isolated_data_dir):
    audit.record("exec", alias="web1", host="web1", command=["uptime"])
    audit.record("exec", alias="web1", host="web1", command=["id"])
    lines = audit.audit_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "exec"
    assert first["alias"] == "web1"
    assert first["actor"] == "cli"
    assert first["command"] == "uptime"
    assert first["at"].endswith("Z") or "+00:00" in first["at"]


def test_the_log_file_is_private(isolated_data_dir):
    audit.record("connect", alias="web1")
    if os.name == "nt":
        pytest.skip("POSIX permission bits do not exist on Windows")
    assert stat.S_IMODE(audit.audit_path().stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("command", "expected_hidden"),
    [
        (["mysql", "--password=hunter2", "-e", "select 1"], "hunter2"),
        (["psql", "--password", "hunter2"], "hunter2"),
        (["env", "MYSQL_PWD=hunter2", "mysql"], "hunter2"),
        (["curl", "-H", "Authorization: Bearer abcdef123456", "https://x"], "abcdef123456"),
        (["app", "--api-key", "hunter2"], "hunter2"),
        (["app", "--token=hunter2"], "hunter2"),
        (["ssh-keygen", "-p", "--passphrase=hunter2"], "hunter2"),
    ],
)
def test_credentials_in_a_command_line_are_masked(isolated_data_dir, command, expected_hidden):
    assert expected_hidden not in audit.redact_command(" ".join(command))
    audit.record("exec", alias="db1", command=command)
    assert expected_hidden not in audit.audit_path().read_text(encoding="utf-8")


def test_an_ordinary_command_is_left_alone(isolated_data_dir):
    text = "systemctl is-active nginx && du -sh /var/log"
    assert audit.redact_command(text) == text


def test_a_secret_named_field_is_never_stored(isolated_data_dir):
    audit.record("reveal", credential="prod root", value="hunter2", password="hunter2")
    stored = audit.audit_path().read_text(encoding="utf-8")
    assert "hunter2" not in stored
    entry = json.loads(stored.strip())
    assert entry["value"] == "***"
    assert entry["password"] == "***"


def test_reading_returns_newest_first_and_honours_limit(isolated_data_dir):
    for index in range(5):
        audit.record("exec", alias="web1", command=[f"cmd{index}"])
    events = audit.read_events(limit=3)
    assert [event["command"] for event in events] == ["cmd4", "cmd3", "cmd2"]


def test_reading_filters_by_alias_and_event(isolated_data_dir):
    audit.record("exec", alias="web1", command=["a"])
    audit.record("exec", alias="db1", command=["b"])
    audit.record("reveal", alias=None, credential="pg")
    assert [event["command"] for event in audit.read_events(alias="db1")] == ["b"]
    assert [event["event"] for event in audit.read_events(events=["reveal"])] == ["reveal"]
    assert audit.read_events(alias="missing") == []


def test_reading_skips_torn_lines_instead_of_hiding_the_rest(isolated_data_dir):
    audit.record("exec", alias="web1", command=["a"])
    with open(audit.audit_path(), "a", encoding="utf-8") as handle:
        handle.write('{"event": "exec", "alias"')  # killed mid-write
        handle.write("\nnot json at all\n\n")
    audit.record("exec", alias="web1", command=["b"])
    events = audit.read_events()
    assert [event["command"] for event in events] == ["b", "a"]


def test_reading_a_missing_log_is_empty_not_an_error(isolated_data_dir):
    assert audit.read_events() == []


def test_since_filter_uses_the_recorded_timestamps(isolated_data_dir):
    audit.record("exec", alias="web1", command=["a"])
    marker = audit.read_events()[0]["at"]
    audit.record("exec", alias="web1", command=["b"])
    events = audit.read_events(since=marker)
    assert [event["command"] for event in events] == ["b", "a"]
    assert [event["command"] for event in audit.read_events(since="9999-01-01T00:00:00+00:00")] == []


def test_an_oversized_event_is_truncated_rather_than_written(isolated_data_dir):
    audit.record("exec", alias="web1", command="x" * (audit.MAX_EVENT_BYTES * 2))
    entry = json.loads(audit.audit_path().read_text(encoding="utf-8").strip())
    assert entry["truncated"] is True
    assert entry["event"] == "exec"
    # The payload is dropped entirely rather than written in pieces.
    assert "command" not in entry


def test_a_write_failure_warns_once_and_does_not_raise(isolated_data_dir, monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(audit.os, "open", explode)
    assert audit.record("exec", alias="web1", command=["a"]) is None
    assert audit.record("exec", alias="web1", command=["b"]) is None
    captured = capsys.readouterr()
    assert captured.err.count("could not append to the audit log") == 1


# --- rotation ---------------------------------------------------------------------


def test_rotation_shifts_segments_and_keeps_every_event_readable(isolated_data_dir, monkeypatch):
    monkeypatch.setenv("SSM_AUDIT_MAX_BYTES", "400")
    for index in range(12):
        audit.record("exec", alias="web1", command=[f"cmd{index}"])
    files = sorted(path.name for path in audit.audit_path().parent.iterdir())
    assert "audit.log" in files
    assert "audit.log.1" in files
    # Rotated segments are still read, so history is not lost by rotating.
    events = audit.read_events(limit=50)
    assert len(events) >= 5
    assert events[0]["command"] == "cmd11"
    # Rotation runs before the append, so the live file may pass the limit by at
    # most the event that triggered it -- it never grows unbounded.
    assert audit.audit_path().stat().st_size <= 400 + 200


def test_rotation_is_bounded_to_the_configured_segments(isolated_data_dir, monkeypatch):
    monkeypatch.setenv("SSM_AUDIT_MAX_BYTES", "200")
    for index in range(60):
        audit.record("exec", alias="web1", command=[f"cmd{index}"])
    names = sorted(path.name for path in audit.audit_path().parent.iterdir())
    assert names == sorted(
        ["audit.log"]
        + [f"audit.log.{index}" for index in range(1, audit.ROTATED_SEGMENTS + 1)]
    )


def test_explicit_rotation_can_be_forced(isolated_data_dir):
    audit.record("exec", alias="web1", command=["a"])
    result = audit.rotate(force=True)
    assert result["rotated"] is True
    assert (isolated_data_dir / "audit.log.1").exists()
    assert not audit.audit_path().exists()
    # Nothing to rotate is reported, not treated as a failure.
    assert audit.rotate()["rotated"] is False


def test_a_failed_rotation_does_not_lose_the_event(isolated_data_dir, monkeypatch, capsys):
    audit.record("exec", alias="web1", command=["before"])

    def explode(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(audit.os, "replace", explode)
    monkeypatch.setenv("SSM_AUDIT_MAX_BYTES", "1")
    assert audit.record("exec", alias="web1", command=["after"]) is not None
    assert "could not rotate the audit log" in capsys.readouterr().err
    assert [event["command"] for event in audit.read_events(limit=2)] == ["after", "before"]
