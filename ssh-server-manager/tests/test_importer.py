from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ssh_server_manager import importer, ssh_config
from ssh_server_manager.db import ConflictError, Database
from ssh_server_manager.process_args import literal_config_args
from ssh_server_manager.ssh_config import quote_value
from ssh_fixture_permissions import private_ssh_fixture_file


def _candidate(alias, **changes):
    return {
        "alias": alias, "hostname": "new.example", "port": 2222, "username": "alice",
        "credential_id": None, "proxy_jumps": [], "notes": "imported", "source": "ssh-config-import",
        **changes,
    }


def _ssh_client():
    selected = os.environ.get("SSM_TEST_SSH_CLIENT")
    if selected:
        assert shutil.which(selected), "SSM_TEST_SSH_CLIENT must select an installed OpenSSH client"
        return shutil.which(selected)
    ssh = shutil.which("ssh")
    if not ssh:
        pytest.skip("OpenSSH is unavailable")
    return ssh


def test_import_preview_and_apply(tmp_path, monkeypatch):
    config = tmp_path / "config"
    included = tmp_path / "extra.conf"
    included.write_text("Host compute\n  HostName 10.0.0.8\n", encoding="utf-8")
    config.write_text(
        f"Host login\n  HostName gateway.example\nInclude {quote_value(included.as_posix())}\nHost *.wild\n",
        encoding="utf-8",
    )

    def resolve(alias, _config, *, ssh_binary="ssh"):
        values = {
            "login": {"hostname": "gateway.example", "port": 2222, "username": "alice", "proxy_jumps": []},
            "compute": {"hostname": "10.0.0.8", "port": 22, "username": "alice", "proxy_jumps": ["login"]},
        }[alias]
        return {"alias": alias, "credential_id": None, "notes": "imported", "source": "ssh-config-import", **values}

    monkeypatch.setattr(importer, "_resolve_alias", resolve)
    database = Database(tmp_path / "manager.db")
    preview = importer.preview_import(database, config=config)
    assert [item["server"]["alias"] for item in preview["items"]] == ["login", "compute"]
    assert preview["skipped_patterns"] == ["*.wild"]
    assert [warning["alias"] for warning in preview["warnings"]] == ["login", "compute"]
    assert all(warning["source_config"] == str(config.resolve()) for warning in preview["warnings"])
    assert all(warning["fallback_config"] == str(config.resolve()) for warning in preview["warnings"])
    result = importer.apply_import(database, preview)
    assert result["added"] == ["login", "compute"]
    assert database.get_server("compute")["proxy_jumps"] == ["login"]
    assert database.get_ssh_config_sources() == {
        database.get_server(alias)["id"]: str(config.resolve()) for alias in ("login", "compute")
    }
    assert [warning["alias"] for warning in result["warnings"]] == ["login", "compute"]


