"""`credential rotate`: save a host's new password only after the host accepts it."""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ssh_server_manager import audit
from ssh_server_manager.application import CredentialApplicationService
from ssh_server_manager.cli import main
from ssh_server_manager.db import ConflictError, Database, utc_now
from ssh_server_manager.validation import ValidationError
from ssh_server_manager.vault import MemoryVault

from test_credential_probe import fixture_runner, loopback_servers  # noqa: F401  (fixture)

OLD = "old-password-never-log"
NEW = "new-password-never-log"


class ScriptedRunner:
    """Accept whatever password each host currently has; record what was offered."""

    def __init__(self, server_passwords: dict[str, str | None], offered: list):
        self.server_passwords = server_passwords
        self.offered = offered

    def __call__(self, database, vault_factory):
        self.database, self.vault_factory = database, vault_factory
        return self

    def test(self, credential_id, host_id, *, timeout=8, candidate=None):
        password = candidate if candidate is not None else self.vault_factory().get_secret(credential_id, "password")
        host = self.database.get_server(host_id)
        self.offered.append((host["alias"], "candidate" if candidate is not None else "saved"))
        current = self.server_passwords[host["alias"]]
        if current is None:
            ok, code = False, "network-unreachable"
        else:
            ok, code = password == current, None if password == current else "authentication-failed"
        return {"ok": ok, "error_code": code, "message": "accepted" if ok else "not accepted",
                "latency_ms": 1, "checked_at": utc_now(), "host_id": host["id"], "host_alias": host["alias"],
                "credential_id": credential_id, "credential_kind": "password",
                "method": "password" if ok else None, "stages": []}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    database = Database()
    vault = MemoryVault()
    rendered = []
    service = CredentialApplicationService(database, lambda: vault, lambda: rendered.append(1) or tmp_path)
    credential = service.create_password("web-pass", OLD)
    host = database.create_server(alias="web1", hostname="a.example", port=22, username="deploy",
                                  credential_id=credential["id"])
    offered: list = []
    passwords = {"web1": NEW}
    monkeypatch.setattr("ssh_server_manager.credential_probe.CredentialProbeRunner",
                        ScriptedRunner(passwords, offered))
    return SimpleNamespace(database=database, vault=vault, service=service, credential=credential,
                           host=host, offered=offered, passwords=passwords, rendered=rendered)


def audit_events():
    return [entry for entry in audit.read_events() if entry["event"] == "password_rotate"]


def test_unshared_password_is_replaced_in_place_after_the_host_accepts_it(state):
    result = state.service.rotate_password("web1", NEW)

    assert result["committed"] and result["verified"] and result["mode"] == "replace"
    assert state.offered == [("web1", "candidate")]
    assert state.vault.get_secret(state.credential["id"], "password") == NEW
    assert state.database.get_server("web1")["credential_id"] == state.credential["id"]
    assert state.rendered
    tests = state.service.tests(state.credential["id"])
    assert tests[0]["ok"] is True and tests[0]["host_id"] == state.host["id"]
    [event] = audit_events()
    assert event["ok"] is True and event["alias"] == "web1" and event["credential"] == "web-pass"
    assert NEW not in json.dumps(result, default=str) and NEW not in json.dumps(audit_events())


def test_rejected_candidate_changes_nothing_and_says_the_saved_password_still_works(state):
    state.passwords["web1"] = OLD

    result = state.service.rotate_password("web1", NEW)

    assert not result["committed"] and not result["verified"]
    assert result["error_code"] == "authentication-failed"
    assert result["saved_password_accepted"] is True
    assert state.offered == [("web1", "candidate"), ("web1", "saved")]
    assert state.vault.get_secret(state.credential["id"], "password") == OLD
    assert state.service.tests(state.credential["id"]) == []
    [event] = audit_events()
    assert event["ok"] is False and event["error_code"] == "authentication-failed"


