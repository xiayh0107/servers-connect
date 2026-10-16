from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ssh_server_manager import process_args


@pytest.mark.parametrize("runtime", ["msys-2.0.dll", "cygwin1.dll"])
def test_detects_the_selected_executables_runtime_only(tmp_path, monkeypatch, runtime):
    selected = tmp_path / "selected" / "ssh.exe"
    selected.parent.mkdir()
    selected.touch()
    other = tmp_path / "other"
    other.mkdir()
    (other / runtime).touch()
    monkeypatch.setattr(process_args.shutil, "which", lambda executable: str(selected))
    assert not process_args._is_msys_executable("ssh")
    (selected.parent / runtime).touch()
    assert process_args._is_msys_executable("ssh")


@pytest.mark.parametrize("platform,msys,path", [
    ("posix", True, "source${literal}.conf"),
    ("nt", False, "source${literal}.conf"),
    ("nt", True, "ordinary.conf"),
    ("nt", True, "directory with spaces/config.conf"),
])
def test_ordinary_and_native_commands_keep_the_original_list(monkeypatch, platform, msys, path):
    monkeypatch.setattr(process_args, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(process_args, "_is_msys_executable", lambda executable: msys)
    command = ["ssh", "-F", path, "fixture", "printf '%s' 'remote value'"]
    assert process_args.literal_config_args(command) is command
    assert process_args.literal_config_args([]) == []
    for missing in (["ssh", "fixture"], ["ssh", "-F"]):
        assert process_args.literal_config_args(missing) is missing


@pytest.mark.parametrize("path", [
    r"C:\fixture\source${literal}.conf", r"C:\fixture\O'Brien.conf",
    r"C:\fixture\[source]*?.conf", "C:\\fixture with spaces\\[source]\\",
])
def test_quotes_only_the_first_config_operand_without_altering_remote_argv(monkeypatch, path):
    monkeypatch.setattr(process_args, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process_args, "_is_msys_executable", lambda executable: True)
    remote = ["fixture", "printf '%s' 'remote ${value} [x]'", "-F", "remote*[x]"]
    command = [r"C:\Program Files\Git\usr\bin\ssh.exe", "-G", "-F", path, *remote]
    unchanged = command.copy()
    expected = subprocess.list2cmdline(command[:3]) + " " + process_args._double_quoted_arg(path)
    expected += " " + subprocess.list2cmdline(remote)
    assert process_args.literal_config_args(command) == expected
    assert command == unchanged


@pytest.mark.parametrize("value,expected", [
    ("plain", '"plain"'),
    ("C:\\[source]\\", '"C:\\[source]\\\\"'),
    ('a\\"b', '"a\\\\\\"b"'),
])
def test_forced_quotes_follow_windows_backslash_rules(value, expected):
    assert process_args._double_quoted_arg(value) == expected


@pytest.mark.skipif(os.name != "nt", reason="native Windows to MSYS startup boundary")
def test_real_msys_startup_preserves_config_and_remote_command_arguments(tmp_path):
    selected = os.environ.get("SSM_TEST_SSH_CLIENT") or shutil.which("ssh")
    if not selected or not process_args._is_msys_executable(selected):
        pytest.skip("the selected client does not use MSYS/Cygwin")
    shell = Path(shutil.which(selected) or selected).resolve().with_name("sh.exe")
    assert shell.is_file(), "the selected POSIX client must provide its sibling shell fixture"
    config = str(tmp_path / "source${SSM_TEST_SOURCE}[x]O'Brien.conf")
    remote = "printf '%s' 'remote literal ${HOME} [x]'"
    command = [str(shell), "-c", 'printf "%s\\n" "$@"', "fixture", "-F", config, remote]
    adapted = process_args.literal_config_args(command)
    assert isinstance(adapted, str)
    result = subprocess.run(adapted, capture_output=True, encoding="utf-8", timeout=10, shell=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["-F", config, remote]