def test_overwrite_preserves_the_current_binding_and_local_metadata(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text("Host box\n", encoding="utf-8")
    database = Database(tmp_path / "manager.db")
    first = database.create_credential(label="First", kind="password", has_secret=True)
    latest = database.create_credential(label="Latest", kind="password", has_secret=True)
    existing = database.create_server(
        alias="box", hostname="old.example", username="bob", credential_id=first["id"],
        notes="Keep this host note", tags=["production"],
        geo={"country_code": "US", "city": "Boston", "source": "manual"},
    )
    saved_path = database.create_server_path("box", label="app", path="/srv/app", notes="Keep this path")
    monkeypatch.setattr(importer, "_resolve_alias", lambda alias, _config, **_: _candidate(alias))
    preview = importer.preview_import(database, config=config)
    candidate = preview["items"][0]
    assert candidate["action"] == "conflict"
    assert candidate["server"]["credential_id"] == first["id"]
    assert candidate["server"]["notes"] == "Keep this host note"

    # The preview must not replace a binding or note changed before apply.
    database.update_server("box", credential_id=latest["id"], notes="Newer local note")
    result = importer.apply_import(database, preview, overwrite=True)
    updated = database.get_server("box")
    assert result["updated"] == ["box"]
    assert updated["id"] == existing["id"]
    assert (updated["hostname"], updated["port"], updated["username"]) == ("new.example", 2222, "alice")
    assert updated["credential_id"] == latest["id"]
    assert updated["notes"] == "Newer local note"
    assert updated["tags"] == existing["tags"]
    assert updated["geo"] == existing["geo"]
    assert database.get_server_path("box", "app") == saved_path
    assert database.get_ssh_config_sources()[existing["id"]] == str(config.resolve())


def test_apply_reports_warnings_only_for_hosts_that_received_the_source(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text("Host added skipped failed unchanged\n", encoding="utf-8")
    database = Database(tmp_path / "manager.db")
    skipped = database.create_server(alias="skipped", hostname="old.example", username="bob")
    unchanged = database.create_server(alias="unchanged", hostname="new.example", port=2222, username="alice")
    monkeypatch.setattr(importer, "_resolve_alias", lambda alias, _config, **_: _candidate(alias))
    preview = importer.preview_import(database, config=config)
    real_create = database.create_server

    def create(**payload):
        if payload["alias"] == "failed":
            raise ConflictError("fixture create conflict")
        return real_create(**payload)

    monkeypatch.setattr(database, "create_server", create)
    result = importer.apply_import(database, preview)
    assert result["added"] == ["added"]
    assert result["skipped"] == ["skipped"]
    assert result["unchanged"] == ["unchanged"]
    assert result["errors"] == [{"alias": "failed", "message": "fixture create conflict"}]
    assert preview["errors"] == []
    assert [warning["alias"] for warning in result["warnings"]] == ["added", "unchanged"]
    sources = database.get_ssh_config_sources()
    assert skipped["id"] not in sources
    assert sources[unchanged["id"]] == str(config.resolve())
    assert set(sources) == {database.get_server(alias)["id"] for alias in ("added", "unchanged")}


def test_import_effective_identity_files_are_warning_metadata_only(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text("Host box\n", encoding="utf-8")
    database = Database(tmp_path / "manager.db")

    def effective(command, **kwargs):
        assert command == ["fixture-ssh", "-G", "-F", str(config.resolve()), "box"]
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(command, 0, stdout=(
            "hostname fixture.example\nport 2200\nuser fixture\nproxyjump none\n"
            "identityfile /fixture/key with spaces\nidentityfile ~/.ssh/second-key\nidentityfile none\n"
        ), stderr="")

    monkeypatch.setattr(importer.subprocess, "run", effective)
    preview = importer.preview_import(database, config=config, ssh_binary="fixture-ssh")
    assert preview["errors"] == []
    warning = preview["warnings"][0]
    assert warning["code"] == "authentication-not-imported"
    assert warning["identity_files"] == ["/fixture/key with spaces", "~/.ssh/second-key"]
    assert "does not verify authentication" in warning["message"]
    candidate = preview["items"][0]["server"]
    assert candidate["credential_id"] is None
    assert "_identity_files" not in candidate
    assert "identity_files" not in candidate
    result = importer.apply_import(database, preview)
    assert result["errors"] == []
    assert database.list_credentials() == []
    assert database.get_server("box")["credential_id"] is None
    assert result["warnings"][0]["identity_files"] == warning["identity_files"]


@pytest.mark.parametrize("config_name", [
    "selected config.conf", "source${SSM_TEST_SOURCE}.conf", "O'Brien.conf", "[source].conf",
])
def test_real_ssh_g_import_reads_config_without_loading_an_identity(tmp_path, monkeypatch, config_name):
    ssh = _ssh_client()
    config = tmp_path / config_name
    included = tmp_path / "profile fragment.conf"
    # This deliberately is a directory, not a private key. Config resolution
    # still succeeds because ssh -G never attempts authentication or key loading.
    identity = tmp_path / "not a private key 中文"
    identity.mkdir()
    included.write_text(
        "Host fixture\n  HostName 127.0.0.1\n  Port 22022\n  User fixture\n"
        f"  IdentityFile {quote_value(identity.as_posix())}\n  IdentitiesOnly yes\n",
        encoding="utf-8",
    )
    config.write_text(f"Include {ssh_config._IncludePaths(ssh).format(included.resolve())}\n", encoding="utf-8")
    private_ssh_fixture_file(config)
    private_ssh_fixture_file(included)
    database = Database(tmp_path / "manager.db")
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "unused-default.conf"))
    monkeypatch.setenv("SSM_TEST_SOURCE", "must-not-expand")
    original_run = subprocess.run
    expected_resolution = literal_config_args([ssh, "-G", "-F", str(config.resolve()), "fixture"])

    def config_only(command, **kwargs):
        if isinstance(command, list) and Path(command[0]).name.casefold() == "cygpath.exe":
            assert command[1:] in (["-u", "-f", "-"], ["-w", "-C", "UTF8", "-f", "-"])
            assert kwargs["encoding"] == "utf-8"
            assert kwargs["input"].endswith("\n") and kwargs["input"].count("\n") == 1
            assert not kwargs.get("shell", False)
        else:
            assert command == expected_resolution
            assert not kwargs.get("shell", False)
        return original_run(command, **kwargs)

    monkeypatch.setattr(importer.subprocess, "run", config_only)
    preview = importer.preview_import(database, config=config, ssh_binary=ssh)
    assert preview["errors"] == []
    assert preview["warnings"][0]["identity_files"] == [identity.as_posix()]
    assert preview["warnings"][0]["source_config"] == str(config.resolve())
    result = importer.apply_import(database, preview)
    assert result["added"] == ["fixture"]
    assert database.list_credentials() == []
    host = database.get_server("fixture")
    assert host["credential_id"] is None
    assert database.get_ssh_config_sources() == {host["id"]: str(config.resolve())}


def test_relative_includes_use_the_user_ssh_directory_even_when_nested(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    user_ssh = home / ".ssh"
    fragments = user_ssh / "parts"
    fragments.mkdir(parents=True)
    selected = tmp_path / "selected-source"
    selected.mkdir()
    config = selected / "config"
    config.write_text('Include = "parts/part*.conf"\n', encoding="utf-8")
    (fragments / "part-one.conf").write_text('Host relative\nInclude nested.conf\n', encoding="utf-8")
    (user_ssh / "nested.conf").write_text('Host nested\nInclude "missing*.conf"\n', encoding="utf-8")
    # OpenSSH does not rebase Include to either the selected file or its child.
    (selected / "parts").mkdir()
    (selected / "parts" / "part-wrong.conf").write_text("Host wrong-parent\n", encoding="utf-8")
    (fragments / "nested.conf").write_text("Host wrong-child-parent\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(importer, "_resolve_alias", lambda alias, _config, **_: _candidate(alias))
    # Avoid selecting a real Windows MSYS converter for this isolated unit case.
    monkeypatch.setattr(importer, "_IncludePaths", lambda binary: ssh_config._IncludePaths(binary, windows=False))
    database = Database(tmp_path / "manager.db")
    preview = importer.preview_import(database, config=config)
    assert preview["errors"] == []
    assert [item["server"]["alias"] for item in preview["items"]] == ["relative", "nested"]


def test_preview_passes_its_client_to_shared_msys_include_expansion(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    home.mkdir()
    config = tmp_path / "config"
    included = tmp_path / "included.conf"
    config.write_text('Include "/c/fixture/included.conf"\n', encoding="utf-8")
    included.write_text("Host msys-host\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    constructed = []
    conversions = []

    class FixturePaths:
        cygpath = Path("fixture-cygpath.exe")

        def local(self, value):
            conversions.append(value)
            return included if value == "/c/fixture/included.conf" else Path(value)

    def paths(binary):
        constructed.append(binary)
        return FixturePaths()

    def resolve(alias, _config, *, ssh_binary):
        assert ssh_binary == "selected-msys-ssh"
        return _candidate(alias)

    monkeypatch.setattr(importer, "_IncludePaths", paths)
    monkeypatch.setattr(importer, "_resolve_alias", resolve)
    preview = importer.preview_import(Database(tmp_path / "manager.db"), config=config, ssh_binary="selected-msys-ssh")
    assert [item["server"]["alias"] for item in preview["items"]] == ["msys-host"]
    assert constructed == ["selected-msys-ssh"]
    assert "/c/fixture/included.conf" in conversions

