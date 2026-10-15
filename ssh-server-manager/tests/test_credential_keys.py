from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ssh_server_manager import credential_keys
from ssh_server_manager.db import ConflictError


def test_snapshot_reads_binary_bytes_without_text_translation(tmp_path):
    path = tmp_path / "key"
    data = b"binary snapshot\r\n\x1aafter the text EOF marker\r\n\x00\xff"
    path.write_bytes(data)
    snapshot = credential_keys.read_snapshot(str(path))
    assert snapshot.data == data
    snapshot.confirm()


def test_windows_path_and_descriptor_ctime_differ_but_bound_revision_stays_valid(tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_bytes(b"disposable snapshot")
    original_stat, original_fstat = Path.stat, os.fstat
    creation_time = path.stat().st_ctime_ns
    change_time = creation_time + 100

    def windows_stat(info, ctime):
        return SimpleNamespace(**{name: getattr(info, name) for name in
            ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")},
            st_ctime_ns=ctime, st_birthtime_ns=creation_time)

    def path_stat(target, *args, **kwargs):
        info = original_stat(target, *args, **kwargs)
        return windows_stat(info, creation_time) if target == path else info

    # Emulate the actual Windows stat/fstat difference without changing the
    # host platform used by pathlib or executing Windows ACL operations.
    windows_os = SimpleNamespace(**{**vars(os), "name": "nt",
        "fstat": lambda fd: windows_stat(original_fstat(fd), change_time)})
    monkeypatch.setattr(credential_keys, "os", windows_os)
    monkeypatch.setattr(Path, "stat", path_stat)
    monkeypatch.setattr(credential_keys, "key_permissions", lambda *_args:
        {"method": "windows_acl", "assessment": "unknown", "mode": None})
    snapshot = credential_keys.read_snapshot(str(path))
    snapshot.confirm()
    assert credential_keys.read_snapshot(str(path)).revision == snapshot.revision
    change_time += 1
    with pytest.raises(ConflictError, match="changed while it was being checked"):
        snapshot.confirm()


def test_descriptor_ctime_change_during_read_remains_a_conflict(tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_bytes(b"disposable snapshot")
    original_fstat = os.fstat
    calls = 0

    def changing_fstat(fd):
        nonlocal calls
        info = original_fstat(fd)
        calls += 1
        return SimpleNamespace(**{name: getattr(info, name) for name in
            ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")},
            st_ctime_ns=info.st_ctime_ns + (1 if calls > 1 else 0))

    monkeypatch.setattr(credential_keys.os, "fstat", changing_fstat)
    with pytest.raises(ConflictError, match="changed while it was being read"):
        credential_keys.read_snapshot(str(path))


def test_path_replacement_during_read_is_a_conflict(tmp_path, monkeypatch):
    path, replacement = tmp_path / "key", tmp_path / "replacement"
    path.write_bytes(b"first snapshot")
    replacement.write_bytes(b"other snapshot")
    original_read, original_stat = os.read, Path.stat
    replaced = False

    def replacing_read(fd, count):
        nonlocal replaced
        result = original_read(fd, count)
        replaced = True
        return result

    def replaced_path_stat(target, *args, **kwargs):
        # Model a path now naming a different inode while the open descriptor
        # still names the original file; Windows may deny an actual rename of
        # a CRT-opened file, so this exercises the same signal on every OS.
        return original_stat(replacement if replaced and target == path else target, *args, **kwargs)

    monkeypatch.setattr(credential_keys.os, "read", replacing_read)
    monkeypatch.setattr(Path, "stat", replaced_path_stat)
    with pytest.raises(ConflictError, match="changed while it was being read"):
        credential_keys.read_snapshot(str(path))
