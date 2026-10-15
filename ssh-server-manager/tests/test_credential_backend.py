from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from ssh_server_manager.application import CredentialApplicationService
from ssh_server_manager.credential_keys import load_snapshot, read_snapshot
from ssh_server_manager.credential_lock import credential_lock
from ssh_server_manager.db import ConflictError, Database, NotFoundError, SCHEMA_VERSION
from ssh_server_manager.local_metadata import LocalKeyBrowser, agent_metadata
from ssh_server_manager.service import CredentialService
from ssh_server_manager.validation import ValidationError
from ssh_server_manager.vault import MemoryVault, VaultError
from test_api_v1 import backend, login


def key_fixture(path: Path, password: bytes | None = None, *, openssh: bool = False) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    content = key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH if openssh else serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption())
    path.write_bytes(content)
    if os.name != "nt":
        path.chmod(0o600)
    return serialization.ssh_key_fingerprint(key.public_key(), hashes.SHA256())


def test_key_metadata_locked_nullable_and_both_formats(tmp_path):
    import base64
    for openssh in (False, True):
        path = tmp_path / ("ssh" if openssh else "pem")
        fingerprint = key_fixture(path, b"  passphrase  ", openssh=openssh)
        locked = load_snapshot(read_snapshot(str(path)), allow_locked=True)
        assert locked["status"] == "passphrase_required"
        assert locked["encrypted"] is True
        assert locked["algorithm"] is None and locked["fingerprint"] is None
        valid = load_snapshot(read_snapshot(str(path)), "  passphrase  ")
        assert valid["algorithm"] == "ED25519"
        assert valid["fingerprint"] == "SHA256:" + base64.b64encode(fingerprint).decode().rstrip("=")
        assert valid["format"] == ("openssh" if openssh else "pem")
        if os.name != "nt":
            assert valid["permissions"] == {"method": "posix_mode", "assessment": "secure", "mode": "0600"}
        with pytest.raises(ValidationError):
            load_snapshot(read_snapshot(str(path)), "passphrase")
        assert "passphrase" not in json.dumps(valid)


def test_wrong_passphrase_is_never_saved_and_blank_preserves_secret(tmp_path):
    database = Database(tmp_path / "manager.db")
    vault = MemoryVault()
    service = CredentialService(database, vault)
    path = tmp_path / "key"
    key_fixture(path, b" secret with spaces ")
    with pytest.raises(ValidationError):
        service.create_key("Bad", str(path), "wrong")
    assert database.list_credentials() == [] and vault.values == {}
    credential = service.create_key("Key", str(path), " secret with spaces ")
    original = database.get_credential(credential["id"])
    assert service.update(credential["id"], passphrase="") == original
    with pytest.raises(ValidationError):
        service.update(credential["id"], passphrase="wrong")
    assert service.reveal(credential["id"])["value"] == " secret with spaces "
    assert database.get_credential(credential["id"])["updated_at"] == original["updated_at"]
    cleared = service.update(credential["id"], clear_passphrase=True)
    assert cleared["has_passphrase"] is False and cleared["key_metadata"] is None
    assert vault.values == {}
    password = service.create_password("Password", "  unchanged  ")
    service.update(password["id"], secret="")
    assert service.reveal(password["id"])["value"] == "  unchanged  "
    assert b"secret with spaces" not in database.path.read_bytes()


