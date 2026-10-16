from __future__ import annotations

import os
import stat
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from ssh_server_manager.db import Database
from ssh_server_manager.ssh_config import SSHConfigError, includes_target, render_config
from ssh_server_manager import ssh_config
from ssh_fixture_permissions import private_ssh_fixture_file


def fixture_ssh():
    selected = os.environ.get("SSM_TEST_SSH_CLIENT")
    if selected:
        assert shutil.which(selected), "SSM_TEST_SSH_CLIENT must select an installed OpenSSH client"
        return selected
    ssh = shutil.which("ssh")
    if not ssh:
        pytest.skip("OpenSSH is unavailable")
    return ssh


def effective_values(ssh, config, alias):
    result = subprocess.run(
        [ssh, "-G", "-F", str(config), alias], capture_output=True,
        text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    values = {}
    for line in result.stdout.splitlines():
        if " " in line:
            key, value = line.split(" ", 1)
            values.setdefault(key, []).append(value)
    return values


def test_rendered_config_contains_no_secret_and_includes_original(tmp_path):
    database = Database(tmp_path / "manager.db")
    credential = database.create_credential(label="Password", kind="password", has_secret=True)
    database.create_server(
        alias="box",
        hostname="box.example",
        port=2200,
        username="alice",
        credential_id=credential["id"],
    )
    original = tmp_path / "config"
    original.write_text("Host *\n  ServerAliveInterval 30\n", encoding="utf-8")
    managed = tmp_path / "managed.conf"
    render_config(database, destination=managed, original=original)
    content = managed.read_text(encoding="utf-8")
    assert "Host box" in content
    assert "PreferredAuthentications keyboard-interactive,password" in content
    assert "Include " in content
    assert "secret" not in content.casefold()
    if os.name != "nt":
        assert stat.S_IMODE(managed.stat().st_mode) == 0o600


def test_include_cycle_is_rejected(tmp_path):
    database = Database(tmp_path / "manager.db")
    managed = tmp_path / "managed.conf"
    original = tmp_path / "config"
    original.write_text(f"Include {managed}\n", encoding="utf-8")
    assert includes_target(original, managed)
    with pytest.raises(SSHConfigError, match="cycle"):
        render_config(database, destination=managed, original=original)


def test_original_defaults_and_external_aliases_apply_before_last_host(tmp_path):
    ssh = fixture_ssh()
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="alpha", hostname="127.0.0.1", username="fixture")
    database.create_server(alias="omega", hostname="127.0.0.2", username="fixture")
    original = tmp_path / "original.conf"
    known_hosts = tmp_path / "known_hosts"
    original.write_text(
        f'Host *\n  UserKnownHostsFile "{known_hosts.as_posix()}"\n'
        "Host external-jump\n  HostName 127.0.0.3\n  Port 22022\n",
        encoding="utf-8",
    )
    private_ssh_fixture_file(original)
    managed = render_config(database, destination=tmp_path / "managed.conf", original=original, ssh_binary=ssh)
    assert effective_values(ssh, managed, "alpha")["userknownhostsfile"] == [known_hosts.as_posix()]
    external = effective_values(ssh, managed, "external-jump")
    assert external["hostname"] == ["127.0.0.3"]
    assert external["port"] == ["22022"]


def test_unbound_host_keeps_original_identity_and_literal_include_filename(tmp_path):
    ssh = fixture_ssh()
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="127.0.0.9", port=2200, username="managed")
    original = tmp_path / "[source] 中文 100% ${SSM_TEST_SOURCE} config.conf"
    dummy_key = tmp_path / "dummy-key"
    original.write_text(
        "Host box\n  HostName 127.0.0.10\n  Port 2300\n  User original\n"
        f'  IdentityFile "{dummy_key.as_posix()}"\n  IdentitiesOnly yes\n', encoding="utf-8",
    )
    private_ssh_fixture_file(original)
    managed = render_config(database, destination=tmp_path / "other" / "managed.conf", original=original, ssh_binary=ssh)
    effective = effective_values(ssh, managed, "box")
    assert effective["identityfile"] == [dummy_key.as_posix()]
    assert effective["identitiesonly"] == ["yes"]
    assert effective["hostname"] == ["127.0.0.9"]
    assert effective["port"] == ["2200"]
    assert effective["user"] == ["managed"]


