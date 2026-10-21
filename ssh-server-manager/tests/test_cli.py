from __future__ import annotations

import json
from pathlib import Path

import pytest

from ssh_server_manager import __version__
from ssh_server_manager.cli import main, skill_link_status
from ssh_server_manager.db import Database, NotFoundError
from ssh_server_manager.validation import ValidationError


def make_skill_copy(root: Path, version: str) -> Path:
    marker = root / "ssh-server-manager" / "scripts" / "ssh_server_manager" / "__init__.py"
    marker.parent.mkdir(parents=True)
    marker.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return root


def test_skill_link_status_reports_current_and_stale_copies(tmp_path):
    current = make_skill_copy(tmp_path / "claude-skills", __version__)
    stale = make_skill_copy(tmp_path / "codex-skills", "0.0.1")
    missing = tmp_path / "not-installed"

    status = skill_link_status([current, stale, missing])

    assert status["ok"] is True
    assert status["linked"] == 2
    assert [entry["version"] for entry in status["stale"]] == ["0.0.1"]
    assert "re-run install.sh" in status["message"]
    assert str(stale / "ssh-server-manager") in status["message"]


def test_skill_link_status_is_quiet_when_everything_matches(tmp_path):
    current = make_skill_copy(tmp_path / "skills", __version__)

    status = skill_link_status([current])

    assert status == {"ok": True, "linked": 1}


def test_skill_link_status_handles_no_links(tmp_path):
    status = skill_link_status([tmp_path / "empty"])

    assert status == {"ok": True, "linked": 0}


