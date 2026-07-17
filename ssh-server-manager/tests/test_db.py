from __future__ import annotations

import sqlite3

import pytest

from ssh_server_manager.db import ConflictError, Database
from ssh_server_manager.paths import ensure_private_dir
from ssh_server_manager.validation import ValidationError


def test_server_and_reusable_credentials(tmp_path):
    database = Database(tmp_path / "manager.db")
    credential = database.create_credential(label="Lab agent", kind="agent")
    jump = database.create_server(
        alias="jump", hostname="jump.example", port=2222, username="alice", credential_id=credential["id"]
    )
    target = database.create_server(
        alias="target",
        hostname="10.0.0.5",
        port=22,
        username="alice",
        credential_id=credential["id"],
        proxy_jumps=["jump"],
    )
    assert target["credential_label"] == "Lab agent"
    assert target["proxy_jumps"] == ["jump"]
    with pytest.raises(ConflictError):
        database.delete_credential(credential["id"])

    renamed = database.update_server(jump["id"], alias="gateway")
    assert renamed["alias"] == "gateway"
    assert database.get_server(target["id"])["proxy_jumps"] == ["gateway"]


def test_proxy_cycle_and_dependent_delete_are_blocked(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="one", hostname="one.example", port=22, username="u")
    database.create_server(alias="two", hostname="two.example", port=22, username="u", proxy_jumps=["one"])
    with pytest.raises(ValidationError, match="cycle"):
        database.update_server("one", proxy_jumps=["two"])
    with pytest.raises(ConflictError, match="ProxyJump"):
        database.delete_server("one")


def test_alias_and_port_validation(tmp_path):
    database = Database(tmp_path / "manager.db")
    with pytest.raises(ValidationError):
        database.create_server(alias="bad alias", hostname="example.org", port=22, username="u")
    with pytest.raises(ValidationError):
        database.create_server(alias="good", hostname="example.org", port=70000, username="u")


def test_server_context_tags_are_normalized_and_persisted(tmp_path):
    database = Database(tmp_path / "manager.db")
    server = database.create_server(
        alias="box",
        hostname="box.example",
        port=22,
        username="u",
        tags=["Research", "production", "research"],
    )

    assert server["tags"] == ["Research", "production"]
    assert database.update_server("box", tags=["Client A"])["tags"] == ["Client A"]
    with pytest.raises(ValidationError, match="printable"):
        database.update_server("box", tags=["bad\ntag"])
    with pytest.raises(ValidationError, match="at most"):
        database.update_server("box", tags=[f"tag-{index}" for index in range(21)])


def test_server_context_registry_and_bulk_membership_are_atomic(tmp_path):
    database = Database(tmp_path / "manager.db")
    one = database.create_server(
        alias="one", hostname="one.example", port=22, username="u", tags=["Research", "GPU"]
    )
    two = database.create_server(alias="two", hostname="two.example", port=22, username="u")

    database.create_server_context("Client A")
    assert database.list_server_contexts() == [
        {"name": "Client A", "count": 0},
        {"name": "GPU", "count": 1},
        {"name": "Research", "count": 1},
    ]

    renamed = database.update_server_context(
        "research", new_name="Lab work", server_ids=[one["id"], two["id"]]
    )
    assert renamed == {"name": "Lab work", "count": 2}
    assert database.get_server("one")["tags"] == ["Lab work", "GPU"]
    assert database.get_server("two")["tags"] == ["Lab work"]

    database.update_server_context("Lab work", server_ids=[])
    assert next(item for item in database.list_server_contexts() if item["name"] == "Lab work")["count"] == 0

    removed = database.delete_server_context("lab work")
    assert removed == {"name": "lab work", "removed_from": 0}
    assert "Lab work" not in {item["name"] for item in database.list_server_contexts()}


def test_server_context_can_be_created_and_assigned_atomically(tmp_path):
    database = Database(tmp_path / "manager.db")
    one = database.create_server(alias="one", hostname="one.example", port=22, username="u")
    two = database.create_server(
        alias="two", hostname="two.example", port=22, username="u", tags=["Existing"]
    )

    created = database.create_server_context("Project A", server_ids=[one["id"], two["id"]])

    assert created == {"name": "Project A", "count": 2}
    assert database.get_server("one")["tags"] == ["Project A"]
    assert database.get_server("two")["tags"] == ["Existing", "Project A"]

    with pytest.raises(ValidationError, match="at most"):
        full = database.create_server(
            alias="full",
            hostname="full.example",
            port=22,
            username="u",
            tags=[f"tag-{index}" for index in range(20)],
        )
        database.create_server_context("Does not persist", server_ids=[full["id"]])
    assert "Does not persist" not in {item["name"] for item in database.list_server_contexts()}


def test_server_context_mutation_rejects_conflicts_and_rolls_back(tmp_path):
    database = Database(tmp_path / "manager.db")
    server = database.create_server(
        alias="box",
        hostname="box.example",
        port=22,
        username="u",
        tags=[f"tag-{index}" for index in range(20)],
    )
    database.create_server_context("overflow")

    with pytest.raises(ValidationError, match="at most"):
        database.update_server_context("overflow", server_ids=[server["id"]])
    assert database.get_server("box")["tags"] == [f"tag-{index}" for index in range(20)]

    with pytest.raises(ConflictError, match="already exists"):
        database.update_server_context("tag-0", new_name="tag-1")


def test_schema_one_database_is_upgraded_with_server_tags(tmp_path):
    path = tmp_path / "manager.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE servers (id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 1")

    Database(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(servers)")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert "tags" in columns
    assert version == 2


def test_private_dir_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="not a real directory"):
        ensure_private_dir(link)