def test_custom_sources_isolate_duplicate_aliases_and_preserve_external_jumps(tmp_path):
    ssh = fixture_ssh()
    database = Database(tmp_path / "manager.db")
    default = tmp_path / "default.conf"
    first = tmp_path / "first.conf"
    second = tmp_path / "second.conf"
    default_key, first_key, second_key = [tmp_path / name for name in ("default-key", "first-key", "second-key")]
    default.write_text(
        f'Host *\n  IdentityFile "{default_key.as_posix()}"\n'
        "Host default-jump\n  HostName 127.0.0.3\n", encoding="utf-8",
    )
    first.write_text(
        f'Host alpha omega\n  IdentityFile "{first_key.as_posix()}"\n'
        "Host first-jump\n  HostName 127.0.0.4\n  Port 22004\n", encoding="utf-8",
    )
    second.write_text(
        f'Host alpha omega\n  IdentityFile "{second_key.as_posix()}"\n'
        "Host second-jump\n  HostName 127.0.0.5\n  Port 22005\n", encoding="utf-8",
    )
    for source in (default, first, second):
        private_ssh_fixture_file(source)
    alpha = database.create_server(alias="alpha", hostname="127.0.0.1", username="fixture", ssh_config_source=str(first))
    database.create_server(alias="omega", hostname="127.0.0.2", username="fixture", ssh_config_source=str(second))
    database.create_server(alias="default-box", hostname="127.0.0.6", username="fixture")
    managed = render_config(database, destination=tmp_path / "managed.conf", original=default, ssh_binary=ssh)
    assert effective_values(ssh, managed, "alpha")["identityfile"] == [first_key.as_posix()]
    assert effective_values(ssh, managed, "omega")["identityfile"] == [second_key.as_posix()]
    assert effective_values(ssh, managed, "default-box")["identityfile"] == [default_key.as_posix()]
    assert effective_values(ssh, managed, "first-jump")["hostname"] == ["127.0.0.4"]
    assert effective_values(ssh, managed, "second-jump")["port"] == ["22005"]
    assert effective_values(ssh, managed, "default-jump")["hostname"] == ["127.0.0.3"]
    # Sources follow stable IDs after rename, and stale IDs cannot add Includes.
    database.update_server(alpha["id"], alias="renamed")
    managed = render_config(database, destination=managed, original=default, ssh_binary=ssh)
    content = managed.read_text(encoding="utf-8")
    assert f"Host renamed\n  HostName 127.0.0.1" in content
    assert f"  Include {ssh_config._IncludePaths(ssh).format(first.resolve())}" in content.split("Host renamed\n", 1)[1].split("\n\n", 1)[0]
    database.set_setting("ssh_config_import_sources", '{"deleted-host": "/not/a/real/source.conf"}')
    managed = render_config(database, destination=managed, original=default, ssh_binary=ssh)
    assert "/not/a/real/source.conf" not in managed.read_text(encoding="utf-8")


def test_relative_include_cycle_uses_ssh_user_directory_not_source_parent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    user_ssh = home / ".ssh"
    user_ssh.mkdir(parents=True)
    monkeypatch.setattr(ssh_config.Path, "home", lambda: home)
    monkeypatch.delenv("HOME", raising=False)
    original = tmp_path / "elsewhere" / "source.conf"
    original.parent.mkdir()
    original.write_text("Include managed.conf\n", encoding="utf-8")
    assert includes_target(original, user_ssh / "managed.conf")
    assert not includes_target(original, original.parent / "managed.conf")


