"""Resolve only local, fingerprinted resources declared by the Vite manifest."""
from __future__ import annotations

import json
from pathlib import Path

MANIFEST = "asset-manifest.json"
RESOURCE_SUFFIXES = {".js", ".css", ".woff", ".woff2", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico"}


def read_manifest(directory: Path) -> dict:
    value = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or "index.html" not in value:
        raise ValueError("UI build manifest is invalid")
    return value


def resource_names(directory: Path) -> frozenset[str]:
    result: set[str] = set()
    for entry in read_manifest(directory).values():
        if not isinstance(entry, dict):
            raise ValueError("UI build manifest entry is invalid")
        names = [entry.get("file"), *entry.get("css", []), *entry.get("assets", [])]
        for name in names:
            if not isinstance(name, str) or not name or Path(name).name != name or name.startswith(".") or "\\" in name:
                raise ValueError("UI resource path is invalid")
            if Path(name).suffix not in RESOURCE_SUFFIXES or (directory / name).resolve().parent != directory.resolve():
                raise ValueError("UI resource type is invalid")
            if not (directory / name).is_file():
                raise ValueError("UI build resource is missing")
            result.add(name)
    return frozenset(result)
