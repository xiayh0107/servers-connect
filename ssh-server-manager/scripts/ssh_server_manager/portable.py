"""Move the inventory between machines as a file a git history can diff.

What travels: host profiles, their saved working directories, and their
forwarding profiles. What never travels: secrets, credential identifiers, and
anything observed remotely (facts, last check results, resolved IPs, host keys).
Those describe one machine's keychain and one moment's network, so copying them
would be either dangerous or wrong.

A host's credential is exported as a *label* — a name the human chose — and on
import it is attached only when a credential with that label already exists on
the destination machine. A missing credential is reported, never invented.

The document is deterministic: hosts sorted by alias, a fixed key order, no
timestamps. Exporting the same inventory twice produces byte-identical output, so
a commit shows real changes instead of churn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .db import ConflictError, Database, DatabaseError, NotFoundError
from .validation import ValidationError, validate_alias

FORMAT = "ssh-server-manager.export"
VERSION = 1

HOST_FIELDS = (
    "alias",
    "hostname",
    "port",
    "username",
    "proxy_jumps",
    "tags",
    "notes",
    "source",
    "geo",
    "credential",
)
PATH_FIELDS = ("label", "path", "notes")
TUNNEL_FIELDS = (
    "name",
    "kind",
    "bind_host",
    "bind_port",
    "remote_host",
    "remote_port",
    "notes",
)
TOP_LEVEL_FIELDS = ("format", "version", "hosts")


def export_document(database: Database) -> dict[str, Any]:
    """Build the portable document for the current inventory."""
    hosts: list[dict[str, Any]] = []
    for host in sorted(database.list_servers(), key=lambda item: item["alias"].casefold()):
        paths = [
            {field: item.get(field) or "" for field in PATH_FIELDS}
            for item in sorted(
                database.list_server_paths(host["id"]), key=lambda entry: entry["label"].casefold()
            )
        ]
        tunnels = [
            {field: item.get(field) for field in TUNNEL_FIELDS}
            for item in sorted(
                database.list_tunnels(host["id"]), key=lambda entry: entry["name"].casefold()
            )
        ]
        hosts.append(
            {
                "alias": host["alias"],
                "hostname": host["hostname"],
                "port": host["port"],
                "username": host["username"],
                "proxy_jumps": list(host["proxy_jumps"]),
                "tags": list(host["tags"]),
                "notes": host["notes"],
                "source": host["source"],
                "geo": host.get("geo"),
                # A label, never an id: ids name rows in *this* database, and the
                # secret itself lives in this machine's keychain.
                "credential": host.get("credential_label"),
                "paths": paths,
                "tunnels": tunnels,
            }
        )
    return {"format": FORMAT, "version": VERSION, "hosts": hosts}


def dump_document(document: dict[str, Any]) -> str:
    """Render the document with a stable layout so diffs stay readable."""
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write_document(document: dict[str, Any], destination: str | Path) -> Path:
    path = Path(destination).expanduser()
    if path.exists() and path.is_dir():
        raise ValidationError(f"export destination is a directory: {path}")
    if not path.parent.is_dir():
        raise ValidationError(f"export destination directory does not exist: {path.parent}")
    temporary = path.with_name(path.name + ".tmp")
    # The document holds no secrets, but it names hosts and users, so it is
    # written privately and only widened by the user if they choose to.
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(dump_document(document))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    os.replace(temporary, path)
    return path


def load_document(source: str | Path) -> dict[str, Any]:
    """Read and structurally validate an export file."""
    path = Path(source).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValidationError(f"export file does not exist: {path}") from exc
    except OSError as exc:
        raise ValidationError(f"cannot read export file {path}: {exc}") from exc
    return load_document_text(text, origin=str(path))


def load_document_text(text: str, *, origin: str = "standard input") -> dict[str, Any]:
    """Validate an export document that was already read (a pipe, a test)."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ValidationError(f"export file is not valid JSON: {origin}") from exc
    if not isinstance(document, dict):
        raise ValidationError("export file must contain a JSON object")
    unknown = sorted(set(document) - set(TOP_LEVEL_FIELDS) - {"servers"})
    if unknown:
        raise ValidationError(f"export file has unknown top-level fields: {', '.join(unknown)}")
    if document.get("format") != FORMAT:
        raise ValidationError(
            f"not a {FORMAT} file (found format={document.get('format')!r})"
        )
    version = document.get("version")
    if version != VERSION:
        raise ValidationError(
            f"export format version {version!r} is not supported (this build reads {VERSION})"
        )
    hosts = document.get("hosts", document.get("servers"))
    if not isinstance(hosts, list):
        raise ValidationError("export file must contain a 'hosts' array")
    seen: set[str] = set()
    for index, entry in enumerate(hosts):
        if not isinstance(entry, dict):
            raise ValidationError(f"host #{index + 1} is not an object")
        unknown = sorted(set(entry) - set(HOST_FIELDS) - {"paths", "tunnels"})
        if unknown:
            raise ValidationError(
                f"host #{index + 1} has unknown fields: {', '.join(unknown)}"
            )
        alias = validate_alias(str(entry.get("alias", "")))
        key = alias.casefold()
        if key in seen:
            raise ValidationError(f"export file lists the host {alias} twice")
        seen.add(key)
        if not str(entry.get("hostname", "")).strip():
            raise ValidationError(f"host {alias} has no hostname")
        if not str(entry.get("username", "")).strip():
            raise ValidationError(f"host {alias} has no username")
        for field in ("paths", "tunnels"):
            value = entry.get(field, [])
            if not isinstance(value, list):
                raise ValidationError(f"host {alias}: '{field}' must be an array")
            allowed = PATH_FIELDS if field == "paths" else TUNNEL_FIELDS
            for item in value:
                if not isinstance(item, dict):
                    raise ValidationError(f"host {alias}: each {field} entry must be an object")
                extra = sorted(set(item) - set(allowed))
                if extra:
                    raise ValidationError(
                        f"host {alias}: unknown {field} fields: {', '.join(extra)}"
                    )
    return {"format": FORMAT, "version": VERSION, "hosts": hosts}


