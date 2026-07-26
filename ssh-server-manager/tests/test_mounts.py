"""Mount coverage stops at the boundary CI can reach.

Real mounting needs a FUSE stack that is not installed on the runners, so these
tests pin the command we build, the capability report, and the mount-table
parsing. Actually mounting has to be verified by hand on each OS.
"""

from __future__ import annotations

import subprocess

import pytest

from ssh_server_manager import mounts
from ssh_server_manager.db import Database
from ssh_server_manager.ssh_runner import SSHError
from ssh_server_manager.validation import ValidationError


def _host(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    return database


def _pretend_backend(monkeypatch):
    monkeypatch.setattr(
        mounts,
        "mount_capability",
        lambda: {"ok": True, "backend": "sshfs", "path": "sshfs"},
    )


def _record_run(monkeypatch, observed, returncode=0, stderr=""):
    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_mount_builds_a_command_that_reuses_the_managed_identity(tmp_path, monkeypatch):
    database = _host(tmp_path, monkeypatch)
    _pretend_backend(monkeypatch)
    observed = {}
    _record_run(monkeypatch, observed)
    target = tmp_path / "mnt" / "box"
    result = mounts.mount(database, "box", "/srv/app", str(target))

    command = observed["command"]
    assert command[0] == "sshfs"
    # The alias, not user@host: the managed config owns hostname, user, key and
    # ProxyJump, so a mount cannot drift from how connect reaches the same host.
    assert command[1] == "box:/srv/app"
    assert command[2] == str(target.resolve())
    assert command[3] == "-F"
    assert command[4].endswith("managed.conf")
    assert "StrictHostKeyChecking=yes" in command
    assert "reconnect" in command
    assert "ro" not in command
    assert result["mountpoint"] == str(target.resolve())
    assert result["read_only"] is False


def test_mount_maps_home_to_the_login_directory(tmp_path, monkeypatch):
    database = _host(tmp_path, monkeypatch)
    _pretend_backend(monkeypatch)
    observed = {}
    _record_run(monkeypatch, observed)
    mounts.mount(database, "box", None, str(tmp_path / "mnt" / "box"))

    # sshfs reads an empty path as the login directory; "~" would be a literal.
    assert observed["command"][1] == "box:"


def test_read_only_mount_passes_ro(tmp_path, monkeypatch):
    database = _host(tmp_path, monkeypatch)
    _pretend_backend(monkeypatch)
    observed = {}
    _record_run(monkeypatch, observed)
    mounts.mount(database, "box", "/srv", str(tmp_path / "mnt" / "box"), read_only=True)
    assert "ro" in observed["command"]


def test_mount_refuses_a_non_empty_mountpoint(tmp_path, monkeypatch):
    database = _host(tmp_path, monkeypatch)
    _pretend_backend(monkeypatch)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing.txt").write_text("keep", encoding="utf-8")
    called = False

    def fake_run(command, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("sshfs must not run over an occupied directory")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValidationError, match="not empty"):
        mounts.mount(database, "box", "/srv", str(occupied))
    assert called is False


def test_mount_surfaces_sshfs_failure(tmp_path, monkeypatch):
    database = _host(tmp_path, monkeypatch)
    _pretend_backend(monkeypatch)
    _record_run(monkeypatch, {}, returncode=1, stderr="read: Connection reset by peer")
    with pytest.raises(SSHError, match="Connection reset"):
        mounts.mount(database, "box", "/srv", str(tmp_path / "mnt" / "box"))


def test_missing_backend_is_reported_before_anything_runs(tmp_path, monkeypatch):
    database = _host(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mounts,
        "mount_capability",
        lambda: {"ok": False, "backend": None, "message": "macFUSE is not installed"},
    )

    def fake_run(command, **kwargs):
        raise AssertionError("nothing should run without a backend")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SSHError, match="macFUSE is not installed"):
        mounts.mount(database, "box", "/srv", str(tmp_path / "mnt" / "box"))


def test_mount_list_reads_the_os_mount_table(monkeypatch):
    table = (
        "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
        "box@box.example:/srv/app on /Users/alice/mnt/box (macfuse, nodev, nosuid, synchronous)\n"
        "web1: on /home/alice/mnt/web1 (fuse.sshfs, rw, nosuid, nodev, user_id=1000)\n"
        "map auto_home on /System/Volumes/Data/home (autofs, automounted)\n"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=table, stderr=""),
    )
    monkeypatch.setattr(mounts, "_is_windows", lambda: False)

    listed = mounts.list_mounts()

    # Only sshfs mounts, and the real root filesystem is never reported.
    assert [item["mountpoint"] for item in listed] == [
        "/Users/alice/mnt/box",
        "/home/alice/mnt/web1",
    ]
    assert listed[0]["type"] == "macfuse"
    assert listed[1]["type"] == "fuse.sshfs"


def test_unmount_refuses_a_directory_that_is_not_mounted(tmp_path, monkeypatch):
    monkeypatch.setattr(mounts, "list_mounts", lambda: [])

    def fake_run(command, **kwargs):
        raise AssertionError("umount must not run for an unmounted path")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValidationError, match="not a mounted directory"):
        mounts.unmount(str(tmp_path / "nowhere"))


def test_capability_names_what_is_missing(monkeypatch):
    monkeypatch.setattr(mounts, "_is_windows", lambda: False)
    monkeypatch.setattr(mounts.shutil, "which", lambda name: None)
    monkeypatch.setattr(mounts, "_macfuse_installed", lambda: False)

    capability = mounts.mount_capability()

    assert capability["ok"] is False
    # An unusable install must say which piece is absent, not just "failed".
    assert capability["message"]
    assert "install" in capability["message"].casefold()


def test_mount_carries_vault_credentials_without_leaking_the_secret(tmp_path, monkeypatch):
    import json

    from ssh_server_manager.service import CredentialService
    from ssh_server_manager.vault import MemoryVault

    database = _host(tmp_path, monkeypatch)
    credential = CredentialService(database, MemoryVault()).create_password("prod-pw", "s3cret")
    database.update_server("box", credential_id=credential["id"])
    _pretend_backend(monkeypatch)
    observed = {}
    _record_run(monkeypatch, observed)

    mounts.mount(database, "box", "/srv", str(tmp_path / "mnt" / "box"))

    environment = observed["env"]
    # sshfs runs ssh underneath, so the existing askpass boundary is all a mount
    # needs — there is no second credential path to review.
    assert environment["SSH_ASKPASS_REQUIRE"] == "force"
    assert environment["SSH_ASKPASS"]
    descriptors = json.loads(environment["SSM_ASKPASS_MAP"])
    assert [(item["alias"], item["slot"]) for item in descriptors] == [("box", "password")]
    # The map carries identifiers only; the secret is fetched by the helper.
    assert "s3cret" not in json.dumps(environment)
