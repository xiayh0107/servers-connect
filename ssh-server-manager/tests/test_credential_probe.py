from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ssh_fixture_permissions import private_ssh_fixture_file

from ssh_server_manager.credential_probe import CredentialProbeRunner, _Broker, _external_config, _method, _ProbeError, _ProcessTree, _windows_callback_command
from ssh_server_manager.credential_probe_helper import _request, probe_askpass
from ssh_server_manager.db import Database
from ssh_server_manager.ssh_config import quote_value
from ssh_server_manager.validation import ValidationError
from ssh_server_manager.vault import MemoryVault


@pytest.fixture
def request_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "original.conf"))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    database = Database(tmp_path / "data" / "manager.db")
    credential = database.create_credential(label="Candidate", kind="password", has_secret=True)
    host = database.create_server(alias="target", hostname="127.0.0.1", username="fixture", port=22)
    vault = MemoryVault()
    vault.set_secret(credential["id"], "password", "fixture-password-never-log")
    return database, credential, host, vault


def test_request_configuration_changes_only_selected_target(tmp_path, request_state):
    database, credential, host, vault = request_state
    original = database.create_credential(label="Original", kind="agent")
    jump_credential = database.create_credential(label="Jump", kind="password", has_secret=True)
    jump = database.create_server(alias="jump", hostname="127.0.0.1", username="fixture", port=22,
                                  credential_id=jump_credential["id"])
    database.update_server(host["id"], credential_id=original["id"], proxy_jumps=[jump["alias"]])
    snapshot = database.get_server(host["id"])
    runner = CredentialProbeRunner(database, lambda: vault)
    hops = runner._hops(snapshot, credential)
    assert [hop["credential"]["id"] for hop in hops] == [jump_credential["id"], credential["id"]]
    config, manifest = runner._prepare(tmp_path, hops, 8)
    text = config.read_text()
    assert "StrictHostKeyChecking yes" in text
    assert "ControlMaster no" in text and "ControlPath none" in text
    assert "SessionType none" in text and "RequestTTY no" in text and "RemoteCommand none" in text
    assert "PreferredAuthentications password" in text
    assert "fixture-password-never-log" not in text + manifest.read_text()
    assert "Include " not in text and "ssm-credential-probe-0" in text
    assert database.get_server(host["id"]) == snapshot
    if os.name != "nt":
        assert config.stat().st_mode & 0o777 == 0o600


def test_external_alias_resolution_never_executes_saved_hooks(tmp_path):
    marker = tmp_path / "side-effect"
    config = tmp_path / "config"
    config.write_text(f"Host external\n HostName 127.0.0.1\n User fixture\n Port 2222\n"
                      f" IdentityFile /tmp/fixture-key\n LocalCommand touch {marker}\n PermitLocalCommand yes\n")
    private_ssh_fixture_file(config)
    result = _external_config("external", config)
    assert result["port"] == 2222
    assert result["external_options"]["identityfile"] == "/tmp/fixture-key"
    assert not marker.exists()
    config.write_text(f"Host external\n ProxyCommand touch {marker}\n")
    with pytest.raises(_ProbeError, match="safely resolved"):
        _external_config("external", config)
    config.write_text(f"Match exec \"touch {marker}\"\n HostName 127.0.0.1\n")
    with pytest.raises(_ProbeError):
        _external_config("external", config)
    assert not marker.exists()


def test_windows_callback_encodes_fixed_script_and_keeps_metadata_out_of_cmd(monkeypatch):
    metadata = r"C:\fixture & percent% bang! quote' 中文\request.json"
    for suffix in ("PYTHON", "HELPER", "MANIFEST"):
        monkeypatch.setenv(f"SSM_CREDENTIAL_PROBE_{suffix}", metadata)
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    command = _windows_callback_command(powershell, 7)
    assert command.startswith(f'""{powershell}" -NoLogo -NoProfile -NonInteractive -EncodedCommand ')
    assert command.endswith('"')
    encoded = command.split("-EncodedCommand ", 1)[1][:-1]
    script = base64.b64decode(encoded, validate=True).decode("utf-16-le")
    assert metadata not in command and metadata not in script
    assert ("& $env:SSM_CREDENTIAL_PROBE_PYTHON $env:SSM_CREDENTIAL_PROBE_HELPER "
            "'callback' $env:SSM_CREDENTIAL_PROBE_MANIFEST '7'") in script
    assert "exit $LASTEXITCODE" in script
    for invalid in (-1, 32, "0 & whoami", True):
        with pytest.raises(ValueError):
            _windows_callback_command(powershell, invalid)


