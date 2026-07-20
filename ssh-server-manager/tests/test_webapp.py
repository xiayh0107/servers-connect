from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient

from ssh_server_manager.db import Database
from ssh_server_manager.service import SkillService
from ssh_server_manager.ssh_runner import SSHRunner
from ssh_server_manager.vault import MemoryVault
from ssh_server_manager.webapp import (
    UIError,
    _read_ui_state,
    _select_loopback_port,
    _write_ui_state,
    create_app,
    run_ui,
    ui_status,
    ui_stop,
)


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
        assert "/assets/themes.css" in response.text
        assert client.get("/assets/contexts.js").status_code == 200
        assert client.get("/assets/contexts.css").status_code == 200
        assert client.get("/assets/themes.css").status_code == 200
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

        server = client.post(
            "/api/servers",
            headers=headers,
            json={
                "alias": "demo",
                "hostname": "demo.example",
                "port": 22,
                "username": "alice",
                "credential_id": credential_id,
                "tags": ["Project A", "production"],
            },
        )
        assert server.status_code == 200
        assert server.json()["tags"] == ["Project A", "production"]

        contexts = client.get("/api/bootstrap").json()["contexts"]
        assert contexts == [
            {"name": "production", "count": 1},
            {"name": "Project A", "count": 1},
        ]
        created_context = client.post(
            "/api/contexts",
            headers=headers,
            json={"name": "Client B", "server_ids": [server.json()["id"]]},
        )
        assert created_context.json() == {"name": "Client B", "count": 1}
        assert database.get_server("demo")["tags"] == ["Project A", "production", "Client B"]
        assigned = client.put(
            "/api/contexts",
            headers=headers,
            json={"name": "Client B", "new_name": "Client work", "server_ids": [server.json()["id"]]},
        )
        assert assigned.json() == {"name": "Client work", "count": 1}
        assert database.get_server("demo")["tags"] == ["Project A", "production", "Client work"]
        assert client.request(
            "DELETE", "/api/contexts", headers=headers, json={"name": "Client work"}
        ).json()["removed_from"] == 1
        assert database.get_server("demo")["tags"] == ["Project A", "production"]

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


def test_ui_records_runtime_state_and_clears_it_on_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    database = Database(tmp_path / "manager.db")
    observed = {}

    def fake_run(*args, **kwargs):
        observed["state"] = _read_ui_state()

    monkeypatch.setattr("uvicorn.run", fake_run)

    run_ui(database=database, port=0, open_browser=False, url_file=tmp_path / "launch-url")

    assert observed["state"]["pid"] == os.getpid()
    assert observed["state"]["port"] > 0
    assert observed["state"]["url_file"] == str(tmp_path / "launch-url")
    assert _read_ui_state() is None


def test_ui_status_and_stop_without_a_recorded_process(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))

    status = ui_status()
    assert status["running"] is False

    result = ui_stop()
    assert result["running"] is False
    assert result["stopped"] is False


