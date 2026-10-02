#!/usr/bin/env python3
"""Disposable host UI fixture. Never connects to hosts or uses the OS keychain.

Run from ssh-server-manager:
    .venv/bin/python tests/manual/hosts_fixture.py --port 8765 --open-browser

Without --open-browser, use the private launch URL file announced on stdout.
Do not print its contents or put the one-time token into screenshots or logs.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ssh_server_manager import webapp
from ssh_server_manager.db import Database, DatabaseError
from ssh_server_manager.service import CredentialService, SkillService
from ssh_server_manager.ssh_runner import SSHError
from ssh_server_manager.validation import ValidationError
from ssh_server_manager.vault import MemoryVault


MODES = ("demo", "empty", "bootstrap-error", "bootstrap-retry")


class DeniedSSHRunner:
    """Fail closed for every runner method, including future remote actions."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return self._deny

    @staticmethod
    def _deny(*args, **kwargs):
        raise SSHError("Remote operations are disabled in this isolated UI fixture")


def deny_local_file_access(*args, **kwargs):
    raise ValidationError("Local file import and credential-file edits are disabled in this UI fixture")


def seed_hosts(database: Database) -> None:
    """Synthetic metadata only: reserved .invalid domains, no usable secrets."""
    agent = database.create_credential(label="Demo SSH agent", kind="agent", credential_id="demo-agent")
    password = database.create_credential(
        label="Demo password (not stored)", kind="password", credential_id="demo-password"
    )
    specs = [
        dict(alias="bastion-prod", hostname="bastion.prod.example.invalid", username="ops",
             credential_id=agent["id"], tags=["production", "infrastructure"],
             notes="Entry point for the production network. Synthetic demo host; all remote actions are blocked."),
        dict(alias="api-prod-eu-01", hostname="api-01.eu.example.invalid", username="deploy",
             credential_id=agent["id"], tags=["production", "api", "Europe"], proxy_jumps=["bastion-prod"],
             notes="Primary API node\nOwner: Platform team\nRunbook: restart only after draining traffic."),
        dict(alias="api-staging", hostname="api.staging.example.invalid", username="deploy", port=2222,
             credential_id=password["id"], tags=["staging", "api"],
             notes="Integration environment. Demo password metadata has no stored secret."),
        dict(alias="db-prod-replica", hostname="db-replica.prod.example.invalid", username="postgres",
             credential_id=agent["id"], tags=["production", "database"], proxy_jumps=["bastion-prod"],
             notes="Read replica. Last check is a seeded failure for UI testing only."),
        dict(alias="edge-lab", hostname="192.0.2.24", username="lab", port=2200,
             tags=["development", "edge"], notes="New host; no check has been run."),
        dict(alias="worker-staging-long-context-name-01", hostname="worker-01.staging.example.invalid",
             username="automation", credential_id=agent["id"], tags=["staging", "background-workers", "QA 演示"],
             notes=("Long-note fixture: check line wrapping, scrolling, and keyboard focus. " * 24)
                   + "\nLiteral markup must stay text: <img src=x onerror=alert('fixture')> & <script>fixture</script>\n"
                   + "Unicode text: 部署说明 · café · Δοκιμή · 🚀"),
        dict(alias="legacy-timeout", hostname="legacy.example.invalid", username="maintainer",
             tags=["legacy", "maintenance"], notes="Seeded timeout. No actual network test was performed."),
        dict(alias="untagged-host", hostname="untagged.example.invalid", username="user", notes=""),
    ]
    for index, spec in enumerate(specs, start=1):
        database.create_server(id=f"fixture-host-{index}", **spec)
    for alias, status, latency, code, message in [
        ("bastion-prod", "ok", 22, None, "Synthetic successful check"),
        ("api-prod-eu-01", "ok", 48, None, "Synthetic successful check"),
        ("api-staging", "ok", 131, None, "Synthetic successful check"),
        ("db-prod-replica", "failed", 17, "authentication-failed", "Synthetic authentication failure; no connection attempted"),
        ("legacy-timeout", "failed", 10000, "timeout", "Synthetic timeout; no connection attempted"),
    ]:
        database.record_test(alias, status=status, latency_ms=latency, error_code=code, message=message)
    database.create_server_context("archived-empty")
    database.create_server_path("api-prod-eu-01", label="Application", path="/srv/demo-api", notes="Synthetic saved directory")


@contextmanager
def fixture_environment(mode: str = "demo"):
    """Keep every path and service replacement scoped to this fixture process."""
    if mode not in MODES:
        raise ValueError(f"unsupported fixture mode: {mode}")
    with tempfile.TemporaryDirectory(prefix="ssm-hosts-fixture-") as directory, ExitStack() as stack:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        stack.enter_context(patch.dict(os.environ, {
            "SSM_DATA_DIR": str(root / "data"),
            "SSM_RUNTIME_DIR": str(root / "runtime"),
            "SSM_MANAGED_SSH_CONFIG": str(root / "managed.conf"),
            "SSM_ORIGINAL_SSH_CONFIG": str(root / "missing-original.conf"),
            "HOME": str(home),
            "USERPROFILE": str(home),
        }))
        database = Database(root / "data" / "manager.db")
        vault = MemoryVault()
        stack.enter_context(patch.object(webapp, "get_vault", return_value=vault))
        stack.enter_context(patch("ssh_server_manager.vault.get_vault", return_value=vault))
        stack.enter_context(patch.object(webapp, "SSHRunner", DeniedSSHRunner))
        stack.enter_context(patch.object(webapp, "preview_import", deny_local_file_access))
        for method in ("discover", "register", "refresh"):
            stack.enter_context(patch.object(SkillService, method, deny_local_file_access))
        for method in ("create_key", "update"):
            stack.enter_context(patch.object(CredentialService, method, deny_local_file_access))
        if mode != "empty":
            seed_hosts(database)
        if mode in {"bootstrap-error", "bootstrap-retry"}:
            real_list_servers = database.list_servers
            failures_remaining = 1

            def list_servers_with_error():
                nonlocal failures_remaining
                if mode == "bootstrap-error" or failures_remaining:
                    failures_remaining -= 1
                    raise DatabaseError("Synthetic bootstrap failure for the isolated UI fixture")
                return real_list_servers()

            stack.enter_context(patch.object(database, "list_servers", list_servers_with_error))
        yield database, vault, root


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, default="demo")
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 selects an unused port")
    parser.add_argument("--open-browser", action="store_true", help="Open the standard one-time URL in the default browser")
    parser.add_argument("--url-file", type=Path, help="Explicit private one-time URL path (deleted after use or shutdown)")
    args = parser.parse_args(argv)
    with fixture_environment(args.mode) as (database, _vault, root):
        print(f"Isolated host UI fixture: {args.mode}; all remote operations disabled", flush=True)
        webapp.run_ui(
            database=database,
            port=args.port,
            open_browser=args.open_browser,
            url_file=args.url_file or root / "launch-url",
        )


if __name__ == "__main__":
    main()
