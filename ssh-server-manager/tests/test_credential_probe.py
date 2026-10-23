from __future__ import annotations

import base64
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ssh_fixture_permissions import private_ssh_fixture_file

from ssh_server_manager.askpass import HOST_KEY_CONFIRM_ENV
from ssh_server_manager.credential_probe import CredentialProbeRunner, _Broker, _external_config, _method, _ProbeError, _ProcessTree, _terminate_windows_job, _windows_callback_command
from ssh_server_manager.credential_probe_helper import _request, _strict_descriptor, probe_askpass
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


def test_probe_environment_clears_interactive_host_confirmation(request_state, tmp_path, monkeypatch):
    database, credential, host, vault = request_state
    monkeypatch.setenv(HOST_KEY_CONFIRM_ENV, "terminal")
    # A synthetic existing launcher avoids consulting any installed application.
    launcher = tmp_path / "askpass.exe"
    launcher.touch()
    monkeypatch.setattr("ssh_server_manager.ssh_runner.SSHRunner._windows_askpass_candidates",
                        lambda: [launcher])
    runner = CredentialProbeRunner(database, lambda: vault)
    hops = runner._hops(host, credential)
    broker = _Broker(hops, lambda: vault)
    try:
        environment = runner._environment(hops, tmp_path, broker)
        assert HOST_KEY_CONFIRM_ENV not in environment
        assert environment["SSH_ASKPASS_REQUIRE"] == "force"
        assert environment["SSM_CREDENTIAL_PROBE_ENDPOINT"]
    finally:
        broker.close()


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


@pytest.mark.parametrize("proof", [
    "valid", "empty", "partial", "duplicate", "wrong-host", "too-large", "io-error", "observer-error"
])
def test_failed_proof_diagnostics_follow_single_read_and_cannot_change_result(tmp_path, monkeypatch, proof):
    import io
    import ssh_server_manager.credential_probe as module

    private = "private-proof-content-never-log"
    host = "fixture-private-hostname"
    path = tmp_path / private
    record = f'Authenticated to {host} ([127.0.0.1]:22) using "publickey".\n'.encode()
    data = {"valid": record, "empty": b"", "partial": record[:-14], "duplicate": record * 2,
            "wrong-host": record.replace(host.encode(), private.encode()),
            "too-large": b"x" * 65537, "observer-error": private.encode()}.get(proof, b"")
    events, summaries = [], []

    class Source(io.BytesIO):
        def read(self, size):
            assert size == 65537
            events.append("read")
            return super().read(size)

        def __exit__(self, *args):
            events.append("closed")
            return super().__exit__(*args)

    def opened(actual, mode):
        assert actual == path and mode == "rb"
        events.append("open")
        if proof == "io-error":
            error = PermissionError(13, private, str(path))
            error.winerror = 32
            raise error
        return Source(data)

    hop = {"hostname": host, "log": str(path)}

    def diagnosed(actual_hop, summary):
        assert actual_hop is hop
        assert events == (["open"] if proof == "io-error" else ["open", "read", "closed"])
        events.append("diagnostic")
        summaries.append(summary)
        if proof == "observer-error":
            raise RuntimeError(private)
        return "publickey"  # An observer cannot upgrade a rejected proof.

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(module, "_proof_diagnostics", diagnosed)
    result = module._method(hop)
    assert result == ("publickey" if proof == "valid" else None)
    assert events.count("open") == 1 and events.count("read") == (proof != "io-error")
    if proof == "valid":
        # No observer callback can delay the next hop's first proof read.
        assert summaries == [] and events == ["open", "read", "closed"]
    else:
        assert len(summaries) == 1 and events[-1] == "diagnostic"
        summary = summaries[0]
        if proof == "io-error":
            assert summary == {"outcome": "io-error", "errno": 13, "winerror": 32}
        elif proof == "too-large":
            assert summary == {"outcome": "too-large", "bytes": 65537}
        else:
            assert summary["outcome"] == "record-rejected" and summary["bytes"] == len(data)
            assert summary["modern_records"] == (2 if proof == "duplicate" else 0)
            assert summary["terminal_newline"] is data.endswith(b"\n")
        serialized = json.dumps(summary)
        assert private not in serialized and host not in serialized and str(path) not in serialized


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
        for prompt in ("password:", "fixture's password:", "127.0.0.1's password:",
                       "other@127.0.0.1's password:", "fixture@other's password:",
                       "banner fixture@127.0.0.1's password:", "fixture@127.0.0.1's password:\n"):
            assert probe_askpass(prompt) == 1
        assert capsys.readouterr().out == ""
        assert _request({"op": "secret", "stage": 0, "slot": "passphrase"}) == {"ok": False}
        assert probe_askpass("fixture@127.0.0.1's password:") == 0
        assert capsys.readouterr().out == "fixture-password-never-log"
        monkeypatch.setenv("SSM_CREDENTIAL_PROBE_STAGE", "1")
        assert probe_askpass("password:") == 1
        assert capsys.readouterr().out == ""
    finally:
        broker.close()


