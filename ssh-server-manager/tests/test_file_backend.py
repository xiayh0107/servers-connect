from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone

import pytest

from ssh_server_manager.db import Database
from ssh_server_manager.remote_files import (
    LIST_PREFIX, LIST_SCRIPT, METADATA_PREFIX, METADATA_SCRIPT, file_error_code,
    normalize_metadata, run_metadata_command, validate_web_file_path,
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
    instance = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp")
    # These tests pin the SFTP compatibility path; the fast one-exec listing has
    # its own tests below.
    instance.fast_listing = False
    return instance


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


def test_web_listing_skips_the_probe_and_reports_the_sftp_phase(runner, monkeypatch):
    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", lambda *_args, **_options: pytest.fail("the split listing must not probe"))
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, listing("report.txt"), ""))
    result = runner.list_directory("box", "/srv", enrich=False)
    assert result["entries"][0]["owner"] == "1000"
    # The listing alone is timed; total_ms exists so a caller can still report
    # what the one-request CLI path cost end to end.
    assert result["enrichment_ms"] is None and result["total_ms"] == result["latency_ms"]


def test_separate_enrichment_keeps_the_type_size_and_owner_agreement_check(runner, monkeypatch):
    def metadata_run(command, **options):
        result = {"ok": True, "entries": {
            "report.txt": metadata(owner="deploy"),
            # A replaced file: same name, different size.
            "grown.txt": metadata(size=99),
            # A shell in another namespace: same size, different numeric owner.
            "chroot.txt": metadata(uid=4242, gid=4242),
        }}
        return subprocess.CompletedProcess(command, 0, METADATA_PREFIX + json.dumps(result), ""), False

    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", metadata_run)
    claims = [
        {"name": "report.txt", "type": "file", "size": 12, "uid": 1000, "gid": 1001},
        {"name": "grown.txt", "type": "file", "size": 12, "uid": 1000, "gid": 1001},
        {"name": "chroot.txt", "type": "file", "size": 12, "uid": 1000, "gid": 1001},
        {"name": ".", "type": "file", "size": 12, "uid": None, "gid": None},
    ]
    result = runner.enrich_listing("box", "/srv", claims)
    assert set(result["entries"]) == {"report.txt"}
    assert result["entries"]["report.txt"]["owner"] == "deploy"
    assert result["entries"]["report.txt"]["modified_at"] == "2026-10-02T13:30:00+00:00"
    # Only the fields a probe may contribute are returned, never size or type.
    assert set(result["entries"]["report.txt"]) == {
        "uid", "gid", "modified_at", "link_target", "owner", "group",
    }


def test_separate_enrichment_is_optional_when_python_or_the_probe_is_missing(runner, monkeypatch):
    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command",
                        lambda command, **_: (subprocess.CompletedProcess(command, 127, "", "python3: not found"), False))
    claims = [{"name": "report.txt", "type": "file", "size": 12, "uid": 1000, "gid": 1001}]
    assert runner.enrich_listing("box", "/srv", claims)["entries"] == {}
    # An empty claim list never opens a connection at all.
    assert runner.enrich_listing("box", "/srv", [])["entries"] == {}


def test_separate_enrichment_rejects_a_path_with_control_characters(runner):
    with pytest.raises(ValidationError):
        runner.enrich_listing("box", "/srv\n@rm escaped", [{"name": "report.txt", "type": "file", "size": 12}])


def fast_listing_document(entries, path="/srv", truncated=False, unparsed=0):
    return LIST_PREFIX + json.dumps({"ok": True, "path": path, "entries": entries,
                                     "truncated": truncated, "unparsed": unparsed})


def fast_entry(name, **changes):
    return {"name": name, "type": "file", "size": 12, "uid": 1000, "gid": 1001,
            "owner": "deploy", "group": "staff", "permissions": "-rw-r--r--",
            "modified_at": "2026-10-02T13:30:00+00:00", "link_target": None, **changes}


@pytest.fixture
def fast_runner(tmp_path, monkeypatch):
    """A runner whose namespace check passes and whose listing comes from Python."""
    database = Database(tmp_path / "hosts.db")
    database.create_server(alias="box", hostname="box.example", username="alice", port=22)
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing.conf"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    instance = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp")
    # The namespace check is answered directly; its own test covers the probe.
    monkeypatch.setattr(instance, "_shell_matches_sftp", lambda *_a, **_k: True)
    return instance


