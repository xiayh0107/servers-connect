#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    environment = root / ".venv"
    if not environment.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-e", f"{root}[test]"], check=True)
    print(f"Ready: {python}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

