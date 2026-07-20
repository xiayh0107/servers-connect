from __future__ import annotations

import json
from pathlib import Path

import pytest

import ssh_server_manager.validation as validation_module
from ssh_server_manager.db import Database
from ssh_server_manager.service import (
    MAX_SKILL_FRONTMATTER_BYTES,
    CredentialService,
    SkillService,
)
from ssh_server_manager.validation import ValidationError
from ssh_server_manager.vault import MemoryVault


def write_skill(path, name: str, description: str = "Host-specific guidance"):
    path.mkdir(parents=True, exist_ok=True)
    manifest = path / "SKILL.md"
    manifest.write_text(
        "---\n"
        f"name: {json.dumps(name)}\n"
        f"description: {json.dumps(description)}\n"
        "---\n\n"
        "# Instructions\n",
        encoding="utf-8",
    )
    return manifest


def test_password_never_enters_database(tmp_path):
    database = Database(tmp_path / "manager.db")
    vault = MemoryVault()
    service = CredentialService(database, vault)
    credential = service.create_password("Production password", "do-not-persist")
    assert credential["has_secret"] is True
    assert "do-not-persist" not in (tmp_path / "manager.db").read_bytes().decode("utf-8", errors="ignore")
    assert service.reveal(credential["id"])["value"] == "do-not-persist"


def test_key_path_and_passphrase(tmp_path):
    key = tmp_path / "id_test"
    key.write_text("fixture", encoding="utf-8")
    database = Database(tmp_path / "manager.db")
    vault = MemoryVault()
    service = CredentialService(database, vault)
    credential = service.create_key("Test key", str(key), "passphrase")
    assert credential["key_path"] == str(key)
    assert credential["has_passphrase"] is True
    assert service.reveal(credential["id"])["slot"] == "passphrase"


def test_skill_register_refresh_status_and_multi_host_resolve(tmp_path):
    database = Database(tmp_path / "manager.db")
    one = database.create_server(alias="one", hostname="one.example", port=22, username="u")
    two = database.create_server(alias="two", hostname="two.example", port=22, username="u")
    manifest = write_skill(tmp_path / "skills" / "ops", "plugin:ops_tools", "Operate hosts")
    service = SkillService(database)

    registered = service.register(
        manifest.parent, server_identifiers=[one["id"], "two"]
    )

    assert registered["path"] == str(manifest.resolve())
    assert [server["alias"] for server in registered["servers"]] == ["one", "two"]
    assert service.list()[0]["status"] == "ready"
    resolved = service.resolve(["two", "one"])
    assert resolved["ok"] is True
    assert resolved["skills"][0]["applies_to"] == ["two", "one"]
    assert all(host["skills"][0]["status"] == "ready" for host in resolved["hosts"])

    write_skill(manifest.parent, "plugin:ops_tools", "New description")
    assert service.list()[0]["status"] == "ready"

    write_skill(manifest.parent, "renamed_ops", "New description")
    assert service.list()[0]["status"] == "name_mismatch"
    assert service.resolve(["one"])["ok"] is False
    refreshed = service.refresh(registered["id"])
    assert refreshed["name"] == "renamed_ops"
    assert refreshed["description"] == "New description"
    assert service.list()[0]["status"] == "ready"

    manifest.unlink()
    assert service.list()[0]["status"] == "missing"


