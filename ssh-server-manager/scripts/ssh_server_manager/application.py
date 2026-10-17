"""Shared use cases for CLI, HTTP, and future desktop shells.

This layer knows about the existing core, but not HTTP sessions, GUI windows,
or output formatting. Constructing it and reading host metadata never opens
the credential vault or starts an SSH process.
"""
from __future__ import annotations

from collections.abc import Callable
from collections import Counter
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore, Event
import uuid
from typing import Any

from .db import ConflictError, Database, NotFoundError
from .credential_lock import credential_lock
from .credential_keys import read_snapshot, load_snapshot
from .importer import apply_import, preview_import
from .paths import managed_ssh_config_path
from .service import CredentialService, SkillService
from .ssh_config import render_config, validate_include_source
from .ssh_runner import SSHError, SSHRunner
from .validation import ValidationError
from .vault import ObservedVault, VaultError, VaultProtocol, get_vault


def _route_hop(alias: str, inventory: dict[str, dict[str, Any]]) -> dict[str, Any]:
    host = inventory.get(alias.casefold())
    if host is None:
        return {"alias": alias, "external": True}
    return {
        "alias": host["alias"], "external": False,
        **{field: host.get(field) for field in (
            "id", "hostname", "port", "last_test_at", "last_test_status",
            "last_test_error_code", "last_success_at",
        )},
    }


class HostService:
    def __init__(self, database: Database, render: Callable[[], Path]) -> None:
        self.database = database
        self.render = render

    def list(self, *, query: str | None = None, tag: str | None = None) -> list[dict[str, Any]]:
        hosts = self.database.list_servers()
        inventory, counts = self._index(hosts)
        hosts = [self._decorate(host, inventory, counts) for host in hosts]
        if tag is not None:
            hosts = [host for host in hosts if tag.casefold() in {item.casefold() for item in host["tags"]}]
        if query:
            needle = query.casefold()
            hosts = [host for host in hosts if needle in " ".join(
                [host["alias"], host["hostname"], host["username"], host["notes"], *host["tags"]]
            ).casefold()]
        return hosts

    def get(self, identifier: str) -> dict[str, Any]:
        inventory, counts = self._index(self.database.list_servers())
        return self._decorate(self._select(identifier, inventory), inventory, counts)

    @staticmethod
    def _select(identifier: str, inventory: dict[str, dict[str, Any]]) -> dict[str, Any]:
        host = next((item for item in inventory.values()
                     if item["id"] == identifier or item["alias"].casefold() == identifier.casefold()), None)
        if host is None:
            raise NotFoundError(f"server not found: {identifier}")
        return host

    @staticmethod
    def _index(hosts: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], Counter]:
        return ({item["alias"].casefold(): item for item in hosts},
                Counter(alias.casefold() for item in hosts for alias in item["proxy_jumps"]))

    @staticmethod
    def _decorate(host: dict[str, Any], inventory: dict[str, dict[str, Any]], counts: Counter) -> dict[str, Any]:
        return {
            **host,
            "proxy_jump_details": [_route_hop(alias, inventory) for alias in host["proxy_jumps"]],
            "jump_for_count": counts[host["alias"].casefold()],
        }

    def route(self, identifier: str) -> dict[str, Any]:
        """Resolve configured hops from local metadata without testing a host."""
        inventory = {item["alias"].casefold(): item for item in self.database.list_servers()}
        host = self._select(identifier, inventory)
        aliases = [host["alias"]]
        seen: set[str] = set()
        current = host
        # OpenSSH supplies the preceding explicit hops as -J to later hops,
        # overriding their own ProxyJump. Only the first hop inherits its
        # configured route. Iterate to support long chains without recursion.
        while current and current["proxy_jumps"]:
            key = current["alias"].casefold()
            if key in seen:
                break
            seen.add(key)
            jumps = current["proxy_jumps"]
            aliases = [*jumps, *aliases]
            current = inventory.get(jumps[0].casefold())
        hops = [_route_hop(alias, inventory) for alias in aliases]
        checked = host.get("last_test_at") or ""
        blocked = next((hop["alias"] for hop in hops[:-1]
                        if hop.get("last_test_status") == "failed"
                        and (hop.get("last_test_at") or "") > checked), None)
        return {"alias": host["alias"], "hops": hops, "blocked_by": blocked}

    def workspace(self, identifier: str) -> dict[str, Any]:
        host = self.get(identifier)
        return {
            "host": host,
            "credential": self.database.get_credential(host["credential_id"]) if host["credential_id"] else None,
            "paths": self.database.list_server_paths(host["id"]),
            "skills": SkillService(self.database).list(host["id"]),
        }

    def create(self, **payload: Any) -> dict[str, Any]:
        credential = self.database.get_credential(payload["credential_id"]) if payload.get("credential_id") else None
        with ExitStack() as stack:
            if credential:
                stack.enter_context(credential_lock(self.database.path, credential["id"]))
                self.database.get_credential(credential["id"])
                payload["credential_id"] = credential["id"]
            result = self.database.create_server(**payload)
        self.render()
        return self.get(result["id"])

    def update(self, identifier: str, **changes: Any) -> dict[str, Any]:
        with ExitStack() as stack:
            if "credential_id" in changes:
                current = self.database.get_server(identifier)
                selected = self.database.get_credential(changes["credential_id"]) if changes["credential_id"] else None
                ids = {item for item in (current["credential_id"], selected["id"] if selected else None) if item}
                for credential_id in sorted(ids):
                    stack.enter_context(credential_lock(self.database.path, credential_id))
                if selected:
                    self.database.get_credential(selected["id"])
                    changes["credential_id"] = selected["id"]
            result = self.database.update_server(identifier, **changes)
        self.render()
        return self.get(result["id"])

    def delete(self, identifier: str) -> dict[str, Any]:
        result = self.get(identifier)
        self.database.delete_server(identifier)
        self.render()
        return result

    def notes(self, identifier: str, text: str = "", *, mode: str = "append") -> dict[str, Any]:
        if mode not in {"append", "set", "clear"}:
            raise ValidationError("note mode must be append, set, or clear")
        result = self.database.update_server_notes(identifier, "" if mode == "clear" else text, append=mode == "append")
        return self.get(result["id"])