def test_passphrase_requires_exact_request_key_and_rejects_ambiguous_prefixes(
        request_state, monkeypatch, capsys, tmp_path):
    database, _, host, vault = request_state
    identity = tmp_path / "quote' space identity"
    identity.write_text("synthetic metadata fixture; not a real key")
    key_path = str(identity)
    selected = database.create_credential(label="Encrypted", kind="key", key_path=key_path, has_passphrase=True)
    vault.set_secret(selected["id"], "passphrase", "synthetic-test-passphrase")
    hops = CredentialProbeRunner(database, lambda: vault)._hops(host, selected)
    broker = _Broker(hops, lambda: vault)
    try:
        monkeypatch.setenv("SSM_CREDENTIAL_PROBE_ENDPOINT", f"127.0.0.1:{broker.server_address[1]}")
        monkeypatch.setenv("SSM_CREDENTIAL_PROBE_TOKEN", broker.token)
        monkeypatch.setenv("SSM_CREDENTIAL_PROBE_STAGE", "0")
        monkeypatch.setenv("SSM_ASKPASS_MAP", json.dumps(hops[0]["descriptors"]))
        for prompt in ("passphrase:", "Enter passphrase for key 'identity':",
                       "Enter passphrase for key '/other/identity':", "One-time passphrase code:"):
            assert probe_askpass(prompt) == 1
        assert capsys.readouterr().out == ""
        filename = key_path.encode("utf-8")[:100].decode("utf-8")
        assert probe_askpass(f"Enter passphrase for key '{filename}': ") == 0
        assert capsys.readouterr().out == "synthetic-test-passphrase"
    finally:
        broker.close()
    prefix = "/" + "a" * 99
    first = {"slot": "passphrase", "key_path": prefix + "one"}
    second = {"slot": "passphrase", "key_path": prefix + "two"}
    prompt = f"Enter passphrase for key '{prefix}': "
    assert _strict_descriptor(prompt, [first]) is first
    assert _strict_descriptor(prompt, [first, second]) is None
    alias = {"slot": "password", "username": "fixture", "hostname": "127.0.0.1",
             "prompt_hostname": "trusted-key-alias"}
    assert _strict_descriptor("fixture@trusted-key-alias's password: ", [alias]) is alias
    assert _strict_descriptor("fixture@127.0.0.1's password:", [alias]) is None


def test_broker_same_username_stages_cannot_select_another_host_secret(request_state, monkeypatch, capsys):
    database, credential, host, vault = request_state
    jump_credential = database.create_credential(label="Jump", kind="password", has_secret=True)
    vault.set_secret(jump_credential["id"], "password", "synthetic-jump-secret")
    database.create_server(alias="jump", hostname="127.0.0.2", username="fixture", port=22,
                           credential_id=jump_credential["id"])
    database.update_server(host["id"], proxy_jumps=["jump"])
    hops = CredentialProbeRunner(database, lambda: vault)._hops(database.get_server(host["id"]), credential)
    broker = _Broker(hops, lambda: vault)
    try:
        monkeypatch.setenv("SSM_CREDENTIAL_PROBE_ENDPOINT", f"127.0.0.1:{broker.server_address[1]}")
        monkeypatch.setenv("SSM_CREDENTIAL_PROBE_TOKEN", broker.token)
        for index, hop in enumerate(hops):
            monkeypatch.setenv("SSM_CREDENTIAL_PROBE_STAGE", str(index))
            monkeypatch.setenv("SSM_ASKPASS_MAP", json.dumps(hop["descriptors"]))
            other = hops[1 - index]
            assert probe_askpass(f"fixture@{other['hostname']}'s password:") == 1
            assert probe_askpass("fixture's password:") == 1
            assert capsys.readouterr().out == ""
            assert probe_askpass(f"fixture@{hop['hostname']}'s password: ") == 0
            assert capsys.readouterr().out == ("synthetic-jump-secret" if index == 0 else "fixture-password-never-log")
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
    ("cleanup-failed", "ssh-failed"),
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
            if proof == "cleanup-failed":
                raise OSError("request process ownership remains unconfirmed")
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
    if proof in {"missing-hop-callback", "cleanup-failed"}:
        assert reads == []
    else:
        assert reads == ["jump", "target"]
    completed_hops = [item for item in result["stages"] if item["stage"] == "hop" and item["status"] == "ok"]
    assert len(completed_hops) == (1 if expected_code is None else 0)
    if expected_code:
        assert not any(item["stage"] == "authenticate" and item["status"] == "ok"
                       for item in result["stages"])
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


class FakeJobKernel:
    """Model object completion separately from job membership accounting."""

    def __init__(self, monkeypatch, *, members=(101, 102), snapshots=None, active=(0,)):
        import ctypes

        self.events = []
        self.members = members
        self.snapshots = iter(snapshots or ())
        self.active = iter(active)
        self.last_error = 0
        self.handles = {}
        self.closed = []
        self.waited = []
        monkeypatch.setattr(ctypes, "get_last_error", lambda: self.last_error, raising=False)

    def QueryInformationJobObject(self, job, kind, information, size, returned):
        import ctypes

        if kind == 3:
            members = next(self.snapshots, self.members)
            capacity = len(information._obj.ProcessIdList)
            assert size == ctypes.sizeof(information._obj)
            information._obj.NumberOfAssignedProcesses = len(members)
            information._obj.NumberOfProcessIdsInList = min(capacity, len(members))
            for index, pid in enumerate(members[:capacity]):
                information._obj.ProcessIdList[index] = pid
            self.events.append(("snapshot", capacity, tuple(members)))
            self.last_error = 234 if len(members) > capacity else 0
            return len(members) <= capacity
        assert kind == 1
        # The extra references must not be held while waiting for accounting.
        assert set(self.handles) == set(self.closed)
        count = next(self.active, 0)
        information._obj.ActiveProcesses = count
        self.events.append(("account", count))
        return True

    def OpenProcess(self, access, inherit, pid):
        assert access == 0x00101000 and inherit is False
        handle = len(self.handles) + 1000
        self.handles[handle] = pid
        self.events.append(("open", pid, handle))
        return handle

    def IsProcessInJob(self, handle, job, owned):
        self.events.append(("member", self.handles[handle]))
        owned._obj.value = True
        return True

    def TerminateJobObject(self, job, status):
        assert status == 1
        self.events.append(("terminate",))
        return True

    def WaitForSingleObject(self, handle, milliseconds):
        assert ("terminate",) in self.events
        assert handle not in self.closed and 0 <= milliseconds <= 1000
        self.waited.append(handle)
        self.events.append(("wait", self.handles[handle], milliseconds))
        return 0

    def CloseHandle(self, handle):
        assert handle not in self.closed
        self.closed.append(handle)
        self.events.append(("close", self.handles.get(handle, "job")))
        return True


