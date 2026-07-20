from __future__ import annotations

import sqlite3

import pytest

from ssh_server_manager.db import ConflictError, Database, DatabaseError, NotFoundError
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


def test_server_notes_can_be_set_appended_and_cleared(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", port=22, username="u")

    assert database.update_server_notes("box", "Primary compute host")["notes"] == "Primary compute host"
    assert database.update_server_notes("box", "Check disk before upgrades", append=True)["notes"] == (
        "Primary compute host\n\nCheck disk before upgrades"
    )
    assert database.update_server_notes("box", "")["notes"] == ""
    with pytest.raises(ValidationError, match="required"):
        database.update_server_notes("box", "", append=True)


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
    assert version == 4


def test_host_skills_are_many_to_many_and_mutations_are_atomic(tmp_path):
    database = Database(tmp_path / "manager.db")
    one = database.create_server(alias="one", hostname="one.example", port=22, username="u")
    two = database.create_server(alias="two", hostname="two.example", port=22, username="u")
    operations = database.create_skill(
        name="operations",
        path=tmp_path / "operations" / "SKILL.md",
        description="Operate production services",
        server_identifiers=[one["id"]],
    )
    research = database.create_skill(
        name="Research_Tools",
        path=tmp_path / "research" / "SKILL.md",
        description="Inspect research jobs",
    )

    assert operations["servers"] == [{"id": one["id"], "alias": "one"}]
    assert [skill["name"] for skill in database.list_skills("one")] == ["operations"]

    with pytest.raises(NotFoundError, match="missing"):
        database.attach_skill("operations", [two["id"], "missing"])
    assert [server["alias"] for server in database.get_skill("operations")["servers"]] == ["one"]

    database.attach_skill(operations["id"], [two["id"]])
    assert [server["alias"] for server in database.get_skill("operations")["servers"]] == [
        "one",
        "two",
    ]
    database.update_server(two["id"], alias="two-renamed")
    assert [server["alias"] for server in database.get_skill("operations")["servers"]] == [
        "one",
        "two-renamed",
    ]
    with pytest.raises(NotFoundError, match="missing"):
        database.set_server_skills("one", [research["id"], "missing"])
    assert [skill["name"] for skill in database.list_skills("one")] == ["operations"]

    database.set_server_skills("one", [research["id"]])
    assert [skill["name"] for skill in database.list_skills("one")] == ["Research_Tools"]
    with pytest.raises(ConflictError, match="assigned"):
        database.delete_skill(research["id"])

    database.delete_server(one["id"])
    assert database.get_skill(research["id"])["servers"] == []
    assert database.delete_skill(research["id"])["name"] == "Research_Tools"


def test_skill_names_use_unicode_casefold_for_identity(tmp_path):
    database = Database(tmp_path / "manager.db")
    first = database.create_skill(
        name="Ågent:Ops",
        path=tmp_path / "first" / "SKILL.md",
        description="Unicode identity",
    )

    assert database.get_skill("åGENT:oPS")["id"] == first["id"]
    with pytest.raises(ConflictError, match="already exists"):
        database.create_skill(
            name="ågent:ops",
            path=tmp_path / "second" / "SKILL.md",
            description="Must conflict",
        )


def test_skill_create_rolls_back_when_a_host_is_unknown(tmp_path):
    database = Database(tmp_path / "manager.db")

    with pytest.raises(NotFoundError, match="missing"):
        database.create_skill(
            name="not-created",
            path=tmp_path / "not-created" / "SKILL.md",
            description="Must be atomic",
            server_identifiers=["missing"],
        )

    assert database.list_skills() == []


def test_multi_host_skill_resolution_groups_applicable_hosts(tmp_path):
    database = Database(tmp_path / "manager.db")
    one = database.create_server(alias="one", hostname="one.example", port=22, username="u")
    two = database.create_server(alias="two", hostname="two.example", port=22, username="u")
    shared = database.create_skill(
        name="shared",
        path=tmp_path / "shared" / "SKILL.md",
        description="Shared host guidance",
        server_identifiers=[one["id"], two["id"]],
    )

    resolved = database.resolve_skills(["two", one["id"], "two"])

    assert [host["alias"] for host in resolved["hosts"]] == ["two", "one"]
    assert resolved["skills"] == [
        {
            "id": shared["id"],
            "name": "shared",
            "path": str((tmp_path / "shared" / "SKILL.md").resolve()),
            "description": "Shared host guidance",
            "status": "registered",
            "applies_to": ["two", "one"],
        }
    ]


def test_schema_two_database_is_upgraded_with_skill_tables(tmp_path):
    path = tmp_path / "manager.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE servers (id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 2")

    Database(path)

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        skill_columns = {row[1] for row in connection.execute("PRAGMA table_info(skills)")}
    assert {"skills", "server_skills"} <= tables
    assert "name_key" in skill_columns
    assert version == 4


def test_schema_three_database_backfills_unicode_skill_identity(tmp_path):
    path = tmp_path / "manager.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE servers (id TEXT PRIMARY KEY, alias TEXT NOT NULL);
            CREATE TABLE skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                path TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE server_skills (
                server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (server_id, skill_id)
            );
            INSERT INTO servers VALUES ('server-1', 'gpu-one');
            INSERT INTO skills VALUES (
                'skill-1', 'Ågent:Ops', '/tmp/agent-ops/SKILL.md',
                'Unicode identity', '2026-01-01', '2026-01-01'
            );
            INSERT INTO server_skills VALUES ('server-1', 'skill-1', '2026-01-01');
            PRAGMA user_version = 3;
            """
        )

    database = Database(path)

    assert database.get_skill("åGENT:oPS")["servers"] == [
        {"id": "server-1", "alias": "gpu-one"}
    ]
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        name_key = connection.execute(
            "SELECT name_key FROM skills WHERE id = 'skill-1'"
        ).fetchone()[0]
    assert version == 4
    assert name_key == "Ågent:Ops".casefold()


def test_schema_three_upgrade_rejects_existing_casefold_ambiguity(tmp_path):
    path = tmp_path / "manager.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                path TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO skills VALUES (
                'one', 'Ågent', '/tmp/one/SKILL.md', 'One', '2026-01-01', '2026-01-01'
            );
            INSERT INTO skills VALUES (
                'two', 'ågent', '/tmp/two/SKILL.md', 'Two', '2026-01-01', '2026-01-01'
            );
            PRAGMA user_version = 3;
            """
        )

    with pytest.raises(DatabaseError, match="conflict case-insensitively"):
        Database(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_fresh_database_has_schema_four_skill_identity(tmp_path):
    path = tmp_path / "manager.db"
    Database(path)

    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(skills)")}
    assert version == 4
    assert "name_key" in columns


def test_private_dir_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="not a real directory"):
        ensure_private_dir(link)