def test_key_replacement_during_db_commit_restores_vault_and_metadata(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    vault = MemoryVault()
    service = CredentialService(database, vault)
    path = tmp_path / "key"
    key_fixture(path, b"original")
    credential = service.create_key("Key", str(path), "original")
    key_fixture(path, b"replacement")
    original_update = database.update_credential
    def racing_update(identifier, **changes):
        path.write_bytes(b"replaced after validation")
        return original_update(identifier, **changes)
    monkeypatch.setattr(database, "update_credential", racing_update)
    with pytest.raises(ConflictError):
        service.update(credential["id"], passphrase="replacement")
    assert database.get_credential(credential["id"]) == credential
    assert vault.get_secret(credential["id"], "passphrase") == "original"


def test_schema_six_migration_preserves_credentials_and_host_facts(tmp_path):
    path = tmp_path / "manager.db"
    database = Database(path)
    credential = database.create_credential(label="Saved", kind="password", has_secret=True)
    host = database.create_server(alias="box", hostname="127.0.0.1", username="user", credential_id=credential["id"])
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE servers SET facts = ?, geo = ? WHERE id = ?",
            (json.dumps({"observed_at": "before", "os": "Linux"}), json.dumps({"country_code": "US", "source": "manual"}), host["id"]))
        connection.execute("DROP TABLE credential_tests")
        connection.execute("DROP TABLE credential_deletions")
        connection.execute("ALTER TABLE credentials DROP COLUMN key_metadata")
        connection.execute("ALTER TABLE credentials DROP COLUMN revision")
        connection.execute("PRAGMA user_version = 6")
    migrated = Database(path)
    assert migrated.get_credential(credential["id"])["has_secret"] is True
    assert migrated.get_credential(credential["id"])["last_test"] is None
    assert migrated.get_server(host["id"])["facts"]["os"] == "Linux"
    assert migrated.get_server(host["id"])["geo"]["country_code"] == "US"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_deletion_preview_conflict_commit_and_persistent_retry(backend, monkeypatch):
    client, app, database, vault = backend
    headers = login(client)
    credential = client.post("/api/v1/credentials", headers=headers,
        json={"kind": "password", "label": "Saved", "secret": "fixture-secret"}).json()
    database.update_server("box", credential_id=credential["id"])
    url = f"/api/v1/credentials/{credential['id']}"
    assert client.delete(url, headers=headers).status_code == 409
    preview = client.get(url + "/deletion-preview").json()
    payload = {"operation_id": str(uuid.uuid4()), "expected_updated_at": preview["expected_updated_at"],
        "expected_host_ids": preview["expected_host_ids"]}
    changed = database.create_server(alias="second", hostname="127.0.0.1", username="u", credential_id=credential["id"])
    assert client.post(url + "/detach-delete", headers=headers, json=payload).status_code == 409
    assert database.get_server("box")["credential_id"] == credential["id"]
    database.delete_server(changed["id"])
    _second_id, other = app.state.web.create_session()
    for session in app.state.web.sessions.values():
        session.reveal_grants[credential["id"]] = time.time() + 30
    original_delete = vault.delete_secret
    def unavailable(identifier, slot):
        if slot == "password":
            raise VaultError("fixture failure")
        original_delete(identifier, slot)
    monkeypatch.setattr(vault, "delete_secret", unavailable)
    committed = client.post(url + "/detach-delete", headers=headers, json=payload)
    assert committed.status_code == 202
    operation = committed.json()
    assert operation["committed"] is True and operation["cleanup_pending"] is True
    assert operation["remaining_slots"] == ["password"]
    assert all(credential["id"] not in session.reveal_grants for session in app.state.web.sessions.values())
    assert database.get_server("box")["credential_id"] is None
    assert client.get(url).status_code == 404
    assert client.get("/api/v1/credential-deletions?pending=true").json()[0]["operation_id"] == payload["operation_id"]
    assert client.post(url + "/detach-delete", headers=headers, json=payload).status_code == 202
    conflicting = {**payload, "expected_host_ids": []}
    assert client.post(url + "/detach-delete", headers=headers, json=conflicting).status_code == 409
    reloaded = Database(database.path)
    assert reloaded.get_credential_deletion(payload["operation_id"])["cleanup_pending"] is True
    monkeypatch.setattr(vault, "delete_secret", original_delete)
    retried = client.post(f"/api/v1/credential-deletions/{payload['operation_id']}/retry", headers=headers)
    assert retried.status_code == 200 and retried.json()["cleanup_pending"] is False
    assert client.get("/api/v1/credential-deletions?pending=true").json() == []
    assert vault.values == {}


def test_delete_preserves_credential_fields_and_reports_config_cleanup_failure(backend, monkeypatch):
    client, app, _database, _vault = backend
    headers = login(client)
    credential = client.post("/api/v1/credentials", headers=headers,
        json={"kind": "agent", "label": "Agent"}).json()
    def fail_render():
        raise OSError("fixture")
    monkeypatch.setattr(app.state.services.credentials, "render", fail_render)
    response = client.delete(f"/api/v1/credentials/{credential['id']}", headers=headers)
    assert response.status_code == 202 and response.json()["id"] == credential["id"]
    assert response.json()["cleanup_pending"] is True
    operation = client.get("/api/v1/credential-deletions?pending=true").json()[0]
    assert operation["remaining_slots"] == ["config"] and operation["error_code"] == "config_unavailable"


