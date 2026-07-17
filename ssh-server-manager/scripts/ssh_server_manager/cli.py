from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .db import ConflictError, Database, DatabaseError, NotFoundError
from .importer import apply_import, preview_import
from .paths import database_path, managed_ssh_config_path, original_ssh_config_path
from .service import CredentialService
from .ssh_config import SSHConfigError, render_config
from .ssh_runner import SSHError, SSHRunner
from .validation import ValidationError
from .vault import VaultError, get_vault


def emit(value: Any, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
        return
    if isinstance(value, list):
        for item in value:
            print(format_item(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{key}={json.dumps(item, ensure_ascii=False)}")
            else:
                print(f"{key}={item}")
    else:
        print(value)


def format_item(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    if "hostname" in item:
        credential = item.get("credential_label") or "OpenSSH default"
        jumps = ",".join(item.get("proxy_jumps", [])) or "-"
        tags = ",".join(item.get("tags", [])) or "-"
        return f"{item['alias']:<22} {item['username']}@{item['hostname']}:{item['port']}  auth={credential}  jump={jumps}  tags={tags}"
    if "kind" in item:
        detail = item.get("key_path") or ("stored" if item.get("has_secret") else "not stored")
        return f"{item['label']:<22} {item['kind']:<8} {detail}"
    return json.dumps(item, ensure_ascii=False)


def add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="serverctl", description="Manage SSH hosts and secure credentials")
    parser.add_argument("--version", action="version", version=f"serverctl {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="diagnose local dependencies and storage")
    add_json_option(doctor)

    ui = commands.add_parser("ui", help="open the local management UI")
    ui.add_argument("--no-open", action="store_true", help="do not open a browser")
    ui.add_argument(
        "--port",
        type=int,
        default=0,
        help="loopback port; 0 (the default) chooses an available port",
    )
    ui.add_argument(
        "--url-file",
        type=Path,
        help="write the one-time URL to a mode-600 local file (required with --no-open)",
    )
    ui_action = ui.add_mutually_exclusive_group()
    ui_action.add_argument("--status", action="store_true", help="report the managed UI process instead of launching")
    ui_action.add_argument("--stop", action="store_true", help="stop the managed UI process and clean up its URL file")
    add_json_option(ui)

    server = commands.add_parser("server", help="manage connection profiles")
    server_commands = server.add_subparsers(dest="server_command", required=True)
    server_list = server_commands.add_parser("list")
    add_json_option(server_list)
    server_show = server_commands.add_parser("show")
    server_show.add_argument("alias")
    add_json_option(server_show)

    server_add = server_commands.add_parser("add")
    server_add.add_argument("alias")
    server_add.add_argument("--hostname", required=True)
    server_add.add_argument("--username", required=True)
    server_add.add_argument("--port", type=int, default=22)
    server_add.add_argument("--credential")
    server_add.add_argument("--proxy-jump", action="append", default=[])
    server_add.add_argument("--tag", action="append", default=[], help="project/context tag; repeat as needed")
    server_add.add_argument("--notes", default="")
    add_json_option(server_add)

    server_edit = server_commands.add_parser("edit")
    server_edit.add_argument("alias")
    server_edit.add_argument("--new-alias")
    server_edit.add_argument("--hostname")
    server_edit.add_argument("--username")
    server_edit.add_argument("--port", type=int)
    server_edit.add_argument("--credential", help="credential label/id, or 'none'")
    server_edit.add_argument("--proxy-jump", action="append")
    edit_tags = server_edit.add_mutually_exclusive_group()
    edit_tags.add_argument("--tag", action="append", help="replace project/context tags; repeat as needed")
    edit_tags.add_argument("--clear-tags", action="store_true")
    server_edit.add_argument("--notes")
    add_json_option(server_edit)

    server_remove = server_commands.add_parser("remove")
    server_remove.add_argument("alias")
    server_remove.add_argument("--yes", action="store_true")
    add_json_option(server_remove)

    server_import = server_commands.add_parser("import")
    server_import.add_argument("--config")
    server_import.add_argument("--apply", action="store_true")
    server_import.add_argument("--overwrite", action="store_true")
    add_json_option(server_import)

    server_test = server_commands.add_parser("test")
    server_test.add_argument("alias")
    server_test.add_argument("--timeout", type=int, default=8)
    add_json_option(server_test)

    credential = commands.add_parser("credential", help="manage reusable credentials")
    credential_commands = credential.add_subparsers(dest="credential_command", required=True)
    credential_list = credential_commands.add_parser("list")
    add_json_option(credential_list)
    add_password = credential_commands.add_parser("add-password")
    add_password.add_argument("label")
    add_json_option(add_password)
    add_key = credential_commands.add_parser("add-key")
    add_key.add_argument("label")
    add_key.add_argument("--key-path", required=True)
    add_key.add_argument("--store-passphrase", action="store_true")
    add_json_option(add_key)
    add_agent = credential_commands.add_parser("add-agent")
    add_agent.add_argument("label")
    add_json_option(add_agent)
    edit_credential = credential_commands.add_parser("edit")
    edit_credential.add_argument("credential")
    edit_credential.add_argument("--label")
    edit_credential.add_argument("--key-path")
    edit_credential.add_argument("--replace-secret", action="store_true")
    edit_credential.add_argument("--replace-passphrase", action="store_true")
    edit_credential.add_argument("--clear-passphrase", action="store_true")
    add_json_option(edit_credential)
    remove_credential = credential_commands.add_parser("remove")
    remove_credential.add_argument("credential")
    remove_credential.add_argument("--yes", action="store_true")
    add_json_option(remove_credential)

    connect = commands.add_parser("connect", help="open an SSH session")
    connect.add_argument("alias")
    connect.add_argument("--timeout", type=int, default=12)

    execute = commands.add_parser("exec", help="execute a remote command")
    execute.add_argument("alias")
    execute.add_argument("--stdin", action="store_true", help="read UTF-8 text from stdin")
    execute.add_argument(
        "--stdin-binary",
        action="store_true",
        help="read raw bytes from stdin (implies --stdin)",
    )
    execute.add_argument("--timeout", type=int, default=30)
    execute.add_argument("--reuse", type=int, default=0)
    execute.add_argument(
        "--shell",
        action="store_true",
        help="run one command string through the remote POSIX sh -lc",
    )
    execute.add_argument("--json", action="store_true")

    config = commands.add_parser("config", help="render managed OpenSSH config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_render = config_commands.add_parser("render")
    add_json_option(config_render)
    return parser


def resolve_credential(database: Database, value: str | None) -> str | None:
    if not value or value.casefold() == "none":
        return None
    return database.get_credential(value)["id"]


def confirm(prompt: str, forced: bool) -> bool:
    if forced:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{prompt} [y/N] ").strip().casefold() in {"y", "yes"}


def normalize_remote_command(remote: Sequence[str], *, shell: bool = False) -> list[str]:
    """Normalize the command after ``--`` without guessing at shell syntax."""
    command = list(remote)
    if not command:
        raise ValidationError("a remote command is required after --")
    if not shell:
        return command
    if len(command) != 1:
        raise ValidationError("--shell expects exactly one command string after --")
    return ["sh", "-lc", command[0]]


def ssh_client_version(ssh_path: str | None) -> str | None:
    if not ssh_path:
        return None
    try:
        result = subprocess.run([ssh_path, "-V"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stderr or result.stdout).strip().splitlines()[0] if (result.stderr or result.stdout).strip() else None


def run_doctor(database: Database) -> dict[str, Any]:
    ssh_path = shutil.which("ssh")
    checks: dict[str, Any] = {
        "version": {"ok": True, "serverctl": __version__},
        "platform": {
            "ok": True,
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
            "connection_reuse": os.name != "nt",
        },
        "database": {"ok": True, "path": str(database_path())},
        "ssh": {"ok": bool(ssh_path), "path": ssh_path, "client": ssh_client_version(ssh_path)},
        "sftp": {"ok": bool(shutil.which("sftp")), "path": shutil.which("sftp")},
        "ssh_keygen": {"ok": bool(shutil.which("ssh-keygen")), "path": shutil.which("ssh-keygen")},
        "original_config": {"ok": True, "path": str(original_ssh_config_path())},
        "managed_config": {"ok": True, "path": str(managed_ssh_config_path())},
    }
    try:
        checks["vault"] = {"ok": True, **get_vault().diagnose()}
    except VaultError as exc:
        checks["vault"] = {"ok": False, "message": str(exc)}
    for module in ("fastapi", "uvicorn", "webauthn", "argon2"):
        try:
            __import__(module)
            checks[module] = {"ok": True}
        except ImportError:
            checks[module] = {"ok": False, "message": "run scripts/bootstrap"}
    checks["ok"] = all(item.get("ok", False) for item in checks.values() if isinstance(item, dict))
    return checks


def handle(args: argparse.Namespace) -> int:
    database = Database()
    if args.command == "doctor":
        result = run_doctor(database)
        emit(result, as_json=args.json)
        return 0 if result["ok"] else 1
    if args.command == "ui":
        from .webapp import run_ui, ui_status, ui_stop

        if args.status:
            result = ui_status()
            emit(result, as_json=args.json)
            return 0 if result["running"] else 1
        if args.stop:
            result = ui_stop()
            emit(result, as_json=args.json)
            return 0 if not result["running"] else 1
        run_ui(
            database=database,
            port=args.port,
            open_browser=not args.no_open,
            url_file=args.url_file,
        )
        return 0
    if args.command == "server":
        if args.server_command == "list":
            emit(database.list_servers(), as_json=args.json)
        elif args.server_command == "show":
            emit(database.get_server(args.alias), as_json=args.json)
        elif args.server_command == "add":
            result = database.create_server(
                alias=args.alias,
                hostname=args.hostname,
                port=args.port,
                username=args.username,
                credential_id=resolve_credential(database, args.credential),
                proxy_jumps=args.proxy_jump,
                tags=args.tag,
                notes=args.notes,
            )
            render_config(database)
            emit(result, as_json=args.json)
        elif args.server_command == "edit":
            changes = {
                key: value
                for key, value in {
                    "alias": args.new_alias,
                    "hostname": args.hostname,
                    "port": args.port,
                    "username": args.username,
                    "notes": args.notes,
                }.items()
                if value is not None
            }
            if args.credential is not None:
                changes["credential_id"] = resolve_credential(database, args.credential)
            if args.proxy_jump is not None:
                changes["proxy_jumps"] = args.proxy_jump
            if args.clear_tags:
                changes["tags"] = []
            elif args.tag is not None:
                changes["tags"] = args.tag
            result = database.update_server(args.alias, **changes)
            render_config(database)
            emit(result, as_json=args.json)
        elif args.server_command == "remove":
            if not confirm(f"Remove server {args.alias}?", args.yes):
                raise DatabaseError("removal cancelled; use --yes for non-interactive removal")
            result = database.delete_server(args.alias)
            render_config(database)
            emit(result, as_json=args.json)
        elif args.server_command == "import":
            preview = preview_import(database, config=args.config)
            result = apply_import(database, preview, overwrite=args.overwrite) if args.apply else preview
            if args.apply:
                render_config(database)
            emit(result, as_json=args.json)
        elif args.server_command == "test":
            result = SSHRunner(database).test(args.alias, timeout=args.timeout)
            emit(result, as_json=args.json)
            return 0 if result["ok"] else 1
        return 0
    if args.command == "credential":
        if args.credential_command == "list":
            emit(database.list_credentials(), as_json=args.json)
            return 0
        service = CredentialService(database, get_vault())
        if args.credential_command == "add-password":
            first = getpass.getpass("Password: ")
            second = getpass.getpass("Confirm password: ")
            if first != second:
                raise VaultError("password confirmation does not match")
            result = service.create_password(args.label, first)
        elif args.credential_command == "add-key":
            passphrase = getpass.getpass("Private-key passphrase: ") if args.store_passphrase else None
            result = service.create_key(args.label, args.key_path, passphrase or None)
        elif args.credential_command == "add-agent":
            result = service.create_agent(args.label)
        elif args.credential_command == "edit":
            secret = getpass.getpass("New password: ") if args.replace_secret else None
            passphrase = getpass.getpass("New private-key passphrase: ") if args.replace_passphrase else None
            result = service.update(
                args.credential,
                label=args.label,
                key_path=args.key_path,
                secret=secret,
                passphrase=passphrase,
                clear_passphrase=args.clear_passphrase,
            )
        elif args.credential_command == "remove":
            if not confirm(f"Remove credential {args.credential}?", args.yes):
                raise DatabaseError("removal cancelled; use --yes for non-interactive removal")
            result = service.delete(args.credential)
        render_config(database)
        emit(result, as_json=args.json)
        return 0
    if args.command == "connect":
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise SSHError(
                "connect opens an interactive shell and requires a terminal (TTY); "
                "run this command directly in your own terminal, or use "
                "'serverctl exec ALIAS -- COMMAND' to run commands non-interactively"
            )
        return SSHRunner(database).connect(args.alias, timeout=args.timeout)
    if args.command == "exec":
        if args.reuse > 0 and os.name == "nt":
            print(
                "warning: --reuse is ignored on Windows; OpenSSH ControlMaster needs Unix domain sockets",
                file=sys.stderr,
            )
        remote = normalize_remote_command(args.remote_command, shell=args.shell)
        if args.stdin_binary:
            stdin_data = sys.stdin.buffer.read()
        else:
            stdin_data = sys.stdin.read() if args.stdin else None
        result = SSHRunner(database).execute(
            args.alias,
            remote,
            stdin_data=stdin_data,
            timeout=args.timeout,
            reuse=args.reuse,
            capture=args.json,
        )
        if args.json:
            emit(result, as_json=True)
        return int(result["returncode"])
    if args.command == "config" and args.config_command == "render":
        path = render_config(database)
        emit({"path": str(path)}, as_json=args.json)
        return 0
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    remote_command: list[str] = []
    if raw_argv and raw_argv[0] == "exec" and "--" in raw_argv:
        separator = raw_argv.index("--")
        remote_command = raw_argv[separator + 1 :]
        raw_argv = raw_argv[:separator]
    args = parser.parse_args(raw_argv)
    if args.command == "exec":
        args.remote_command = remote_command
    try:
        return handle(args)
    except (ValidationError, VaultError, DatabaseError, SSHConfigError, SSHError, ValueError) as exc:
        as_json = bool(getattr(args, "json", False))
        if as_json:
            print(json.dumps({"ok": False, "error": exc.__class__.__name__, "message": str(exc)}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