def test_fast_listing_returns_display_fields_and_precise_metadata_in_one_call(fast_runner, monkeypatch):
    requests = []

    def run(command, **options):
        requests.append(json.loads(options["request"]))
        document = fast_listing_document([
            fast_entry("report.txt"),
            fast_entry("current", type="symlink", link_target="report.txt"),
            fast_entry(".hidden", type="directory", permissions="drwxr-xr-x"),
        ])
        return subprocess.CompletedProcess(command, 0, document, ""), False

    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", run)
    # The SFTP subsystem must not be started at all on this path.
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("the fast listing must not run sftp"))
    result = fast_runner.list_directory("box", "/srv")
    assert requests == [{"path": "/srv"}]
    assert result["path"] == "/srv" and result["parent"] == "/"
    assert result["enrichment_ms"] is None
    by_name = {entry["name"]: entry for entry in result["entries"]}
    assert by_name["report.txt"]["owner"] == "deploy"
    assert by_name["report.txt"]["modified_at"] == "2026-10-02T13:30:00+00:00"
    assert by_name["report.txt"]["permissions"] == "-rw-r--r--"
    assert by_name["current"]["type"] == "symlink" and by_name["current"]["link_target"] == "report.txt"
    assert by_name[".hidden"]["hidden"] is True
    assert by_name["report.txt"]["path"] == "/srv/report.txt"
    # Directory-first ordering still applies.
    assert result["entries"][0]["name"] == ".hidden"


def test_fast_listing_falls_back_to_sftp_when_python_is_missing(fast_runner, monkeypatch):
    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command",
                        lambda command, **_: (subprocess.CompletedProcess(command, 127, "", "python3: not found"), False))
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, listing("report.txt"), ""))
    result = fast_runner.list_directory("box", "/srv")
    # The SFTP parser produced this entry, not the Python listing.
    assert result["entries"][0]["name"] == "report.txt"
    assert result["path"] == "/srv"


def test_fast_listing_falls_back_to_sftp_when_the_shell_namespace_differs(tmp_path, monkeypatch):
    database = Database(tmp_path / "hosts.db")
    database.create_server(alias="box", hostname="box.example", username="alice", port=22)
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing.conf"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    instance = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp")
    monkeypatch.setattr(instance, "_shell_matches_sftp", lambda *_a, **_k: False)
    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command",
                        lambda command, **_: (subprocess.CompletedProcess(command, 0, fast_listing_document([fast_entry("shell-only.txt")]), ""), False))
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, listing("sftp-only.txt"), ""))
    result = instance.list_directory("box", "/srv")
    # A shell that disagrees with SFTP must never supply the listing.
    assert [entry["name"] for entry in result["entries"]] == ["sftp-only.txt"]


def test_fast_listing_rejects_untrusted_entries(fast_runner, monkeypatch):
    def run(command, **_options):
        document = fast_listing_document([
            fast_entry("ok.txt"),
            fast_entry("../escape"),
            fast_entry("badtype", type="socket"),
            fast_entry("badsize", size=-1),
            fast_entry("badperm", permissions="rw-r--r--"),
            fast_entry("control\x1b[31m"),
            "not-a-dict",
            fast_entry("baduid", uid=-5),
        ], unparsed=2)
        return subprocess.CompletedProcess(command, 0, document, ""), False

    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", run)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("must not fall back"))
    result = fast_runner.list_directory("box", "/srv")
    names = [entry["name"] for entry in result["entries"]]
    # A name that escapes the directory, a bad type, a negative size and a
    # malformed mode are dropped; the surviving entry keeps its fields.
    assert "ok.txt" in names
    assert not {"../escape", "badtype", "badsize", "badperm", "not-a-dict"} & set(names)
    assert result["unparsed"] >= 2


def test_fast_listing_reports_truncation_and_filtering(fast_runner, monkeypatch):
    def run(command, **_options):
        document = fast_listing_document([fast_entry("alpha.txt"), fast_entry("beta.txt")], truncated=True)
        return subprocess.CompletedProcess(command, 0, document, ""), False

    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", run)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("must not fall back"))
    result = fast_runner.list_directory("box", "/srv", name_filter="ALPHA")
    assert result["truncated"] is True
    assert [entry["name"] for entry in result["entries"]] == ["alpha.txt"]
    assert result["total"] == 2 and result["matching_total"] == 1


def test_fast_listing_failure_documents_keep_sftp_in_charge(fast_runner, monkeypatch):
    for payload in (
        "__ssm_list__=not json",
        "__ssm_list__=" + json.dumps({"ok": False, "error_code": "not_found"}),
        "__ssm_list__=" + json.dumps({"ok": True, "path": "", "entries": []}),
        "__ssm_list__=" + json.dumps({"ok": True, "path": "/srv", "entries": "nope"}),
        "",
    ):
        monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command",
                            lambda command, _p=payload, **_: (subprocess.CompletedProcess(command, 0, _p, ""), False))
        monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, listing("fallback.txt"), ""))
        result = fast_runner.list_directory("box", "/srv")
        assert [entry["name"] for entry in result["entries"]] == ["fallback.txt"]