def test_label_detach_delete_retries_old_operation_after_label_reuse(backend, monkeypatch):
    client, _app, database, vault = backend
    headers = login(client)
    credential = client.post("/api/v1/credentials", headers=headers,
        json={"kind": "password", "label": "Shared", "secret": "old-fixture-secret"}).json()
    database.update_server("box", credential_id=credential["id"])
    url = "/api/v1/credentials/sHaReD"
    preview = client.get(url + "/deletion-preview").json()
    payload = {"operation_id": str(uuid.uuid4()), "expected_updated_at": preview["expected_updated_at"],
        "expected_host_ids": preview["expected_host_ids"]}
    original_delete = vault.delete_secret

    def unavailable(identifier, slot):
        if slot == "password":
            raise VaultError("fixture cleanup failure")
        original_delete(identifier, slot)

    monkeypatch.setattr(vault, "delete_secret", unavailable)
    first = client.post(url + "/detach-delete", headers=headers, json=payload)
    assert first.status_code == 202 and first.json()["credential_id"] == credential["id"]
    replacement = client.post("/api/v1/credentials", headers=headers,
        json={"kind": "password", "label": "Shared", "secret": "new-fixture-secret"}).json()
    assert replacement["id"] != credential["id"]
    monkeypatch.setattr(vault, "delete_secret", original_delete)
    retried = client.post(url + "/detach-delete", headers=headers, json=payload)
    assert retried.status_code == 200 and retried.json()["cleanup_pending"] is False
    assert database.get_credential("Shared")["id"] == replacement["id"]
    assert vault.get_secret(replacement["id"], "password") == "new-fixture-secret"
    assert vault.get_secret(credential["id"], "password") is None
    assert client.post(url + "/detach-delete", headers=headers, json=payload).json() == retried.json()
    assert client.post(f"/api/v1/credentials/{replacement['id']}/detach-delete",
        headers=headers, json=payload).status_code == 409


