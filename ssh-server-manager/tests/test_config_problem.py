from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
from fastapi.testclient import TestClient

from ssh_server_manager import ssh_config, webapp
from ssh_server_manager.config_errors import directory_write_problem
from ssh_server_manager.db import Database
from ssh_server_manager.ssh_config import SSHConfigError, render_config
from ssh_server_manager.webapp import create_app


ORIGIN = "http://localhost:8765"


def managed_fixture(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example.invalid", username="alice")
    managed = tmp_path / "ssh" / "managed.conf"
    original = tmp_path / "ssh" / "config"
    original.parent.mkdir(parents=True)
    original.write_text("Host *\n  ServerAliveInterval 30\n", encoding="utf-8")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(managed))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(original))
    return database, managed, original


def test_a_read_only_config_directory_is_a_configuration_error(tmp_path, monkeypatch):
    database, managed, original = managed_fixture(tmp_path, monkeypatch)

    def deny(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted", str(managed.parent / ".temporary"))

    monkeypatch.setattr(ssh_config.tempfile, "mkstemp", deny)
    with pytest.raises(SSHConfigError) as caught:
        render_config(database, destination=managed, original=original)
    message = str(caught.value)
    assert str(managed.parent) in message
    assert "Full Disk Access" in message


def test_config_directory_chmod_failure_is_not_fatal(tmp_path, monkeypatch):
    database, managed, original = managed_fixture(tmp_path, monkeypatch)

    original_chmod = Path.chmod

    def deny_directory_chmod(self, mode, *args, **kwargs):
        if self == managed.parent:
            raise PermissionError(1, "Operation not permitted", str(self))
        return original_chmod(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", deny_directory_chmod, raising=False)
    rendered = render_config(database, destination=managed, original=original)
    assert "Host box" in rendered.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(rendered.stat().st_mode) == 0o600


def test_write_probe_reports_an_unwritable_directory(tmp_path, monkeypatch):
    directory = tmp_path / "ssh"
    assert directory_write_problem(directory) is None

    import ssh_server_manager.config_errors as config_errors

    def deny(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted", str(directory / ".write-probe"))

    monkeypatch.setattr(config_errors.tempfile, "mkstemp", deny)
    problem = directory_write_problem(directory)
    assert problem is not None
    assert str(directory) in str(problem)


@pytest.fixture
def blocked_ui(tmp_path, monkeypatch):
    database, managed, _original = managed_fixture(tmp_path, monkeypatch)

    import ssh_server_manager.config_errors as config_errors

    def deny(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted", str(managed.parent / ".write-probe"))

    # Deny only the usual location: the private fallback stays usable, which is
    # what the UI reports as "remote actions still work".
    original_mkstemp = config_errors.tempfile.mkstemp

    def deny_usual(*args, **kwargs):
        if Path(kwargs.get("dir") or ".") == managed.parent or (args and Path(args[0]) == managed.parent):
            raise PermissionError(1, "Operation not permitted", str(managed.parent / ".write-probe"))
        return original_mkstemp(*args, **kwargs)

    monkeypatch.setattr(config_errors.tempfile, "mkstemp", deny_usual)
    monkeypatch.setenv("SSM_CACHE_DIR", str(tmp_path / "cache"))
    app = create_app(database, launch_token="launch", port=8765)
    with TestClient(app, base_url=ORIGIN) as client:
        client.get("/?token=launch")
        csrf = client.get("/api/v1/runtime").json()["csrf"]
        yield client, app, {"X-CSRF-Token": csrf, "Origin": ORIGIN}


def test_runtime_names_an_unwritable_config_location(blocked_ui):
    client, _app, _headers = blocked_ui
    runtime = client.get("/api/v1/runtime").json()
    assert "this app cannot write" in runtime["config_problem"]
    assert "Full Disk Access" in runtime["config_problem"]


def test_runtime_names_an_unwritable_database_directory(tmp_path, monkeypatch):
    database, _managed, _original = managed_fixture(tmp_path, monkeypatch)

    import ssh_server_manager.config_errors as config_errors

    original = config_errors._can_create_file
    monkeypatch.setattr(
        config_errors, "_can_create_file",
        lambda directory: False if Path(directory) == database.path.parent else original(directory),
    )
    app = create_app(database, launch_token="launch", port=8765)
    with TestClient(app, base_url=ORIGIN) as client:
        client.get("/?token=launch")
        runtime = client.get("/api/v1/runtime").json()
    assert "this app cannot write" in runtime["config_problem"]
    assert str(database.path.parent) in runtime["config_problem"]


def test_an_unusable_managed_config_fails_listing_with_its_reason(tmp_path, monkeypatch):
    managed_fixture(tmp_path, monkeypatch)

    import ssh_server_manager.config_errors as config_errors

    def deny(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted", str(tmp_path / "ssh" / ".write-probe"))

    monkeypatch.setattr(config_errors.tempfile, "mkstemp", deny)
    monkeypatch.setattr(config_errors, "cache_dir", lambda: tmp_path / "blocked-cache")
    app = create_app(Database(tmp_path / "manager.db"), launch_token="launch", port=8765)
    called = []
    with TestClient(app, base_url=ORIGIN) as client:
        client.get("/?token=launch")
        runtime = client.get("/api/v1/runtime").json()
        assert runtime["config_problem"]
        assert "Full Disk Access" in runtime["config_problem"] or "not allowed" in runtime["config_problem"]
        monkeypatch.setattr(
            app.state.services.files, "list_directory",
            lambda *args, **kwargs: called.append(args) or pytest.fail("SFTP was attempted"),
        )
        response = client.get("/api/v1/hosts/box/files")
    assert response.status_code == 502
    assert response.json()["error"] == "SSHConfigError"
    assert called == []


def test_remote_commands_fall_back_to_a_private_config(tmp_path, monkeypatch):
    database, managed, _original = managed_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("SSM_CACHE_DIR", str(tmp_path / "cache"))

    import ssh_server_manager.config_errors as config_errors

    original_mkstemp = ssh_config.tempfile.mkstemp

    def denied_only_in_the_usual_place(prefix="", dir=None, **kwargs):
        if dir is not None and Path(dir) == managed.parent:
            raise PermissionError(1, "Operation not permitted", str(Path(dir) / ".temporary"))
        return original_mkstemp(prefix=prefix, dir=dir, **kwargs)

    monkeypatch.setattr(ssh_config.tempfile, "mkstemp", denied_only_in_the_usual_place)
    fallback = tmp_path / "cache" / "ssh-ssh.conf"
    with pytest.raises(SSHConfigError):
        render_config(database, destination=managed)
    assert config_errors.directory_write_problem(fallback.parent) is None
    written = ssh_config.render_config(database, destination=managed, fallback=True)
    assert written == fallback
    assert "Host box" in fallback.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(fallback.stat().st_mode) == 0o600


def test_runtime_has_no_config_problem_when_the_directory_is_writable(tmp_path, monkeypatch):
    database, managed, _original = managed_fixture(tmp_path, monkeypatch)
    app = create_app(database, launch_token="launch", port=8765)
    with TestClient(app, base_url=ORIGIN) as client:
        client.get("/?token=launch")
        runtime = client.get("/api/v1/runtime").json()
    assert runtime["config_problem"] is None
    assert directory_write_problem(managed.parent) is None
