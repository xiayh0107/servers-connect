from __future__ import annotations

import os
import stat
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ssh_server_manager.db import Database
from ssh_server_manager.ssh_config import SSHConfigError, includes_target, render_config
from ssh_server_manager import ssh_config
from ssh_fixture_permissions import private_ssh_fixture_file


def test_rendered_config_contains_no_secret_and_includes_original(tmp_path):
    database = Database(tmp_path / "manager.db")
    credential = database.create_credential(label="Password", kind="password", has_secret=True)
    database.create_server(
        alias="box",
        hostname="box.example",
        port=2200,
        username="alice",
        credential_id=credential["id"],
    )
    original = tmp_path / "config"
    original.write_text("Host *\n  ServerAliveInterval 30\n", encoding="utf-8")
    managed = tmp_path / "managed.conf"
    render_config(database, destination=managed, original=original)
    content = managed.read_text(encoding="utf-8")
    assert "Host box" in content
    assert "PreferredAuthentications keyboard-interactive,password" in content
    assert "Include " in content
    assert "secret" not in content.casefold()
    if os.name != "nt":
        assert stat.S_IMODE(managed.stat().st_mode) == 0o600


def test_include_cycle_is_rejected(tmp_path):
    database = Database(tmp_path / "manager.db")
    managed = tmp_path / "managed.conf"
    original = tmp_path / "config"
    original.write_text(f"Include {managed}\n", encoding="utf-8")
    assert includes_target(original, managed)
    with pytest.raises(SSHConfigError, match="cycle"):
        render_config(database, destination=managed, original=original)


def test_original_defaults_and_external_aliases_apply_before_last_host(tmp_path):
    ssh = shutil.which("ssh")
    if not ssh:
        pytest.skip("OpenSSH is unavailable")
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="alpha", hostname="127.0.0.1", username="fixture")
    database.create_server(alias="omega", hostname="127.0.0.2", username="fixture")
    original = tmp_path / "original.conf"
    known_hosts = tmp_path / "known_hosts"
    original.write_text(
        f'Host *\n  UserKnownHostsFile "{known_hosts.as_posix()}"\n'
        "Host external-jump\n  HostName 127.0.0.3\n  Port 22022\n",
        encoding="utf-8",
    )
    private_ssh_fixture_file(original)
    managed = render_config(database, destination=tmp_path / "managed.conf", original=original)

    def effective(alias):
        result = subprocess.run(
            [ssh, "-G", "-F", str(managed), alias], capture_output=True,
            text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)

    assert effective("alpha")["userknownhostsfile"] == known_hosts.as_posix()
    external = effective("external-jump")
    assert external["hostname"] == "127.0.0.3"
    assert external["port"] == "22022"


def test_render_serializes_snapshot_and_replace_with_concurrent_edit(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="old.example", username="alice")
    managed = tmp_path / "managed.conf"
    original = tmp_path / "missing.conf"
    snapshot = Event()
    release = Event()
    editing = Event()
    updated = Event()
    render_server = ssh_config._render_server

    def pause(server, credential):
        snapshot.set()
        assert release.wait(timeout=5)
        return render_server(server, credential)

    def edit():
        editing.set()
        database.update_server("box", hostname="new.example")
        updated.set()
        render_config(database, destination=managed, original=original)

    monkeypatch.setattr(ssh_config, "_render_server", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        render = pool.submit(render_config, database, destination=managed, original=original)
        assert snapshot.wait(timeout=5)
        change = pool.submit(edit)
        assert editing.wait(timeout=5)
        try:
            assert not updated.wait(timeout=.05)
        finally:
            release.set()
        render.result(timeout=5)
        change.result(timeout=5)
    assert "HostName new.example" in managed.read_text()
    assert "old.example" not in managed.read_text()