def test_windows_job_cleanup_waits_for_all_members_before_releasing_handle(monkeypatch, tmp_path):
    kernel = FakeJobKernel(monkeypatch, active=(1, 0))
    events = kernel.events
    job = object()

    class Process:
        def wait(self, *, timeout):
            assert events[-1] == ("close", "job")
            events.append(("parent-wait",))

    class Reader:
        def join(self, *, timeout):
            assert events[-1] == ("parent-wait",)
            events.append(("pipe-eof",))

        def is_alive(self):
            return False

    tree = object.__new__(_ProcessTree)
    tree.job = (kernel, job)
    tree.process = Process()
    tree.reader = Reader()
    log = tmp_path / "proof.log"
    log.write_text('Authenticated to fixture-host ([127.0.0.1]:22) using "publickey".\n')
    original_open = Path.open

    def proof_open(path, *args, **kwargs):
        if path == log:
            assert events[-1] == ("pipe-eof",)
            assert kernel.waited == list(kernel.handles)
            assert all(handle in kernel.closed for handle in kernel.handles)
            events.append(("proof",))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", proof_open)
    tree.close()
    assert _method({"log": str(log), "hostname": "fixture-host"}) == "publickey"
    assert [event[0] for event in events] == [
        "snapshot", "open", "member", "open", "member", "terminate", "wait", "wait",
        "close", "close", "account", "account", "close", "parent-wait", "pipe-eof", "proof"
    ]
    assert tree.job is None


@pytest.mark.parametrize("failure", ["terminate", "query", "active"])
def test_windows_job_barrier_fails_closed_on_unconfirmed_exit(failure, monkeypatch):
    class Kernel(FakeJobKernel):
        def TerminateJobObject(self, job, status):
            super().TerminateJobObject(job, status)
            return failure != "terminate"

        def QueryInformationJobObject(self, job, kind, information, size, returned):
            result = super().QueryInformationJobObject(job, kind, information, size, returned)
            if kind == 1:
                information._obj.ActiveProcesses = 1
                return failure != "query"
            return result

    kernel = Kernel(monkeypatch)
    with pytest.raises(OSError):
        _terminate_windows_job(kernel, object(), timeout=.01)
    assert ("terminate",) in kernel.events
    assert set(kernel.handles) == set(kernel.closed)


def test_windows_job_snapshot_grows_and_waits_for_every_child(monkeypatch):
    kernel = FakeJobKernel(monkeypatch, members=tuple(range(101, 138)))
    _terminate_windows_job(kernel, object())
    assert [event[1] for event in kernel.events if event[0] == "snapshot"] == [32, 64]
    assert len(kernel.waited) == 37 and set(kernel.waited) == set(kernel.closed)
    assert kernel.events[-1] == ("account", 0)


@pytest.mark.parametrize("race", ["exit", "pid-reuse"])
def test_windows_job_snapshot_reenumerates_natural_exit_or_reused_pid(race, monkeypatch):
    class Kernel(FakeJobKernel):
        def OpenProcess(self, access, inherit, pid):
            if race == "exit" and pid == 102:
                self.last_error = 87  # PID disappeared before a handle was acquired.
                self.events.append(("gone", pid))
                return None
            return super().OpenProcess(access, inherit, pid)

        def IsProcessInJob(self, handle, job, owned):
            result = super().IsProcessInJob(handle, job, owned)
            if race == "pid-reuse" and self.handles[handle] == 102:
                owned._obj.value = False
            return result

    kernel = Kernel(monkeypatch, snapshots=((101, 102), (101, 103)))
    _terminate_windows_job(kernel, object())
    assert [event[2] for event in kernel.events if event[0] == "snapshot"] == [(101, 102), (101, 103)]
    assert [kernel.handles[handle] for handle in kernel.waited] == [101, 103]
    assert set(kernel.handles) == set(kernel.closed)


@pytest.mark.parametrize("failure", [
    "snapshot-query", "snapshot-exception", "access-denied", "membership-query",
    "membership-changes", "duplicate-pid", "too-many-pids", "invalid-pid",
    "invalid-size", "incomplete-list", "wait-timeout", "wait-failed", "handle-release"
])
def test_windows_job_snapshot_or_object_failure_still_terminates_and_closes(failure, monkeypatch):
    class Kernel(FakeJobKernel):
        def QueryInformationJobObject(self, job, kind, information, size, returned):
            if kind == 3:
                if failure == "snapshot-exception":
                    raise ValueError("synthetic observer failure")
                result = super().QueryInformationJobObject(job, kind, information, size, returned)
                if failure == "snapshot-query":
                    self.last_error = 5
                    return False
                if failure == "invalid-size":
                    information._obj.NumberOfProcessIdsInList = len(information._obj.ProcessIdList) + 1
                if failure == "incomplete-list":
                    information._obj.NumberOfAssignedProcesses = len(information._obj.ProcessIdList)
                    information._obj.NumberOfProcessIdsInList = 0
                return result
            return super().QueryInformationJobObject(job, kind, information, size, returned)

        def OpenProcess(self, access, inherit, pid):
            if failure == "access-denied" and pid == 102:
                self.last_error = 5
                return None
            return super().OpenProcess(access, inherit, pid)

        def IsProcessInJob(self, handle, job, owned):
            result = super().IsProcessInJob(handle, job, owned)
            if failure == "membership-query":
                return False
            if failure == "membership-changes":
                owned._obj.value = False
            return result

        def WaitForSingleObject(self, handle, milliseconds):
            super().WaitForSingleObject(handle, milliseconds)
            return {"wait-timeout": 258, "wait-failed": 0xffffffff}.get(failure, 0)

        def CloseHandle(self, handle):
            result = super().CloseHandle(handle)
            return result if failure != "handle-release" or len(self.closed) != 1 else False

    members = {"duplicate-pid": (101, 101), "too-many-pids": tuple(range(1, 1026)),
               "invalid-pid": (0,)}.get(failure, (101, 102))
    kernel = Kernel(monkeypatch, members=members)
    with pytest.raises(OSError):
        _terminate_windows_job(kernel, object())
    assert kernel.events.count(("terminate",)) == 1
    assert set(kernel.handles) == set(kernel.closed)
    assert not any(event[0] == "account" for event in kernel.events)
    if failure == "membership-changes":
        assert len([event for event in kernel.events if event[0] == "snapshot"]) == 3


