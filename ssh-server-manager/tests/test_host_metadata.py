from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from ssh_server_manager.db import Database, SCHEMA_VERSION
from ssh_server_manager.host_metadata import address_scope, sanitize_facts, validate_geo
from ssh_server_manager.validation import ValidationError


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example.invalid", username="alice")
    return database


@pytest.mark.parametrize(("hostname", "scope"), [
    ("10.0.0.1", "private"), ("172.31.255.255", "private"), ("192.168.1.9", "private"),
    ("172.32.0.1", "public"), ("1.1.1.1", "public"), ("2606:4700:4700::1111", "public"),
    ("[fd12::1]", "private"), ("127.0.0.1", "loopback"), ("::1", "loopback"),
    ("localhost", "loopback"), ("dev.localhost.", "loopback"), ("printer.local.", "lan"),
    ("server.lan", "lan"), ("storage.home.arpa", "lan"), ("169.254.1.1", "lan"),
    ("fe80::1", "lan"), ("example.invalid", "unresolved"), ("0.0.0.0", "unresolved"),
])
def test_address_scope_is_literal_and_offline(hostname, scope):
    assert address_scope(hostname) == scope


@pytest.mark.parametrize(("os_id", "os_like", "logo"), [
    ("ubuntu", "debian", "ubuntu"), ("rocky", "rhel", "rocky"),
    ("almalinux", "rhel fedora", "rhel"), ("custom-distro", "debian", "debian"),
    ("macos", "", "macos"), ("windows", "", "windows"), ("freebsd", "", "freebsd"),
    ("arbitrary/remote.svg", "", "unknown"),
])
def test_facts_have_closed_logo_and_bounded_untrusted_fields(os_id, os_like, logo):
    facts = sanitize_facts({"os_id": os_id, "os_like": os_like, "os": "Ubuntu\x00\x1b\u202e24.04", "cpu_model": "x" * 1000, "secret": "must-not-surface", "os_logo": "evil.svg"})
    assert facts["os_logo"] == logo
    assert facts["os"] == "Ubuntu24.04"
    assert len(facts["cpu_model"]) == 500
    assert "secret" not in facts
    assert facts["observed_at"].endswith("+00:00")


def test_observations_and_last_success_survive_failed_checks(database, monkeypatch):
    monkeypatch.setattr("ssh_server_manager.db.utc_now", lambda: "2026-10-15T12:00:00+00:00")
    success = database.record_test("box", status="ok", latency_ms=5, error_code=None, message="OK")
    recorded = database.record_observation("box", facts={"os_id": "ubuntu", "os": "Ubuntu 24.04"}, resolved_ip="10.1.2.3", host_key={"fingerprint": "SHA256:" + "A" * 43, "state": "verified"}, observed_at="2026-10-15T08:00:00-04:00")
    monkeypatch.setattr("ssh_server_manager.db.utc_now", lambda: "2026-10-16T12:00:00+00:00")
    failed = database.record_test("box", status="failed", latency_ms=10, error_code="timeout", message="Timed out")
    assert failed["last_test_at"] == "2026-10-16T12:00:00+00:00"
    assert failed["last_success_at"] == success["last_success_at"] == "2026-10-15T12:00:00+00:00"
    assert failed["facts"] == recorded["facts"]
    assert failed["last_resolved_ip"] == "10.1.2.3"
    assert failed["host_key"]["state"] == "verified"
    assert database.record_observation("box")["facts"] == recorded["facts"]
    changed = database.record_observation("box", host_key={"fingerprint": None, "state": "changed"})
    assert changed["facts"] == recorded["facts"]
    assert changed["host_key"]["state"] == "changed"


def test_checks_in_same_second_retain_their_order(database, monkeypatch):
    moments = iter([
        datetime(2026, 10, 15, 12, 0, 0, 100000, tzinfo=timezone.utc),
        datetime(2026, 10, 15, 12, 0, 0, 200000, tzinfo=timezone.utc),
    ])
    class Clock:
        @staticmethod
        def now(_timezone):
            return next(moments)
    monkeypatch.setattr("ssh_server_manager.db.datetime", Clock)
    first = database.record_test("box", status="ok", latency_ms=1, error_code=None, message="OK")
    second = database.record_test("box", status="failed", latency_ms=1, error_code="timeout", message="Timed out")
    assert first["last_test_at"] < second["last_test_at"]
    assert first["last_test_at"].split(".")[0] == second["last_test_at"].split(".")[0]
    assert second["last_success_at"] == first["last_test_at"]


