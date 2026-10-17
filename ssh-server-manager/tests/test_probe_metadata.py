from __future__ import annotations

import base64
import hashlib
import subprocess
import sys

import pytest

from ssh_server_manager import probe_metadata
from ssh_server_manager.db import Database
from ssh_server_manager.ssh_runner import SSHRunner, _summarize_remote_identity


def key(seed=b"x"):
    kind = b"ssh-ed25519"
    blob = len(kind).to_bytes(4, "big") + kind + (32).to_bytes(4, "big") + seed * 32
    encoded = base64.b64encode(blob).decode()
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"box ssh-ed25519 {encoded}", f"SHA256:{digest}"


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    # Transport is mocked in these tests; keep the developer's ambient secret
    # environment out of captured calls and any assertion diagnostics.
    monkeypatch.setattr(SSHRunner, "_environment", lambda self, server: {})
    db = Database(tmp_path / "manager.db")
    db.create_server(alias="box", hostname="box.invalid", port=2222, username="alice")
    return db


def local_commands(monkeypatch, tmp_path, *, debug="", known=None, ssh_status=0):
    path = tmp_path / "known hosts"
    path.touch()
    line, fingerprint = key()
    calls = []
    monkeypatch.setattr(probe_metadata.shutil, "which", lambda name: "ssh-keygen")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert "ssh-keyscan" not in command
        if command[0] == sys.executable:
            assert "socket.getaddrinfo" in command[2]
            assert command[-2:] == ["box.invalid", "2222"]
            return subprocess.CompletedProcess(command, 0, "192.0.2.42\n", "")
        if "-G" in command:
            output = f'hostname box.invalid\nuserknownhostsfile "{path}"\nglobalknownhostsfile none\n'
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[0] == "ssh-keygen":
            assert command[1:] == ["-F", "[box.invalid]:2222", "-f", str(path)]
            return subprocess.CompletedProcess(command, 0, known or line, "")
        assert command[0] == "ssh" and "StrictHostKeyChecking=yes" in command
        assert "LogLevel=DEBUG1" in command
        assert "FingerprintHash=sha256" in command
        return subprocess.CompletedProcess(command, ssh_status, "", debug)

    monkeypatch.setattr(probe_metadata.subprocess, "run", run)
    return calls, fingerprint


def test_fingerprints_hash_public_blob_and_reject_non_host_records():
    line, fingerprint = key()
    _, other = key(b"y")
    assert probe_metadata.known_key_fingerprints("# comment\n" + line + "\n" + line) == {fingerprint}
    invalid = "box ssh-ed25519 !!!\nbox ssh-rsa " + line.split()[2]
    assert probe_metadata.known_key_fingerprints(invalid) == set()
    assert probe_metadata.known_key_fingerprints("@revoked " + line + "\n@cert-authority " + line) == set()
    assert fingerprint != other


def test_explicit_check_records_negotiated_key_and_local_dns(database, monkeypatch, tmp_path):
    _, fingerprint = key()
    calls, _ = local_commands(monkeypatch, tmp_path, debug=f"debug1: Server host key: ssh-ed25519 {fingerprint}\n")
    # Inventory reads alone do not resolve DNS, inspect config, or read keys.
    assert database.list_servers()[0]["host_key"] is None
    assert calls == []
    result = SSHRunner(database, ssh_binary="ssh").test("box")
    observed = database.get_server("box")
    assert result["ok"] is True
    assert observed["last_resolved_ip"] == "192.0.2.42"
    assert observed["host_key"]["fingerprint"] == fingerprint
    assert observed["host_key"]["state"] == "verified"
    assert observed["last_success_at"]
    assert observed["facts"] is None
    assert len(calls) == 4
    before = len(calls)
    database.get_server("box")
    database.list_servers()
    assert len(calls) == before