def test_windows_job_object_waits_share_one_cleanup_deadline(monkeypatch):
    from ssh_server_manager import credential_probe as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    class Kernel(FakeJobKernel):
        def WaitForSingleObject(self, handle, milliseconds):
            super().WaitForSingleObject(handle, milliseconds)
            clock[0] += .8
            return 0 if len(self.waited) == 1 else 258

    kernel = Kernel(monkeypatch)
    with pytest.raises(OSError, match="process exit remains unconfirmed"):
        _terminate_windows_job(kernel, object(), timeout=1)
    assert [event[2] for event in kernel.events if event[0] == "wait"] == [1000, 200]
    assert set(kernel.handles) == set(kernel.closed)


def test_stderr_reader_consumes_short_live_output_and_owns_pipe_close():
    tree = _ProcessTree([sys.executable, "-c",
                         "import sys,time; sys.stderr.write('safe'); sys.stderr.flush(); time.sleep(3)"],
                        os.environ.copy())
    try:
        deadline = time.monotonic() + 2
        while not tree.errors and time.monotonic() < deadline:
            time.sleep(.01)
        assert bytes(tree.errors) == b"safe"
        assert tree.process.poll() is None
    finally:
        tree.close()
    assert not tree.reader.is_alive() and tree.process.stderr.closed


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
        self.workers = []
        self.worker_lock = threading.Lock()
        self.none_attempts = 0
        self.stop = threading.Event()
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.socket.settimeout(.1)
        self.port = self.socket.getsockname()[1]
        owner = self

        class Server(paramiko.ServerInterface):
            def check_auth_none(self, username):
                owner.none_attempts += 1
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
            self._start_worker(self._serve, connection)

    def _start_worker(self, target, *arguments):
        worker = threading.Thread(target=target, args=arguments, daemon=True)
        with self.worker_lock:
            if self.stop.is_set():
                for argument in arguments:
                    if hasattr(argument, "close"):
                        argument.close()
                return
            self.workers.append(worker)
            worker.start()

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
                remote.settimeout(.2)
                channel.settimeout(.2)

                def copy(source, destination):
                    try:
                        while not self.stop.is_set():
                            try:
                                chunk = source.recv(32768)
                            except socket.timeout:
                                continue
                            if not chunk:
                                break
                            destination.sendall(chunk)
                    except (OSError, EOFError):
                        pass
                    finally:
                        source.close()
                        destination.close()

                self._start_worker(copy, channel, remote)
                self._start_worker(copy, remote, channel)
        except (OSError, EOFError, self.paramiko.SSHException):
            pass
        finally:
            transport.close()

    def close(self):
        self.stop.set()
        self.socket.close()
        self.thread.join(timeout=1)
        for transport in self.transports:
            transport.close()
        for connection in self.connections:
            connection.close()
        with self.worker_lock:
            workers = list(self.workers)
        for worker in workers:
            worker.join(timeout=1)


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


def _ssh_client_metadata(binary):
    """Keep version diagnostics bounded and exclude executable paths/errors."""
    location = "missing"
    if binary:
        normalized = str(binary).replace("\\", "/").casefold()
        location = "other"
        if os.name == "nt" and normalized.endswith("/system32/openssh/ssh.exe"):
            location = "windows-system"
        elif os.name == "nt" and normalized.endswith("/usr/bin/ssh.exe"):
            location = "git-msys"
    metadata = {"location": location, "family": "unknown", "version": None}
    if not binary:
        return metadata
    try:
        result = subprocess.run([binary, "-V"], capture_output=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return metadata
    # Only the fixed OpenSSH prefix and numeric version may reach a failure.
    # Do not copy arbitrary output from a launcher into the diagnostic JSON.
    output = (result.stdout[:512] + result.stderr[:512]).decode("ascii", "replace")
    match = re.search(r"(?m)^(OpenSSH_for_Windows|OpenSSH)_([0-9]{1,2}\.[0-9]{1,2}(?:p[0-9]{1,2})?)(?:[,\s]|$)", output)
    if match:
        metadata.update(family="windows-native" if match[1] == "OpenSSH_for_Windows" else "openssh-portable",
                        version=match[2])
    return metadata


def test_ssh_client_metadata_never_returns_launcher_output(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, stdout=b"", stderr=b"OpenSSH_for_Windows_9.5p2, LibreSSL 3.8.2\r\nsecret=never-emit\n"))
    metadata = _ssh_client_metadata("fixture-client")
    assert metadata == {"location": "other", "family": "windows-native", "version": "9.5p2"}
    assert "never-emit" not in json.dumps(metadata)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 1, stdout=b"secret=never-emit", stderr=b"untrusted launcher error"))
    assert _ssh_client_metadata("fixture-client") == {"location": "other", "family": "unknown", "version": None}


