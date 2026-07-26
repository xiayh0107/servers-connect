"""Mount a managed host's directory locally through sshfs.

sshfs is the one capability here that depends on a FUSE stack we cannot ship,
install, or test in CI: macFUSE on macOS, libfuse on Linux, WinFsp on Windows.
So the design is deliberately defensive — detect the backend first, report
exactly what is missing, and never guess.

Credentials are not handled separately. sshfs runs ssh underneath, so passing
the managed config plus the same askpass environment the rest of the tool uses
means a mount authenticates exactly like `serverctl connect` does.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .db import Database
from .ssh_config import render_config
from .ssh_runner import SSHError, SSHRunner, redact
from .validation import ValidationError, validate_alias, validate_remote_path


# Filesystem types the OS reports for an sshfs mount, per platform.
_MOUNT_TYPES = ("fuse.sshfs", "macfuse", "osxfuse", "sshfs")
_MOUNT_LINE_RE = re.compile(r"^(?P<source>.+?) on (?P<target>.+?) \((?P<options>.*)\)$")


# os.uname() does not exist on Windows, so platform checks go through these.
def _is_windows() -> bool:
    return os.name == "nt"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _macfuse_installed() -> bool:
    return any(
        Path(candidate).exists()
        for candidate in (
            "/Library/Filesystems/macfuse.fs",
            "/Library/Filesystems/osxfuse.fs",
        )
    )


def _windows_sshfs() -> str | None:
    found = shutil.which("sshfs") or shutil.which("sshfs-win")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\SSHFS-Win\bin\sshfs.exe",
        r"C:\Program Files (x86)\SSHFS-Win\bin\sshfs.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def mount_capability() -> dict[str, Any]:
    """Report whether this machine can mount, and what is missing if it cannot."""
    if _is_windows():
        binary = _windows_sshfs()
        if not binary:
            return {
                "ok": False,
                "backend": None,
                "message": "install SSHFS-Win and WinFsp from https://github.com/winfsp/sshfs-win",
            }
        return {"ok": True, "backend": "sshfs-win", "path": binary}

    binary = shutil.which("sshfs")
    if _is_macos():
        if not _macfuse_installed():
            return {
                "ok": False,
                "backend": None,
                "path": binary,
                # Being specific matters: people routinely install sshfs via brew
                # and then cannot work out why nothing mounts.
                "message": "macFUSE is not installed; sshfs on macOS needs it (https://macfuse.io)",
            }
        if not binary:
            return {
                "ok": False,
                "backend": None,
                "message": "sshfs is not installed; try `brew install gromgit/fuse/sshfs-mac`",
            }
        return {"ok": True, "backend": "sshfs", "path": binary}

    if not binary:
        return {
            "ok": False,
            "backend": None,
            "message": "sshfs is not installed; install it with your package manager",
        }
    unmount = shutil.which("fusermount3") or shutil.which("fusermount")
    if not unmount:
        return {
            "ok": False,
            "backend": "sshfs",
            "path": binary,
            "message": "fusermount is missing; install the FUSE userspace tools",
        }
    return {"ok": True, "backend": "sshfs", "path": binary, "unmount": unmount}


def _require_backend() -> dict[str, Any]:
    capability = mount_capability()
    if not capability["ok"]:
        raise SSHError(capability["message"])
    return capability


def list_mounts() -> list[dict[str, str]]:
    """Read sshfs mounts from the OS.

    Deliberately not tracked in the database: a user can unmount in Finder or
    with umount at any time, and a stored list would then be confidently wrong.
    """
    if _is_windows():
        return _list_windows_mounts()
    try:
        result = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    mounts: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        match = _MOUNT_LINE_RE.match(line.strip())
        if not match:
            continue
        options = match.group("options")
        if not any(kind in options for kind in _MOUNT_TYPES):
            continue
        mounts.append(
            {
                "source": match.group("source"),
                "mountpoint": match.group("target"),
                "type": next(kind for kind in _MOUNT_TYPES if kind in options),
            }
        )
    return mounts


def _list_windows_mounts() -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            ["net", "use"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    mounts = []
    for line in result.stdout.splitlines():
        if "\\sshfs" not in line.casefold():
            continue
        parts = line.split()
        drive = next((part for part in parts if len(part) == 2 and part.endswith(":")), "")
        source = next((part for part in parts if part.startswith("\\\\")), "")
        if drive:
            mounts.append({"source": source, "mountpoint": drive, "type": "sshfs-win"})
    return mounts


def _prepare_mountpoint(mountpoint: str | None, alias: str) -> Path:
    target = Path(mountpoint).expanduser() if mountpoint else Path.home() / "mnt" / alias
    target = target.resolve(strict=False)
    if _is_windows() and len(str(target)) == 3 and str(target)[1] == ":":
        return target  # a drive letter is assigned, not created
    if target.exists():
        if not target.is_dir():
            raise ValidationError(f"mount point is not a directory: {target}")
        if any(target.iterdir()):
            raise ValidationError(f"mount point is not empty: {target}")
    else:
        target.mkdir(parents=True, exist_ok=True)
    return target


def mount(
    database: Database,
    identifier: str,
    remote_path: str | None = None,
    mountpoint: str | None = None,
    *,
    read_only: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """Mount one remote directory locally, reusing the managed SSH identity."""
    capability = _require_backend()
    remote = validate_remote_path(remote_path)
    server = database.get_server(identifier)
    alias = validate_alias(server["alias"])
    target = _prepare_mountpoint(mountpoint, alias)

    config = render_config(database)
    # "alias:" with an empty path means the login directory, which is what "~"
    # means everywhere else in this tool.
    source = f"{alias}:{'' if remote == '~' else remote}"
    options = [
        "reconnect",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=3",
        "StrictHostKeyChecking=yes",
        # Without this a dropped link leaves processes blocked in uninterruptible
        # I/O and the mount point cannot even be unmounted.
        "ConnectTimeout=%d" % timeout,
    ]
    if read_only:
        options.append("ro")
    if _is_macos():
        options.append(f"volname={alias}")
    command = [capability["path"], source, str(target), "-F", str(config)]
    for option in options:
        command.extend(["-o", option])

    runner = SSHRunner(database)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # The same askpass map the rest of the tool uses, so a vault password
            # or key passphrase reaches sshfs's ssh without a second mechanism.
            env=runner._environment(server),
            timeout=timeout + 15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SSHError(f"mount did not complete within {timeout + 15} seconds") from exc
    except OSError as exc:
        raise SSHError(f"could not run {capability['path']}") from exc
    if result.returncode != 0:
        raise SSHError(redact(result.stderr.strip()) or f"sshfs exited with status {result.returncode}")
    return {
        "alias": alias,
        "source": source,
        "mountpoint": str(target),
        "read_only": read_only,
        "backend": capability["backend"],
    }


def unmount(target: str) -> dict[str, Any]:
    """Unmount by mount point or by the alias whose default mount point it is."""
    candidate = Path(target).expanduser()
    if not candidate.exists() and "/" not in target and "\\" not in target:
        candidate = Path.home() / "mnt" / target
    resolved = str(candidate.resolve(strict=False))
    known = {item["mountpoint"] for item in list_mounts()}
    if resolved not in known and target not in known:
        raise ValidationError(f"not a mounted directory: {resolved}")

    if _is_windows():
        command = ["net", "use", target, "/delete", "/y"]
    elif _is_macos():
        command = ["umount", resolved]
    else:
        command = [shutil.which("fusermount3") or "fusermount", "-u", resolved]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SSHError(f"could not run {command[0]}") from exc
    if result.returncode != 0:
        raise SSHError(result.stderr.strip() or f"{command[0]} exited with status {result.returncode}")
    return {"mountpoint": resolved, "unmounted": True}