def test_observe_collects_sanitized_facts_without_sftp(database, monkeypatch, tmp_path):
    local_commands(monkeypatch, tmp_path)
    runner = SSHRunner(database, ssh_binary="ssh")
    monkeypatch.setattr(runner, "_probe_sftp", lambda *args, **kwargs: pytest.fail("Observation must not probe SFTP"))
    calls = []

    def probe(host, *, timeout):
        calls.append((host["id"], timeout))
        return {"ok": True, "latency_ms": 1, "message": "ok", "details": {
            "hostname": "box\x00\u202e", "os_id": "ubuntu", "os_pretty": "Ubuntu 24.04", "arch": "x86_64",
            "cpu_model": "z" * 900, "secret": "never persist",
        }}

    monkeypatch.setattr(runner, "_probe_remote_identity", probe)
    result = runner.observe("box", timeout=12)
    host = database.get_server("box")
    assert result["ok"] is True and result["error_code"] is None
    assert result["message"] == "Host info collected"
    assert calls == [(host["id"], 12)]
    assert host["facts"]["os_pretty"] == "Ubuntu 24.04"
    assert host["facts"]["hostname"] == "box" and len(host["facts"]["cpu_model"]) == 500
    assert host["facts"]["observed_at"] and "secret" not in host["facts"]
    assert host["last_test_status"] == "ok"


@pytest.mark.parametrize("handshake_ok", [True, False])
def test_failed_observation_preserves_previous_facts(database, monkeypatch, tmp_path, handshake_ok):
    database.record_observation("box", facts={"hostname": "box", "os_id": "debian"})
    previous = database.get_server("box")["facts"]
    local_commands(monkeypatch, tmp_path, ssh_status=0 if handshake_ok else 255, debug="Permission denied (publickey).")
    runner = SSHRunner(database, ssh_binary="ssh")
    probes = []

    def probe(*args, **kwargs):
        probes.append(True)
        return {"ok": False, "latency_ms": 1, "error_code": "timeout", "message": "token=hidden\x00\u202e"}

    monkeypatch.setattr(runner, "_probe_remote_identity", probe)
    result = runner.observe("box")
    assert result["ok"] is False
    assert result["error_code"] == ("timeout" if handshake_ok else "authentication-failed")
    assert len(probes) == int(handshake_ok)
    assert "hidden" not in result["message"] and "\x00" not in result["message"] and "\u202e" not in result["message"]
    host = database.get_server("box")
    assert host["facts"] == previous
    assert host["last_test_status"] == ("ok" if handshake_ok else "failed")


def test_observation_discards_facts_after_endpoint_change(database, monkeypatch, tmp_path):
    local_commands(monkeypatch, tmp_path)
    runner = SSHRunner(database, ssh_binary="ssh")

    def probe(*args, **kwargs):
        database.update_server("box", hostname="replacement.invalid")
        return {"ok": True, "latency_ms": 1, "message": "ok", "details": {"hostname": "old-box", "os_id": "ubuntu"}}

    monkeypatch.setattr(runner, "_probe_remote_identity", probe)
    result = runner.observe("box")
    assert result["ok"] is False and result["error_code"] == "host-changed"
    assert database.get_server("box")["facts"] is None


@pytest.mark.parametrize(("diagnostic", "state"), [
    ("Permission denied (publickey).", "unknown"),
    ("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\nHost key verification failed.", "changed"),
    ("WARNING: REVOKED HOST KEY DETECTED!\nHost key verification failed.", "revoked"),
])
def test_failed_check_invalidates_previous_verified_state(database, monkeypatch, tmp_path, diagnostic, state):
    _, fingerprint = key()
    database.record_observation("box", host_key={"fingerprint": fingerprint, "state": "verified"})
    local_commands(monkeypatch, tmp_path, debug="debug1: private local path\n" + diagnostic, ssh_status=255)
    result = SSHRunner(database, ssh_binary="ssh").test("box")
    assert result["ok"] is False
    assert "debug1" not in result["message"]
    assert database.get_server("box")["host_key"]["state"] == state


def test_unknown_negotiated_key_does_not_verify_unrelated_saved_key(database, monkeypatch, tmp_path):
    _, different = key(b"y")
    local_commands(monkeypatch, tmp_path, debug=f"debug1: Server host key: ssh-ed25519 {different}\n")
    SSHRunner(database, ssh_binary="ssh").test("box")
    assert database.get_server("box")["host_key"]["state"] == "unknown"