@pytest.fixture
def probe_evidence(monkeypatch):
    """Safe failure diagnostics for the exact real fixtures that CI runs."""
    import ssh_server_manager.credential_probe as module
    import windows_probe_evidence

    # Inspect the same default selection as CredentialProbeRunner before its
    # authentication deadline begins. The -V command makes no SSH connection.
    proof_observer = os.environ.get("SSM_TEST_NATIVE_PROOF") != "1"
    evidence = {"client": _ssh_client_metadata(shutil.which("ssh")), "proof_observer": proof_observer,
                "events": [], "processes": [], "method_checks": [], "method_reads": [], "proof_results": []}
    original_tree = module._ProcessTree
    original_broker = module._Broker
    original_method = module._method
    original_directory = module.tempfile.TemporaryDirectory
    registered_trees = {}
    registered_hops = {}
    snapshotting = False

    class EventQueue(queue.Queue):
        def put(self, item, *args, **kwargs):
            evidence["events"].append(item)
            return super().put(item, *args, **kwargs)

    class EvidenceBroker(original_broker):
        def __init__(self, hops, vault_factory):
            registered_hops.update((id(hop), (hop, index)) for index, hop in enumerate(hops))
            super().__init__(hops, vault_factory)
            self.events = EventQueue()

    class EvidenceTree(original_tree):
        def __init__(self, command, environment):
            manifest_path = Path(environment["SSM_CREDENTIAL_PROBE_MANIFEST"])
            manifest = json.loads(manifest_path.read_text())
            self.hops = manifest["hops"]
            self.cleanup_observed = False
            self.callback_event_offset = len(evidence["events"])
            self.callback_processes = None
            super().__init__(command, environment)
            registered_trees.setdefault(manifest_path.parent, []).append(self)

        def close(self):
            if not self.cleanup_observed:
                self.returncode_before_cleanup = self.process.poll()
                self.cleanup_observed = True
                authenticated = {event[1] for event in evidence["events"][self.callback_event_offset:]
                                 if isinstance(event, tuple) and len(event) == 2 and
                                 event[0] == "authenticated" and type(event[1]) is int}
                if getattr(self, "job", None) and authenticated != set(range(len(self.hops))):
                    try:
                        self.callback_processes = windows_probe_evidence.snapshot_job_process_families(*self.job)
                    except Exception:
                        self.callback_processes = {"diagnostic_error": True}
            # No private-log reads or extra parser calls here: production's
            # proof check must immediately follow its own process barrier.
            super().close()

        def snapshot(self):
            logs = []
            for hop in self.hops:
                try:
                    with Path(hop["log"]).open("rb") as source:
                        proof = source.read(65537)
                    logs.append({
                        "bytes": len(proof), "readable": True,
                        "authentication_records": proof.count(b"Authenticated to "),
                        "legacy_method_records": proof.count(b"Authentication succeeded ("),
                        "connection_established": b"Connection established." in proof,
                        "password_started": b"Next authentication method: password" in proof,
                        "post_auth_no_more_sessions": b"Requesting no-more-sessions@openssh.com" in proof,
                        "after_local_command_entered_loop": b"Entering interactive session." in proof,
                        "method": original_method(hop),
                    })
                except OSError:
                    logs.append({"readable": False})
            errors = bytes(self.errors).lower()
            evidence["processes"].append({
                "returncode_before_cleanup": self.returncode_before_cleanup, "logs": logs,
                "callback_processes": self.callback_processes,
                "stderr_python_traceback": b"traceback (most recent call last)" in errors,
                "stderr_helper_missing": b"can't open file" in errors,
                "stderr_command_missing": b"is not recognized" in errors,
                "stderr_permissions_rejected": b"bad permissions" in errors or b"bad owner" in errors,
            })

    class EvidenceDirectory(original_directory):
        def cleanup(self):
            nonlocal snapshotting
            # tempfile is shared by other fixtures. Only directories actually
            # registered by this probe's process tree receive diagnostic reads.
            trees = registered_trees.pop(Path(self.name), [])
            try:
                snapshotting = True
                for tree in trees:
                    try:
                        tree.snapshot()
                    except Exception:
                        # Observation must never replace the real result or
                        # reveal an exception containing private paths/data.
                        evidence["processes"].append({"diagnostic_error": True})
            finally:
                snapshotting = False
                super().cleanup()

    def failed_proof(hop, summary):
        # The production parser reports only after deciding to reject its
        # actual single read. Later cleanup observations are kept separate.
        registered = registered_hops.get(id(hop))
        if not snapshotting and registered is not None and registered[0] is hop and 0 <= registered[1] < 32:
            evidence["proof_results"].append({"hop_index": registered[1], **summary})

    def checked_method(hop):
        original_open = Path.open
        proof_path = Path(hop["log"])
        read = {"readable": None}

        def observed_open(path, *args, **kwargs):
            try:
                source = original_open(path, *args, **kwargs)
            except OSError as exc:
                if path == proof_path:
                    read.update(readable=False, errno=exc.errno, winerror=getattr(exc, "winerror", None))
                raise
            if path == proof_path:
                read["readable"] = True
            return source

        # Observe only the real parser's existing open, with no extra reads.
        # Preserve the original exception so production still fails closed.
        with monkeypatch.context() as observation:
            observation.setattr(Path, "open", observed_open)
            method = original_method(hop)
        evidence["method_checks"].append(method)
        evidence["method_reads"].append(read)
        return method

    monkeypatch.setattr(module, "_ProcessTree", EvidenceTree)
    monkeypatch.setattr(module, "_Broker", EvidenceBroker)
    if proof_observer:
        monkeypatch.setattr(module, "_method", checked_method)
    monkeypatch.setattr(module, "_proof_diagnostics", failed_proof)
    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", EvidenceDirectory)
    return evidence


@pytest.mark.parametrize("callbacks", ["complete", "missing", "observer-error"])
def test_callback_process_observation_is_failure_only_and_scoped_to_request(
        tmp_path, monkeypatch, probe_evidence, callbacks):
    import ssh_server_manager.credential_probe as module
    import windows_probe_evidence

    events = []
    job = (object(), object())
    roles = {"families": {"ssh": 1, "python": 0, "powershell": 1, "cmd": 1, "other": 0, "unknown": 0},
             "errors": [], "complete": True}

    class RunningProcess:
        def poll(self):
            return None

    def started(tree, command, environment):
        tree.process = RunningProcess()
        tree.errors = bytearray()
        tree.job = job

    def closed(tree):
        events.append("production-close")
        tree.job = None

    def observed(kernel, current_job):
        assert (kernel, current_job) == job
        assert events == []
        events.append("callback-observation")
        if callbacks == "observer-error":
            raise RuntimeError("private-account-token-never-log")
        return roles

    monkeypatch.setattr(_ProcessTree, "__init__", started)
    monkeypatch.setattr(_ProcessTree, "close", closed)
    monkeypatch.setattr(windows_probe_evidence, "snapshot_job_process_families", observed)
    # Earlier requests' complete callbacks must not count toward this tree.
    probe_evidence["events"].extend([("authenticated", 0), ("authenticated", 1), ("authenticated", 2)])
    with module.tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        manifest = Path(directory, "request.json")
        manifest.write_text(json.dumps({"hops": [
            {"log": str(Path(directory, "hop-0.log")), "hostname": "fixture"},
            {"log": str(Path(directory, "hop-1.log")), "hostname": "fixture"}
        ]}))
        tree = module._ProcessTree([], {"SSM_CREDENTIAL_PROBE_MANIFEST": str(manifest)})
        probe_evidence["events"].append(("authenticated", 0))
        if callbacks == "complete":
            probe_evidence["events"].append(("authenticated", 1))
        tree.close()
        tree.close()
        assert events == (["production-close", "production-close"] if callbacks == "complete" else
                          ["callback-observation", "production-close", "production-close"])
    expected = None if callbacks == "complete" else roles if callbacks == "missing" else {"diagnostic_error": True}
    assert probe_evidence["processes"][0]["callback_processes"] == expected
    assert "private-account-token-never-log" not in json.dumps(probe_evidence)