def test_both_passwords_rejected_is_reported_as_such(state):
    state.passwords["web1"] = "something-else"

    result = state.service.rotate_password("web1", NEW)

    assert not result["committed"] and result["saved_password_accepted"] is False
    assert state.vault.get_secret(state.credential["id"], "password") == OLD


def test_unreachable_host_does_not_spend_a_second_login_attempt(state):
    state.passwords["web1"] = None

    result = state.service.rotate_password("web1", NEW)

    assert not result["committed"] and result["error_code"] == "network-unreachable"
    assert result["saved_password_accepted"] is None
    assert state.offered == [("web1", "candidate")]


def test_shared_credential_is_split_so_other_hosts_keep_the_old_password(state):
    state.database.create_server(alias="web2", hostname="b.example", port=22, username="deploy",
                                 credential_id=state.credential["id"])

    plan = state.service.rotation_plan("web1")
    assert plan["mode"] == "split" and plan["shared_with"] == ["web2"]
    assert plan["new_label"] == "web-pass (web1)"
    result = state.service.rotate_password("web1", NEW)

    assert result["committed"] and result["mode"] == "split"
    created = result["credential"]
    assert created["label"] == "web-pass (web1)" and created["id"] != state.credential["id"]
    assert state.database.get_server("web1")["credential_id"] == created["id"]
    assert state.database.get_server("web2")["credential_id"] == state.credential["id"]
    assert state.vault.get_secret(created["id"], "password") == NEW
    assert state.vault.get_secret(state.credential["id"], "password") == OLD
    assert state.service.tests(created["id"])[0]["credential_id"] == created["id"]


def test_split_label_can_be_chosen_and_must_be_free(state):
    state.database.create_server(alias="web2", hostname="b.example", port=22, username="deploy",
                                 credential_id=state.credential["id"])
    state.service.create_password("web-pass (web1)", "unrelated")

    with pytest.raises(ConflictError, match="--new-label"):
        state.service.rotation_plan("web1")
    assert state.service.rotation_plan("web1", new_label="web1 login")["new_label"] == "web1 login"


def test_plan_refuses_what_cannot_be_rotated_without_reading_a_secret(state):
    agent = state.service.create_agent("agent")
    state.database.create_server(alias="bare", hostname="c.example", port=22, username="deploy")
    state.database.create_server(alias="agented", hostname="d.example", port=22, username="deploy",
                                 credential_id=agent["id"])

    with pytest.raises(ValidationError, match="no saved credential"):
        state.service.rotation_plan("bare")
    with pytest.raises(ValidationError, match="only a saved password"):
        state.service.rotation_plan("agented")
    with pytest.raises(ValidationError, match="drop --new-label"):
        state.service.rotation_plan("web1", new_label="unneeded")
    with pytest.raises(ValidationError):
        state.service.rotate_password("web1", "")
    assert state.offered == []


def test_rotation_refuses_when_the_binding_changed_under_it(state, monkeypatch):
    plan = state.service.rotation_plan("web1")
    stale = {**plan, "credential_updated_at": "1970-01-01T00:00:00Z"}
    calls = iter([stale, plan])
    monkeypatch.setattr(state.service, "rotation_plan", lambda *args, **kwargs: next(calls))

    with pytest.raises(ConflictError):
        state.service.rotate_password("web1", NEW)
    assert state.offered == []
    assert state.vault.get_secret(state.credential["id"], "password") == OLD


def test_real_rotation_saves_only_what_the_server_accepted(tmp_path, monkeypatch, loopback_servers):
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "original.conf"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    database = Database()
    vault = MemoryVault()
    service = CredentialApplicationService(database, lambda: vault, lambda: tmp_path)
    credential = service.create_password("web-pass", OLD)
    server = loopback_servers(password=NEW)
    database.create_server(alias="web1", hostname="127.0.0.1", port=server.port, username="fixture",
                           credential_id=credential["id"])
    runner = fixture_runner(database, vault, tmp_path, [server])
    monkeypatch.setattr("ssh_server_manager.credential_probe.CredentialProbeRunner",
                        lambda database, vault_factory: runner)

    rejected = service.rotate_password("web1", "typo-password", timeout=8)
    assert not rejected["committed"] and rejected["saved_password_accepted"] is False
    assert vault.get_secret(credential["id"], "password") == OLD

    accepted = service.rotate_password("web1", NEW, timeout=8)
    assert accepted["committed"], accepted
    assert vault.get_secret(credential["id"], "password") == NEW
    assert server.password_attempts == ["typo-password", OLD, NEW]
    assert not server.sessions