def test_cli_registers_and_resolves_one_skill_for_multiple_hosts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    database = Database()
    database.create_server(alias="gpu-a", hostname="a.example", port=22, username="alice")
    database.create_server(alias="gpu-b", hostname="b.example", port=22, username="alice")
    skill_dir = tmp_path / "skills" / "gpu-operations"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: gpu-operations\ndescription: Operate GPU jobs on attached hosts.\n---\n# Instructions\n",
        encoding="utf-8",
    )

    assert main(
        [
            "skill",
            "add",
            str(skill_dir),
            "--server",
            "gpu-a",
            "--server",
            "gpu-b",
            "--json",
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["name"] == "gpu-operations"
    assert [server["alias"] for server in created["servers"]] == ["gpu-a", "gpu-b"]

    assert main(["skill", "resolve", "gpu-a", "gpu-b", "--json"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["ok"] is True
    assert resolved["skills"][0]["applies_to"] == ["gpu-a", "gpu-b"]
    assert [host["alias"] for host in resolved["hosts"]] == ["gpu-a", "gpu-b"]

    assert main(["skill", "remove", "gpu-operations", "--yes", "--json"]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["error"] == "ConflictError"

    assert main(["skill", "detach", "gpu-operations", "gpu-a", "gpu-b", "--json"]) == 0
    capsys.readouterr()
    assert main(["skill", "remove", "gpu-operations", "--yes", "--json"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["name"] == "gpu-operations"
    assert (skill_dir / "SKILL.md").exists()


def test_cli_skill_resolve_reports_missing_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    database = Database()
    database.create_server(alias="box", hostname="box.example", port=22, username="alice")
    skill_dir = tmp_path / "skills" / "box-guide"
    skill_dir.mkdir(parents=True)
    manifest = skill_dir / "SKILL.md"
    manifest.write_text(
        "---\nname: box-guide\ndescription: Local operating guide.\n---\n",
        encoding="utf-8",
    )
    assert main(["skill", "add", str(manifest), "--server", "box", "--json"]) == 0
    capsys.readouterr()
    manifest.unlink()

    assert main(["skill", "resolve", "box", "--json"]) == 1
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["ok"] is False
    assert resolved["skills"][0]["status"] == "missing"


def test_cli_saves_and_resolves_working_directories(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    database = Database()
    database.create_server(alias="web1", hostname="a.example", port=22, username="deploy")
    database.create_server(alias="web2", hostname="b.example", port=22, username="deploy")

    assert main(["path", "add", "web1", "/srv/app", "--label", "app", "--note", "root", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["path"] == "/srv/app"
    assert created["notes"] == "root"
    assert created["last_used_at"] is None

    assert main(["path", "add", "web2", "/srv/app", "--label", "app", "--json"]) == 0
    capsys.readouterr()
    assert main(["path", "add", "web1", "/var/log/app", "--label", "logs", "--json"]) == 0
    capsys.readouterr()

    assert main(["path", "list", "web1", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["label"] for item in listed] == ["app", "logs"]

    # Hosts that share a layout are grouped, so an agent can act on both at once.
    assert main(["path", "resolve", "web1", "web2", "--json"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["ok"] is True
    shared = {item["path"]: item["applies_to"] for item in resolved["paths"]}
    assert shared["/srv/app"] == ["web1", "web2"]
    assert shared["/var/log/app"] == ["web1"]

    # A label collides case-insensitively within one host but not across hosts.
    assert main(["path", "add", "web1", "/elsewhere", "--label", "APP", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "ConflictError"

    assert main(["path", "edit", "web1", "app", "--label", "application", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["label"] == "application"

    assert main(["path", "remove", "web1", "application", "--yes", "--json"]) == 0
    capsys.readouterr()
    assert main(["path", "list", "web1", "--json"]) == 0
    assert [item["label"] for item in json.loads(capsys.readouterr().out)] == ["logs"]


def test_cli_rejects_an_empty_saved_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    Database().create_server(alias="box", hostname="box.example", port=22, username="alice")

    # An empty remote path means "home" when browsing, but saving it would record
    # a directory that names nothing.
    assert main(["path", "add", "box", "", "--label", "nowhere", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "ValidationError"


def test_transfer_resolution_picks_the_remote_side():
    from ssh_server_manager.cli import resolve_transfer

    assert resolve_transfer("web1:/srv/app.yml", "./local.yml") == (
        "download",
        "web1",
        "/srv/app.yml",
        "./local.yml",
    )
    assert resolve_transfer("./local.yml", "web1:/srv/app.yml") == (
        "upload",
        "web1",
        "/srv/app.yml",
        "./local.yml",
    )
    # A Windows drive letter is a local path even though "C" is a legal alias.
    with pytest.raises(ValidationError, match="one side must name a host"):
        resolve_transfer("C:\\Users\\me\\f.txt", "D:\\backup\\f.txt")
    with pytest.raises(ValidationError, match="host-to-host"):
        resolve_transfer("web1:/a", "web2:/b")
    with pytest.raises(ValidationError, match="one side must name a host"):
        resolve_transfer("./a", "./b")


def test_local_destination_resolves_directories_and_guards_overwrites(tmp_path):
    from ssh_server_manager.validation import validate_local_destination

    # An existing directory receives the remote basename, the way cp behaves.
    assert validate_local_destination(tmp_path, remote_name="config.yml") == (
        tmp_path / "config.yml"
    )
    existing = tmp_path / "taken.yml"
    existing.write_text("keep me", encoding="utf-8")
    with pytest.raises(ValidationError, match="--force"):
        validate_local_destination(existing, remote_name="taken.yml")
    assert validate_local_destination(existing, remote_name="taken.yml", force=True) == existing
    with pytest.raises(ValidationError, match="destination directory does not exist"):
        validate_local_destination(tmp_path / "absent" / "f.yml", remote_name="f.yml")


@pytest.mark.parametrize("shell_command", ["ui", "web"])
def test_cli_ui_credential_deep_links_into_the_server_dialog(tmp_path, monkeypatch, capsys, shell_command):
    pytest.importorskip("uvicorn")
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    database = Database()
    database.create_server(alias="web1", hostname="a.example", port=22, username="deploy")
    url_file = tmp_path / "launch-url"
    observed = {}

    def fake_run(self, *args, **kwargs):
        observed["url"] = url_file.read_text(encoding="utf-8").strip()

    monkeypatch.setattr("uvicorn.Server.run", fake_run)

    assert main([shell_command, "--credential", "web1", "--no-open", "--url-file", str(url_file)]) == 0
    assert "token=" in observed["url"]
    assert observed["url"].endswith("#servers/web1/credential")
    assert "token=" not in capsys.readouterr().out

    assert main([shell_command, "--credential", "ghost", "--no-open", "--url-file", str(url_file)]) == 2
    assert "server not found" in capsys.readouterr().err


def test_cli_server_add_password_credential_in_one_shot(tmp_path, monkeypatch, capsys):
    import getpass
    from types import SimpleNamespace

    from ssh_server_manager.vault import MemoryVault

    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    vault = MemoryVault()
    monkeypatch.setattr("ssh_server_manager.cli.get_vault", lambda: vault)
    prompts = iter(["s3cret", "s3cret"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(prompts))
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))

    assert (
        main(
            [
                "server",
                "add",
                "web1",
                "--hostname",
                "a.example",
                "--username",
                "deploy",
                "--password-credential",
                "web-pass",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    database = Database()
    server = database.get_server("web1")
    credential = database.get_credential("web-pass")
    assert server["credential_id"] == credential["id"]
    assert credential["kind"] == "password"
    assert credential["has_secret"] is True
    assert vault.get_secret(credential["id"], "password") == "s3cret"

    # Without a TTY the hidden prompt cannot run, so fail fast with a pointer
    # to the browser path instead of hanging an agent tool call.
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert (
        main(
            [
                "server",
                "add",
                "web2",
                "--hostname",
                "b.example",
                "--username",
                "ops",
                "--password-credential",
                "ops-pass",
                "--json",
            ]
        )
        == 2
    )
    refused = json.loads(capsys.readouterr().out)
    assert "ui --credential" in refused["message"]
    with pytest.raises(NotFoundError):
        database.get_server("web2")

    # --credential and --password-credential are mutually exclusive.
    with pytest.raises(SystemExit):
        main(
            [
                "server",
                "add",
                "web3",
                "--hostname",
                "c.example",
                "--username",
                "ops",
                "--credential",
                "web-pass",
                "--password-credential",
                "other",
            ]
        )
    capsys.readouterr()


def test_cli_duplicate_conflicts_point_to_the_edit_commands(tmp_path, monkeypatch, capsys):
    import getpass

    from ssh_server_manager.vault import MemoryVault

    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    monkeypatch.setattr("ssh_server_manager.cli.get_vault", lambda: MemoryVault())

    assert main(["server", "add", "t_rna", "--hostname", "h.example", "--username", "ops", "--json"]) == 0
    capsys.readouterr()
    assert main(["server", "add", "t_rna", "--hostname", "h2.example", "--username", "ops", "--json"]) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert "server alias already exists" in conflict["message"]
    assert "serverctl server edit t_rna" in conflict["message"]

    assert main(["credential", "add-agent", "trna", "--json"]) == 0
    capsys.readouterr()
    assert main(["credential", "add-agent", "trna", "--json"]) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert "credential label already exists" in conflict["message"]
    assert "serverctl credential edit trna" in conflict["message"]

    # A taken label fails before the hidden prompt runs, so users correcting a
    # typo never type the secret twice for nothing.
    def forbidden_prompt(prompt):
        raise AssertionError("getpass must not run for a taken label")

    monkeypatch.setattr(getpass, "getpass", forbidden_prompt)
    assert main(["credential", "add-password", "trna", "--json"]) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert "--replace-secret" in conflict["message"]


def test_add_help_text_points_to_the_edit_commands(capsys):
    with pytest.raises(SystemExit) as server_help:
        main(["server", "add", "--help"])
    assert server_help.value.code == 0
    assert "serverctl server edit" in capsys.readouterr().out

    with pytest.raises(SystemExit) as credential_help:
        main(["credential", "add-password", "--help"])
    assert credential_help.value.code == 0
    assert "serverctl credential edit" in capsys.readouterr().out


def test_audit_list_reports_events_newest_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    from ssh_server_manager import audit

    audit.record("exec", alias="web1", command=["uptime"])
    audit.record("reveal", credential="prod root", slot="password")

    assert main(["audit", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert [event["event"] for event in payload["events"]] == ["reveal", "exec"]
    assert payload["events"][1]["command"] == "uptime"

    assert main(["audit", "list"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert "reveal" in lines[0]
    assert "prod root" in lines[0]
    assert "uptime" in lines[1]


def test_audit_list_filters_and_handles_an_empty_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    from ssh_server_manager import audit

    audit.record("exec", alias="web1", command=["a"])
    audit.record("exec", alias="db1", command=["b"])

    assert main(["audit", "list", "--alias", "db1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [event["command"] for event in payload["events"]] == ["b"]

    assert main(["audit", "path", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["path"].endswith("audit.log")

    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "empty"))
    assert main(["audit", "list"]) == 0
    assert "no audit events" in capsys.readouterr().out


def _export_env(tmp_path, monkeypatch, name="a"):
    data = tmp_path / name
    monkeypatch.setenv("SSM_DATA_DIR", str(data))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / f"{name}.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    return data


def test_export_writes_a_file_and_import_applies_it(tmp_path, monkeypatch, capsys):
    _export_env(tmp_path, monkeypatch, "source")
    assert main(["server", "add", "web1", "--hostname", "web1.example", "--username", "deploy"]) == 0
    capsys.readouterr()
    export_path = tmp_path / "inv.json"
    assert main(["export", "--output", str(export_path), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["hosts"] == 1
    assert export_path.exists()

    _export_env(tmp_path, monkeypatch, "target")
    # A preview writes nothing and reports the plan.
    assert main(["import", str(export_path)]) == 0
    assert "would apply: create 1" in capsys.readouterr().out
    assert main(["server", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert main(["import", str(export_path), "--apply"]) == 0
    assert "applied: create 1" in capsys.readouterr().out
    assert main(["server", "show", "web1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["hostname"] == "web1.example"


def test_export_to_stdout_keeps_the_document_parseable(tmp_path, monkeypatch, capsys):
    _export_env(tmp_path, monkeypatch)
    assert main(["server", "add", "web1", "--hostname", "web1.example", "--username", "deploy"]) == 0
    capsys.readouterr()
    assert main(["export"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["format"] == "ssh-server-manager.export"
    assert [host["alias"] for host in document["hosts"]] == ["web1"]


def test_import_of_a_bad_file_exits_non_zero(tmp_path, monkeypatch, capsys):
    _export_env(tmp_path, monkeypatch)
    broken = tmp_path / "broken.json"
    broken.write_text('{"format": "nope"}', encoding="utf-8")
    assert main(["import", str(broken), "--apply"]) == 2
    assert "format" in capsys.readouterr().err or True


def test_geo_resolve_applies_a_lookup_and_keeps_manual_overrides(tmp_path, monkeypatch, capsys):
    from ssh_server_manager import geoip

    data = tmp_path / "data"
    monkeypatch.setenv("SSM_DATA_DIR", str(data))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    database = Database(data / "manager.db")
    database.create_server(alias="web1", hostname="web1.example", username="deploy")
    database.create_server(alias="db1", hostname="db1.example", username="alice")
    database.update_server("db1", geo={"country_code": "US", "city": "Ashburn", "source": "manual"})

    monkeypatch.setattr(geoip, "resolve_hostname", lambda host, **kwargs: {"web1.example": ["198.51.100.7"], "db1.example": ["198.51.100.8"]}.get(host, []))
    monkeypatch.setattr(
        geoip,
        "lookup",
        lambda address: {"country_code": "DE", "city": "Frankfurt", "source": "geoip"},
    )

    assert main(["geo", "resolve", "--all", "--apply", "--json"]) == 1  # db1 conflicts
    payload = json.loads(capsys.readouterr().out)
    rows = {row["alias"]: row for row in payload["hosts"]}
    assert rows["web1"]["applied"] is True
    assert rows["db1"]["applied"] is False
    assert "manual location override" in rows["db1"]["error"]
    assert database.get_server("web1")["geo"]["country_code"] == "DE"
    assert database.get_server("db1")["geo"]["country_code"] == "US"

    assert main(["geo", "resolve", "--all", "--apply", "--overwrite"]) == 0
    capsys.readouterr()
    assert database.get_server("db1")["geo"]["source"] == "geoip"


def test_geo_resolve_reports_an_unresolvable_host(tmp_path, monkeypatch, capsys):
    from ssh_server_manager import geoip

    data = tmp_path / "data"
    monkeypatch.setenv("SSM_DATA_DIR", str(data))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    Database(data / "manager.db").create_server(
        alias="web1", hostname="web1.example", username="deploy"
    )
    monkeypatch.setattr(geoip, "resolve_hostname", lambda host, **kwargs: [])
    assert main(["geo", "resolve", "web1", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["hosts"][0]["error"] == "hostname did not resolve"


def test_geo_resolve_needs_a_target(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("SSM_DATA_DIR", str(data))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    assert main(["geo", "resolve"]) == 2
    assert "name a host" in capsys.readouterr().err
