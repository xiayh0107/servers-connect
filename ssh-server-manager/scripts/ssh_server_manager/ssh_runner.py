from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .db import Database, NotFoundError
from .paths import runtime_dir
from .ssh_config import render_config


class SSHError(RuntimeError):
    pass


PROMPT_REDACTIONS = [
    (re.compile(r"(?i)(password|passphrase)(\s*[:=]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(token|secret|cookie)(\s*[:=]\s*)\S+"), r"\1\2[REDACTED]"),
]


def redact(text: str) -> str:
    result = text
    for pattern, replacement in PROMPT_REDACTIONS:
        result = pattern.sub(replacement, result)
    return result[:4000]


def classify_error(stderr: str, returncode: int) -> str:
    lowered = stderr.casefold()
    if "host key verification failed" in lowered or "no host key is known" in lowered:
        return "host-key-untrusted"
    if "permission denied" in lowered or "authentication failed" in lowered:
        return "authentication-failed"
    if "connection timed out" in lowered or "operation timed out" in lowered:
        return "timeout"
    if "connection refused" in lowered:
        return "connection-refused"
    if "could not resolve hostname" in lowered or "name or service not known" in lowered:
        return "dns-failed"
    if "no route to host" in lowered or "network is unreachable" in lowered:
        return "network-unreachable"
    if returncode == 127:
        return "ssh-not-found"
    return "ssh-failed"


