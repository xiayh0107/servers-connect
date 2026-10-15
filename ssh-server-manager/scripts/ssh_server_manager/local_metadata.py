"""Explicit local metadata actions, without exposing file contents."""
from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .validation import ValidationError


class LocalKeyBrowser:
    def __init__(self) -> None:
        self.scopes: dict[str, tuple[str, Path, float, tuple[int, int]]] = {}
        self.revisions: dict[str, tuple[str, str, str, float]] = {}
        self.lock = threading.Lock()

    def clean(self, active_sessions: set[str]) -> None:
        now = time.time()
        with self.lock:
            self.scopes = {key: item for key, item in self.scopes.items() if item[0] in active_sessions and item[2] > now}
            self.revisions = {key: item for key, item in self.revisions.items() if item[0] in active_sessions and item[3] > now}

    def scope(self, session_id: str, root_path: str | None = None) -> dict[str, Any]:
        value = root_path or str(Path.home() / ".ssh")
        if any(ord(char) < 32 for char in value):
            raise ValidationError("directory path is invalid")
        try:
            root = Path(value).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise ValidationError("choose a directory for the key browser")
            # Check enumerability only after the explicit user-selected scope.
            with os.scandir(root):
                pass
        except OSError:
            raise ValidationError("selected directory is unavailable or not readable") from None
        token = secrets.token_urlsafe(24)
        expires = time.time() + 300
        with self.lock:
            info = root.stat()
            self.scopes[token] = (session_id, root, expires, (info.st_dev, info.st_ino))
        return {"scope_id": token, "root_path": str(root), "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat()}

    def list(self, session_id: str, scope_id: str, path: str | None = None, offset: int = 0) -> dict[str, Any]:
        with self.lock:
            item = self.scopes.get(scope_id)
        if not item or item[0] != session_id or item[2] <= time.time():
            raise ValidationError("key-browser scope is missing or expired")
        root = item[1]
        try:
            target = (Path(path).expanduser() if path else root).resolve(strict=True)
            if not target.is_relative_to(root):
                raise ValidationError("directory is outside the selected key-browser scope")
            if not target.is_dir():
                raise ValidationError("key browser can only enumerate directories")
            # Never follow links or inspect private bytes while enumerating.
            entries = []
            truncated = False
            before = target.stat()
            with self._enumerate(root, target, item[3]) as iterator:
                for entry in iterator:
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            continue
                        kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else None
                        if kind:
                            entries.append({"name": entry.name, "path": str(target / entry.name), "kind": kind})
                    except OSError:
                        continue
                    if len(entries) >= 10000:
                        truncated = True
                        break
            after = target.stat()
            if (before.st_dev, before.st_ino, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_ctime_ns):
                raise ValidationError("directory changed while listing; choose the scope again")
            entries.sort(key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold()))
            return {"scope_id": scope_id, "root_path": str(root), "path": str(target),
                "parent": str(target.parent) if target != root else None,
                "entries": entries[offset:offset + 250], "has_more": len(entries) > offset + 250,
                "next_offset": offset + 250 if len(entries) > offset + 250 else None, "truncated": truncated}
        except (OSError, RuntimeError):
            raise ValidationError("directory is unavailable or not readable") from None

    @staticmethod
    @contextmanager
    def _enumerate(root: Path, target: Path, identity: tuple[int, int]):
        if os.name == "nt":
            # Windows cannot scandir an fd. Refuse links at every component
            # and verify directory identity again before returning any paths.
            paths = [root, *[root.joinpath(*target.relative_to(root).parts[:index])
                for index in range(1, len(target.relative_to(root).parts) + 1)]]
            infos = [path.lstat() for path in paths]
            if any(stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 for info in infos):
                raise ValidationError("key-browser directories must not be links or junctions")
            if (infos[0].st_dev, infos[0].st_ino) != identity:
                raise ValidationError("selected directory changed; choose the scope again")
            with os.scandir(target) as iterator:
                yield iterator
            for path, before in zip(paths, infos):
                after = path.lstat()
                if (before.st_dev, before.st_ino, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_ctime_ns):
                    raise ValidationError("key-browser directory changed; choose the scope again")
            return
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(root, flags)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != identity:
                raise ValidationError("selected directory changed; choose the scope again")
            for part in target.relative_to(root).parts:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            with os.scandir(fd) as iterator:
                yield iterator
        finally:
            os.close(fd)

    def revision(self, session_id: str, path: str, revision: str) -> str:
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.revisions[token] = (session_id, path, revision, time.time() + 120)
        return token

    def expected_revision(self, session_id: str, token: str | None, path: str) -> str | None:
        if token is None:
            return None
        with self.lock:
            item = self.revisions.get(token)
        if not item or item[0] != session_id or item[1] != path or item[3] <= time.time():
            raise ValidationError("key revision is missing, expired, or belongs to another path")
        return item[2]


def agent_metadata() -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc).isoformat()
    executable = shutil.which("ssh-add")
    result = {"status": "unavailable", "identities": [], "observed_at": observed_at, "truncated": False}
    if not executable:
        return result
    try:
        process = subprocess.run([executable, "-l", "-E", "sha256"], capture_output=True,
            stdin=subprocess.DEVNULL, timeout=3, check=False)
    except subprocess.TimeoutExpired:
        return {**result, "status": "timeout"}
    except OSError:
        return result
    output = process.stdout[:65536].decode("utf-8", errors="replace")
    if process.returncode == 1 and "no identities" in output.casefold():
        return {**result, "status": "empty"}
    if process.returncode != 0:
        return result
    entries = []
    pattern = re.compile(r"^(\d+)\s+(SHA256:[A-Za-z0-9+/]+)\s+(.*?)\s+\(([^)]+)\)$")
    for line in output.splitlines()[:128]:
        match = pattern.fullmatch(line)
        if match:
            comment = "".join(char for char in match[3] if ord(char) >= 32 and char != "\x7f")[:500]
            entries.append({"bits": int(match[1]), "fingerprint": match[2], "comment": comment, "algorithm": match[4][:50]})
    return {**result, "status": "ready" if entries else "unknown", "identities": entries,
        "truncated": len(process.stdout) > 65536 or len(output.splitlines()) > 128}
