from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .db import ConflictError, Database, NotFoundError
from .paths import original_ssh_config_path
from .process_args import literal_config_args
from .ssh_config import _IncludePaths, _expand_include, _parse_include_tokens, split_config_line
from .validation import ValidationError, validate_alias


class ImportError(RuntimeError):
    pass


def _read_config_aliases(
    config: Path, seen: set[Path] | None = None, *,
    ssh_binary: str = "ssh", _paths: _IncludePaths | None = None,
) -> tuple[list[str], list[str]]:
    paths = _paths or _IncludePaths(ssh_binary)
    config = config.expanduser().resolve()
    seen = seen or set()
    if config in seen or not config.exists():
        return [], []
    seen.add(config)
    aliases: list[str] = []
    skipped: list[str] = []
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ImportError(f"cannot read SSH config {config}: {exc}") from exc
    for line in lines:
        patterns = _parse_include_tokens(line)
        if patterns:
            for pattern in patterns:
                for match in _expand_include(pattern, paths):
                    if not match.is_file():
                        continue
                    child_aliases, child_skipped = _read_config_aliases(match, seen, _paths=paths)
                    aliases.extend(child_aliases)
                    skipped.extend(child_skipped)
            continue
        tokens = split_config_line(line)
        if not tokens:
            continue
        directive = tokens[0].casefold()
        if directive == "host":
            for token in tokens[1:]:
                if token.startswith("!") or any(mark in token for mark in "*?"):
                    skipped.append(token)
                    continue
                try:
                    aliases.append(validate_alias(token))
                except ValidationError:
                    skipped.append(token)
    deduped: list[str] = []
    known: set[str] = set()
    for alias in aliases:
        key = alias.casefold()
        if key not in known:
            deduped.append(alias)
            known.add(key)
    return deduped, sorted(set(skipped), key=str.casefold)


def _resolve_alias(alias: str, config: Path, *, ssh_binary: str = "ssh") -> dict[str, Any]:
    result = subprocess.run(
        literal_config_args([ssh_binary, "-G", "-F", str(config), alias]),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise ImportError(f"ssh -G failed for {alias}: {result.stderr.strip() or 'unknown error'}")
    values: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if separator:
            values.setdefault(key.casefold(), []).append(value.strip())
    proxy_value = values.get("proxyjump", ["none"])[0]
    proxy_jumps = [] if proxy_value.casefold() == "none" else [item for item in proxy_value.split(",") if item]
    for jump in proxy_jumps:
        try:
            validate_alias(jump)
        except ValidationError as exc:
            raise ImportError(
                f"{alias} uses a non-alias ProxyJump value ({jump}); create that jump host manually first"
            ) from exc
    return {
        "alias": alias,
        "hostname": values.get("hostname", [alias])[0],
        "port": int(values.get("port", ["22"])[0]),
        "username": values.get("user", [os.environ.get("USER", "unknown")])[0],
        "credential_id": None,
        "proxy_jumps": proxy_jumps,
        "notes": f"Imported from {config}",
        "source": "ssh-config-import",
        "_identity_files": [value for value in values.get("identityfile", []) if value.casefold() != "none"],
    }


def preview_import(
    database: Database,
    *,
    config: str | Path | None = None,
    ssh_binary: str = "ssh",
) -> dict[str, Any]:
    config_path = Path(config) if config else original_ssh_config_path()
    config_path = config_path.expanduser().resolve()
    aliases, skipped = _read_config_aliases(config_path, ssh_binary=ssh_binary)
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, Any]] = []
    for alias in aliases:
        try:
            candidate = _resolve_alias(alias, config_path, ssh_binary=ssh_binary)
            identity_files = candidate.pop("_identity_files", [])
            try:
                existing = database.get_server(alias)
            except NotFoundError:
                action = "add"
            else:
                comparable = ("hostname", "port", "username", "proxy_jumps")
                action = "unchanged" if all(existing[key] == candidate[key] for key in comparable) else "conflict"
                candidate["credential_id"] = existing["credential_id"]
                candidate["notes"] = existing["notes"]
            items.append({"action": action, "server": candidate})
            warnings.append({
                "alias": alias,
                "code": "authentication-not-imported",
                "message": (
                    "Imported authentication directives are not converted to managed credentials. "
                    "If applied, the host will continue using the source SSH config or an explicit saved credential. "
                    "Import does not verify authentication."
                ),
                "source_config": str(config_path),
                "identity_files": identity_files,
                "fallback_config": str(config_path),
            })
        except (ImportError, ValidationError, ValueError) as exc:
            errors.append({"alias": alias, "message": str(exc)})
    return {
        "config": str(config_path), "items": items, "skipped_patterns": skipped,
        "errors": errors, "warnings": warnings,
    }


def apply_import(
    database: Database,
    preview: dict[str, Any],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    result = {"added": [], "updated": [], "unchanged": [], "skipped": [], "errors": list(preview["errors"])}
    affected: set[str] = set()
    for item in preview["items"]:
        action = item["action"]
        server = item["server"]
        try:
            if action == "add":
                result["added"].append(database.create_server(ssh_config_source=preview["config"], **server)["alias"])
            elif action == "conflict" and overwrite:
                imported = {key: server[key] for key in ("hostname", "port", "username", "proxy_jumps", "source")}
                result["updated"].append(database.update_server(
                    server["alias"], ssh_config_source=preview["config"], **imported,
                )["alias"])
            elif action == "unchanged":
                database.set_server_ssh_config_source(server["alias"], preview["config"])
                result["unchanged"].append(server["alias"])
            else:
                result["skipped"].append(server["alias"])
                continue
            affected.add(server["alias"].casefold())
        except (ConflictError, NotFoundError, ValidationError) as exc:
            result["errors"].append({"alias": server["alias"], "message": str(exc)})
    result["warnings"] = [
        {**warning, "message": (
            "Imported authentication directives were not converted to managed credentials. "
            "The host uses the source SSH config or an explicit saved credential. "
            "Import did not verify authentication."
        )}
        for warning in preview.get("warnings", []) if warning["alias"].casefold() in affected
    ]
    return result