def test_label_detach_delete_waits_for_uuid_probe_lock(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    vault = MemoryVault()
    service = CredentialApplicationService(database, lambda: vault, lambda: tmp_path / "managed")
    credential = service.create_password("Shared", "fixture-secret")
    host = database.create_server(alias="box", hostname="127.0.0.1", username="u", credential_id=credential["id"])
    preview = service.deletion_preview("Shared")
    started, finish, attempted = Event(), Event(), Event()
    import ssh_server_manager.credential_probe as module

    class BlockingRunner:
        def __init__(self, db, factory):
            pass

        def stream(self, identifier, host_id, **options):
            started.set()
            assert finish.wait(3)
            assert vault.get_secret(identifier, "password") == "fixture-secret"
            yield {"type": "result", "result": {"ok": True, "message": "Authenticated", "error_code": None,
                "latency_ms": 1, "checked_at": "2026-10-15T00:00:00+00:00", "host_id": host_id,
                "host_alias": host["alias"], "credential_id": identifier, "method": "password", "stages": []}}

    monkeypatch.setattr(module, "CredentialProbeRunner", BlockingRunner)

    def deleting():
        attempted.set()
        return service.detach_delete("sHaReD", operation_id=str(uuid.uuid4()),
            expected_updated_at=preview["expected_updated_at"], expected_host_ids=preview["expected_host_ids"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        probe = pool.submit(lambda: list(service.test_stream(credential["id"], host["id"])))
        assert started.wait(2)
        deletion = pool.submit(deleting)
        assert attempted.wait(2)
        with pytest.raises(TimeoutError):
            deletion.result(timeout=0.1)
        finish.set()
        assert probe.result(timeout=3)[-1]["result"]["ok"] is True
        assert deletion.result(timeout=3)["status"] == "complete"
    assert database.get_server(host["id"])["credential_id"] is None
    assert vault.values == {}


def test_session_key_revision_detects_changes_and_cross_session_use(backend, tmp_path):
    client, app, database, vault = backend
    headers = login(client)
    path = tmp_path / "key"
    key_fixture(path, b"  fixture-pass  ")
    inspected = client.post("/api/v1/credentials/key/inspect", headers=headers, json={"key_path": str(path)}).json()
    assert inspected["key_metadata"]["fingerprint"] is None
    valid = client.post("/api/v1/credentials/key/validate", headers=headers, json={"key_path": str(path),
        "passphrase": "  fixture-pass  ", "expected_key_revision": inspected["key_revision"]})
    assert valid.status_code == 200
    assert "fixture-pass" not in valid.text
    create = {"kind": "key", "label": "Key", "key_path": str(path), "passphrase": "  fixture-pass  ",
        "expected_key_revision": valid.json()["key_revision"]}
    created = client.post("/api/v1/credentials", headers=headers, json=create)
    assert created.status_code == 201
    credential = created.json()
    stored = client.post("/api/v1/credentials/key/validate", headers=headers, json={"key_path": str(path),
        "credential_id": credential["id"], "use_stored_passphrase": True})
    assert stored.status_code == 200
    other_id, other = app.state.web.create_session()
    client.cookies.set("ssm_session", other_id)
    rejected = client.post("/api/v1/credentials/key/validate", headers={**headers, "X-CSRF-Token": other.csrf},
        json={"key_path": str(path), "passphrase": "  fixture-pass  ", "expected_key_revision": inspected["key_revision"]})
    assert rejected.status_code == 400
    from ssh_server_manager.webapp import SESSION_COOKIE
    # Restore the existing session using its cookie name from the implementation.
    active_id = next(identifier for identifier in app.state.web.sessions if identifier != other_id)
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, active_id)
    key_fixture(path, b"new-password")
    create["label"] = "Changed"
    conflict = client.post("/api/v1/credentials", headers=headers, json=create)
    assert conflict.status_code == 409
    assert len(database.list_credentials()) == 1
    assert vault.get_secret(credential["id"], "passphrase") == "  fixture-pass  "


def test_picker_scope_is_session_bound_and_never_reads_private_bytes(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    child = root / "subdir"
    child.mkdir()
    private = root / "private"
    private.write_bytes(b"never enumerate contents")
    outside = tmp_path / "outside"
    outside.mkdir()
    browser = LocalKeyBrowser()
    scope = browser.scope("one", str(root))
    def deny(*args, **kwargs):
        raise AssertionError("key bytes were read")
    monkeypatch.setattr(Path, "read_bytes", deny)
    listing = browser.list("one", scope["scope_id"])
    assert listing["parent"] is None
    assert [entry["kind"] for entry in listing["entries"]] == ["directory", "file"]
    assert "never enumerate" not in json.dumps(listing)
    for session, path in (("two", None), ("one", str(outside)), ("one", str(root / ".."))):
        with pytest.raises(ValidationError):
            browser.list(session, scope["scope_id"], path)
    if os.name != "nt":
        (root / "escape").symlink_to(outside, target_is_directory=True)
        assert all(entry["name"] != "escape" for entry in browser.list("one", scope["scope_id"])["entries"])
        with pytest.raises(ValidationError):
            browser.list("one", scope["scope_id"], str(root / "escape"))
    token = browser.revision("one", str(private), "revision")
    browser.clean(set())
    with pytest.raises(ValidationError):
        browser.list("one", scope["scope_id"])
    with pytest.raises(ValidationError):
        browser.expected_revision("one", token, str(private))


def test_agent_metadata_uses_only_list_command_and_filters_output(monkeypatch):
    import ssh_server_manager.local_metadata as module
    monkeypatch.setattr(module.shutil, "which", lambda command: "ssh-add")
    def listing(arguments, **kwargs):
        assert arguments == ["ssh-add", "-l", "-E", "sha256"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stdout=b"256 SHA256:abc123 local-comment (ED25519)\ninvalid output\n", stderr=b"not exposed")
    monkeypatch.setattr(module.subprocess, "run", listing)
    metadata = agent_metadata()
    assert metadata["status"] == "ready"
    assert metadata["identities"] == [{"bits": 256, "fingerprint": "SHA256:abc123", "comment": "local-comment", "algorithm": "ED25519"}]
    assert "invalid output" not in json.dumps(metadata) and "not exposed" not in json.dumps(metadata)


def test_credential_lock_is_reentrant_and_cross_process(tmp_path):
    path = tmp_path / "manager.db"
    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    code = "from pathlib import Path\nfrom ssh_server_manager.credential_lock import credential_lock\nfrom ssh_server_manager.validation import ValidationError\nimport sys\ntry:\n with credential_lock(Path(sys.argv[1]), 'same', timeout=.1):\n  print('acquired')\nexcept ValidationError:\n print('busy')\n"
    with credential_lock(path, "same"):
        with credential_lock(path, "same"):
            process = subprocess.run([sys.executable, "-c", code, str(path)],
                env={**os.environ, "PYTHONPATH": scripts}, capture_output=True, text=True, timeout=5)
            assert process.returncode == 0 and process.stdout.strip() == "busy"
    process = subprocess.run([sys.executable, "-c", code, str(path)],
        env={**os.environ, "PYTHONPATH": scripts}, capture_output=True, text=True, timeout=5)
    assert process.stdout.strip() == "acquired"


def test_ndjson_probe_updates_only_credential_history_and_accepts_unbound_host(backend, monkeypatch):
    client, app, database, _vault = backend
    headers = login(client)
    credential = client.post("/api/v1/credentials", headers=headers,
        json={"kind": "agent", "label": "Agent"}).json()
    saved_host = database.get_server("box")
    stage = {"type": "stage", "stage": "authenticate", "status": "ok", "message": "Authenticated", "method": "publickey"}
    result = {"ok": True, "error_code": None, "message": "Authenticated", "latency_ms": 2,
        "checked_at": "2026-10-15T00:00:00+00:00", "host_id": saved_host["id"], "host_alias": "box",
        "credential_id": credential["id"], "credential_kind": "agent", "method": "publickey", "stages": [stage]}
    import ssh_server_manager.credential_probe as module
    class FakeRunner:
        def __init__(self, db, vault):
            assert db is database
        def stream(self, selected, host, **options):
            assert selected == credential["id"] and host == saved_host["id"]
            yield stage
            yield {"type": "result", "result": result}
    monkeypatch.setattr(module, "CredentialProbeRunner", FakeRunner)
    response = client.post(f"/api/v1/credentials/{credential['id']}/test", headers=headers,
        json={"host_id": saved_host["id"]})
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["stage", "result"]
    assert events[0]["method"] == "publickey" and events[-1]["result"]["ok"] is True
    assert database.get_server("box") == saved_host
    history = client.get(f"/api/v1/credentials/{credential['id']}/tests").json()
    assert history[0]["host_id"] == saved_host["id"]
    assert client.get("/api/v1/credentials").json()[0]["last_test"]["method"] == "publickey"


def test_runtime_expiry_and_vault_metadata_are_offline(backend, monkeypatch):
    client, app, _database, _vault = backend
    headers = login(client)
    def deny():
        raise AssertionError("runtime touched vault")
    monkeypatch.setattr(app.state.services.credentials, "vault", deny)
    data = client.get("/api/v1/runtime").json()
    expires = datetime.fromisoformat(data["session"]["expires_at"]).timestamp()
    from ssh_server_manager.webapp import SESSION_COOKIE
    session = app.state.web.sessions[client.cookies[SESSION_COOKIE]]
    assert expires == pytest.approx(session.created_at + 12 * 60 * 60)
    assert data["vault"] == {"name": None, "status": "unknown"}
    assert data["capabilities"]["local_key_browser"] is True
    session.created_at -= 12 * 60 * 60 + 1
    assert client.get("/api/v1/runtime").status_code == 401


def test_schema_five_to_seven_preserves_reusable_credentials_and_checks(tmp_path):
    path = tmp_path / "manager.db"
    database = Database(path)
    credential = database.create_credential(label="Reusable", kind="password", has_secret=True)
    host = database.create_server(alias="box", hostname="127.0.0.1", username="u", credential_id=credential["id"], notes="Saved")
    database.record_test(host["id"], status="ok", latency_ms=1, error_code=None, message="OK")
    old_host = database.get_server(host["id"])
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE credential_tests")
        connection.execute("DROP TABLE credential_deletions")
        for column in ("key_metadata", "revision"):
            connection.execute(f"ALTER TABLE credentials DROP COLUMN {column}")
        for column in ("facts", "last_success_at", "last_resolved_ip", "host_key", "geo"):
            connection.execute(f"ALTER TABLE servers DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 5")
    migrated = Database(path)
    assert migrated.get_credential(credential["id"])["has_secret"] is True
    assert migrated.get_credential(credential["id"])["label"] == "Reusable"
    assert migrated.get_server(host["id"])["credential_id"] == credential["id"]
    assert migrated.get_server(host["id"])["notes"] == "Saved"
    assert migrated.get_server(host["id"])["last_success_at"] == old_host["last_test_at"]


def test_partial_create_failure_leaves_durable_cleanup_instead_of_an_orphan(tmp_path):
    database = Database(tmp_path / "manager.db")
    class PartialVault(MemoryVault):
        unavailable = True
        def set_secret(self, identifier, slot, value):
            super().set_secret(identifier, slot, value)
            raise VaultError("fixture partial write")
        def delete_secret(self, identifier, slot):
            if self.unavailable:
                raise VaultError("fixture unavailable")
            super().delete_secret(identifier, slot)
    vault = PartialVault()
    with pytest.raises(VaultError):
        CredentialService(database, vault).create_password("Partial", "fixture-only")
    assert database.list_credentials() == []
    pending = Database(database.path).list_credential_deletions(pending=True)
    assert len(pending) == 1 and pending[0]["remaining_slots"] == ["password", "passphrase"]
    assert "fixture-only" not in json.dumps(pending)
    vault.unavailable = False
    service = CredentialApplicationService(Database(database.path), lambda: vault, lambda: tmp_path / "managed")
    assert service.retry_deletion(pending[0]["operation_id"])["cleanup_pending"] is False
    assert vault.values == {}


def test_probe_blocks_update_and_invalidates_cached_success_on_new_revision(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    vault = MemoryVault()
    service = CredentialApplicationService(database, lambda: vault, lambda: tmp_path / "managed")
    credential = service.create_password("Password", "before")
    host = database.create_server(alias="box", hostname="127.0.0.1", username="u")
    started, finish = Event(), Event()
    import ssh_server_manager.credential_probe as module
    class BlockingRunner:
        def __init__(self, db, factory):
            pass
        def stream(self, identifier, host_id, **options):
            started.set()
            assert finish.wait(3)
            assert vault.get_secret(identifier, "password") == "before"
            yield {"type": "result", "result": {"ok": True, "message": "Authenticated", "error_code": None,
                "latency_ms": 1, "checked_at": "2026-10-15T00:00:00+00:00", "host_id": host_id,
                "host_alias": host["alias"], "credential_id": identifier, "method": "password", "stages": []}}
    monkeypatch.setattr(module, "CredentialProbeRunner", BlockingRunner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        probe = pool.submit(lambda: list(service.test_stream(credential["id"], host["id"])))
        assert started.wait(2)
        update = pool.submit(service.update, credential["id"], secret="after")
        assert not update.done()
        finish.set()
        result = probe.result(timeout=3)[-1]["result"]
        updated = update.result(timeout=3)
    assert result["credential_revision"] == credential["revision"]
    assert updated["last_test"] is None
    assert database.list_credential_tests(credential["id"])[0]["ok"] is True
    assert vault.get_secret(credential["id"], "password") == "after"


def test_history_retention_is_bounded_and_orders_by_completion(tmp_path):
    database = Database(tmp_path / "manager.db")
    credential = database.create_credential(label="Agent", kind="agent")
    for index in range(55):
        database.record_credential_test(credential["id"], {"ok": False, "host_id": "host", "checked_at": str(100 - index), "message": str(index)})
    history = database.list_credential_tests(credential["id"])
    assert len(history) == 50 and history[0]["message"] == "54" and history[-1]["message"] == "5"
    assert database.get_credential(credential["id"])["last_test"]["message"] == "54"
