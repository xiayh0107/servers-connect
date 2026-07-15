from __future__ import annotations

import os
import stat

import pytest

from ssh_server_manager.db import Database
from ssh_server_manager.ssh_config import SSHConfigError, includes_target, render_config


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