def test_successful_key_observation_uses_actual_path_with_spaces(database, monkeypatch, tmp_path):
    line, fingerprint = key()
    actual_path = tmp_path / "known hosts"
    calls = []
    monkeypatch.setattr(probe_metadata.shutil, "which", lambda name: "ssh-keygen")
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == sys.executable:
            return subprocess.CompletedProcess(command, 0, "192.0.2.42\n", "")
        if "-G" in command:
            # This is OpenSSH's actual dump format: path arrays are unquoted.
            return subprocess.CompletedProcess(command, 0,
                f"hostname box.invalid\nuserknownhostsfile {actual_path}\nglobalknownhostsfile none\n", "")
        if command[0] == "ssh-keygen":
            assert command[1:] == ["-F", "[box.invalid]:2222", "-f", str(actual_path)]
            return subprocess.CompletedProcess(command, 0, line, "")
        assert "StrictHostKeyChecking=yes" in command
        return subprocess.CompletedProcess(command, 0, "",
            f"debug1: Server host key: ssh-ed25519 {fingerprint}\ndebug1: Found key in {actual_path}:12\n")
    monkeypatch.setattr(probe_metadata.subprocess, "run", run)
    assert SSHRunner(database, ssh_binary="ssh").test("box")["ok"] is True
    assert database.get_server("box")["host_key"]["state"] == "verified"
    assert database.get_server("box")["host_key"]["fingerprint"] == fingerprint
    assert len(calls) == 4


def test_debug_metadata_does_not_override_actual_error_classification(database, monkeypatch, tmp_path):
    local_commands(monkeypatch, tmp_path, ssh_status=255,
                   debug="debug1: local identity: Permission denied\nConnection refused\n")
    result = SSHRunner(database, ssh_binary="ssh").test("box")
    assert result["error_code"] == "connection-refused"
    assert result["message"] == "Connection refused"


def test_check_failure_message_is_redacted_sanitized_and_bounded(database, monkeypatch, tmp_path):
    local_commands(monkeypatch, tmp_path, ssh_status=255,
                   debug="Permission denied. token=hidden\x00\u202e\n" + "x" * 8000)
    result = SSHRunner(database, ssh_binary="ssh").test("box")
    assert "hidden" not in result["message"]
    assert "[REDACTED]" in result["message"]
    assert "\x00" not in result["message"] and "\u202e" not in result["message"]
    assert len(result["message"]) <= 4000
    assert database.get_server("box")["last_test_message"] == result["message"][:500]


def test_multiple_saved_keys_require_negotiation_evidence(database, monkeypatch, tmp_path):
    one, _ = key()
    two, expected = key(b"y")
    local_commands(monkeypatch, tmp_path, known=one + "\n" + two, debug=f"debug1: Server host key: ssh-ed25519 {expected}\n")
    SSHRunner(database, ssh_binary="ssh").test("box")
    assert database.get_server("box")["host_key"]["fingerprint"] == expected
    local_commands(monkeypatch, tmp_path, known=one + "\n" + two)
    SSHRunner(database, ssh_binary="ssh").test("box")
    assert database.get_server("box")["host_key"]["state"] == "unknown"
    assert database.get_server("box")["host_key"]["fingerprint"] is None


