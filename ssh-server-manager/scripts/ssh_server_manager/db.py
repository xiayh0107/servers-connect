from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .host_metadata import address_scope, observation_time, sanitize_facts, sanitize_host_key, validate_geo, validate_resolved_ip
from .paths import database_path, ensure_private_dir
from .validation import (
    DEFAULT_TUNNEL_BIND_HOST,
    ValidationError,
    format_forward_spec,
    parse_forward_spec,
    tunnel_name_key,
    validate_alias,
    validate_bind_host,
    validate_hostname,
    validate_key_path,
    validate_kind,
    validate_label,
    validate_port,
    validate_proxy_jumps,
    validate_tunnel_kind,
    validate_tunnel_name,
    validate_tunnel_note,
    skill_name_key,
    validate_skill_description,
    validate_skill_path,
    validate_skill_name,
    validate_server_tags,
    validate_path_label,
    validate_path_note,
    validate_saved_remote_path,
    path_label_key,
    validate_server_note,
    validate_username,
)


SCHEMA_VERSION = 8
SERVER_CONTEXTS_SETTING = "server_contexts"
SSH_CONFIG_SOURCES_SETTING = "ssh_config_import_sources"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class DatabaseError(RuntimeError):
    pass


class NotFoundError(DatabaseError):
    pass


