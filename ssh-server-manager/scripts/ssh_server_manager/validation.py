from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Iterable


ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
KINDS = {"password", "key", "agent"}


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

