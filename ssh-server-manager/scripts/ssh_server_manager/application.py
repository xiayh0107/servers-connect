"""Shared use cases for CLI, HTTP, and future desktop shells.

This layer knows about the existing core, but not HTTP sessions, GUI windows,
or output formatting. Constructing it and reading host metadata never opens
the credential vault or starts an SSH process.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .db import Database
from .importer import apply_import, preview_import
from .service import CredentialService, SkillService
from .ssh_config import render_config
from .ssh_runner import SSHRunner
from .validation import ValidationError
from .vault import VaultProtocol, get_vault


class HostService:
    def __init__(self, database: Database, render: Callable[[], Path]) -> None:
        self.database = database
        self.render = render

    def list(self, *, query: str | None = None, tag: str | None = None) -> list[dict[str, Any]]:
        hosts = self.database.list_servers()
        if tag is not None:
            hosts = [host for host in hosts if tag.casefold() in {item.casefold() for item in host["tags"]}]
        if query:
            needle = query.casefold()
            hosts = [host for host in hosts if needle in " ".join(
                [host["alias"], host["hostname"], host["username"], host["notes"], *host["tags"]]
            ).casefold()]
        return hosts

    def get(self, identifier: str) -> dict[str, Any]:
        return self.database.get_server(identifier)

    def workspace(self, identifier: str) -> dict[str, Any]:
        host = self.get(identifier)
        return {
            "host": host,
            "credential": self.database.get_credential(host["credential_id"]) if host["credential_id"] else None,
            "paths": self.database.list_server_paths(host["id"]),
            "skills": SkillService(self.database).list(host["id"]),
        }

    def create(self, **payload: Any) -> dict[str, Any]:
        result = self.database.create_server(**payload)
        self.render()
        return result

    def update(self, identifier: str, **changes: Any) -> dict[str, Any]:
        result = self.database.update_server(identifier, **changes)
        self.render()
        return result

    def delete(self, identifier: str) -> dict[str, Any]:
        result = self.database.delete_server(identifier)
        self.render()
        return result

    def notes(self, identifier: str, text: str = "", *, mode: str = "append") -> dict[str, Any]:
        if mode not in {"append", "set", "clear"}:
            raise ValidationError("note mode must be append, set, or clear")
        return self.database.update_server_notes(identifier, "" if mode == "clear" else text, append=mode == "append")


class FileService:
    def __init__(self, database: Database, runner: Callable[[], SSHRunner]) -> None:
        self.database = database
        self.runner = runner

    def list_directory(self, identifier: str, path: str | None = None) -> dict[str, Any]:
        return self.runner().list_directory(identifier, path)

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
    def __init__(self, runner: Callable[[], SSHRunner]) -> None:
        self.runner = runner

    def test(self, identifier: str, *, timeout: int = 8) -> dict[str, Any]:
        return self.runner().test(identifier, timeout=timeout)

    def diagnose(self, identifier: str, *, timeout: int = 8) -> dict[str, Any]:
        return self.runner().diagnose(identifier, timeout=timeout)


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

    def list(self) -> list[dict[str, Any]]:
        return self.database.list_credentials()

    def get(self, identifier: str) -> dict[str, Any]:
        return self.database.get_credential(identifier)

    def _service(self) -> CredentialService:
        return CredentialService(self.database, self.vault())

    def create_password(self, label: str, secret: str) -> dict[str, Any]:
        result = self._service().create_password(label, secret)
        self.render()
        return result

    def create_key(self, label: str, key_path: str, passphrase: str | None = None) -> dict[str, Any]:
        result = self._service().create_key(label, key_path, passphrase)
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
        result = self._service().delete(identifier)
        self.render()
        return result

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
        self.diagnostics = DiagnosticsService(self.runner)
        self.connections = ConnectionService(self.runner)
        self.credentials = CredentialApplicationService(database, vault_factory or get_vault, self.render_config)
        self.skills = SkillService(database)
        self.contexts = ContextService(database)
        self.imports = ImportService(database, self.render_config)