def test_proxy_helper_reads_destination_from_manifest_without_shell_argument(tmp_path, monkeypatch):
    import ssh_server_manager.credential_probe_helper as helper

    manifest = tmp_path / "request.json"
    manifest.write_text(json.dumps({
        "config": "fixture.conf", "ssh_binary": "fixture-ssh", "hops": [
            {"log": "fixture.log", "probe_alias": "ssm-credential-probe-0", "descriptors": []},
            {"hostname": "fixture&whoami", "port": 2222},
        ],
    }))
    calls = []

    def capture_call(command, *, env):
        calls.append(command)
        return 0

    def capture_exec(program, command, environment):
        calls.append(command)
        raise SystemExit(0)

    monkeypatch.setattr(sys, "argv", ["credential_probe_helper.py", "proxy", str(manifest), "0"])
    monkeypatch.setattr(helper.subprocess, "call", capture_call)
    monkeypatch.setattr(helper.os, "execvpe", capture_exec)
    if os.name == "nt":
        assert helper.main() == 0
    else:
        with pytest.raises(SystemExit) as result:
            helper.main()
        assert result.value.code == 0
    assert calls == [["fixture-ssh", "-F", "fixture.conf", "-E", "fixture.log", "-T", "-W",
                      "[fixture&whoami]:2222", "ssm-credential-probe-0"]]


def test_method_requires_single_complete_trusted_log_record(tmp_path):
    log = tmp_path / "client.log"
    hop = {"hostname": "127.0.0.1", "log": str(log)}
    log.write_text('Authenticated to 127.0.0.1 ([127.0.0.1]:22) using "none".\r\n')
    assert _method(hop) == "none"
    log.write_text('banner: Authenticated to 127.0.0.1 ([127.0.0.1]:22) using "password".\n')
    assert _method(hop) is None
    log.write_text('Authenticated to 127.0.0.1 ([127.0.0.1]:22) using "password".\n' * 2)
    assert _method(hop) is None


@pytest.mark.parametrize("method", ["none", "password", "publickey"])
def test_method_supports_legacy_local_client_records(tmp_path, method):
    log = tmp_path / "client.log"
    hop = {"hostname": "127.0.0.1", "log": str(log)}
    authenticated = 'Authenticated to 127.0.0.1 ([127.0.0.1]:22).\r\n'
    legacy_method = f'debug1: Authentication succeeded ({method}).\r\n'
    log.write_text(legacy_method + authenticated)
    assert _method(hop) == method
    log.write_text(legacy_method)
    assert _method(hop) is None
    log.write_text(legacy_method + authenticated.replace("127.0.0.1", "192.0.2.1"))
    assert _method(hop) is None
    log.write_text(legacy_method * 2 + authenticated)
    assert _method(hop) is None
    log.write_text('banner: ' + legacy_method + authenticated)
    assert _method(hop) is None


def test_broker_scopes_secret_to_stage_and_rejects_remote_prompts(request_state, monkeypatch, capsys):
    database, credential, host, vault = request_state
    runner = CredentialProbeRunner(database, lambda: vault)
    hops = runner._hops(host, credential)
    broker = _Broker(hops, lambda: vault)
    try:
        monkeypatch.setenv("SSM_CREDENTIAL_PROBE_ENDPOINT", f"127.0.0.1:{broker.server_address[1]}")
        monkeypatch.setenv("SSM_CREDENTIAL_PROBE_TOKEN", broker.token)
        monkeypatch.setenv("SSM_CREDENTIAL_PROBE_STAGE", "0")
        monkeypatch.setenv("SSM_ASKPASS_MAP", json.dumps(hops[0]["descriptors"]))
        assert probe_askpass("Confirm fingerprint (yes/no)?") == 1
        assert probe_askpass("One-time verification code:") == 1
        assert _request({"op": "secret", "stage": 0, "slot": "passphrase"}) == {"ok": False}
        assert probe_askpass("fixture@127.0.0.1's password:") == 0
        assert capsys.readouterr().out == "fixture-password-never-log"
        monkeypatch.setenv("SSM_CREDENTIAL_PROBE_STAGE", "1")
        assert probe_askpass("password:") == 1
        assert capsys.readouterr().out == ""
    finally:
        broker.close()


