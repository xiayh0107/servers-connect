from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Iterable


ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
KINDS = {"password", "key", "agent"}
MAX_REMOTE_PATH_LENGTH = 4096
MAX_SKILL_DESCRIPTION_LENGTH = 4096
MAX_SERVER_TAGS = 20
MAX_SERVER_TAG_LENGTH = 40
MAX_SERVER_NOTE_LENGTH = 10_000
NOTE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
CASE_INSENSITIVE_SKILL_FILENAMES = os.name == "nt"


class ValidationError(ValueError):
    pass


def validate_alias(value: str) -> str:
    value = value.strip()
    if not ALIAS_RE.fullmatch(value):
        raise ValidationError("alias must be 1-63 characters using letters, digits, '.', '_' or '-'")
    return value


def validate_hostname(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 253 or CONTROL_RE.search(value) or any(c.isspace() for c in value):
        raise ValidationError("hostname must be a non-empty DNS name or IP address without whitespace")
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        labels = value.rstrip(".").split(".")
        if any(not label or len(label) > 63 for label in labels):
            raise ValidationError("invalid hostname")
    return value


def validate_port(value: int | str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValidationError("port must be between 1 and 65535")
    return port


def validate_username(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 128 or CONTROL_RE.search(value) or any(c.isspace() for c in value):
        raise ValidationError("username must not be empty or contain whitespace")
    return value


def validate_label(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 100 or CONTROL_RE.search(value):
        raise ValidationError("credential label must be 1-100 printable characters")
    return value


def validate_skill_name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 100 or not value.isprintable() or "/" in value or "\\" in value:
        raise ValidationError("skill name must be 1-100 printable characters without '/' or '\\'")
    return value


def skill_name_key(value: str) -> str:
    """Return the persisted, Unicode-aware key used for Skill identity."""
    return validate_skill_name(value).casefold()


def is_skill_manifest_name(value: str) -> bool:
    """Match the manifest filename using the current platform's path rules."""
    return (
        value.casefold() == "skill.md"
        if CASE_INSENSITIVE_SKILL_FILENAMES
        else value == "SKILL.md"
    )


def validate_skill_path(value: str | Path) -> str:
    raw_value = str(value).strip()
    if not raw_value or len(raw_value) > MAX_REMOTE_PATH_LENGTH or CONTROL_RE.search(raw_value):
        raise ValidationError("skill manifest path must be a valid path to SKILL.md")
    path = Path(raw_value).expanduser()
    try:
        if not path.is_absolute():
            path = path.resolve()
        else:
            path = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValidationError("skill manifest path cannot be resolved") from exc
    if not is_skill_manifest_name(path.name):
        raise ValidationError("skill manifest path must point to SKILL.md")
    # Preserve the resolved spelling.  normcase() is suitable for comparison
    # keys, but lowercases paths on Windows and can break NTFS directories that
    # opt into per-directory case sensitivity.
    return str(path)


def validate_skill_description(value: str) -> str:
    description = str(value).strip()
    if not description or len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
        raise ValidationError(
            f"skill description must be 1-{MAX_SKILL_DESCRIPTION_LENGTH} characters"
        )
    if NOTE_CONTROL_RE.search(description):
        raise ValidationError("skill description must not contain control characters")
    return description


def validate_kind(value: str) -> str:
    if value not in KINDS:
        raise ValidationError(f"credential kind must be one of {', '.join(sorted(KINDS))}")
    return value


def validate_key_path(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    if not path.exists() or not path.is_file():
        raise ValidationError(f"private key does not exist: {path}")
    return str(path)


def validate_remote_path(value: str | None) -> str:
    """Validate a path before it is placed in an SFTP command stream.

    Remote paths are deliberately not stripped: leading and trailing spaces
    are valid filename characters. Empty input means the remote home directory.
    """
    if value is None or value == "":
        return "~"
    if len(value) > MAX_REMOTE_PATH_LENGTH:
        raise ValidationError(f"remote path must be at most {MAX_REMOTE_PATH_LENGTH} characters")
    if CONTROL_RE.search(value):
        raise ValidationError("remote path must not contain control characters")
    return value


def validate_server_tags(values: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values or []:
        value = str(raw_value).strip()
        if not value or len(value) > MAX_SERVER_TAG_LENGTH or CONTROL_RE.search(value) or "," in value:
            raise ValidationError(
                f"server tags must be 1-{MAX_SERVER_TAG_LENGTH} printable characters without commas"
            )
        key = value.casefold()
        if key not in seen:
            normalized.append(value)
            seen.add(key)
        if len(normalized) > MAX_SERVER_TAGS:
            raise ValidationError(f"a server can have at most {MAX_SERVER_TAGS} tags")
    return normalized


def validate_server_note(value: str | None) -> str:
    """Normalize one server note while allowing human-readable line breaks."""
    normalized = str(value or "").strip()
    if len(normalized) > MAX_SERVER_NOTE_LENGTH:
        raise ValidationError(f"server notes must be at most {MAX_SERVER_NOTE_LENGTH} characters")
    if NOTE_CONTROL_RE.search(normalized):
        raise ValidationError("server notes must not contain control characters")
    return normalized


def validate_proxy_jumps(alias: str, jumps: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for jump in jumps:
        item = validate_alias(jump)
        key = item.casefold()
        if key == alias.casefold():
            raise ValidationError("a server cannot ProxyJump through itself")
        if key not in seen:
            normalized.append(item)
            seen.add(key)
    return normalized
