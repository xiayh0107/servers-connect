"""The manual fixture must be disposable and fail closed for remote actions."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
from fastapi.testclient import TestClient

from ssh_server_manager import webapp
from ssh_server_manager.ssh_runner import SSHError


SPEC = importlib.util.spec_from_file_location("hosts_fixture", Path(__file__).parent / "manual" / "hosts_fixture.py")
hosts_fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hosts_fixture)
ORIGIN = "http://localhost:8765"


@pytest.mark.parametrize("mode", hosts_fixture.MODES)
def test_manual_fixture_modes_and_cleanup(mode):
    previous_data_dir = os.environ.get("SSM_DATA_DIR")
    with hosts_fixture.fixture_environment(mode) as (database, vault, root):
        assert database.path.is_relative_to(root)
        assert vault.values == {}
        app = webapp.create_app(database, launch_token="fixture-launch", port=8765)
        with TestClient(app, base_url=ORIGIN) as client:
            assert client.get("/api/bootstrap").status_code == 401
            client.get("/?token=fixture-launch")
            first = client.get("/api/bootstrap")
            if mode in {"bootstrap-error", "bootstrap-retry"}:
                assert first.status_code == 400
                assert "Synthetic bootstrap failure" in first.json()["message"]
            else:
                assert first.status_code == 200
                assert len(first.json()["servers"]) == (0 if mode == "empty" else 8)
            if mode == "bootstrap-error":
                assert client.get("/api/bootstrap").status_code == 400
            elif mode == "bootstrap-retry":
                assert len(client.get("/api/bootstrap").json()["servers"]) == 8
    assert not root.exists()
    assert os.environ.get("SSM_DATA_DIR") == previous_data_dir


def test_manual_fixture_blocks_remote_operations_and_local_imports():
    with hosts_fixture.fixture_environment() as (database, _vault, _root):
        app = webapp.create_app(database, launch_token="fixture-launch", port=8765)
        with TestClient(app, base_url=ORIGIN) as client:
            client.get("/?token=fixture-launch")
            csrf = client.get("/api/bootstrap").json()["csrf"]
            headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
            for suffix in ("test", "diagnose"):
                response = client.post(f"/api/servers/bastion-prod/{suffix}", headers=headers)
                assert response.status_code == 502
                assert "disabled" in response.json()["message"]
            assert client.get("/api/servers/bastion-prod/files").status_code == 502
            assert client.post("/api/servers/bastion-prod/download", headers=headers, json={"path": "/tmp/demo"}).status_code == 502
            assert client.post("/api/servers/bastion-prod/upload", headers=headers, data={"path": "/tmp/demo"}, files={"file": ("demo.txt", b"fixture")}).status_code == 502
            assert client.post("/api/import/preview", headers=headers, json={}).status_code == 400
            assert client.get("/api/skills/discover").status_code == 400
            assert client.post("/api/credentials", headers=headers, json={"kind": "key", "label": "No local keys", "key_path": "/arbitrary/private/key"}).status_code == 400
        with pytest.raises(SSHError, match="disabled"):
            hosts_fixture.DeniedSSHRunner(database).any_future_remote_method()


def test_manual_fixture_launcher_keeps_standard_loopback_auth(tmp_path, monkeypatch, capsys):
    observed = {}
    url_file = tmp_path / "private-launch-url"

    def fake_run(app, **kwargs):
        observed.update(kwargs)
        url = url_file.read_text(encoding="utf-8").strip()
        assert "token=" in url
        if os.name != "nt":
            assert url_file.stat().st_mode & 0o777 == 0o600
        with TestClient(app, base_url=url.split("/?")[0]) as client:
            assert client.get("/").status_code == 401
            assert client.get(url).status_code == 200
            assert not url_file.exists()
            assert client.get("/api/bootstrap").status_code == 200

    monkeypatch.setattr("uvicorn.run", fake_run)
    hosts_fixture.main(["--url-file", str(url_file)])
    assert observed["host"] == "127.0.0.1"
    assert observed["access_log"] is False
    assert "token=" not in capsys.readouterr().out
    assert not url_file.exists()
