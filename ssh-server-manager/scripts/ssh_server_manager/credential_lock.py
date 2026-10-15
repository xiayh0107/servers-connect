"""One OS-account-private, cross-process lock per credential.

Lock files contain no credential values and are deliberately never unlinked:
unlinking a locked file would let another process lock a different inode.
"""
from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .paths import ensure_private_dir
from .validation import ValidationError

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_held = threading.local()


@contextmanager
def credential_lock(database_path: Path, identifier: str, *, timeout: float = 30) -> Iterator[None]:
    key = hashlib.sha256(str(database_path.resolve()).encode() + b"\0" + identifier.encode()).hexdigest()
    with _guard:
        local = _locks.setdefault(key, threading.RLock())
    with local:
        held = getattr(_held, "keys", set())
        if key in held:
            yield
            return
        directory = ensure_private_dir(database_path.parent / "credential-locks")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(directory / key, flags, 0o600)
        acquired = False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValidationError("credential lock is not a regular private file")
            if os.name != "nt":
                if info.st_uid != os.getuid():
                    raise ValidationError("credential lock belongs to another user")
                os.fchmod(fd, 0o600)
            if not info.st_size:
                os.write(fd, b"0")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    _held.keys = held | {key}
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise ValidationError("credential is busy; retry later") from None
                    time.sleep(0.025)
            yield
        finally:
            if acquired:
                _held.keys = held
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
