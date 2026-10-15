from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone

import pytest

from ssh_server_manager.db import Database
from ssh_server_manager.remote_files import (
    METADATA_PREFIX, METADATA_SCRIPT, file_error_code, normalize_metadata,
    run_metadata_command, validate_web_file_path,
)
from ssh_server_manager.ssh_runner import FileOperationError, SSHRunner
from ssh_server_manager.validation import ValidationError


@pytest.fixture
def runner(tmp_path, monkeypatch):
    database = Database(tmp_path / "hosts.db")
    database.create_server(alias="box", hostname="box.example", username="alice", port=22)
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing.conf"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    return SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp")


def listing(*names):
    return "Remote working directory: /srv\n" + "".join(
        f"-rw-r--r-- 1 1000 1001 12 Oct 2 09:30 {name}\n" for name in names
    )


def metadata(kind="file", size=12, **changes):
    return {
        "type": kind, "size": size, "uid": 1000, "gid": 1001,
        "owner": "deploy", "group": "staff", "link_target": None,
        "modified_at": "2026-10-02T13:30:00+00:00", **changes,
    }


def test_listing_enriches_visible_metadata_without_changing_connection_policy(runner, monkeypatch):
    requests = []

    def run(command, **options):
        assert "StrictHostKeyChecking=yes" in command
        return subprocess.CompletedProcess(command, 0, listing("report.txt"), "")

    def metadata_run(command, **options):
        assert "StrictHostKeyChecking=yes" in command
        assert "-T" in command
        if os.name != "nt":
            assert "ControlMaster=auto" in command
            assert "ControlPersist=300" in command
        request = json.loads(options["request"])
        requests.append(request)
        result = {"ok": True, "entries": {"report.txt": metadata(owner="de\x1bploy", group="staff\x00")}}
        return subprocess.CompletedProcess(command, 0, METADATA_PREFIX + json.dumps(result), ""), False

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", metadata_run)
    result = runner.list_directory("box", "/srv")
    entry = result["entries"][0]
    assert entry["uid"] == 1000 and entry["gid"] == 1001
    assert entry["owner"] == "deploy" and entry["group"] == "staff"
    assert entry["modified_at"] == "2026-10-02T13:30:00+00:00"
    assert requests == [{"mode": "entries", "path": "/srv", "names": ["report.txt"]}]


def test_sftp_only_listing_retains_numeric_owner_and_does_not_guess_time(runner, monkeypatch):
    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", lambda command, **_: (subprocess.CompletedProcess(command, 127, "", "python3: not found"), False))
    def run(command, **_options):
        if command[0] == "ssh":
            return subprocess.CompletedProcess(command, 127, "", "python3: not found")
        return subprocess.CompletedProcess(command, 0, listing("old.txt"), "")

    monkeypatch.setattr(subprocess, "run", run)
    entry = runner.list_directory("box", "/srv")["entries"][0]
    assert entry["owner"] == "1000" and entry["uid"] == 1000
    assert entry["modified"] == "Oct 2 09:30"
    assert entry["modified_at"] is None and entry["link_target"] is None


def test_name_filter_precedes_cap_and_only_visible_names_are_enriched(runner, monkeypatch):
    monkeypatch.setattr("ssh_server_manager.ssh_runner.MAX_DIRECTORY_ENTRIES", 2)
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, listing("z-target", "a-target", "b-other", "c-target"), ""))

    def enrich(_server, request, **_options):
        seen.extend(request["names"])
        return None

    monkeypatch.setattr(runner, "_file_metadata", enrich)
    result = runner.list_directory("box", "/srv", name_filter="TARGET")
    assert result["total"] == 4 and result["matching_total"] == 3
    assert result["truncated"] is True
    assert seen == ["a-target", "c-target"]