def test_ui_status_removes_a_stale_record(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    _write_ui_state({"pid": dead.pid, "port": closed_port, "url_file": None})

    status = ui_status()
    assert status["running"] is False
    assert _read_ui_state() is None


def test_ui_stop_terminates_the_recorded_process(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    url_file = tmp_path / "launch-url"
    url_file.write_text("http://localhost:1/?token=x\n", encoding="utf-8")
    script = (
        "import socket, time\n"
        "listener = socket.socket()\n"
        "listener.bind(('127.0.0.1', 0))\n"
        "listener.listen(1)\n"
        "print(listener.getsockname()[1], flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline())
        _write_ui_state({"pid": proc.pid, "port": port, "url_file": str(url_file)})
        # Reap the child as soon as it dies so _pid_alive does not see a zombie;
        # a real ui_stop runs in a separate process where this cannot happen.
        threading.Thread(target=proc.wait, daemon=True).start()

        result = ui_stop()

        assert result["stopped"] is True
        assert result["pid"] == proc.pid
        assert not url_file.exists()
        assert _read_ui_state() is None
        assert ui_status()["running"] is False
    finally:
        proc.kill()
        proc.stdout.close()


def test_ui_auto_port_and_busy_port_error():
    automatic = _select_loopback_port(0)
    assert automatic > 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        with pytest.raises(UIError, match="already in use"):
            _select_loopback_port(occupied.getsockname()[1])


def test_ui_lists_remote_files_for_an_authenticated_browser_session(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    server = database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    app = create_app(database, launch_token="launch", port=8765)

    observed = {}

    def fake_list_directory(self, identifier, path=None):
        observed.update(identifier=identifier, path=path)
        return {
            "alias": "box",
            "path": "/home/alice",
            "parent": "/home",
            "entries": [
                {
                    "name": "src",
                    "path": "/home/alice/src",
                    "type": "directory",
                    "size": 4096,
                    "modified": "Jul 16 12:04",
                    "permissions": "drwxr-xr-x",
                    "owner": "alice",
                    "group": "users",
                    "hidden": False,
                }
            ],
            "total": 1,
            "truncated": False,
            "unparsed": 0,
            "latency_ms": 42,
            "connection_checked_at": "2026-07-16T12:00:00+00:00",
        }

    monkeypatch.setattr(SSHRunner, "list_directory", fake_list_directory)
    with TestClient(app, base_url="http://localhost:8765") as client:
        assert client.get("/?token=launch").status_code == 200
        response = client.get(f"/api/servers/{server['id']}/files", params={"path": "~/project"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["entries"][0]["name"] == "src"
    assert observed == {"identifier": server["id"], "path": "~/project"}


def test_ui_host_diagnostics_endpoint_and_assets(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    server = database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    app = create_app(database, launch_token="launch", port=8765)
    observed = {}

    def fake_diagnose(self, identifier, timeout=8):
        observed.update(identifier=identifier, timeout=timeout)
        return {
            "alias": "box",
            "checked_at": "2026-07-17T12:00:00+00:00",
            "overall": "ok",
            "summary": {"total": 4, "passed": 4, "failed": 0, "warnings": 0, "skipped": 0},
            "checks": [],
        }

    monkeypatch.setattr(SSHRunner, "diagnose", fake_diagnose)
    with TestClient(app, base_url="http://localhost:8765") as client:
        index = client.get("/?token=launch")
        assert index.status_code == 200
        assert "/assets/diagnostics.js" in index.text
        assert client.get("/assets/diagnostics.js").status_code == 200
        assert client.get("/assets/diagnostics.css").status_code == 200
        bootstrap = client.get("/api/bootstrap").json()
        headers = {"X-CSRF-Token": bootstrap["csrf"], "Origin": "http://localhost:8765"}
        response = client.post(f"/api/servers/{server['id']}/diagnose", headers=headers)

    assert response.status_code == 200
    assert response.json()["overall"] == "ok"
    assert observed == {"identifier": server["id"], "timeout": 8}


def test_ui_server_notes_endpoint_and_assets(tmp_path):
    database = Database(tmp_path / "manager.db")
    server = database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    app = create_app(database, launch_token="launch", port=8765)

    with TestClient(app, base_url="http://localhost:8765") as client:
        index = client.get("/?token=launch")
        assert index.status_code == 200
        assert "/assets/notes.js" in index.text
        assert client.get("/assets/notes.js").status_code == 200
        assert client.get("/assets/notes.css").status_code == 200
        csrf = client.get("/api/bootstrap").json()["csrf"]
        headers = {"X-CSRF-Token": csrf, "Origin": "http://localhost:8765"}
        added = client.put(
            f"/api/servers/{server['id']}/notes",
            headers=headers,
            json={"mode": "append", "text": "Keep this host for model serving"},
        )
        appended = client.put(
            f"/api/servers/{server['id']}/notes",
            headers=headers,
            json={"mode": "append", "text": "Agent note: check GPU memory first"},
        )

    assert added.status_code == 200
    assert appended.status_code == 200
    assert appended.json()["notes"] == "Keep this host for model serving\n\nAgent note: check GPU memory first"


def test_ui_host_skill_registry_and_atomic_bindings(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    one = database.create_server(alias="one", hostname="one.example", port=22, username="alice")
    two = database.create_server(alias="two", hostname="two.example", port=22, username="alice")
    skill_dir = tmp_path / "skills" / "gpu-operations"
    skill_dir.mkdir(parents=True)
    manifest = skill_dir / "SKILL.md"
    manifest.write_text(
        "---\nname: gpu-operations\ndescription: Inspect GPU jobs on the attached hosts.\n---\n"
        "PRIVATE SKILL BODY MUST NOT ENTER BOOTSTRAP\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(SkillService, "standard_roots", staticmethod(lambda: [tmp_path / "skills"]))
    app = create_app(database, launch_token="launch", port=8765)

    with TestClient(app, base_url="http://localhost:8765") as client:
        assert client.get("/?token=launch").status_code == 200
        assert client.get("/assets/skills.js").status_code == 200
        assert client.get("/assets/skills.css").status_code == 200
        bootstrap = client.get("/api/bootstrap").json()
        headers = {"X-CSRF-Token": bootstrap["csrf"], "Origin": "http://localhost:8765"}

        discovered = client.get("/api/skills/discover")
        assert discovered.status_code == 200
        assert discovered.json()["candidates"][0]["name"] == "gpu-operations"
        assert client.post("/api/skills", headers=headers, json={}).status_code == 400
        assert client.post(
            "/api/skills", json={"path": str(skill_dir), "server_ids": [one["id"]]}
        ).status_code == 403
        created = client.post(
            "/api/skills",
            headers=headers,
            json={"path": str(skill_dir), "server_ids": [one["id"]]},
        )
        assert created.status_code == 200
        skill = created.json()
        assert skill["name"] == "gpu-operations"
        assert [server["alias"] for server in skill["servers"]] == ["one"]

        refreshed_bootstrap = client.get("/api/bootstrap").json()
        assert refreshed_bootstrap["skills"][0]["status"] == "ready"
        assert refreshed_bootstrap["skills"][0]["servers"] == [
            {"id": one["id"], "alias": "one"}
        ]
        assert "PRIVATE SKILL BODY" not in str(refreshed_bootstrap)

        original_manifest = manifest.read_text(encoding="utf-8")
        manifest.write_text(
            "---\nname: gpu-operations\ndescription: Deep metadata\nnested: "
            + "[" * 500
            + "0"
            + "]" * 500
            + "\n---\n",
            encoding="utf-8",
        )
        deep_bootstrap = client.get("/api/bootstrap")
        assert deep_bootstrap.status_code == 200
        assert deep_bootstrap.json()["skills"][0]["status"] == "invalid"
        assert SkillService(database).resolve(["one"])["ok"] is False
        manifest.write_text(original_manifest, encoding="utf-8")

        bound = client.put(
            f"/api/skills/{skill['id']}/servers",
            headers=headers,
            json={"server_ids": [one["id"], two["id"]]},
        )
        assert bound.status_code == 200
        assert [server["alias"] for server in bound.json()["servers"]] == ["one", "two"]

        server_scoped = client.put(
            f"/api/servers/{one['id']}/skills",
            headers=headers,
            json={"skill_ids": [skill["id"]]},
        )
        assert server_scoped.status_code == 200
        assert server_scoped.json()[0]["status"] == "ready"
        assert client.put(
            f"/api/servers/{one['id']}/skills",
            headers=headers,
            json={"skill_ids": [skill["id"], "missing-skill"]},
        ).status_code == 404
        assert [server["alias"] for server in database.get_skill(skill["id"])["servers"]] == [
            "one",
            "two",
        ]

        manifest.unlink()
        missing_bound = client.put(
            f"/api/servers/{one['id']}/skills",
            headers=headers,
            json={"skill_ids": [skill["id"]]},
        )
        assert missing_bound.status_code == 200
        assert missing_bound.json()[0]["status"] == "missing"
        assert SkillService(database).resolve(["one"])["ok"] is False
        manifest.write_text(original_manifest, encoding="utf-8")

        server_scoped = client.put(
            f"/api/servers/{one['id']}/skills",
            headers=headers,
            json={"skill_ids": []},
        )
        assert server_scoped.status_code == 200
        assert [server["alias"] for server in database.get_skill(skill["id"])["servers"]] == ["two"]

        assert client.delete(f"/api/skills/{skill['id']}", headers=headers).status_code == 409
        assert client.put(
            f"/api/skills/{skill['id']}/servers", headers=headers, json={"server_ids": []}
        ).status_code == 200
        removed = client.delete(f"/api/skills/{skill['id']}", headers=headers)
        assert removed.status_code == 200

    assert manifest.exists()
