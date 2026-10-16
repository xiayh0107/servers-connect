from __future__ import annotations

import ctypes
import os
import select
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from ssh_server_manager import askpass
from ssh_server_manager.askpass import choose_descriptor


FINGERPRINT = "SHA256:" + "A" * 43
HOST_PROMPT = ("The authenticity of host 'fixture.example (127.0.0.1)' can't be established.\n"
               f"ED25519 key fingerprint is {FINGERPRINT}.\n"
               "Are you sure you want to continue connecting (yes/no/[fingerprint])? ")


def _never_vault():
    raise AssertionError("host confirmation must never read a vault secret")


@pytest.mark.parametrize("answer,expected", [("yes\n", "yes"), ("no\n", "no"),
                                           (FINGERPRINT + "\n", FINGERPRINT),
                                           ("\n", None), ("YES\n", None),
                                           ("SHA256:" + "B" * 43 + "\n", None)])
def test_host_confirmation_returns_only_explicit_expected_answers(monkeypatch, capsys, answer, expected):
    monkeypatch.setenv(askpass.HOST_KEY_CONFIRM_ENV, "terminal")
    monkeypatch.setattr(sys, "argv", ["askpass", HOST_PROMPT])
    monkeypatch.setitem(sys.modules, "ssh_server_manager.vault", SimpleNamespace(get_vault=_never_vault))
    reader = "_read_windows_confirmation" if os.name == "nt" else "_read_posix_confirmation"
    observed = []
    monkeypatch.setattr(askpass, reader, lambda prompt: observed.append(prompt) or answer)
    assert askpass.main() == (0 if expected is not None else 1)
    assert capsys.readouterr().out == (expected or "")
    assert observed == [HOST_PROMPT]


@pytest.mark.parametrize("prompt", [
    "Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
    "Password then continue connecting (yes/no)? ",
    HOST_PROMPT.replace(FINGERPRINT, "SHA256:short"),
    HOST_PROMPT.replace("ED25519 key fingerprint", "Server key fingerprint"),
    HOST_PROMPT.replace("Are you sure", "Could you"),
    HOST_PROMPT.replace("ED25519 key fingerprint", "\x1b[2JED25519 key fingerprint"),
    HOST_PROMPT.replace("fixture.example", "fixture\x7f.example"),
    HOST_PROMPT.replace("fixture.example", "fixture\x85.example"),
    HOST_PROMPT.replace("fixture.example", "fixture\u202e.example"),
    HOST_PROMPT.replace("ED25519 key fingerprint", "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\nED25519 key fingerprint"),
    HOST_PROMPT.replace("ED25519 key fingerprint", "WARNING: REVOKED HOST KEY DETECTED!\nED25519 key fingerprint"),
    HOST_PROMPT.replace("ED25519 key fingerprint", f"RSA key fingerprint is {FINGERPRINT}.\nED25519 key fingerprint"),
])
def test_ambiguous_or_changed_host_prompts_never_open_terminal_or_vault(monkeypatch, capsys, prompt):
    monkeypatch.setenv(askpass.HOST_KEY_CONFIRM_ENV, "terminal")
    monkeypatch.setattr(sys, "argv", ["askpass", prompt])
    monkeypatch.setitem(sys.modules, "ssh_server_manager.vault", SimpleNamespace(get_vault=_never_vault))
    monkeypatch.setattr(askpass, "_read_posix_confirmation", lambda prompt: pytest.fail("opened a terminal"))
    monkeypatch.setattr(askpass, "_read_windows_confirmation", lambda prompt: pytest.fail("opened a console"))
    assert askpass.main() == 1
    assert capsys.readouterr().out == ""


