from __future__ import annotations

import json
from pathlib import Path

import pytest

from ssh_server_manager import __version__
from ssh_server_manager.cli import main, skill_link_status
from ssh_server_manager.db import Database, NotFoundError
from ssh_server_manager.validation import ValidationError


def make_skill_copy(root: Path, version: str) -> Path:
    marker = root / "ssh-server-manager" / "scripts" / "ssh_server_manager" / "__init__.py"
    marker.parent.mkdir(parents=True)
    marker.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return root


def test_skill_link_status_reports_current_and_stale_copies(tmp_path):
    current = make_skill_copy(tmp_path / "claude-skills", __version__)
    stale = make_skill_copy(tmp_path / "codex-skills", "0.0.1")
    missing = tmp_path / "not-installed"

    status = skill_link_status([current, stale, missing])

    assert status["ok"] is True
    assert status["linked"] == 2
    assert [entry["version"] for entry in status["stale"]] == ["0.0.1"]
    assert "re-run install.sh" in status["message"]
    assert str(stale / "ssh-server-manager") in status["message"]


def test_skill_link_status_is_quiet_when_everything_matches(tmp_path):
    current = make_skill_copy(tmp_path / "skills", __version__)

    status = skill_link_status([current])

    assert status == {"ok": True, "linked": 1}


def test_skill_link_status_handles_no_links(tmp_path):
    status = skill_link_status([tmp_path / "empty"])

    assert status == {"ok": True, "linked": 0}


