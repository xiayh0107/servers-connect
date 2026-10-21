"""Mosh support: capability detection and the command it would run.

CI has no mosh client, so this pins the parts that matter and can be checked
without one: the capability report names what is missing, and the command it
would build reuses the managed config and keeps host-key verification on.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from ssh_server_manager import mosh
from ssh_server_manager.db import Database
from ssh_server_manager.ssh_runner import SSHError


@pytest.fixture()
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", username="alice")
    return database


def test_a_missing_client_is_reported_with_a_next_step(monkeypatch):
    monkeypatch.setattr(mosh.shutil, "which", lambda name: None)
    capability = mosh.mosh_capability()
    assert capability["ok"] is False
    assert "install" in capability["message"]
    assert "mosh.org" in capability["message"]


def test_an_installed_client_is_reported(monkeypatch, tmp_path):
    binary = tmp_path / "mosh"
    binary.write_text("#!/bin/sh\necho 'mosh 1.4.0'\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(mosh.shutil, "which", lambda name: str(binary))
    capability = mosh.mosh_capability()
    if os.name == "nt":
        # The Windows client is deliberately not offered.
        assert capability["ok"] is False
        assert "not supported" in capability["message"]
    else:
        assert capability["ok"] is True
        assert capability["path"] == str(binary)
        assert capability["version"] == "mosh 1.4.0"


def test_require_mosh_raises_the_capability_message(monkeypatch):
    monkeypatch.setattr(mosh.shutil, "which", lambda name: None)
    with pytest.raises(SSHError, match="mosh is not installed"):
        mosh.require_mosh()


def test_the_command_bootstraps_over_the_managed_config(database, monkeypatch):
    monkeypatch.setattr(mosh.shutil, "which", lambda name: "/usr/local/bin/mosh")
    server = database.get_server("box")
    command = mosh.mosh_command(database, server, ssh_binary="/usr/bin/ssh")

    assert command[0] == "/usr/local/bin/mosh"
    assert command[1] == "--ssh"
    bootstrap = command[2]
    assert bootstrap.startswith("/usr/bin/ssh -F ")
    assert "-o StrictHostKeyChecking=yes" in bootstrap
    # mosh resolves the alias, not user@host: the managed config owns the rest.
    assert command[3] == "box"
    assert "alice@" not in " ".join(command)


def test_a_missing_client_does_not_silently_fall_back(database, monkeypatch):
    """A user who asked for mosh must not get a plain ssh session instead."""
    monkeypatch.setattr(mosh.shutil, "which", lambda name: None)
    with pytest.raises(SSHError):
        mosh.connect(database, "box")


def test_connect_runs_the_client_with_the_askpass_environment(database, monkeypatch, tmp_path):
    binary = tmp_path / "mosh"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(mosh.shutil, "which", lambda name: str(binary))
    observed = {}

    def fake_call(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs.get("env")
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)
    monkeypatch.setitem(os.environ, "LANG", "C")
    assert mosh.connect(database, "box") == 0
    assert observed["command"][0] == str(binary)
    # A non-UTF-8 environment is corrected rather than left for mosh to reject.
    assert observed["env"]["LC_ALL"] == "en_US.UTF-8"


def test_connect_keeps_a_utf8_locale_the_user_already_has(database, monkeypatch, tmp_path):
    binary = tmp_path / "mosh"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(mosh.shutil, "which", lambda name: str(binary))
    observed = {}

    def fake_call(command, **kwargs):
        observed["env"] = kwargs.get("env")
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)
    monkeypatch.setitem(os.environ, "LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    mosh.connect(database, "box")
    assert "LC_ALL" not in observed["env"]


def test_doctor_reports_a_mosh_row(tmp_path, monkeypatch, capsys):
    import json

    from ssh_server_manager.cli import main

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SSM_DATA_DIR", str(data))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    monkeypatch.setattr(mosh.shutil, "which", lambda name: None)
    main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "mosh" in payload
    assert payload["mosh"]["ok"] is False
