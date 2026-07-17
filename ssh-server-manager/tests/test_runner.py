from __future__ import annotations

import subprocess

from ssh_server_manager.db import Database
import pytest

from ssh_server_manager.cli import build_parser, normalize_remote_command
from ssh_server_manager.validation import ValidationError
from ssh_server_manager.ssh_runner import SSHError, SSHRunner


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


def test_connect_without_a_tty_fails_fast_with_guidance(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path))
    from ssh_server_manager.cli import main

    exit_code = main(["connect", "box"])

    assert exit_code == 2
    stderr = capsys.readouterr().err
    assert "requires a terminal" in stderr
    assert "serverctl exec" in stderr


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


def test_sftp_directory_listing_is_structured_and_keeps_host_key_checks(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))

    listing = """Remote working directory: /home/alice/project
drwxr-xr-x    ? alice users        4096 Jul 16 12:04 .
drwx------    ? alice users        4096 Jul 15 09:00 ..
-rw-r--r--    ? alice users          12 Jul 16 11:52 report final.txt
drwxr-xr-x    ? alice users        4096 Jul 14 08:11 src
lrwxrwxrwx    ? alice users           3 Jul 14 08:12 current
-rw-------    ? alice users          40 Jul 13 2025 .env.example
"""

    def fake_run(command, **kwargs):
        assert command[0] == "sftp"
        assert command[-1] == "box"
        assert "StrictHostKeyChecking=yes" in command
        assert "BatchMode=no" in command
        assert '@cd "/home/alice/project"' in kwargs["input"]
        assert kwargs["env"].get("SSH_ASKPASS_REQUIRE") is None
        return subprocess.CompletedProcess(command, 0, stdout=listing, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp").list_directory(
        "box", "/home/alice/project"
    )

    assert result["path"] == "/home/alice/project"
    assert result["parent"] == "/home/alice"
    assert result["total"] == 4
    assert [entry["name"] for entry in result["entries"]] == [
        "src",
        ".env.example",
        "current",
        "report final.txt",
    ]
    assert result["entries"][0]["type"] == "directory"
    assert result["entries"][0]["path"] == "/home/alice/project/src"
    assert result["entries"][1]["hidden"] is True
    assert result["entries"][2]["type"] == "symlink"
    assert result["connection_checked_at"]
    assert database.get_server("box")["last_test_status"] == "ok"


def test_sftp_directory_listing_quotes_paths_and_rejects_command_injection(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))

    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid paths must fail before SFTP starts")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValidationError, match="control characters"):
        SSHRunner(database, sftp_binary="sftp").list_directory("box", "/tmp\n@rm escaped")
    assert called is False
    assert SSHRunner._directory_batch('/tmp/a "quoted" path').startswith(
        '@cd "/tmp/a \\"quoted\\" path"'
    )


def test_sftp_directory_listing_treats_diagnostics_as_failure(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="", stderr="realpath /missing: No such file"
        ),
    )
    with pytest.raises(SSHError, match="No such file"):
        SSHRunner(database, sftp_binary="sftp").list_directory("box", "/missing")
    assert database.get_server("box")["last_test_status"] == "ok"


def test_sftp_connection_failure_updates_host_status(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 255, stdout="", stderr="Connection refused"
        ),
    )

    with pytest.raises(SSHError, match="Connection refused"):
        SSHRunner(database, sftp_binary="sftp").list_directory("box", "~")

    server = database.get_server("box")
    assert server["last_test_status"] == "failed"
    assert server["last_test_error_code"] == "connection-refused"


def test_sftp_directory_listing_allows_a_server_login_banner(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="Remote working directory: /home/alice\n",
            stderr="Authorized users only. Activity may be monitored.\n",
        ),
    )
    result = SSHRunner(database, sftp_binary="sftp").list_directory("box", "~")
    assert result["path"] == "/home/alice"
    assert result["entries"] == []
