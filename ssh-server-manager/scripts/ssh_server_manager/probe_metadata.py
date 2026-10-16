"""Best-effort observations made only by an explicit SSH check.

DNS and local OpenSSH metadata commands share a short deadline. Public-key
material is discarded after fingerprinting; this module never fetches keys or
changes known_hosts. Missing tools/configuration leave the observation unknown.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .host_metadata import observation_time
from .process_args import literal_config_args
from .ssh_config import split_config_line


_RESOLVE_SCRIPT = (
    "import socket,sys; "
    "addresses=socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM); "
    "print(addresses[0][4][0] if addresses else '')"
)
_OBSERVED_KEY = re.compile(r"^debug\d+: Server host key: \S+ (SHA256:[A-Za-z0-9+/]{43})=?$", re.MULTILINE)
_ACCEPTED_KEY_FILE = re.compile(r"^debug\d+: Found key in (.+):\d+$", re.MULTILINE)


def connection_diagnostics(stderr: str) -> str:
    """Keep user-facing SSH failures free of the extra local debug metadata."""
    return "\n".join(line for line in stderr.splitlines() if not re.match(r"^debug\d+:", line)).strip()


def _run(command: list[str], deadline: float) -> subprocess.CompletedProcess[str] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        return subprocess.run(
            literal_config_args(command), text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=remaining, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def resolve_configured_ip(hostname: str, port: int, *, deadline: float) -> str | None:
    """Resolve locally in a killable process; getaddrinfo has no timeout API."""
    try:
        return str(ipaddress.ip_address(hostname.strip("[]")))
    except ValueError:
        pass
    result = _run([sys.executable, "-c", _RESOLVE_SCRIPT, hostname, str(port)], deadline)
    if result is None or result.returncode:
        return None
    try:
        return str(ipaddress.ip_address(result.stdout.strip()))
    except ValueError:
        return None


def known_key_fingerprints(output: str) -> set[str]:
    """Hash actual ordinary known_hosts keys, excluding CA/revocation records."""
    fingerprints = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0].startswith(("#", "@")):
            continue
        key_type, encoded = parts[1:3]
        try:
            blob = base64.b64decode(encoded, validate=True)
            size = int.from_bytes(blob[:4], "big")
            # Validate the key blob's embedded algorithm before treating the
            # text as a public key. Certificate/CA records are not host keys.
            if size == 0 or size > 128 or len(blob) <= 4 + size:
                continue
            if blob[4:4 + size].decode("ascii") != key_type or "-cert-" in key_type:
                continue
        except (ValueError, UnicodeError, binascii.Error):
            continue
        digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
        fingerprints.add(f"SHA256:{digest}")
    return fingerprints


def read_known_fingerprints(
    server: dict[str, Any], *, ssh_binary: str, config: str, deadline: float, stderr: str = "",
) -> set[str]:
    """Ask OpenSSH for effective local paths, including hashed-host lookups."""
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        return set()
    effective = _run([ssh_binary, "-G", "-F", config, server["alias"]], deadline)
    if effective is None or effective.returncode:
        return set()
    values: dict[str, list[str]] = {}
    for line in effective.stdout.splitlines():
        parts = split_config_line(line)
        if parts:
            values[parts[0].casefold()] = parts[1:]
    alias = values.get("hostkeyalias", [])
    hostname_values = values.get("hostname") or [server["hostname"]]
    hostname = hostname_values[0]
    host = alias[0] if alias and alias[0] != "none" else hostname
    # HostKeyAlias is already the lookup name, including its user-selected port
    # convention. Otherwise OpenSSH brackets nonstandard-port hostnames.
    lookup = host if alias and alias[0] != "none" or server["port"] == 22 else f"[{host}]:{server['port']}"
    fingerprints = set()
    # ssh -G prints path arrays without quoting, so paths containing whitespace
    # cannot always be separated. Successful SSH debug output identifies the
    # actual accepted file unambiguously; prefer that ordinary-key evidence.
    paths = _ACCEPTED_KEY_FILE.findall(stderr) or (values.get("userknownhostsfile", []) + values.get("globalknownhostsfile", []))
    for value in dict.fromkeys(paths):
        if value == "none" or "%" in value:
            continue
        try:
            path = Path(value).expanduser()
        except (OSError, RuntimeError):
            continue
        # Let the timed child handle missing files and slow filesystem mounts;
        # a parent-side stat could otherwise escape the metadata deadline.
        found = _run([keygen, "-F", lookup, "-f", str(path)], deadline)
        if found is not None and found.returncode == 0:
            fingerprints.update(known_key_fingerprints(found.stdout))
    return fingerprints


def collect_probe_metadata(
    server: dict[str, Any], *, ssh_binary: str, config: str,
    success: bool, stderr: str, timeout: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, min(float(timeout), 2.0))
    observed_at = observation_time()
    resolved = resolve_configured_ip(server["hostname"], server["port"], deadline=deadline)
    known = read_known_fingerprints(server, ssh_binary=ssh_binary, config=config, deadline=deadline, stderr=stderr)
    match = _OBSERVED_KEY.search(stderr)
    selected = match.group(1) if match and match.group(1) in known else None
    # Older/fake SSH clients may omit debug metadata. A single ordinary saved
    # key is unambiguous; several keys without negotiation evidence are not.
    if selected is None and len(known) == 1:
        selected = next(iter(known))
    lowered = stderr.casefold()
    state = "unknown"
    if "revoked host key" in lowered:
        state = "revoked"
    elif "remote host identification has changed" in lowered:
        state = "changed"
    elif success and match and match.group(1) in known:
        state = "verified"
    return {
        "resolved_ip": resolved,
        "host_key": {"fingerprint": selected, "state": state, "observed_at": observed_at},
        "observed_at": observed_at,
    }
