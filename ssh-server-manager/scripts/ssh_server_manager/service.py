from __future__ import annotations

import os
import shlex
import uuid
from pathlib import Path
from typing import Any, Sequence

import yaml

from .db import Database
from .validation import (
    ValidationError,
    is_skill_manifest_name,
    skill_name_key,
    validate_skill_description,
    validate_skill_name,
    validate_skill_path,
)
from .vault import VaultProtocol


MAX_SKILL_FRONTMATTER_BYTES = 64 * 1024
MAX_SKILL_YAML_DEPTH = 64
TRANSPORT_SKILL_NAME = "ssh-server-manager"


class _BoundedSafeLoader(yaml.SafeLoader):
    """SafeLoader with a frontmatter nesting cap before object construction."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._composition_depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        self._composition_depth += 1
        try:
            if self._composition_depth > MAX_SKILL_YAML_DEPTH:
                raise yaml.YAMLError("SKILL.md frontmatter nesting is too deep")
            return super().compose_node(parent, index)
        finally:
            self._composition_depth -= 1


class CredentialService:
    def __init__(self, database: Database, vault: VaultProtocol) -> None:
        self.database = database
        self.vault = vault

    def create_password(self, label: str, secret: str) -> dict[str, Any]:
        identifier = str(uuid.uuid4())
        credential = self.database.create_credential(
            credential_id=identifier, label=label, kind="password", has_secret=False
        )
        try:
            self.vault.set_secret(identifier, "password", secret)
            return self.database.update_credential(identifier, has_secret=True)
        except Exception:
            self.vault.delete_secret(identifier, "password")
            self.database.delete_credential(identifier)
            raise

    def create_key(self, label: str, key_path: str, passphrase: str | None = None) -> dict[str, Any]:
        identifier = str(uuid.uuid4())
        credential = self.database.create_credential(
            credential_id=identifier,
            label=label,
            kind="key",
            key_path=key_path,
            has_passphrase=False,
        )
        if not passphrase:
            return credential
        try:
            self.vault.set_secret(identifier, "passphrase", passphrase)
            return self.database.update_credential(identifier, has_passphrase=True)
        except Exception:
            self.vault.delete_secret(identifier, "passphrase")
            self.database.delete_credential(identifier)
            raise

    def create_agent(self, label: str) -> dict[str, Any]:
        return self.database.create_credential(label=label, kind="agent")

    def update(
        self,
        identifier: str,
        *,
        label: str | None = None,
        key_path: str | None = None,
        secret: str | None = None,
        passphrase: str | None = None,
        clear_passphrase: bool = False,
    ) -> dict[str, Any]:
        credential = self.database.get_credential(identifier)
        changes: dict[str, Any] = {}
        if label is not None:
            changes["label"] = label
        if key_path is not None:
            changes["key_path"] = key_path
        if secret is not None:
            if credential["kind"] != "password":
                raise ValueError("only password credentials have a password secret")
            self.vault.set_secret(credential["id"], "password", secret)
            changes["has_secret"] = True
        if passphrase is not None:
            if credential["kind"] != "key":
                raise ValueError("only key credentials have a passphrase")
            self.vault.set_secret(credential["id"], "passphrase", passphrase)
            changes["has_passphrase"] = True
        if clear_passphrase:
            self.vault.delete_secret(credential["id"], "passphrase")
            changes["has_passphrase"] = False
        return self.database.update_credential(credential["id"], **changes) if changes else credential

    def delete(self, identifier: str) -> dict[str, Any]:
        credential = self.database.get_credential(identifier)
        # The database enforces that referenced credentials cannot be removed.
        removed = self.database.delete_credential(credential["id"])
        self.vault.delete_secret(credential["id"], "password")
        self.vault.delete_secret(credential["id"], "passphrase")
        return removed

    def reveal(self, identifier: str) -> dict[str, Any]:
        credential = self.database.get_credential(identifier)
        if credential["kind"] == "password":
            slot = "password"
        elif credential["kind"] == "key" and credential["has_passphrase"]:
            slot = "passphrase"
        else:
            raise ValueError("this credential has no revealable secret")
        value = self.vault.get_secret(credential["id"], slot)
        if value is None:
            raise ValueError("the OS credential vault has no matching secret")
        return {"credential_id": credential["id"], "slot": slot, "value": value}


class SkillService:
    """Manage host-scoped Agent Skills without executing their contents."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _normalize_path(path: str | Path) -> Path:
        raw_path = str(path).strip()
        if not raw_path:
            raise ValidationError("skill path is required")
        candidate = Path(raw_path).expanduser()
        try:
            if candidate.is_dir():
                manifests = [
                    child
                    for child in candidate.iterdir()
                    if child.is_file() and is_skill_manifest_name(child.name)
                ]
                if len(manifests) > 1:
                    raise ValidationError(
                        f"skill directory contains multiple SKILL.md case variants: {candidate}"
                    )
                candidate = manifests[0] if manifests else candidate / "SKILL.md"
            candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValidationError(f"skill manifest does not exist: {candidate}") from exc
        if not candidate.is_file():
            raise ValidationError(f"skill manifest is not a file: {candidate}")
        return Path(validate_skill_path(candidate))

    @classmethod
    def read_metadata(cls, path: str | Path) -> dict[str, str]:
        """Read only a bounded YAML frontmatter block from one SKILL.md."""
        manifest = cls._normalize_path(path)
        try:
            with manifest.open("rb") as stream:
                first = stream.readline(MAX_SKILL_FRONTMATTER_BYTES + 1)
                consumed = len(first)
                if first.rstrip(b"\r\n") != b"---":
                    raise ValidationError("SKILL.md must start with YAML frontmatter")
                frontmatter: list[bytes] = []
                while consumed <= MAX_SKILL_FRONTMATTER_BYTES:
                    line = stream.readline(MAX_SKILL_FRONTMATTER_BYTES - consumed + 1)
                    if not line:
                        raise ValidationError("SKILL.md frontmatter is not closed")
                    consumed += len(line)
                    if consumed > MAX_SKILL_FRONTMATTER_BYTES:
                        break
                    if line.strip() == b"---":
                        break
                    frontmatter.append(line)
                else:  # pragma: no cover - loop exits through a concrete error above
                    raise ValidationError("SKILL.md frontmatter is too large")
                if consumed > MAX_SKILL_FRONTMATTER_BYTES:
                    raise ValidationError(
                        f"SKILL.md frontmatter must be at most {MAX_SKILL_FRONTMATTER_BYTES} bytes"
                    )
        except OSError as exc:
            raise ValidationError(f"cannot read skill manifest: {manifest}") from exc

        try:
            text = b"".join(frontmatter).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("SKILL.md frontmatter must be UTF-8") from exc
        try:
            metadata = yaml.load(text, Loader=_BoundedSafeLoader)
        except (yaml.YAMLError, RecursionError) as exc:
            raise ValidationError("SKILL.md frontmatter is invalid YAML") from exc
        if not isinstance(metadata, dict):
            raise ValidationError("SKILL.md frontmatter must be a mapping")
        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            raise ValidationError("SKILL.md frontmatter requires string name and description fields")
        return {
            "name": validate_skill_name(name),
            "description": validate_skill_description(description),
            "path": str(manifest),
        }

    @staticmethod
    def _ensure_host_scoped(metadata: dict[str, str]) -> None:
        if skill_name_key(metadata["name"]) == skill_name_key(TRANSPORT_SKILL_NAME):
            raise ValidationError(
                "ssh-server-manager is the base transport skill and cannot be bound to a host"
            )

    def register(
        self, path: str | Path, *, server_identifiers: list[str] | None = None
    ) -> dict[str, Any]:
        metadata = self.read_metadata(path)
        self._ensure_host_scoped(metadata)
        return self.database.create_skill(
            **metadata, server_identifiers=server_identifiers or []
        )

    def refresh(self, identifier: str, *, path: str | Path | None = None) -> dict[str, Any]:
        current = self.database.get_skill(identifier)
        metadata = self.read_metadata(path or current["path"])
        self._ensure_host_scoped(metadata)
        return self.database.update_skill(current["id"], **metadata)

    def delete(self, identifier: str) -> dict[str, Any]:
        return self.database.delete_skill(identifier)

    def attach(self, identifier: str, server_identifiers: list[str]) -> dict[str, Any]:
        return self.database.attach_skill(identifier, server_identifiers)

    def detach(self, identifier: str, server_identifiers: list[str]) -> dict[str, Any]:
        return self.database.detach_skill(identifier, server_identifiers)

    def set_servers(self, identifier: str, server_identifiers: list[str]) -> dict[str, Any]:
        return self.database.set_skill_servers(identifier, server_identifiers)

    def set_server_skills(
        self, server_identifier: str, skill_identifiers: list[str]
    ) -> list[dict[str, Any]]:
        self.database.set_server_skills(server_identifier, skill_identifiers)
        return self.list(server_identifier)

    def _status(self, skill: dict[str, Any]) -> dict[str, str]:
        path = Path(skill["path"])
        if not path.exists():
            return {"status": "missing", "status_message": f"SKILL.md not found: {path}"}
        try:
            metadata = self.read_metadata(path)
        except ValidationError as exc:
            return {"status": "invalid", "status_message": str(exc)}
        if metadata["name"] != skill["name"]:
            return {
                "status": "name_mismatch",
                "status_message": (
                    f"SKILL.md declares {metadata['name']!r}, expected {skill['name']!r}; "
                    "refresh the registered skill after reviewing the change"
                ),
            }
        return {"status": "ready"}

    def list(self, server_identifier: str | None = None) -> list[dict[str, Any]]:
        result = []
        for skill in self.database.list_skills(server_identifier):
            result.append({**skill, **self._status(skill)})
        return result

    def resolve(self, server_identifiers: list[str]) -> dict[str, Any]:
        resolution = self.database.resolve_skills(server_identifiers)
        statuses: dict[str, dict[str, str]] = {}
        for skill in resolution["skills"]:
            status = self._status(skill)
            skill.update(status)
            statuses[skill["id"]] = status
        for host in resolution["hosts"]:
            for skill in host["skills"]:
                skill.update(statuses[skill["id"]])
        resolution["ok"] = all(skill["status"] == "ready" for skill in resolution["skills"])
        return resolution

    @staticmethod
    def standard_roots() -> list[Path]:
        roots = [
            Path.home() / ".claude" / "skills",
            Path.home() / ".codex" / "skills",
            Path.home() / ".agents" / "skills",
        ]
        configured = os.environ.get("SSM_SKILLS_DIRS", "")
        if configured:
            entries = configured.split(";") if os.name == "nt" else shlex.split(configured)
            roots.extend(Path(entry).expanduser() for entry in entries if entry)
        result: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                resolved = root.resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            key = os.path.normcase(str(resolved))
            if key not in seen:
                result.append(resolved)
                seen.add(key)
        return result

    @staticmethod
    def _discover_paths(roots: Sequence[str | Path]) -> list[Path]:
        manifests: list[Path] = []
        seen_manifests: set[str] = set()
        seen_directories: set[str] = set()

        def ignore_error(_error: OSError) -> None:
            return None

        for raw_root in roots:
            root = Path(raw_root).expanduser()
            try:
                is_directory = root.is_dir()
            except (OSError, RuntimeError):
                continue
            if not is_directory:
                continue
            for current, directories, files in os.walk(root, followlinks=True, onerror=ignore_error):
                current_path = Path(current)
                try:
                    directory_key = os.path.normcase(str(current_path.resolve(strict=True)))
                except (OSError, RuntimeError):
                    directories[:] = []
                    continue
                if directory_key in seen_directories:
                    directories[:] = []
                    continue
                seen_directories.add(directory_key)
                manifest_names = [name for name in files if is_skill_manifest_name(name)]
                if not manifest_names:
                    continue
                for manifest_name in sorted(manifest_names):
                    try:
                        manifest = (current_path / manifest_name).resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    manifest_key = os.path.normcase(str(manifest))
                    if manifest_key not in seen_manifests:
                        manifests.append(manifest)
                        seen_manifests.add(manifest_key)
        return sorted(manifests, key=lambda path: os.path.normcase(str(path)))

    def discover(
        self, roots: Sequence[str | Path] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Inspect installed skills and report conflicts without mutating the database."""
        candidates: list[dict[str, Any]] = []
        for path in self._discover_paths(roots if roots is not None else self.standard_roots()):
            try:
                metadata = self.read_metadata(path)
                if skill_name_key(metadata["name"]) == skill_name_key(TRANSPORT_SKILL_NAME):
                    continue
                candidate: dict[str, Any] = {**metadata, "status": "available"}
            except ValidationError as exc:
                candidate = {"path": str(path), "status": "invalid", "error": str(exc)}
            candidates.append(candidate)

        conflicts: list[dict[str, Any]] = []
        valid = [candidate for candidate in candidates if candidate["status"] != "invalid"]
        by_name: dict[str, list[dict[str, Any]]] = {}
        for candidate in valid:
            by_name.setdefault(skill_name_key(candidate["name"]), []).append(candidate)
        for same_name in by_name.values():
            paths = sorted({candidate["path"] for candidate in same_name})
            if len(paths) > 1:
                conflict = {"type": "name", "name": same_name[0]["name"], "paths": paths}
                conflicts.append(conflict)
                for candidate in same_name:
                    candidate["status"] = "conflict"

        for registered in self.database.list_skills():
            registered_path = os.path.normcase(registered["path"])
            for candidate in valid:
                candidate_path = os.path.normcase(candidate["path"])
                same_name = skill_name_key(candidate["name"]) == skill_name_key(
                    registered["name"]
                )
                same_path = candidate_path == registered_path
                if same_name and same_path:
                    if candidate["status"] == "available":
                        candidate["status"] = "registered"
                    candidate["registered_id"] = registered["id"]
                elif same_name:
                    conflicts.append(
                        {
                            "type": "name",
                            "name": candidate["name"],
                            "paths": [registered["path"], candidate["path"]],
                            "registered_id": registered["id"],
                        }
                    )
                    candidate["status"] = "conflict"
                elif same_path:
                    conflicts.append(
                        {
                            "type": "path",
                            "path": candidate["path"],
                            "names": [registered["name"], candidate["name"]],
                            "registered_id": registered["id"],
                        }
                    )
                    candidate["status"] = "conflict"

        unique_conflicts: list[dict[str, Any]] = []
        seen_conflicts: set[str] = set()
        for conflict in conflicts:
            key = repr(sorted(conflict.items()))
            if key not in seen_conflicts:
                unique_conflicts.append(conflict)
                seen_conflicts.add(key)
        return {"candidates": candidates, "conflicts": unique_conflicts}
