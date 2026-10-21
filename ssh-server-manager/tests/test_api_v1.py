from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ssh_server_manager import webapp
from ssh_server_manager.cli import main
from ssh_server_manager.db import Database
from ssh_server_manager.ssh_runner import FileOperationError, SSHRunner
from ssh_server_manager.vault import MemoryVault


ORIGIN = "http://localhost:8765"


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing.conf"))
    database = Database()
    database.create_server(alias="box", hostname="box.example.invalid", username="alice", tags=["Lab"])
    vault = MemoryVault()
    monkeypatch.setattr(webapp, "get_vault", lambda: vault)
    app = webapp.create_app(database, launch_token="fixture-launch", port=8765)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, app, database, vault


def login(client):
    assert client.get("/?token=fixture-launch").status_code == 200
    runtime = client.get("/api/v1/runtime")
    assert runtime.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": runtime.json()["csrf"]}


def test_import_api_reports_authentication_fallback_and_preserves_saved_binding(backend, monkeypatch, tmp_path):
    import ssh_server_manager.importer as importer

    client, app, database, vault = backend
    headers = login(client)
    source = tmp_path / "selected config"
    source.write_text("Host box\n  HostName changed.invalid\n  User alice\n", encoding="utf-8")
    credential = database.create_credential(label="Selected agent", kind="agent")
    database.update_server("box", credential_id=credential["id"], notes="Keep these notes")
    monkeypatch.setattr(importer, "_resolve_alias", lambda *args, **kwargs: {
        "alias": "box", "hostname": "changed.invalid", "port": 22, "username": "alice",
        "credential_id": None, "proxy_jumps": [], "source": "ssh-config-import",
        "notes": "Imported", "_identity_files": ["~/.ssh/id_selected"],
    })
    preview = client.post("/api/v1/import/preview", headers=headers, json={"config": str(source)})
    assert preview.status_code == 200
    assert preview.json()["warnings"][0] == {
        "alias": "box", "code": "authentication-not-imported",
        "message": preview.json()["warnings"][0]["message"],
        "source_config": str(source.resolve()), "fallback_config": str(source.resolve()),
        "identity_files": ["~/.ssh/id_selected"],
    }
    applied = client.post("/api/v1/import/apply", headers=headers, json={"config": str(source), "overwrite": True})
    assert applied.status_code == 200
    assert applied.json()["updated"] == ["box"]
    assert applied.json()["warnings"][0]["fallback_config"] == str(source.resolve())
    saved = database.get_server("box")
    assert saved["credential_id"] == credential["id"] and saved["notes"] == "Keep these notes"
    assert database.get_ssh_config_sources() == {saved["id"]: str(source.resolve())}
    assert vault.values == {}


def test_all_v1_routes_require_session_and_mutations_require_csrf(backend, monkeypatch):
    client, app, database, vault = backend
    def deny(*_args, **_kwargs):
        raise AssertionError("unauthorized request reached core/vault")
    monkeypatch.setattr(webapp, "SSHRunner", deny)
    monkeypatch.setattr(webapp, "get_vault", deny)
    routes = {path: operations for path, operations in app.openapi()["paths"].items() if path.startswith("/api/v1/")}
    assert sum(len(operations) for operations in routes.values()) >= 40
    def concrete(template):
        return template.replace("{identifier}", "box").replace("{path_id}", "missing").replace("{name}", "Lab")
    for path_template, operations in routes.items():
        path = concrete(path_template)
        for method in operations:
            assert client.request(method, path, json={}).status_code == 401, (method, path)
    headers = login(client)
    for path_template, operations in routes.items():
        path = concrete(path_template)
        for method in operations.keys() - {"get", "head", "options"}:
            for supplied in ({}, {"X-CSRF-Token": "wrong"}, {**headers, "Origin": "https://attacker.invalid"}):
                response = client.request(method, path, headers=supplied, json={})
                assert response.status_code == 403, (method, path, response.text)
                assert response.headers["cache-control"] == "no-store"
    assert len(database.list_servers()) == 1
    assert vault.values == {}


