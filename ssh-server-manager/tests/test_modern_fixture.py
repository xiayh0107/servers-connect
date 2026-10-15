"""Actual OpenSSH integration against the isolated manual/browser fixture."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("paramiko")
if not shutil.which("ssh") or not shutil.which("sftp"):
    pytest.skip("OpenSSH client required for loopback integration", allow_module_level=True)

from ssh_server_manager.ssh_runner import FileOperationError, SSHRunner
from ssh_server_manager.credential_probe import CredentialProbeRunner
from test_credential_probe import probe_evidence

spec = importlib.util.spec_from_file_location("ssm_modern_fixture", Path(__file__).parent / "manual" / "modern_fixture.py")
modern_fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(modern_fixture)


@pytest.fixture(scope="module")
def loopback():
    with modern_fixture.fixture_environment() as fixture:
        yield fixture


@pytest.mark.parametrize("alias", ["direct", "multi-hop", "external-route"])
def test_real_health_and_diagnosis(loopback, alias):
    runner = SSHRunner(loopback["database"])
    result = runner.test(alias, timeout=4)
    assert result["ok"], result
    diagnosis = runner.diagnose(alias, timeout=4)
    assert all(item["status"] in {"ok", "warning"} for item in diagnosis["checks"]), diagnosis
    host = loopback["database"].get_server(alias)
    assert host["facts"]["os_pretty"] == "Fixture Linux"
    assert host["host_key"]["state"] == "verified"
    assert host["last_success_at"] and host["address_scope"] == "loopback"


def test_real_auth_failure_and_strict_routed_forwarding(loopback):
    result = SSHRunner(loopback["database"]).test("bad-auth", timeout=4)
    assert result["ok"] is False and result["error_code"] == "authentication-failed"
    assert loopback["servers"][1].forward_attempts > 0
    assert loopback["servers"][2].forward_attempts > 0
    interface = modern_fixture.FixtureSSHServer(loopback["servers"][1])
    assert interface.check_channel_direct_tcpip_request(0, ("127.0.0.1", 1), ("example.invalid", 22)) != 0
    assert interface.check_channel_direct_tcpip_request(0, ("127.0.0.1", 1), ("127.0.0.1", 1)) != 0


def test_request_credential_probe_uses_saved_trust_stores_without_touching_hosts(loopback):
    database = loopback["database"]
    runner = CredentialProbeRunner(database, lambda: loopback["vault"])
    for alias in ("bad-auth", "multi-hop", "external-route"):
        original = database.get_server(alias)
        before_commands = sum(len(server.exec_commands) for server in loopback["servers"])
        result = runner.test("fixture-key", alias, timeout=4)
        assert result["ok"] and result["method"] == "publickey", result
        assert database.get_server(alias) == original
        assert sum(len(server.exec_commands) for server in loopback["servers"]) == before_commands


def test_request_probe_preserves_an_unbound_managed_jumps_configured_identity(probe_evidence):
    with modern_fixture.fixture_environment() as fixture:
        database = fixture["database"]
        original = fixture["root"] / "original.conf"
        original.write_text(f"Host unbound-jump\n IdentityFile {modern_fixture.quote_value(str(fixture['root'] / 'client-key'))}\n"
                            " IdentitiesOnly yes\n" + original.read_text())
        jump = database.create_server(alias="unbound-jump", hostname="127.0.0.1", username="fixture",
                                      port=fixture["servers"][1].port)
        target = database.create_server(alias="via-unbound", hostname="127.0.0.1", username="fixture",
                                        port=fixture["servers"][0].port, credential_id="fixture-key",
                                        proxy_jumps=["unbound-jump"])
        result = CredentialProbeRunner(database, lambda: fixture["vault"]).test("fixture-key", target["id"], timeout=4)
        assert result["ok"] and result["method"] == "publickey", {
            "result_code": result["error_code"], "latency_ms": result["latency_ms"],
            "method": result["method"], "sanitized_evidence": probe_evidence,
            "server_auth_attempts": [server.auth_attempts for server in fixture["servers"]],
        }
        assert database.get_server(jump["id"])["credential_id"] is None


def test_sftp_protocol_returns_parseable_fixture_listing(loopback):
    """Keep native OpenSSH output available when a platform's format changes."""
    runner = SSHRunner(loopback["database"])
    host = loopback["database"].get_server("direct")
    before_auth = loopback["servers"][0].auth_attempts
    command = runner._sftp_command(host, timeout=4, reuse=0)
    result = subprocess.run(
        command,
        input=runner._directory_batch("/"), text=True, encoding="utf-8", errors="replace",
        capture_output=True, env=runner._environment(host), timeout=9, check=False,
    )
    parse_error = None
    path, entries, unparsed = None, [], None
    if result.returncode == 0:
        try:
            path, entries, unparsed = runner._parse_directory_listing(result.stdout)
        except FileOperationError as exc:
            parse_error = str(exc)
    if result.returncode or path != "/" or unparsed or not any(item["name"] == "readme.txt" for item in entries):
        version = subprocess.run([runner.ssh_binary, "-V"], capture_output=True, text=True,
                                 timeout=5, check=False)
        # sftp may report only "Connection closed" for a bad Include ACL.
        # Discard the effective configuration; only fixed fixture-file errors
        # are useful here, and neither check makes a remote connection.
        configuration = subprocess.run(
            [runner.ssh_binary, "-G", "-F", command[command.index("-F") + 1], host["alias"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            timeout=5, check=False,
        )
        # This fixture contains only generated names/metadata. Never print the
        # command environment, config, launch files or credential material.
        pytest.fail("Fixture SFTP protocol output was not parsed:\n" + json.dumps({
            "ssh_binary": runner.ssh_binary, "sftp_binary": runner.sftp_binary,
            "client_version": (version.stdout + version.stderr).strip(), "returncode": result.returncode,
            "configuration_returncode": configuration.returncode,
            "configuration_stderr": configuration.stderr,
            "auth_attempts": loopback["servers"][0].auth_attempts - before_auth,
            "parse_error": parse_error,
            "stdout": result.stdout, "stderr": result.stderr, "unparsed": unparsed,
        }, ensure_ascii=True, indent=2), pytrace=False)


def test_sftp_metadata_large_directory_filter_and_fallback(loopback):
    runner = SSHRunner(loopback["database"])
    listing = runner.list_directory("multi-hop", "/", timeout=4)
    entry = next((item for item in listing["entries"] if item["name"] == "readme.txt"), None)
    if entry is None:
        pytest.fail("Fixture readme missing from directory listing:\n" +
                    json.dumps(listing, ensure_ascii=True, indent=2), pytrace=False)
    assert entry["owner"] == "fixture" and entry["modified_at"] and entry["uid"] is not None
    large = runner.list_directory("direct", "/large", timeout=4, reuse=0)
    assert large["total"] == 5005 and large["matching_total"] == 5005 and large["truncated"]
    filtered = runner.list_directory("direct", "/large", timeout=4, reuse=0, name_filter="entry-05004")
    assert filtered["total"] == 5005 and filtered["matching_total"] == 1
    assert filtered["entries"][0]["name"] == "entry-05004.txt"
    fallback = runner.list_directory("metadata-fallback", "/", timeout=4, reuse=0)
    assert fallback["entries"] and all(item["modified_at"] is None for item in fallback["entries"])


def test_sftp_conflict_explicit_overwrite_atomic_publication_download_and_cap(loopback, tmp_path):
    runner = SSHRunner(loopback["database"])
    local = tmp_path / "upload.txt"
    local.write_text("explicit replacement\n")
    with pytest.raises(FileOperationError) as conflict:
        runner.upload("direct", local, "/existing.txt", timeout=4, reuse=0, overwrite=False)
    assert conflict.value.error_code == "exists", str(conflict.value)
    assert (loopback["remote"] / "existing.txt").read_text() == "original fixture content\n"
    runner.upload("direct", local, "/existing.txt", timeout=4, reuse=0, overwrite=True)
    runner.upload("external-route", local, "/new.txt", timeout=4, reuse=0, overwrite=False)
    assert not list(loopback["remote"].glob(".ssm-upload-*"))
    downloaded = tmp_path / "download.txt"
    runner.download("multi-hop", "/new.txt", downloaded, timeout=4, reuse=0, max_bytes=1000)
    assert downloaded.read_text() == "explicit replacement\n"
    with pytest.raises(FileOperationError) as oversized:
        runner.download("direct", "/oversize.bin", tmp_path / "oversize.bin", timeout=4, reuse=0,
                        max_bytes=loopback["max_web_transfer_bytes"])
    assert oversized.value.error_code == "too_large"
    assert not (tmp_path / "oversize.bin").exists()


def test_sftp_binary_upload_and_download_preserve_bytes(loopback, tmp_path):
    runner = SSHRunner(loopback["database"])
    payload = b"binary\x00\x1a\r\n\n\r\xff\x80" + bytes(range(256))
    local = tmp_path / "binary-upload.bin"
    local.write_bytes(payload)
    runner.upload("direct", local, "/binary-roundtrip.bin", timeout=4, reuse=0, overwrite=False)
    assert (loopback["remote"] / "binary-roundtrip.bin").read_bytes() == payload
    downloaded = tmp_path / "binary-download.bin"
    runner.download("direct", "/binary-roundtrip.bin", downloaded, timeout=4, reuse=0,
                    max_bytes=len(payload))
    assert downloaded.read_bytes() == payload


def test_fixture_filesystem_cannot_escape(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    filesystem = modern_fixture.FixtureSFTP(None, root=root)
    outside = tmp_path / "outside"
    outside.write_text("outside")
    try:
        (root / "escape").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires a Windows privilege")
    with pytest.raises(PermissionError):
        filesystem.local("/escape")
    assert filesystem.canonicalize("/.hidden") == "/.hidden"


def test_fixture_filesystem_rebases_parent_paths_and_rejects_windows_drives(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    filesystem = modern_fixture.FixtureSFTP(None, root=root)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    assert filesystem.local("/../../outside") == root / "outside"
    assert filesystem.canonicalize("/") == "/"
    if os.name == "nt":
        for path in (outside.as_posix(), str(outside)):
            for follow in (False, True):
                with pytest.raises(PermissionError):
                    filesystem.local(path, follow=follow)


def test_fixture_shutdown_removes_state():
    with modern_fixture.fixture_environment() as fixture:
        temporary = fixture["root"]
        ports = [server.port for server in fixture["servers"]]
    assert not temporary.exists()
    for port in ports:
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