class ConflictError(DatabaseError):
    pass


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else database_path()
        ensure_private_dir(self.path.parent)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        # SQLite context exit commits/rolls back; callers must also close the handle.
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise self._write_error(exc) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _write_error(self, error: sqlite3.OperationalError) -> Exception:
        """Name why a write failed instead of leaking sqlite3 internals.

        A read-only database is almost always a privacy or permission boundary:
        the app may read the file it is asked to write. Say where it lives and
        what to do, so callers (HTTP included) can report it as expected.
        """
        message = str(error)
        if "readonly" not in message and "unable to open" not in message:
            return error
        return DatabaseError(
            f"the host database at {self.path} cannot be written by this app; "
            f"run the command from an app with full disk access and try again: {message}"
        )

    def _initialize(self) -> None:
        is_new = not self.path.exists()
        # Serialize the version read, initial DDL, and every migration together.
        # executescript() commits a pending transaction, so static schema SQL
        # must be executed statement by statement to retain this write lock.
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < 0 or version > SCHEMA_VERSION:
                raise DatabaseError(f"database schema {version} is newer than supported {SCHEMA_VERSION}")
            if version == SCHEMA_VERSION:
                return
            if version == 0:
                self._execute_schema(connection,
                    """
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    kind TEXT NOT NULL CHECK(kind IN ('password', 'key', 'agent')),
                    key_path TEXT,
                    has_secret INTEGER NOT NULL DEFAULT 0 CHECK(has_secret IN (0, 1)),
                    has_passphrase INTEGER NOT NULL DEFAULT 0 CHECK(has_passphrase IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK((kind = 'key' AND key_path IS NOT NULL) OR (kind != 'key' AND key_path IS NULL))
                );

                CREATE TABLE IF NOT EXISTS servers (
                    id TEXT PRIMARY KEY,
                    alias TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    hostname TEXT NOT NULL,
                    port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535),
                    username TEXT NOT NULL,
                    credential_id TEXT REFERENCES credentials(id) ON DELETE RESTRICT,
                    proxy_jumps TEXT NOT NULL DEFAULT '[]',
                    tags TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'managed',
                    last_test_at TEXT,
                    last_test_status TEXT,
                    last_test_latency_ms INTEGER,
                    last_test_error_code TEXT,
                    last_test_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS webauthn_credentials (
                    credential_id TEXT PRIMARY KEY,
                    public_key TEXT NOT NULL,
                    sign_count INTEGER NOT NULL DEFAULT 0,
                    transports TEXT NOT NULL DEFAULT '[]',
                    device_type TEXT,
                    backed_up INTEGER NOT NULL DEFAULT 0 CHECK(backed_up IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    name_key TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS server_skills (
                    server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                    skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE RESTRICT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (server_id, skill_id)
                );

                CREATE TABLE IF NOT EXISTS server_paths (
                    id TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    label_key TEXT NOT NULL,
                    path TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_servers_credential ON servers(credential_id);
                CREATE INDEX IF NOT EXISTS idx_server_skills_skill ON server_skills(skill_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_skills_name_key ON skills(name_key);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_server_paths_label ON server_paths(server_id, label_key);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_server_paths_path ON server_paths(server_id, path);
                PRAGMA user_version = 5;
                """
                )
                version = 5
            if version == 1:
                connection.execute("ALTER TABLE servers ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
                connection.execute("PRAGMA user_version = 2")
                version = 2
            if version == 2:
                self._execute_schema(connection,
                    """
                    CREATE TABLE IF NOT EXISTS skills (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                        name_key TEXT NOT NULL,
                        path TEXT NOT NULL UNIQUE,
                        description TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS server_skills (
                        server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                        skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE RESTRICT,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (server_id, skill_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_server_skills_skill ON server_skills(skill_id);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_skills_name_key ON skills(name_key);
                    PRAGMA user_version = 4;
                    """
                )
                version = 4
            if version == 3:
                rows = connection.execute("SELECT id, name FROM skills").fetchall()
                names_by_key: dict[str, str] = {}
                keys_by_id: dict[str, str] = {}
                for row in rows:
                    try:
                        key = skill_name_key(row["name"])
                    except ValidationError as exc:
                        raise DatabaseError(
                            f"saved skill has an invalid name: {row['name']!r}"
                        ) from exc
                    if key in names_by_key:
                        raise DatabaseError(
                            "saved skills conflict case-insensitively: "
                            f"{names_by_key[key]!r} and {row['name']!r}"
                        )
                    names_by_key[key] = row["name"]
                    keys_by_id[row["id"]] = key
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(skills)")
                }
                if "name_key" not in columns:
                    connection.execute(
                        "ALTER TABLE skills ADD COLUMN name_key TEXT NOT NULL DEFAULT ''"
                    )
                connection.executemany(
                    "UPDATE skills SET name_key = ? WHERE id = ?",
                    [(key, identifier) for identifier, key in keys_by_id.items()],
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_skills_name_key ON skills(name_key)"
                )
                connection.execute("PRAGMA user_version = 4")
                version = 4
            if version == 4:
                self._execute_schema(connection,
                    """
                    CREATE TABLE IF NOT EXISTS server_paths (
                        id TEXT PRIMARY KEY,
                        server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                        label TEXT NOT NULL,
                        label_key TEXT NOT NULL,
                        path TEXT NOT NULL,
                        notes TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_used_at TEXT
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_server_paths_label ON server_paths(server_id, label_key);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_server_paths_path ON server_paths(server_id, path);
                    PRAGMA user_version = 5;
                    """
                )
                version = 5
            if version == 5:
                for column in ("facts", "last_success_at", "last_resolved_ip", "host_key", "geo"):
                    connection.execute(f"ALTER TABLE servers ADD COLUMN {column} TEXT")
                connection.execute("PRAGMA user_version = 6")
                # Preserve a known successful latest check while upgrading.
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(servers)")}
                if {"last_test_status", "last_test_at"} <= columns:
                    connection.execute("UPDATE servers SET last_success_at = last_test_at WHERE last_test_status = 'ok'")
                version = 6
            if version == 6:
                connection.execute("""CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    kind TEXT NOT NULL CHECK(kind IN ('password', 'key', 'agent')),
                    key_path TEXT, has_secret INTEGER NOT NULL DEFAULT 0,
                    has_passphrase INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                connection.execute("ALTER TABLE credentials ADD COLUMN key_metadata TEXT")
                connection.execute("ALTER TABLE credentials ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
                self._execute_schema(connection, """
                    CREATE TABLE credential_tests (
                        id TEXT PRIMARY KEY, credential_id TEXT NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
                        host_id TEXT NOT NULL, checked_at TEXT NOT NULL, ok INTEGER NOT NULL, result TEXT NOT NULL
                    );
                    CREATE INDEX idx_credential_tests_latest ON credential_tests(credential_id, checked_at DESC);
                    CREATE TABLE credential_deletions (
                        operation_id TEXT PRIMARY KEY, credential_id TEXT NOT NULL,
                        expected_updated_at TEXT NOT NULL, expected_host_ids TEXT NOT NULL,
                        credential TEXT NOT NULL, detached_hosts TEXT NOT NULL,
                        status TEXT NOT NULL, remaining_slots TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    );
                    PRAGMA user_version = 7;
                """)
                version = 7
            if version == 7:
                # Port-forwarding profiles store the parsed halves of an -L/-R/-D
                # spelling rather than the raw string, so a stored profile cannot
                # carry shell syntax into the command that implements it. The label
                # is unique database-wide: "serverctl tunnel open db1-local" names
                # one tunnel without also naming a host.
                self._execute_schema(connection, """
                    CREATE TABLE IF NOT EXISTS server_tunnels (
                        id TEXT PRIMARY KEY,
                        server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                        label TEXT NOT NULL,
                        label_key TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('local', 'remote', 'dynamic')),
                        bind_host TEXT NOT NULL,
                        bind_port INTEGER NOT NULL CHECK(bind_port BETWEEN 0 AND 65535),
                        remote_host TEXT,
                        remote_port INTEGER CHECK(remote_port BETWEEN 1 AND 65535),
                        notes TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK((kind = 'dynamic' AND remote_host IS NULL AND remote_port IS NULL)
                              OR (kind != 'dynamic' AND remote_host IS NOT NULL AND remote_port IS NOT NULL))
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_server_tunnels_label ON server_tunnels(label_key);
                    CREATE INDEX IF NOT EXISTS idx_server_tunnels_server ON server_tunnels(server_id);
                    PRAGMA user_version = 8;
                """)
                version = 8
        if is_new and os.name != "nt":
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    @staticmethod
    def _execute_schema(connection: sqlite3.Connection, schema: str) -> None:
        # These static DDL blocks contain neither triggers nor semicolon literals.
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)

    @staticmethod
    def _credential_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "label": row["label"],
            "kind": row["kind"],
            "key_path": row["key_path"],
            "has_secret": bool(row["has_secret"]),
            "has_passphrase": bool(row["has_passphrase"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "key_metadata": json.loads(row["key_metadata"]) if row["key_metadata"] else None,
            "revision": row["revision"],
        }

    @staticmethod
    def _server_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "alias": row["alias"],
            "hostname": row["hostname"],
            "port": row["port"],
            "username": row["username"],
            "credential_id": row["credential_id"],
            "credential_label": row["credential_label"] if "credential_label" in row.keys() else None,
            "credential_kind": row["credential_kind"] if "credential_kind" in row.keys() else None,
            "proxy_jumps": json.loads(row["proxy_jumps"] or "[]"),
            "tags": json.loads(row["tags"] or "[]"),
            "notes": row["notes"],
            "source": row["source"],
            "last_test_at": row["last_test_at"],
            "last_test_status": row["last_test_status"],
            "last_test_latency_ms": row["last_test_latency_ms"],
            "last_test_error_code": row["last_test_error_code"],
            "last_test_message": row["last_test_message"],
            "facts": json.loads(row["facts"]) if row["facts"] else None,
            "last_success_at": row["last_success_at"],
            "last_resolved_ip": row["last_resolved_ip"],
            "host_key": json.loads(row["host_key"]) if row["host_key"] else None,
            "geo": json.loads(row["geo"]) if row["geo"] else None,
            "address_scope": address_scope(row["hostname"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_credentials(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection, connection:
            rows = connection.execute("SELECT * FROM credentials ORDER BY label COLLATE NOCASE").fetchall()
        return [self._with_credential_test(self._credential_dict(row)) for row in rows]

    def _with_credential_test(self, credential: dict[str, Any]) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT result FROM credential_tests WHERE credential_id = ? ORDER BY rowid DESC LIMIT 1", (credential["id"],)).fetchone()
        result = json.loads(row["result"]) if row else None
        return {**credential, "last_test": result if result and result.get("credential_revision") == credential["revision"] else None}

    def get_credential(self, identifier: str) -> dict[str, Any]:
        with closing(self.connect()) as connection, connection:
            credential = self._get_credential(connection, identifier)
        return self._with_credential_test(credential)

    def _get_credential(self, connection: sqlite3.Connection, identifier: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM credentials WHERE id = ? OR label = ? COLLATE NOCASE", (identifier, identifier)
        ).fetchone()
        if not row:
            raise NotFoundError(f"credential not found: {identifier}")
        return self._credential_dict(row)

    def create_credential(
        self,
        *,
        label: str,
        kind: str,
        key_path: str | None = None,
        has_secret: bool = False,
        has_passphrase: bool = False,
        credential_id: str | None = None,
    ) -> dict[str, Any]:
        label = validate_label(label)
        kind = validate_kind(kind)
        if kind == "key":
            if not key_path:
                raise ValidationError("key credentials require --key-path")
            key_path = validate_key_path(key_path)
        else:
            key_path = None
            has_passphrase = False
        if kind != "password":
            has_secret = False
        now = utc_now()
        identifier = credential_id or str(uuid.uuid4())
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO credentials
                    (id, label, kind, key_path, has_secret, has_passphrase, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (identifier, label, kind, key_path, int(has_secret), int(has_passphrase), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"credential label already exists: {label}") from exc
        return self.get_credential(identifier)

    def update_credential(self, identifier: str, **changes: Any) -> dict[str, Any]:
        before_commit = changes.pop("_before_commit", None)
        try:
            with self.transaction() as connection:
                current = self._get_credential(connection, identifier)
                label = validate_label(changes.get("label", current["label"]))
                key_path = changes.get("key_path", current["key_path"])
                if current["kind"] == "key" and "key_path" in changes:
                    key_path = validate_key_path(key_path)
                has_secret = bool(changes.get("has_secret", current["has_secret"]))
                has_passphrase = bool(changes.get("has_passphrase", current["has_passphrase"]))
                connection.execute(
                    """
                    UPDATE credentials SET label = ?, key_path = ?, has_secret = ?, has_passphrase = ?, updated_at = ?,
                        key_metadata = ?, revision = revision + 1
                    WHERE id = ?
                    """,
                    (label, key_path, int(has_secret), int(has_passphrase), utc_now(),
                     json.dumps(changes.get("key_metadata", current["key_metadata"])) if changes.get("key_metadata", current["key_metadata"]) is not None else None,
                     current["id"]),
                )
                if before_commit is not None:
                    before_commit()
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"credential label already exists: {label}") from exc
        return self.get_credential(current["id"])

    def delete_credential(self, identifier: str) -> dict[str, Any]:
        try:
            with self.transaction() as connection:
                current = self._get_credential(connection, identifier)
                connection.execute("DELETE FROM credentials WHERE id = ?", (current["id"],))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("credential is still assigned to one or more servers") from exc
        return current

    def credential_deletion_preview(self, identifier: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            credential = self._get_credential(connection, identifier)
            hosts = connection.execute("SELECT id, alias FROM servers WHERE credential_id = ? ORDER BY alias COLLATE NOCASE", (credential["id"],)).fetchall()
        return {"credential": credential, "expected_updated_at": credential["updated_at"],
            "hosts": [dict(row) for row in hosts], "expected_host_ids": sorted(row["id"] for row in hosts)}

    @staticmethod
    def _deletion_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {"operation_id": row["operation_id"], "credential_id": row["credential_id"],
            "credential": json.loads(row["credential"]), "detached_hosts": json.loads(row["detached_hosts"]),
            "committed": True, "cleanup_pending": row["status"] != "complete", "status": row["status"],
            "remaining_slots": json.loads(row["remaining_slots"]), "attempts": row["attempts"],
            "error_code": row["error_code"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def get_credential_deletion(self, operation_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM credential_deletions WHERE operation_id = ?", (operation_id,)).fetchone()
        if row is None:
            raise NotFoundError("credential deletion operation was not found")
        return self._deletion_dict(row)

    def list_credential_deletions(self, *, pending: bool = False) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT * FROM credential_deletions " + ("WHERE status != 'complete' " if pending else "") + "ORDER BY created_at DESC LIMIT 250").fetchall()
        return [self._deletion_dict(row) for row in rows]

    def commit_credential_deletion(self, identifier: str, *, operation_id: str, expected_updated_at: str,
                                   expected_host_ids: list[str], detach: bool) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute("SELECT * FROM credential_deletions WHERE operation_id = ?", (operation_id,)).fetchone()
            if existing:
                if (existing["credential_id"] != identifier or existing["expected_updated_at"] != expected_updated_at
                        or json.loads(existing["expected_host_ids"]) != sorted(set(expected_host_ids))):
                    raise ConflictError("deletion operation id was already used for a different request")
                return self._deletion_dict(existing)
            current = self._get_credential(connection, identifier)
            hosts = connection.execute("SELECT id, alias FROM servers WHERE credential_id = ? ORDER BY alias COLLATE NOCASE", (current["id"],)).fetchall()
            host_ids = sorted(row["id"] for row in hosts)
            if current["updated_at"] != expected_updated_at or host_ids != sorted(set(expected_host_ids)):
                raise ConflictError("credential or its host bindings changed; review deletion again")
            if hosts and not detach:
                raise ConflictError("credential is still assigned to one or more servers")
            if detach:
                connection.execute("""UPDATE servers SET credential_id = NULL, updated_at = ?, last_test_at = NULL,
                    last_test_status = NULL, last_test_latency_ms = NULL, last_test_error_code = NULL,
                    last_test_message = NULL, last_success_at = NULL WHERE credential_id = ?""", (now, current["id"]))
            connection.execute("""INSERT INTO credential_deletions
                (operation_id, credential_id, expected_updated_at, expected_host_ids, credential, detached_hosts,
                 status, remaining_slots, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (operation_id, current["id"], expected_updated_at, json.dumps(host_ids), json.dumps(current),
                 json.dumps([dict(row) for row in hosts]), json.dumps(["password", "passphrase", "config"]), now, now))
            connection.execute("DELETE FROM credentials WHERE id = ?", (current["id"],))
        return self.get_credential_deletion(operation_id)

    def update_credential_deletion(self, operation_id: str, remaining_slots: list[str], *, error_code: str | None) -> dict[str, Any]:
        with self.transaction() as connection:
            connection.execute("""UPDATE credential_deletions SET remaining_slots = ?, status = ?,
                attempts = attempts + 1, error_code = ?, updated_at = ? WHERE operation_id = ?""",
                (json.dumps(remaining_slots), "pending" if remaining_slots else "complete", error_code, utc_now(), operation_id))
        return self.get_credential_deletion(operation_id)

    def record_credential_test(self, credential_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as connection:
            credential = self._get_credential(connection, credential_id)
            result = {**result, "credential_revision": credential["revision"]}
            connection.execute("INSERT INTO credential_tests (id, credential_id, host_id, checked_at, ok, result) VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), credential_id, result["host_id"], result["checked_at"], int(result["ok"]), json.dumps(result)))
            connection.execute("DELETE FROM credential_tests WHERE credential_id = ? AND id NOT IN (SELECT id FROM credential_tests WHERE credential_id = ? ORDER BY rowid DESC LIMIT 50)", (credential_id, credential_id))
        return result

    def list_credential_tests(self, identifier: str) -> list[dict[str, Any]]:
        credential = self.get_credential(identifier)
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT result FROM credential_tests WHERE credential_id = ? ORDER BY rowid DESC LIMIT 50", (credential["id"],)).fetchall()
        return [json.loads(row["result"]) for row in rows]

    def _server_query(self) -> str:
        return """
            SELECT s.*, c.label AS credential_label, c.kind AS credential_kind
            FROM servers s LEFT JOIN credentials c ON c.id = s.credential_id
        """

    @staticmethod
    def _context_name(value: str) -> str:
        return validate_server_tags([value])[0]

    def _read_context_registry(self, connection: sqlite3.Connection) -> list[str]:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (SERVER_CONTEXTS_SETTING,)
        ).fetchone()
        if not row:
            return []
        try:
            values = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise DatabaseError("saved server contexts are invalid") from exc
        if not isinstance(values, list):
            raise DatabaseError("saved server contexts are invalid")
        contexts: list[str] = []
        seen: set[str] = set()
        for value in values:
            name = self._context_name(str(value))
            if name.casefold() not in seen:
                contexts.append(name)
                seen.add(name.casefold())
        return contexts

    def _write_context_registry(self, connection: sqlite3.Connection, contexts: list[str]) -> None:
        connection.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (SERVER_CONTEXTS_SETTING, json.dumps(contexts, ensure_ascii=False), utc_now()),
        )

    def _register_contexts(self, connection: sqlite3.Connection, names: list[str]) -> None:
        contexts = self._read_context_registry(connection)
        seen = {name.casefold() for name in contexts}
        for name in names:
            if name.casefold() not in seen:
                contexts.append(name)
                seen.add(name.casefold())
        self._write_context_registry(connection, contexts)

    def list_server_contexts(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection, connection:
            contexts = self._read_context_registry(connection)
            rows = connection.execute("SELECT tags FROM servers").fetchall()
        names = {name.casefold(): name for name in contexts}
        counts: dict[str, int] = {key: 0 for key in names}
        for row in rows:
            server_keys: set[str] = set()
            for tag in json.loads(row["tags"] or "[]"):
                name = self._context_name(str(tag))
                key = name.casefold()
                names.setdefault(key, name)
                server_keys.add(key)
            for key in server_keys:
                counts[key] = counts.get(key, 0) + 1
        return [
            {"name": names[key], "count": counts.get(key, 0)}
            for key in sorted(names, key=lambda item: names[item].casefold())
        ]

    def create_server_context(
        self, name: str, *, server_ids: list[str] | None = None
    ) -> dict[str, Any]:
        name = self._context_name(name)
        with self.transaction() as connection:
            contexts = self._read_context_registry(connection)
            if name.casefold() in {item.casefold() for item in contexts}:
                raise ConflictError(f"server context already exists: {name}")
            rows = connection.execute("SELECT id, tags FROM servers").fetchall()
            if any(name.casefold() in {str(tag).casefold() for tag in json.loads(row["tags"] or "[]")} for row in rows):
                raise ConflictError(f"server context already exists: {name}")

            selected = {str(identifier) for identifier in (server_ids or [])}
            known = {row["id"] for row in rows}
            missing = selected - known
            if missing:
                raise NotFoundError(f"server not found: {sorted(missing)[0]}")

            now = utc_now()
            for row in rows:
                if row["id"] not in selected:
                    continue
                tags = [str(tag) for tag in json.loads(row["tags"] or "[]")]
                normalized = validate_server_tags([*tags, name])
                connection.execute(
                    "UPDATE servers SET tags = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(normalized, ensure_ascii=False), now, row["id"]),
                )
            contexts.append(name)
            self._write_context_registry(connection, contexts)
        return {"name": name, "count": len(selected)}

    def update_server_context(
        self,
        name: str,
        *,
        new_name: str | None = None,
        server_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        requested = self._context_name(name)
        renamed = self._context_name(new_name) if new_name is not None else None
        with self.transaction() as connection:
            rows = connection.execute("SELECT id, tags FROM servers").fetchall()
            contexts = self._read_context_registry(connection)
            available = {item.casefold(): item for item in contexts}
            for row in rows:
                for tag in json.loads(row["tags"] or "[]"):
                    available.setdefault(str(tag).casefold(), str(tag))
            old_key = requested.casefold()
            if old_key not in available:
                raise NotFoundError(f"server context not found: {requested}")
            current_name = available[old_key]
            target_name = renamed or current_name
            target_key = target_name.casefold()
            if target_key != old_key and target_key in available:
                raise ConflictError(f"server context already exists: {target_name}")

            selected: set[str] | None = None
            if server_ids is not None:
                selected = {str(identifier) for identifier in server_ids}
                known = {row["id"] for row in rows}
                missing = selected - known
                if missing:
                    raise NotFoundError(f"server not found: {sorted(missing)[0]}")

            assigned = 0
            now = utc_now()
            for row in rows:
                tags = [str(tag) for tag in json.loads(row["tags"] or "[]")]
                rewritten = [target_name if tag.casefold() == old_key else tag for tag in tags]
                if selected is not None:
                    has_target = any(tag.casefold() == target_key for tag in rewritten)
                    if row["id"] not in selected:
                        rewritten = [tag for tag in rewritten if tag.casefold() != target_key]
                    elif not has_target:
                        rewritten.append(target_name)
                normalized = validate_server_tags(rewritten)
                if any(tag.casefold() == target_key for tag in normalized):
                    assigned += 1
                if normalized != tags:
                    connection.execute(
                        "UPDATE servers SET tags = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(normalized, ensure_ascii=False), now, row["id"]),
                    )

            registry: list[str] = []
            seen: set[str] = set()
            for item in [*contexts, *available.values()]:
                value = target_name if item.casefold() == old_key else item
                if value.casefold() not in seen:
                    registry.append(value)
                    seen.add(value.casefold())
            self._write_context_registry(connection, registry)
        return {"name": target_name, "count": assigned}

    def delete_server_context(self, name: str) -> dict[str, Any]:
        requested = self._context_name(name)
        key = requested.casefold()
        removed_from = 0
        with self.transaction() as connection:
            rows = connection.execute("SELECT id, tags FROM servers").fetchall()
            contexts = self._read_context_registry(connection)
            exists = key in {item.casefold() for item in contexts}
            now = utc_now()
            for row in rows:
                tags = [str(tag) for tag in json.loads(row["tags"] or "[]")]
                rewritten = [tag for tag in tags if tag.casefold() != key]
                if rewritten != tags:
                    exists = True
                    removed_from += 1
                    connection.execute(
                        "UPDATE servers SET tags = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(rewritten, ensure_ascii=False), now, row["id"]),
                    )
            if not exists:
                raise NotFoundError(f"server context not found: {requested}")
            self._write_context_registry(
                connection, [item for item in contexts if item.casefold() != key]
            )
        return {"name": requested, "removed_from": removed_from}

    @staticmethod
    def _find_server_row(connection: sqlite3.Connection, identifier: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT id, alias FROM servers WHERE id = ? OR alias = ? COLLATE NOCASE",
            (identifier, identifier),
        ).fetchone()
        if not row:
            raise NotFoundError(f"server not found: {identifier}")
        return row

    @classmethod
    def _resolve_server_rows(
        cls, connection: sqlite3.Connection, identifiers: list[str]
    ) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        seen: set[str] = set()
        for identifier in identifiers:
            row = cls._find_server_row(connection, str(identifier))
            if row["id"] not in seen:
                rows.append(row)
                seen.add(row["id"])
        return rows

    @staticmethod
    def _find_skill_row(connection: sqlite3.Connection, identifier: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM skills WHERE id = ?", (identifier,)).fetchone()
        if not row:
            try:
                key = skill_name_key(identifier)
            except ValidationError:
                key = ""
            row = connection.execute(
                "SELECT * FROM skills WHERE name_key = ?", (key,)
            ).fetchone()
        if not row:
            raise NotFoundError(f"skill not found: {identifier}")
        return row

    @classmethod
    def _resolve_skill_rows(
        cls, connection: sqlite3.Connection, identifiers: list[str]
    ) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        seen: set[str] = set()
        for identifier in identifiers:
            row = cls._find_skill_row(connection, str(identifier))
            if row["id"] not in seen:
                rows.append(row)
                seen.add(row["id"])
        return rows

    @staticmethod
    def _skill_servers(connection: sqlite3.Connection, skill_id: str) -> list[dict[str, str]]:
        rows = connection.execute(
            """
            SELECT s.id, s.alias
            FROM server_skills ss JOIN servers s ON s.id = ss.server_id
            WHERE ss.skill_id = ?
            ORDER BY s.alias COLLATE NOCASE
            """,
            (skill_id,),
        ).fetchall()
        return [{"id": row["id"], "alias": row["alias"]} for row in rows]

    @classmethod
    def _skill_dict(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "path": row["path"],
            "description": row["description"],
            "servers": cls._skill_servers(connection, row["id"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_skills(self, server_identifier: str | None = None) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection, connection:
            parameters: tuple[str, ...] = ()
            query = "SELECT sk.* FROM skills sk"
            if server_identifier is not None:
                server = self._find_server_row(connection, str(server_identifier))
                query += " JOIN server_skills ss ON ss.skill_id = sk.id WHERE ss.server_id = ?"
                parameters = (server["id"],)
            query += " ORDER BY sk.name COLLATE NOCASE"
            rows = connection.execute(query, parameters).fetchall()
            return [self._skill_dict(connection, row) for row in rows]

    def get_skill(self, identifier: str) -> dict[str, Any]:
        with closing(self.connect()) as connection, connection:
            row = self._find_skill_row(connection, identifier)
            return self._skill_dict(connection, row)

    def create_skill(
        self,
        *,
        name: str,
        path: str | Path,
        description: str,
        skill_id: str | None = None,
        server_identifiers: list[str] | None = None,
    ) -> dict[str, Any]:
        name = validate_skill_name(name)
        name_key = skill_name_key(name)
        path = validate_skill_path(path)
        description = validate_skill_description(description)
        identifier = skill_id or str(uuid.uuid4())
        now = utc_now()
        try:
            with self.transaction() as connection:
                servers = self._resolve_server_rows(connection, server_identifiers or [])
                connection.execute(
                    """
                    INSERT INTO skills
                    (id, name, name_key, path, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (identifier, name, name_key, path, description, now, now),
                )
                connection.executemany(
                    "INSERT INTO server_skills (server_id, skill_id, created_at) VALUES (?, ?, ?)",
                    [(server["id"], identifier, now) for server in servers],
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"skill name or path already exists: {name}") from exc
        return self.get_skill(identifier)

    def update_skill(self, identifier: str, **changes: Any) -> dict[str, Any]:
        try:
            with self.transaction() as connection:
                current = self._skill_dict(connection, self._find_skill_row(connection, identifier))
                name = validate_skill_name(changes.get("name", current["name"]))
                name_key = skill_name_key(name)
                path = validate_skill_path(changes.get("path", current["path"]))
                description = validate_skill_description(changes.get("description", current["description"]))
                connection.execute(
                    """
                    UPDATE skills
                    SET name = ?, name_key = ?, path = ?, description = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (name, name_key, path, description, utc_now(), current["id"]),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"skill name or path already exists: {name}") from exc
        return self.get_skill(current["id"])

    def delete_skill(self, identifier: str) -> dict[str, Any]:
        try:
            with self.transaction() as connection:
                current = self._skill_dict(connection, self._find_skill_row(connection, identifier))
                connection.execute("DELETE FROM skills WHERE id = ?", (current["id"],))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("skill is still assigned to one or more servers") from exc
        return current

    def attach_skill(self, identifier: str, server_identifiers: list[str]) -> dict[str, Any]:
        with self.transaction() as connection:
            skill = self._find_skill_row(connection, identifier)
            servers = self._resolve_server_rows(connection, server_identifiers)
            now = utc_now()
            connection.executemany(
                "INSERT OR IGNORE INTO server_skills (server_id, skill_id, created_at) VALUES (?, ?, ?)",
                [(server["id"], skill["id"], now) for server in servers],
            )
        return self.get_skill(skill["id"])

    def detach_skill(self, identifier: str, server_identifiers: list[str]) -> dict[str, Any]:
        with self.transaction() as connection:
            skill = self._find_skill_row(connection, identifier)
            servers = self._resolve_server_rows(connection, server_identifiers)
            connection.executemany(
                "DELETE FROM server_skills WHERE server_id = ? AND skill_id = ?",
                [(server["id"], skill["id"]) for server in servers],
            )
        return self.get_skill(skill["id"])

    def set_skill_servers(self, identifier: str, server_identifiers: list[str]) -> dict[str, Any]:
        """Replace one skill's host bindings after validating every host."""
        with self.transaction() as connection:
            skill = self._find_skill_row(connection, identifier)
            servers = self._resolve_server_rows(connection, server_identifiers)
            connection.execute("DELETE FROM server_skills WHERE skill_id = ?", (skill["id"],))
            now = utc_now()
            connection.executemany(
                "INSERT INTO server_skills (server_id, skill_id, created_at) VALUES (?, ?, ?)",
                [(server["id"], skill["id"], now) for server in servers],
            )
        return self.get_skill(skill["id"])

    def set_server_skills(
        self, server_identifier: str, skill_identifiers: list[str]
    ) -> list[dict[str, Any]]:
        """Replace one host's skill bindings after validating every skill."""
        with self.transaction() as connection:
            server = self._find_server_row(connection, server_identifier)
            skills = self._resolve_skill_rows(connection, skill_identifiers)
            connection.execute("DELETE FROM server_skills WHERE server_id = ?", (server["id"],))
            now = utc_now()
            connection.executemany(
                "INSERT INTO server_skills (server_id, skill_id, created_at) VALUES (?, ?, ?)",
                [(server["id"], skill["id"], now) for skill in skills],
            )
        return self.list_skills(server["id"])

    def resolve_skills(self, server_identifiers: list[str]) -> dict[str, Any]:
        """Resolve host-specific skills in one read-only, multi-host operation."""
        with closing(self.connect()) as connection, connection:
            servers = self._resolve_server_rows(connection, server_identifiers)
            hosts: list[dict[str, Any]] = []
            applies_to: dict[str, list[str]] = {}
            skill_rows: dict[str, sqlite3.Row] = {}
            for server in servers:
                rows = connection.execute(
                    """
                    SELECT sk.*
                    FROM server_skills ss JOIN skills sk ON sk.id = ss.skill_id
                    WHERE ss.server_id = ?
                    ORDER BY sk.name COLLATE NOCASE
                    """,
                    (server["id"],),
                ).fetchall()
                summaries = []
                for row in rows:
                    skill_rows[row["id"]] = row
                    applies_to.setdefault(row["id"], []).append(server["alias"])
                    summaries.append(
                        {
                            "id": row["id"],
                            "name": row["name"],
                            "path": row["path"],
                            "description": row["description"],
                            "status": "registered",
                        }
                    )
                hosts.append({"id": server["id"], "alias": server["alias"], "skills": summaries})

            skills = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "path": row["path"],
                    "description": row["description"],
                    "status": "registered",
                    "applies_to": applies_to[row["id"]],
                }
                for row in sorted(skill_rows.values(), key=lambda item: item["name"].casefold())
            ]
        return {"ok": True, "hosts": hosts, "skills": skills}

    # --- saved working directories -------------------------------------------------
    #
    # Unlike skills, a saved directory belongs to exactly one host: "/srv/app" is a
    # statement about that machine's filesystem, so the relation is one-to-many and
    # deleting the host takes its directories with it.

    @staticmethod
    def _find_server_path_row(
        connection: sqlite3.Connection, server_id: str, identifier: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM server_paths WHERE server_id = ? AND id = ?",
            (server_id, identifier),
        ).fetchone()
        if not row:
            try:
                key = path_label_key(identifier)
            except ValidationError:
                key = ""
            row = connection.execute(
                "SELECT * FROM server_paths WHERE server_id = ? AND label_key = ?",
                (server_id, key),
            ).fetchone()
        if not row:
            raise NotFoundError(f"saved directory not found: {identifier}")
        return row

    @staticmethod
    def _server_path_dict(row: sqlite3.Row, alias: str) -> dict[str, Any]:
        return {
            "id": row["id"],
            "server": alias,
            "label": row["label"],
            "path": row["path"],
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_used_at": row["last_used_at"],
        }

    # Most recently used first, then alphabetical. A directory that has never been
    # used sorts after every used one rather than jumping to the top.
    _PATH_ORDER = "ORDER BY last_used_at IS NULL, last_used_at DESC, label COLLATE NOCASE"

    def list_server_paths(self, server_identifier: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection, connection:
            server = self._find_server_row(connection, str(server_identifier))
            rows = connection.execute(
                f"SELECT * FROM server_paths WHERE server_id = ? {self._PATH_ORDER}",
                (server["id"],),
            ).fetchall()
            return [self._server_path_dict(row, server["alias"]) for row in rows]

    def get_server_path(self, server_identifier: str, identifier: str) -> dict[str, Any]:
        with closing(self.connect()) as connection, connection:
            server = self._find_server_row(connection, str(server_identifier))
            row = self._find_server_path_row(connection, server["id"], str(identifier))
            return self._server_path_dict(row, server["alias"])

    def create_server_path(
        self,
        server_identifier: str,
        *,
        label: str,
        path: str,
        notes: str | None = None,
        path_id: str | None = None,
    ) -> dict[str, Any]:
        label = validate_path_label(label)
        label_key = path_label_key(label)
        path = validate_saved_remote_path(path)
        notes = validate_path_note(notes)
        identifier = path_id or str(uuid.uuid4())
        now = utc_now()
        try:
            with self.transaction() as connection:
                server = self._find_server_row(connection, str(server_identifier))
                connection.execute(
                    """
                    INSERT INTO server_paths
                    (id, server_id, label, label_key, path, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (identifier, server["id"], label, label_key, path, notes, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                f"this host already has a saved directory with that label or path: {label}"
            ) from exc
        return self.get_server_path(server_identifier, identifier)

    def update_server_path(
        self, server_identifier: str, identifier: str, **changes: Any
    ) -> dict[str, Any]:
        try:
            with self.transaction() as connection:
                server = self._find_server_row(connection, str(server_identifier))
                current = self._server_path_dict(self._find_server_path_row(connection, server["id"], str(identifier)), server["alias"])
                label = validate_path_label(changes.get("label", current["label"]))
                label_key = path_label_key(label)
                path = validate_saved_remote_path(changes.get("path", current["path"]))
                notes = validate_path_note(changes.get("notes", current["notes"]))
                connection.execute(
                    """
                    UPDATE server_paths
                    SET label = ?, label_key = ?, path = ?, notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (label, label_key, path, notes, utc_now(), current["id"]),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                f"this host already has a saved directory with that label or path: {label}"
            ) from exc
        return self.get_server_path(server["id"], current["id"])

    def delete_server_path(self, server_identifier: str, identifier: str) -> dict[str, Any]:
        with self.transaction() as connection:
            server = self._find_server_row(connection, str(server_identifier))
            current = self._server_path_dict(self._find_server_path_row(connection, server["id"], str(identifier)), server["alias"])
            connection.execute("DELETE FROM server_paths WHERE id = ?", (current["id"],))
        return current

    def touch_server_path(self, server_identifier: str, identifier: str) -> dict[str, Any]:
        with self.transaction() as connection:
            server = self._find_server_row(connection, str(server_identifier))
            current = self._server_path_dict(self._find_server_path_row(connection, server["id"], str(identifier)), server["alias"])
            connection.execute(
                "UPDATE server_paths SET last_used_at = ? WHERE id = ?",
                (utc_now(), current["id"]),
            )
        return self.get_server_path(server["id"], current["id"])

    def set_server_paths(
        self, server_identifier: str, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Replace one host's saved directories in a single transaction."""
        normalized: list[tuple[str, str, str, str]] = []
        seen_labels: set[str] = set()
        seen_paths: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValidationError("each saved directory must be an object")
            label = validate_path_label(entry.get("label", ""))
            key = path_label_key(label)
            path = validate_saved_remote_path(entry.get("path"))
            notes = validate_path_note(entry.get("notes"))
            if key in seen_labels:
                raise ConflictError(f"duplicate saved directory label: {label}")
            if path in seen_paths:
                raise ConflictError(f"duplicate saved directory path: {path}")
            seen_labels.add(key)
            seen_paths.add(path)
            normalized.append((label, key, path, notes))
        now = utc_now()
        with self.transaction() as connection:
            server = self._find_server_row(connection, str(server_identifier))
            # Carry last_used_at across a replace so reordering the list in the UI
            # does not erase which directories the user actually works in.
            previous = {
                row["path"]: row["last_used_at"]
                for row in connection.execute(
                    "SELECT path, last_used_at FROM server_paths WHERE server_id = ?",
                    (server["id"],),
                ).fetchall()
            }
            connection.execute("DELETE FROM server_paths WHERE server_id = ?", (server["id"],))
            connection.executemany(
                """
                INSERT INTO server_paths
                (id, server_id, label, label_key, path, notes, created_at, updated_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(uuid.uuid4()),
                        server["id"],
                        label,
                        key,
                        path,
                        notes,
                        now,
                        now,
                        previous.get(path),
                    )
                    for label, key, path, notes in normalized
                ],
            )
        return self.list_server_paths(server_identifier)

    def resolve_server_paths(self, server_identifiers: list[str]) -> dict[str, Any]:
        """Resolve saved working directories for one or more hosts, read-only.

        The deduplicated view collapses identical paths across hosts so an agent can
        see that several machines share a layout. This never touches the network:
        it reports what the user saved, not what currently exists on disk.
        """
        with closing(self.connect()) as connection, connection:
            servers = self._resolve_server_rows(connection, server_identifiers)
            hosts: list[dict[str, Any]] = []
            applies_to: dict[str, list[str]] = {}
            labels: dict[str, str] = {}
            for server in servers:
                rows = connection.execute(
                    f"SELECT * FROM server_paths WHERE server_id = ? {self._PATH_ORDER}",
                    (server["id"],),
                ).fetchall()
                summaries = []
                for row in rows:
                    applies_to.setdefault(row["path"], []).append(server["alias"])
                    labels.setdefault(row["path"], row["label"])
                    summaries.append(
                        {
                            "id": row["id"],
                            "label": row["label"],
                            "path": row["path"],
                            "notes": row["notes"],
                            "last_used_at": row["last_used_at"],
                        }
                    )
                hosts.append({"id": server["id"], "alias": server["alias"], "paths": summaries})

            paths = [
                {"path": path, "label": labels[path], "applies_to": aliases}
                for path, aliases in sorted(applies_to.items(), key=lambda item: item[0])
            ]
        return {"ok": True, "hosts": hosts, "paths": paths}

    # --- saved port-forwarding profiles ---------------------------------------------
    #
    # A tunnel profile is a saved -L/-R/-D spelling plus the host it reports to.
    # Runtime state (pid, log) deliberately lives outside the database in the
    # private runtime directory: it describes this machine's processes, not the
    # inventory, and a copied or versioned database must never claim a tunnel is
    # running somewhere it is not.

    @staticmethod
    def _tunnel_dict(row: sqlite3.Row, alias: str) -> dict[str, Any]:
        entry = {
            "id": row["id"],
            "server": alias,
            "name": row["label"],
            "kind": row["kind"],
            "bind_host": row["bind_host"],
            "bind_port": row["bind_port"],
            "remote_host": row["remote_host"],
            "remote_port": row["remote_port"],
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        entry["forward"] = format_forward_spec(entry)
        return entry

    def _find_tunnel_row(self, connection: sqlite3.Connection, identifier: str) -> tuple[sqlite3.Row, str]:
        text = str(identifier).strip()
        row = connection.execute(
            """
            SELECT t.*, s.alias AS alias
            FROM server_tunnels t JOIN servers s ON s.id = t.server_id
            WHERE t.id = ?
            """,
            (text,),
        ).fetchone()
        if row:
            return row, row["alias"]
        alias, separator, label = text.partition(":")
        if separator and label:
            try:
                key = tunnel_name_key(label)
            except ValidationError:
                key = ""
            if key:
                row = connection.execute(
                    """
                    SELECT t.*, s.alias AS alias
                    FROM server_tunnels t JOIN servers s ON s.id = t.server_id
                    WHERE s.alias = ? COLLATE NOCASE AND t.label_key = ?
                    """,
                    (alias.strip(), key),
                ).fetchone()
                if row:
                    return row, row["alias"]
        try:
            key = tunnel_name_key(text)
        except ValidationError as exc:
            raise NotFoundError(f"tunnel not found: {identifier}") from exc
        row = connection.execute(
            """
            SELECT t.*, s.alias AS alias
            FROM server_tunnels t JOIN servers s ON s.id = t.server_id
            WHERE t.label_key = ?
            """,
            (key,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"tunnel not found: {identifier}")
        return row, row["alias"]

    def _tunnel_duplicate(self, connection: sqlite3.Connection, entry: dict[str, Any], *, exclude_id: str | None = None) -> None:
        """Refuse a second profile on the same host that would bind the same port."""
        row = connection.execute(
            """
            SELECT label FROM server_tunnels
            WHERE server_id = ? AND kind = ? AND bind_host = ? AND bind_port = ?
              AND IFNULL(remote_host, '') = IFNULL(?, '')
              AND IFNULL(remote_port, 0) = IFNULL(?, 0)
              AND id != ?
            """,
            (
                entry["server_id"], entry["kind"], entry["bind_host"], entry["bind_port"],
                entry["remote_host"], entry["remote_port"], exclude_id or "",
            ),
        ).fetchone()
        if row:
            raise ConflictError(
                f"tunnel '{row['label']}' already forwards that specification for this host"
                f" ({format_forward_spec(entry)})"
            )

    def list_tunnels(self, server_identifier: str | None = None) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection, connection:
            if server_identifier is not None:
                server = self._find_server_row(connection, str(server_identifier))
                rows = connection.execute(
                    """
                    SELECT t.*, s.alias AS alias
                    FROM server_tunnels t JOIN servers s ON s.id = t.server_id
                    WHERE t.server_id = ?
                    ORDER BY t.label COLLATE NOCASE
                    """,
                    (server["id"],),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT t.*, s.alias AS alias
                    FROM server_tunnels t JOIN servers s ON s.id = t.server_id
                    ORDER BY s.alias COLLATE NOCASE, t.label COLLATE NOCASE
                    """
                ).fetchall()
            return [self._tunnel_dict(row, row["alias"]) for row in rows]

    def get_tunnel(self, identifier: str) -> dict[str, Any]:
        with closing(self.connect()) as connection, connection:
            row, alias = self._find_tunnel_row(connection, identifier)
            return self._tunnel_dict(row, alias)

    def create_tunnel(
        self,
        server_identifier: str,
        *,
        name: str,
        kind: str = "local",
        forward: str | None = None,
        bind_host: str | None = None,
        bind_port: int | str | None = None,
        remote_host: str | None = None,
        remote_port: int | str | None = None,
        notes: str | None = None,
        tunnel_id: str | None = None,
    ) -> dict[str, Any]:
        label = validate_tunnel_name(name)
        kind = validate_tunnel_kind(kind)
        if forward is not None:
            spec = parse_forward_spec(forward, kind)
        elif bind_port is None:
            raise ValidationError(
                "a tunnel needs a forwarding specification (FORWARD) or a bind port"
            )
        elif kind == "dynamic":
            # Explicit fields are accepted so a UI can post structured data without
            # re-assembling a spelling the backend would parse again.
            spec = {
                "kind": kind,
                "bind_host": validate_bind_host(bind_host or DEFAULT_TUNNEL_BIND_HOST),
                "bind_port": validate_port(bind_port, allow_zero=True),
                "remote_host": None,
                "remote_port": None,
            }
        else:
            if remote_host is None or remote_port is None:
                raise ValidationError(
                    f"a {kind} tunnel needs both a remote host and a remote port"
                )
            spec = {
                "kind": kind,
                "bind_host": validate_bind_host(bind_host or DEFAULT_TUNNEL_BIND_HOST),
                "bind_port": validate_port(bind_port, allow_zero=True),
                "remote_host": validate_hostname(remote_host),
                "remote_port": validate_port(remote_port),
            }
        identifier = tunnel_id or str(uuid.uuid4())
        now = utc_now()
        try:
            with self.transaction() as connection:
                server = self._find_server_row(connection, str(server_identifier))
                entry = {"server_id": server["id"], **spec}
                self._tunnel_duplicate(connection, entry)
                connection.execute(
                    """
                    INSERT INTO server_tunnels
                    (id, server_id, label, label_key, kind, bind_host, bind_port,
                     remote_host, remote_port, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier, server["id"], label, tunnel_name_key(label), spec["kind"],
                        spec["bind_host"], spec["bind_port"], spec["remote_host"],
                        spec["remote_port"], validate_tunnel_note(notes), now, now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"a tunnel named '{label}' already exists") from exc
        return self.get_tunnel(identifier)

    def update_tunnel(
        self,
        identifier: str,
        *,
        name: str | None = None,
        kind: str | None = None,
        forward: str | None = None,
        bind_host: str | None = None,
        bind_port: int | str | None = None,
        remote_host: str | None = None,
        remote_port: int | str | None = None,
        notes: str | None = None,
        server_identifier: str | None = None,
    ) -> dict[str, Any]:
        try:
            with self.transaction() as connection:
                current_row, current_alias = self._find_tunnel_row(connection, identifier)
                current = self._tunnel_dict(current_row, current_alias)
                label = validate_tunnel_name(name if name is not None else current["name"])
                target = current_row["server_id"]
                if server_identifier is not None:
                    target = self._find_server_row(connection, str(server_identifier))["id"]
                kind_value = kind if kind is not None else current["kind"]
                spec = parse_forward_spec(
                    forward if forward is not None else self._forward_source(current, kind_value),
                    kind_value,
                )
                if bind_host is not None or bind_port is not None or remote_host is not None or remote_port is not None:
                    merged = {
                        **spec,
                        "bind_host": bind_host if bind_host is not None else spec["bind_host"],
                        "bind_port": bind_port if bind_port is not None else spec["bind_port"],
                        "remote_host": remote_host if remote_host is not None else spec["remote_host"],
                        "remote_port": remote_port if remote_port is not None else spec["remote_port"],
                    }
                    spec = parse_forward_spec(format_forward_spec(merged), kind_value)
                entry = {"server_id": target, **spec}
                self._tunnel_duplicate(connection, entry, exclude_id=current["id"])
                connection.execute(
                    """
                    UPDATE server_tunnels
                    SET server_id = ?, label = ?, label_key = ?, kind = ?, bind_host = ?,
                        bind_port = ?, remote_host = ?, remote_port = ?, notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        target, label, tunnel_name_key(label), spec["kind"], spec["bind_host"],
                        spec["bind_port"], spec["remote_host"], spec["remote_port"],
                        validate_tunnel_note(notes if notes is not None else current["notes"]),
                        utc_now(), current["id"],
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"a tunnel named '{label}' already exists") from exc
        return self.get_tunnel(current["id"])

    @staticmethod
    def _forward_source(entry: dict[str, Any], kind: str) -> str:
        """Reuse a stored specification only when the forwarding kind is unchanged."""
        if kind != entry["kind"]:
            raise ValidationError("changing the forwarding kind needs a new FORWARD specification")
        return format_forward_spec(entry)

    def delete_tunnel(self, identifier: str) -> dict[str, Any]:
        with self.transaction() as connection:
            row, alias = self._find_tunnel_row(connection, identifier)
            entry = self._tunnel_dict(row, alias)
            connection.execute("DELETE FROM server_tunnels WHERE id = ?", (entry["id"],))
        return entry

    def list_servers(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection, connection:
            return self._list_servers(connection)

    def _list_servers(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(self._server_query() + " ORDER BY s.alias COLLATE NOCASE").fetchall()
        return [self._server_dict(row) for row in rows]

    def get_server(self, identifier: str) -> dict[str, Any]:
        with closing(self.connect()) as connection, connection:
            return self._get_server(connection, identifier)

    def _get_server(self, connection: sqlite3.Connection, identifier: str) -> dict[str, Any]:
        row = connection.execute(
            self._server_query() + " WHERE s.id = ? OR s.alias = ? COLLATE NOCASE", (identifier, identifier)
        ).fetchone()
        if not row:
            raise NotFoundError(f"server not found: {identifier}")
        return self._server_dict(row)

    def _validate_server_payload(self, payload: dict[str, Any], *, current_id: str | None = None, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        alias = validate_alias(str(payload["alias"]))
        credential_id = payload.get("credential_id") or None
        if credential_id:
            credential_id = (self._get_credential(connection, str(credential_id)) if connection is not None else self.get_credential(str(credential_id)))["id"]
        normalized = {
            "alias": alias,
            "hostname": validate_hostname(str(payload["hostname"])),
            "port": validate_port(payload.get("port", 22)),
            "username": validate_username(str(payload["username"])),
            "credential_id": credential_id,
            "proxy_jumps": validate_proxy_jumps(alias, payload.get("proxy_jumps", [])),
            "tags": validate_server_tags(payload.get("tags", [])),
            "notes": validate_server_note(payload.get("notes", "")),
            "source": str(payload.get("source", "managed")),
            "geo": validate_geo(payload.get("geo")),
        }
        self._validate_proxy_graph(normalized, current_id=current_id, connection=connection)
        return normalized

    def _validate_proxy_graph(self, candidate: dict[str, Any], *, current_id: str | None, connection: sqlite3.Connection | None = None) -> None:
        servers = self._list_servers(connection) if connection is not None else self.list_servers()
        old_alias = next((item["alias"].casefold() for item in servers if item["id"] == current_id), None)
        new_alias = candidate["alias"].casefold()
        # Validate the graph that will be committed, including dependents whose
        # references are rewritten when an alias changes.
        graph: dict[str, list[str]] = {
            item["alias"].casefold(): [new_alias if jump.casefold() == old_alias else jump.casefold()
                                      for jump in item["proxy_jumps"]]
            for item in servers
            if item["id"] != current_id
        }
        graph[new_alias] = [jump.casefold() for jump in candidate["proxy_jumps"]]

        # Iterative DFS keeps long valid hop chains independent of Python's
        # recursion limit. Unknown aliases remain external terminal nodes.
        state: dict[str, int] = {}
        for node in graph:
            if state.get(node) == 2:
                continue
            state[node] = 1
            stack = [(node, iter(graph[node]))]
            while stack:
                current, neighbors = stack[-1]
                neighbor = next(neighbors, None)
                if neighbor is None:
                    state[current] = 2
                    stack.pop()
                elif neighbor in graph:
                    if state.get(neighbor) == 1:
                        raise ValidationError("ProxyJump configuration contains a cycle")
                    if state.get(neighbor) != 2:
                        state[neighbor] = 1
                        stack.append((neighbor, iter(graph[neighbor])))

    def create_server(self, *, ssh_config_source: str | None = None, **payload: Any) -> dict[str, Any]:
        identifier = str(payload.get("id") or uuid.uuid4())
        now = utc_now()
        try:
            with self.transaction() as connection:
                normalized = self._validate_server_payload(payload, connection=connection)
                connection.execute(
                    """
                    INSERT INTO servers
                    (id, alias, hostname, port, username, credential_id, proxy_jumps, tags, notes, source, geo, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        normalized["alias"],
                        normalized["hostname"],
                        normalized["port"],
                        normalized["username"],
                        normalized["credential_id"],
                        json.dumps(normalized["proxy_jumps"]),
                        json.dumps(normalized["tags"], ensure_ascii=False),
                        normalized["notes"],
                        normalized["source"],
                        json.dumps(normalized["geo"], ensure_ascii=False) if normalized["geo"] is not None else None,
                        now,
                        now,
                    ),
                )
                self._register_contexts(connection, normalized["tags"])
                if ssh_config_source is not None:
                    self._set_ssh_config_source(connection, identifier, ssh_config_source)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"server alias already exists: {normalized['alias']}") from exc
        return self.get_server(identifier)

    def set_server_geo(
        self, identifier: str, geo: dict[str, Any] | None, *, overwrite_manual: bool = False
    ) -> dict[str, Any]:
        """Store a looked-up location, never over a value a human asserted."""
        normalized = validate_geo(geo, allow_geoip=True)
        with self.transaction() as connection:
            current = self._get_server(connection, identifier)
            existing = current.get("geo") or {}
            if (
                isinstance(existing, dict)
                and existing.get("source") == "manual"
                and not overwrite_manual
            ):
                raise ConflictError(
                    f"{current['alias']} has a manual location override; "
                    "replace it explicitly if the lookup should win"
                )
            connection.execute(
                "UPDATE servers SET geo = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(normalized, ensure_ascii=False) if normalized is not None else None,
                    utc_now(),
                    current["id"],
                ),
            )
        return self.get_server(current["id"])

    def update_server(self, identifier: str, *, ssh_config_source: str | None = None, **changes: Any) -> dict[str, Any]:
        try:
            with self.transaction() as connection:
                current = self._get_server(connection, identifier)
                payload = {**current, **changes}
                normalized = self._validate_server_payload(payload, current_id=current["id"], connection=connection)
                connection.execute(
                    """
                    UPDATE servers SET alias = ?, hostname = ?, port = ?, username = ?, credential_id = ?,
                        proxy_jumps = ?, tags = ?, notes = ?, source = ?, geo = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        normalized["alias"],
                        normalized["hostname"],
                        normalized["port"],
                        normalized["username"],
                        normalized["credential_id"],
                        json.dumps(normalized["proxy_jumps"]),
                        json.dumps(normalized["tags"], ensure_ascii=False),
                        normalized["notes"],
                        normalized["source"],
                        json.dumps(normalized["geo"], ensure_ascii=False) if normalized["geo"] is not None else None,
                        utc_now(),
                        current["id"],
                    ),
                )
                if (current["hostname"], current["port"]) != (normalized["hostname"], normalized["port"]):
                    # Observations belong to the checked endpoint, not a reused alias.
                    connection.execute(
                        """UPDATE servers SET facts = NULL, last_resolved_ip = NULL, host_key = NULL,
                            last_success_at = NULL, last_test_at = NULL, last_test_status = NULL,
                            last_test_latency_ms = NULL, last_test_error_code = NULL, last_test_message = NULL
                            WHERE id = ?""", (current["id"],),
                    )
                if current["alias"].casefold() != normalized["alias"].casefold():
                    dependent_rows = connection.execute("SELECT id, proxy_jumps FROM servers WHERE id != ?", (current["id"],)).fetchall()
                    for row in dependent_rows:
                        jumps = json.loads(row["proxy_jumps"] or "[]")
                        rewritten = [
                            normalized["alias"] if jump.casefold() == current["alias"].casefold() else jump
                            for jump in jumps
                        ]
                        if rewritten != jumps:
                            connection.execute(
                                "UPDATE servers SET proxy_jumps = ?, updated_at = ? WHERE id = ?",
                                (json.dumps(rewritten), utc_now(), row["id"]),
                            )
                self._register_contexts(connection, normalized["tags"])
                if ssh_config_source is not None:
                    self._set_ssh_config_source(connection, current["id"], ssh_config_source)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"server alias already exists: {normalized['alias']}") from exc
        return self.get_server(current["id"])

    def update_server_notes(self, identifier: str, text: str | None, *, append: bool = False) -> dict[str, Any]:
        """Set or append a human-authored note without changing host settings."""
        note = validate_server_note(text)
        if append and not note:
            raise ValidationError("note text is required when appending")
        with self.transaction() as connection:
            current = self._get_server(connection, identifier)
            combined = f"{current['notes']}\n\n{note}" if append and current["notes"] else note
            combined = validate_server_note(combined)
            connection.execute(
                "UPDATE servers SET notes = ?, updated_at = ? WHERE id = ?",
                (combined, utc_now(), current["id"]),
            )
        return self.get_server(current["id"])

    def delete_server(self, identifier: str) -> dict[str, Any]:
        with self.transaction() as connection:
            current = self._get_server(connection, identifier)
            target = current["alias"].casefold()
            dependents = [s["alias"] for s in self._list_servers(connection) if target in {j.casefold() for j in s["proxy_jumps"]}]
            if dependents:
                raise ConflictError(f"server is used as ProxyJump by: {', '.join(dependents)}")
            connection.execute("DELETE FROM servers WHERE id = ?", (current["id"],))
            sources = self._ssh_config_sources(connection)
            if sources.pop(current["id"], None) is not None:
                self._save_ssh_config_sources(connection, sources)
        return current

    def record_test(
        self,
        identifier: str,
        *,
        status: str,
        latency_ms: int,
        error_code: str | None,
        message: str,
        expected_endpoint: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            current = self._get_server(connection, identifier)
            if expected_endpoint is not None and expected_endpoint != (current["hostname"], current["port"]):
                return current
            checked_at = utc_now()
            connection.execute(
                """
                UPDATE servers SET last_test_at = ?, last_test_status = ?, last_test_latency_ms = ?,
                    last_test_error_code = ?, last_test_message = ?, updated_at = ?,
                    last_success_at = CASE WHEN ? = 'ok' THEN ? ELSE last_success_at END WHERE id = ?
                """,
                (checked_at, status, latency_ms, error_code, message[:500], checked_at, status, checked_at, current["id"]),
            )
        return self.get_server(current["id"])

    def record_observation(
        self, identifier: str, *, facts: dict[str, Any] | None = None,
        resolved_ip: str | None = None, host_key: dict[str, Any] | None = None,
        observed_at: str | None = None,
        expected_endpoint: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        """Persist explicit check results; omitted observations retain prior values."""
        observed = observation_time(observed_at)
        changes: dict[str, Any] = {}
        if facts is not None:
            changes["facts"] = json.dumps(sanitize_facts(facts, observed_at=observed), ensure_ascii=False)
        if resolved_ip is not None:
            changes["last_resolved_ip"] = validate_resolved_ip(resolved_ip)
        if host_key is not None:
            changes["host_key"] = json.dumps(sanitize_host_key(host_key, observed_at=observed))
        with self.transaction() as connection:
            current = self._get_server(connection, identifier)
            if expected_endpoint is not None and expected_endpoint != (current["hostname"], current["port"]):
                return current
            if changes:
                assignments = ", ".join(f"{field} = ?" for field in changes)
                connection.execute(
                    f"UPDATE servers SET {assignments}, updated_at = ? WHERE id = ?",
                    (*changes.values(), observed, current["id"]),
                )
        return self.get_server(current["id"])

    @staticmethod
    def _ssh_config_sources(connection: sqlite3.Connection) -> dict[str, str]:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (SSH_CONFIG_SOURCES_SETTING,)).fetchone()
        if row is None:
            return {}
        try:
            sources = json.loads(row["value"])
        except (TypeError, ValueError) as exc:
            raise DatabaseError("invalid SSH config import sources") from exc
        if not isinstance(sources, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in sources.items()
        ):
            raise DatabaseError("invalid SSH config import sources")
        return sources

    @staticmethod
    def _save_ssh_config_sources(connection: sqlite3.Connection, sources: dict[str, str]) -> None:
        connection.execute(
            """INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (SSH_CONFIG_SOURCES_SETTING, json.dumps(sources, ensure_ascii=False, sort_keys=True), utc_now()),
        )

    def _set_ssh_config_source(self, connection: sqlite3.Connection, identifier: str, config: str) -> None:
        if not config or any(ord(char) < 32 or ord(char) == 127 for char in config):
            raise ValidationError("SSH config source must be a printable local path")
        canonical = str(Path(config).expanduser().resolve())
        sources = self._ssh_config_sources(connection)
        sources[identifier] = canonical
        self._save_ssh_config_sources(connection, sources)

    def get_ssh_config_sources(self) -> dict[str, str]:
        with closing(self.connect()) as connection:
            return self._ssh_config_sources(connection)

    def set_server_ssh_config_source(self, identifier: str, config: str) -> None:
        with self.transaction() as connection:
            server = self._get_server(connection, identifier)
            self._set_ssh_config_source(connection, server["id"], config)

    def get_setting(self, key: str) -> str | None:
        with closing(self.connect()) as connection, connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, utc_now()),
            )

    def list_webauthn_credentials(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection, connection:
            rows = connection.execute("SELECT * FROM webauthn_credentials ORDER BY created_at").fetchall()
        return [
            {
                "credential_id": row["credential_id"],
                "public_key": row["public_key"],
                "sign_count": row["sign_count"],
                "transports": json.loads(row["transports"] or "[]"),
                "device_type": row["device_type"],
                "backed_up": bool(row["backed_up"]),
            }
            for row in rows
        ]

    def save_webauthn_credential(self, credential: dict[str, Any]) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO webauthn_credentials
                (credential_id, public_key, sign_count, transports, device_type, backed_up, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(credential_id) DO UPDATE SET public_key = excluded.public_key,
                    sign_count = excluded.sign_count, transports = excluded.transports,
                    device_type = excluded.device_type, backed_up = excluded.backed_up,
                    updated_at = excluded.updated_at
                """,
                (
                    credential["credential_id"],
                    credential["public_key"],
                    int(credential.get("sign_count", 0)),
                    json.dumps(credential.get("transports", [])),
                    credential.get("device_type"),
                    int(bool(credential.get("backed_up"))),
                    now,
                    now,
                ),
            )

    def update_webauthn_sign_count(self, credential_id: str, sign_count: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE webauthn_credentials SET sign_count = ?, updated_at = ? WHERE credential_id = ?",
                (sign_count, utc_now(), credential_id),
            )
