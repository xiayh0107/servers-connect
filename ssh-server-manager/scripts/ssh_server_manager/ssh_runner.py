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

from .db import Database, NotFoundError, utc_now
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


# (os-release ID prefix, family, conventional package manager). Prefix matching
# covers variants such as opensuse-leap/opensuse-tumbleweed.
_OS_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("ubuntu", "debian", "apt"),
    ("debian", "debian", "apt"),
    ("linuxmint", "debian", "apt"),
    ("raspbian", "debian", "apt"),
    ("pop", "debian", "apt"),
    ("kali", "debian", "apt"),
    ("fedora", "rhel", "dnf"),
    ("rhel", "rhel", "dnf"),
    ("centos", "rhel", "dnf"),
    ("rocky", "rhel", "dnf"),
    ("almalinux", "rhel", "dnf"),
    ("ol", "rhel", "dnf"),
    ("amzn", "rhel", "dnf"),
    ("opensuse", "suse", "zypper"),
    ("sles", "suse", "zypper"),
    ("suse", "suse", "zypper"),
    ("arch", "arch", "pacman"),
    ("manjaro", "arch", "pacman"),
    ("endeavouros", "arch", "pacman"),
    ("alpine", "alpine", "apk"),
    ("gentoo", "gentoo", "emerge"),
    ("nixos", "nixos", "nix"),
    ("void", "void", "xbps"),
    ("openwrt", "openwrt", "opkg"),
    ("freebsd", "bsd", "pkg"),
    ("openbsd", "bsd", "pkg_add"),
    ("netbsd", "bsd", "pkgin"),
)


def _classify_os_id(candidates: Sequence[str]) -> tuple[str, str] | None:
    for candidate in candidates:
        token = candidate.strip().casefold()
        if not token:
            continue
        for prefix, family, manager in _OS_FAMILIES:
            if token == prefix or token.startswith(prefix + "-"):
                return family, manager
    return None


