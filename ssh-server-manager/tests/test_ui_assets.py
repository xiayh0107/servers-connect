from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest

from ssh_server_manager.ui_assets import read_manifest, resource_names

UI_DIR = Path(__file__).resolve().parents[1] / "scripts" / "ssh_server_manager" / "assets" / "ui"


def test_built_resources_are_complete_local_and_within_wire_budgets():
    manifest = read_manifest(UI_DIR)
    names = resource_names(UI_DIR)
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert '<div id="root"></div>' in html
    assert 'type="module"' in html
    assert not re.search(r'(?:src|href)=["\'](?:https?:)?//', html)
    assert not re.search(r"<script[^>]*>\s*[^<\s]", html)
    for name in names:
        assert re.search(r"-[\w-]{8,}\.", name), name
        if name.endswith(".css"):
            css = (UI_DIR / name).read_text(encoding="utf-8")
            assert "@import" not in css
            assert not re.search(r"url\(\s*[\"']?(?:https?:)?//", css)
    first = {"index.html"}
    visited = set()

    def entry(key):
        if key in visited:
            return
        visited.add(key)
        chunk = manifest[key]
        first.add(chunk["file"])
        first.update(chunk.get("css", []))
        for imported in chunk.get("imports", []):
            entry(imported)

    entry("index.html")
    gz = lambda name: len(gzip.compress((UI_DIR / name).read_bytes(), compresslevel=9, mtime=0))
    assert sum(map(gz, first)) <= 150 * 1024
    assert sum(gz(name) for name in names if name.endswith((".js", ".css"))) <= 250 * 1024
    assert len(manifest["index.html"].get("dynamicImports", [])) >= 4
    assert set(path.name for path in UI_DIR.iterdir()) == names | {"index.html", "asset-manifest.json"}


@pytest.mark.parametrize("resource", ["../outside.js", "..\\outside.js", "folder\\resource.js", "/outside.js", ".hidden.js", "source.ts", "config.json", "index.html"])
def test_manifest_cannot_expose_unintended_files(tmp_path, resource):
    (tmp_path / "asset-manifest.json").write_text(json.dumps({"index.html": {"file": resource}}))
    with pytest.raises(ValueError):
        resource_names(tmp_path)


def test_manifest_requires_every_declared_resource(tmp_path):
    (tmp_path / "asset-manifest.json").write_text(json.dumps({"index.html": {"file": "index-01234567.js"}}))
    with pytest.raises(ValueError, match="missing"):
        resource_names(tmp_path)
