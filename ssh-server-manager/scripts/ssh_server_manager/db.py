from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .paths import database_path, ensure_private_dir
from .validation import (
    ValidationError,
    validate_alias,
    validate_hostname,
    validate_key_path,
    validate_kind,
    validate_label,
    validate_port,
    validate_proxy_jumps,
    validate_username,
)


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        is_new = not self.path.exists()
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise DatabaseError(f"database schema {version} is newer than supported {SCHEMA_VERSION}")
            if version == SCHEMA_VERSION:
                return
            connection.executescript(
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

                CREATE INDEX IF NOT EXISTS idx_servers_credential ON servers(credential_id);
                PRAGMA user_version = 1;
                """
            )
        if is_new and os.name != "nt":
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)

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
            "notes": row["notes"],
            "source": row["source"],
            "last_test_at": row["last_test_at"],
            "last_test_status": row["last_test_status"],
            "last_test_latency_ms": row["last_test_latency_ms"],
            "last_test_error_code": row["last_test_error_code"],
            "last_test_message": row["last_test_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_credentials(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM credentials ORDER BY label COLLATE NOCASE").fetchall()
        return [self._credential_dict(row) for row in rows]

    def get_credential(self, identifier: str) -> dict[str, Any]:
        with self.connect() as connection:
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
        current = self.get_credential(identifier)
        label = validate_label(changes.get("label", current["label"]))
        key_path = changes.get("key_path", current["key_path"])
        if current["kind"] == "key":
            key_path = validate_key_path(key_path)
        has_secret = bool(changes.get("has_secret", current["has_secret"]))
        has_passphrase = bool(changes.get("has_passphrase", current["has_passphrase"]))
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    UPDATE credentials SET label = ?, key_path = ?, has_secret = ?, has_passphrase = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (label, key_path, int(has_secret), int(has_passphrase), utc_now(), current["id"]),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"credential label already exists: {label}") from exc
        return self.get_credential(current["id"])

    def delete_credential(self, identifier: str) -> dict[str, Any]:
        current = self.get_credential(identifier)
        try:
            with self.transaction() as connection:
                connection.execute("DELETE FROM credentials WHERE id = ?", (current["id"],))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("credential is still assigned to one or more servers") from exc
        return current

    def _server_query(self) -> str:
        return """
            SELECT s.*, c.label AS credential_label, c.kind AS credential_kind
            FROM servers s LEFT JOIN credentials c ON c.id = s.credential_id
        """

    def list_servers(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(self._server_query() + " ORDER BY s.alias COLLATE NOCASE").fetchall()
        return [self._server_dict(row) for row in rows]

    def get_server(self, identifier: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                self._server_query() + " WHERE s.id = ? OR s.alias = ? COLLATE NOCASE", (identifier, identifier)
            ).fetchone()
        if not row:
            raise NotFoundError(f"server not found: {identifier}")
        return self._server_dict(row)

    def _validate_server_payload(self, payload: dict[str, Any], *, current_id: str | None = None) -> dict[str, Any]:
        alias = validate_alias(str(payload["alias"]))
        credential_id = payload.get("credential_id") or None
        if credential_id:
            credential_id = self.get_credential(str(credential_id))["id"]
        normalized = {
            "alias": alias,
            "hostname": validate_hostname(str(payload["hostname"])),
            "port": validate_port(payload.get("port", 22)),
            "username": validate_username(str(payload["username"])),
            "credential_id": credential_id,
            "proxy_jumps": validate_proxy_jumps(alias, payload.get("proxy_jumps", [])),
            "notes": str(payload.get("notes", "")),
            "source": str(payload.get("source", "managed")),
        }
        self._validate_proxy_graph(normalized, current_id=current_id)
        return normalized

    def _validate_proxy_graph(self, candidate: dict[str, Any], *, current_id: str | None) -> None:
        servers = self.list_servers()
        graph: dict[str, list[str]] = {
            item["alias"].casefold(): [jump.casefold() for jump in item["proxy_jumps"]]
            for item in servers
            if item["id"] != current_id
        }
        graph[candidate["alias"].casefold()] = [jump.casefold() for jump in candidate["proxy_jumps"]]

        def visit(node: str, active: set[str], complete: set[str]) -> None:
            if node in active:
                raise ValidationError("ProxyJump configuration contains a cycle")
            if node in complete or node not in graph:
                return
            active.add(node)
            for neighbor in graph[node]:
                visit(neighbor, active, complete)
            active.remove(node)
            complete.add(node)

        complete: set[str] = set()
        for node in graph:
            visit(node, set(), complete)

    def create_server(self, **payload: Any) -> dict[str, Any]:
        normalized = self._validate_server_payload(payload)
        identifier = str(payload.get("id") or uuid.uuid4())
        now = utc_now()
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO servers
                    (id, alias, hostname, port, username, credential_id, proxy_jumps, notes, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        normalized["alias"],
                        normalized["hostname"],
                        normalized["port"],
                        normalized["username"],
                        normalized["credential_id"],
                        json.dumps(normalized["proxy_jumps"]),
                        normalized["notes"],
                        normalized["source"],
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"server alias already exists: {normalized['alias']}") from exc
        return self.get_server(identifier)

    def update_server(self, identifier: str, **changes: Any) -> dict[str, Any]:
        current = self.get_server(identifier)
        payload = {**current, **changes}
        normalized = self._validate_server_payload(payload, current_id=current["id"])
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    UPDATE servers SET alias = ?, hostname = ?, port = ?, username = ?, credential_id = ?,
                        proxy_jumps = ?, notes = ?, source = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        normalized["alias"],
                        normalized["hostname"],
                        normalized["port"],
                        normalized["username"],
                        normalized["credential_id"],
                        json.dumps(normalized["proxy_jumps"]),
                        normalized["notes"],
                        normalized["source"],
                        utc_now(),
                        current["id"],
                    ),
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
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"server alias already exists: {normalized['alias']}") from exc
        return self.get_server(current["id"])

    def delete_server(self, identifier: str) -> dict[str, Any]:
        current = self.get_server(identifier)
        target = current["alias"].casefold()
        dependents = [s["alias"] for s in self.list_servers() if target in {j.casefold() for j in s["proxy_jumps"]}]
        if dependents:
            raise ConflictError(f"server is used as ProxyJump by: {', '.join(dependents)}")
        with self.transaction() as connection:
            connection.execute("DELETE FROM servers WHERE id = ?", (current["id"],))
        return current

    def record_test(
        self,
        identifier: str,
        *,
        status: str,
        latency_ms: int,
        error_code: str | None,
        message: str,
    ) -> dict[str, Any]:
        current = self.get_server(identifier)
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE servers SET last_test_at = ?, last_test_status = ?, last_test_latency_ms = ?,
                    last_test_error_code = ?, last_test_message = ?, updated_at = ? WHERE id = ?
                """,
                (utc_now(), status, latency_ms, error_code, message[:500], utc_now(), current["id"]),
            )
        return self.get_server(current["id"])

    def get_setting(self, key: str) -> str | None:
        with self.connect() as connection:
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
        with self.connect() as connection:
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
