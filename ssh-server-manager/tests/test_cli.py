from __future__ import annotations

import json
from pathlib import Path

from ssh_server_manager import __version__
from ssh_server_manager.cli import main, skill_link_status
from ssh_server_manager.db import Database


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
