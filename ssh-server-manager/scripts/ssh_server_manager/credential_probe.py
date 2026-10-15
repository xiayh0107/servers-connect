"""Authenticate a selected credential without changing a saved host binding.

Success requires both a fixed LocalCommand callback and OpenSSH's private
local log. SSH authentication banners go to stderr, not the -E log; stderr
can never establish authentication success. The same connection's actual
method is checked, including OpenSSH's initial implicit `none` attempt.
"""
from __future__ import annotations

import fnmatch
import getpass
import glob
import json
import math
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .db import Database, utc_now
from .paths import original_ssh_config_path, runtime_dir
from .ssh_config import quote_value, split_config_line
from .ssh_runner import SSHRunner, classify_error
from .validation import ValidationError, validate_alias, validate_hostname, validate_port, validate_username
from .vault import VaultProtocol, get_vault


_MESSAGES = {
    "authentication-failed": "The selected credential was not accepted.",
    "host-key-untrusted": "The host key is untrusted or has changed; verify it before testing again.",
    "timeout": "Credential authentication exceeded its deadline.",
    "cancelled": "Credential authentication was cancelled.",
    "credential-not-used": "The server accepted authentication without using a credential.",
    "authentication-unconfirmed": "OpenSSH did not provide a trusted authentication result.",
    "connection-refused": "The SSH endpoint refused the connection.",
    "dns-failed": "The SSH endpoint could not be resolved.",
    "network-unreachable": "The SSH endpoint is unreachable.",
    "ssh-not-found": "The OpenSSH client is unavailable.",
    "ssh-failed": "SSH authentication could not be completed.",
    "external-config-unsupported": "The external jump configuration cannot be safely resolved for this test.",
    "vault-unavailable": "A required credential secret could not be read from the credential vault.",
    "credential-secret-missing": "The selected password credential has no stored secret.",
}


class _ProbeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_MESSAGES.get(code, _MESSAGES["ssh-failed"]))


def _private_write(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)


def _shell_command(arguments: list[str]) -> str:
    # OpenSSH performs percent expansion before invoking the local shell.
    result = subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
    return result.replace("%", "%%")


def _external_config(alias: str, path: Path, *, trust_only: bool = False,
                     inherit_identities: bool = False) -> dict[str, Any]:
    """Read common external profiles without running ssh -G / Match exec."""
    values: dict[str, str] = {}
    seen: set[Path] = set()
    allowed = {"hostname", "port", "user", "identityfile", "identityagent", "proxyjump",
               "userknownhostsfile", "globalknownhostsfile", "hostkeyalias", "identitiesonly"}
    if trust_only:
        allowed = {"userknownhostsfile", "globalknownhostsfile", "hostkeyalias"}
        if inherit_identities:
            allowed.update({"identityfile", "identityagent", "identitiesonly"})
    active = True

    def read(config: Path) -> None:
        nonlocal active
        config = config.expanduser().resolve()
        if config in seen:
            return
        if len(seen) >= 32:
            raise _ProbeError("external-config-unsupported")
        seen.add(config)
        if not config.exists():
            return
        if config.stat().st_size > 1024 * 1024:
            raise _ProbeError("external-config-unsupported")
        for line in config.read_text(encoding="utf-8").splitlines():
            tokens = split_config_line(line)
            if not tokens:
                continue
            directive = tokens[0].casefold()
            if directive == "host":
                positives = [p for p in tokens[1:] if not p.startswith("!")]
                negatives = [p[1:] for p in tokens[1:] if p.startswith("!")]
                active = (any(fnmatch.fnmatchcase(alias.casefold(), p.casefold()) for p in positives)
                          and not any(fnmatch.fnmatchcase(alias.casefold(), p.casefold()) for p in negatives))
            elif directive == "match":
                # Evaluation may depend on commands, canonicalization or DNS.
                raise _ProbeError("external-config-unsupported")
            elif directive == "include" and active:
                for pattern in tokens[1:]:
                    expanded = Path(os.path.expanduser(pattern))
                    if not expanded.is_absolute():
                        expanded = config.parent / expanded
                    for child in sorted(glob.glob(str(expanded))):
                        read(Path(child))
            elif active and directive in {"proxycommand", "knownhostscommand"}:
                if len(tokens) > 1 and tokens[1].casefold() != "none":
                    raise _ProbeError("external-config-unsupported")
            elif active and directive in allowed and directive not in values:
                if len(tokens) != 2 or "%" in tokens[1] or any(ord(c) < 32 for c in tokens[1]):
                    raise _ProbeError("external-config-unsupported")
                values[directive] = tokens[1]

    try:
        read(path)
        jumps = values.get("proxyjump", "none")
        return {"alias": alias, "hostname": validate_hostname(values.get("hostname", alias)),
                "username": validate_username(values.get("user", getpass.getuser())),
                "port": validate_port(values.get("port", 22)), "credential_id": None,
                "proxy_jumps": [] if jumps.casefold() == "none" else [validate_alias(j) for j in jumps.split(",")],
                "external_options": {key: value for key, value in values.items()
                                     if key not in {"hostname", "user", "port", "proxyjump"}}}
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        raise _ProbeError("external-config-unsupported") from exc