def _fail_probe(result, evidence, *, expected_hop_count, server_password_attempts=None, server_none_attempts=None):
    """Emit fixed result/stage metadata, never messages, aliases or paths."""
    failure = {
        "result_code": result["error_code"], "latency_ms": result["latency_ms"],
        "method": result["method"], "expected_hop_count": expected_hop_count,
        "stages": [{field: item[field] for field in ("stage", "status", "hop_index", "method", "error_code")
                    if field in item} for item in result["stages"]],
        "sanitized_evidence": evidence,
    }
    if server_password_attempts is not None:
        failure["server_password_attempts"] = server_password_attempts
    if server_none_attempts is not None:
        failure["server_none_attempts"] = server_none_attempts
    pytest.fail("Credential fixture authentication evidence:\n" +
                json.dumps(failure, ensure_ascii=True, indent=2), pytrace=False)


@pytest.mark.parametrize("native_proof", ["valid", "missing"])
def test_native_probe_evidence_preserves_original_parser_and_cleanup_snapshot(
        request_state, monkeypatch, request, native_proof):
    import ssh_server_manager.credential_probe as module

    monkeypatch.setenv("SSM_TEST_NATIVE_PROOF", "1")
    original_method = module._method
    evidence = request.getfixturevalue("probe_evidence")
    assert module._method is original_method is _method
    database, credential, host, vault = request_state
    brokers, logs = [], []

    def broker_start(broker, hops, vault_factory):
        brokers.append(broker)

    class RunningProcess:
        def poll(self):
            return None

    def tree_start(tree, command, environment):
        tree.process = RunningProcess()
        tree.errors = bytearray()
        logs.extend(Path(hop["log"]) for hop in tree.hops)
        brokers[-1].events.put(("authenticated", 0))

    def tree_close(tree):
        for log in logs:
            log.write_text('Authenticated to 127.0.0.1 ([127.0.0.1]:22) using "password".\n'
                           if native_proof == "valid" else "")

    monkeypatch.setattr(_Broker, "__init__", broker_start)
    monkeypatch.setattr(_Broker, "close", lambda broker: None)
    monkeypatch.setattr(_ProcessTree, "__init__", tree_start)
    monkeypatch.setattr(_ProcessTree, "close", tree_close)
    runner = CredentialProbeRunner(database, lambda: vault)
    monkeypatch.setattr(runner, "_environment", lambda hops, directory, broker: {
        "SSM_CREDENTIAL_PROBE_MANIFEST": str(directory / "request.json")})

    result = runner.test(credential["id"], host["id"])

    valid = native_proof == "valid"
    assert result["ok"] is valid and result["method"] == ("password" if valid else None)
    assert result["error_code"] == (None if valid else "authentication-unconfirmed")
    assert module._method is original_method
    assert evidence["proof_observer"] is False
    assert evidence["events"] == [("authenticated", 0)]
    assert evidence["method_checks"] == evidence["method_reads"] == []
    assert evidence["processes"][0]["logs"][0]["method"] == ("password" if valid else None)
    assert evidence["processes"][0]["logs"][0]["readable"] is True
    assert len(evidence["proof_results"]) == (0 if valid else 1)
    if not valid:
        assert evidence["proof_results"][0]["hop_index"] == 0
        assert evidence["proof_results"][0]["outcome"] == "record-rejected"
        assert evidence["proof_results"][0]["bytes"] == 0
    assert not logs[0].exists()