def test_cancel_before_connect_and_missing_password_are_stable(request_state):
    database, credential, host, vault = request_state
    cancel = threading.Event()
    cancel.set()
    result = CredentialProbeRunner(database, lambda: vault).test(credential["id"], host["id"], cancel_event=cancel)
    assert result["error_code"] == "cancelled"
    empty = database.create_credential(label="Empty", kind="password")
    result = CredentialProbeRunner(database, lambda: vault).test(empty["id"], host["id"])
    assert result["error_code"] == "credential-secret-missing"
    assert not list(Path(os.environ["SSM_RUNTIME_DIR"]).glob("credential-probe-*"))
    with pytest.raises(ValidationError):
        CredentialProbeRunner(database, lambda: vault).test(credential["id"], host["id"], timeout=float("nan"))


def test_success_log_and_exit_zero_still_require_local_callback(request_state, monkeypatch):
    database, credential, host, vault = request_state

    class UnconfirmedProcess:
        returncode = 0

        def poll(self):
            return 0

    class UnconfirmedTree:
        def __init__(self, command, environment):
            log = Path(command[command.index("-E") + 1])
            log.write_text('Authenticated to 127.0.0.1 ([127.0.0.1]:22) using "password".\n')
            self.process = UnconfirmedProcess()
            self.errors = bytearray()
            self.reader = threading.Thread(target=lambda: None)
            self.reader.start()

        def close(self):
            pass

    monkeypatch.setattr("ssh_server_manager.credential_probe._ProcessTree", UnconfirmedTree)
    result = CredentialProbeRunner(database, lambda: vault).test(credential["id"], host["id"])
    assert result["error_code"] == "authentication-unconfirmed"
    assert result["ok"] is False


@pytest.mark.parametrize("proof,expected_code", [
    ("valid", None),
    ("missing-hop-proof", "authentication-unconfirmed"),
    ("wrong-hop-method", "authentication-unconfirmed"),
    ("wrong-target", "authentication-unconfirmed"),
    ("missing-hop-callback", "timeout"),
    ("target-callback-first", None),
    ("target-none", "credential-not-used"),
])
def test_route_proof_is_verified_only_after_all_log_owners_close(
        request_state, monkeypatch, proof, expected_code):
    import ssh_server_manager.credential_probe as module

    database, credential, host, vault = request_state
    database.create_server(alias="jump", hostname="127.0.0.1", username="fixture", port=22,
                           credential_id=credential["id"])
    database.update_server(host["id"], proxy_jumps=["jump"])
    closed = False
    original_method = module._method
    reads = []

    class CallbackBroker(_Broker):
        def __init__(self, hops, vault_factory):
            super().__init__(hops, vault_factory)
            if proof == "target-callback-first":
                self.events.put(("authenticated", 1))
            if proof != "missing-hop-callback":
                self.events.put(("authenticated", 0))
            self.events.put(("authenticated", 1))

    class RunningProcess:
        def poll(self):
            return None

    class LockedLogTree:
        def __init__(self, command, environment):
            self.process = RunningProcess()
            self.manifest = json.loads(Path(command[command.index("-F") + 1]).with_name("request.json").read_text())

        def close(self):
            nonlocal closed
            closed = True
            for index, hop in enumerate(self.manifest["hops"]):
                method = "publickey" if index == 0 and proof == "wrong-hop-method" else "password"
                if index == 1 and proof == "target-none":
                    method = "none"
                hostname = "192.0.2.1" if index == 1 and proof == "wrong-target" else "127.0.0.1"
                record = f'Authenticated to {hostname} ([127.0.0.1]:22) using "{method}".\r\n'
                Path(hop["log"]).write_text("" if index == 0 and proof == "missing-hop-proof" else record)

    def read_closed_log(hop):
        assert closed, "Private authentication logs must be read after their owners close"
        reads.append(hop["alias"])
        return original_method(hop)

    monkeypatch.setattr(module, "_Broker", CallbackBroker)
    monkeypatch.setattr(module, "_ProcessTree", LockedLogTree)
    monkeypatch.setattr(module, "_method", read_closed_log)
    result = CredentialProbeRunner(database, lambda: vault).test(
        credential["id"], host["id"], timeout=.1 if proof == "missing-hop-callback" else 8)

    assert result["ok"] is (expected_code is None)
    assert result["error_code"] == expected_code
    assert closed
    if proof == "missing-hop-callback":
        assert reads == []
    else:
        assert reads == ["jump", "target"]
    completed_hops = [item for item in result["stages"] if item["stage"] == "hop" and item["status"] == "ok"]
    assert len(completed_hops) == (1 if expected_code is None else 0)
    assert not list(Path(os.environ["SSM_RUNTIME_DIR"]).glob("credential-probe-*"))