def test_namespace_check_compares_home_between_sftp_and_shell(tmp_path, monkeypatch):
    database = Database(tmp_path / "hosts.db")
    database.create_server(alias="box", hostname="box.example", username="alice", port=22)
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing.conf"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    instance = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp")
    server = database.get_server("box")
    monkeypatch.setattr(instance, "_sftp_resolved_path", lambda *_a, **_k: "/jail/home/alice")
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, "/home/alice\n", ""))
    # SFTP lands in a jail the shell does not share: refuse the fast path.
    assert instance._shell_matches_sftp(server, timeout=5, reuse=0) is False
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, "/jail/home/alice\n", ""))
    instance.listing.forget()
    assert instance._shell_matches_sftp(server, timeout=5, reuse=0) is True
    # The verdict is cached per host; the probe is not repeated per directory.
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("the verdict must be cached"))
    assert instance._shell_matches_sftp(server, timeout=5, reuse=0) is True


def test_namespace_check_fails_closed_without_an_answer(tmp_path, monkeypatch):
    database = Database(tmp_path / "hosts.db")
    database.create_server(alias="box", hostname="box.example", username="alice", port=22)
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing.conf"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    instance = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp")
    server = database.get_server("box")
    monkeypatch.setattr(instance, "_sftp_resolved_path", lambda *_a, **_k: None)
    assert instance._shell_matches_sftp(server, timeout=5, reuse=0) is False
    instance.listing.forget()
    monkeypatch.setattr(instance, "_sftp_resolved_path", lambda *_a, **_k: "/root")
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 1, "", "boom"))
    assert instance._shell_matches_sftp(server, timeout=5, reuse=0) is False


def test_fast_listing_is_not_retried_after_a_transport_failure(fast_runner, monkeypatch):
    listing_calls = []

    def run(command, **options):
        # Only the listing request is counted; the SFTP fallback's own metadata
        # probe shares this helper and is expected.
        if json.loads(options["request"]) == {"path": "/srv"}:
            listing_calls.append(command)
        return subprocess.CompletedProcess(command, 255, "", "Connection closed"), False

    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", run)
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, listing("via-sftp.txt"), ""))
    first = fast_runner.list_directory("box", "/srv", enrich=False)
    assert [entry["name"] for entry in first["entries"]] == ["via-sftp.txt"]
    assert len(listing_calls) == 1
    # The host has now shown it cannot answer this route; a second folder must
    # go straight to SFTP instead of paying the same failure again.
    fast_runner.list_directory("box", "/srv", enrich=False)
    assert len(listing_calls) == 1


def test_fast_listing_is_skipped_for_a_host_with_a_recorded_transport_failure(fast_runner, monkeypatch):
    fast_runner.database.record_test(
        "box", status="failed", latency_ms=1, error_code="connection-refused",
        message="Connection refused", expected_endpoint=("box.example", 22),
    )
    def run(command, **options):
        # The SFTP fallback may still run its metadata probe; the one-exec
        # listing request (which carries no "mode") must never be attempted.
        assert json.loads(options["request"]).get("mode") == "entries", "a known-unreachable host must not try the fast route"
        return subprocess.CompletedProcess(command, 0, "", ""), False

    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", run)
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, listing("via-sftp.txt"), ""))
    result = fast_runner.list_directory("box", "/srv")
    assert [entry["name"] for entry in result["entries"]] == ["via-sftp.txt"]


# -- both routes over one real directory -------------------------------------
#
# The fast listing replaced SFTP for browsing, so the two routes are only
# interchangeable if they describe the same directory the same way. Asserting
# that field by field on a fixed fixture host drifts; running both parsers over
# one real directory cannot drift.

def sftp_listing_text(directory):
    """Render what `sftp @ls -lan` prints for this directory."""
    lines = [f"Remote working directory: {directory}"]
    for entry in sorted(os.scandir(directory), key=lambda item: item.name):
        info = entry.stat(follow_symlinks=False)
        lines.append(
            f"{stat.filemode(info.st_mode)} 1 {info.st_uid} {info.st_gid} "
            f"{info.st_size} Oct  2 09:30 {entry.name}"
        )
    return "\n".join(lines) + "\n"


def populate(directory):
    """A directory with the shapes a real share has."""
    (directory / "report.txt").write_bytes(b"0123456789")
    (directory / "sub").mkdir()
    (directory / "with space.txt").write_bytes(b"x")
    (directory / ".hidden").write_bytes(b"h")
    (directory / "unicode-\u6587\u4ef6.txt").write_bytes(b"u")
    (directory / "Upper.TXT").write_bytes(b"U")
    try:
        (directory / "current").symlink_to("report.txt")
    except (OSError, NotImplementedError):
        pass  # Windows without the privilege: the rest still compares.


