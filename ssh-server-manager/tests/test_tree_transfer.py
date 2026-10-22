"""Recursive transfer of a whole tree over the same SFTP path.

A fake `sftp` on PATH interprets the batch this code generates, so the tests
exercise the real subprocess plumbing — the batch text, the ordering, the
destination layout — without needing an SSH server. The remote listing used by a
download is stubbed, because the listing route has its own coverage.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from ssh_server_manager.db import Database
from ssh_server_manager.ssh_runner import SSHRunner
from ssh_server_manager.validation import ValidationError


FAKE_SFTP = r'''
import os, shlex, shutil, sys
root = os.environ["FAKE_SFTP_ROOT"]
cwd = "/"
def resolve(path):
    if path.startswith("~"):
        return os.path.join(root, path[1:].lstrip("/"))
    if os.path.isabs(path):
        return os.path.join(root, path.lstrip("/"))
    return os.path.join(root, cwd.lstrip("/"), path)
status = 0
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if not line:
        continue
    if line.startswith("@"):
        line = line[1:]
    parts = shlex.split(line)
    if not parts:
        continue
    verb, rest = parts[0], parts[1:]
    if verb == "cd":
        cwd = rest[0] if rest else "/"
    elif verb == "mkdir":
        os.makedirs(resolve(rest[0]), exist_ok=True)
    elif verb == "put":
        rest = [p for p in rest if p != "-p"]
        try:
            shutil.copy2(rest[0], resolve(rest[1]))
        except OSError as exc:
            print(f"put failed: {exc}", file=sys.stderr); status = 1
    elif verb == "get":
        rest = [p for p in rest if p != "-p"]
        try:
            shutil.copy2(resolve(rest[0]), rest[1])
        except OSError as exc:
            print(f"get failed: {exc}", file=sys.stderr); status = 1
    elif verb == "quit":
        break
sys.exit(status)
'''


@pytest.fixture()
def remote(tmp_path, monkeypatch):
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    script = tmp_path / "fake_sftp.py"
    script.write_text("#!" + sys.executable + "\n" + FAKE_SFTP, encoding="utf-8")
    # A shim on PATH lets the real subprocess plumbing run unchanged. Windows has
    # no /bin/sh, so the launcher is a .bat there; shutil.which finds either via
    # PATHEXT, which keeps the batch-text assertions covered on all three OSes.
    if os.name == "nt":
        launcher = binary_directory / "sftp.bat"
        launcher.write_text('@"{}" "{}" %*\r\n'.format(sys.executable, script), encoding="utf-8")
    else:
        launcher = binary_directory / "sftp"
        launcher.write_text("#!/bin/sh\nexec {} {}\n".format(sys.executable, script), encoding="utf-8")
        launcher.chmod(0o755)
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    monkeypatch.setenv("FAKE_SFTP_ROOT", str(remote_root))
    monkeypatch.setenv("PATH", f"{binary_directory}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    return remote_root


@pytest.fixture()
def runner(remote, tmp_path):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", username="alice")
    return SSHRunner(database)


def _local_tree(root: Path) -> None:
    (root / "sub").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "a.txt").write_text("root file\n", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("nested file\n", encoding="utf-8")


def test_upload_copies_every_directory_and_file(runner, remote, tmp_path):
    local = tmp_path / "local"
    _local_tree(local)

    result = runner.upload_tree("box", local, "/incoming")

    assert result["recursive"] is True
    assert result["files"] == 2
    assert result["directories"] == 3  # the tree root, sub, and empty
    assert (remote / "incoming" / "a.txt").read_text(encoding="utf-8") == "root file\n"
    assert (remote / "incoming" / "sub" / "b.txt").read_text(encoding="utf-8") == "nested file\n"
    assert (remote / "incoming" / "empty").is_dir()


def test_upload_preserves_the_relative_layout(runner, remote, tmp_path):
    local = tmp_path / "local"
    (local / "deep" / "deeper").mkdir(parents=True)
    (local / "deep" / "deeper" / "file.txt").write_text("x", encoding="utf-8")

    runner.upload_tree("box", local, "/srv")

    assert (remote / "srv" / "deep" / "deeper" / "file.txt").is_file()


def test_upload_refuses_a_symlink_inside_the_tree(runner, remote, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    (local / "link.txt").symlink_to(target)

    with pytest.raises(ValidationError, match="refusing to follow a symlink"):
        runner.upload_tree("box", local, "/incoming")


def test_upload_refuses_a_symlinked_root(runner, remote, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.txt").write_text("a", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValidationError, match="symlinked directory"):
        runner.upload_tree("box", link, "/incoming")


def test_upload_refuses_a_tree_past_the_entry_cap(runner, remote, tmp_path, monkeypatch):
    from ssh_server_manager import ssh_runner

    monkeypatch.setattr(ssh_runner, "MAX_TREE_ENTRIES", 3)
    local = tmp_path / "local"
    _local_tree(local)
    with pytest.raises(ValidationError, match="more than 3 entries"):
        runner.upload_tree("box", local, "/incoming")


def test_upload_of_a_missing_directory_is_refused(runner, tmp_path):
    with pytest.raises(ValidationError, match="local directory does not exist"):
        runner.upload_tree("box", tmp_path / "nope", "/incoming")


def test_download_rebuilds_the_tree_locally(runner, remote, tmp_path, monkeypatch):
    (remote / "nested" / "deeper").mkdir(parents=True)
    (remote / "nested" / "m.txt").write_text("mid\n", encoding="utf-8")
    (remote / "nested" / "deeper" / "d.txt").write_text("deep\n", encoding="utf-8")
    listings = {
        "/nested": [
            {"name": "m.txt", "type": "file"},
            {"name": "deeper", "type": "directory"},
        ],
        "/nested/deeper": [{"name": "d.txt", "type": "file"}],
    }
    monkeypatch.setattr(
        runner, "list_directory", lambda identifier, path, **kwargs: {"entries": listings[path]}
    )
    destination = tmp_path / "out"

    result = runner.download_tree("box", "/nested", destination)

    assert result["files"] == 2
    assert result["directories"] == 1
    assert (destination / "m.txt").read_text(encoding="utf-8") == "mid\n"
    assert (destination / "deeper" / "d.txt").read_text(encoding="utf-8") == "deep\n"


def test_download_refuses_a_link_in_the_remote_tree(runner, remote, tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner,
        "list_directory",
        lambda identifier, path, **kwargs: {
            "entries": [{"name": "link", "type": "symlink"}]
        },
    )
    with pytest.raises(ValidationError, match="only files and directories"):
        runner.download_tree("box", "/nested", tmp_path / "out")


def test_download_refuses_the_home_directory(runner, tmp_path):
    with pytest.raises(ValidationError, match="name a remote directory"):
        runner.download_tree("box", "~", tmp_path / "out")


def test_download_creates_the_destination_directory(runner, remote, tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner,
        "list_directory",
        lambda identifier, path, **kwargs: {"entries": []},
    )
    destination = tmp_path / "brand-new"
    result = runner.download_tree("box", "/empty", destination)
    assert destination.is_dir()
    assert result["files"] == 0


def test_a_cli_recursive_put_then_get_round_trips(tmp_path, remote, monkeypatch, capsys):
    import json

    from ssh_server_manager.cli import main

    data = tmp_path / "data"
    monkeypatch.setenv("SSM_DATA_DIR", str(data))
    assert main(["server", "add", "box", "--hostname", "box.example", "--username", "alice"]) == 0
    capsys.readouterr()

    local = tmp_path / "local"
    _local_tree(local)
    assert main(["put", "--recursive", str(local), "box:/uploaded", "--json"]) == 0
    uploaded = json.loads(capsys.readouterr().out)
    assert uploaded["files"] == 2 and uploaded["recursive"] is True
    assert (remote / "uploaded" / "sub" / "b.txt").is_file()