class _Broker(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, hops: list[dict[str, Any]], vault_factory: Callable[[], VaultProtocol]) -> None:
        self.hops = hops
        self.vault_factory = vault_factory
        self.token = secrets.token_urlsafe(32)
        self.events: queue.Queue[tuple[str, int]] = queue.Queue()
        self.active = True
        super().__init__(("127.0.0.1", 0), _BrokerHandler)
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.active = False
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=.2)


class _BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: _Broker = self.server  # type: ignore[assignment]
        self.connection.settimeout(2)
        response: dict[str, Any] = {"ok": False}
        try:
            data = self.rfile.readline(4097)
            if len(data) > 4096:
                return
            request = json.loads(data)
            if not server.active or not secrets.compare_digest(request.get("token", ""), server.token):
                return
            stage = request["stage"]
            if type(stage) is not int or not 0 <= stage < len(server.hops):
                return
            if request["op"] == "authenticated":
                server.events.put(("authenticated", stage))
                response = {"ok": True}
            elif request["op"] == "secret":
                descriptor = next((item for item in server.hops[stage]["descriptors"]
                                   if item["slot"] == request.get("slot")), None)
                if descriptor:
                    try:
                        value = server.vault_factory().get_secret(descriptor["credential_id"], descriptor["slot"])
                        if server.active and isinstance(value, str) and value:
                            response = {"ok": True, "secret": value}
                        else:
                            server.events.put(("vault-unavailable", stage))
                    except Exception:
                        server.events.put(("vault-unavailable", stage))
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except Exception:
            return


class _ProcessTree:
    """Private POSIX process group or a Windows kill-on-close job."""

    def __init__(self, command: list[str], environment: dict[str, str]) -> None:
        self.job: Any = None
        kwargs: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {
            # Attach the job before any SSH/helper instruction can execute.
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004}  # CREATE_SUSPENDED
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, env=environment, **kwargs)
        self.errors = bytearray()
        self.reader = threading.Thread(target=self._drain, daemon=True)
        self.reader.start()
        if os.name == "nt":
            try:
                self._attach_job()
            except Exception:
                self.close()
                raise _ProbeError("ssh-failed")

    def _attach_job(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise OSError("job creation failed")
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not \
                kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(self.process._handle))):
            kernel.CloseHandle(job)
            raise OSError("job assignment failed")
        self.job = (kernel, job)
        self._resume_primary_thread(kernel)

    def _resume_primary_thread(self, kernel) -> None:
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [(name, wintypes.DWORD) for name in
                        ("dwSize", "cntUsage", "th32ThreadID", "th32OwnerProcessID")] + [
                        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

        # subprocess closes CreateProcess's primary-thread handle. A suspended
        # new process has exactly one thread; reopen it using the documented
        # Toolhelp API, with only THREAD_SUSPEND_RESUME permission.
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError("thread snapshot failed")
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            threads = []
            while found:
                if entry.th32OwnerProcessID == self.process.pid:
                    threads.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            if len(threads) != 1:
                raise OSError("suspended primary thread unavailable")
            thread = kernel.OpenThread(0x0002, False, threads[0])
            if not thread:
                raise OSError("primary thread access failed")
            try:
                if kernel.ResumeThread(thread) != 1:
                    raise OSError("primary thread resume failed")
            finally:
                kernel.CloseHandle(thread)
        finally:
            kernel.CloseHandle(snapshot)

    def _drain(self) -> None:
        assert self.process.stderr is not None
        while True:
            chunk = self.process.stderr.read(4096)
            if not chunk:
                break
            remaining = 32768 - len(self.errors)
            if remaining > 0:
                self.errors.extend(chunk[:remaining])

    def close(self) -> None:
        if self.job:
            kernel, job = self.job
            kernel.CloseHandle(job)
            self.job = None
        elif os.name != "nt":
            # The parent may already have exited while proxy children remain.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin may report EPERM, rather than ESRCH, after the last
                # member of an already-reaped private process group exits.
                # Never suppress a signal failure for a still-running parent.
                if self.process.poll() is None:
                    raise
            try:
                self.process.wait(timeout=.15)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if self.process.poll() is None:
                    raise
        elif self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)
        self.reader.join(timeout=.2)
        if self.process.stderr:
            self.process.stderr.close()


