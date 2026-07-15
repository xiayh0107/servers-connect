from __future__ import annotations

import glob
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .db import ConflictError, Database, NotFoundError
from .paths import original_ssh_config_path
from .validation import ValidationError, validate_alias


class ImportError(RuntimeError):
    pass


def _read_config_aliases(config: Path, seen: set[Path] | None = None) -> tuple[list[str], list[str]]:
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
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not tokens:
            continue
        directive = tokens[0].casefold()
        if directive == "include":
            for pattern in tokens[1:]:
                expanded = Path(os.path.expanduser(pattern))
                if not expanded.is_absolute():
                    expanded = config.parent / expanded
                for match in sorted(glob.glob(str(expanded))):
                    child_aliases, child_skipped = _read_config_aliases(Path(match), seen)
                    aliases.extend(child_aliases)
                    skipped.extend(child_skipped)
        elif directive == "host":
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
        [ssh_binary, "-G", "-F", str(config), alias],
        text=True,
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
    }


def preview_import(
    database: Database,
    *,
    config: str | Path | None = None,
    ssh_binary: str = "ssh",
) -> dict[str, Any]:
    config_path = Path(config) if config else original_ssh_config_path()
    config_path = config_path.expanduser().resolve()
    aliases, skipped = _read_config_aliases(config_path)
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for alias in aliases:
        try:
            candidate = _resolve_alias(alias, config_path, ssh_binary=ssh_binary)
            try:
                existing = database.get_server(alias)
            except NotFoundError:
                action = "add"
            else:
                comparable = ("hostname", "port", "username", "proxy_jumps")
                action = "unchanged" if all(existing[key] == candidate[key] for key in comparable) else "conflict"
            items.append({"action": action, "server": candidate})
        except (ImportError, ValidationError, ValueError) as exc:
            errors.append({"alias": alias, "message": str(exc)})
    return {"config": str(config_path), "items": items, "skipped_patterns": skipped, "errors": errors}


def apply_import(
    database: Database,
    preview: dict[str, Any],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    result = {"added": [], "updated": [], "unchanged": [], "skipped": [], "errors": preview["errors"]}
    for item in preview["items"]:
        action = item["action"]
        server = item["server"]
        try:
            if action == "add":
                result["added"].append(database.create_server(**server)["alias"])
            elif action == "conflict" and overwrite:
                result["updated"].append(database.update_server(server["alias"], **server)["alias"])
            elif action == "unchanged":
                result["unchanged"].append(server["alias"])
            else:
                result["skipped"].append(server["alias"])
        except (ConflictError, ValidationError) as exc:
            result["errors"].append({"alias": server["alias"], "message": str(exc)})
    return result