def test_failed_parent_releases_proxy_log_owners_before_classification(request_state, monkeypatch):
    import ssh_server_manager.credential_probe as module

    database, credential, host, vault = request_state
    original_open = Path.open
    closed = False

    class FailedProcess:
        returncode = 255

        def poll(self):
            return self.returncode

    class LockedLogTree:
        def __init__(self, command, environment):
            self.process = FailedProcess()
            self.errors = bytearray()
            self.log = Path(command[command.index("-E") + 1])

        def close(self):
            nonlocal closed
            closed = True
            self.log.write_text("Permission denied (password).\r\n")

    def shared_log_open(path, mode="r", *args, **kwargs):
        if path.name.startswith("hop-") and mode == "rb" and not closed:
            raise PermissionError("Authentication log is held by a proxy process")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(module, "_ProcessTree", LockedLogTree)
    monkeypatch.setattr(Path, "open", shared_log_open)
    result = CredentialProbeRunner(database, lambda: vault).test(credential["id"], host["id"])

    assert closed
    assert not result["ok"] and result["error_code"] == "authentication-failed"
    assert not list(Path(os.environ["SSM_RUNTIME_DIR"]).glob("credential-probe-*"))


def test_process_cleanup_includes_descendant_after_parent_exit(tmp_path):
    heartbeat = tmp_path / "child-heartbeat"
    child_code = (
        "import sys,time; from pathlib import Path; p=Path(sys.argv[1]); "
        "p.write_text('ready\\n'); "
        "exec(\"while True:\\n with p.open('a') as f: f.write('tick\\\\n')\\n time.sleep(.03)\")"
    )
    parent_code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
        "time.sleep(.2)"
    )
    tree = _ProcessTree([sys.executable, "-c", parent_code, child_code, str(heartbeat)], os.environ.copy())
    try:
        tree.process.wait(timeout=3)
        assert heartbeat.exists()
    finally:
        tree.close()
    observed = heartbeat.read_text()
    time.sleep(.15)
    assert heartbeat.read_text() == observed
    assert not tree.reader.is_alive()


