"""Local, append-only record of privileged actions.

Two things are worth remembering after the fact: that a stored secret was
revealed, and which commands were run on which host. Both are audited by the
person who owns the machine, and neither may become a second place a secret
lives, so this log records *what happened* and never a value.

Storage is one JSON object per line in a mode-600 file under the private data
directory, appended with a single O_APPEND write. That choice is deliberate:

* the format cannot be silently edited by an application-level UPDATE, which is
  what "append-only" has to mean for a record anyone might be asked to trust;
* a single small `write()` on an O_APPEND descriptor cannot interleave with
  another process on POSIX, so parallel CLI calls do not corrupt lines;
* it stays readable even if the database is gone, which is exactly the case a
  log is for.

Losing an event is the lesser evil compared with breaking the privileged action,
so a failed write warns once on stderr and the command continues.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import ensure_private_dir, data_dir

AUDIT_FILE = "audit.log"
DEFAULT_LIMIT = 50
MAX_LIMIT = 10_000
# Rotation keeps the log append-only in spirit while bounding its size: the live
# file becomes .1, older segments shift up, and the oldest is dropped. Reading
# always includes every segment, so rotating never hides history.
ROTATED_SEGMENTS = 3
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
# A line longer than this is not something this module wrote; refuse to extend it.
MAX_EVENT_BYTES = 16 * 1024

# Command lines are user-controlled text that routinely carries credentials
# (`--password=hunter2`, `MYSQL_PWD=...`, `Authorization: Bearer ...`). An audit
# log that stores those verbatim would leak exactly what it is meant to watch, so
# the obvious shapes are masked before the event is written.
_SECRET_FLAG_RE = re.compile(
    r"(?i)(--?(?:password|passwd|pass|token|secret|api[-_]?key|auth(?:orization)?|"
    r"access[-_]?key|private[-_]?key|passphrase)(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:[A-Z0-9]+_)*(?:PASSWORD|PASSWD|TOKEN|SECRET|API_KEY|APIKEY|PASS|"
    r"PRIVATE_KEY|PASSPHRASE|CREDENTIALS?)=)(\"[^\"]*\"|'[^']*'|\S+)"
)
# `MYSQL_PWD`, `REDIS_PWD` and friends hold secrets; a bare `PWD` is just the
# working directory, so the name has to carry a prefix to match here.
_PWD_ASSIGNMENT_RE = re.compile(r"(?i)\b([A-Z0-9]+_PWD=)(\"[^\"]*\"|'[^']*'|\S+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})")
_REDACTED = "***"

_warned = False


def audit_path() -> Path:
    return data_dir() / AUDIT_FILE


def max_bytes() -> int:
    override = os.environ.get("SSM_AUDIT_MAX_BYTES")
    if override:
        try:
            value = int(override)
        except ValueError:
            return DEFAULT_MAX_BYTES
        if value > 0:
            return value
    return DEFAULT_MAX_BYTES


def log_paths() -> list[Path]:
    """Every segment, oldest first, ending with the live file."""
    base = data_dir()
    rotated = [base / f"{AUDIT_FILE}.{index}" for index in range(ROTATED_SEGMENTS, 0, -1)]
    return [*rotated, base / AUDIT_FILE]


def rotate(*, force: bool = False) -> dict[str, Any]:
    """Shift the live file into .1 when it is over the limit."""
    path = audit_path()
    try:
        size = path.stat().st_size
    except OSError:
        return {"rotated": False, "reason": "no log yet", "max_bytes": max_bytes()}
    limit = max_bytes()
    if not force and size < limit:
        return {"rotated": False, "reason": "under the limit", "bytes": size, "max_bytes": limit}
    base = data_dir()
    try:
        oldest = base / f"{AUDIT_FILE}.{ROTATED_SEGMENTS}"
        if oldest.exists():
            oldest.unlink()
        for index in range(ROTATED_SEGMENTS - 1, 0, -1):
            source = base / f"{AUDIT_FILE}.{index}"
            if source.exists():
                os.replace(source, base / f"{AUDIT_FILE}.{index + 1}")
        os.replace(path, base / f"{AUDIT_FILE}.1")
    except OSError as exc:
        # A rotation that fails must not stop the event being written.
        _warn_once(f"could not rotate the audit log: {exc}")
        return {"rotated": False, "reason": str(exc), "bytes": size, "max_bytes": limit}
    return {"rotated": True, "bytes": size, "max_bytes": limit, "segments": ROTATED_SEGMENTS}


def _warn_once(message: str) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    print(f"warning: {message}", file=sys.stderr)


def redact_command(text: str) -> str:
    """Mask values that look like credentials inside a command line."""
    masked = _SECRET_FLAG_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", text)
    masked = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", masked)
    masked = _PWD_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", masked)
    masked = _BEARER_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", masked)
    return masked


def record(event: str, *, actor: str = "cli", **fields: Any) -> dict[str, Any] | None:
    """Append one event. Returns the event, or None when it could not be stored.

    Every field is reduced to something JSON- and human-readable; a caller that
    passes a secret by accident still cannot store it as one, because values of
    keys named like secrets are dropped rather than trusted.
    """
    entry: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": str(event),
        "actor": actor,
    }
    for key, value in fields.items():
        if value is None:
            continue
        if key.lower() in {"value", "secret", "password", "passphrase", "token"}:
            entry[key] = _REDACTED
            continue
        if key == "command":
            # A sequence is joined into the line a human would type before
            # redaction: the space-separated forms (`--password hunter2`,
            # `MYSQL_PWD=...`) are the ones that carry secrets, and a Python
            # repr would hide them from the masks.
            if isinstance(value, (list, tuple)):
                entry[key] = redact_command(" ".join(str(item) for item in value))
            else:
                entry[key] = redact_command(str(value))
            continue
        if isinstance(value, (str, int, float, bool)):
            entry[key] = value
        else:
            entry[key] = str(value)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=False) + "\n"
    encoded = line.encode("utf-8", "replace")
    if len(encoded) > MAX_EVENT_BYTES:
        encoded = (
            json.dumps(
                {
                    "at": entry["at"],
                    "event": entry["event"],
                    "actor": entry["actor"],
                    "truncated": True,
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    try:
        directory = ensure_private_dir(data_dir())
        # Rotate before appending, never after: an event must land in the live
        # file, not in the segment that is about to be renamed away.
        try:
            if (directory / AUDIT_FILE).stat().st_size >= max_bytes():
                rotate()
        except OSError:
            pass
        descriptor = os.open(
            str(directory / AUDIT_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            os.write(descriptor, encoded)
        finally:
            os.close(descriptor)
    except OSError as exc:
        _warn_once(f"could not append to the audit log at {audit_path()}: {exc}")
        return None
    return entry


def read_events(
    *,
    limit: int = DEFAULT_LIMIT,
    alias: str | None = None,
    events: Iterable[str] | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Read the log newest-first, skipping lines that are not valid events."""
    chunks: list[str] = []
    for path in log_paths():
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OSError(f"cannot read the audit log at {path}: {exc}") from exc
    text = "".join(chunks)
    wanted = {str(item).casefold() for item in events} if events else None
    alias_key = alias.casefold() if alias else None
    collected: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # A torn or foreign line must not hide the rest of the log.
        if not isinstance(entry, dict) or "event" not in entry:
            continue
        if wanted is not None and str(entry.get("event", "")).casefold() not in wanted:
            continue
        if alias_key is not None and str(entry.get("alias", "")).casefold() != alias_key:
            continue
        if since is not None and str(entry.get("at", "")) < since:
            continue
        collected.append(entry)
    collected.reverse()
    return collected[: max(1, min(int(limit), MAX_LIMIT))]
