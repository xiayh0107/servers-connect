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
import sysconfig
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from .db import Database, NotFoundError, utc_now
from .host_metadata import sanitize_facts, sanitize_text
from .paths import runtime_dir
from .probe_metadata import collect_probe_metadata, connection_diagnostics
from .ssh_config import render_config
from .validation import ValidationError, validate_remote_path
from .remote_files import (
    MAX_METADATA_BYTES, METADATA_PREFIX, METADATA_SCRIPT, clean_remote_text,
    file_error_code, normalize_metadata, run_metadata_command, validate_web_file_path,
)


class SSHError(RuntimeError):
    pass


class FileOperationError(SSHError):
    """A stable file-operation failure, including optional conflict metadata."""

    def __init__(self, message: str, error_code: str, *, existing: dict[str, Any] | None = None) -> None:
        super().__init__(clean_remote_text(redact(message), 4000))
        self.error_code = error_code
        self.existing = existing


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
# Transfers get a wider wall-clock margin than the connect timeout: a large
# file is slow for reasons that have nothing to do with reachability.
TRANSFER_TIMEOUT_GRACE = 30


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
    if raw.get("os_pretty"):
        details["os_pretty"] = raw["os_pretty"]
    if os_id:
        details["os_id"] = os_id
    if os_version:
        details["os_version"] = os_version
    if raw.get("os_like"):
        details["os_like"] = raw["os_like"]
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
    return sanitize_facts(details)


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

    @staticmethod
    def _windows_askpass_candidates() -> list[Path]:
        """Every place pip may have put the askpass console script on Windows.

        A virtualenv puts it beside python.exe, but a base or --user install
        puts it in the sysconfig scripts directory instead, which is not the
        interpreter's own directory. Only checking beside python.exe made
        password authentication fail on those installs with a misleading
        "run bootstrap.cmd".
        """
        name = "ssh-server-manager-askpass.exe"
        candidates = [Path(sys.executable).parent / name]
        for scheme in ("nt", "nt_user"):
            try:
                scripts = sysconfig.get_path("scripts", scheme=scheme)
            except (KeyError, ValueError):
                continue
            if scripts:
                candidates.append(Path(scripts) / name)
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
        return candidates

    def _askpass_launcher(self) -> Path:
        directory = runtime_dir()
        if os.name == "nt":
            for candidate in self._windows_askpass_candidates():
                if candidate.exists():
                    return candidate
            raise SSHError("Windows AskPass launcher is missing; run scripts\\bootstrap.cmd")
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

    def _sftp_command(
        self, server: dict[str, Any], *, timeout: int, reuse: int, quiet: bool = True
    ) -> list[str]:
        config = render_config(self.database)
        command = [
            self.sftp_binary,
            # -q also suppresses the transfer progress meter, so transfers that
            # stream to a terminal turn it off. Parsed callers keep it: the
            # listing parser assumes stdout carries nothing but sftp output.
            *(["-q"] if quiet else []),
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

    @classmethod
    def _transfer_batch(cls, verb: str, first: str, second: str) -> str:
        """Build the sftp command stream for one get/put.

        "~" is never handed to sftp literally — it does not expand it — so a
        remote path anchored at home is reached by returning to the login
        directory with a bare @cd first, exactly as _directory_batch does.
        """
        prefix: list[str] = []
        remote_is_first = verb == "get"
        remote = first if remote_is_first else second
        if remote == "~" or remote.startswith("~/"):
            prefix.append("@cd")
            remote = remote[2:] if remote.startswith("~/") else "."
            if remote_is_first:
                first = remote
            else:
                second = remote
        transfer = f"@{verb} -p {cls._sftp_path(first)} {cls._sftp_path(second)}"
        return "\n".join([*prefix, transfer, "@quit", ""])

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
                    "modified": clean_remote_text(f'{values["month"]} {values["day"]} {values["when"]}'),
                    "permissions": permissions,
                    "owner": clean_remote_text(values["owner"]),
                    "group": clean_remote_text(values["group"]),
                    "uid": int(values["owner"]) if values["owner"].isascii() and values["owner"].isdigit() else None,
                    "gid": int(values["group"]) if values["group"].isascii() and values["group"].isdigit() else None,
                    "modified_at": None,
                    "link_target": None,
                    "hidden": name.startswith("."),
                }
            )
        if not current_directory:
            raise FileOperationError("SFTP did not report the remote working directory", "unreachable")
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
        name_filter: str | None = None,
        enrich: bool = True,
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
                expected_endpoint=(server["hostname"], server["port"]),
            )
            raise FileOperationError(f"remote directory listing exceeded {timeout + 5} seconds", "timeout") from exc
        except OSError as exc:
            latency = round((time.monotonic() - start) * 1000)
            self.database.record_test(
                server["id"],
                status="failed",
                latency_ms=latency,
                error_code="ssh-not-found",
                message="sftp is unavailable; install the OpenSSH client and retry",
                expected_endpoint=(server["hostname"], server["port"]),
            )
            raise FileOperationError("sftp is unavailable; install the OpenSSH client and retry", "unreachable") from exc
        latency = round((time.monotonic() - start) * 1000)
        diagnostics = sanitize_text(redact(result.stderr), limit=4000)
        # Preserve line boundaries for recognition only; displayed/persisted
        # diagnostics strip controls and Unicode formatting characters.
        operation_failed = bool(SFTP_OPERATION_ERROR_RE.search(result.stderr))
        if result.returncode != 0:
            self.database.record_test(
                server["id"],
                status="failed",
                latency_ms=latency,
                error_code=classify_error(diagnostics, result.returncode),
                message=diagnostics or f"SFTP exited with status {result.returncode}",
                expected_endpoint=(server["hostname"], server["port"]),
            )
            raise FileOperationError(diagnostics or f"SFTP exited with status {result.returncode}", file_error_code(diagnostics, result.returncode))
        connection = self.database.record_test(
            server["id"],
            status="ok",
            latency_ms=latency,
            error_code=None,
            message="SFTP connection succeeded",
            expected_endpoint=(server["hostname"], server["port"]),
        )
        if (connection["hostname"], connection["port"]) != (server["hostname"], server["port"]):
            raise FileOperationError("host address changed during the file operation; retry", "unreachable")
        if operation_failed:
            raise FileOperationError(diagnostics or "remote directory operation failed", file_error_code(diagnostics))
        current_directory, entries, unparsed = self._parse_directory_listing(result.stdout)
        total = len(entries)
        if name_filter:
            needle = name_filter.casefold()
            entries = [entry for entry in entries if needle in entry["name"].casefold()]
        matching_total = len(entries)
        visible_entries = entries[:MAX_DIRECTORY_ENTRIES]
        if enrich and visible_entries:
            try:
                metadata = self._file_metadata(
                    server, {"mode": "entries", "path": current_directory,
                             "names": [entry["name"] for entry in visible_entries]},
                    timeout=timeout, reuse=reuse,
                )
            except FileOperationError:
                metadata = None
            extras = metadata.get("entries", {}) if metadata else {}
            if isinstance(extras, dict):
                for entry in visible_entries:
                    extra = normalize_metadata(extras.get(entry["name"]))
                    # Do not combine metadata from a replaced file or a shell
                    # namespace that differs from the SFTP directory.
                    if (extra and extra["type"] == entry["type"] and extra["size"] == entry["size"]
                            and all(entry[key] is None or entry[key] == extra[key] for key in ("uid", "gid"))):
                        for key in ("uid", "gid", "modified_at", "link_target", "owner", "group"):
                            if extra[key] is not None:
                                entry[key] = extra[key]
        normalized = current_directory.rstrip("/") or "/"
        parent = None if normalized == "/" else posixpath.dirname(normalized) or "/"
        return {
            "alias": server["alias"],
            "path": normalized,
            "parent": parent,
            "entries": visible_entries,
            "total": total,
            "matching_total": matching_total,
            "truncated": matching_total > len(visible_entries),
            "unparsed": unparsed,
            "latency_ms": latency,
            "connection_checked_at": connection["last_test_at"],
        }

    def _file_metadata(
        self, server: dict[str, Any], request: dict[str, Any], *, timeout: int, reuse: int,
    ) -> dict[str, Any] | None:
        """One bounded optional SSH metadata probe, using the managed route."""
        request_json = json.dumps(request, ensure_ascii=True)
        if len(request_json) > 2 * 1024 * 1024:
            return None
        command = self._base_command(server, timeout=timeout, reuse=reuse)
        command[-1:-1] = ["-T", "-o", "StrictHostKeyChecking=yes", "-o", "NumberOfPasswordPrompts=1"]
        command.append("python3 -c " + shlex.quote(METADATA_SCRIPT))
        try:
            result, exceeded = run_metadata_command(
                command, request=request_json, env=self._environment(server), timeout=timeout + 5,
            )
        except subprocess.TimeoutExpired as exc:
            raise FileOperationError("remote metadata check timed out", "timeout") from exc
        except OSError:
            return None
        if exceeded:
            return None
        if result.returncode == 255:
            raise FileOperationError("remote metadata connection failed", file_error_code(result.stderr, result.returncode))
        if result.returncode != 0 or len(result.stdout) > MAX_METADATA_BYTES:
            return None
        for line in reversed(result.stdout.splitlines()):
            if line.startswith(METADATA_PREFIX):
                try:
                    document = json.loads(line[len(METADATA_PREFIX):])
                except (ValueError, TypeError):
                    return None
                if not isinstance(document, dict):
                    return None
                if document.get("ok") is False:
                    code = document.get("error_code")
                    if code not in {"not_found", "not_a_directory", "permission_denied", "exists", "unreachable"}:
                        code = "unreachable"
                    raise FileOperationError(f"remote file metadata check failed: {code.replace('_', ' ')}", code)
                return document if document.get("ok") is True else None
        return None

    def stat_file(
        self, identifier: str, path: str, *, timeout: int = 12, reuse: int = 300,
        follow_symlinks: bool = True,
    ) -> dict[str, Any]:
        """Read remote file metadata before a bounded download or safe upload.

        The SFTP parent listing is authoritative even if a shell is chrooted
        differently. A symlink's target size cannot be established by that
        listing, so bounded downloads fail closed for symlinks.
        """
        remote = validate_web_file_path(path)
        # SFTP and an SSH shell can have different filesystem namespaces (e.g.
        # chroot). Only SFTP metadata may authorize a transfer or overwrite.
        parent = posixpath.dirname(remote) or "."
        # Browsing allows the current directory, whereas web destinations do not.
        listing = self.list_directory(
            identifier, parent, timeout=timeout, reuse=reuse,
            name_filter=posixpath.basename(remote), enrich=False,
        )
        entry = next((item for item in listing["entries"] if item["name"] == posixpath.basename(remote)), None)
        if entry is None:
            if listing["truncated"]:
                raise FileOperationError("cannot verify a file beyond the directory limit; use the CLI", "permission_denied")
            raise FileOperationError("remote file was not found", "not_found")
        if follow_symlinks and entry["type"] == "symlink":
            raise FileOperationError("cannot verify a symlink target size on this host; use the CLI", "permission_denied")
        return entry

    def _run_transfer(
        self,
        server: dict[str, Any],
        *,
        batch: str,
        timeout: int,
        reuse: int,
        stream: bool,
        batch_mode: bool = False,
    ) -> tuple[int, str, str]:
        """Run one sftp transfer, either streaming to the terminal or captured.

        Streaming keeps sftp's own progress meter, which is the only progress
        signal available without reimplementing the protocol.
        """
        command = self._sftp_command(server, timeout=timeout, reuse=reuse, quiet=not stream)
        if batch_mode:
            # Abort before publishing if put failed; explicit BatchMode=no in
            # _sftp_command still permits managed AskPass authentication.
            command[-1:-1] = ["-b", "-"]
        try:
            result = subprocess.run(
                command,
                input=batch,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=not stream,
                env=self._environment(server),
                timeout=timeout + TRANSFER_TIMEOUT_GRACE,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FileOperationError(
                f"transfer exceeded {timeout + TRANSFER_TIMEOUT_GRACE} seconds", "timeout"
            ) from exc
        except OSError as exc:
            raise FileOperationError("sftp is unavailable; install the OpenSSH client and retry", "unreachable") from exc
        return result.returncode, result.stdout or "", result.stderr or ""

    def _finish_transfer(self, returncode: int, stderr: str) -> None:
        diagnostics = sanitize_text(redact(stderr), limit=4000)
        if returncode != 0:
            raise FileOperationError(diagnostics or f"SFTP exited with status {returncode}", file_error_code(diagnostics, returncode))
        # sftp exits 0 even when the transfer itself failed, so stderr is the
        # only reliable signal. Same trap list_directory works around.
        if SFTP_OPERATION_ERROR_RE.search(stderr):
            raise FileOperationError(diagnostics or "remote file transfer failed", file_error_code(diagnostics))

    def download(
        self,
        identifier: str,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout: int = 120,
        reuse: int = 300,
        stream: bool = False,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Copy one remote file to a local path over SFTP."""
        remote = validate_remote_path(remote_path)
        if remote == "~":
            raise ValidationError("remote source must name a file, not the home directory")
        destination = Path(local_path)
        server = self.database.get_server(identifier)
        start = time.monotonic()
        if max_bytes is not None:
            if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
                raise ValidationError("download limit must be a nonnegative integer")
            metadata = self.stat_file(identifier, remote, timeout=min(timeout, 12), reuse=reuse)
            if metadata["type"] != "file":
                raise FileOperationError("remote source must be a regular file", "permission_denied")
            if metadata["size"] > max_bytes:
                raise FileOperationError("file is larger than the web transfer limit; use serverctl get instead", "too_large")
        returncode, _stdout, stderr = self._run_transfer(
            server,
            batch=self._transfer_batch("get", remote, str(destination)),
            timeout=timeout,
            reuse=reuse,
            stream=stream,
        )
        self._finish_transfer(returncode, stderr)
        latency = round((time.monotonic() - start) * 1000)
        # sftp reports a failed get on stderr rather than by exit code, so a
        # missing destination here means the transfer silently did nothing.
        if not destination.exists():
            raise FileOperationError(f"transfer reported success but {destination} was not written", "unreachable")
        downloaded_size = destination.stat().st_size
        if max_bytes is not None and downloaded_size > max_bytes:
            raise FileOperationError("file grew beyond the web transfer limit; use serverctl get instead", "too_large")
        return {
            "alias": server["alias"],
            "direction": "download",
            "remote_path": remote,
            "local_path": str(destination),
            "bytes": downloaded_size,
            "latency_ms": latency,
        }

    def upload(
        self,
        identifier: str,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout: int = 120,
        reuse: int = 300,
        stream: bool = False,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Copy one local file to a remote path over SFTP."""
        remote = validate_remote_path(remote_path)
        source = Path(local_path)
        if not source.is_file():
            raise ValidationError(f"local file does not exist: {source}")
        size = source.stat().st_size
        server = self.database.get_server(identifier)
        start = time.monotonic()
        if not overwrite:
            validate_web_file_path(remote)
            try:
                existing = self.stat_file(identifier, remote, timeout=min(timeout, 12), reuse=reuse, follow_symlinks=False)
            except FileOperationError as exc:
                if exc.error_code != "not_found":
                    raise
            else:
                raise FileOperationError("remote destination already exists; confirm overwrite before retrying", "exists", existing=existing)
            temporary = posixpath.join(posixpath.dirname(remote), ".ssm-upload-" + uuid.uuid4().hex)
            prefix = "@cd\n" if remote.startswith("~/") else ""
            publication_target = remote[2:] if prefix else remote
            publication_temp = temporary[2:] if prefix else temporary
            batch = prefix + "\n".join([
                f"@put -p {self._sftp_path(str(source))} {self._sftp_path(publication_temp)}",
                f"@ln {self._sftp_path(publication_temp)} {self._sftp_path(publication_target)}",
                f"@rm {self._sftp_path(publication_temp)}", "",
            ])
            succeeded = False
            try:
                returncode, _stdout, stderr = self._run_transfer(
                    server, batch=batch, timeout=timeout, reuse=reuse,
                    stream=False, batch_mode=True,
                )
                try:
                    self._finish_transfer(returncode, stderr)
                except FileOperationError as failure:
                    # OpenSSH reports EEXIST as generic "remote link: Failure".
                    # Re-read the authoritative SFTP target to classify a race.
                    try:
                        existing = self.stat_file(identifier, remote, timeout=min(timeout, 12), reuse=reuse, follow_symlinks=False)
                    except FileOperationError:
                        raise failure
                    raise FileOperationError("remote destination already exists; confirm overwrite before retrying", "exists", existing=existing) from failure
                succeeded = True
            finally:
                if not succeeded:
                    # Batch aborts before rm on failure, so cleanup is a second
                    # operation over the shared master, never a target removal.
                    cleanup = prefix + f"@rm {self._sftp_path(publication_temp)}\n@quit\n"
                    try:
                        self._run_transfer(server, batch=cleanup, timeout=min(timeout, 12), reuse=reuse, stream=False)
                    except FileOperationError:
                        pass
            return {
                "alias": server["alias"], "direction": "upload", "remote_path": remote,
                "local_path": str(source), "bytes": size,
                "latency_ms": round((time.monotonic() - start) * 1000),
            }
        returncode, _stdout, stderr = self._run_transfer(
            server,
            batch=self._transfer_batch("put", str(source), remote),
            timeout=timeout,
            reuse=reuse,
            stream=stream,
        )
        self._finish_transfer(returncode, stderr)
        return {
            "alias": server["alias"],
            "direction": "upload",
            "remote_path": remote,
            "local_path": str(source),
            "bytes": size,
            "latency_ms": round((time.monotonic() - start) * 1000),
        }

    def test(self, identifier: str, *, timeout: int = 8) -> dict[str, Any]:
        server = self.database.get_server(identifier)
        command = self._base_command(server, timeout=timeout)
        # Debug metadata identifies the negotiated public host key, but never
        # enters user-facing failure messages or changes verification policy.
        command[command.index("LogLevel=ERROR")] = "LogLevel=DEBUG1"
        command[-1:-1] = ["-T", "-o", "StrictHostKeyChecking=yes", "-o", "NumberOfPasswordPrompts=1", "-o", "FingerprintHash=sha256"]
        command.append("true")
        start = time.monotonic()
        stderr = ""
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
            stderr = result.stderr
            success = result.returncode == 0
            diagnostics = connection_diagnostics(result.stderr)
            error_code = None if success else classify_error(diagnostics, result.returncode)
            message = "connection succeeded" if success else redact(diagnostics or "SSH returned no diagnostics")
        except subprocess.TimeoutExpired:
            latency = round((time.monotonic() - start) * 1000)
            success, error_code, message = False, "timeout", f"connection exceeded {timeout + 5} seconds"
        except OSError as exc:
            latency = round((time.monotonic() - start) * 1000)
            success, error_code, message = False, "ssh-not-found", str(exc)
        message = sanitize_text(redact(message), limit=4000)
        self.database.record_test(
            server["id"],
            status="ok" if success else "failed",
            latency_ms=latency,
            error_code=error_code,
            message=message,
            expected_endpoint=(server["hostname"], server["port"]),
        )
        observation = collect_probe_metadata(
            server, ssh_binary=self.ssh_binary, config=command[command.index("-F") + 1],
            success=success, stderr=stderr, timeout=min(timeout, 2),
        )
        self.database.record_observation(
            server["id"], expected_endpoint=(server["hostname"], server["port"]), **observation,
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
            "details": sanitize_facts(details),
        }

    def diagnose(self, identifier: str, *, timeout: int = 8) -> dict[str, Any]:
        """Run a bounded, read-only host diagnosis for the CLI and web UI."""
        server = self.database.get_server(identifier)
        proxy_jumps = list(server.get("proxy_jumps", []))
        external_jumps = []
        for jump in proxy_jumps:
            try:
                self.database.get_server(jump)
            except NotFoundError:
                external_jumps.append(jump)

        checks: list[dict[str, Any]] = [
            {
                "id": "profile",
                "label": "Host profile",
                "status": "warning" if external_jumps else "ok",
                "message": (
                    f"External ProxyJump aliases are resolved by OpenSSH configuration: {', '.join(external_jumps)}"
                    if external_jumps
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
            if remote_result["ok"] and remote_result.get("details"):
                facts = sanitize_facts(remote_result["details"])
                remote_result["details"] = facts
                self.database.record_observation(
                    server["id"], facts=facts, observed_at=facts["observed_at"],
                    expected_endpoint=(server["hostname"], server["port"]),
                )
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