def _summarize_remote_identity(raw: dict[str, str]) -> dict[str, str]:
    """Turn raw probe output into user-facing system-identity fields.

    Sources are tried from most to least specific: sw_vers (macOS),
    os-release (Linux and modern BSDs), freebsd-version, legacy release
    files, and finally the kernel name.
    """
    details: dict[str, str] = {}
    for key in ("hostname", "uptime", "load", "cpu_cores", "cpu_model", "memory", "disk"):
        if raw.get(key):
            details[key] = raw[key]

    os_id = raw.get("os_id", "").strip().casefold()
    os_version = raw.get("os_version", "")
    kernel_name = raw.get("kernel_name", "")
    name = ""
    family: tuple[str, str] | None = None

    if raw.get("mac_product"):
        name = " ".join(part for part in (raw["mac_product"], raw.get("mac_version", "")) if part)
        os_id = os_id or "macos"
        os_version = os_version or raw.get("mac_version", "")
        family = ("macos", "brew") if raw.get("brew") else ("macos", "")
    elif raw.get("os_pretty") or os_id:
        name = raw.get("os_pretty") or " ".join(part for part in (os_id, os_version) if part)
        family = _classify_os_id([os_id, *raw.get("os_like", "").split()])
    elif raw.get("bsd_version"):
        name = f"FreeBSD {raw['bsd_version']}"
        os_id = "freebsd"
        os_version = os_version or raw["bsd_version"]
        family = ("bsd", "pkg")
    elif raw.get("os_legacy"):
        name = raw["os_legacy"]
        lowered = name.casefold()
        if "red hat" in lowered or "centos" in lowered:
            family = ("rhel", "yum")
    elif kernel_name:
        name = kernel_name
        family = _classify_os_id([kernel_name])

    if name:
        details["os"] = name
    if os_id:
        details["os_id"] = os_id
    if os_version:
        details["os_version"] = os_version
    if family:
        details["os_family"] = family[0]
        if family[1]:
            details["package_manager"] = family[1]
    if raw.get("kernel"):
        details["kernel"] = raw["kernel"]
    elif kernel_name:
        details["kernel"] = kernel_name
    if raw.get("arch"):
        details["arch"] = raw["arch"]
    return details


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

    def _probe_sftp(self, server: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        """Verify the SFTP subsystem and the user's home directory without listing files."""
        command = self._sftp_command(server, timeout=timeout, reuse=0)
        start = time.monotonic()
        try:
            result = subprocess.run(
                command,
                input="@cd\n@pwd\n@quit\n",
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                env=self._environment(server),
                timeout=timeout + 5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "latency_ms": round((time.monotonic() - start) * 1000),
                "error_code": "timeout",
                "message": f"SFTP probe exceeded {timeout + 5} seconds",
            }
        except OSError as exc:
            return {
                "ok": False,
                "latency_ms": round((time.monotonic() - start) * 1000),
                "error_code": "ssh-not-found",
                "message": str(exc),
            }

        latency = round((time.monotonic() - start) * 1000)
        diagnostics = redact(result.stderr.strip())
        if result.returncode != 0:
            return {
                "ok": False,
                "latency_ms": latency,
                "error_code": classify_error(result.stderr, result.returncode),
                "message": diagnostics or f"SFTP exited with status {result.returncode}",
            }
        try:
            current_directory, _entries, _unparsed = self._parse_directory_listing(result.stdout)
        except SSHError as exc:
            return {
                "ok": False,
                "latency_ms": latency,
                "error_code": "sftp-failed",
                "message": str(exc),
            }
        if SFTP_OPERATION_ERROR_RE.search(diagnostics):
            return {
                "ok": False,
                "latency_ms": latency,
                "error_code": "sftp-failed",
                "message": diagnostics or "SFTP home directory probe failed",
            }
        return {
            "ok": True,
            "latency_ms": latency,
            "message": "SFTP home directory probe succeeded",
            "details": {"home_directory": current_directory.rstrip("/") or "/"},
        }

    def _probe_remote_identity(self, server: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        """Collect a small, non-sensitive system summary through a fixed command."""
        # Every source is optional and best-effort: os-release covers Linux and
        # modern BSDs, sw_vers exists only on macOS, freebsd-version and the
        # legacy release files cover older systems. uname -o is not portable
        # (absent on older macOS/BSD), so kernel facts use -srm/-s/-m only.
        script = (
            "printf '__ssm_hostname='; hostname 2>/dev/null || true; printf '\\n'; "
            "printf '__ssm_os_pretty='; sed -n \"s/^PRETTY_NAME=//p\" /etc/os-release /usr/lib/os-release 2>/dev/null | head -n 1 || true; printf '\\n'; "
            "printf '__ssm_os_id='; sed -n \"s/^ID=//p\" /etc/os-release /usr/lib/os-release 2>/dev/null | head -n 1 || true; printf '\\n'; "
            "printf '__ssm_os_version='; sed -n \"s/^VERSION_ID=//p\" /etc/os-release /usr/lib/os-release 2>/dev/null | head -n 1 || true; printf '\\n'; "
            "printf '__ssm_os_like='; sed -n \"s/^ID_LIKE=//p\" /etc/os-release /usr/lib/os-release 2>/dev/null | head -n 1 || true; printf '\\n'; "
            "printf '__ssm_os_legacy='; cat /etc/redhat-release /etc/system-release 2>/dev/null | head -n 1 || true; printf '\\n'; "
            "printf '__ssm_mac_product='; sw_vers -productName 2>/dev/null || true; printf '\\n'; "
            "printf '__ssm_mac_version='; sw_vers -productVersion 2>/dev/null || true; printf '\\n'; "
            "printf '__ssm_brew='; command -v brew >/dev/null 2>&1 && printf 'brew'; printf '\\n'; "
            "printf '__ssm_bsd_version='; freebsd-version 2>/dev/null || true; printf '\\n'; "
            "printf '__ssm_kernel_name='; uname -s 2>/dev/null || true; printf '\\n'; "
            "printf '__ssm_kernel='; uname -srm 2>/dev/null || true; printf '\\n'; "
            "printf '__ssm_arch='; uname -m 2>/dev/null || true; printf '\\n'; "
            "printf '__ssm_uptime='; uptime -p 2>/dev/null || uptime 2>/dev/null || true; printf '\\n'; "
            "printf '__ssm_load='; cut -d\" \" -f1-3 /proc/loadavg 2>/dev/null || true; printf '\\n'; "
            "printf '__ssm_cpu_cores='; nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || true; printf '\\n'; "
            "printf '__ssm_cpu_model='; grep -m1 \"model name\" /proc/cpuinfo 2>/dev/null | cut -d: -f2- || true; printf '\\n'; "
            "printf '__ssm_memory='; free -h 2>/dev/null | grep \"^Mem:\" || true; printf '\\n'; "
            "printf '__ssm_disk='; df -hP / 2>/dev/null | tail -n 1 || true; printf '\\n'"
        )
        result = self.execute(
            server["id"],
            ["sh", "-lc", script],
            timeout=timeout,
            capture=True,
        )
        if not result["ok"]:
            # A host without a POSIX sh (Windows OpenSSH with a cmd/powershell
            # default shell) fails here with an unclassified error; try to
            # identify it before reporting a failure. Connection-level errors
            # (timeout, auth, dns, ...) are already classified and skip this.
            if result.get("error_code") == "ssh-failed":
                windows = self._probe_windows_identity(server, timeout=timeout)
                if windows is not None:
                    return windows
            return {
                "ok": False,
                "latency_ms": result["latency_ms"],
                "error_code": result.get("error_code", "ssh-failed"),
                "message": result.get("stderr") or "Remote system probe failed",
            }

        values: dict[str, str] = {}
        for line in str(result.get("stdout", "")).splitlines():
            for key in (
                "hostname",
                "os_pretty",
                "os_id",
                "os_version",
                "os_like",
                "os_legacy",
                "mac_product",
                "mac_version",
                "brew",
                "bsd_version",
                "kernel_name",
                "kernel",
                "arch",
                "uptime",
                "load",
                "cpu_cores",
                "cpu_model",
                "memory",
                "disk",
            ):
                prefix = f"__ssm_{key}="
                if line.startswith(prefix):
                    value = line[len(prefix) :].strip().strip('"')
                    if value:
                        values[key] = value
                    break
        if not values.get("hostname"):
            return {
                "ok": False,
                "latency_ms": result["latency_ms"],
                "error_code": "remote-probe-failed",
                "message": "Remote system probe returned no hostname",
            }
        return {
            "ok": True,
            "latency_ms": result["latency_ms"],
            "message": "Remote system responded",
            "details": _summarize_remote_identity(values),
        }

    def _probe_windows_identity(self, server: dict[str, Any], *, timeout: int) -> dict[str, Any] | None:
        """Identify a Windows host whose default shell rejected the POSIX probe."""
        probe = self.execute(server["id"], ["cmd.exe", "/c", "ver"], timeout=timeout, capture=True)
        stdout = " ".join(str(probe.get("stdout", "")).split())
        if not probe["ok"] or "windows" not in stdout.casefold():
            return None
        details = {"os": stdout, "os_id": "windows", "os_family": "windows"}
        version = re.search(r"version\s+([\d.]+)", stdout, re.IGNORECASE)
        if version:
            details["os_version"] = version.group(1)
        hostname = self.execute(server["id"], ["hostname"], timeout=timeout, capture=True)
        hostname_value = str(hostname.get("stdout", "")).strip()
        if hostname["ok"] and hostname_value:
            details["hostname"] = hostname_value
        return {
            "ok": True,
            "latency_ms": probe["latency_ms"],
            "message": "Remote system responded (Windows)",
            "details": details,
        }

    def diagnose(self, identifier: str, *, timeout: int = 8) -> dict[str, Any]:
        """Run a bounded, read-only host diagnosis for the CLI and web UI."""
        server = self.database.get_server(identifier)
        proxy_jumps = list(server.get("proxy_jumps", []))
        missing_jumps = []
        for jump in proxy_jumps:
            try:
                self.database.get_server(jump)
            except NotFoundError:
                missing_jumps.append(jump)

        checks: list[dict[str, Any]] = [
            {
                "id": "profile",
                "label": "Host profile",
                "status": "failed" if missing_jumps else "ok",
                "message": (
                    f"ProxyJump alias not found: {', '.join(missing_jumps)}"
                    if missing_jumps
                    else "Local host profile is valid"
                ),
                "details": {
                    "endpoint": f"{server['username']}@{server['hostname']}:{server['port']}",
                    "proxy_jumps": proxy_jumps,
                    "credential": server.get("credential_label") or "OpenSSH default / agent",
                    "host_key_policy": "StrictHostKeyChecking=yes",
                },
            }
        ]

        ssh_result = self.test(server["id"], timeout=timeout)
        checks.append(
            {
                "id": "ssh",
                "label": "SSH handshake",
                "status": "ok" if ssh_result["ok"] else "failed",
                "message": ssh_result["message"],
                "latency_ms": ssh_result["latency_ms"],
                **({"error_code": ssh_result["error_code"]} if ssh_result.get("error_code") else {}),
            }
        )

        if ssh_result["ok"]:
            sftp_result = self._probe_sftp(server, timeout=timeout)
            checks.append(
                {
                    "id": "sftp",
                    "label": "SFTP subsystem",
                    "status": "ok" if sftp_result["ok"] else "failed",
                    "message": sftp_result["message"],
                    "latency_ms": sftp_result["latency_ms"],
                    **(
                        {"error_code": sftp_result["error_code"]}
                        if sftp_result.get("error_code")
                        else {}
                    ),
                    **({"details": sftp_result["details"]} if sftp_result.get("details") else {}),
                }
            )
            remote_result = self._probe_remote_identity(server, timeout=timeout)
            checks.append(
                {
                    "id": "remote",
                    "label": "Remote system",
                    "status": "ok" if remote_result["ok"] else "failed",
                    "message": remote_result["message"],
                    "latency_ms": remote_result["latency_ms"],
                    **(
                        {"error_code": remote_result["error_code"]}
                        if remote_result.get("error_code")
                        else {}
                    ),
                    **({"details": remote_result["details"]} if remote_result.get("details") else {}),
                }
            )
        else:
            for check_id, label in (("sftp", "SFTP subsystem"), ("remote", "Remote system")):
                checks.append(
                    {
                        "id": check_id,
                        "label": label,
                        "status": "skipped",
                        "message": "Skipped because the SSH handshake failed",
                    }
                )

        failed = sum(check["status"] == "failed" for check in checks)
        warnings = sum(check["status"] == "warning" for check in checks)
        skipped = sum(check["status"] == "skipped" for check in checks)
        passed = sum(check["status"] == "ok" for check in checks)
        overall = "failed" if failed else "warning" if warnings else "ok"
        return {
            "alias": server["alias"],
            "checked_at": utc_now(),
            "overall": overall,
            "summary": {
                "total": len(checks),
                "passed": passed,
                "failed": failed,
                "warnings": warnings,
                "skipped": skipped,
            },
            "checks": checks,
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
        try:
            result = subprocess.run(
                command,
                input=stdin_data,
                text=not binary_input,
                capture_output=capture,
                env=self._environment(server),
                timeout=timeout + 5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            output = {
                "alias": server["alias"],
                "ok": False,
                "returncode": 124,
                "latency_ms": round((time.monotonic() - start) * 1000),
                "error_code": "timeout",
            }
            if capture:
                output.update({"stdout": "", "stderr": f"remote command exceeded {timeout + 5} seconds"})
            return output
        except OSError as exc:
            output = {
                "alias": server["alias"],
                "ok": False,
                "returncode": 127,
                "latency_ms": round((time.monotonic() - start) * 1000),
                "error_code": "ssh-not-found",
            }
            if capture:
                output.update({"stdout": "", "stderr": str(exc)})
            return output
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
