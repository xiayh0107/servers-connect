from __future__ import annotations

import json
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


def test_diagnose_runs_profile_ssh_sftp_and_remote_checks(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[0])
        if command[0] == "sftp":
            return subprocess.CompletedProcess(
                command, 0, stdout="Remote working directory: /home/alice\n", stderr=""
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '__ssm_hostname=box\n__ssm_os_pretty="Ubuntu 24.04 LTS"\n'
                "__ssm_os_id=ubuntu\n__ssm_os_version=\"24.04\"\n"
                "__ssm_kernel_name=Linux\n__ssm_kernel=Linux 5.15 x86_64\n"
                "__ssm_arch=x86_64\n__ssm_uptime=up 1 day\n"
                "__ssm_load=0.10 0.20 0.30\n__ssm_cpu_cores=8\n"
                "__ssm_cpu_model=Intel Xeon\n__ssm_memory=Mem: 16Gi 4Gi 8Gi\n"
                "__ssm_disk=/dev/vda 100G 20G 80G 20% /\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp").diagnose("box")

    assert result["overall"] == "ok"
    assert result["summary"] == {"total": 4, "passed": 4, "failed": 0, "warnings": 0, "skipped": 0}
    assert [check["id"] for check in result["checks"]] == ["profile", "ssh", "sftp", "remote"]
    assert result["checks"][2]["details"]["home_directory"] == "/home/alice"
    remote = result["checks"][3]["details"]
    assert remote["hostname"] == "box"
    assert remote["os"] == "Ubuntu 24.04 LTS"
    assert remote["os_id"] == "ubuntu"
    assert remote["os_version"] == "24.04"
    assert remote["os_family"] == "debian"
    assert remote["package_manager"] == "apt"
    assert remote["arch"] == "x86_64"
    assert remote["cpu_cores"] == "8"
    assert remote["memory"] == "Mem: 16Gi 4Gi 8Gi"
    assert remote["disk"].endswith("20% /")
    assert calls == ["ssh", "sftp", "ssh"]


def test_diagnose_identifies_windows_hosts_without_posix_shell(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="winbox", hostname="win.example", port=22, username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))

    def fake_run(command, **kwargs):
        if command[0] == "sftp":
            return subprocess.CompletedProcess(
                command, 0, stdout="Remote working directory: /C:/Users/alice\n", stderr=""
            )
        remote_command = command[-1]
        if remote_command == "true":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if remote_command.startswith("sh -lc"):
            return subprocess.CompletedProcess(
                command,
                49,
                stdout="",
                stderr="'sh' is not recognized as an internal or external command",
            )
        if remote_command == "cmd.exe /c ver":
            return subprocess.CompletedProcess(
                command, 0, stdout="\nMicrosoft Windows [Version 10.0.20348.2113]\n", stderr=""
            )
        if remote_command == "hostname":
            return subprocess.CompletedProcess(command, 0, stdout="WINBOX\n", stderr="")
        raise AssertionError(f"unexpected remote command: {remote_command}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp").diagnose("winbox")

    remote = next(check for check in result["checks"] if check["id"] == "remote")
    assert remote["status"] == "ok"
    assert remote["details"]["os"] == "Microsoft Windows [Version 10.0.20348.2113]"
    assert remote["details"]["os_id"] == "windows"
    assert remote["details"]["os_family"] == "windows"
    assert remote["details"]["os_version"] == "10.0.20348.2113"
    assert remote["details"]["hostname"] == "WINBOX"


def test_summarize_remote_identity_maps_distro_to_family_and_package_manager():
    from ssh_server_manager.ssh_runner import _summarize_remote_identity

    details = _summarize_remote_identity(
        {
            "hostname": "box",
            "os_pretty": "openSUSE Leap 15.6",
            "os_id": "opensuse-leap",
            "os_version": "15.6",
            "kernel_name": "Linux",
            "kernel": "Linux 6.4.0 x86_64",
            "arch": "x86_64",
        }
    )
    assert details["os"] == "openSUSE Leap 15.6"
    assert details["os_family"] == "suse"
    assert details["package_manager"] == "zypper"


def test_summarize_remote_identity_uses_id_like_for_unknown_derivatives():
    from ssh_server_manager.ssh_runner import _summarize_remote_identity

    details = _summarize_remote_identity(
        {"hostname": "box", "os_id": "acmelinux", "os_like": "rhel fedora", "os_version": "9.4"}
    )
    assert details["os"] == "acmelinux 9.4"
    assert details["os_family"] == "rhel"
    assert details["package_manager"] == "dnf"


def test_summarize_remote_identity_detects_macos_via_sw_vers():
    from ssh_server_manager.ssh_runner import _summarize_remote_identity

    details = _summarize_remote_identity(
        {
            "hostname": "studio",
            "mac_product": "macOS",
            "mac_version": "15.5",
            "brew": "brew",
            "kernel_name": "Darwin",
            "kernel": "Darwin 24.5.0 arm64",
            "arch": "arm64",
        }
    )
    assert details["os"] == "macOS 15.5"
    assert details["os_id"] == "macos"
    assert details["os_version"] == "15.5"
    assert details["os_family"] == "macos"
    assert details["package_manager"] == "brew"
    assert details["kernel"] == "Darwin 24.5.0 arm64"


def test_summarize_remote_identity_falls_back_to_legacy_and_kernel_sources():
    from ssh_server_manager.ssh_runner import _summarize_remote_identity

    legacy = _summarize_remote_identity(
        {"hostname": "old", "os_legacy": "CentOS release 6.10 (Final)", "kernel_name": "Linux"}
    )
    assert legacy["os"] == "CentOS release 6.10 (Final)"
    assert legacy["os_family"] == "rhel"
    assert legacy["package_manager"] == "yum"

    bsd = _summarize_remote_identity(
        {"hostname": "puffy", "kernel_name": "OpenBSD", "kernel": "OpenBSD 7.5 amd64"}
    )
    assert bsd["os"] == "OpenBSD"
    assert bsd["os_family"] == "bsd"
    assert bsd["package_manager"] == "pkg_add"


def test_diagnose_skips_remote_checks_after_ssh_failure(tmp_path, monkeypatch):
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
    result = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp").diagnose("box")

    assert result["overall"] == "failed"
    assert result["summary"]["failed"] == 1
    assert [check["status"] for check in result["checks"]] == ["ok", "failed", "skipped", "skipped"]


def test_cli_server_note_supports_agent_append_workflow(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path))
    Database(tmp_path / "manager.db").create_server(
        alias="box", hostname="box.example", port=22, username="alice"
    )
    from ssh_server_manager.cli import main

    assert main(["server", "note", "box", "--text", "Primary compute host", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["notes"] == "Primary compute host"
    assert main(["server", "note", "box", "--text", "Agent checked disk", "--append", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["notes"] == "Primary compute host\n\nAgent checked disk"


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
