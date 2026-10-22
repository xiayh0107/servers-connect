#!/usr/bin/env python3
"""Upgrade a populated, released 0.8.0 installation in a disposable environment.

Download the actual published wheel, seed schema 5 through its own database API,
then force-install the candidate wheel and verify schema 7 without losing data.
This never opens the OS credential vault, reads user state, or connects to SSH.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import venv
from contextlib import closing
from pathlib import Path


RELEASE = "0.8.0"
LEGACY_SCHEMA = 5
CANDIDATE_SCHEMA = 8
FIXTURE_PASSWORD = "disposable-upgrade-fixture-password"
LEGACY_TABLES = (
    "credentials", "servers", "settings", "webauthn_credentials",
    "skills", "server_skills", "server_paths",
)


def require(condition: bool, message: str) -> None:
    # Do not include record values, authentication hashes, or subprocess output
    # in failures. The isolated database is never emitted as a test artifact.
    if not condition:
        raise RuntimeError(message)


def snapshot(path: Path, tables: tuple[str, ...] | None = None) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        require(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                "SQLite integrity check failed")
        require(not connection.execute("PRAGMA foreign_key_check").fetchall(),
                "SQLite foreign-key integrity check failed")
        selected = tables or tuple(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"))
        result = {}
        for table in selected:
            # Table and column names come exclusively from SQLite's own schema.
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")]
            require(bool(columns), f"Missing table: {table}")
            quoted = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
            rows = connection.execute(f"SELECT {quoted} FROM {quoted_table} ORDER BY {quoted}").fetchall()
            result[table] = {"columns": columns, "rows": [list(row) for row in rows]}
        return result


def private_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as target:
        if os.name != "nt":
            os.fchmod(target.fileno(), 0o600)
        json.dump(value, target, ensure_ascii=False)


def installed_package(environment: Path, expected_version: str) -> None:
    import ssh_server_manager

    package_file = Path(ssh_server_manager.__file__).resolve()
    require(package_file.is_relative_to(environment.resolve()),
            "Package was imported from outside the disposable virtual environment")
    require(importlib.metadata.version("ssh-server-manager") == expected_version,
            "Installed distribution has the wrong version")
    require(ssh_server_manager.__version__ == expected_version,
            "Installed module has the wrong version")


def worker(mode: str, root: Path, expected_version: str) -> None:
    # This worker imports only the metadata database and disclosure auth layer.
    # A future incidental vault/SSH operation must fail before reaching user state.
    from unittest.mock import patch
    from ssh_server_manager import vault

    def denied(*_args, **_kwargs):
        raise RuntimeError("Upgrade smoke attempted a forbidden vault or network operation")

    with patch.object(vault, "get_vault", denied), \
            patch.object(vault.KeyringVault, "__init__", denied), \
            patch("socket.socket.connect", denied), patch("socket.socket.connect_ex", denied), \
            patch("socket.create_connection", denied), patch("subprocess.Popen", denied):
        installed_package(root / "environment", expected_version)
        from ssh_server_manager.auth import RevealAuth
        from ssh_server_manager.db import Database, SCHEMA_VERSION

        database = Database()
        require(database.path.resolve() == (root / "data" / "manager.db").resolve(),
                "Database did not use the isolated data directory")
        auth = RevealAuth(database, origin="http://localhost")
        if mode == "seed":
            require(SCHEMA_VERSION == LEGACY_SCHEMA, "Published 0.8.0 is not schema 5")
            fixtures = root / "fixtures"
            fixtures.mkdir()
            key_path = fixtures / "metadata-only-key"
            # A metadata-only key path deliberately has no usable private key.
            key_path.write_text("upgrade fixture: not a private key\n", encoding="utf-8")
            if os.name != "nt":
                key_path.chmod(0o600)
            skill_path = fixtures / "upgrade-skill" / "SKILL.md"
            skill_path.parent.mkdir()
            skill_path.write_text("---\nname: upgrade-fixture\ndescription: Disposable migration fixture.\n---\n",
                                  encoding="utf-8")
            password = database.create_credential(label="升级 Password", kind="password",
                has_secret=True, credential_id="upgrade-password-id")
            key = database.create_credential(label="升级 Key", kind="key", key_path=str(key_path),
                has_passphrase=True, credential_id="upgrade-key-id")
            agent = database.create_credential(label="升级 Agent", kind="agent",
                credential_id="upgrade-agent-id")
            hosts = [
                {"id": "upgrade-jump-id", "alias": "upgrade-jump", "hostname": "192.0.2.10",
                 "credential_id": key["id"], "tags": ["生产", "jump"], "proxy_jumps": []},
                {"id": "upgrade-success-id", "alias": "upgrade-success", "hostname": "192.0.2.20",
                 "credential_id": password["id"], "tags": ["生产", "unicode 标签"],
                 "proxy_jumps": ["upgrade-jump"]},
                {"id": "upgrade-failed-id", "alias": "upgrade-failed", "hostname": "example.invalid",
                 "credential_id": agent["id"], "tags": ["staging"],
                 "proxy_jumps": ["external-fixture-jump"]},
                {"id": "upgrade-unchecked-id", "alias": "upgrade-unchecked", "hostname": "198.51.100.30",
                 "credential_id": None, "tags": [], "proxy_jumps": []},
            ]
            for index, host in enumerate(hosts):
                database.create_server(**host, username="fixture-user", port=2200 + index,
                    notes="升级保留笔记\nLiteral <script> & # ? / Unicode ✓", source="managed")
            database.record_test("upgrade-success-id", status="ok", latency_ms=12,
                                 error_code=None, message="Saved successful historical check")
            database.record_test("upgrade-failed-id", status="failed", latency_ms=34,
                                 error_code="auth_failed", message="Saved failed historical check")
            database.create_server_context("empty-context")
            database.create_server_context("Assigned context", server_ids=["upgrade-success-id"])
            database.create_skill(name="upgrade-fixture", path=skill_path,
                description="保留 Skill metadata & bindings", skill_id="upgrade-skill-id",
                server_identifiers=["upgrade-success-id", "upgrade-failed-id"])
            database.create_server_path("upgrade-success-id", label="数据目录",
                path="/srv/data with spaces/中文 # & ?", notes="Saved path note\n第二行",
                path_id="upgrade-used-path-id")
            database.touch_server_path("upgrade-success-id", "upgrade-used-path-id")
            database.create_server_path("upgrade-unchecked-id", label="Home",
                path="~/workspace", path_id="upgrade-unused-path-id")
            auth.enroll_master_password(FIXTURE_PASSWORD)
            database.set_setting("webauthn_user_id", "dXBncmFkZS1maXh0dXJlLXVzZXI")
            database.set_setting("upgrade_custom_setting", "保留 arbitrary setting")
            # Metadata persistence is under test; this is not a usable passkey.
            database.save_webauthn_credential({
                "credential_id": "dXBncmFkZS1maXh0dXJlLWNyZWRlbnRpYWw",
                "public_key": "bm90LWEtdXNhYmxlLXBhc3NrZXktcHVibGljLWtleQ",
                "sign_count": 9, "transports": ["internal", "usb"],
                "device_type": "multi_device", "backed_up": True,
            })
            with closing(database.connect()) as connection:
                require(connection.execute("PRAGMA user_version").fetchone()[0] == LEGACY_SCHEMA,
                        "Published package did not create schema 5")
            private_json(root / "baseline.json", snapshot(database.path, LEGACY_TABLES))
            require(auth.verify_master_password(FIXTURE_PASSWORD), "Legacy authentication setup failed")
            print("Published 0.8.0 seeded schema 5: 4 hosts, 3 credentials, contexts, notes, paths, Skill bindings and auth settings")
            return

        require(SCHEMA_VERSION == CANDIDATE_SCHEMA, "Candidate does not support schema 8")
        with closing(database.connect()) as connection:
            require(connection.execute("PRAGMA user_version").fetchone()[0] == CANDIDATE_SCHEMA,
                    "Upgrade did not reach schema 8")
            baseline = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
            for table, saved in baseline.items():
                quoted = ", ".join('"' + column.replace('"', '""') + '"' for column in saved["columns"])
                rows = connection.execute(f'SELECT {quoted} FROM "{table}" ORDER BY {quoted}').fetchall()
                require([list(row) for row in rows] == saved["rows"],
                        f"Upgrade changed existing {table} records")
            require(connection.execute("SELECT count(*) FROM credential_tests").fetchone()[0] == 0,
                    "Upgrade invented credential test history")
            require(connection.execute("SELECT count(*) FROM credential_deletions").fetchone()[0] == 0,
                    "Upgrade invented credential deletion history")
        hosts = database.list_servers()
        for host in hosts:
            require(all(host[field] is None for field in ("facts", "last_resolved_ip", "host_key", "geo")),
                    "Upgrade invented host observations")
            expected_success = host["last_test_at"] if host["last_test_status"] == "ok" else None
            require(host["last_success_at"] == expected_success,
                    "Upgrade did not preserve a known successful check correctly")
        for credential in database.list_credentials():
            require(credential["revision"] == 1 and credential["key_metadata"] is None
                    and credential["last_test"] is None, "Credential migration defaults are incorrect")
        require(len(database.list_skills("upgrade-success-id")) == 1
                and len(database.list_skills("upgrade-failed-id")) == 1,
                "Migrated Skill bindings cannot be read")
        require(len(database.list_server_paths("upgrade-success-id")) == 1
                and len(database.list_server_paths("upgrade-unchecked-id")) == 1,
                "Migrated saved directories cannot be read")
        require(auth.status()["master_password_enrolled"] and auth.status()["passkeys"] == 1,
                "Migrated authentication enrollment cannot be read")
        require(auth.verify_master_password(FIXTURE_PASSWORD)
                and not auth.verify_master_password("wrong-disposable-password"),
                "Migrated master-password verification changed")
        current = snapshot(database.path)
        Database()
        Database()
        require(snapshot(database.path) == current, "Repeated database startup changed migrated data")
        print(f"Candidate {expected_version}: schema 5 to 8, all original records/auth settings preserved, defaults and repeated startup passed")


def run_upgrade(distribution: Path, expected_version: str) -> None:
    require(distribution.is_file() and distribution.suffix == ".whl", "Candidate must be an existing wheel")
    with tempfile.TemporaryDirectory(prefix="ssm-upgrade-") as temporary:
        root = Path(temporary)
        environment = root / "environment"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        isolated = {**os.environ, "SSM_DATA_DIR": str(root / "data"),
                    "SSM_RUNTIME_DIR": str(root / "runtime"),
                    "SSM_ORIGINAL_SSH_CONFIG": str(root / "original.conf"),
                    "SSM_MANAGED_SSH_CONFIG": str(root / "managed.conf"),
                    "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
        isolated.pop("PYTHONPATH", None)
        isolated.pop("PYTHONHOME", None)

        def run(arguments: list[str], stage: str, *, timeout: int = 180) -> str:
            try:
                result = subprocess.run([str(python), *arguments], cwd=root, env=isolated,
                    capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"Upgrade smoke timed out during {stage}") from None
            require(result.returncode == 0, f"Upgrade smoke failed during {stage} (subprocess output withheld)")
            return result.stdout

        wheels = root / "released"
        run(["-m", "pip", "download", "--no-deps", "--only-binary=:all:",
             "--dest", str(wheels), f"ssh-server-manager=={RELEASE}"], "published-wheel download")
        published = list(wheels.glob("ssh_server_manager-0.8.0-*.whl"))
        require(len(published) == 1, "Could not identify the published 0.8.0 wheel")
        run(["-m", "pip", "install", str(published[0])], "published-wheel installation")
        script = str(Path(__file__).resolve())
        # -I excludes this script's checkout directory from sys.path. Merely
        # changing cwd would still import scripts/ssh_server_manager locally.
        print(run(["-I", script, "--worker", "seed", "--root", str(root), "--version", RELEASE],
                  "released-data seeding", timeout=60).strip())
        # Force replacement also validates development candidates whose package
        # version still matches 0.8.0. Dependencies were installed above.
        run(["-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-deps",
             str(distribution)], "candidate-wheel upgrade")
        run(["-m", "pip", "check"], "candidate dependency consistency")
        print(run(["-I", script, "--worker", "verify", "--root", str(root), "--version", expected_version],
                  "candidate migration verification", timeout=60).strip())
    print("Upgrade isolation cleanup passed; no OS vault or SSH operations were permitted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution", type=Path, nargs="?")
    parser.add_argument("--version", required=True)
    parser.add_argument("--worker", choices=("seed", "verify"), help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    try:
        if arguments.worker:
            require(arguments.root is not None, "Internal worker has no isolated directory")
            worker(arguments.worker, arguments.root.resolve(), arguments.version)
        else:
            require(arguments.distribution is not None, "A candidate wheel is required")
            run_upgrade(arguments.distribution.resolve(), arguments.version)
    except (RuntimeError, OSError, sqlite3.Error) as error:
        raise SystemExit(str(error)) from None