class FileService:
    def __init__(self, database: Database, runner: Callable[[], SSHRunner]) -> None:
        self.database = database
        self.runner = runner

    def list_directory(self, identifier: str, path: str | None = None, *, name_filter: str | None = None) -> dict[str, Any]:
        if name_filter is None:
            return self.runner().list_directory(identifier, path)
        return self.runner().list_directory(identifier, path, name_filter=name_filter)

    def download(self, identifier: str, remote_path: str, local_path: str | Path, **options: Any) -> dict[str, Any]:
        return self.runner().download(identifier, remote_path, local_path, **options)

    def upload(self, identifier: str, local_path: str | Path, remote_path: str, **options: Any) -> dict[str, Any]:
        return self.runner().upload(identifier, local_path, remote_path, **options)

    def list_paths(self, identifier: str) -> list[dict[str, Any]]:
        return self.database.list_server_paths(identifier)

    def create_path(self, identifier: str, **payload: Any) -> dict[str, Any]:
        return self.database.create_server_path(identifier, **payload)

    def update_path(self, identifier: str, path_id: str, **changes: Any) -> dict[str, Any]:
        return self.database.update_server_path(identifier, path_id, **changes)

    def replace_paths(self, identifier: str, paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.database.set_server_paths(identifier, paths)

    def touch_path(self, identifier: str, path_id: str) -> dict[str, Any]:
        return self.database.touch_server_path(identifier, path_id)

    def delete_path(self, identifier: str, path_id: str) -> dict[str, Any]:
        return self.database.delete_server_path(identifier, path_id)

    def resolve_paths(self, identifiers: list[str]) -> dict[str, Any]:
        return self.database.resolve_server_paths(identifiers)


class DiagnosticsService:
    def __init__(self, runner: Callable[[], SSHRunner], database: Database) -> None:
        self.runner = runner
        self.database = database
        self._bulk_slots = BoundedSemaphore(4)

    def test(self, identifier: str, *, timeout: int = 8) -> dict[str, Any]:
        return self.runner().test(identifier, timeout=timeout)

    def diagnose(self, identifier: str, *, timeout: int = 8) -> dict[str, Any]:
        return self.runner().diagnose(identifier, timeout=timeout)

    def observe(self, identifier: str, *, timeout: int = 8) -> dict[str, Any]:
        return self.runner().observe(identifier, timeout=timeout)

    def test_many(self, identifiers: list[str], *, timeout: int = 8, concurrency: int = 4) -> dict[str, Any]:
        return self._probe_many(identifiers, self.test, timeout=timeout, concurrency=concurrency)

    def observe_many(self, identifiers: list[str], *, timeout: int = 8, concurrency: int = 4) -> dict[str, Any]:
        return self._probe_many(identifiers, self.observe, timeout=timeout, concurrency=concurrency, observe=True)

    def _probe_many(self, identifiers: list[str], probe: Callable[..., dict[str, Any]], *,
                    timeout: int, concurrency: int, observe: bool = False) -> dict[str, Any]:
        if not 1 <= len(identifiers) <= 64 or not 1 <= concurrency <= 4 or not 1 <= timeout <= 120:
            raise ValidationError("batch probes require 1-64 hosts, concurrency 1-4, and timeout 1-120 seconds")
        # Resolve the whole selection before any remote work. Aliases and IDs
        # for the same host collapse to one check, in the caller's order.
        hosts = {}
        for identifier in identifiers:
            host = self.database.get_server(identifier)
            hosts[host["id"]] = host
        def check(host: dict[str, Any]) -> dict[str, Any]:
            try:
                with self._bulk_slots:
                    return probe(host["id"], timeout=timeout)
            except (SSHError, VaultError, ValidationError, NotFoundError):
                return {"alias": host["alias"], "ok": False, "latency_ms": 0,
                        "error_code": "observation-failed" if observe else "check-failed",
                        "message": "Host info collection failed; run diagnose for details" if observe else
                        "Connection check failed; run diagnose for details"}
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ssm-probe") as pool:
            return {"results": list(pool.map(check, hosts.values()))}


class ConnectionService:
    def __init__(self, runner: Callable[[], SSHRunner]) -> None:
        self.runner = runner

    def connect(self, identifier: str, *, timeout: int = 12) -> int:
        return self.runner().connect(identifier, timeout=timeout)

    def execute(self, identifier: str, command: list[str], **options: Any) -> dict[str, Any]:
        return self.runner().execute(identifier, command, **options)


class CredentialApplicationService:
    """Lazy vault access plus the config side effects shared by all shells."""
    def __init__(self, database: Database, vault: Callable[[], VaultProtocol], render: Callable[[], Path]) -> None:
        self.database = database
        self.vault = vault
        self.render = render
        self.vault_metadata: dict[str, Any] = {"name": None, "status": "unknown"}

    def list(self) -> list[dict[str, Any]]:
        return self.database.list_credentials()

    def get(self, identifier: str) -> dict[str, Any]:
        return self.database.get_credential(identifier)

    def _service(self) -> CredentialService:
        return CredentialService(self.database, self._vault())

    def _vault(self) -> VaultProtocol:
        try:
            vault = self.vault()
        except VaultError:
            self.vault_metadata["status"] = "unavailable"
            raise
        # Only an explicitly requested vault operation reaches this point.
        # Constructor/diagnose are not a health or unlock check.
        try:
            self.vault_metadata["name"] = str(vault.diagnose().get("backend"))
        except Exception:
            pass
        return ObservedVault(vault, self.vault_metadata)

    def create_password(self, label: str, secret: str) -> dict[str, Any]:
        result = self._service().create_password(label, secret)
        self.render()
        return result

    def create_key(self, label: str, key_path: str, passphrase: str | None = None,
                   *, expected_key_revision: str | None = None) -> dict[str, Any]:
        result = self._service().create_key(label, key_path, passphrase, expected_key_revision=expected_key_revision)
        self.render()
        return result

    def create_agent(self, label: str) -> dict[str, Any]:
        result = self.database.create_credential(label=label, kind="agent")
        self.render()
        return result

    def update(self, identifier: str, **changes: Any) -> dict[str, Any]:
        try:
            result = self._service().update(identifier, **changes)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        self.render()
        return result

    def delete(self, identifier: str) -> dict[str, Any]:
        preview = self.deletion_preview(identifier)
        operation = self.detach_delete(preview["credential"]["id"], operation_id=str(uuid.uuid4()),
            expected_updated_at=preview["expected_updated_at"], expected_host_ids=preview["expected_host_ids"], detach=False)
        return {**operation["credential"], "operation_id": operation["operation_id"],
            "committed": True, "cleanup_pending": operation["cleanup_pending"]}

    def deletion_preview(self, identifier: str) -> dict[str, Any]:
        return self.database.credential_deletion_preview(identifier)

    def detach_delete(self, identifier: str, *, operation_id: str, expected_updated_at: str,
                      expected_host_ids: list[str], detach: bool = True) -> dict[str, Any]:
        # Labels must share the UUID lock used by probes, edits and reveal.
        # The journal retains that identity after deletion, even if its label
        # has since been reused by a new credential.
        operation = None
        try:
            operation = self.database.get_credential_deletion(operation_id)
        except NotFoundError:
            pass
        if operation is None:
            try:
                credential_id = self.database.get_credential(identifier)["id"]
            except NotFoundError:
                # Another request may have committed after our journal read.
                operation = self.database.get_credential_deletion(operation_id)
        if operation is not None:
            if identifier != operation["credential_id"] and identifier.casefold() != operation["credential"]["label"].casefold():
                raise ConflictError("deletion operation id was already used for a different credential")
            credential_id = operation["credential_id"]
        with credential_lock(self.database.path, credential_id):
            operation = self.database.commit_credential_deletion(credential_id, operation_id=operation_id,
                expected_updated_at=expected_updated_at, expected_host_ids=expected_host_ids, detach=detach)
            return self._cleanup_deletion(operation)

    def _cleanup_deletion(self, operation: dict[str, Any]) -> dict[str, Any]:
        if not operation["cleanup_pending"]:
            return operation
        remaining = []
        codes = []
        vault = None
        for slot in operation["remaining_slots"]:
            try:
                if slot == "config":
                    self.render()
                else:
                    vault = vault or self._vault()
                    vault.delete_secret(operation["credential_id"], slot)
            except Exception:
                remaining.append(slot)
                codes.append("config_unavailable" if slot == "config" else "vault_unavailable")
        return self.database.update_credential_deletion(operation["operation_id"], remaining,
            error_code=codes[0] if codes else None)

    def retry_deletion(self, operation_id: str) -> dict[str, Any]:
        operation = self.database.get_credential_deletion(operation_id)
        with credential_lock(self.database.path, operation["credential_id"]):
            return self._cleanup_deletion(self.database.get_credential_deletion(operation_id))

    def deletions(self, *, pending: bool = False) -> list[dict[str, Any]]:
        return self.database.list_credential_deletions(pending=pending)

    def inspect_key(self, key_path: str, *, passphrase: str | None = None,
                    credential_id: str | None = None, use_stored_passphrase: bool = False,
                    expected_key_revision: str | None = None) -> tuple[dict[str, Any], str]:
        if use_stored_passphrase:
            if not credential_id or passphrase:
                raise ValidationError("select one source for the passphrase")
            credential = self.database.get_credential(credential_id)
            with credential_lock(self.database.path, credential["id"]):
                credential = self.database.get_credential(credential["id"])
                if credential["kind"] != "key" or not credential["has_passphrase"]:
                    raise ValidationError("credential has no stored key passphrase")
                passphrase = self._vault().get_secret(credential["id"], "passphrase")
                if passphrase is None:
                    raise VaultError("stored key passphrase is unavailable")
                return self.inspect_key(key_path, passphrase=passphrase, expected_key_revision=expected_key_revision)
        snapshot = read_snapshot(key_path)
        if expected_key_revision is not None and snapshot.revision != expected_key_revision:
            from .db import ConflictError
            raise ConflictError("private key changed; inspect it again")
        metadata = load_snapshot(snapshot, passphrase, allow_locked=not passphrase)
        snapshot.confirm()
        return metadata, snapshot.revision

    def test_stream(self, identifier: str, host_id: str, *, timeout: int = 8, cancel_event: Event | None = None):
        from .credential_probe import CredentialProbeRunner
        credential = self.database.get_credential(identifier)
        with credential_lock(self.database.path, credential["id"]):
            for event in CredentialProbeRunner(self.database, self._vault).stream(
                    credential["id"], host_id, timeout=timeout, cancel_event=cancel_event):
                if event["type"] == "result":
                    event = {**event, "result": self.database.record_credential_test(credential["id"], event["result"])}
                yield event

    def tests(self, identifier: str) -> list[dict[str, Any]]:
        return self.database.list_credential_tests(identifier)

    def reveal(self, identifier: str) -> dict[str, Any]:
        # Authorization belongs to the calling shell. HTTP must consume a
        # short-lived, credential-specific grant before calling this method.
        try:
            return self._service().reveal(identifier)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc


class ContextService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self) -> list[dict[str, Any]]:
        return self.database.list_server_contexts()

    def create(self, name: str, *, server_ids: list[str] | None = None) -> dict[str, Any]:
        return self.database.create_server_context(name, server_ids=server_ids)

    def update(self, name: str, **changes: Any) -> dict[str, Any]:
        return self.database.update_server_context(name, **changes)

    def delete(self, name: str) -> dict[str, Any]:
        return self.database.delete_server_context(name)


