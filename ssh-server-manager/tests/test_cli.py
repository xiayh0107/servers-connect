from __future__ import annotations

import json
from pathlib import Path

import pytest

from ssh_server_manager import __version__
from ssh_server_manager.cli import main, skill_link_status
from ssh_server_manager.db import Database
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