def _host_payload(entry: dict[str, Any], credential_id: str | None) -> dict[str, Any]:
    """Only the fields the file actually states.

    A field the document omits is not the same as a field it sets to empty: a
    hand-written file with just an alias and a port must not erase the notes
    somebody wrote on this machine. Presence, not parsing, decides what is
    authoritative.
    """
    payload: dict[str, Any] = {
        "alias": entry["alias"],
        "hostname": entry["hostname"],
        "username": entry["username"],
    }
    for field in ("port", "proxy_jumps", "tags", "notes", "source"):
        if field in entry and entry[field] is not None:
            payload[field] = list(entry[field]) if field in {"proxy_jumps", "tags"} else entry[field]
    # geo is deliberately not part of the profile payload: a stored location can
    # carry source "geoip", which the ordinary profile write path refuses (only a
    # human may assert a manual location through it). It is applied afterwards
    # through the resolver's own entry point, so an export round-trips without
    # weakening that rule.
    if credential_id is not None:
        payload["credential_id"] = credential_id
    return payload


def _compare(current: dict[str, Any], payload: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """What actually differs between the stored host and the file's host."""
    changes: dict[str, Any] = {}
    for field, value in payload.items():
        if field in {"alias", "credential_id"}:
            continue
        if current.get(field) != value:
            changes[field] = value
    if "credential_id" in payload and current.get("credential_id") != payload["credential_id"]:
        changes["credential_id"] = payload["credential_id"]
    if entry.get("geo") is not None and current.get("geo") != entry["geo"]:
        changes["geo"] = entry["geo"]
    return changes


def _apply_geo(database: Database, alias: str, geo: dict[str, Any] | None) -> None:
    """Store a location through the resolver path, which understands both sources."""
    if geo is None:
        return
    database.set_server_geo(alias, geo, overwrite_manual=True)


def preview(document: dict[str, Any], database: Database) -> dict[str, Any]:
    """Decide, without writing anything, what applying this file would do."""
    existing = {host["alias"].casefold(): host for host in database.list_servers()}
    known_credentials = {
        credential["label"].casefold(): credential["id"]
        for credential in database.list_credentials()
    }
    existing_tunnels = {
        entry["name"].casefold(): entry for entry in database.list_tunnels()
    }
    actions: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for entry in document["hosts"]:
        alias = entry["alias"]
        label = entry.get("credential")
        credential_id = None
        if label:
            credential_id = known_credentials.get(str(label).casefold())
            if credential_id is None:
                warnings.append(
                    {
                        "alias": alias,
                        "message": (
                            f"credential '{label}' does not exist on this machine, so the host "
                            "is imported without one; create it before --apply "
                            f"(serverctl credential add-password '{label}', or add-key / add-agent) "
                            f"or attach it later with serverctl server edit {alias} --credential '{label}'"
                        ),
                    }
                )
        payload = _host_payload(entry, credential_id)
        current = existing.get(alias.casefold())
        if current is None:
            actions.append({"alias": alias, "action": "create", "host": payload})
        else:
            changes = _compare(current, payload, entry)
            if not changes:
                actions.append({"alias": alias, "action": "unchanged"})
            else:
                actions.append(
                    {
                        "alias": alias,
                        "action": "update",
                        # geo is written separately, so it must not travel in the
                        # column update where it would be refused.
                        "changes": {key: value for key, value in changes.items() if key != "geo"},
                        "geo": entry.get("geo"),
                        "host": payload,
                        "host_id": current["id"],
                    }
                )
        for tunnel in entry.get("tunnels", []) or []:
            name = str(tunnel.get("name") or "")
            clash = existing_tunnels.get(name.casefold())
            if clash is not None and clash["server"].casefold() != alias.casefold():
                warnings.append(
                    {
                        "alias": alias,
                        "message": (
                            f"tunnel name '{name}' is already used by host "
                            f"{clash['server']}; it is not imported"
                        ),
                    }
                )
    counts: dict[str, int] = {}
    for action in actions:
        counts[action["action"]] = counts.get(action["action"], 0) + 1
    return {
        "ok": True,
        "format": document["format"],
        "version": document["version"],
        "hosts": actions,
        "counts": counts,
        "warnings": warnings,
    }


def apply(
    document: dict[str, Any], database: Database, *, overwrite: bool = False
) -> dict[str, Any]:
    """Write the file's hosts, directories, and forwards into the inventory.

    Directories and tunnels are merged rather than replaced: an export file may
    omit something that exists here, and silently deleting a human's saved
    directory because it was missing from a file is not a recoverable surprise.
    """
    plan = preview(document, database)
    created: list[str] = []
    updated: list[str] = []
    # "skipped" is a conflict (it exists and --overwrite was not given);
    # "unchanged" means the file and the inventory already agree. Reporting them
    # separately keeps a repeated import from looking like a refusal.
    skipped: list[str] = []
    unchanged: list[str] = []
    errors: list[dict[str, str]] = []
    for action, entry in zip(plan["hosts"], document["hosts"]):
        alias = entry["alias"]
        try:
            if action["action"] == "create":
                database.create_server(**action["host"])
                created.append(database.get_server(alias)["alias"])
                _apply_geo(database, alias, entry.get("geo"))
            elif action["action"] == "update":
                if not overwrite:
                    skipped.append(alias)
                    continue
                database.update_server(action["host_id"], **action["changes"])
                _apply_geo(database, alias, entry.get("geo"))
                updated.append(alias)
            else:
                unchanged.append(alias)
        except (ValidationError, ConflictError, DatabaseError) as exc:
            errors.append({"alias": alias, "message": str(exc)})
            continue
        if action["action"] == "update" and not overwrite:
            continue
        try:
            _apply_children(database, alias, entry)
        except (ValidationError, ConflictError, DatabaseError) as exc:
            errors.append({"alias": alias, "message": str(exc)})
    return {
        "ok": not errors,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "unchanged": unchanged,
        "errors": errors,
        "warnings": plan["warnings"],
        "counts": plan["counts"],
    }


def _apply_children(database: Database, alias: str, entry: dict[str, Any]) -> None:
    server = database.get_server(alias)
    for item in entry.get("paths", []) or []:
        label = str(item.get("label") or "")
        if not label:
            raise ValidationError(f"host {alias}: a saved directory has no label")
        try:
            current = database.get_server_path(server["id"], label)
        except NotFoundError:
            database.create_server_path(
                server["id"],
                label=label,
                path=item.get("path"),
                notes=item.get("notes"),
            )
            continue
        if (
            current["path"] != item.get("path")
            or (current["notes"] or "") != (item.get("notes") or "")
        ):
            database.update_server_path(
                server["id"], current["id"], path=item.get("path"), notes=item.get("notes")
            )
    for item in entry.get("tunnels", []) or []:
        name = str(item.get("name") or "")
        if not name:
            raise ValidationError(f"host {alias}: a tunnel profile has no name")
        try:
            database.get_tunnel(name)
        except NotFoundError:
            database.create_tunnel(
                server["id"],
                name=name,
                kind=item.get("kind") or "local",
                bind_host=item.get("bind_host"),
                bind_port=item.get("bind_port"),
                remote_host=item.get("remote_host"),
                remote_port=item.get("remote_port"),
                notes=item.get("notes"),
            )