def test_metadata_deadline_kills_dns_and_does_not_start_more_processes(monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(probe_metadata.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(probe_metadata.shutil, "which", lambda name: "ssh-keygen")

    def timeout(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        clock[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(probe_metadata.subprocess, "run", timeout)
    observed = probe_metadata.collect_probe_metadata(
        {"alias": "box", "hostname": "box.invalid", "port": 22},
        ssh_binary="ssh", config="/fixture/config", success=False, stderr="", timeout=99,
    )
    assert len(calls) == 1
    assert calls[0][1] == 2.0
    assert observed["resolved_ip"] is None
    assert observed["host_key"]["state"] == "unknown"


def test_literal_addresses_need_no_resolver_process(monkeypatch):
    monkeypatch.setattr(probe_metadata.subprocess, "run", lambda *args, **kwargs: pytest.fail("DNS subprocess on literal IP"))
    assert probe_metadata.resolve_configured_ip("[2001:db8::2]", 22, deadline=0) == "2001:db8::2"


def test_malformed_local_config_observation_is_best_effort(monkeypatch):
    monkeypatch.setattr(probe_metadata.shutil, "which", lambda name: "ssh-keygen")
    monkeypatch.setattr(probe_metadata.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, "hostname\nuserknownhostsfile ~nonexistent-user/keys\n", ""))
    assert probe_metadata.read_known_fingerprints(
        {"alias": "box", "hostname": "box.invalid", "port": 22},
        ssh_binary="ssh", config="/fixture/config", deadline=probe_metadata.time.monotonic() + 2,
    ) == set()


@pytest.mark.parametrize("reachable", [True, False])
def test_diagnose_external_jump_defers_reachability_to_ssh(database, monkeypatch, reachable):
    database.update_server("box", proxy_jumps=["external-gateway"])
    runner = SSHRunner(database, ssh_binary="ssh")
    monkeypatch.setattr(runner, "test", lambda *args, **kwargs:
                        {"ok": reachable, "message": "ok" if reachable else "Could not resolve hostname external-gateway",
                         "latency_ms": 1, "error_code": None if reachable else "dns-failed"})
    monkeypatch.setattr(runner, "_probe_sftp", lambda *args, **kwargs: {"ok": True, "message": "ok", "latency_ms": 1})
    monkeypatch.setattr(runner, "_probe_remote_identity", lambda *args, **kwargs: {"ok": True, "message": "ok", "latency_ms": 1})
    result = runner.diagnose("box")
    assert result["checks"][0]["status"] == "warning"
    assert "OpenSSH configuration" in result["checks"][0]["message"]
    assert result["overall"] == ("warning" if reachable else "failed")
    assert result["checks"][1]["status"] == ("ok" if reachable else "failed")
    if not reachable:
        assert result["checks"][1]["error_code"] == "dns-failed"
        assert [check["status"] for check in result["checks"][2:]] == ["skipped", "skipped"]


def test_diagnose_persists_only_successful_sanitized_identity(database, monkeypatch):
    runner = SSHRunner(database, ssh_binary="ssh")
    monkeypatch.setattr(runner, "test", lambda *args, **kwargs: {"ok": True, "message": "ok", "latency_ms": 1})
    monkeypatch.setattr(runner, "_probe_sftp", lambda *args, **kwargs: {"ok": True, "message": "ok", "latency_ms": 1})
    raw = {"hostname": "box\x00\u202e", "os_id": "custom-linux", "os_like": "ubuntu debian", "cpu_model": "z" * 900, "secret": "never persist"}
    monkeypatch.setattr(runner, "_probe_remote_identity", lambda *args, **kwargs: {"ok": True, "details": raw, "message": "ok", "latency_ms": 1})
    result = runner.diagnose("box")
    facts = database.get_server("box")["facts"]
    assert facts["hostname"] == "box"
    assert facts["os_logo"] == "ubuntu"
    assert len(facts["cpu_model"]) == 500
    assert "secret" not in facts
    assert result["checks"][3]["details"] == facts
    monkeypatch.setattr(runner, "_probe_remote_identity", lambda *args, **kwargs: {"ok": False, "message": "failed", "latency_ms": 1})
    runner.diagnose("box")
    assert database.get_server("box")["facts"] == facts


def test_identity_summary_sanitizes_response_and_closed_logo():
    facts = _summarize_remote_identity({"hostname": "x\x00", "os_id": "../../invented", "kernel": "Linux\x1b", "os_pretty": "a" * 700})
    assert facts["hostname"] == "x"
    assert facts["os_logo"] == "unknown"
    assert len(facts["os"]) == 500
    assert facts["os_pretty"] == facts["os"]
    assert facts["kernel"] == "Linux"