def test_inventory_reads_do_not_probe_network_or_vault(database, monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("metadata read attempted external work")
    monkeypatch.setattr("socket.getaddrinfo", deny)
    monkeypatch.setattr("subprocess.run", deny)
    monkeypatch.setattr("ssh_server_manager.vault.get_vault", deny)
    assert database.get_server("box")["address_scope"] == "unresolved"
    assert database.list_servers()[0]["facts"] is None


def test_manual_geo_can_be_saved_and_cleared(database):
    geo = database.update_server("box", geo={"country_code": "jp", "city": " Tokyo "})["geo"]
    assert geo == {"country_code": "JP", "city": "Tokyo", "source": "manual"}
    assert database.update_server("box", notes="Keep the override")["geo"] == geo
    assert database.update_server("box", geo=None)["geo"] is None
    assert validate_geo({"country_code": "US"}) == {"country_code": "US", "city": None, "source": "manual"}


@pytest.mark.parametrize("value", [{"country_code": "ZZ"}, {"country_code": "USA"}, {"country_code": "JP", "city": "bad\ncity"}, {"country_code": "JP", "source": "geoip"}, {"country_code": "JP", "url": "https://external.invalid"}, "JP"])
def test_manual_geo_rejects_invalid_data(database, value):
    with pytest.raises(ValidationError):
        database.update_server("box", geo=value)
    assert database.get_server("box")["geo"] is None


def test_endpoint_change_invalidates_derived_observations_but_retains_manual_geo(database):
    database.update_server("box", geo={"country_code": "JP"})
    database.record_test("box", status="ok", latency_ms=1, error_code=None, message="OK")
    database.record_observation("box", facts={"os_id": "ubuntu"}, resolved_ip="10.0.0.1", host_key={"fingerprint": "SHA256:" + "A" * 43, "state": "verified"})
    assert database.update_server("box", username="bob")["facts"] is not None
    changed = database.update_server("box", port=2222)
    assert all(changed[field] is None for field in ("facts", "last_resolved_ip", "host_key", "last_test_at", "last_success_at"))
    assert changed["geo"]["country_code"] == "JP"


def test_late_results_for_previous_endpoint_cannot_repopulate_observations(database):
    previous = database.get_server("box")
    endpoint = (previous["hostname"], previous["port"])
    database.update_server("box", hostname="replacement.example.invalid")
    checked = database.record_test("box", status="ok", latency_ms=1, error_code=None, message="old endpoint", expected_endpoint=endpoint)
    assert checked["last_test_at"] is None
    observed = database.record_observation("box", facts={"os_id": "ubuntu"}, resolved_ip="10.0.0.1", host_key={"fingerprint": "SHA256:" + "A" * 43, "state": "verified"}, expected_endpoint=endpoint)
    assert observed["facts"] is None and observed["host_key"] is None


def test_schema_five_migration_preserves_profile_and_backfills_last_success(tmp_path):
    path = tmp_path / "manager.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE credentials(id TEXT PRIMARY KEY, label TEXT, kind TEXT);
            CREATE TABLE servers(
                id TEXT PRIMARY KEY, alias TEXT, hostname TEXT, port INTEGER, username TEXT,
                credential_id TEXT, proxy_jumps TEXT, tags TEXT, notes TEXT, source TEXT,
                last_test_at TEXT, last_test_status TEXT, last_test_latency_ms INTEGER,
                last_test_error_code TEXT, last_test_message TEXT, created_at TEXT, updated_at TEXT
            );
            INSERT INTO servers VALUES('host-1','box','10.0.0.1',22,'alice',NULL,'[]','[]','saved','managed',
                '2026-10-14T00:00:00+00:00','ok',5,NULL,'OK','2026-01-01','2026-10-14');
            PRAGMA user_version = 5;
        """)
    migrated = Database(path).get_server("box")
    assert migrated["id"] == "host-1" and migrated["notes"] == "saved"
    assert migrated["last_success_at"] == migrated["last_test_at"]
    assert migrated["facts"] is None and migrated["geo"] is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert Database(path).get_server("box") == migrated


@pytest.mark.parametrize("existing_version", [0, 5])
def test_concurrent_initialization_and_migration_keep_schema_lock(tmp_path, monkeypatch, existing_version):
    path = tmp_path / "manager.db"
    if existing_version == 5:
        with sqlite3.connect(path) as connection:
            connection.executescript("""
                CREATE TABLE servers(id TEXT PRIMARY KEY, last_test_at TEXT, last_test_status TEXT);
                INSERT INTO servers VALUES('retained', '2026-10-14', 'ok');
                PRAGMA user_version = 5;
            """)
    observations = []
    original = Database.connect
    def traced_connect(database):
        connection = original(database)
        def trace(statement):
            if statement.lstrip().upper().startswith(("PRAGMA USER_VERSION", "CREATE ", "ALTER ")):
                observations.append((statement, connection.in_transaction))
        connection.set_trace_callback(trace)
        return connection
    monkeypatch.setattr(Database, "connect", traced_connect)
    barrier = threading.Barrier(2)
    def initialize():
        barrier.wait(timeout=5)
        Database(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(initialize) for _ in range(2)]
        for job in jobs:
            job.result(timeout=5)
    assert observations and all(locked for _statement, locked in observations)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = [row[1] for row in connection.execute("PRAGMA table_info(servers)")]
        assert all(columns.count(column) == 1 for column in ("facts", "last_success_at", "last_resolved_ip", "host_key", "geo"))
        if existing_version == 5:
            assert connection.execute("SELECT id, last_success_at FROM servers").fetchone() == ("retained", "2026-10-14")
        else:
            assert connection.execute("SELECT COUNT(*) FROM servers").fetchone()[0] == 0


def test_concurrent_partial_host_updates_preserve_independent_fields(database, monkeypatch):
    # Yield after the first transaction's read. A second writer must wait before
    # reading the profile, instead of merging its patch into that stale record.
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    original = database._validate_server_payload
    calls = []
    def pause(payload, **options):
        assert options["connection"].in_transaction
        calls.append(payload["notes"])
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=5)
        return original(payload, **options)
    monkeypatch.setattr(database, "_validate_server_payload", pause)
    def second():
        second_started.set()
        return database.update_server("box", tags=["Lab"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(database.update_server, "box", notes="saved-note")
        assert entered.wait(timeout=5)
        two = pool.submit(second)
        assert second_started.wait(timeout=5)
        release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    result = database.get_server("box")
    assert result["notes"] == "saved-note" and result["tags"] == ["Lab"]
    assert calls == ["saved-note", "saved-note"]


def test_concurrent_note_appends_and_path_patches_retain_both_changes(database):
    barrier = threading.Barrier(2)
    def append(note):
        barrier.wait(timeout=5)
        return database.update_server_notes("box", note, append=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(append, ["first", "second"]))
    assert set(database.get_server("box")["notes"].split("\n\n")) == {"first", "second"}
    path = database.create_server_path("box", label="Work", path="~/work")
    barrier = threading.Barrier(2)
    def patch(changes):
        barrier.wait(timeout=5)
        return database.update_server_path("box", path["id"], **changes)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(patch, [{"label": "Code"}, {"notes": "saved"}]))
    saved = database.get_server_path("box", path["id"])
    assert saved["label"] == "Code" and saved["notes"] == "saved"


def test_concurrent_route_changes_cannot_commit_a_cycle(database):
    database.create_server(alias="other", hostname="other.example.invalid", username="alice")
    barrier = threading.Barrier(2)
    def route(pair):
        barrier.wait(timeout=5)
        try:
            database.update_server(pair[0], proxy_jumps=[pair[1]])
            return "ok"
        except ValidationError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(route, [("box", "other"), ("other", "box")]))
    assert sorted(outcomes) == ["ok", "rejected"]


def test_alias_rename_validates_rewritten_dependents_and_rolls_back(database):
    database.create_server(alias="dependent", hostname="other.example.invalid", username="alice", proxy_jumps=["box"])
    before = database.list_servers()
    with pytest.raises(ValidationError, match="cycle"):
        database.update_server("box", alias="renamed", proxy_jumps=["dependent"], notes="must roll back", tags=["New"])
    assert database.list_servers() == before
    assert database.get_server("dependent")["proxy_jumps"] == ["box"]
    assert database.list_server_contexts() == []


@pytest.mark.parametrize("operation", ["create", "rename"])
def test_external_alias_becoming_managed_cannot_introduce_cycle(database, operation):
    database.update_server("box", proxy_jumps=["external"])
    before = database.list_servers()
    with pytest.raises(ValidationError, match="cycle"):
        if operation == "create":
            database.create_server(alias="external", hostname="other.example.invalid", username="alice", proxy_jumps=["box"])
        else:
            database.create_server(alias="other", hostname="other.example.invalid", username="alice")
            before = database.list_servers()
            database.update_server("other", alias="external", proxy_jumps=["box"])
    assert database.list_servers() == before


def test_proxy_graph_handles_long_chain_and_detects_deep_cycle(database, monkeypatch):
    hosts = [{"id": str(index), "alias": f"node{index}", "proxy_jumps": [f"node{index + 1}"]}
             for index in range(1500)]
    monkeypatch.setattr(database, "list_servers", lambda: hosts)
    database._validate_proxy_graph({"alias": "end", "proxy_jumps": []}, current_id=None)
    with pytest.raises(ValidationError, match="cycle"):
        database._validate_proxy_graph({"alias": "node1500", "proxy_jumps": ["node0"]}, current_id=None)


def test_context_membership_and_profile_patch_preserve_each_other(database):
    host_id = database.get_server("box")["id"]
    barrier = threading.Barrier(2)
    def context():
        barrier.wait(timeout=5)
        database.create_server_context("Lab", server_ids=[host_id])
    def profile():
        barrier.wait(timeout=5)
        database.update_server("box", notes="saved")
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(context), pool.submit(profile)]
        for job in jobs:
            job.result(timeout=5)
    host = database.get_server("box")
    assert host["tags"] == ["Lab"] and host["notes"] == "saved"