def test_custom_source_cycle_is_rejected_before_replacing_managed(tmp_path):
    database = Database(tmp_path / "manager.db")
    managed = tmp_path / "managed.conf"
    managed.write_text("existing config\n", encoding="utf-8")
    source = tmp_path / "imported.conf"
    source.write_text(f"Include {ssh_config.quote_value(managed.as_posix())}\n", encoding="utf-8")
    database.create_server(alias="box", hostname="127.0.0.1", username="fixture", ssh_config_source=str(source))
    with pytest.raises(SSHConfigError, match="cycle"):
        render_config(database, destination=managed, original=tmp_path / "missing")
    assert managed.read_text(encoding="utf-8") == "existing config\n"


def test_import_source_validator_checks_format_and_cycle_without_writing(tmp_path):
    managed = tmp_path / "managed.conf"
    managed.write_text("previous config\n", encoding="utf-8")
    source = tmp_path / "source.conf"
    source.write_text(f"Include {ssh_config.quote_value(managed.as_posix())}\n", encoding="utf-8")
    with pytest.raises(SSHConfigError, match="cycle"):
        ssh_config.validate_include_source(source, destination=managed)
    with pytest.raises(SSHConfigError, match="printable"):
        ssh_config.validate_include_source(tmp_path / "invalid\npath", destination=managed)
    valid = tmp_path / "valid ${literal} source.conf"
    assert ssh_config.validate_include_source(valid, destination=managed) == valid.resolve()
    assert managed.read_text(encoding="utf-8") == "previous config\n"


@pytest.mark.parametrize("directive", ["Include=", "Include = ", "  include\t"])
def test_include_cycle_accepts_openssh_equals_syntax(tmp_path, directive):
    managed = tmp_path / "managed config"
    source = tmp_path / "source.conf"
    source.write_text(f"{directive}{ssh_config.quote_value(managed.as_posix())}\n", encoding="utf-8")
    assert includes_target(source, managed)


def test_msys_cycle_check_converts_posix_include_with_selected_client(tmp_path, monkeypatch):
    source = tmp_path / "source.conf"
    managed = tmp_path / "managed.conf"
    source.write_text("Include /c/fixture/managed.conf\n", encoding="utf-8")
    converter = tmp_path / "cygpath.exe"
    monkeypatch.setattr(ssh_config, "_cygpath_for_client", lambda binary: converter)
    # The fake converter returns this platform's temporary path. Production
    # cygpath -w returns a drive-qualified Windows path instead.
    is_absolute = ssh_config.ntpath.isabs
    monkeypatch.setattr(ssh_config.ntpath, "isabs", lambda value: value == str(managed.resolve()) or is_absolute(value))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[1:] == ["-w", "-C", "UTF8", "--", "/c/fixture/managed.conf"]
        return SimpleNamespace(returncode=0, stdout=str(managed.resolve()) + "\n")

    monkeypatch.setattr(ssh_config.subprocess, "run", run)
    assert includes_target(source, managed, _paths=ssh_config._IncludePaths("fixture-ssh", windows=True))
    assert len(calls) == 1


def test_native_windows_include_uses_forward_slash_absolute_path(monkeypatch):
    monkeypatch.setattr(ssh_config, "_cygpath_for_client", lambda binary: None)
    paths = ssh_config._IncludePaths("native-ssh", windows=True)
    assert paths.format(PureWindowsPath(r"D:\custom profile\original.conf")) == '"D:/custom profile/original.conf"'


def test_msys_include_uses_selected_distribution_converter_and_caches(tmp_path, monkeypatch):
    client_dir = tmp_path / "git" / "usr" / "bin"
    client_dir.mkdir(parents=True)
    client = client_dir / "ssh.exe"
    converter = client_dir / "cygpath.exe"
    for file in (client, converter, client_dir / "msys-2.0.dll"):
        file.write_bytes(b"fixture")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 5
        return SimpleNamespace(returncode=0, stdout="/d/custom profile/original.conf\n")

    monkeypatch.setattr(ssh_config.subprocess, "run", run)
    paths = ssh_config._IncludePaths(str(client), windows=True)
    source = PureWindowsPath(r"D:\custom profile\original.conf")
    assert paths.format(source) == '"/d/custom profile/original.conf"'
    assert paths.format(source) == '"/d/custom profile/original.conf"'
    assert calls == [[str(converter), "-u", "--", str(source)]]