def test_cli_registers_and_resolves_one_skill_for_multiple_hosts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    database = Database()
    database.create_server(alias="gpu-a", hostname="a.example", port=22, username="alice")
    database.create_server(alias="gpu-b", hostname="b.example", port=22, username="alice")
    skill_dir = tmp_path / "skills" / "gpu-operations"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: gpu-operations\ndescription: Operate GPU jobs on attached hosts.\n---\n# Instructions\n",
        encoding="utf-8",
    )

    assert main(
        [
            "skill",
            "add",
            str(skill_dir),
            "--server",
            "gpu-a",
            "--server",
            "gpu-b",
            "--json",
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["name"] == "gpu-operations"
    assert [server["alias"] for server in created["servers"]] == ["gpu-a", "gpu-b"]

    assert main(["skill", "resolve", "gpu-a", "gpu-b", "--json"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["ok"] is True
    assert resolved["skills"][0]["applies_to"] == ["gpu-a", "gpu-b"]
    assert [host["alias"] for host in resolved["hosts"]] == ["gpu-a", "gpu-b"]

    assert main(["skill", "remove", "gpu-operations", "--yes", "--json"]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["error"] == "ConflictError"

    assert main(["skill", "detach", "gpu-operations", "gpu-a", "gpu-b", "--json"]) == 0
    capsys.readouterr()
    assert main(["skill", "remove", "gpu-operations", "--yes", "--json"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["name"] == "gpu-operations"
    assert (skill_dir / "SKILL.md").exists()


def test_cli_skill_resolve_reports_missing_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    database = Database()
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    skill_dir = tmp_path / "skills" / "box-guide"
    skill_dir.mkdir(parents=True)
    manifest = skill_dir / "SKILL.md"
    manifest.write_text(
        "---\nname: box-guide\ndescription: Local operating guide.\n---\n",
        encoding="utf-8",
    )
    assert main(["skill", "add", str(manifest), "--server", "box", "--json"]) == 0
    capsys.readouterr()
    manifest.unlink()

    assert main(["skill", "resolve", "box", "--json"]) == 1
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["ok"] is False
    assert resolved["skills"][0]["status"] == "missing"


def test_cli_saves_and_resolves_working_directories(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    database = Database()
    database.create_server(alias="web1", hostname="a.example", port=22, username="deploy")
    database.create_server(alias="web2", hostname="b.example", port=22, username="deploy")

    assert main(["path", "add", "web1", "/srv/app", "--label", "app", "--note", "root", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["path"] == "/srv/app"
    assert created["notes"] == "root"
    assert created["last_used_at"] is None

    assert main(["path", "add", "web2", "/srv/app", "--label", "app", "--json"]) == 0
    capsys.readouterr()
    assert main(["path", "add", "web1", "/var/log/app", "--label", "logs", "--json"]) == 0
    capsys.readouterr()

    assert main(["path", "list", "web1", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["label"] for item in listed] == ["app", "logs"]

    # Hosts that share a layout are grouped, so an agent can act on both at once.
    assert main(["path", "resolve", "web1", "web2", "--json"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["ok"] is True
    shared = {item["path"]: item["applies_to"] for item in resolved["paths"]}
    assert shared["/srv/app"] == ["web1", "web2"]
    assert shared["/var/log/app"] == ["web1"]

    # A label collides case-insensitively within one host but not across hosts.
    assert main(["path", "add", "web1", "/elsewhere", "--label", "APP", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "ConflictError"

    assert main(["path", "edit", "web1", "app", "--label", "application", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["label"] == "application"

    assert main(["path", "remove", "web1", "application", "--yes", "--json"]) == 0
    capsys.readouterr()
    assert main(["path", "list", "web1", "--json"]) == 0
    assert [item["label"] for item in json.loads(capsys.readouterr().out)] == ["logs"]


def test_cli_rejects_an_empty_saved_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    Database().create_server(alias="box", hostname="box.example", port=22, username="alice")

    # An empty remote path means "home" when browsing, but saving it would record
    # a directory that names nothing.
    assert main(["path", "add", "box", "", "--label", "nowhere", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "ValidationError"


def test_transfer_resolution_picks_the_remote_side():
    from ssh_server_manager.cli import resolve_transfer

    assert resolve_transfer("web1:/srv/app.yml", "./local.yml") == (
        "download",
        "web1",
        "/srv/app.yml",
        "./local.yml",
    )
    assert resolve_transfer("./local.yml", "web1:/srv/app.yml") == (
        "upload",
        "web1",
        "/srv/app.yml",
        "./local.yml",
    )
    # A Windows drive letter is a local path even though "C" is a legal alias.
    with pytest.raises(ValidationError, match="one side must name a host"):
        resolve_transfer("C:\\Users\\me\\f.txt", "D:\\backup\\f.txt")
    with pytest.raises(ValidationError, match="host-to-host"):
        resolve_transfer("web1:/a", "web2:/b")
    with pytest.raises(ValidationError, match="one side must name a host"):
        resolve_transfer("./a", "./b")


def test_local_destination_resolves_directories_and_guards_overwrites(tmp_path):
    from ssh_server_manager.validation import validate_local_destination

    # An existing directory receives the remote basename, the way cp behaves.
    assert validate_local_destination(tmp_path, remote_name="config.yml") == (
        tmp_path / "config.yml"
    )
    existing = tmp_path / "taken.yml"
    existing.write_text("keep me", encoding="utf-8")
    with pytest.raises(ValidationError, match="--force"):
        validate_local_destination(existing, remote_name="taken.yml")
    assert validate_local_destination(existing, remote_name="taken.yml", force=True) == existing
    with pytest.raises(ValidationError, match="destination directory does not exist"):
        validate_local_destination(tmp_path / "absent" / "f.yml", remote_name="f.yml")


def test_cli_ui_credential_deep_links_into_the_server_dialog(tmp_path, monkeypatch, capsys):
    pytest.importorskip("uvicorn")
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    database = Database()
    database.create_server(alias="web1", hostname="a.example", port=22, username="deploy")
    url_file = tmp_path / "launch-url"
    observed = {}

    def fake_run(*args, **kwargs):
        observed["url"] = url_file.read_text(encoding="utf-8").strip()

    monkeypatch.setattr("uvicorn.run", fake_run)

    assert main(["ui", "--credential", "web1", "--no-open", "--url-file", str(url_file)]) == 0
    assert "token=" in observed["url"]
    assert observed["url"].endswith("#servers/web1/credential")
    assert "token=" not in capsys.readouterr().out

    assert main(["ui", "--credential", "ghost", "--no-open", "--url-file", str(url_file)]) == 2
    assert "server not found" in capsys.readouterr().err


def test_cli_server_add_password_credential_in_one_shot(tmp_path, monkeypatch, capsys):
    import getpass
    from types import SimpleNamespace

    from ssh_server_manager.vault import MemoryVault

    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    vault = MemoryVault()
    monkeypatch.setattr("ssh_server_manager.cli.get_vault", lambda: vault)
    prompts = iter(["s3cret", "s3cret"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(prompts))
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))

    assert (
        main(
            [
                "server",
                "add",
                "web1",
                "--hostname",
                "a.example",
                "--username",
                "deploy",
                "--password-credential",
                "web-pass",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    database = Database()
    server = database.get_server("web1")
    credential = database.get_credential("web-pass")
    assert server["credential_id"] == credential["id"]
    assert credential["kind"] == "password"
    assert credential["has_secret"] is True
    assert vault.get_secret(credential["id"], "password") == "s3cret"

    # Without a TTY the hidden prompt cannot run, so fail fast with a pointer
    # to the browser path instead of hanging an agent tool call.
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert (
        main(
            [
                "server",
                "add",
                "web2",
                "--hostname",
                "b.example",
                "--username",
                "ops",
                "--password-credential",
                "ops-pass",
                "--json",
            ]
        )
        == 2
    )
    refused = json.loads(capsys.readouterr().out)
    assert "ui --credential" in refused["message"]
    with pytest.raises(NotFoundError):
        database.get_server("web2")

    # --credential and --password-credential are mutually exclusive.
    with pytest.raises(SystemExit):
        main(
            [
                "server",
                "add",
                "web3",
                "--hostname",
                "c.example",
                "--username",
                "ops",
                "--credential",
                "web-pass",
                "--password-credential",
                "other",
            ]
        )
    capsys.readouterr()


def test_cli_duplicate_conflicts_point_to_the_edit_commands(tmp_path, monkeypatch, capsys):
    import getpass

    from ssh_server_manager.vault import MemoryVault

    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    monkeypatch.setattr("ssh_server_manager.cli.get_vault", lambda: MemoryVault())

    assert main(["server", "add", "t_rna", "--hostname", "h.example", "--username", "ops", "--json"]) == 0
    capsys.readouterr()
    assert main(["server", "add", "t_rna", "--hostname", "h2.example", "--username", "ops", "--json"]) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert "server alias already exists" in conflict["message"]
    assert "serverctl server edit t_rna" in conflict["message"]

    assert main(["credential", "add-agent", "trna", "--json"]) == 0
    capsys.readouterr()
    assert main(["credential", "add-agent", "trna", "--json"]) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert "credential label already exists" in conflict["message"]
    assert "serverctl credential edit trna" in conflict["message"]

    # A taken label fails before the hidden prompt runs, so users correcting a
    # typo never type the secret twice for nothing.
    def forbidden_prompt(prompt):
        raise AssertionError("getpass must not run for a taken label")

    monkeypatch.setattr(getpass, "getpass", forbidden_prompt)
    assert main(["credential", "add-password", "trna", "--json"]) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert "--replace-secret" in conflict["message"]


def test_add_help_text_points_to_the_edit_commands(capsys):
    with pytest.raises(SystemExit) as server_help:
        main(["server", "add", "--help"])
    assert server_help.value.code == 0
    assert "serverctl server edit" in capsys.readouterr().out

    with pytest.raises(SystemExit) as credential_help:
        main(["credential", "add-password", "--help"])
    assert credential_help.value.code == 0
    assert "serverctl credential edit" in capsys.readouterr().out