class SSHRunner:
    def __init__(self, database: Database, *, ssh_binary: str | None = None) -> None:
        self.database = database
        self.ssh_binary = ssh_binary or shutil.which("ssh") or "ssh"

    def _credential_descriptor(self, server: dict[str, Any]) -> dict[str, Any] | None:
        if not server.get("credential_id"):
            return None
        credential = self.database.get_credential(server["credential_id"])
        if credential["kind"] == "password":
            return {
                "credential_id": credential["id"],
                "slot": "password",
                "alias": server["alias"],
                "hostname": server["hostname"],
                "username": server["username"],
            }
        if credential["kind"] == "key" and credential["has_passphrase"]:
            return {
                "credential_id": credential["id"],
                "slot": "passphrase",
                "alias": server["alias"],
                "hostname": server["hostname"],
                "username": server["username"],
                "key_path": credential["key_path"],
            }
        return None

    def _auth_descriptors(self, server: dict[str, Any]) -> list[dict[str, Any]]:
        descriptors: list[dict[str, Any]] = []
        visited: set[str] = set()

        def add(item: dict[str, Any]) -> None:
            key = item["alias"].casefold()
            if key in visited:
                return
            visited.add(key)
            for jump in item["proxy_jumps"]:
                try:
                    add(self.database.get_server(jump))
                except NotFoundError:
                    continue
            descriptor = self._credential_descriptor(item)
            if descriptor:
                descriptors.append(descriptor)

        add(server)
        return descriptors

    def _askpass_launcher(self) -> Path:
        directory = runtime_dir()
        if os.name == "nt":
            launcher = Path(sys.executable).parent / "ssh-server-manager-askpass.exe"
            if not launcher.exists():
                raise SSHError("Windows AskPass launcher is missing; run scripts\\bootstrap.cmd")
            return launcher
        else:
            # Invoke the packaged module so the launcher works both from a
            # source checkout and from a pipx/uv tool install. The package
            # parent goes on PYTHONPATH because ssh launches this in a fresh
            # process that has no knowledge of how serverctl was started.
            package_parent = Path(__file__).resolve().parents[1]
            launcher = directory / "askpass"
            content = (
                "#!/bin/sh\n"
                f'PYTHONPATH={shlex.quote(str(package_parent))}${{PYTHONPATH:+:$PYTHONPATH}}\n'
                "export PYTHONPATH\n"
                f'exec {shlex.quote(sys.executable)} -m ssh_server_manager.askpass "$@"\n'
            )
        if not launcher.exists() or launcher.read_text(encoding="utf-8") != content:
            launcher.write_text(content, encoding="utf-8", newline="\n")
            if os.name != "nt":
                launcher.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return launcher

    def _environment(self, server: dict[str, Any]) -> dict[str, str]:
        environment = os.environ.copy()
        descriptors = self._auth_descriptors(server)
        if descriptors:
            environment["SSH_ASKPASS"] = str(self._askpass_launcher())
            environment["SSH_ASKPASS_REQUIRE"] = "force"
            environment.setdefault("DISPLAY", "ssh-server-manager:0")
            environment["SSM_ASKPASS_MAP"] = json.dumps(descriptors, separators=(",", ":"))
        return environment

    def _base_command(self, server: dict[str, Any], *, timeout: int, reuse: int = 0) -> list[str]:
        config = render_config(self.database)
        command = [
            self.ssh_binary,
            "-F",
            str(config),
            "-o",
            f"ConnectTimeout={timeout}",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "GSSAPIAuthentication=no",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "LogLevel=ERROR",
        ]
        if reuse > 0 and os.name != "nt":
            command.extend(
                [
                    "-o",
                    "ControlMaster=auto",
                    "-o",
                    f"ControlPersist={reuse}",
                    "-o",
                    f"ControlPath={runtime_dir() / 'cm-%C'}",
                ]
            )
        command.append(server["alias"])
        return command

    def test(self, identifier: str, *, timeout: int = 8) -> dict[str, Any]:
        server = self.database.get_server(identifier)
        command = self._base_command(server, timeout=timeout)
        command[-1:-1] = ["-T", "-o", "StrictHostKeyChecking=yes", "-o", "NumberOfPasswordPrompts=1"]
        command.append("true")
        start = time.monotonic()
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                env=self._environment(server),
                timeout=timeout + 5,
                check=False,
            )
            latency = round((time.monotonic() - start) * 1000)
            success = result.returncode == 0
            error_code = None if success else classify_error(result.stderr, result.returncode)
            message = "connection succeeded" if success else redact(result.stderr.strip() or "SSH returned no diagnostics")
        except subprocess.TimeoutExpired:
            latency = round((time.monotonic() - start) * 1000)
            success, error_code, message = False, "timeout", f"connection exceeded {timeout + 5} seconds"
        except OSError as exc:
            latency = round((time.monotonic() - start) * 1000)
            success, error_code, message = False, "ssh-not-found", str(exc)
        self.database.record_test(
            server["id"],
            status="ok" if success else "failed",
            latency_ms=latency,
            error_code=error_code,
            message=message,
        )
        return {
            "alias": server["alias"],
            "ok": success,
            "latency_ms": latency,
            "error_code": error_code,
            "message": message,
        }

    def connect(self, identifier: str, *, timeout: int = 12) -> int:
        server = self.database.get_server(identifier)
        command = self._base_command(server, timeout=timeout)
        return subprocess.call(command, env=self._environment(server))

    def execute(
        self,
        identifier: str,
        remote_args: Sequence[str],
        *,
        stdin_data: str | bytes | None = None,
        timeout: int = 30,
        reuse: int = 0,
        capture: bool = False,
    ) -> dict[str, Any]:
        if not remote_args:
            raise SSHError("a remote command is required after --")
        server = self.database.get_server(identifier)
        command = self._base_command(server, timeout=timeout, reuse=reuse)
        command[-1:-1] = ["-T", "-o", "StrictHostKeyChecking=yes", "-o", "NumberOfPasswordPrompts=1"]
        command.append(shlex.join(list(remote_args)))
        start = time.monotonic()
        binary_input = isinstance(stdin_data, bytes)
        result = subprocess.run(
            command,
            input=stdin_data,
            text=not binary_input,
            capture_output=capture,
            env=self._environment(server),
            check=False,
        )
        output = {
            "alias": server["alias"],
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "latency_ms": round((time.monotonic() - start) * 1000),
        }
        if capture:
            stdout = result.stdout
            stderr = result.stderr
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            output["stdout"] = stdout
            output["stderr"] = redact(stderr)
            if result.returncode:
                output["error_code"] = classify_error(stderr, result.returncode)
        return output
