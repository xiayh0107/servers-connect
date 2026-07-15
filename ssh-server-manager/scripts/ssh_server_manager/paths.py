from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


APP_NAME = "ssh-server-manager"


def skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    override = os.environ.get("SSM_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    try:
        from platformdirs import user_data_path

        return Path(user_data_path(APP_NAME, appauthor=False))
    except ImportError:
        home = Path.home()
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / APP_NAME
        if os.name == "nt":
            return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / APP_NAME
        return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / APP_NAME


def ensure_private_dir(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"private data path is not a real directory: {path}")
        if os.name != "nt" and info.st_uid != os.getuid():
            raise RuntimeError(f"private data directory is owned by another user: {path}")
    else:
        path.mkdir(parents=True, mode=stat.S_IRWXU)
    if os.name != "nt":
        current_mode = stat.S_IMODE(path.stat().st_mode)
        if current_mode != stat.S_IRWXU:
            path.chmod(stat.S_IRWXU)
    return path


def database_path() -> Path:
    return data_dir() / "manager.db"


def runtime_dir() -> Path:
    override = os.environ.get("SSM_RUNTIME_DIR")
    if override:
        base = Path(override).expanduser()
    elif os.name == "nt":
        base = data_dir() / "runtime"
    else:
        # Keep this path short: OpenSSH limits Unix-domain ControlPath length.
        base = Path("/tmp") / f"ssm-{os.getuid()}"
    return ensure_private_dir(base.resolve())


def original_ssh_config_path() -> Path:
    override = os.environ.get("SSM_ORIGINAL_SSH_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".ssh" / "config"


def managed_ssh_config_path() -> Path:
    override = os.environ.get("SSM_MANAGED_SSH_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".ssh" / "ssh-server-manager.conf"


def asset_dir() -> Path:
    return Path(__file__).resolve().parent / "assets" / "ui"