class LoopbackSSH:
    """Disposable SSH server. Refuse and count every remote session request."""

    def __init__(self, *, none=False, password="fixture-password-never-log", public_key=None,
                 banner=None, stall=False):
        paramiko = pytest.importorskip("paramiko")
        self.paramiko = paramiko
        self.key = paramiko.RSAKey.generate(2048)
        self.sessions = []
        self.password_attempts = []
        self.forward_requests = []
        self.transports = []
        self.connections = []
        self.stop = threading.Event()
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.socket.settimeout(.1)
        self.port = self.socket.getsockname()[1]
        owner = self

        class Server(paramiko.ServerInterface):
            def check_auth_none(self, username):
                return paramiko.AUTH_SUCCESSFUL if none else paramiko.AUTH_FAILED

            def check_auth_password(self, username, candidate):
                owner.password_attempts.append(candidate)
                return paramiko.AUTH_SUCCESSFUL if candidate == password else paramiko.AUTH_FAILED

            def check_auth_publickey(self, username, candidate):
                return paramiko.AUTH_SUCCESSFUL if public_key is not None and candidate == public_key else paramiko.AUTH_FAILED

            def get_allowed_auths(self, username):
                return "publickey,password"

            def get_banner(self):
                return (banner, "en") if banner else (None, None)

            def check_channel_request(self, kind, chanid):
                owner.sessions.append(kind)
                return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_direct_tcpip_request(self, chanid, origin, destination):
                owner.forward_requests.append((chanid, destination))
                return paramiko.OPEN_SUCCEEDED if destination[0] == "127.0.0.1" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        self.server_class = Server
        self.stall = stall
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections.append(connection)
            if self.stall:
                continue
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        transport = self.paramiko.Transport(connection)
        self.transports.append(transport)
        try:
            transport.add_server_key(self.key)
            transport.start_server(server=self.server_class())
            while transport.is_active() and not self.stop.is_set():
                channel = transport.accept(.1)
                if channel is None:
                    continue
                destination = next(dest for chanid, dest in self.forward_requests if chanid == channel.chanid)
                remote = socket.create_connection(destination, timeout=2)

                def copy(source, destination):
                    try:
                        while chunk := source.recv(32768):
                            destination.sendall(chunk)
                    except (OSError, EOFError):
                        pass
                    finally:
                        source.close()
                        destination.close()

                threading.Thread(target=copy, args=(channel, remote), daemon=True).start()
                threading.Thread(target=copy, args=(remote, channel), daemon=True).start()
        except (OSError, EOFError, self.paramiko.SSHException):
            pass
        finally:
            transport.close()

    def close(self):
        self.stop.set()
        self.socket.close()
        for transport in self.transports:
            transport.close()
        for connection in self.connections:
            connection.close()
        self.thread.join(timeout=1)


@pytest.fixture
def loopback_servers():
    servers = []

    def start(**kwargs):
        server = LoopbackSSH(**kwargs)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def fixture_runner(database, vault, tmp_path, servers, *, trust=True):
    if not shutil.which("ssh"):
        pytest.skip("OpenSSH client is required for loopback authentication fixtures")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("\n".join(
        f"[127.0.0.1]:{server.port} {server.key.get_name()} {server.key.get_base64()}" for server in servers
    ) if trust else "")
    private_ssh_fixture_file(known_hosts)

    class IsolatedRunner(CredentialProbeRunner):
        def _prepare(self, directory, hops, timeout):
            config, manifest = super()._prepare(directory, hops, timeout)
            with config.open("a") as output:
                output.write(f"  UserKnownHostsFile {quote_value(str(known_hosts))}\n  GlobalKnownHostsFile none\n")
            return config, manifest

    return IsolatedRunner(database, lambda: vault)


@pytest.mark.parametrize("none,expected", [(False, True), (True, False)])
def test_real_authentication_password_none_and_banner_isolation(request_state, loopback_servers, tmp_path, none, expected):
    database, credential, host, vault = request_state
    banner = 'Authenticated to 127.0.0.1 ([127.0.0.1]:22) using "password".\nsecret=remote-banner-never-log\n'
    server = loopback_servers(none=none, banner=banner)
    database.update_server(host["id"], port=server.port)
    snapshot = database.get_server(host["id"])
    result = fixture_runner(database, vault, tmp_path, [server]).test(credential["id"], host["id"], timeout=5)
    assert result["ok"] is expected, result
    assert result["method"] == ("none" if none else "password")
    assert result["error_code"] == ("credential-not-used" if none else None)
    assert not server.sessions
    assert not server.forward_requests
    assert database.get_server(host["id"]) == snapshot
    assert "remote-banner-never-log" not in json.dumps(result)
    assert "fixture-password-never-log" not in json.dumps(result)
    assert not list(Path(os.environ["SSM_RUNTIME_DIR"]).glob("credential-probe-*"))


