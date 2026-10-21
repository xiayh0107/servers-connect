"""Report a managed-config directory the current process may not write to.

Listing a remote directory, testing a host or transferring a file all render
the managed OpenSSH config first, because every OpenSSH invocation points
``-F`` at it. When that render fails the operation never reaches the host, so
the failure must be reported as a local problem rather than as a generic HTTP
500 per request.

On macOS, TCC can deny reads of ``~/Library`` and writes to protected folders
such as ``~/.ssh`` for processes the user did not approve, even when the POSIX
mode allows them. The UI cannot render its managed config in that case; the
CLI it launched keeps running with the access it was granted.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .paths import cache_dir
from .ssh_config import SSHConfigError

# The permission belongs to the application that runs the command, not to
# serverctl or to the shell: macOS attaches it to the GUI app (or the agent
# host, e.g. ChatGPT/Codex, Claude, an IDE) that ultimately starts the process.
UI_WRITE_NOTE = (
    "this app is not allowed to write there; run it from an app with Full Disk "
    "Access (macOS Settings › Privacy & Security), or use the CLI in your own "
    "terminal"
)


# Remembered per destination: once a fallback location works, later renders go
# straight there instead of failing against the denied one first.
_fallback: dict[Path, Path] = {}


def fallback_managed_config(destination: Path, error: Exception) -> Path:
    """Where this process can render its managed config instead of `destination`.

    Every OpenSSH invocation points at one config file, so a host action asked
    to render into a directory this app may not write still needs a usable file
    somewhere private. Callers cite this path (with the denied one) so the next
    step is not another opaque failure.
    """
    remembered = _fallback.get(destination)
    if remembered is not None and directory_write_problem(remembered.parent) is None:
        return remembered
    stem = "".join(ch if ch.isalnum() else "-" for ch in destination.parent.name).strip("-")
    candidate = cache_dir() / f"ssh-{stem or 'local'}.conf"
    if directory_write_problem(candidate.parent) is not None:
        raise config_location_error(error)
    _fallback[destination] = candidate
    return candidate


def config_location_error(error: Exception) -> SSHConfigError:
    """Wrap a failed managed-config write with the destination it targeted."""
    if isinstance(error, PermissionError):
        # The failing path is usually the mkstemp temporary inside the
        # destination directory, which names the directory without the noise.
        location = Path(getattr(error, "filename", "") or "")
        where = f" {location.parent}" if location.parent != Path(".") else ""
        return SSHConfigError(
            f"cannot write the managed SSH config directory{where}: {UI_WRITE_NOTE}"
        )
    location = getattr(error, "filename", None) if isinstance(error, OSError) else None
    detail = (getattr(error, "strerror", None) or str(error)) if isinstance(error, OSError) else str(error)
    return SSHConfigError(f"cannot render the managed SSH config{f' {location}' if location else ''}: {detail}")


def database_write_problem(database_path: Path) -> str | None:
    """Report a host database this app can read but not write.

    Every host action records its outcome, and SQLite needs the directory for
    its journal, so a writable file alone is not enough.
    """
    directory = Path(database_path).parent
    if _can_create_file(directory):
        return None
    return f"cannot record host results in {directory}: {UI_WRITE_NOTE}"


def directory_write_problem(directory: Path) -> SSHConfigError | None:
    """Prove the managed-config directory is writable before rendering into it."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return SSHConfigError(
            f"cannot create the managed SSH config directory {directory}: {UI_WRITE_NOTE}"
        )
    except OSError as exc:
        return SSHConfigError(f"cannot create the managed SSH config directory {directory}: {exc}")
    if not _can_create_file(directory):
        return SSHConfigError(
            f"cannot write the managed SSH config directory {directory}: {UI_WRITE_NOTE}"
        )
    return None


def _can_create_file(directory: Path) -> bool:
    # Probe creation rather than inspecting mode bits: ACLs and platform
    # privacy controls can deny an operation the mode alone would allow.
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".write-probe-", dir=directory)
        temporary = Path(name)
        os.close(descriptor)
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