def test_host_confirmation_requires_explicit_flag_and_probe_ignores_it(monkeypatch, capsys):
    monkeypatch.delenv(askpass.HOST_KEY_CONFIRM_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["askpass", HOST_PROMPT])
    monkeypatch.setitem(sys.modules, "ssh_server_manager.vault", SimpleNamespace(get_vault=_never_vault))
    monkeypatch.setattr(askpass, "_read_posix_confirmation", lambda prompt: pytest.fail("opened a terminal"))
    monkeypatch.setattr(askpass, "_read_windows_confirmation", lambda prompt: pytest.fail("opened a console"))
    assert askpass.main() == 1
    monkeypatch.setenv(askpass.HOST_KEY_CONFIRM_ENV, "terminal")
    monkeypatch.setenv("SSM_CREDENTIAL_PROBE_ENDPOINT", "127.0.0.1:1")
    calls = []
    monkeypatch.setattr("ssh_server_manager.credential_probe_helper.probe_askpass", lambda prompt: calls.append(prompt) or 1)
    assert askpass.main() == 1
    assert calls == [HOST_PROMPT]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("prompt", [HOST_PROMPT,
    HOST_PROMPT.replace("fingerprint is ", "fingerprint is: ").replace(FINGERPRINT + ".", FINGERPRINT),
    HOST_PROMPT.replace("established.", "established\nbut keys of different type are already known for this host."),
    HOST_PROMPT.replace("\n", "\r\n"),
])
def test_full_openssh_prompt_variants_keep_the_exact_fingerprint(prompt):
    assert askpass._host_key_fingerprint(prompt) == FINGERPRINT


def test_posix_confirmation_rejects_a_nonterminal_and_reclaims_descriptor(monkeypatch):
    monkeypatch.setattr(os, "open", lambda *args: 73)
    monkeypatch.setattr(os, "isatty", lambda descriptor: False)
    monkeypatch.setattr(os, "read", lambda *args: pytest.fail("read redirected input"))
    monkeypatch.setattr(os, "write", lambda *args: pytest.fail("wrote redirected output"))
    closed = []
    monkeypatch.setattr(os, "close", closed.append)
    assert askpass._read_posix_confirmation(HOST_PROMPT) is None
    assert closed == [73]


class ConsoleKernel:
    def __init__(self, failure=None, response="yes\r\n"):
        self.failure, self.response = failure, response
        self.opened, self.closed, self.written = [], [], []
        self.CreateFileW = self._function(self.open)
        self.GetConsoleMode = self._function(self.mode)
        self.WriteConsoleW = self._function(self.write)
        self.ReadConsoleW = self._function(self.read)
        self.CloseHandle = self._function(self.closed.append)

    @staticmethod
    def _function(method):
        return lambda *args: method(*args)

    def open(self, name, access, share, security, disposition, flags, template):
        assert name in ("CONIN$", "CONOUT$") and share == 3 and disposition == 3
        assert security is None and template is None
        if self.failure == name:
            return ctypes.c_void_p(-1).value
        handle = 11 if name == "CONIN$" else 12
        self.opened.append(handle)
        return handle

    def mode(self, handle, pointer):
        pointer._obj.value = 0 if self.failure == "raw" else 2
        return self.failure != f"mode-{handle}"

    def write(self, handle, buffer, count, pointer, reserved):
        assert handle == 12 and reserved is None
        self.written.append(buffer.value)
        assert count == len(buffer.value.encode("utf-16-le")) // 2
        pointer._obj.value = count - (self.failure == "partial-write")
        return self.failure != "write"

    def read(self, handle, buffer, size, pointer, control):
        assert handle == 11 and control is None and size == 1023
        buffer.value = self.response
        pointer._obj.value = len(self.response)
        return self.failure != "read"