def test_real_authentication_failure_cannot_be_spoofed_by_banner(request_state, loopback_servers, tmp_path):
    database, credential, host, vault = request_state
    server = loopback_servers(password="different", banner=(
        'Authenticated to 127.0.0.1 ([127.0.0.1]:22) using "password".\n'
        'debug1: Authentication succeeded (password).\n'
        'Authenticated to 127.0.0.1 ([127.0.0.1]:22).\n'))
    database.update_server(host["id"], port=server.port)
    result = fixture_runner(database, vault, tmp_path, [server]).test(credential["id"], host["id"], timeout=5)
    assert not result["ok"] and result["error_code"] == "authentication-failed", result
    assert not server.sessions


def test_real_probe_reports_only_sanitized_authentication_evidence(
        request_state, loopback_servers, tmp_path, monkeypatch):
    """Distinguish a missing local callback from an unreadable proof on CI."""
    import ssh_server_manager.credential_probe as module

    database, credential, host, vault = request_state
    server = loopback_servers()
    database.update_server(host["id"], port=server.port)
    method_checks = []
    evidence = []
    original_method = module._method
    original_tree = module._ProcessTree

    def checked_method(hop):
        method = original_method(hop)
        method_checks.append(method)
        return method

    class EvidenceTree(original_tree):
        def __init__(self, command, environment):
            self.proof_log = Path(command[command.index("-E") + 1])
            super().__init__(command, environment)

        def close(self):
            returncode = self.process.poll()
            super().close()
            if self.proof_log.exists():
                with self.proof_log.open("rb") as source:
                    proof = source.read(65537)
                errors = bytes(self.errors).lower()
                # Never emit private log records, callback environment values,
                # paths, remote banners or raw stderr. All output is fixed
                # booleans/counts and the strict parser's bounded method enum.
                evidence.append({
                    "process_returncode_before_cleanup": returncode,
                    "private_log_bytes": len(proof),
                    "private_authentication_records": proof.count(b"Authenticated to "),
                    "private_legacy_method_records": proof.count(b"Authentication succeeded ("),
                    "parsed_method": original_method({"log": str(self.proof_log), "hostname": "127.0.0.1"}),
                    "callback_method_checks": list(method_checks),
                    "stderr_python_traceback": b"traceback (most recent call last)" in errors,
                    "stderr_helper_missing": b"can't open file" in errors,
                    "stderr_command_missing": b"is not recognized" in errors,
                    "stderr_permissions_rejected": b"bad permissions" in errors or b"bad owner" in errors,
                })

    monkeypatch.setattr(module, "_method", checked_method)
    monkeypatch.setattr(module, "_ProcessTree", EvidenceTree)
    result = fixture_runner(database, vault, tmp_path, [server]).test(credential["id"], host["id"], timeout=5)
    assert result["ok"] and result["method"] == "password", {
        "result_code": result["error_code"], "sanitized_evidence": evidence}
    assert method_checks == ["password"]
    assert not server.sessions


def test_real_callback_and_proxy_handle_runtime_path_metacharacters(
        request_state, loopback_servers, tmp_path, monkeypatch):
    database, credential, host, vault = request_state
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime & percent% bang! quote' 中文"))
    jump = loopback_servers()
    target = loopback_servers()
    database.create_server(alias="jump", hostname="127.0.0.1", username="fixture", port=jump.port,
                           credential_id=credential["id"])
    database.update_server(host["id"], port=target.port, proxy_jumps=["jump"])
    result = fixture_runner(database, vault, tmp_path, [jump, target]).test(
        credential["id"], host["id"], timeout=8)
    assert result["ok"] and result["method"] == "password", result
    assert not jump.sessions and not target.sessions
    assert not list(Path(os.environ["SSM_RUNTIME_DIR"]).glob("credential-probe-*"))