def test_metadata_helper_reads_precise_utc_stat_and_symlink_target(tmp_path):
    target = tmp_path / "release.txt"
    target.write_bytes(b"content must not be returned")
    os.utime(target, (1790947800, 1790947800))
    link = tmp_path / "current"
    try:
        link.symlink_to("release.txt")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    result = subprocess.run(
        [sys.executable, "-c", METADATA_SCRIPT],
        input=json.dumps({"mode": "entries", "path": str(tmp_path), "names": ["release.txt", "current"]}),
        capture_output=True, text=True, check=True,
    )
    document = json.loads(result.stdout.removeprefix(METADATA_PREFIX))
    item = document["entries"]["release.txt"]
    assert item["size"] == target.stat().st_size
    assert item["modified_at"] == datetime.fromtimestamp(1790947800, timezone.utc).isoformat()
    assert document["entries"]["current"]["link_target"] == "release.txt"
    assert "content must not" not in result.stdout


def test_stat_falls_back_to_sftp_without_python_and_rejects_unknown_symlink_size(runner, monkeypatch):
    monkeypatch.setattr(runner, "_file_metadata", lambda *_args, **_options: None)
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, listing("report.txt"), ""))
    assert runner.stat_file("box", "/srv/report.txt")["size"] == 12
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, "Remote working directory: /srv\nlrwxrwxrwx 1 1000 1001 3 Oct 2 09:30 current\n", ""))
    with pytest.raises(FileOperationError) as error:
        runner.stat_file("box", "/srv/current")
    assert error.value.error_code == "permission_denied"
    assert runner.stat_file("box", "/srv/current", follow_symlinks=False)["type"] == "symlink"


def test_download_rejects_oversize_before_any_transfer(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "stat_file", lambda *_args, **_options: metadata(size=101))
    monkeypatch.setattr(runner, "_run_transfer", lambda *_args, **_options: pytest.fail("oversized file must not be transferred"))
    with pytest.raises(FileOperationError) as error:
        runner.download("box", "/srv/report.txt", tmp_path / "report.txt", max_bytes=100)
    assert error.value.error_code == "too_large"


def test_download_detects_growth_after_preflight(runner, tmp_path, monkeypatch):
    destination = tmp_path / "report.txt"
    monkeypatch.setattr(runner, "stat_file", lambda *_args, **_options: metadata(size=1))

    def transfer(*_args, **_options):
        destination.write_bytes(b"a" * 101)
        return 0, "", ""

    monkeypatch.setattr(runner, "_run_transfer", transfer)
    with pytest.raises(FileOperationError) as error:
        runner.download("box", "/srv/report.txt", destination, max_bytes=100)
    assert error.value.error_code == "too_large"


def test_upload_conflict_contains_existing_metadata_and_never_puts(runner, tmp_path, monkeypatch):
    source = tmp_path / "upload.txt"
    source.write_bytes(b"new")
    monkeypatch.setattr(runner, "stat_file", lambda *_args, **options: metadata(size=42, type="symlink"))
    monkeypatch.setattr(runner, "_run_transfer", lambda *_args, **_options: pytest.fail("existing destination must not be overwritten"))
    with pytest.raises(FileOperationError) as error:
        runner.upload("box", source, "/srv/upload.txt", overwrite=False)
    assert error.value.error_code == "exists"
    assert error.value.existing["size"] == 42


def test_upload_only_not_found_allows_put(runner, tmp_path, monkeypatch):
    source = tmp_path / "upload.txt"
    source.write_bytes(b"new")
    transferred = []

    def missing(*_args, **_options):
        raise FileOperationError("missing", "not_found")

    monkeypatch.setattr(runner, "stat_file", missing)
    monkeypatch.setattr(runner, "_run_transfer", lambda *_args, **options: (transferred.append(options["batch"]) or (0, "", "")))
    assert runner.upload("box", source, "/srv/upload.txt", overwrite=False)["bytes"] == 3
    assert len(transferred) == 1

    def denied(*_args, **_options):
        raise FileOperationError("denied", "permission_denied")

    monkeypatch.setattr(runner, "stat_file", denied)
    with pytest.raises(FileOperationError):
        runner.upload("box", source, "/srv/upload.txt", overwrite=False)
    assert len(transferred) == 1


