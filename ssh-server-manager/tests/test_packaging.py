"""Packaging renders from the artifact that was actually built.

The failure this prevents is a Homebrew formula whose hash does not match the
uploaded sdist, which only ever surfaces for someone who cannot install the tool.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_packaging"


def load_script():
    loader = importlib.machinery.SourceFileLoader("prepare_packaging", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def packaging():
    return load_script()


def make_sdist(directory: Path, version: str, *, root_name: str | None = None) -> Path:
    root = root_name or f"ssh_server_manager-{version}"
    source = directory / "src" / root
    (source / "scripts").mkdir(parents=True, exist_ok=True)
    (source / "PKG-INFO").write_text(f"Version: {version}\n", encoding="utf-8")
    archive = directory / f"ssh_server_manager-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(source, arcname=root)
    return archive


def test_the_template_has_no_stale_placeholders(packaging):
    template = packaging.TEMPLATE_DIRECTORY / "homebrew" / "ssh-server-manager.rb.in"
    assert template.is_file()
    text = template.read_text(encoding="utf-8")
    assert "${VERSION}" in text and "${SDIST_SHA256}" in text


def test_rendering_fills_the_version_and_the_real_hash(packaging, tmp_path):
    archive = make_sdist(tmp_path, packaging.package_version())
    assert packaging.main([str(archive), "--output", str(tmp_path / "out")]) == 0
    rendered = (tmp_path / "out" / "homebrew" / "ssh-server-manager.rb").read_text(encoding="utf-8")
    assert f'sha256 "{packaging.sha256_of(archive)}"' in rendered
    assert f"ssh_server_manager-{packaging.package_version()}.tar.gz" in rendered
    assert "${" not in rendered
    # The formula must not pin a Python that the project does not support.
    assert 'depends_on "python@3.13"' in rendered


def test_a_version_mismatch_is_refused(packaging, tmp_path):
    archive = make_sdist(tmp_path, "0.0.1")
    with pytest.raises(SystemExit, match="build the sdist from the tree you are packaging"):
        packaging.main([str(archive), "--output", str(tmp_path / "out")])
    assert not (tmp_path / "out").exists()


def test_an_sdist_that_does_not_contain_its_root_is_refused(packaging, tmp_path):
    archive = make_sdist(tmp_path, packaging.package_version(), root_name="something-else")
    with pytest.raises(SystemExit, match="does not contain"):
        packaging.main([str(archive), "--output", str(tmp_path / "out")])


def test_a_file_that_is_not_an_sdist_is_refused(packaging, tmp_path):
    not_archive = tmp_path / f"ssh_server_manager-{packaging.package_version()}.tar.gz"
    not_archive.write_text("not a tarball", encoding="utf-8")
    with pytest.raises(SystemExit, match="not a readable sdist"):
        packaging.main([str(not_archive), "--output", str(tmp_path / "out")])


def test_a_badly_named_artifact_is_refused(packaging, tmp_path):
    wrong_name = tmp_path / "ssh-server-manager-1.0.0.tar.gz"
    wrong_name.write_bytes(b"")
    with pytest.raises(SystemExit, match="is not an sdist named"):
        packaging.main([str(wrong_name), "--output", str(tmp_path / "out")])


def test_unfilled_placeholders_are_caught(packaging, tmp_path):
    template = tmp_path / "broken.rb.in"
    template.write_text("version ${VERSION}\nother ${SOMETHING_ELSE}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="SOMETHING_ELSE"):
        packaging.render(template, {"VERSION": "1.2.3"})