@pytest.mark.parametrize("initial_proof", ["valid", "missing", "read-error", "diagnostic-error"])
def test_probe_evidence_reads_logs_only_after_production_proof(
        request_state, monkeypatch, probe_evidence, tmp_path, initial_proof):
    import ssh_server_manager.credential_probe as module

    database, credential, host, vault = request_state
    brokers, reads, logs = [], [], []
    original_open = Path.open
    valid_proof = initial_proof in {"valid", "diagnostic-error"}
    expected_method = "password" if valid_proof else None

    def broker_start(broker, hops, vault_factory):
        broker.server_address = ("127.0.0.1", 0)
        broker.token = "fixture-capability-not-used"
        brokers.append(broker)

    class RunningProcess:
        def poll(self):
            return None

    def tree_start(tree, command, environment):
        tree.process = RunningProcess()
        tree.errors = bytearray()
        logs.extend(Path(hop["log"]) for hop in tree.hops)
        brokers[-1].events.put(("authenticated", 0))

    def tree_close(tree):
        for log in logs:
            log.write_text('Authenticated to 127.0.0.1 ([127.0.0.1]:22) using "password".\n'
                           if initial_proof != "missing" else "")
        # The process barrier itself must not trigger observation reads.
        assert reads == []

    def observe_open(path, mode="r", *args, **kwargs):
        if path in logs and mode == "rb":
            # The first read belongs to the real parser. Only later cleanup
            # reads may observe its recorded result, including a strict failure.
            assert probe_evidence["method_checks"] == ([] if not reads else [expected_method])
            assert probe_evidence["processes"] == []
            reads.append(path)
            if initial_proof == "read-error" and len(reads) == 1:
                raise PermissionError(13, "private-proof-path-never-log")
            if initial_proof == "diagnostic-error" and len(reads) == 2:
                raise RuntimeError("private-proof-path-never-log")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(_Broker, "__init__", broker_start)
    monkeypatch.setattr(_Broker, "close", lambda broker: None)
    monkeypatch.setattr(_ProcessTree, "__init__", tree_start)
    monkeypatch.setattr(_ProcessTree, "close", tree_close)
    monkeypatch.setattr(Path, "open", observe_open)
    runner = CredentialProbeRunner(database, lambda: vault)
    monkeypatch.setattr(runner, "_environment", lambda hops, directory, broker: {
        "SSM_CREDENTIAL_PROBE_MANIFEST": str(directory / "request.json")})

    # Patching the shared tempfile module must leave unrelated directories
    # completely unobserved and preserve their normal cleanup.
    with module.tempfile.TemporaryDirectory(dir=tmp_path) as unrelated:
        Path(unrelated, "hop-unrelated.log").write_text("unrelated fixture")
    assert not Path(unrelated).exists() and probe_evidence["processes"] == []

    result = runner.test(credential["id"], host["id"])

    assert result["ok"] is valid_proof
    assert result["error_code"] == (None if valid_proof else "authentication-unconfirmed")
    # Production parser, cleanup snapshot, then diagnostic parser (unless the
    # diagnostic snapshot failed without affecting the production result).
    assert reads == logs * (2 if initial_proof == "diagnostic-error" else 3)
    assert probe_evidence["method_checks"] == [expected_method]
    assert probe_evidence["method_reads"] == ([{"readable": False, "errno": 13, "winerror": None}]
                                               if initial_proof == "read-error" else [{"readable": True}])
    assert len(probe_evidence["proof_results"]) == (0 if valid_proof else 1)
    if not valid_proof:
        assert probe_evidence["proof_results"][0]["hop_index"] == 0
        assert probe_evidence["proof_results"][0]["outcome"] == (
            "io-error" if initial_proof == "read-error" else "record-rejected")
    # A proof that becomes readable during later diagnostics cannot upgrade
    # the production failure to success or replace its actual method result.
    if initial_proof == "diagnostic-error":
        assert probe_evidence["processes"] == [{"diagnostic_error": True}]
    else:
        assert probe_evidence["processes"][0]["logs"][0]["method"] == (
            "password" if initial_proof != "missing" else None)
    assert "private-proof-path-never-log" not in json.dumps(probe_evidence)
    assert not logs[0].exists()


@pytest.mark.parametrize("none,expected", [(False, True), (True, False)])
def test_real_authentication_password_none_and_banner_isolation(
        request_state, loopback_servers, tmp_path, none, expected, probe_evidence):
    database, credential, host, vault = request_state
    banner = 'Authenticated to 127.0.0.1 ([127.0.0.1]:22) using "password".\nsecret=remote-banner-never-log\n'
    server = loopback_servers(none=none, banner=banner)
    database.update_server(host["id"], port=server.port)
    snapshot = database.get_server(host["id"])
    # Allow shared Windows CI more time for SSH/system PowerShell startup.
    # This checks authentication/banner isolation;
    # explicit deadline and cancellation tests keep their short budgets.
    result = fixture_runner(database, vault, tmp_path, [server]).test(
        credential["id"], host["id"], timeout=15 if os.name == "nt" else 5)
    if (result["ok"] is not expected or result["method"] != ("none" if none else "password")
            or result["error_code"] != ("credential-not-used" if none else None)):
        _fail_probe(result, probe_evidence, expected_hop_count=1,
                    server_password_attempts=len(server.password_attempts), server_none_attempts=server.none_attempts)
    assert not server.sessions
    assert not server.forward_requests
    assert database.get_server(host["id"]) == snapshot
    assert "remote-banner-never-log" not in json.dumps(result)
    assert "fixture-password-never-log" not in json.dumps(result)
    assert not list(Path(os.environ["SSM_RUNTIME_DIR"]).glob("credential-probe-*"))


def test_real_host_key_alias_is_the_exact_password_prompt_host(request_state, loopback_servers, tmp_path, probe_evidence):
    database, credential, host, vault = request_state
    server = loopback_servers()
    database.update_server(host["id"], port=server.port)
    original = Path(os.environ["SSM_ORIGINAL_SSH_CONFIG"])
    original.write_text("Host target\n HostKeyAlias trusted-key-alias\n")
    private_ssh_fixture_file(original)
    runner = fixture_runner(database, vault, tmp_path, [server])
    (tmp_path / "known_hosts").write_text(
        f"trusted-key-alias {server.key.get_name()} {server.key.get_base64()}\n")
    result = runner.test(credential["id"], host["id"], timeout=8)
    if not result["ok"] or result["method"] != "password":
        _fail_probe(result, probe_evidence, expected_hop_count=1,
                    server_password_attempts=len(server.password_attempts))
    assert not server.sessions


def test_real_forwarding_survives_idle_then_closes_fixture_workers(loopback_servers):
    paramiko = pytest.importorskip("paramiko")
    server = loopback_servers()
    client = paramiko.SSHClient()
    client.get_host_keys().add(f"[127.0.0.1]:{server.port}", server.key.get_name(), server.key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(3)
    remote = None
    try:
        client.connect("127.0.0.1", port=server.port, username="fixture", password="fixture-password-never-log",
                       look_for_keys=False, allow_agent=False, timeout=3)
        channel = client.get_transport().open_channel("direct-tcpip", listener.getsockname(), ("127.0.0.1", 0))
        remote, _ = listener.accept()
        remote.settimeout(3)
        channel.settimeout(3)
        channel.sendall(b"before-idle")
        assert remote.recv(32) == b"before-idle"
        # Exceed the old create_connection timeout: an idle tunnel must not
        # disappear while another hop's local authentication helper starts.
        time.sleep(2.2)
        remote.sendall(b"after-idle")
        assert channel.recv(32) == b"after-idle"
        assert not server.sessions
    finally:
        client.close()
        if remote is not None:
            remote.close()
        listener.close()
        server.close()
    assert not server.thread.is_alive()
    assert not any(worker.is_alive() for worker in server.workers)


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
        request_state, loopback_servers, tmp_path, probe_evidence):
    """Distinguish a missing local callback from an unreadable proof on CI."""
    database, credential, host, vault = request_state
    server = loopback_servers()
    database.update_server(host["id"], port=server.port)
    result = fixture_runner(database, vault, tmp_path, [server]).test(credential["id"], host["id"], timeout=5)
    if not result["ok"] or result["method"] != "password":
        _fail_probe(result, probe_evidence, expected_hop_count=1,
                    server_password_attempts=len(server.password_attempts))
    assert probe_evidence["method_checks"] == ["password"]
    log_summary = probe_evidence["processes"][0]["logs"][0]
    assert type(log_summary["post_auth_no_more_sessions"]) is bool
    assert type(log_summary["after_local_command_entered_loop"]) is bool
    assert "fixture-password-never-log" not in json.dumps(probe_evidence)
    assert "127.0.0.1" not in json.dumps(probe_evidence)
    assert not server.sessions


