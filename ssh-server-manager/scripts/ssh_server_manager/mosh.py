"""Mosh: roaming, latency-tolerant sessions over the same SSH bootstrap.

Mosh is not an alternative credential path. It starts `mosh-server` over ssh and
then speaks UDP to it, so the managed config, the askpass environment and
OpenSSH's host-key verification all still apply — which is exactly why it is safe
to offer and also why it can fail in ways ssh does not.

Nothing here installs mosh or guesses whether a remote has `mosh-server`: mosh
reports that itself. The capability check only answers "is the client here and
usable on this platform", so `serverctl doctor` can say something more useful
than "command not found" later.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from .db import Database
from .ssh_config import render_config
from .ssh_runner import SSHError, SSHRunner, redact


def mosh_capability() -> dict[str, Any]:
    """Report whether this machine can start a mosh client."""
    binary = shutil.which("mosh")
    if not binary:
        return {
            "ok": False,
            "path": None,
            "message": "mosh is not installed; install it from https://mosh.org and try again",
        }
    version = _mosh_version(binary)
    if os.name == "nt":
        # The Windows client is a wrapper around a bundled environment and does
        # not take this tool's -F config the same way; do not promise it works.
        return {
            "ok": False,
            "path": binary,
            "version": version,
            "message": "mosh on Windows is not supported by serverctl; use connect",
        }
    return {"ok": True, "path": binary, "version": version}


def _mosh_version(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (result.stdout or result.stderr or "").strip().splitlines()
    return text[0].strip() if text else None


def require_mosh() -> dict[str, Any]:
    capability = mosh_capability()
    if not capability["ok"]:
        raise SSHError(capability["message"])
    return capability


def mosh_command(
    database: Database,
    server: dict[str, Any],
    *,
    ssh_binary: str | None = None,
    mosh_binary: str | None = None,
) -> list[str]:
    """Build `mosh` so its ssh bootstrap is the same connection as `connect`.

    The ssh command is passed whole through `--ssh` because mosh re-parses it as
    a string. `StrictHostKeyChecking=yes` is repeated there rather than assumed:
    it travels through a second program now, and the rule does not become
    optional just because another binary is in the middle.
    """
    capability = mosh_capability()
    binary = mosh_binary or capability.get("path") or "mosh"
    runner = SSHRunner(database, ssh_binary=ssh_binary)
    config = render_config(database, ssh_binary=runner.ssh_binary, fallback=True)
    bootstrap = " ".join(
        [
            runner.ssh_binary,
            "-F",
            str(config),
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=no",
        ]
    )
    return [binary, "--ssh", bootstrap, server["alias"]]


def connect(database: Database, identifier: str, *, timeout: int = 12) -> int:
    """Run a mosh session in the terminal, reusing the managed identity."""
    require_mosh()
    server = database.get_server(identifier)
    command = mosh_command(database, server)
    environment = SSHRunner(database)._environment(server, host_key_confirmation=True)
    # mosh refuses a non-UTF-8 locale; a terminal without one would otherwise
    # fail with mosh's own wording rather than anything about this tool.
    if not any("UTF-8" in environment.get(name, "").upper() or "UTF8" in environment.get(name, "").upper()
               for name in ("LC_ALL", "LC_CTYPE", "LANG")):
        environment["LC_ALL"] = "en_US.UTF-8"
    try:
        return subprocess.call(command, env=environment)
    except FileNotFoundError as exc:
        raise SSHError(f"could not run mosh: {exc}") from exc
    except OSError as exc:
        raise SSHError(redact(str(exc))) from exc
