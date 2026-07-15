from __future__ import annotations

import subprocess

from ssh_server_manager.db import Database
import pytest

from ssh_server_manager.cli import build_parser, normalize_remote_command
from ssh_server_manager.validation import ValidationError
from ssh_server_manager.ssh_runner import SSHRunner


def test_test_classifies_auth_failure(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))

    def fake_run(command, **kwargs):
        assert "StrictHostKeyChecking=yes" in command
        return subprocess.CompletedProcess(command, 255, stdout="", stderr="Permission denied (publickey).")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SSHRunner(database, ssh_binary="ssh").test("box")
    assert result["ok"] is False
    assert result["error_code"] == "authentication-failed"


def test_exec_options_are_valid_before_remote_separator():
    parser = build_parser()
    args = parser.parse_args(["exec", "box", "--stdin-binary", "--reuse", "600", "--shell"])
    assert args.alias == "box"
    assert args.stdin_binary is True
    assert args.reuse == 600
    assert args.shell is True


def test_shell_normalization_is_explicit():
    assert normalize_remote_command(["echo $HOME"], shell=True) == ["sh", "-lc", "echo $HOME"]
    assert normalize_remote_command(["echo", "$HOME"]) == ["echo", "$HOME"]
    with pytest.raises(ValidationError):
        normalize_remote_command(["echo", "one", "two"], shell=True)


def test_exec_accepts_binary_stdin_without_text_coercion(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))

    def fake_run(command, **kwargs):
        assert kwargs["input"] == b"\x00\xff"
        assert kwargs["text"] is False
        return subprocess.CompletedProcess(command, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SSHRunner(database, ssh_binary="ssh").execute(
        "box", ["cat"], stdin_data=b"\x00\xff", capture=True
    )
    assert result["ok"] is True
    assert result["stdout"] == "ok"
