"""Export and import of host metadata: portable, deterministic, secret-free.

The tests that matter here are the negative ones — no secret and no
machine-specific identifier may appear in an export, and an import must never
invent a credential — plus determinism, because the whole point is a file a git
history can diff without churn.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from ssh_server_manager import portable
from ssh_server_manager.db import Database
from ssh_server_manager.validation import ValidationError


@pytest.fixture()
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    return Database(tmp_path / "manager.db")


def _seed(database):
    database.create_server(
        alias="web1",
        hostname="web1.example",
        port=2222,
        username="deploy",
        tags=["prod", "web"],
        notes="primary",
    )
    database.create_server(
        alias="db1", hostname="db1.example", username="alice", proxy_jumps=["web1"]
    )
    database.create_server_path("web1", label="app", path="/srv/app", notes="code")
    database.create_tunnel(
        "web1", name="web1-local", kind="local", forward="8080:localhost:80", notes="site"
    )
    return database


def test_export_contains_profiles_paths_and_tunnels(database):
    _seed(database)
    document = portable.export_document(database)

    assert document["format"] == portable.FORMAT
    assert document["version"] == portable.VERSION
    assert [host["alias"] for host in document["hosts"]] == ["db1", "web1"]
    web1 = document["hosts"][1]
    assert web1["hostname"] == "web1.example"
    assert web1["port"] == 2222
    assert web1["tags"] == ["prod", "web"]
    assert web1["proxy_jumps"] == []
    assert web1["paths"] == [{"label": "app", "path": "/srv/app", "notes": "code"}]
    assert web1["tunnels"][0]["forward"] if "forward" in web1["tunnels"][0] else True
    assert web1["tunnels"] == [
        {
            "name": "web1-local",
            "kind": "local",
            "bind_host": "127.0.0.1",
            "bind_port": 8080,
            "remote_host": "localhost",
            "remote_port": 80,
            "notes": "site",
        }
    ]
    assert document["hosts"][0]["proxy_jumps"] == ["web1"]


def test_export_is_deterministic(database):
    _seed(database)
    first = portable.dump_document(portable.export_document(database))
    second = portable.dump_document(portable.export_document(database))
    assert first == second


def test_export_never_carries_a_secret_or_a_local_identifier(database):
    credential = database.create_credential(label="deploy key", kind="agent")
    database.create_server(
        alias="web1",
        hostname="web1.example",
        username="deploy",
        credential_id=credential["id"],
    )
    text = portable.dump_document(portable.export_document(database))

    # The credential travels as a label a human chose, never as a download of the
    # secret or as a row id that means nothing on another machine.
    assert json.loads(text)["hosts"][0]["credential"] == "deploy key"
    assert credential["id"] not in text
    assert "value" not in text and "secret" not in text.lower()


def test_export_leaves_remote_observations_behind(database):
    database.create_server(alias="web1", hostname="web1.example", username="deploy")
    document = portable.export_document(database)
    entry = document["hosts"][0]
    for field in ("facts", "host_key", "last_test_at", "last_resolved_ip", "id"):
        assert field not in entry


def test_written_files_are_private_and_round_trip(database, tmp_path):
    _seed(database)
    path = portable.write_document(portable.export_document(database), tmp_path / "inv.json")
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = portable.load_document(path)
    assert loaded["hosts"] == portable.export_document(database)["hosts"]


# --- validation -------------------------------------------------------------------


def _document(**overrides):
    base = {
        "format": portable.FORMAT,
        "version": portable.VERSION,
        "hosts": [
            {"alias": "web1", "hostname": "web1.example", "username": "deploy", "port": 22}
        ],
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[]", "JSON object"),
        ("{not json", "not valid JSON"),
        ('{"format": "other", "version": 1, "hosts": []}', "not a"),
        ('{"format": "ssh-server-manager.export", "version": 99, "hosts": []}', "version 99"),
        ('{"format": "ssh-server-manager.export", "version": 1}', "hosts"),
        (
            '{"format": "ssh-server-manager.export", "version": 1, "hosts": [], "extra": 1}',
            "unknown top-level",
        ),
        (
            '{"format": "ssh-server-manager.export", "version": 1, "hosts": [{"alias": "a", "hostname": "h", "username": "u", "wat": 1}]}',
            "unknown fields",
        ),
        (
            '{"format": "ssh-server-manager.export", "version": 1, "hosts": [{"alias": "a", "hostname": "", "username": "u"}]}',
            "hostname",
        ),
        (
            '{"format": "ssh-server-manager.export", "version": 1, "hosts": [{"alias": "a", "hostname": "h", "username": "u"}, {"alias": "A", "hostname": "h", "username": "u"}]}',
            "twice",
        ),
        (
            '{"format": "ssh-server-manager.export", "version": 1, "hosts": [{"alias": "a", "hostname": "h", "username": "u", "paths": [{"label": "x", "path": "/p", "notes": "", "z": 1}]}]}',
            "unknown paths",
        ),
    ],
)
def test_malformed_documents_are_refused(text, message):
    with pytest.raises(ValidationError, match=message):
        portable.load_document_text(text)


def test_a_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        portable.load_document(tmp_path / "nope.json")


# --- preview and apply ------------------------------------------------------------


def test_preview_reports_actions_and_writes_nothing(database):
    document = portable.load_document_text(_document())
    plan = portable.preview(document, database)
    assert plan["counts"] == {"create": 1}
    assert database.list_servers() == []


def test_apply_creates_hosts_directories_and_tunnels(database):
    _seed(database)
    document = portable.export_document(database)
    fresh = Database(database.path.parent / "other.db")
    preview = portable.preview(document, fresh)
    assert preview["counts"] == {"create": 2}

    result = portable.apply(document, fresh)
    assert result["ok"] is True
    assert sorted(result["created"]) == ["db1", "web1"]
    hosts = {host["alias"]: host for host in fresh.list_servers()}
    assert hosts["web1"]["port"] == 2222
    assert hosts["web1"]["tags"] == ["prod", "web"]
    assert hosts["db1"]["proxy_jumps"] == ["web1"]
    assert [entry["label"] for entry in fresh.list_server_paths("web1")] == ["app"]
    assert [entry["name"] for entry in fresh.list_tunnels()] == ["web1-local"]


def test_reapplying_the_same_document_changes_nothing(database):
    _seed(database)
    document = portable.export_document(database)
    fresh = Database(database.path.parent / "other.db")
    portable.apply(document, fresh)
    again = portable.apply(document, fresh)
    assert again["created"] == []
    assert again["updated"] == []
    assert sorted(again["unchanged"]) == ["db1", "web1"]


def test_an_existing_host_is_left_alone_without_overwrite(database):
    _seed(database)
    document = portable.load_document_text(
        json.dumps(
            {
                "format": portable.FORMAT,
                "version": 1,
                "hosts": [
                    {
                        "alias": "web1",
                        "hostname": "changed.example",
                        "username": "deploy",
                        "port": 22,
                    }
                ],
            }
        )
    )
    plan = portable.preview(document, database)
    assert plan["hosts"][0]["action"] == "update"
    result = portable.apply(document, database)
    assert result["skipped"] == ["web1"]
    assert database.get_server("web1")["hostname"] == "web1.example"

    applied = portable.apply(document, database, overwrite=True)
    assert applied["updated"] == ["web1"]
    assert database.get_server("web1")["hostname"] == "changed.example"


def test_the_update_plan_names_the_fields_that_differ(database):
    _seed(database)
    document = portable.load_document_text(
        json.dumps(
            {
                "format": portable.FORMAT,
                "version": 1,
                "hosts": [
                    {
                        "alias": "web1",
                        "hostname": "web1.example",
                        "username": "root",
                        "port": 2222,
                        "tags": ["prod", "web"],
                    }
                ],
            }
        )
    )
    plan = portable.preview(document, database)
    assert sorted(plan["hosts"][0]["changes"]) == ["username"]


def test_a_missing_credential_is_a_warning_not_a_secret(database, tmp_path):
    document = portable.load_document_text(
        json.dumps(
            {
                "format": portable.FORMAT,
                "version": 1,
                "hosts": [
                    {
                        "alias": "web1",
                        "hostname": "web1.example",
                        "username": "deploy",
                        "credential": "deploy key",
                    }
                ],
            }
        )
    )
    plan = portable.preview(document, database)
    assert any("does not exist on this machine" in item["message"] for item in plan["warnings"])
    portable.apply(document, database)
    assert database.get_server("web1")["credential_id"] is None


def test_a_present_credential_is_attached_by_label(database):
    credential = database.create_credential(label="Deploy Key", kind="agent")
    document = portable.load_document_text(
        json.dumps(
            {
                "format": portable.FORMAT,
                "version": 1,
                "hosts": [
                    {
                        "alias": "web1",
                        "hostname": "web1.example",
                        "username": "deploy",
                        "credential": "deploy key",
                    }
                ],
            }
        )
    )
    portable.apply(document, database)
    assert database.get_server("web1")["credential_id"] == credential["id"]


def test_tunnels_and_directories_are_merged_not_replaced(database):
    _seed(database)
    database.create_server_path("web1", label="logs", path="/var/log")
    document = portable.load_document_text(
        json.dumps(
            {
                "format": portable.FORMAT,
                "version": 1,
                "hosts": [
                    {
                        "alias": "web1",
                        "hostname": "web1.example",
                        "username": "deploy",
                        "port": 2222,
                        "tags": ["prod", "web"],
                        "notes": "primary",
                        "paths": [{"label": "app", "path": "/srv/app2", "notes": "moved"}],
                    }
                ],
            }
        )
    )
    portable.apply(document, database, overwrite=True)
    paths = {entry["label"]: entry for entry in database.list_server_paths("web1")}
    # The file updated the directory it knows about and kept the local-only one.
    assert paths["app"]["path"] == "/srv/app2"
    assert paths["app"]["notes"] == "moved"
    assert "logs" in paths


def test_a_tunnel_name_clash_on_another_host_is_reported(database):
    database.create_server(alias="web1", hostname="web1.example", username="deploy")
    database.create_server(alias="db1", hostname="db1.example", username="alice")
    database.create_tunnel("db1", name="shared", kind="local", forward="1:localhost:1")
    document = portable.load_document_text(
        json.dumps(
            {
                "format": portable.FORMAT,
                "version": 1,
                "hosts": [
                    {
                        "alias": "web1",
                        "hostname": "web1.example",
                        "username": "deploy",
                        "tunnels": [
                            {
                                "name": "shared",
                                "kind": "local",
                                "bind_host": "127.0.0.1",
                                "bind_port": 2,
                                "remote_host": "localhost",
                                "remote_port": 2,
                                "notes": "",
                            }
                        ],
                    }
                ],
            }
        )
    )
    plan = portable.preview(document, database)
    assert any("already used by host db1" in item["message"] for item in plan["warnings"])
    portable.apply(document, database, overwrite=True)
    assert database.get_tunnel("shared")["server"] == "db1"


def test_apply_collects_errors_instead_of_stopping(database):
    document = portable.load_document_text(
        json.dumps(
            {
                "format": portable.FORMAT,
                "version": 1,
                "hosts": [
                    {"alias": "bad", "hostname": "bad.example", "username": "u", "port": 70000},
                    {"alias": "good", "hostname": "good.example", "username": "u"},
                ],
            }
        )
    )
    result = portable.apply(document, database)
    assert result["ok"] is False
    assert [item["alias"] for item in result["errors"]] == ["bad"]
    assert [host["alias"] for host in database.list_servers()] == ["good"]