@pytest.mark.parametrize("failure", [None, "CONIN$", "CONOUT$", "mode-11", "mode-12", "raw", "write", "partial-write", "read"])
def test_windows_confirmation_uses_console_devices_and_closes_every_handle(monkeypatch, failure):
    kernel = ConsoleKernel(failure)
    monkeypatch.setattr(askpass, "_windows_console_kernel", lambda: kernel)
    prompt = HOST_PROMPT.replace("fixture.example", "主机😀")
    assert askpass._read_windows_confirmation(prompt) == ("yes\r\n" if failure is None else None)
    assert kernel.closed == list(reversed(kernel.opened))
    if failure is None:
        assert kernel.written == [prompt]
    assert kernel.GetConsoleMode.argtypes == [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    assert kernel.ReadConsoleW.restype is wintypes.BOOL


def test_windows_confirmation_rejects_incomplete_input(monkeypatch):
    kernel = ConsoleKernel(response="yes")
    monkeypatch.setattr(askpass, "_windows_console_kernel", lambda: kernel)
    assert askpass._read_windows_confirmation(HOST_PROMPT) is None
    assert kernel.closed == [12, 11]


_HOST_CONFIRM_CHILD = """
import os, sys, types
def no_vault():
    raise AssertionError('host-key prompt attempted vault access')
sys.modules['ssh_server_manager.vault'] = types.SimpleNamespace(get_vault=no_vault)
from ssh_server_manager.askpass import main
raise SystemExit(main())
"""

_CONTROLLING_TERMINAL_PARENT = """
import fcntl, os, subprocess, sys, termios
os.setsid()
fcntl.ioctl(int(sys.argv[1]), termios.TIOCSCTTY, 0)
# Model SSH's forced AskPass child: redirected stdio, inherited controlling TTY.
result = subprocess.run([sys.executable, '-c', sys.argv[2], sys.argv[3]],
                        stdin=subprocess.DEVNULL, capture_output=True)
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
"""


def _child_environment(tmp_path):
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(Path(askpass.__file__).resolve().parents[1]),
                       SSH_ASKPASS_REQUIRE="force", SSM_ASKPASS_HOST_KEY_CONFIRM="terminal",
                       SSM_DATA_DIR=str(tmp_path / "data"), SSM_RUNTIME_DIR=str(tmp_path / "runtime"))
    environment.pop("SSM_CREDENTIAL_PROBE_ENDPOINT", None)
    return environment


@pytest.mark.skipif(os.name == "nt", reason="POSIX controlling PTY regression; Windows console API is covered separately")
@pytest.mark.parametrize("response,expected", [(b"yes\n", "yes"), (b"no\n", "no"),
                                             ((FINGERPRINT + "\n").encode(), FINGERPRINT), (b"\x04", "")])
def test_askpass_subprocess_uses_controlling_pty_with_redirected_stdio(tmp_path, response, expected):
    import pty

    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, "-c", _CONTROLLING_TERMINAL_PARENT,
                                str(slave), _HOST_CONFIRM_CHILD, HOST_PROMPT],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               pass_fds=(slave,), env=_child_environment(tmp_path))
    try:
        visible = bytearray()
        deadline = time.monotonic() + 5
        while b"(yes/no/[fingerprint])? " not in visible:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "AskPass did not display the complete controlling-terminal prompt"
            ready, _, _ = select.select([master], [], [], remaining)
            assert ready, "AskPass did not display the controlling-terminal prompt"
            visible.extend(os.read(master, 16384))
        assert HOST_PROMPT.replace("\n", "\r\n").encode() in visible
        os.write(master, response)
        # Drain terminal echo as a real terminal emulator would. On macOS the
        # session owner's exit can wait for pending PTY output to be consumed.
        deadline = time.monotonic() + 5
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "AskPass did not finish after a terminal answer"
            ready, _, _ = select.select([master], [], [], remaining)
            if ready and not os.read(master, 16384):
                break
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == (0 if expected else 1)
        assert stdout.decode() == expected
        assert stderr == b""
    finally:
        os.close(master)
        os.close(slave)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_askpass_without_controlling_terminal_never_reads_redirected_yes_or_vault(tmp_path):
    result = subprocess.run([sys.executable, "-c", _HOST_CONFIRM_CHILD, HOST_PROMPT], input=b"yes\n",
                            capture_output=True, env=_child_environment(tmp_path),
                            start_new_session=os.name != "nt",
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                            timeout=5)
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""


def test_askpass_never_sends_password_to_otp_or_host_key_prompt():
    descriptors = [
        {
            "credential_id": "id",
            "slot": "password",
            "alias": "box",
            "hostname": "box.example",
            "username": "alice",
        }
    ]
    assert choose_descriptor("alice@box.example's password:", descriptors)["credential_id"] == "id"
    assert choose_descriptor("Enter OTP:", descriptors) is None
    assert choose_descriptor("Are you sure you want to continue connecting (yes/no/[fingerprint])?", descriptors) is None
