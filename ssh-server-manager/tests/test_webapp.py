from __future__ import annotations

import os
import socket

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient

from ssh_server_manager.db import Database
from ssh_server_manager.vault import MemoryVault
from ssh_server_manager.webapp import UIError, _select_loopback_port, create_app, run_ui


def test_local_ui_session_crud_and_master_reveal(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    vault = MemoryVault()
    monkeypatch.setattr("ssh_server_manager.webapp.get_vault", lambda: vault)
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    app = create_app(database, launch_token="launch", port=8765)

    with TestClient(app, base_url="http://localhost:8765") as client:
        response = client.get("/?token=launch")
        assert response.status_code == 200
        bootstrap = client.get("/api/bootstrap").json()
        headers = {"X-CSRF-Token": bootstrap["csrf"], "Origin": "http://localhost:8765"}

        created = client.post(
            "/api/credentials",
            headers=headers,
            json={"label": "Demo", "kind": "password", "secret": "vault-only"},
        )
        assert created.status_code == 200
        credential_id = created.json()["id"]
        assert client.post(f"/api/credentials/{credential_id}/reveal", headers=headers).status_code == 400

        assert client.post(
            "/api/auth/master/enroll", headers=headers, json={"password": "long-enough-master"}
        ).status_code == 200
        assert client.post(
            "/api/auth/master/verify",
            headers=headers,
            json={"credential_id": credential_id, "password": "long-enough-master"},
        ).status_code == 200
        reveal = client.post(f"/api/credentials/{credential_id}/reveal", headers=headers)
        assert reveal.json()["value"] == "vault-only"
        assert reveal.headers["cache-control"] == "no-store"
        assert client.post(f"/api/credentials/{credential_id}/reveal", headers=headers).status_code == 400


def test_ui_never_prints_launch_token_and_writes_private_url(tmp_path, monkeypatch, capsys):
    database = Database(tmp_path / "manager.db")
    url_file = tmp_path / "launch-url"
    observed = {}

    def fake_run(*args, **kwargs):
        observed["url"] = url_file.read_text(encoding="utf-8")
        observed["mode"] = url_file.stat().st_mode & 0o777

    monkeypatch.setattr("uvicorn.run", fake_run)

    run_ui(database=database, port=0, open_browser=False, url_file=url_file)

    output = capsys.readouterr().out
    assert "token=" not in output
    assert "launch URL written to" in output
    assert "token=" in observed["url"]
    if os.name != "nt":
        assert observed["mode"] == 0o600
    assert not url_file.exists()


def test_ui_removes_url_file_after_token_is_consumed(tmp_path):
    database = Database(tmp_path / "manager.db")
    url_file = tmp_path / "launch-url"
    url_file.write_text("http://localhost:8765/?token=launch\n", encoding="utf-8")
    app = create_app(database, launch_token="launch", port=8765, launch_url_file=url_file)

    with TestClient(app, base_url="http://localhost:8765") as client:
        assert client.get("/?token=launch").status_code == 200
    assert not url_file.exists()


def test_ui_auto_port_and_busy_port_error():
    automatic = _select_loopback_port(0)
    assert automatic > 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        with pytest.raises(UIError, match="already in use"):
            _select_loopback_port(occupied.getsockname()[1])