@pytest.mark.parametrize(("stderr", "status", "code"), [
    ("remote opendir: Permission denied", 0, "permission_denied"),
    ("Permission denied (publickey)", 255, "auth_failed"),
    ("No such file or directory", 0, "not_found"),
    ("Not a directory", 0, "not_a_directory"),
    ("File exists", 0, "exists"),
    ("Server does not support hardlink@openssh.com extension", 0, "unsupported"),
    ("Connection timed out", 255, "timeout"),
    ("Connection refused", 255, "unreachable"),
])
def test_file_errors_have_stable_codes(stderr, status, code):
    assert file_error_code(stderr, status) == code


@pytest.mark.parametrize("path", ["/srv/.", "/srv/..", "/srv/../payload", "~/./payload", "/srv/", "~", ""])
def test_web_file_destinations_reject_directory_and_traversal_targets(path):
    with pytest.raises(ValidationError):
        validate_web_file_path(path)


def test_optional_remote_metadata_is_validated_and_errors_are_sanitized():
    assert normalize_metadata(metadata(modified_at="Oct 2 09:30"))["modified_at"] is None
    assert normalize_metadata(metadata(size=True)) is None
    error = FileOperationError("password=hidden\x1b\nnot found", "not_found")
    assert "hidden" not in str(error) and "\x1b" not in str(error) and "\n" not in str(error)


def test_stat_authority_is_sftp_even_when_python_has_another_namespace(runner, monkeypatch):
    monkeypatch.setattr(runner, "_file_metadata", lambda *_args, **_options: pytest.fail("Python metadata must not authorize a file transfer"))
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, listing("report.txt").replace(" 12 Oct", " 200 Oct"), ""))
    monkeypatch.setattr(runner, "_run_transfer", lambda *_args, **_options: pytest.fail("actual SFTP size exceeds the limit"))
    with pytest.raises(FileOperationError) as error:
        runner.download("box", "/srv/report.txt", "/unused/output", max_bytes=100)
    assert error.value.error_code == "too_large"


def test_upload_target_appearing_after_preflight_is_never_overwritten(runner, tmp_path, monkeypatch):
    source = tmp_path / "upload.txt"
    source.write_bytes(b"new")
    checks = []
    batches = []

    def stat(*_args, **_options):
        checks.append(True)
        if len(checks) == 1:
            raise FileOperationError("missing", "not_found")
        return metadata(size=777)

    def transfer(*_args, **options):
        batches.append(options)
        if options.get("batch_mode"):
            assert "@ln " in options["batch"]
            assert '"/srv/upload.txt"' not in options["batch"].splitlines()[0]
            return 1, "", "remote link: Failure"
        assert options["batch"].startswith('@rm "/srv/.ssm-upload-')
        assert '"/srv/upload.txt"' not in options["batch"]
        return 0, "", ""

    monkeypatch.setattr(runner, "stat_file", stat)
    monkeypatch.setattr(runner, "_run_transfer", transfer)
    with pytest.raises(FileOperationError) as error:
        runner.upload("box", source, "/srv/upload.txt", overwrite=False)
    assert error.value.error_code == "exists" and error.value.existing["size"] == 777
    assert len(batches) == 2  # publication failure, then temporary-only cleanup