class ImportService:
    def __init__(self, database: Database, render: Callable[[], Path]) -> None:
        self.database = database
        self.render = render

    def preview(self, *, config: str | Path | None = None) -> dict[str, Any]:
        return preview_import(self.database, config=config)

    def apply(self, preview: dict[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
        validate_include_source(preview["config"], destination=managed_ssh_config_path())
        result = apply_import(self.database, preview, overwrite=overwrite)
        self.render()
        return result


class ApplicationServices:
    def __init__(
        self,
        database: Database,
        *,
        runner_factory: Callable[[], SSHRunner] | None = None,
        vault_factory: Callable[[], VaultProtocol] | None = None,
        config_renderer: Callable[[Database], Path] | None = None,
    ) -> None:
        self.database = database
        self.runner = runner_factory or (lambda: SSHRunner(database))
        self.render_config = lambda: (config_renderer or render_config)(database)
        self.hosts = HostService(database, self.render_config)
        self.files = FileService(database, self.runner)
        self.diagnostics = DiagnosticsService(self.runner, database)
        self.connections = ConnectionService(self.runner)
        self.credentials = CredentialApplicationService(database, vault_factory or get_vault, self.render_config)
        self.skills = SkillService(database)
        self.contexts = ContextService(database)
        self.imports = ImportService(database, self.render_config)