def _method(hop: dict[str, Any]) -> str | None:
    try:
        with Path(hop["log"]).open("rb") as source:
            data = source.read(65537)
        if len(data) > 65536:
            return None
        methods_pattern = "none|password|publickey|keyboard-interactive|hostbased|gssapi-with-mic"
        target = r'Authenticated to ' + re.escape(hop["hostname"]) + r' \([^\r\n]*\)'
        expression = re.compile(target + r' using "(' + methods_pattern + r')"\.')
        lines = [line.strip() for line in data.decode("utf-8", "replace").splitlines()]
        matches = [expression.fullmatch(line) for line in lines]
        methods = [match[1] for match in matches if match]
        # Older clients, including the Windows inbox OpenSSH, log the method
        # separately at DEBUG1. Both complete local records must be present:
        # the method and the expected destination. Remote banners stay on
        # stderr and cannot supply either private -E log record.
        legacy_method = re.compile(r'debug1: Authentication succeeded \((' + methods_pattern + r')\)\.')
        legacy_target = re.compile(target + r'\.')
        if sum(legacy_target.fullmatch(line) is not None for line in lines) == 1:
            methods.extend(match[1] for line in lines if (match := legacy_method.fullmatch(line)))
        return methods[0] if len(methods) == 1 else None
    except OSError:
        return None