def test_runtime_and_workspace_are_offline_and_secret_free(backend, monkeypatch, tmp_path):
    client, app, database, _vault = backend
    credential = database.create_credential(label="Password", kind="password", has_secret=True)
    database.update_server("box", credential_id=credential["id"], notes="A saved observation")
    database.create_server_path("box", label="Work", path="~/work")
    skill = tmp_path / "guide" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: box-guide\ndescription: Guide for box.\n---\nBody must stay local.\n")
    app.state.services.skills.register(skill, server_identifiers=["box"])
    def deny(*_args, **_kwargs):
        raise AssertionError("metadata reads must not use SSH/vault")
    monkeypatch.setattr(webapp, "SSHRunner", deny)
    monkeypatch.setattr(webapp, "get_vault", deny)
    login(client)
    runtime = client.get("/api/v1/runtime").json()
    assert runtime["shell"] == "web" and runtime["ui"] == "modern"
    assert runtime["capabilities"]["native_window"] is False
    assert runtime["capabilities"]["max_web_transfer_bytes"] == 100 * 1024 * 1024
    result = client.get("/api/v1/hosts/BOX/workspace")
    assert result.status_code == 200
    workspace = result.json()
    assert workspace["host"]["notes"] == "A saved observation"
    assert workspace["credential"]["has_secret"] is True
    assert workspace["paths"][0]["path"] == "~/work"
    assert workspace["skills"][0]["status"] == "ready"
    assert "Body must stay local" not in result.text
    assert "secret" not in workspace["credential"]
    assert client.get("/api/v1/hosts", params={"query": "observation", "tag": "lab"}).json()[0]["alias"] == "box"
    assert client.get("/api/v1/hosts", params={"tag": "other"}).json() == []
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_host_crud_cli_legacy_parity_and_config_side_effects(backend, capsys, tmp_path):
    client, _app, database, _vault = backend
    headers = login(client)
    created = client.post("/api/v1/hosts", headers=headers, json={
        "alias": "new", "hostname": "new.example.invalid", "username": "bob", "tags": ["Work"],
    })
    assert created.status_code == 201
    assert "Host new" in (tmp_path / "managed.conf").read_text()
    assert main(["server", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == client.get("/api/v1/hosts").json()
    changed = client.patch("/api/v1/hosts/new", headers=headers, json={"port": 2200, "tags": []})
    assert changed.status_code == 200
    assert changed.json()["tags"] == []
    assert "Port 2200" in (tmp_path / "managed.conf").read_text()
    assert client.get("/api/bootstrap").json()["servers"] == client.get("/api/v1/hosts").json()
    assert client.put("/api/v1/hosts/new/notes", headers=headers, json={"text": "first"}).status_code == 200
    assert client.put("/api/v1/hosts/new/notes", headers=headers, json={"text": "second"}).json()["notes"] == "first\n\nsecond"
    assert client.put("/api/v1/hosts/new/notes", headers=headers, json={"mode": "clear"}).json()["notes"] == ""
    duplicate = client.post("/api/v1/hosts", headers=headers, json={"alias": "NEW", "hostname": "a", "username": "a"})
    assert duplicate.status_code == 409
    assert client.get("/api/v1/hosts/missing").status_code == 404
    assert client.patch("/api/v1/hosts/new", headers=headers, json={"username": None}).status_code == 422
    assert client.patch("/api/v1/hosts/new", headers=headers, json={"credential_id": None}).status_code == 200
    # Updates are PATCH-only; PUT would imply full replacement.
    assert client.put("/api/v1/hosts/new", headers=headers, json={"port": 22}).status_code == 405
    assert client.delete("/api/v1/hosts/new", headers=headers).status_code == 200
    assert "Host new\n" not in (tmp_path / "managed.conf").read_text()
    assert len(database.list_servers()) == 1


def test_v1_paths_contexts_and_skill_bindings(backend, tmp_path):
    client, _app, _database, _vault = backend
    headers = login(client)
    result = client.post("/api/v1/hosts/box/paths", headers=headers, json={"label": "Work", "path": "~/work"})
    assert result.status_code == 201
    path_id = result.json()["id"]
    assert client.patch(f"/api/v1/hosts/box/paths/{path_id}", headers=headers, json={"notes": "saved"}).json()["notes"] == "saved"
    assert client.post(f"/api/v1/hosts/box/paths/{path_id}/used", headers=headers).json()["last_used_at"]
    assert client.delete(f"/api/v1/hosts/box/paths/{path_id}", headers=headers).status_code == 200
    assert client.put("/api/v1/hosts/box/paths", headers=headers, json={"paths": [{"label": "App", "path": "/srv/app"}]}).status_code == 200
    box_id = client.get("/api/v1/hosts/box").json()["id"]
    assert client.post("/api/v1/contexts", headers=headers, json={"name": "Work", "server_ids": [box_id]}).status_code == 201
    assert client.patch("/api/v1/contexts/Work", headers=headers, json={"new_name": "Team A/B"}).json()["name"] == "Team A/B"
    # Names are path segments: clients percent-encode spaces and slashes.
    assert client.patch("/api/v1/contexts/Team%20A%2FB", headers=headers, json={"name": "x"}).status_code == 422
    assert client.delete("/api/v1/contexts/Team%20A%2FB", headers=headers).json()["removed_from"] == 1
    assert client.delete("/api/v1/contexts/Team%20A%2FB", headers=headers).status_code == 404
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: box-guide\ndescription: Guide for box.\n---\n")
    result = client.post("/api/v1/skills", headers=headers, json={"path": str(skill), "server_ids": ["box"]})
    assert result.status_code == 201
    skill_id = result.json()["id"]
    assert client.post(f"/api/v1/skills/{skill_id}/refresh", headers=headers).status_code == 200
    assert client.put(f"/api/v1/skills/{skill_id}/servers", headers=headers, json={"server_ids": []}).json()["servers"] == []
    assert client.put("/api/v1/hosts/box/skills", headers=headers, json={"skill_ids": [skill_id]}).status_code == 200
    assert len(client.get("/api/v1/hosts/box/workspace").json()["skills"]) == 1
    assert client.delete(f"/api/v1/skills/{skill_id}", headers=headers).status_code == 409
    assert client.put("/api/v1/hosts/box/skills", headers=headers, json={"skill_ids": []}).status_code == 200
    assert client.delete(f"/api/v1/skills/{skill_id}", headers=headers).status_code == 200


def test_vault_grants_are_canonical_single_use_expiring_and_shared(backend):
    client, app, _database, vault = backend
    headers = login(client)
    created = client.post("/api/v1/credentials", headers=headers, json={"kind": "password", "label": "Password", "secret": "fixture-only"})
    assert created.status_code == 201 and "fixture-only" not in created.text
    credential_id = created.json()["id"]
    assert client.get("/api/v1/credentials").json() == client.get("/api/bootstrap").json()["credentials"]
    assert client.post(f"/api/v1/credentials/{credential_id}/reveal", headers=headers).status_code == 400
    assert client.post("/api/v1/auth/master/enroll", headers=headers, json={"password": "long-fixture-master"}).status_code == 200
    verify = {"password": "long-fixture-master", "credential_id": "Password"}
    assert client.post("/api/v1/auth/master/verify", headers=headers, json=verify).status_code == 200
    revealed = client.post(f"/api/credentials/{credential_id}/reveal", headers=headers)
    assert revealed.json()["value"] == "fixture-only"
    assert revealed.headers["cache-control"] == "no-store"
    assert client.post(f"/api/v1/credentials/{credential_id}/reveal", headers=headers).status_code == 400
    assert client.post("/api/auth/master/verify", headers=headers, json=verify).status_code == 200
    assert client.post("/api/v1/credentials/Password/reveal", headers=headers).json()["value"] == "fixture-only"
    session = app.state.web.sessions[client.cookies[webapp.SESSION_COOKIE]]
    session.reveal_grants[credential_id] = time.time() - 1
    assert client.post(f"/api/v1/credentials/{credential_id}/reveal", headers=headers).status_code == 400
    assert credential_id not in session.reveal_grants
    assert client.patch(f"/api/v1/credentials/{credential_id}", headers=headers, json={"secret": "replacement-fixture"}).status_code == 200
    assert client.delete(f"/api/v1/credentials/{credential_id}", headers=headers).status_code == 200
    assert vault.values == {}


def test_input_errors_output_filtering_and_session_expiry(backend, monkeypatch):
    client, app, database, _vault = backend
    headers = login(client)
    for payload in (
        {"kind": "password", "secret": "do-not-echo"},
        {"kind": "password", "label": "A", "secret": {"value": "do-not-echo"}},
        {"kind": "do-not-echo", "label": "A"},
    ):
        response = client.post("/api/v1/credentials", headers=headers, json=payload)
        assert response.status_code == 422
        assert "do-not-echo" not in response.text
        assert response.json()["error"] == "RequestValidationError"
    monkeypatch.setattr(app.state.services.hosts, "get", lambda _id: {**database.get_server("box"), "value": "do-not-echo"})
    assert "do-not-echo" not in client.get("/api/v1/hosts/box").text
    response = client.get("/api/v1/runtime", headers={"Host": "attacker.invalid"})
    assert response.status_code == 400 and response.headers["cache-control"] == "no-store"
    app.state.web.sessions[client.cookies[webapp.SESSION_COOKIE]].created_at -= 12 * 60 * 60 + 1
    assert client.get("/api/v1/runtime").status_code == 401


def test_file_transfers_share_limits_and_cleanup_with_legacy(backend, monkeypatch, tmp_path):
    client, _app, _database, _vault = backend
    headers = login(client)
    monkeypatch.setattr(webapp, "MAX_WEB_TRANSFER_BYTES", 8)
    size = 4
    def download(_self, identifier, remote_path, target, *, max_bytes=None):
        assert max_bytes == 8
        target.write_bytes(b"x" * size)
        return {"bytes": size}
    def upload(_self, identifier, source, remote_path, *, overwrite=True):
        assert overwrite is False
        return {"alias": "box", "direction": "upload", "remote_path": remote_path, "local_path": str(source), "bytes": source.stat().st_size, "latency_ms": 1}
    monkeypatch.setattr(SSHRunner, "download", download)
    monkeypatch.setattr(SSHRunner, "upload", upload)
    for prefix in ("/api/v1/hosts", "/api/servers"):
        for size, expected in ((4, 200), (9, 413 if prefix.endswith("hosts") else 400)):
            result = client.post(f"{prefix}/box/download", headers=headers, json={"path": "/srv/file"})
            assert result.status_code == expected
            assert result.headers["cache-control"] == "no-store"
            result = client.post(f"{prefix}/box/upload", headers=headers, data={"path": "/srv/file"}, files={"file": ("fixture", b"x" * size)})
            assert result.status_code == expected
            if prefix.endswith("hosts") and expected == 200:
                assert "local_path" not in result.json()
            assert list((tmp_path / "runtime").iterdir()) == []
        assert client.get(f"{prefix}/box/download").status_code == 405
        assert client.post(f"{prefix}/box/download", headers=headers, json={"path": "/srv/.."}).status_code == 400


def test_explicit_diagnostics_directory_and_import_contracts(backend, monkeypatch, tmp_path):
    client, _app, database, _vault = backend
    headers = login(client)
    observed = []
    def test(_self, identifier, *, timeout=8):
        observed.append(("test", identifier, timeout))
        return {"alias": "box", "ok": True, "latency_ms": 1, "error_code": None, "message": "fixture check"}
    def diagnose(_self, identifier, *, timeout=8):
        observed.append(("diagnose", identifier, timeout))
        return {"alias": "box", "checked_at": "2026-10-03T00:00:00+00:00", "overall": "ok", "summary": {"total": 0, "passed": 0, "failed": 0, "warnings": 0, "skipped": 0}, "checks": []}
    def observe(_self, identifier, *, timeout=8):
        observed.append(("observe", identifier, timeout))
        return {"alias": "box", "ok": True, "latency_ms": 2, "error_code": None, "message": "Host info collected"}
    def directory(_self, identifier, path, *, enrich=True):
        observed.append(("files", identifier, path, enrich))
        return {"alias": "box", "path": "/srv", "parent": "/", "entries": [], "total": 0, "truncated": False, "unparsed": 0, "latency_ms": 1, "connection_checked_at": "2026-10-03T00:00:00+00:00"}
    monkeypatch.setattr(SSHRunner, "test", test)
    monkeypatch.setattr(SSHRunner, "diagnose", diagnose)
    monkeypatch.setattr(SSHRunner, "observe", observe)
    monkeypatch.setattr(SSHRunner, "list_directory", directory)
    assert client.post("/api/v1/hosts/box/test", headers=headers, json={"timeout": 10}).json()["ok"]
    assert client.post("/api/v1/hosts/box/diagnose", headers=headers).json()["overall"] == "ok"
    assert client.post("/api/v1/hosts/box/observe", headers=headers, json={"timeout": 12}).json()["ok"]
    assert client.post("/api/v1/hosts/box/observe", headers=headers).json()["ok"]
    assert client.get("/api/v1/hosts/box/files", params={"path": "/srv"}).json()["entries"] == []
    assert observed == [("test", "box", 10), ("diagnose", "box", 8), ("observe", "box", 12), ("observe", "box", 8), ("files", "box", "/srv", False)]
    assert client.post("/api/v1/hosts/box/test", headers=headers, json={"timeout": 0}).status_code == 422
    assert client.post("/api/v1/hosts/box/observe", headers=headers, json={"timeout": 121}).status_code == 422
    preview = client.post("/api/v1/import/preview", headers=headers, json={"config": str(tmp_path / "missing.conf")})
    assert preview.status_code == 200 and preview.json()["items"] == []
    assert client.post("/api/v1/import/apply", headers=headers, json={"config": str(tmp_path / "missing.conf")}).json()["added"] == []
    assert len(database.list_servers()) == 1


def test_offline_contract_matches_artifact(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "scripts" / "export-openapi.py"), "--check", str(root / "contracts" / "api-v1.openapi.json")], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    schema = json.loads((root / "contracts" / "api-v1.openapi.json").read_text())
    assert all(path.startswith("/api/v1/") for path in schema["paths"])
    assert schema["paths"]["/api/v1/hosts"]["post"]["security"] == [{"APIKeyCookie": [], "APIKeyHeader": []}]
    assert "value" not in schema["components"]["schemas"]["Credential"]["properties"]
    assert schema["components"]["schemas"]["PasswordCreate"]["properties"]["secret"]["writeOnly"] is True


def test_cached_host_facts_geo_and_routes_are_offline(backend, monkeypatch):
    client, app, database, _vault = backend
    headers = login(client)
    gateway = database.create_server(alias="gateway", hostname="10.1.2.3", username="root", proxy_jumps=["external"])
    database.update_server("box", proxy_jumps=["gateway"])
    database.record_test("box", status="ok", latency_ms=1, error_code=None, message="ok")
    database.record_test("gateway", status="failed", latency_ms=1, error_code="timeout", message="timeout")
    database.record_observation("box", facts={"os_id": "ubuntu", "os_pretty": "Ubuntu 24.04", "os_like": "debian", "kernel": "\x1bLinux"}, resolved_ip="203.0.113.5")
    updated = client.patch("/api/v1/hosts/box", headers=headers, json={"geo": {"country_code": "jp"}})
    assert updated.status_code == 200
    assert updated.json()["geo"] == {"country_code": "JP", "city": None, "source": "manual"}

    def deny(*_args, **_kwargs):
        raise AssertionError("cached host reads must remain offline")

    monkeypatch.setattr(app.state.services, "runner", deny)
    monkeypatch.setattr(webapp, "get_vault", deny)
    host = client.get("/api/v1/hosts/box").json()
    assert host["facts"]["os_logo"] == "ubuntu"
    assert host["facts"]["os_pretty"] == "Ubuntu 24.04"
    assert host["facts"]["kernel"] == "Linux"
    assert host["last_resolved_ip"] == "203.0.113.5"
    assert host["last_success_at"] is not None
    assert host["proxy_jump_details"][0]["id"] == gateway["id"]
    assert client.get("/api/v1/hosts/gateway").json()["jump_for_count"] == 1
    route = client.get("/api/v1/hosts/box/route").json()
    assert [hop["alias"] for hop in route["hops"]] == ["external", "gateway", "box"]
    assert route["hops"][0]["external"] is True
    assert route["hops"][1]["external"] is False
    assert client.get("/api/v1/hosts/box/workspace").json()["host"] == host
    assert client.patch("/api/v1/hosts/box", headers=headers, json={"geo": None}).json()["geo"] is None
    assert client.patch("/api/v1/hosts/box", headers=headers, json={"geo": {"country_code": "XX"}}).status_code == 400


@pytest.mark.parametrize("operation", ["test", "observe"])
def test_bulk_probes_validate_whole_selection_and_deduplicate(backend, monkeypatch, operation):
    client, app, database, _vault = backend
    headers = login(client)
    host_id = database.get_server("box")["id"]
    calls = []

    def check(identifier, *, timeout):
        calls.append((identifier, timeout))
        return {"alias": "box", "ok": True, "latency_ms": 1, "error_code": None, "message": "ok"}

    monkeypatch.setattr(app.state.services.diagnostics, operation, check)
    endpoint = f"/api/v1/hosts/{operation}"
    assert client.post(endpoint, headers=headers, json={"identifiers": ["box", "missing"]}).status_code == 404
    assert calls == []
    result = client.post(endpoint, headers=headers, json={"identifiers": ["BOX", host_id, "box"], "timeout": 12, "concurrency": 2})
    assert result.status_code == 200 and len(result.json()["results"]) == 1
    assert calls == [(host_id, 12)]
    for body in ({"identifiers": []}, {"identifiers": ["box"] * 65}, {"identifiers": ["box"], "concurrency": 0}, {"identifiers": ["box"], "concurrency": 5}, {"identifiers": ["box"], "timeout": 0}, {"identifiers": ["box"], "timeout": 121}):
        assert client.post(endpoint, headers=headers, json=body).status_code == 422


def test_route_matches_openssh_override_for_explicit_multi_hop(backend, monkeypatch):
    client, _app, database, _vault = backend
    login(client)
    database.create_server(alias="entry", hostname="localhost", username="user", proxy_jumps=["external"])
    database.create_server(alias="irrelevant", hostname="localhost", username="user")
    database.create_server(alias="exit", hostname="localhost", username="user", proxy_jumps=["irrelevant"])
    database.update_server("box", proxy_jumps=["entry", "exit"])
    monkeypatch.setattr("ssh_server_manager.db.utc_now", lambda: "2026-10-15T12:00:00+00:00")
    database.record_test("box", status="ok", latency_ms=1, error_code=None, message="OK")
    monkeypatch.setattr("ssh_server_manager.db.utc_now", lambda: "2026-10-15T13:00:00+00:00")
    database.record_test("irrelevant", status="failed", latency_ms=1, error_code="timeout", message="timeout")
    route = client.get("/api/v1/hosts/box/route").json()
    assert [hop["alias"] for hop in route["hops"]] == ["external", "entry", "exit", "box"]
    assert route["blocked_by"] is None
    database.record_test("exit", status="failed", latency_ms=1, error_code="timeout", message="timeout")
    assert client.get("/api/v1/hosts/box/route").json()["blocked_by"] == "exit"


@pytest.mark.parametrize("operation", ["test", "observe"])
def test_host_deleted_after_batch_resolution_is_a_per_host_failure(backend, monkeypatch, operation):
    client, app, database, _vault = backend
    headers = login(client)
    database.create_server(alias="remaining", hostname="localhost", username="user")

    def check(identifier, *, timeout):
        host = database.get_server(identifier)
        if host["alias"] == "box":
            database.delete_server(identifier)
            database.get_server(identifier)
        return {"alias": host["alias"], "ok": True, "latency_ms": 1, "error_code": None, "message": "ok"}

    monkeypatch.setattr(app.state.services.diagnostics, operation, check)
    result = client.post(f"/api/v1/hosts/{operation}", headers=headers, json={"identifiers": ["box", "remaining"], "concurrency": 1})
    assert result.status_code == 200
    assert [item["ok"] for item in result.json()["results"]] == [False, True]


@pytest.mark.parametrize("operations", [("test", "test"), ("observe", "observe"), ("test", "observe")])
def test_bulk_probes_share_global_concurrency_bound_and_isolate_failures(backend, monkeypatch, operations):
    _client, app, database, _vault = backend
    service = app.state.services.diagnostics
    for index in range(8):
        database.create_server(alias=f"node{index}", hostname="localhost", username="user")
    identifiers = [f"node{index}" for index in range(8)]
    active = peak = 0
    lock = Lock()
    start = Barrier(2)

    def check(identifier, *, timeout):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
        try:
            time.sleep(.015)
            alias = database.get_server(identifier)["alias"]
            if alias == "node0":
                raise FileOperationError("unavailable", "unreachable")
            return {"alias": alias, "ok": True, "latency_ms": 1, "error_code": None, "message": "ok"}
        finally:
            with lock:
                active -= 1

    def batch(operation):
        start.wait(timeout=5)
        return getattr(service, f"{operation}_many")(identifiers, concurrency=4)

    monkeypatch.setattr(service, "test", check)
    monkeypatch.setattr(service, "observe", check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(batch, operations))
    assert peak <= 4 and active == 0
    for result in results:
        assert [item["alias"] for item in result["results"]] == identifiers
        assert result["results"][0]["ok"] is False
        assert all(item["ok"] for item in result["results"][1:])


def test_bulk_observation_backfills_facts_and_retains_previous_on_probe_failure(backend, monkeypatch):
    client, _app, database, _vault = backend
    headers = login(client)
    database.create_server(alias="other", hostname="other.invalid", username="alice")
    database.record_observation("other", facts={"hostname": "other", "os_id": "debian"})
    previous = client.get("/api/v1/hosts/other").json()["facts"]
    monkeypatch.setattr("ssh_server_manager.ssh_runner.collect_probe_metadata", lambda *args, **kwargs: {})
    monkeypatch.setattr(SSHRunner, "_environment", lambda *args: {})
    monkeypatch.setattr("ssh_server_manager.ssh_runner.subprocess.run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, "", ""))

    def probe(_self, host, *, timeout):
        if host["alias"] == "other":
            return {"ok": False, "error_code": "remote-probe-failed", "latency_ms": 1, "message": "Probe unavailable"}
        return {"ok": True, "latency_ms": 1, "message": "ok", "details": {"hostname": "box", "os_id": "ubuntu", "os_pretty": "Ubuntu 24.04", "secret": "must-not-persist"}}

    monkeypatch.setattr(SSHRunner, "_probe_remote_identity", probe)
    response = client.post("/api/v1/hosts/observe", headers=headers, json={"identifiers": ["box", "other"]})
    assert response.status_code == 200
    assert [result["ok"] for result in response.json()["results"]] == [True, False]
    hosts = {host["alias"]: host for host in client.get("/api/v1/hosts").json()}
    assert hosts["box"]["facts"]["os_pretty"] == "Ubuntu 24.04"
    assert hosts["other"]["facts"] == previous
    assert hosts["other"]["last_test_status"] == "ok"
    assert "must-not-persist" not in json.dumps(hosts)


@pytest.mark.parametrize("code,status", [("permission_denied", 403), ("not_found", 404), ("exists", 409), ("too_large", 413), ("not_a_directory", 502), ("auth_failed", 502), ("timeout", 502), ("unreachable", 502), ("unsupported", 501)])
def test_file_errors_have_stable_codes_and_legacy_statuses(backend, monkeypatch, code, status):
    client, app, _database, _vault = backend
    login(client)
    existing = {"name": "file", "path": "/srv/file", "type": "file", "size": 42, "modified_at": "2026-10-15T00:00:00Z", "value": "must-not-leak"} if code == "exists" else None

    def fail(*_args, **_kwargs):
        raise FileOperationError("file operation failed", code, existing=existing)

    monkeypatch.setattr(app.state.services.files, "list_directory", fail)
    result = client.get("/api/v1/hosts/box/files")
    assert result.status_code == status
    assert result.json()["error_code"] == code
    assert "must-not-leak" not in result.text
    if existing:
        assert result.json()["existing"]["size"] == 42
    legacy = client.get("/api/servers/box/files")
    assert legacy.status_code == (400 if code == "too_large" else 409 if code == "exists" else 502)


def test_file_filter_metadata_and_explicit_overwrite_contract(backend, monkeypatch):
    client, app, _database, _vault = backend
    headers = login(client)
    observed = []

    def directory(identifier, path, *, name_filter=None, enrich=True):
        # The v1 listing route skips the probe; the UI requests it separately.
        observed.append((name_filter, enrich))
        return {"alias": "box", "path": "/srv", "parent": "/", "entries": [{"name": "file", "path": "/srv/file", "type": "symlink", "size": 4, "modified": "Oct 15 14:00", "permissions": "lrwxrwxrwx", "owner": "user", "group": "staff", "hidden": False, "uid": 1000, "gid": 100, "link_target": "release", "modified_at": "2026-10-15T14:00:00Z"}], "total": 6000, "matching_total": 1, "truncated": False, "unparsed": 0, "latency_ms": 1, "connection_checked_at": "2026-10-15T00:00:00Z"}

    def upload(identifier, source, path, *, overwrite):
        observed.append(overwrite)
        return {"alias": "box", "direction": "upload", "remote_path": path, "bytes": source.stat().st_size, "latency_ms": 1}

    monkeypatch.setattr(app.state.services.files, "list_directory", directory)
    monkeypatch.setattr(app.state.services.files, "upload", upload)
    result = client.get("/api/v1/hosts/box/files", params={"path": "/srv", "name_filter": "file"})
    assert result.status_code == 200
    assert result.json()["matching_total"] == 1
    assert result.json()["entries"][0]["link_target"] == "release"
    for flag in (None, "true"):
        data = {"path": "/srv/file"}
        if flag:
            data["overwrite"] = flag
        assert client.post("/api/v1/hosts/box/upload", headers=headers, data=data, files={"file": ("file", b"abcd")}).status_code == 200
    assert observed == [("file", False), False, True]
    # Local staging never derives a platform path from an untrusted filename.
    assert client.post("/api/v1/hosts/box/upload", headers=headers, data={"path": "/srv/file"}, files={"file": ("..", b"abcd")}).status_code == 200
    for path in (".", "..", "/srv/../file", "/srv/./file", "/srv/"):
        assert client.post("/api/v1/hosts/box/upload", headers=headers, data={"path": path}, files={"file": ("file", b"data")}).status_code == 400
    capabilities = client.get("/api/v1/runtime").json()["capabilities"]
    assert capabilities["bulk_checks"] and capabilities["host_routes"] and capabilities["file_conflicts"]


def test_file_metadata_probe_is_a_separate_csrf_protected_request(backend, monkeypatch):
    client, app, _database, _vault = backend
    headers = login(client)
    observed = []

    def enrich(identifier, path, claims):
        observed.append((identifier, path, claims))
        return {"entries": {"file": {"uid": 1000, "gid": 100, "owner": "user", "group": "staff",
                                     "modified_at": "2026-10-15T14:00:00+00:00", "link_target": "release"}}}

    monkeypatch.setattr(app.state.services.files, "enrich_directory", enrich)
    body = {"path": "/srv", "entries": [{"name": "file", "type": "file", "size": 4, "uid": 1000, "gid": 100}]}
    result = client.post("/api/v1/hosts/box/files/metadata", headers=headers, json=body)
    assert result.status_code == 200
    assert result.json() == {"path": "/srv", "entries": {"file": {
        "uid": 1000, "gid": 100, "owner": "user", "group": "staff",
        "modified_at": "2026-10-15T14:00:00+00:00", "link_target": "release",
    }}}
    assert observed == [("box", "/srv", [{"name": "file", "type": "file", "size": 4, "uid": 1000, "gid": 100}])]
    # A read-only probe is still a remote operation, so it needs the CSRF header.
    assert client.post("/api/v1/hosts/box/files/metadata", json=body).status_code == 403
    # The DTO refuses a claim the server would have to guess about.
    for bad in (
        {"path": "/srv", "entries": [{"name": "file", "type": "socket", "size": 4}]},
        {"path": "/srv", "entries": [{"name": "file", "type": "file", "size": -1}]},
        {"path": "/srv", "entries": [{"name": "file", "type": "file", "size": 4, "uid": -2}]},
        {"path": "/srv", "entries": [{"name": "file", "type": "file", "size": 4, "unknown": 1}]},
        {"path": "", "entries": []},
    ):
        assert client.post("/api/v1/hosts/box/files/metadata", headers=headers, json=bad).status_code == 422