# --- CLI -----------------------------------------------------------------


@pytest.fixture
def cli_state(state, monkeypatch):
    monkeypatch.setattr("ssh_server_manager.cli.get_vault", lambda: state.vault)
    return state


def terminal(monkeypatch, *, lines=("\n",), prompts=(NEW, NEW), tty=True):
    import getpass
    import sys

    answers = iter(prompts)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
    stdin = io.StringIO("".join(lines))
    stdin.isatty = lambda: tty
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)


def test_cli_rotation_saves_after_the_host_accepts(cli_state, monkeypatch, capsys):
    terminal(monkeypatch)

    assert main(["credential", "rotate", "web1"]) == 0
    captured = capsys.readouterr()
    assert "Nothing is saved yet" in captured.err and "passwd" in captured.err
    assert "web-pass now holds it" in captured.out
    assert cli_state.vault.get_secret(cli_state.credential["id"], "password") == NEW
    assert NEW not in captured.out + captured.err


def test_cli_rotation_explains_a_rejection_and_stops_without_saving(cli_state, monkeypatch, capsys):
    cli_state.passwords["web1"] = OLD
    terminal(monkeypatch, lines=("\n",))  # one check, then end of input

    assert main(["credential", "rotate", "web1"]) == 1
    captured = capsys.readouterr()
    assert "still accepts the saved one" in captured.err
    assert "Nothing was saved; web-pass still holds the old password" in captured.err
    assert cli_state.vault.get_secret(cli_state.credential["id"], "password") == OLD


def test_cli_rotation_retries_on_enter(cli_state, monkeypatch, capsys):
    cli_state.passwords["web1"] = OLD
    terminal(monkeypatch, lines=("\n", "\n"))
    calls = []
    original = cli_state.service.rotate_password

    def change_server_after_first_check(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            cli_state.passwords["web1"] = NEW
        return original(*args, **kwargs)

    monkeypatch.setattr(CredentialApplicationService, "rotate_password",
                        lambda self, *args, **kwargs: change_server_after_first_check(*args, **kwargs))
    assert main(["credential", "rotate", "web1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["committed"] is True and payload["mode"] == "replace"
    assert len(calls) == 2


def test_cli_rotation_without_a_terminal_fails_before_any_prompt(cli_state, monkeypatch, capsys):
    import getpass

    def forbidden(prompt):
        raise AssertionError("no hidden prompt without a terminal")

    terminal(monkeypatch, tty=False)
    monkeypatch.setattr(getpass, "getpass", forbidden)
    assert main(["credential", "rotate", "web1", "--json"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert "needs a terminal" in refused["message"]
    assert "serverctl credential rotate web1" in refused["message"]

    cli_state.database.create_server(alias="bare", hostname="c.example", port=22, username="deploy")
    terminal(monkeypatch, tty=True)
    monkeypatch.setattr(getpass, "getpass", forbidden)
    assert main(["credential", "rotate", "bare", "--json"]) == 2
    assert "no saved credential" in json.loads(capsys.readouterr().out)["message"]


def test_cli_rotation_rejects_a_mismatched_confirmation(cli_state, monkeypatch, capsys):
    terminal(monkeypatch, prompts=(NEW, "different"))

    assert main(["credential", "rotate", "web1"]) == 2
    assert "does not match" in capsys.readouterr().err
    assert cli_state.offered == []


def test_edit_help_says_replace_secret_is_local_only(capsys):
    with pytest.raises(SystemExit):
        main(["credential", "edit", "--help"])
    text = capsys.readouterr().out
    assert "local vault only" in text and "credential rotate" in text
