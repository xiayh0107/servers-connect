"""Saved port-forwarding profiles and the background processes behind them.

The interesting parts are (a) parsing the OpenSSH -L/-R/-D spelling so a stored
profile cannot smuggle shell syntax into a command line, (b) refusing to call a
tunnel "running" just because a pid exists, and (c) tearing the process down
again. Everything that spawns a process is POSIX-only here: the fake ssh used by
these tests is a shebang script, which Windows cannot execute without a shell.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ssh_server_manager import tunnels
from ssh_server_manager.db import ConflictError, Database, NotFoundError
from ssh_server_manager.ssh_runner import SSHError
from ssh_server_manager.validation import (
    ValidationError,
    format_forward_spec,
    parse_forward_spec,
)


POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the fake ssh used here is a POSIX shebang script"
)


@pytest.fixture()
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    # A short runtime directory on purpose: it mirrors /tmp/ssm-UID, and OpenSSH
    # caps the ControlPath marker this directory is part of.
    runtime = Path(tempfile.mkdtemp(prefix="ssm-t-"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(runtime))
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="db1", hostname="db1.example", port=22, username="alice")
    database.create_server(alias="web1", hostname="web1.example", port=22, username="alice")
    try:
        yield database
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


# --- parsing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "kind", "expected"),
    [
        (
            "15432:localhost:5432",
            "local",
            {"kind": "local", "bind_host": "127.0.0.1", "bind_port": 15432,
             "remote_host": "localhost", "remote_port": 5432},
        ),
        (
            "0.0.0.0:8080:internal:80",
            "local",
            {"kind": "local", "bind_host": "0.0.0.0", "bind_port": 8080,
             "remote_host": "internal", "remote_port": 80},
        ),
        (
            "[::1]:9000:localhost:9000",
            "local",
            {"kind": "local", "bind_host": "::1", "bind_port": 9000,
             "remote_host": "localhost", "remote_port": 9000},
        ),
        (
            "8022:localhost:22",
            "remote",
            {"kind": "remote", "bind_host": "127.0.0.1", "bind_port": 8022,
             "remote_host": "localhost", "remote_port": 22},
        ),
        (
            "1080",
            "dynamic",
            {"kind": "dynamic", "bind_host": "127.0.0.1", "bind_port": 1080,
             "remote_host": None, "remote_port": None},
        ),
        (
            "127.0.0.1:1080",
            "dynamic",
            {"kind": "dynamic", "bind_host": "127.0.0.1", "bind_port": 1080,
             "remote_host": None, "remote_port": None},
        ),
    ],
)
def test_forward_specs_parse_the_way_ssh_reads_them(spec, kind, expected):
    assert parse_forward_spec(spec, kind) == expected


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "15432",
        "15432:localhost",
        "15432:localhost:5432:extra",
        "15432::5432",
        "15432:local host:5432",
        "[::1:15432:localhost:5432",
        "[::1]x:15432:localhost:5432",
        "15432:localhost:0",
        "70000:localhost:5432",
    ],
)
def test_bad_forward_specs_are_refused(spec):
    with pytest.raises(ValidationError):
        parse_forward_spec(spec, "local")


def test_dynamic_specs_reject_a_remote_half():
    with pytest.raises(ValidationError):
        parse_forward_spec("1080:localhost:80", "dynamic")


def test_unknown_kind_is_refused():
    with pytest.raises(ValidationError, match="kind"):
        parse_forward_spec("1:localhost:2", "sideways")


def test_forward_specs_render_back_to_an_ssh_spelling():
    entry = parse_forward_spec("[::1]:9000:db:5432", "local")
    assert format_forward_spec(entry) == "[::1]:9000:db:5432"
    assert format_forward_spec(parse_forward_spec("1080", "dynamic")) == "127.0.0.1:1080"


# --- profiles ---------------------------------------------------------------------


def test_profiles_are_saved_and_resolved_by_name_alias_and_id(database):
    profile = database.create_tunnel(
        "db1", name="db1-local", kind="local", forward="15432:localhost:5432"
    )
    assert profile["server"] == "db1"
    assert profile["forward"] == "127.0.0.1:15432:localhost:5432"
    assert database.get_tunnel("db1-local")["id"] == profile["id"]
    assert database.get_tunnel("DB1:DB1-LOCAL")["id"] == profile["id"]
    assert database.get_tunnel(profile["id"])["id"] == profile["id"]
    with pytest.raises(NotFoundError):
        database.get_tunnel("nope")


def test_profile_names_are_unique_across_hosts(database):
    database.create_tunnel("db1", name="shared", kind="local", forward="15432:localhost:5432")
    with pytest.raises(ConflictError, match="already exists"):
        database.create_tunnel("web1", name="Shared", kind="local", forward="5432:localhost:5432")


def test_a_host_cannot_hold_two_profiles_for_the_same_forward(database):
    database.create_tunnel("db1", name="one", kind="local", forward="15432:localhost:5432")
    with pytest.raises(ConflictError, match="already forwards"):
        database.create_tunnel("db1", name="two", kind="local", forward="15432:localhost:5432")
    # The same forward on another host is a different thing and is allowed.
    database.create_tunnel("web1", name="two", kind="local", forward="15432:localhost:5432")


def test_dynamic_and_local_profiles_can_share_a_port(database):
    database.create_tunnel("db1", name="socks", kind="dynamic", forward="1080")
    database.create_tunnel("db1", name="web", kind="local", forward="1080:localhost:80")


def test_profiles_list_for_one_host_and_for_all(database):
    database.create_tunnel("db1", name="a", kind="local", forward="1:localhost:1")
    database.create_tunnel("web1", name="b", kind="local", forward="2:localhost:2")
    assert [entry["server"] for entry in database.list_tunnels("db1")] == ["db1"]
    assert sorted(entry["name"] for entry in database.list_tunnels()) == ["a", "b"]


def test_profile_edit_renames_and_reports_a_kind_change(database):
    profile = database.create_tunnel(
        "db1", name="db1-local", kind="local", forward="15432:localhost:5432"
    )
    renamed = database.update_tunnel(profile["id"], name="db1-pg", notes="primary")
    assert renamed["name"] == "db1-pg"
    assert renamed["notes"] == "primary"
    assert renamed["forward"] == "127.0.0.1:15432:localhost:5432"
    # Changing the kind silently would reinterpret the stored ports, so it needs
    # an explicit spelling for the new kind.
    with pytest.raises(ValidationError, match="needs a new FORWARD"):
        database.update_tunnel(profile["id"], kind="dynamic")
    changed = database.update_tunnel(profile["id"], kind="dynamic", forward="1080")
    assert changed["kind"] == "dynamic"
    assert changed["remote_host"] is None


def test_profile_edit_moves_between_hosts(database):
    profile = database.create_tunnel(
        "db1", name="db1-local", kind="local", forward="15432:localhost:5432"
    )
    moved = database.update_tunnel(profile["id"], server_identifier="web1")
    assert moved["server"] == "web1"


def test_profile_delete_returns_the_removed_row(database):
    profile = database.create_tunnel(
        "db1", name="db1-local", kind="local", forward="15432:localhost:5432"
    )
    removed = database.delete_tunnel("db1-local")
    assert removed["id"] == profile["id"]
    with pytest.raises(NotFoundError):
        database.get_tunnel("db1-local")


def test_deleting_a_host_takes_its_profiles_with_it(database):
    database.create_tunnel("db1", name="db1-local", kind="local", forward="15432:localhost:5432")
    database.delete_server("db1")
    assert database.list_tunnels() == []


def test_structured_fields_create_the_same_profile_as_a_spelling(database):
    by_string = database.create_tunnel(
        "db1", name="one", kind="local", forward="15432:localhost:5432"
    )
    by_fields = database.create_tunnel(
        "web1",
        name="two",
        kind="local",
        bind_port=15432,
        remote_host="localhost",
        remote_port=5432,
    )
    assert by_fields["forward"] == by_string["forward"] == "127.0.0.1:15432:localhost:5432"
    with pytest.raises(ValidationError, match="remote host"):
        database.create_tunnel("db1", name="three", kind="local", bind_port=15432)


# --- command construction ---------------------------------------------------------


def test_the_command_is_a_background_ssh_with_the_saved_forward(database):
    profile = database.create_tunnel(
        "db1", name="db1-local", kind="local", forward="15432:localhost:5432"
    )
    server = database.get_server("db1")
    command = tunnels.tunnel_command(database, server, profile, timeout=7, ssh_binary="/bin/ssh")

    assert command[0] == "/bin/ssh"
    assert command[-1] == "db1"
    assert "-N" in command
    assert "-L" in command
    assert command[command.index("-L") + 1] == "127.0.0.1:15432:localhost:5432"
    options = [command[index + 1] for index, item in enumerate(command[:-1]) if item == "-o"]
    assert "ExitOnForwardFailure=yes" in options
    # A tunnel must never be the place where host-key checking gets relaxed.
    assert "StrictHostKeyChecking=yes" in options
    assert f"ControlPath={tunnels.control_path(profile['id'])}" in options
    assert "ConnectTimeout=7" in options


@pytest.mark.parametrize(("kind", "flag"), [("local", "-L"), ("remote", "-R"), ("dynamic", "-D")])
def test_each_kind_maps_to_its_ssh_flag(database, kind, flag):
    forward = "1080" if kind == "dynamic" else "15432:localhost:5432"
    profile = database.create_tunnel("db1", name="t", kind=kind, forward=forward)
    command = tunnels.tunnel_command(
        database, database.get_server("db1"), profile, timeout=5, ssh_binary="/bin/ssh"
    )
    assert flag in command
    assert command[command.index(flag) + 1] == profile["forward"]


# --- runtime state ----------------------------------------------------------------


def test_state_file_is_private_and_survives_a_reread(database):
    path = tunnels._state_path()
    tunnels._write_state({"abc": {"id": "abc", "pid": 1}})
    assert tunnels._read_state()["abc"]["pid"] == 1
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A corrupt state file must not take the CLI down: a lost record is a stale
    # record, and status prunes it anyway.
    path.write_text("{not json", encoding="utf-8")
    assert tunnels._read_state() == {}


def test_state_lock_is_released_even_when_the_body_raises(database):
    with pytest.raises(RuntimeError):
        with tunnels._state_lock():
            raise RuntimeError("boom")
    with tunnels._state_lock():
        pass


def test_a_dead_pid_is_not_running():
    assert tunnels.process_alive(999_999_999) is False
    assert tunnels.process_alive(0) is False


@POSIX_ONLY
def test_a_recycled_pid_is_not_treated_as_our_tunnel(database):
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        record = {"id": "someone-else", "pid": child.pid}
        # Alive, but its command line carries no marker for this tunnel.
        assert tunnels.process_alive(child.pid) is True
        assert tunnels._record_is_running(record) is False
    finally:
        child.kill()
        child.wait(timeout=10)


# --- open / close -----------------------------------------------------------------


def _fake_ssh(path: Path, *, body: str) -> Path:
    """A stand-in for `ssh -N` that keeps the caller's argv, like ssh does.

    It must not `exec` anything: these tests assert that the ControlPath marker is
    still readable from the live process's command line, which is exactly what a
    real, non-exec'ing ssh leaves behind.
    """
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_ssh_that_stays_up(path: Path) -> Path:
    return _fake_ssh(path, body="while :; do sleep 1; done")


@POSIX_ONLY
def test_open_records_the_process_and_close_stops_it(database, tmp_path):
    binary = _fake_ssh_that_stays_up(tmp_path / "fake-ssh")
    profile = database.create_tunnel(
        "db1", name="db1-local", kind="local", forward="15432:localhost:5432"
    )
    opened = tunnels.open_tunnel(database, profile["name"], ssh_binary=str(binary))
    try:
        assert opened["opened"] is True
        assert tunnels.process_alive(opened["pid"]) is True
        # The marker really is on the command line, which is what makes the
        # liveness check more than a pid guess.
        assert str(tunnels.control_path(profile["id"])) in (
            tunnels.process_command_line(opened["pid"]) or ""
        )
        status = tunnels.summarize(database)
        assert [(row["name"], row["running"]) for row in status] == [("db1-local", True)]
        assert not any(row.get("orphan") for row in status)

        # Opening twice is a no-op rather than a second process on the same port.
        again = tunnels.open_tunnel(database, profile["name"], ssh_binary=str(binary))
        assert again["already_open"] is True
        assert again["pid"] == opened["pid"]

        closed = tunnels.close_tunnel(database, profile["name"])
        assert closed["closed"] is True
        assert tunnels.process_alive(opened["pid"]) is False
        assert tunnels.summarize(database)[0]["running"] is False
        # Closing a saved profile that is not running is a no-op, so teardown
        # scripts can be idempotent; a name that does not exist is still an error.
        again_closed = tunnels.close_tunnel(database, profile["name"])
        assert again_closed["closed"] is False
        assert again_closed["already_stopped"] is True
        with pytest.raises(ValidationError, match="not open"):
            tunnels.close_tunnel(database, "no-such-tunnel")
    finally:
        if tunnels.process_alive(opened["pid"]):
            os.kill(opened["pid"], 9)


@POSIX_ONLY
def test_a_tunnel_that_dies_at_once_reports_the_reason(database, tmp_path):
    binary = _fake_ssh(
        tmp_path / "fail-ssh",
        body='echo "ssh: connect to host db1.example port 22: Connection refused" >&2; exit 255',
    )
    database.create_tunnel("db1", name="db1-local", kind="local", forward="15432:localhost:5432")
    with pytest.raises(SSHError, match="Connection refused"):
        tunnels.open_tunnel(database, "db1-local", ssh_binary=str(binary))
    assert tunnels.tunnel_state() == []


@POSIX_ONLY
def test_a_missing_ssh_binary_is_reported(database, tmp_path):
    database.create_tunnel("db1", name="db1-local", kind="local", forward="15432:localhost:5432")
    with pytest.raises(SSHError, match="could not run ssh"):
        tunnels.open_tunnel(
            database, "db1-local", ssh_binary=str(tmp_path / "definitely-not-ssh")
        )


@POSIX_ONLY
def test_a_live_record_without_a_profile_is_reported_and_closeable(database, tmp_path):
    import subprocess

    binary = _fake_ssh_that_stays_up(tmp_path / "fake-ssh")
    profile = database.create_tunnel(
        "db1", name="db1-local", kind="local", forward="15432:localhost:5432"
    )
    opened = tunnels.open_tunnel(database, profile["name"], ssh_binary=str(binary))
    try:
        # Simulate a profile deleted out from under a running process.
        database.delete_tunnel(profile["id"])
        orphans = [row for row in tunnels.summarize(database) if row.get("orphan")]
        assert [row["name"] for row in orphans] == ["db1-local"]
        closed = tunnels.close_tunnel(database, "db1-local")
        assert closed["closed"] is True
        assert tunnels.tunnel_state() == []
    finally:
        if tunnels.process_alive(opened["pid"]):
            os.kill(opened["pid"], 9)


@POSIX_ONLY
def test_removing_a_host_closes_its_tunnels(database, tmp_path):
    binary = _fake_ssh_that_stays_up(tmp_path / "fake-ssh")
    profile = database.create_tunnel(
        "db1", name="db1-local", kind="local", forward="15432:localhost:5432"
    )
    opened = tunnels.open_tunnel(database, profile["name"], ssh_binary=str(binary))
    try:
        results = tunnels.close_for_server(database, "db1")
        assert [item["closed"] for item in results] == [True]
        assert tunnels.process_alive(opened["pid"]) is False
    finally:
        if tunnels.process_alive(opened["pid"]):
            os.kill(opened["pid"], 9)


@POSIX_ONLY
def test_a_runtime_directory_too_deep_for_openssh_is_named(database, monkeypatch, tmp_path):
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(deep))
    profile = database.create_tunnel(
        "db1", name="db1-local", kind="local", forward="15432:localhost:5432"
    )
    with pytest.raises(SSHError, match="SSM_RUNTIME_DIR"):
        tunnels.open_tunnel(database, profile["name"], ssh_binary="/bin/true")