def test_real_callback_and_proxy_handle_runtime_path_metacharacters(
        request_state, loopback_servers, tmp_path, monkeypatch, probe_evidence):
    database, credential, host, vault = request_state
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime & percent% bang! quote' 中文"))
    jump = loopback_servers()
    target = loopback_servers()
    database.create_server(alias="jump", hostname="127.0.0.1", username="fixture", port=jump.port,
                           credential_id=credential["id"])
    database.update_server(host["id"], port=target.port, proxy_jumps=["jump"])
    result = fixture_runner(database, vault, tmp_path, [jump, target]).test(
        credential["id"], host["id"], timeout=8)
    if not result["ok"] or result["method"] != "password":
        _fail_probe(result, probe_evidence, expected_hop_count=2,
                    server_password_attempts=[len(server.password_attempts) for server in (jump, target)])
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


def test_real_key_authentication_has_no_remote_session(request_state, loopback_servers, tmp_path, probe_evidence):
    paramiko = pytest.importorskip("paramiko")
    database, _, host, vault = request_state
    key = tmp_path / "identity"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    private_ssh_fixture_file(key)
    selected = database.create_credential(label="Fixture key", kind="key", key_path=str(key))
    server = loopback_servers(public_key=paramiko.Ed25519Key.from_private_key_file(str(key)))
    database.update_server(host["id"], port=server.port)
    result = fixture_runner(database, vault, tmp_path, [server]).test(selected["id"], host["id"], timeout=5)
    if not result["ok"] or result["method"] != "publickey":
        _fail_probe(result, probe_evidence, expected_hop_count=1,
                    server_password_attempts=len(server.password_attempts))
    assert not server.sessions and not server.password_attempts


def test_real_encrypted_key_uses_vault_passphrase(request_state, loopback_servers, tmp_path, probe_evidence):
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
    if not result["ok"] or result["method"] != "publickey":
        _fail_probe(result, probe_evidence, expected_hop_count=1,
                    server_password_attempts=len(server.password_attempts))
    assert passphrase not in json.dumps(result)
    assert not server.sessions and not server.password_attempts


def test_real_external_jump_uses_safe_key_configuration(request_state, loopback_servers, tmp_path, probe_evidence):
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
    if not result["ok"] or result["method"] != "password":
        _fail_probe(result, probe_evidence, expected_hop_count=2,
                    server_password_attempts=[len(server.password_attempts) for server in (jump, target)])
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


def test_real_multihop_keeps_same_username_credentials_separate(request_state, loopback_servers, tmp_path, probe_evidence):
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
    if not result["ok"]:
        _fail_probe(result, probe_evidence, expected_hop_count=3,
                    server_password_attempts=[len(server.password_attempts) for server in (first, second, target)])
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


def test_candidate_requires_a_password_credential(request_state):
    database, credential, host, vault = request_state
    agent = database.create_credential(label="Agent", kind="agent")
    runner = CredentialProbeRunner(database, lambda: vault)
    with pytest.raises(ValidationError):
        runner.test(agent["id"], host["id"], candidate="rotated-candidate-secret")
    with pytest.raises(ValidationError):
        runner.test(credential["id"], host["id"], candidate="")


def test_real_candidate_reaches_the_target_only_and_never_the_vault(
        request_state, loopback_servers, tmp_path, probe_evidence):
    database, credential, host, vault = request_state
    # The jump shares the credential and still has the saved password; only the
    # target's account was changed.
    jump = loopback_servers()
    target = loopback_servers(password="rotated-candidate-secret")
    database.create_server(alias="jump", hostname="127.0.0.1", username="fixture", port=jump.port,
                           credential_id=credential["id"])
    database.update_server(host["id"], port=target.port, proxy_jumps=["jump"])
    result = fixture_runner(database, vault, tmp_path, [jump, target]).test(
        credential["id"], host["id"], timeout=8, candidate="rotated-candidate-secret")
    if not result["ok"] or result["method"] != "password":
        _fail_probe(result, probe_evidence, expected_hop_count=2,
                    server_password_attempts=[len(server.password_attempts) for server in (jump, target)])
    assert jump.password_attempts == ["fixture-password-never-log"]
    assert target.password_attempts == ["rotated-candidate-secret"]
    assert result["credential_id"] == credential["id"]
    assert vault.get_secret(credential["id"], "password") == "fixture-password-never-log"
    assert "rotated-candidate-secret" not in json.dumps(result)
    assert not jump.sessions and not target.sessions
    assert not list(Path(os.environ["SSM_RUNTIME_DIR"]).glob("credential-probe-*"))


def test_real_rejected_candidate_is_an_authentication_failure(request_state, loopback_servers, tmp_path):
    database, credential, host, vault = request_state
    server = loopback_servers()
    database.update_server(host["id"], port=server.port)
    result = fixture_runner(database, vault, tmp_path, [server]).test(
        credential["id"], host["id"], timeout=5, candidate="not-yet-changed")
    assert not result["ok"] and result["error_code"] == "authentication-failed", result
    assert server.password_attempts == ["not-yet-changed"]
