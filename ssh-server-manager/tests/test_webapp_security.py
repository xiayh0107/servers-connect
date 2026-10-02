"""Security gates that host-list/detail UI changes must preserve."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
from fastapi.testclient import TestClient

from ssh_server_manager import webapp
from ssh_server_manager.db import Database
from ssh_server_manager.webapp import SESSION_COOKIE, create_app


ORIGIN = "http://localhost:8765"


@pytest.fixture
def secured_app(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="demo", hostname="demo.example.invalid", username="demo")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    def deny_runner(*args, **kwargs):
        raise AssertionError("unauthorized host action reached the SSH runner")

    monkeypatch.setattr(webapp, "SSHRunner", deny_runner)
    app = create_app(database, launch_token="test-only-launch", port=8765)
    return app, database


@pytest.mark.parametrize("path", ["/", "/assets/app.js", "/api/bootstrap", "/api/servers/demo/paths", "/api/servers/demo/files"])
def test_host_ui_reads_require_browser_session(secured_app, path):
    app, _database = secured_app
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get(path).status_code == 401


def test_launch_token_is_single_use_and_session_is_private(secured_app):
    app, _database = secured_app
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/?token=incorrect").status_code == 401
        response = client.get("/?token=test-only-launch")
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert f"{SESSION_COOKIE}=" in cookie
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert "test-only-launch" not in response.text
        assert client.get("/").status_code == 200
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.headers["cache-control"] == "no-store"
        assert bootstrap.headers["x-frame-options"] == "DENY"
        assert bootstrap.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in bootstrap.headers["content-security-policy"]
        client.cookies.clear()
        assert client.get("/?token=test-only-launch").status_code == 401
        assert client.get("/api/bootstrap").status_code == 401


@pytest.mark.parametrize("headers, expected", [
    ({"Host": "attacker.example"}, 400),
    ({"Origin": "https://attacker.example"}, 403),
    ({"Origin": "http://localhost:8766"}, 403),
    ({"Origin": "null"}, 403),
])
def test_host_ui_rejects_foreign_host_and_origin(secured_app, headers, expected):
    app, _database = secured_app
    with TestClient(app, base_url=ORIGIN) as client:
        client.get("/?token=test-only-launch")
        assert client.get("/api/bootstrap", headers=headers).status_code == expected


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/api/servers", {"alias": "new", "hostname": "new.example.invalid", "username": "demo"}),
    ("PUT", "/api/servers/demo", {"notes": "changed"}),
    ("DELETE", "/api/servers/demo", None),
    ("POST", "/api/servers/demo/test", None),
    ("POST", "/api/servers/demo/diagnose", None),
    ("POST", "/api/servers/demo/notes", {"mode": "set", "text": "changed"}),
    ("POST", "/api/servers/demo/paths", {"label": "App", "path": "/srv/app"}),
    ("POST", "/api/servers/demo/download", {"path": "/srv/app/file"}),
])
def test_host_actions_require_valid_csrf_and_origin(secured_app, method, path, payload):
    app, database = secured_app
    with TestClient(app, base_url=ORIGIN) as client:
        client.get("/?token=test-only-launch")
        csrf = client.get("/api/bootstrap").json()["csrf"]
        for headers in (
            {"Origin": ORIGIN},
            {"Origin": ORIGIN, "X-CSRF-Token": "incorrect"},
            {"Origin": "https://attacker.example", "X-CSRF-Token": csrf},
        ):
            response = client.request(method, path, headers=headers, json=payload)
            assert response.status_code == 403
        assert database.get_server("demo")["notes"] == ""
        assert len(database.list_servers()) == 1
        assert database.list_server_paths("demo") == []
