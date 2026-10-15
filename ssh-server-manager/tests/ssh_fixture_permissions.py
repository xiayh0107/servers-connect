"""Private permissions for newly created, disposable SSH fixture files only."""
from __future__ import annotations

import csv
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _windows_user_sid() -> str:
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    rows = list(csv.reader(result.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) != 2 or not re.fullmatch(r"S-\d+(?:-\d+)+", rows[0][1]):
        raise RuntimeError("Cannot determine the fixture owner's Windows SID")
    return rows[0][1]


def private_ssh_fixture_file(path: Path) -> None:
    """Remove inherited grants, including Python 3.13's OWNER RIGHTS ACE.

    Windows OpenSSH rejects that ACE even on private tempfile directories.
    These files are freshly created test state: retain only the current user's
    explicit grant. Never use this helper on a developer's SSH config or keys.
    """
    if os.name != "nt":
        path.chmod(0o600)
        return
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{_windows_user_sid()}:F",
         "/remove:g", "*S-1-3-4", "/Q"],
        capture_output=True, check=True, timeout=10,
    )