def test_real_host_key_is_never_silently_accepted(request_state, loopback_servers, tmp_path):
    database, credential, host, vault = request_state
    server = loopback_servers()
    database.update_server(host["id"], port=server.port)
    result = fixture_runner(database, vault, tmp_path, [server], trust=False).test(credential["id"], host["id"], timeout=5)
    assert result["error_code"] == "host-key-untrusted", result
    assert not server.password_attempts and not server.sessions


def test_real_changed_host_key_is_rejected_without_accepting_or_authenticating(request_state, loopback_servers, tmp_path):
    paramiko = pytest.importorskip("paramiko")
    database, credential, host, vault = request_state
    server = loopback_servers()
    database.update_server(host["id"], port=server.port)
    snapshot = database.get_server(host["id"])
    runner = fixture_runner(database, vault, tmp_path, [server])
    other_key = paramiko.RSAKey.generate(2048)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:{server.port} {other_key.get_name()} {other_key.get_base64()}\n")
    trusted_before = known_hosts.read_bytes()

    result = runner.test(credential["id"], host["id"], timeout=5)

    assert not result["ok"] and result["error_code"] == "host-key-untrusted", result
    assert not server.password_attempts and not server.sessions
    assert known_hosts.read_bytes() == trusted_before
    assert database.get_server(host["id"]) == snapshot


def test_real_key_authentication_has_no_remote_session(request_state, loopback_servers, tmp_path):
    paramiko = pytest.importorskip("paramiko")
    database, _, host, vault = request_state
    key = tmp_path / "identity"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    private_ssh_fixture_file(key)
    selected = database.create_credential(label="Fixture key", kind="key", key_path=str(key))
    server = loopback_servers(public_key=paramiko.Ed25519Key.from_private_key_file(str(key)))
    database.update_server(host["id"], port=server.port)
    result = fixture_runner(database, vault, tmp_path, [server]).test(selected["id"], host["id"], timeout=5)
    assert result["ok"] and result["method"] == "publickey", result
    assert not server.sessions and not server.password_attempts


def test_real_encrypted_key_uses_vault_passphrase(request_state, loopback_servers, tmp_path):
    paramiko = pytest.importorskip("paramiko")
    database, _, host, vault = request_state
    key = tmp_path / "encrypted-identity"
    passphrase = "fixture-key-passphrase-never-log"
    # The fixture key and passphrase are disposable test data. Production
    # never passes a vault value to ssh-keygen or any other process argument.
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(key)], check=True)
    private_ssh_fixture_file(key)
    selected = database.create_credential(label="Encrypted fixture", kind="key", key_path=str(key), has_passphrase=True)
    vault.set_secret(selected["id"], "passphrase", passphrase)
    server = loopback_servers(public_key=paramiko.Ed25519Key.from_private_key_file(str(key), password=passphrase))
    database.update_server(host["id"], port=server.port)
    result = fixture_runner(database, vault, tmp_path, [server]).test(selected["id"], host["id"], timeout=5)
    assert result["ok"] and result["method"] == "publickey", result
    assert passphrase not in json.dumps(result)
    assert not server.sessions and not server.password_attempts


def test_real_external_jump_uses_safe_key_configuration(request_state, loopback_servers, tmp_path):
    paramiko = pytest.importorskip("paramiko")
    database, credential, host, vault = request_state
    key = tmp_path / "external-identity"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    private_ssh_fixture_file(key)
    jump = loopback_servers(public_key=paramiko.Ed25519Key.from_private_key_file(str(key)))
    target = loopback_servers()
    marker = tmp_path / "saved-localcommand-was-run"
    Path(os.environ["SSM_ORIGINAL_SSH_CONFIG"]).write_text(
        f"Host external\n HostName 127.0.0.1\n User fixture\n Port {jump.port}\n"
        f" IdentityFile {quote_value(str(key))}\n IdentitiesOnly yes\n LocalCommand touch {marker}\n")
    private_ssh_fixture_file(Path(os.environ["SSM_ORIGINAL_SSH_CONFIG"]))
    database.update_server(host["id"], port=target.port, proxy_jumps=["external"])
    result = fixture_runner(database, vault, tmp_path, [jump, target]).test(credential["id"], host["id"], timeout=5)
    assert result["ok"], result
    assert not marker.exists()
    assert not jump.password_attempts
    assert not jump.sessions and not target.sessions