def test_skill_registration_uses_safe_bounded_frontmatter(tmp_path):
    database = Database(tmp_path / "manager.db")
    service = SkillService(database)
    base = write_skill(tmp_path / "base", "ssh-server-manager")
    with pytest.raises(ValidationError, match="base transport"):
        service.register(base)

    unsafe = tmp_path / "unsafe" / "SKILL.md"
    unsafe.parent.mkdir()
    unsafe.write_text(
        "---\nname: unsafe\ndescription: !!python/object/apply:os.system ['echo no']\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="invalid YAML"):
        service.register(unsafe)

    oversized = tmp_path / "oversized" / "SKILL.md"
    oversized.parent.mkdir()
    oversized.write_bytes(b"---\n" + b"a" * (MAX_SKILL_FRONTMATTER_BYTES + 1))
    with pytest.raises(ValidationError, match="at most"):
        service.register(oversized)


def test_skill_resolution_fails_closed_for_deep_yaml_frontmatter(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="one", hostname="one.example", port=22, username="u")
    manifest = write_skill(tmp_path / "deep", "deep-skill")
    service = SkillService(database)
    service.register(manifest, server_identifiers=["one"])

    manifest.write_text(
        "---\nname: deep-skill\ndescription: Deep metadata\nnested: "
        + "[" * 500
        + "0"
        + "]" * 500
        + "\n---\n",
        encoding="utf-8",
    )

    listed = service.list()
    resolved = service.resolve(["one"])
    assert listed[0]["status"] == "invalid"
    assert "invalid YAML" in listed[0]["status_message"]
    assert resolved["ok"] is False
    assert resolved["skills"][0]["status"] == "invalid"


def test_skill_discovery_is_recursive_deduplicated_and_reports_conflicts(tmp_path):
    database = Database(tmp_path / "manager.db")
    service = SkillService(database)
    root = tmp_path / "catalog"
    first = write_skill(root / "collection" / "skills" / "first", "Ågent-ops")
    second = write_skill(root / "second", "åGENT-OPS", "A conflicting installation")
    mutable = write_skill(root / "mutable", "original-name")
    base = write_skill(root / "base", "ssh-server-manager")
    invalid = root / "invalid" / "SKILL.md"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("# no frontmatter\n", encoding="utf-8")

    discovered = service.discover([root, root / "collection"])

    assert len(discovered["candidates"]) == 4
    assert all(candidate["path"] != str(base.resolve()) for candidate in discovered["candidates"])
    assert sum(candidate["path"] == str(first.resolve()) for candidate in discovered["candidates"]) == 1
    assert any(candidate["path"] == str(invalid.resolve()) and candidate["status"] == "invalid" for candidate in discovered["candidates"])
    assert any(conflict["type"] == "name" for conflict in discovered["conflicts"])
    assert database.list_skills() == []

    service.register(mutable)
    write_skill(mutable.parent, "different-name")
    discovered = service.discover([root])
    assert any(
        conflict["type"] == "path" and conflict["path"] == str(mutable.resolve())
        for conflict in discovered["conflicts"]
    )
    assert {candidate["path"] for candidate in discovered["candidates"]} >= {
        str(first.resolve()),
        str(second.resolve()),
    }


def test_skill_path_resolution_runtime_errors_do_not_escape(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    service = SkillService(database)
    manifest = write_skill(tmp_path / "loop", "loop-safe")
    original_resolve = Path.resolve

    def looping_resolve(self, *args, **kwargs):
        if self == manifest:
            raise RuntimeError("symlink loop")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", looping_resolve)

    with pytest.raises(ValidationError, match="does not exist"):
        service.register(manifest)


def test_skill_discovery_skips_runtime_error_directories(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    service = SkillService(database)
    root = tmp_path / "loop-root"
    write_skill(root / "nested", "nested-skill")
    original_resolve = Path.resolve

    def looping_resolve(self, *args, **kwargs):
        if self == root:
            raise RuntimeError("symlink loop")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", looping_resolve)

    assert service.discover([root]) == {"candidates": [], "conflicts": []}


def test_windows_skill_paths_preserve_case_and_discover_filename_variants(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "manager.db")
    service = SkillService(database)
    manifest = write_skill(tmp_path / "MiXeD-Skill", "mixed-skill")
    lowercase_manifest = manifest.with_name("skill.md")
    manifest.rename(lowercase_manifest)

    monkeypatch.setattr(validation_module, "CASE_INSENSITIVE_SKILL_FILENAMES", True)
    monkeypatch.setattr(validation_module.os.path, "normcase", lambda value: value.lower())

    discovered = service.discover([tmp_path / "MiXeD-Skill"])
    registered = service.register(tmp_path / "MiXeD-Skill")

    expected = str(lowercase_manifest.resolve())
    assert discovered["candidates"][0]["path"] == expected
    assert registered["path"] == expected
    assert Path(registered["path"]).is_file()