def test_both_listing_routes_describe_one_directory_identically(fast_runner, tmp_path, monkeypatch):
    directory = tmp_path / "share"
    directory.mkdir()
    populate(directory)
    path = os.path.realpath(directory)
    # Captured before the SFTP stand-in replaces the module attribute below.
    local_run = subprocess.run

    def fast_transport(command, *, request, env, timeout):
        # The real script, run locally instead of over ssh.
        result = local_run(
            [sys.executable, "-c", LIST_SCRIPT], input=request,
            capture_output=True, text=True, check=False,
        )
        return result, False

    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", fast_transport)
    # Browsing through the shell must not start the SFTP subsystem at all.
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("the fast listing must not run sftp"))
    fast = fast_runner.list_directory("box", path)
    assert fast["enriched"] is True

    # The same directory as the SFTP route sees it.
    fast_runner.fast_listing = False
    monkeypatch.setattr(
        subprocess, "run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, sftp_listing_text(path), ""),
    )
    plain = fast_runner.list_directory("box", path, enrich=False)
    assert plain["enriched"] is False

    assert fast["path"] == plain["path"] == path
    assert fast["truncated"] is plain["truncated"] is False
    # Same entries, in the same order, with the same display facts.
    assert [entry["name"] for entry in fast["entries"]] == [entry["name"] for entry in plain["entries"]]
    assert fast["total"] == plain["total"] == len(plain["entries"])
    for fast_entry_, plain_entry in zip(fast["entries"], plain["entries"]):
        for key in ("name", "type", "size", "permissions", "hidden"):
            assert fast_entry_[key] == plain_entry[key], (fast_entry_["name"], key)
        assert fast_entry_["path"] == plain_entry["path"]
    # The difference is only the precise metadata the fast route already has.
    for entry in fast["entries"]:
        assert entry["uid"] is not None and entry["gid"] is not None
        assert entry["modified_at"] is not None
    symlinks = [entry for entry in fast["entries"] if entry["type"] == "symlink"]
    if symlinks:
        assert symlinks[0]["link_target"] == "report.txt"


def test_a_jailed_shell_stops_paying_for_the_discarded_fast_exec(tmp_path, monkeypatch):
    database = Database(tmp_path / "hosts.db")
    database.create_server(alias="box", hostname="box.example", username="alice", port=22)
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing.conf"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    instance = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp")
    fast_calls = []

    def fast_transport(command, **_options):
        fast_calls.append(command)
        return subprocess.CompletedProcess(command, 0, fast_listing_document([fast_entry("shell-only.txt")]), ""), False

    def run(command, **_options):
        # SFTP lands in a jail the shell's Python does not share.
        if any("python3 -c" in argument for argument in command):
            return subprocess.CompletedProcess(command, 0, "/home/alice\n", "")
        return subprocess.CompletedProcess(command, 0, listing("sftp-only.txt"), "")

    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", fast_transport)
    monkeypatch.setattr(subprocess, "run", run)
    first = instance.list_directory("box", "/srv", enrich=False)
    assert [entry["name"] for entry in first["entries"]] == ["sftp-only.txt"]
    assert len(fast_calls) == 1
    # The verdict is a "no" with a lifetime, so the next folder does not even
    # pay for the exec it would have to discard.
    second = instance.list_directory("box", "/srv", enrich=False)
    assert [entry["name"] for entry in second["entries"]] == ["sftp-only.txt"]
    assert len(fast_calls) == 1


def test_concurrent_folders_share_one_namespace_check(tmp_path, monkeypatch):
    database = Database(tmp_path / "hosts.db")
    database.create_server(alias="box", hostname="box.example", username="alice", port=22)
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing.conf"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    instance = SSHRunner(database, ssh_binary="ssh", sftp_binary="sftp")

    def fast_transport(command, **_options):
        return subprocess.CompletedProcess(command, 0, fast_listing_document([fast_entry("report.txt")]), ""), False

    monkeypatch.setattr("ssh_server_manager.ssh_runner.run_metadata_command", fast_transport)
    monkeypatch.setattr(instance, "_sftp_resolved_path", lambda *_a, **_k: "/srv")
    probes = []

    def run(command, **_options):
        assert any("python3 -c" in argument for argument in command)
        probes.append(command)
        time.sleep(0.05)
        return subprocess.CompletedProcess(command, 0, "/srv\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    results = []
    threads = [threading.Thread(target=lambda: results.append(instance.list_directory("box", "/srv"))) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    # Six concurrent folders, one expensive namespace check.
    assert len(probes) == 1
    assert len(results) == 6
    assert all(result["entries"][0]["name"] == "report.txt" for result in results)
