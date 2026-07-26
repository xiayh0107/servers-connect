"""The published site is generated, so the generator is what CI must check."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "website" / "build.py"

pytest.importorskip("markdown", reason="website/build.py needs the markdown package")


def _load_build():
    spec = importlib.util.spec_from_file_location("website_build", BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    _load_build().build(out)
    return out


def test_every_declared_doc_page_is_rendered(site):
    build = _load_build()
    for page in build.PAGES:
        assert page.source.is_file(), page.source
        rendered = site / "docs" / page.out_name
        assert rendered.is_file(), page.out_name
        assert "<h1" in rendered.read_text(encoding="utf-8")
    assert (site / "docs" / "index.html").is_file()
    assert (site / "docs" / "docs.css").is_file()


def test_landing_page_is_wrapped_exactly_once(site):
    html = (site / "index.html").read_text(encoding="utf-8")

    assert html.lower().count("<!doctype") == 1
    assert len(re.findall(r"<html\b", html)) == 1
    assert len(re.findall(r"<body\b", html)) == 1
    # The authored file stays headless so it can double as a Claude artifact.
    source = (REPO_ROOT / "website" / "index.html").read_text(encoding="utf-8")
    assert "<!doctype" not in source.lower()
    assert "<html" not in source.lower()


def test_no_internal_link_dangles(site):
    for page in site.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        for target in re.findall(r'(?:href|src)="([^"]+)"', html):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = target.partition("#")[0]
            if not path:
                continue
            resolved = (page.parent / path).resolve()
            if resolved.is_dir():
                resolved = resolved / "index.html"
            assert resolved.exists(), f"{page.name} -> {target}"


def test_markdown_cross_links_are_rewritten(site):
    quickstart = (site / "docs" / "quickstart.html").read_text(encoding="utf-8")

    # Sibling docs resolve to generated pages, not raw .md files...
    assert 'href="installation.html"' in quickstart
    assert not re.search(r'href="(?!https://)[^"]*\.md"', quickstart)
    # ...and the link text reads as a page title rather than a filename.
    assert ">Installation<" in quickstart

    # Files outside the rendered set still have to go somewhere real.
    agents = (site / "docs" / "ai-agents.html").read_text(encoding="utf-8")
    for target in re.findall(r'href="([^"]*\.md[^"]*)"', agents):
        assert target.startswith("https://github.com/"), target