class CredentialProbeRunner:
    def __init__(self, database: Database, vault_factory: Callable[[], VaultProtocol] = get_vault,
                 *, ssh_binary: str | None = None) -> None:
        self.database = database
        self.vault_factory = vault_factory
        self.ssh_binary = ssh_binary or shutil.which("ssh") or "ssh"

    def _hops(self, target: dict[str, Any], credential: dict[str, Any]) -> list[dict[str, Any]]:
        inventory = {item["alias"].casefold(): item for item in self.database.list_servers()}
        credentials = {item["id"]: item for item in self.database.list_credentials()}

        def resolve(alias: str) -> dict[str, Any]:
            return inventory.get(alias.casefold()) or _external_config(alias, original_ssh_config_path())

        chain = [target]
        current = target
        seen = {target["alias"].casefold()}
        while current.get("proxy_jumps"):
            items = [resolve(alias) for alias in current["proxy_jumps"]]
            for item in items:
                key = item["alias"].casefold()
                if key in seen or len(seen) >= 32:
                    raise _ProbeError("external-config-unsupported")
                seen.add(key)
            chain = [*items, *chain]
            current = items[0]
        result = []
        for index, host in enumerate(chain):
            selected = credential if index == len(chain) - 1 else credentials.get(host.get("credential_id"))
            if "external_options" not in host:
                # Respect saved trust stores without importing executable hooks
                # or allowing original identity settings to replace the candidate.
                host = {**host, "external_options": _external_config(
                    host["alias"], original_ssh_config_path(), trust_only=True,
                    inherit_identities=selected is None)["external_options"]}
            descriptors = []
            slot = "password" if selected and selected["kind"] == "password" else (
                "passphrase" if selected and selected["kind"] == "key" and selected["has_passphrase"] else None)
            if slot:
                descriptors = [{"credential_id": selected["id"], "slot": slot, "alias": host["alias"],
                                "hostname": host["hostname"], "username": host["username"],
                                "key_path": selected.get("key_path")}]
            result.append({**host, "credential": selected, "descriptors": descriptors,
                           "probe_alias": f"ssm-credential-probe-{index}"})
        return result

    def _prepare(self, directory: Path, hops: list[dict[str, Any]], timeout: float) -> tuple[Path, Path]:
        manifest = directory / "request.json"
        config = directory / "ssh.conf"
        helper = str(Path(__file__).with_name("credential_probe_helper.py"))
        lines = []
        for index, hop in enumerate(hops):
            hop["log"] = str(directory / f"hop-{index}.log")
            _private_write(Path(hop["log"]), "")
            lines.extend([f"Host {hop['probe_alias']}", f"  HostName {quote_value(hop['hostname'])}",
                          f"  User {quote_value(hop['username'])}", f"  Port {hop['port']}",
                          "  PermitLocalCommand yes", "  LocalCommand " + _shell_command(
                              [sys.executable, helper, "callback", str(manifest), str(index)])])
            if index:
                lines.append("  ProxyCommand " + _shell_command(
                    [sys.executable, helper, "proxy", str(manifest), str(index - 1),
                     f"[{hop['hostname']}]:{hop['port']}"]))
            else:
                lines.append("  ProxyCommand none")
            selected = hop["credential"]
            if selected and selected["kind"] == "password":
                lines.extend(["  IdentityFile none", "  IdentityAgent none", "  PubkeyAuthentication no",
                              "  PasswordAuthentication yes", "  PreferredAuthentications password"])
            elif selected and selected["kind"] == "key":
                lines.extend(["  IdentityFile " + quote_value(selected["key_path"]), "  IdentityAgent none",
                              "  IdentitiesOnly yes", "  PubkeyAuthentication yes", "  PasswordAuthentication no",
                              "  PreferredAuthentications publickey"])
            else:
                # Explicit agent credentials use only agent identities. A jump
                # with no bound credential retains its configured SSH identity.
                if selected:
                    lines.append("  IdentityFile none")
                lines.extend(["  PubkeyAuthentication yes", "  PasswordAuthentication no",
                              "  PreferredAuthentications publickey"])
                for key, value in hop.get("external_options", {}).items():
                    if key not in {"userknownhostsfile", "globalknownhostsfile", "hostkeyalias"}:
                        lines.append(f"  {key} {quote_value(value)}")
            for key, value in hop.get("external_options", {}).items():
                if key in {"userknownhostsfile", "globalknownhostsfile", "hostkeyalias"}:
                    lines.append(f"  {key} {quote_value(value)}")
            lines.append("")
        lines.extend(["Host *", "  StrictHostKeyChecking yes", "  UpdateHostKeys no", "  LogLevel DEBUG1",
                      "  ControlMaster no", "  ControlPath none", "  ControlPersist no", "  RequestTTY no",
                      "  SessionType none", "  ForwardAgent no", "  ForwardX11 no", "  ClearAllForwardings yes",
                      "  RemoteCommand none", "  GSSAPIAuthentication no", "  HostbasedAuthentication no",
                      "  KbdInteractiveAuthentication no", "  NumberOfPasswordPrompts 1", "  ConnectionAttempts 1",
                      f"  ConnectTimeout {max(1, math.ceil(timeout))}", "  ServerAliveInterval 1",
                      "  ServerAliveCountMax 1", "  AddKeysToAgent no", "  EscapeChar none", ""])
        _private_write(config, "\n".join(lines))
        # Credential metadata only: no vault values or request capability.
        _private_write(manifest, json.dumps({"hops": hops, "config": str(config), "ssh_binary": self.ssh_binary}))
        return config, manifest

    def _environment(self, hops: list[dict[str, Any]], directory: Path, broker: _Broker) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("SSM_CREDENTIAL_PROBE_") and key not in {
                           "SSM_ASKPASS_MAP", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}}
        if os.name == "nt":
            launcher = next((path for path in SSHRunner._windows_askpass_candidates() if path.exists()), None)
            if launcher is None:
                raise _ProbeError("ssh-failed")
        else:
            launcher = directory / "askpass"
            package = Path(__file__).resolve().parents[1]
            _private_write(launcher, "#!/bin/sh\n" +
                           f"export PYTHONPATH={shlex.quote(str(package))}\n" +
                           f"exec {shlex.quote(sys.executable)} -m ssh_server_manager.credential_probe_helper askpass \"$@\"\n")
            launcher.chmod(stat.S_IRWXU)
        # Direct helper scripts need their package import for the proxy's
        # AskPass subprocess. Never inherit another request's descriptors.
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        environment.update({"SSH_ASKPASS": str(launcher), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": "ssm-probe:0",
                            "SSM_ASKPASS_MAP": json.dumps(hops[-1]["descriptors"], separators=(",", ":")),
                            "SSM_CREDENTIAL_PROBE_STAGE": str(len(hops) - 1),
                            "SSM_CREDENTIAL_PROBE_ENDPOINT": f"127.0.0.1:{broker.server_address[1]}",
                            "SSM_CREDENTIAL_PROBE_TOKEN": broker.token})
        return environment

    def test(self, credential_id: str, host_identifier: str, *, timeout: float = 8,
             cancel_event: threading.Event | None = None) -> dict[str, Any]:
        for event in self.stream(credential_id, host_identifier, timeout=timeout, cancel_event=cancel_event):
            if event["type"] == "result":
                return event["result"]
        raise RuntimeError("credential probe produced no result")

    def stream(self, credential_id: str, host_identifier: str, *, timeout: float = 8,
               cancel_event: threading.Event | None = None) -> Iterator[dict[str, Any]]:
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValidationError("credential test timeout must be between 0 and 120 seconds")
        started = time.monotonic()
        deadline = started + timeout
        credential = self.database.get_credential(credential_id)
        host = self.database.get_server(host_identifier)
        stages: list[dict[str, Any]] = []
        code: str | None = None
        method: str | None = None
        tree: _ProcessTree | None = None
        broker: _Broker | None = None
        temporary: tempfile.TemporaryDirectory[str] | None = None

        def stage(name: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
            item = {"stage": name, "status": status, "message": message, **extra}
            stages.append(item)
            return {"type": "stage", **item}

        yield stage("prepare", "running", "Preparing an isolated authentication request.")
        try:
            if credential["kind"] == "password" and not credential["has_secret"]:
                raise _ProbeError("credential-secret-missing")
            hops = self._hops(host, credential)
            temporary = tempfile.TemporaryDirectory(prefix="credential-probe-", dir=runtime_dir())
            directory = Path(temporary.name)
            config, _ = self._prepare(directory, hops, timeout)
            broker = _Broker(hops, self.vault_factory)
            environment = self._environment(hops, directory, broker)
            if cancel_event and cancel_event.is_set():
                raise _ProbeError("cancelled")
            if time.monotonic() >= deadline:
                raise _ProbeError("timeout")
            yield stage("prepare", "ok", "The request configuration is ready.")
            yield stage("authenticate", "running", "Connecting and authenticating without a remote session.")
            command = [self.ssh_binary, "-F", str(config), "-E", hops[-1]["log"], "-N", "-T",
                       hops[-1]["probe_alias"]]
            tree = _ProcessTree(command, environment)
            confirmed: set[int] = set()
            while True:
                if cancel_event and cancel_event.is_set():
                    raise _ProbeError("cancelled")
                if time.monotonic() >= deadline:
                    raise _ProbeError("timeout")
                try:
                    kind, index = broker.events.get(timeout=min(.04, max(.001, deadline - time.monotonic())))
                    if kind == "vault-unavailable":
                        raise _ProbeError("vault-unavailable")
                    if index not in confirmed:
                        confirmed.add(index)
                        hop_method = _method(hops[index])
                        if index < len(hops) - 1:
                            yield stage("hop", "ok", "Jump authentication completed.", hop_index=index,
                                        alias=hops[index]["alias"], method=hop_method)
                        else:
                            method = hop_method
                            if method == "none":
                                raise _ProbeError("credential-not-used")
                            expected = "password" if credential["kind"] == "password" else "publickey"
                            if method != expected:
                                raise _ProbeError("authentication-unconfirmed")
                            break
                except queue.Empty:
                    pass
                if tree.process.poll() is not None and broker.events.empty():
                    tree.reader.join(timeout=.1)
                    diagnostics = bytes(tree.errors).decode("utf-8", "replace")
                    for hop in hops:
                        with Path(hop["log"]).open("rb") as diagnostic_log:
                            diagnostics += diagnostic_log.read(32768).decode("utf-8", "replace")
                    # Success cannot be inferred from stderr or exit code.
                    raise _ProbeError(classify_error(diagnostics, tree.process.returncode)
                                      if tree.process.returncode else "authentication-unconfirmed")
            yield stage("authenticate", "ok", "The selected credential was accepted.", method=method)
            tree.close()
            tree = None
            broker.close()
            broker = None
        except _ProbeError as exc:
            code = exc.code
        except FileNotFoundError:
            code = "ssh-not-found"
        except (OSError, ValueError):
            code = "ssh-failed"
        finally:
            if tree is not None:
                tree.close()
            if broker is not None:
                broker.close()
            if temporary is not None:
                temporary.cleanup()
        if code:
            yield stage("authenticate", "failed", _MESSAGES.get(code, _MESSAGES["ssh-failed"]), error_code=code)
        yield {"type": "result", "result": {
            "ok": code is None, "error_code": code, "message": _MESSAGES.get(code, "The selected credential was accepted."),
            "latency_ms": round((time.monotonic() - started) * 1000), "checked_at": utc_now(),
            "host_id": host["id"], "host_alias": host["alias"], "credential_id": credential["id"],
            "credential_kind": credential["kind"], "method": method, "stages": stages,
        }}
