from __future__ import annotations

from pathlib import Path

from ssh_server_manager import __version__
from ssh_server_manager.cli import skill_link_status


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
