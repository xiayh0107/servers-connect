from __future__ import annotations

import json
import os
import posixpath
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
from .validation import validate_remote_path


class SSHError(RuntimeError):
    pass


PROMPT_REDACTIONS = [
    (re.compile(r"(?i)(password|passphrase)(\s*[:=]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(token|secret|cookie)(\s*[:=]\s*)\S+"), r"\1\2[REDACTED]"),
]

SFTP_WORKING_DIRECTORY_PREFIX = "Remote working directory: "
SFTP_LISTING_RE = re.compile(
    r"^(?P<permissions>[bcdlps-][rwxStTs-]{9}[+@.]?)\s+"
    r"\S+\s+(?P<owner>\S+)\s+(?P<group>\S+)\s+(?P<size>\d+)\s+"
    r"(?P<month>\S+)\s+(?P<day>\d{1,2})\s+(?P<when>\S+)\s(?P<name>.*)$"
)
SFTP_OPERATION_ERROR_RE = re.compile(
    r"^(?:realpath |remote (?:opendir|readdir|stat)|stat remote:|Couldn't |Can't )",
    re.MULTILINE,
)
MAX_DIRECTORY_ENTRIES = 5000


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
    def __init__(
        self,
        database: Database,
        *,
        ssh_binary: str | None = None,
        sftp_binary: str | None = None,
    ) -> None:
        self.database = database
        self.ssh_binary = ssh_binary or shutil.which("ssh") or "ssh"
        self.sftp_binary = sftp_binary or shutil.which("sftp") or "sftp"

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

    def _sftp_command(self, server: dict[str, Any], *, timeout: int, reuse: int) -> list[str]:
        config = render_config(self.database)
        command = [
            self.sftp_binary,
            "-q",
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
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "BatchMode=no",
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

    @staticmethod
    def _sftp_path(path: str) -> str:
        """Quote one literal path for OpenSSH sftp's command parser."""
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @classmethod
    def _directory_batch(cls, path: str) -> str:
        if path == "~":
            changes = ["@cd"]
        elif path.startswith("~/"):
            changes = ["@cd", f"@cd {cls._sftp_path(path[2:])}"]
        else:
            changes = [f"@cd {cls._sftp_path(path)}"]
        return "\n".join([*changes, "@pwd", "@ls -lan", "@quit", ""])

    @staticmethod
    def _parse_directory_listing(stdout: str) -> tuple[str, list[dict[str, Any]], int]:
        current_directory = ""
        entries: list[dict[str, Any]] = []
        unparsed = 0
        for line in stdout.splitlines():
            if line.startswith(SFTP_WORKING_DIRECTORY_PREFIX):
                current_directory = line.removeprefix(SFTP_WORKING_DIRECTORY_PREFIX)
                continue
            match = SFTP_LISTING_RE.match(line)
            if not match:
                if line.strip():
                    unparsed += 1
                continue
            values = match.groupdict()
            name = values["name"]
            if name in {".", ".."}:
                continue
            permissions = values["permissions"][:10]
            kind = {
                "d": "directory",
                "l": "symlink",
                "-": "file",
            }.get(permissions[0], "special")
            entries.append(
                {
                    "name": name,
                    "type": kind,
                    "size": int(values["size"]),
                    "modified": f'{values["month"]} {values["day"]} {values["when"]}',
                    "permissions": permissions,
                    "owner": values["owner"],
                    "group": values["group"],
                    "hidden": name.startswith("."),
                }
            )
        if not current_directory:
            raise SSHError("SFTP did not report the remote working directory")
        for entry in entries:
            entry["path"] = posixpath.join(current_directory, entry["name"])
        entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
        return current_directory, entries, unparsed

    def list_directory(
        self,
        identifier: str,
        path: str | None = None,
        *,
        timeout: int = 12,
        reuse: int = 300,
    ) -> dict[str, Any]:
        """List one remote directory through the OpenSSH SFTP subsystem."""
        remote_path = validate_remote_path(path)
        server = self.database.get_server(identifier)
        command = self._sftp_command(server, timeout=timeout, reuse=reuse)
        start = time.monotonic()
        try:
            result = subprocess.run(
                command,
                input=self._directory_batch(remote_path),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                env=self._environment(server),
                timeout=timeout + 5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            latency = round((time.monotonic() - start) * 1000)
            self.database.record_test(
                server["id"],
                status="failed",
                latency_ms=latency,
                error_code="timeout",
                message=f"remote directory listing exceeded {timeout + 5} seconds",
            )
            raise SSHError(f"remote directory listing exceeded {timeout + 5} seconds") from exc
        except OSError as exc:
            latency = round((time.monotonic() - start) * 1000)
            self.database.record_test(
                server["id"],
                status="failed",
                latency_ms=latency,
                error_code="ssh-not-found",
                message="sftp is unavailable; install the OpenSSH client and retry",
            )
            raise SSHError("sftp is unavailable; install the OpenSSH client and retry") from exc
        latency = round((time.monotonic() - start) * 1000)
        diagnostics = redact(result.stderr.strip())
        operation_failed = bool(SFTP_OPERATION_ERROR_RE.search(diagnostics))
        if result.returncode != 0:
            self.database.record_test(
                server["id"],
                status="failed",
                latency_ms=latency,
                error_code=classify_error(result.stderr, result.returncode),
                message=diagnostics or f"SFTP exited with status {result.returncode}",
            )
            raise SSHError(diagnostics or f"SFTP exited with status {result.returncode}")
        connection = self.database.record_test(
            server["id"],
            status="ok",
            latency_ms=latency,
            error_code=None,
            message="SFTP connection succeeded",
        )
        if operation_failed:
            raise SSHError(diagnostics or "remote directory operation failed")
        current_directory, entries, unparsed = self._parse_directory_listing(result.stdout)
        total = len(entries)
        visible_entries = entries[:MAX_DIRECTORY_ENTRIES]
        normalized = current_directory.rstrip("/") or "/"
        parent = None if normalized == "/" else posixpath.dirname(normalized) or "/"
        return {
            "alias": server["alias"],
            "path": normalized,
            "parent": parent,
            "entries": visible_entries,
            "total": total,
            "truncated": total > len(visible_entries),
            "unparsed": unparsed,
            "latency_ms": latency,
            "connection_checked_at": connection["last_test_at"],
        }

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
