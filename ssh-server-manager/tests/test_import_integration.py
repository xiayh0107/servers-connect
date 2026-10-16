from __future__ import annotations

import json

import pytest

from ssh_server_manager.application import ImportService
from ssh_server_manager.cli import main
from ssh_server_manager.db import Database
from ssh_server_manager.ssh_config import SSHConfigError
from ssh_server_manager.validation import ValidationError


def test_import_source_follows_stable_id_and_survives_restart(tmp_path):
    path = tmp_path / "manager.db"
    source = tmp_path / "selected config"
    database = Database(path)
    server = database.create_server(alias="one", hostname="one.invalid", username="alice", ssh_config_source=str(source))
    database.update_server("one", alias="renamed")

    database = Database(path)
    assert database.get_ssh_config_sources() == {server["id"]: str(source.resolve())}
    database.delete_server("renamed")
    assert database.get_ssh_config_sources() == {}


def test_import_source_validation_rolls_back_host_and_context_changes(tmp_path):
    database = Database(tmp_path / "manager.db")
    with pytest.raises(ValidationError, match="printable local path"):
        database.create_server(alias="one", hostname="one.invalid", username="alice", tags=["new"], ssh_config_source="bad\nconfig")
    assert database.list_servers() == []
    assert database.list_server_contexts() == []

    server = database.create_server(alias="one", hostname="one.invalid", username="alice", ssh_config_source=str(tmp_path / "original"))
    with pytest.raises(ValidationError, match="printable local path"):
        database.update_server("one", hostname="changed.invalid", tags=["new"], ssh_config_source="bad\nconfig")
    assert database.get_server("one")["hostname"] == "one.invalid"
    assert database.list_server_contexts() == []
    assert database.get_ssh_config_sources() == {server["id"]: str(tmp_path / "original")}


def test_import_rejects_managed_include_cycle_before_mutating_database(tmp_path, monkeypatch):
    managed = tmp_path / "managed config"
    source = tmp_path / "source config"
    # This file is deliberately not fed to ssh -G: the service must reject the
    # cycle even when apply receives a previously valid preview.
    source.write_text(f'Include "{managed.as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(managed))
    database = Database(tmp_path / "manager.db")
    calls = []
    service = ImportService(database, lambda: calls.append("render"))
    preview = {"config": str(source), "items": [{"action": "add", "server": {
        "alias": "one", "hostname": "one.invalid", "username": "alice",
    }}], "errors": []}
    with pytest.raises(SSHConfigError, match="Include cycle"):
        service.apply(preview)
    assert database.list_servers() == []
    assert database.get_ssh_config_sources() == {}
    assert calls == []


def test_import_validates_source_before_applying_a_previously_created_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed"))
    database = Database(tmp_path / "manager.db")
    service = ImportService(database, lambda: pytest.fail("invalid source must not render"))
    preview = {"config": str(tmp_path / "invalid\nsource"), "items": [{"action": "add", "server": {
        "alias": "one", "hostname": "one.invalid", "username": "alice",
    }}], "errors": []}
    with pytest.raises(SSHConfigError, match="printable"):
        service.apply(preview)
    assert database.list_servers() == []
    assert database.get_ssh_config_sources() == {}


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_import_exposes_authentication_warning_without_creating_credentials(tmp_path, monkeypatch, capsys, as_json):
    import ssh_server_manager.importer as importer

    source = tmp_path / "selected config"
    source.write_text("Host one\n  HostName one.invalid\n  User alice\n", encoding="utf-8")
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing"))
    monkeypatch.setattr(importer, "_resolve_alias", lambda *args, **kwargs: {
        "alias": "one", "hostname": "one.invalid", "port": 22,
        "username": "alice", "credential_id": None, "proxy_jumps": [],
        "source": "ssh-config-import", "notes": "Imported profile",
        "_identity_files": ["~/.ssh/id_selected"],
    })
    args = ["server", "import", "--config", str(source), "--apply"]
    assert main(args + (["--json"] if as_json else [])) == 0
    output = capsys.readouterr().out
    if as_json:
        result = json.loads(output)
        assert result["warnings"][0]["identity_files"] == ["~/.ssh/id_selected"]
        assert result["warnings"][0]["fallback_config"] == str(source.resolve())
    else:
        assert "Warning: one:" in output and "not converted" in output
        assert f"Authentication fallback: {source.resolve()}" in output
    database = Database()
    assert database.list_credentials() == []
    assert database.get_ssh_config_sources() == {database.get_server("one")["id"]: str(source.resolve())}