def test_vault_errors_are_redacted(request_state, loopback_servers, tmp_path):
    database, credential, host, _ = request_state
    server = loopback_servers()
    database.update_server(host["id"], port=server.port)

    class BrokenVault:
        def get_secret(self, *args):
            raise RuntimeError("password=private-vault-error")

    result = fixture_runner(database, BrokenVault(), tmp_path, [server]).test(credential["id"], host["id"], timeout=5)
    assert result["error_code"] == "vault-unavailable", result
    assert "private-vault-error" not in json.dumps(result)


def test_real_multihop_keeps_same_username_credentials_separate(request_state, loopback_servers, tmp_path):
    database, credential, host, vault = request_state
    first = loopback_servers(password="first-jump-secret")
    second = loopback_servers(password="second-jump-secret")
    target = loopback_servers()
    aliases = []
    for index, server in enumerate([first, second]):
        selected = database.create_credential(label=f"Jump {index}", kind="password", has_secret=True)
        vault.set_secret(selected["id"], "password", ["first-jump-secret", "second-jump-secret"][index])
        jump = database.create_server(alias=f"jump{index}", hostname="127.0.0.1", username="fixture",
                                      port=server.port, credential_id=selected["id"])
        aliases.append(jump["alias"])
    database.update_server(host["id"], port=target.port, proxy_jumps=aliases)
    result = fixture_runner(database, vault, tmp_path, [first, second, target]).test(credential["id"], host["id"], timeout=8)
    assert result["ok"], result
    assert first.password_attempts == ["first-jump-secret"]
    assert second.password_attempts == ["second-jump-secret"]
    assert target.password_attempts == ["fixture-password-never-log"]
    assert not any(server.sessions for server in [first, second, target])
    assert [item["alias"] for item in result["stages"]
            if item["stage"] == "hop" and item["status"] == "ok"] == aliases


def test_closing_stream_after_hop_disconnects_entire_route(request_state, loopback_servers, tmp_path):
    database, credential, host, vault = request_state
    jump = loopback_servers()
    target = loopback_servers(stall=True)
    database.create_server(alias="jump", hostname="127.0.0.1", username="fixture", port=jump.port,
                           credential_id=credential["id"])
    database.update_server(host["id"], port=target.port, proxy_jumps=["jump"])
    stream = fixture_runner(database, vault, tmp_path, [jump, target]).stream(credential["id"], host["id"], timeout=5)
    for event in stream:
        if event.get("stage") == "hop":
            stream.close()
            break
    assert not list(Path(os.environ["SSM_RUNTIME_DIR"]).glob("credential-probe-*"))
    deadline = time.monotonic() + 1
    while any(transport.is_active() for transport in jump.transports) and time.monotonic() < deadline:
        time.sleep(.02)
    assert not any(transport.is_active() for transport in jump.transports)


def test_real_deadline_and_disconnect_close_processes(request_state, loopback_servers, tmp_path):
    database, credential, host, vault = request_state
    server = loopback_servers(stall=True)
    database.update_server(host["id"], port=server.port)
    runner = fixture_runner(database, vault, tmp_path, [server])
    started = time.monotonic()
    result = runner.test(credential["id"], host["id"], timeout=.25)
    assert result["error_code"] == "timeout"
    assert time.monotonic() - started < 1.5
    stream = runner.stream(credential["id"], host["id"], timeout=8)
    while (event := next(stream))["stage"] != "authenticate":
        pass
    # Advance past the yielded stage so the process is running, then cancel
    # through the normal request cancellation flag.
    stream.close()
    cancel = threading.Event()
    timer = threading.Timer(.15, cancel.set)
    timer.start()
    try:
        result = runner.test(credential["id"], host["id"], timeout=8, cancel_event=cancel)
        assert result["error_code"] == "cancelled"
    finally:
        timer.cancel()
    assert not list(Path(os.environ["SSM_RUNTIME_DIR"]).glob("credential-probe-*"))