def test_msys_conversion_uses_isolated_utf8_environment(tmp_path, monkeypatch):
    converter = tmp_path / "cygpath.exe"
    monkeypatch.setattr(ssh_config, "_cygpath_for_client", lambda binary: converter)
    monkeypatch.setenv("LC_ALL", "en_US.CP1252")
    monkeypatch.setenv("LC_CTYPE", "C")
    monkeypatch.setenv("LANG", "C")
    parent_environment = os.environ.copy()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        child_environment = kwargs["env"]
        assert id(child_environment) != id(os.environ)
        assert child_environment.get("LC_ALL") == "C.UTF-8"
        # Compare without exposing inherited environment values on failure.
        assert len(child_environment) == len(parent_environment)
        assert all(
            child_environment.get(key) == value
            for key, value in parent_environment.items() if key != "LC_ALL"
        )
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "strict"
        converted = "/d/中文/source.conf" if command[1] == "-u" else "D:\\中文\\source.conf"
        return SimpleNamespace(returncode=0, stdout=converted + "\n")

    monkeypatch.setattr(ssh_config.subprocess, "run", run)
    paths = ssh_config._IncludePaths("fixture-ssh", windows=True)
    assert paths.format(PureWindowsPath("D:/中文/source.conf")) == "/d/中文/source.conf"
    assert str(paths.local("/d/中文/source.conf")) == "D:\\中文\\source.conf"
    assert calls == [
        [str(converter), "-u", "--", "D:\\中文\\source.conf"],
        [str(converter), "-w", "-C", "UTF8", "--", "/d/中文/source.conf"],
    ]
    assert all(os.environ.get(key) == value for key, value in parent_environment.items())
    assert len(os.environ) == len(parent_environment)


@pytest.mark.parametrize("failure", ["missing", "error", "relative", "multiline", "timeout"])
def test_msys_conversion_failure_never_falls_back_to_native_drive_paths(tmp_path, monkeypatch, failure):
    client = tmp_path / "ssh.exe"
    client.write_bytes(b"fixture")
    (tmp_path / "msys-2.0.dll").write_bytes(b"fixture")
    if failure != "missing":
        (tmp_path / "cygpath.exe").write_bytes(b"fixture")

    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("fixture-converter", 5)
        return SimpleNamespace(returncode=1 if failure == "error" else 0, stdout={
            "relative": "custom/original.conf", "multiline": "/d/original\n/other", "error": "",
        }.get(failure, ""))

    monkeypatch.setattr(ssh_config.subprocess, "run", run)
    with pytest.raises(SSHConfigError, match="cygpath|convert"):
        ssh_config._IncludePaths(str(client), windows=True).format(PureWindowsPath(r"D:\custom\original.conf"))


def test_render_serializes_snapshot_and_replace_with_concurrent_edit(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="old.example", username="alice")
    managed = tmp_path / "managed.conf"
    original = tmp_path / "missing.conf"
    snapshot = Event()
    release = Event()
    editing = Event()
    updated = Event()
    render_server = ssh_config._render_server

    def pause(server, credential):
        snapshot.set()
        assert release.wait(timeout=5)
        return render_server(server, credential)

    def edit():
        editing.set()
        database.update_server("box", hostname="new.example")
        updated.set()
        render_config(database, destination=managed, original=original)

    monkeypatch.setattr(ssh_config, "_render_server", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        render = pool.submit(render_config, database, destination=managed, original=original)
        assert snapshot.wait(timeout=5)
        change = pool.submit(edit)
        assert editing.wait(timeout=5)
        try:
            assert not updated.wait(timeout=.05)
        finally:
            release.set()
        render.result(timeout=5)
        change.result(timeout=5)
    assert "HostName new.example" in managed.read_text()
    assert "old.example" not in managed.read_text()