def test_upload_without_hardlink_support_fails_safely_and_cleans_temp(runner, tmp_path, monkeypatch):
    source = tmp_path / "upload.txt"
    source.write_bytes(b"new")
    batches = []

    def missing(*_args, **_options):
        raise FileOperationError("missing", "not_found")

    def transfer(*_args, **options):
        batches.append(options["batch"])
        if options.get("batch_mode"):
            return 1, "", "Server does not support hardlink@openssh.com extension"
        return 0, "", ""

    monkeypatch.setattr(runner, "stat_file", missing)
    monkeypatch.setattr(runner, "_run_transfer", transfer)
    with pytest.raises(FileOperationError) as error:
        runner.upload("box", source, "~/upload.txt", overwrite=False)
    assert error.value.error_code == "unsupported"
    assert batches[0].startswith("@cd\n@put ")
    assert batches[1].startswith('@cd\n@rm ".ssm-upload-')
    assert '"upload.txt"' not in batches[1]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_metadata_capture_kills_over_budget_process_without_unbounded_buffers(monkeypatch, stream):
    monkeypatch.setattr("ssh_server_manager.remote_files.MAX_METADATA_BYTES", 128)
    result, exceeded = run_metadata_command(
        [sys.executable, "-c", f"import sys; sys.{stream}.buffer.write(b'x' * (2 * 1024 * 1024)); sys.{stream}.flush()"],
        request="{}", env=os.environ.copy(), timeout=3,
    )
    assert exceeded is True
    assert len(result.stdout) <= 128 and len(result.stderr) <= 65536
    assert result.returncode != 0


def test_metadata_deadline_applies_when_child_does_not_consume_stdin():
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_metadata_command(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            request="x" * (1024 * 1024), env=os.environ.copy(), timeout=0.1,
        )
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("status", [0, 255])
def test_file_diagnostics_strip_controls_formatting_and_secrets(runner, monkeypatch, status):
    diagnostics = "banner\nremote opendir: Permission denied password=hidden\x1b\x00\u202e\u200b" + "x" * 6000
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, status, listing("report.txt"), diagnostics))
    with pytest.raises(FileOperationError) as error:
        runner.list_directory("box", "/srv", enrich=False)
    message = str(error.value)
    assert "hidden" not in message and len(message) <= 4000
    assert not any(unicodedata.category(character).startswith("C") for character in message)
    recorded = runner.database.get_server("box")["last_test_message"]
    assert "hidden" not in recorded and len(recorded) <= 500
    assert not any(unicodedata.category(character).startswith("C") for character in recorded)
    with pytest.raises(FileOperationError) as transfer_error:
        runner._finish_transfer(status, diagnostics)
    assert str(transfer_error.value) == message


def test_display_metadata_is_bounded_and_sanitized_without_mutating_file_paths(runner, monkeypatch):
    raw_name = "report\u200b.txt"
    remote_output = listing(raw_name).replace("1000 1001", "de\u202eploy st\u200baff")
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, remote_output, ""))
    entry = runner.list_directory("box", "/srv", enrich=False)["entries"][0]
    assert entry["name"] == raw_name and entry["path"] == "/srv/" + raw_name
    assert entry["owner"] == "deploy" and entry["group"] == "staff"
    normalized = normalize_metadata(metadata(owner="o\u202e" * 1000, group="g\u200b" * 1000, link_target="a\x00\u202e" * 2000))
    assert len(normalized["owner"]) == 512 and len(normalized["group"]) == 512
    assert len(normalized["link_target"]) == 1024
    assert not any(unicodedata.category(character).startswith("C") for key in ("owner", "group", "link_target") for character in normalized[key])


@pytest.mark.parametrize("status", [0, 255])
def test_listing_does_not_record_checks_after_host_address_changes(runner, monkeypatch, status):
    def run(command, **_options):
        runner.database.update_server("box", hostname="new.example", port=2222)
        return subprocess.CompletedProcess(command, status, listing("report.txt"), "Connection refused" if status else "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(FileOperationError) as error:
        runner.list_directory("box", "/srv", enrich=False)
    assert error.value.error_code == "unreachable"
    current = runner.database.get_server("box")
    assert current["hostname"] == "new.example" and current["port"] == 2222
    assert current["last_test_at"] is None and current["last_test_status"] is None
