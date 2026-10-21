from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .application import ApplicationServices, CredentialApplicationService
from .db import ConflictError, Database, DatabaseError, NotFoundError
from .geoip import capability as geoip_capability
from .mosh import mosh_capability
from .mounts import list_mounts, mount_capability
from .mounts import mount as mount_host
from .mounts import unmount as unmount_path
from .paths import database_path, managed_ssh_config_path, original_ssh_config_path
from .ssh_config import SSHConfigError, render_config
from .ssh_runner import SSHError, SSHRunner
from .validation import (
    ValidationError,
    parse_remote_ref,
    validate_local_destination,
    validate_local_source,
)
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
    if "name" in item and "forward" in item:
        kind = item.get("kind", "local")
        aliases = item.get("server") or item.get("alias") or "-"
        if item.get("running"):
            state = f"running pid={item.get('pid')}"
        else:
            state = "saved"
        orphan = "  orphan" if item.get("orphan") else ""
        return f"{item['name']:<28} {aliases:<16} {kind:<8} {item['forward']}  {state}{orphan}"
    if "kind" in item:
        detail = item.get("key_path") or ("stored" if item.get("has_secret") else "not stored")
        return f"{item['label']:<22} {item['kind']:<8} {detail}"
    if "label" in item and "path" in item:
        used = item.get("last_used_at") or "never"
        notes = f"  {item['notes']}" if item.get("notes") else ""
        return f"{item['label']:<24} {item['path']:<32} used={used}{notes}"
    if "name" in item and "path" in item:
        servers = ",".join(server["alias"] for server in item.get("servers", [])) or "-"
        status = item.get("status")
        suffix = f"  status={status}" if status else ""
        return f"{item['name']:<28} {item['path']}  servers={servers}{suffix}"
    return json.dumps(item, ensure_ascii=False)


def tunnel_label(item: dict[str, Any]) -> str:
    forward = item.get("forward")
    return f"{item.get('name')} ({forward})" if forward else str(item.get("name"))


def emit_tunnel_line(message: str, payload: dict[str, Any], *, as_json: bool = False) -> None:
    """One line for a person, the whole record for a machine."""
    if as_json:
        emit(payload, as_json=True)
    else:
        print(message)


def print_import_plan(
    plan: dict[str, Any], *, applied: bool, result: dict[str, Any] | None = None
) -> None:
    verb = "applied" if applied else "would apply"
    counts = plan.get("counts", {})
    summary = ", ".join(f"{key} {value}" for key, value in sorted(counts.items())) or "nothing"
    print(f"{verb}: {summary}")
    for action in plan.get("hosts", []):
        name = action["alias"]
        kind = action["action"]
        if kind == "update":
            fields = ", ".join(sorted(action.get("changes", {})))
            print(f"  update  {name}  ({fields})")
        elif kind == "create":
            print(f"  create  {name}")
        else:
            print(f"  {kind:<7} {name}")
    for warning in plan.get("warnings", []):
        print(f"warning: {warning['alias']}: {warning['message']}", file=sys.stderr)
    if result:
        for problem in result.get("errors", []):
            print(f"error: {problem['alias']}: {problem['message']}", file=sys.stderr)
        if result.get("skipped"):
            print(
                "skipped (already here; re-run with --overwrite to update): "
                + ", ".join(result["skipped"]),
                file=sys.stderr,
            )


def format_audit_event(entry: dict[str, Any]) -> str:
    when = str(entry.get("at") or "")[:19].replace("T", " ")
    kind = str(entry.get("event") or "?")
    alias = entry.get("alias") or entry.get("credential") or entry.get("host") or "-"
    detail = ""
    if entry.get("command") is not None:
        command = entry["command"]
        detail = command if isinstance(command, str) else " ".join(str(item) for item in command)
    elif entry.get("returncode") is not None:
        detail = f"returncode={entry.get('returncode')}"
    elif entry.get("slot"):
        detail = f"slot={entry['slot']}"
    elif entry.get("error_code"):
        detail = f"error={entry['error_code']}"
    actor = entry.get("actor") or "cli"
    return f"{when}  {kind:<14} {actor:<4} {alias:<18} {detail}".rstrip()


def add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def add_transfer_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true", help="overwrite an existing local file")
    parser.add_argument(
        "--timeout", type=int, default=120, help="seconds to allow for the transfer (default 120)"
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="copy a whole directory tree (symlinks inside it are refused)",
    )
    add_json_option(parser)


def resolve_transfer(source: str, destination: str) -> tuple[str, str, str, str]:
    """Work out which side of a copy is remote.

    Returns (direction, alias, remote_path, local_path). Exactly one side may be
    a remote reference: sftp cannot move bytes between two hosts without staging
    them here, and silently doing that would hide the local copy from the user.
    """
    source_alias, source_path = parse_remote_ref(source)
    destination_alias, destination_path = parse_remote_ref(destination)
    if source_alias and destination_alias:
        raise ValidationError(
            "host-to-host copies are not supported; download to this machine first"
        )
    if source_alias:
        return "download", source_alias, source_path, destination_path
    if destination_alias:
        return "upload", destination_alias, destination_path, source_path
    raise ValidationError(
        "one side must name a host as ALIAS:PATH, for example web1:/srv/app/config.yml"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="serverctl", description="Manage SSH hosts and secure credentials")
    parser.add_argument("--version", action="version", version=f"serverctl {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="diagnose local dependencies and storage")
    add_json_option(doctor)

    ui = commands.add_parser("web", aliases=["ui"], help="open the local management UI (ui is a compatibility alias)")
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
    ui_action.add_argument(
        "--credential",
        metavar="ALIAS",
        help="open the UI directly on ALIAS's edit dialog with the new-credential form expanded",
    )
    add_json_option(ui)

    server = commands.add_parser("server", help="manage connection profiles")
    server_commands = server.add_subparsers(dest="server_command", required=True)
    server_list = server_commands.add_parser("list")
    add_json_option(server_list)
    server_show = server_commands.add_parser("show")
    server_show.add_argument("alias")
    add_json_option(server_show)

    server_add = server_commands.add_parser(
        "add",
        epilog="add only creates and refuses an existing alias; "
        "to change a server use 'serverctl server edit ALIAS'.",
    )
    server_add.add_argument("alias")
    server_add.add_argument("--hostname", required=True)
    server_add.add_argument("--username", required=True)
    server_add.add_argument("--port", type=int, default=22)
    server_add_credential = server_add.add_mutually_exclusive_group()
    server_add_credential.add_argument("--credential")
    server_add_credential.add_argument(
        "--password-credential",
        metavar="LABEL",
        help="create a password credential via a hidden local prompt and attach it (terminal only)",
    )
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

    server_diagnose = server_commands.add_parser(
        "diagnose", help="run read-only profile, SSH, SFTP, and remote-system checks"
    )
    server_diagnose.add_argument("alias")
    server_diagnose.add_argument("--timeout", type=int, default=8)
    add_json_option(server_diagnose)

    server_note = server_commands.add_parser("note", help="set, append, or clear a server note")
    server_note.add_argument("alias")
    note_action = server_note.add_mutually_exclusive_group(required=True)
    note_action.add_argument("--text", help="note text; use --append to keep the existing note")
    note_action.add_argument("--clear", action="store_true", help="remove the current note")
    server_note.add_argument("--append", action="store_true", help="append text after the existing note")
    add_json_option(server_note)

    skill = commands.add_parser("skill", help="manage local Agent Skills attached to hosts")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    skill_discover = skill_commands.add_parser("discover", help="find local Skills without registering them")
    add_json_option(skill_discover)
    skill_list = skill_commands.add_parser("list", help="list registered Skills")
    skill_list.add_argument("--server", help="only Skills attached to this server alias/id")
    add_json_option(skill_list)
    skill_show = skill_commands.add_parser("show", help="show one registered Skill")
    skill_show.add_argument("skill", help="Skill name or id")
    add_json_option(skill_show)
    skill_add = skill_commands.add_parser("add", help="register a local Skill and optionally attach hosts")
    skill_add.add_argument("path", help="Skill directory or SKILL.md path")
    skill_add.add_argument("--server", action="append", default=[], help="server alias/id; repeat as needed")
    add_json_option(skill_add)
    skill_refresh = skill_commands.add_parser("refresh", help="refresh metadata from SKILL.md")
    skill_refresh.add_argument("skill", help="Skill name or id")
    skill_refresh.add_argument("--path", help="new Skill directory or SKILL.md path")
    add_json_option(skill_refresh)
    skill_attach = skill_commands.add_parser("attach", help="attach a Skill to one or more hosts")
    skill_attach.add_argument("skill", help="Skill name or id")
    skill_attach.add_argument("servers", nargs="+", metavar="SERVER", help="server alias or id")
    add_json_option(skill_attach)
    skill_detach = skill_commands.add_parser("detach", help="detach a Skill from one or more hosts")
    skill_detach.add_argument("skill", help="Skill name or id")
    skill_detach.add_argument("servers", nargs="+", metavar="SERVER", help="server alias or id")
    add_json_option(skill_detach)
    skill_resolve = skill_commands.add_parser(
        "resolve", help="resolve the local Skills that apply to one or more target hosts"
    )
    skill_resolve.add_argument("servers", nargs="+", metavar="SERVER", help="server alias or id")
    add_json_option(skill_resolve)
    skill_remove = skill_commands.add_parser("remove", help="unregister a Skill without deleting local files")
    skill_remove.add_argument("skill", help="Skill name or id")
    skill_remove.add_argument("--yes", action="store_true")
    add_json_option(skill_remove)

    path_parser = commands.add_parser(
        "path", help="manage a host's saved working directories"
    )
    path_commands = path_parser.add_subparsers(dest="path_command", required=True)
    path_list = path_commands.add_parser("list", help="list a host's saved directories")
    path_list.add_argument("server", help="server alias or id")
    add_json_option(path_list)
    path_add = path_commands.add_parser("add", help="save a working directory for a host")
    path_add.add_argument("server", help="server alias or id")
    path_add.add_argument("path", help="absolute remote directory, or ~ for the login directory")
    path_add.add_argument("--label", required=True, help="short name used to recall the directory")
    path_add.add_argument("--note", default=None, help="optional reminder of what lives there")
    add_json_option(path_add)
    path_edit = path_commands.add_parser("edit", help="change a saved directory")
    path_edit.add_argument("server", help="server alias or id")
    path_edit.add_argument("directory", help="saved directory label or id")
    path_edit.add_argument("--label")
    path_edit.add_argument("--path")
    path_edit.add_argument("--note")
    add_json_option(path_edit)
    path_remove = path_commands.add_parser("remove", help="forget a saved directory")
    path_remove.add_argument("server", help="server alias or id")
    path_remove.add_argument("directory", help="saved directory label or id")
    path_remove.add_argument("--yes", action="store_true")
    add_json_option(path_remove)
    path_resolve = path_commands.add_parser(
        "resolve", help="resolve the saved directories for one or more target hosts"
    )
    path_resolve.add_argument("servers", nargs="+", metavar="SERVER", help="server alias or id")
    add_json_option(path_resolve)

    copy = commands.add_parser(
        "cp",
        help="copy one file (or, with --recursive, one tree) between this machine and a managed host",
    )
    copy.add_argument("source", help="LOCAL path or ALIAS:REMOTE path")
    copy.add_argument("destination", help="LOCAL path or ALIAS:REMOTE path")
    add_transfer_options(copy)
    get = commands.add_parser(
        "get", help="download one file (or, with --recursive, one tree) from a managed host"
    )
    get.add_argument("source", metavar="ALIAS:REMOTE", help="remote file to download")
    get.add_argument("destination", nargs="?", default=None, help="local path (default: .)")
    add_transfer_options(get)
    put = commands.add_parser(
        "put", help="upload one file (or, with --recursive, one tree) to a managed host"
    )
    put.add_argument("source", metavar="LOCAL", help="local file to upload")
    put.add_argument("destination", metavar="ALIAS:REMOTE", help="remote destination path")
    add_transfer_options(put)

    mount = commands.add_parser("mount", help="mount a host's directory locally with sshfs")
    # --list rather than a `mount list` subcommand: argparse cannot offer both a
    # subcommand and an optional positional on the same parser, and
    # `serverctl mount web1:/srv/app` is the form worth keeping.
    mount.add_argument("--list", action="store_true", help="list active sshfs mounts and exit")
    mount.add_argument(
        "server", nargs="?", metavar="ALIAS[:REMOTE]", help="host to mount, optionally with a path"
    )
    mount.add_argument("mountpoint", nargs="?", default=None, help="local directory (default ~/mnt/ALIAS)")
    mount.add_argument("--read-only", action="store_true", help="mount without write access")
    add_json_option(mount)
    unmount = commands.add_parser("unmount", help="unmount a directory mounted by serverctl")
    unmount.add_argument("target", metavar="ALIAS|MOUNTPOINT")
    add_json_option(unmount)

    tunnel = commands.add_parser(
        "tunnel",
        help="save and open port forwards (-L/-R/-D) for a host",
        epilog="the forward the tunnel points at is resolved on the remote side, so "
        "'--local 15432:localhost:5432' means: on this machine, port 15432 reaches "
        "the host's own localhost:5432.",
    )
    tunnel_commands = tunnel.add_subparsers(dest="tunnel_command", required=True)
    tunnel_list = tunnel_commands.add_parser("list", help="list saved forwarding profiles")
    tunnel_list.add_argument("--server", help="only profiles for this host")
    add_json_option(tunnel_list)
    tunnel_show = tunnel_commands.add_parser("show", help="show one forwarding profile")
    tunnel_show.add_argument("tunnel", help="profile name, id, or ALIAS:NAME")
    add_json_option(tunnel_show)
    tunnel_add = tunnel_commands.add_parser(
        "add",
        help="save a forwarding profile for a host",
        epilog="bind addresses default to 127.0.0.1; pass an address only when the "
        "forward really should be reachable from other machines.",
    )
    tunnel_add.add_argument("name", help="profile name, unique across all hosts")
    tunnel_add.add_argument("--server", required=True, help="host alias or id the forward belongs to")
    tunnel_kind = tunnel_add.add_mutually_exclusive_group(required=True)
    tunnel_kind.add_argument(
        "--local",
        metavar="SPEC",
        help="local forward, PORT:HOST:PORT or BIND:PORT:HOST:PORT",
    )
    tunnel_kind.add_argument(
        "--remote",
        metavar="SPEC",
        help="remote forward, PORT:HOST:PORT or BIND:PORT:HOST:PORT",
    )
    tunnel_kind.add_argument("--dynamic", metavar="SPEC", help="SOCKS proxy, PORT or BIND:PORT")
    tunnel_add.add_argument("--note", default=None, help="optional reminder of what the forward is for")
    add_json_option(tunnel_add)
    tunnel_edit = tunnel_commands.add_parser("edit", help="change a saved forwarding profile")
    tunnel_edit.add_argument("tunnel", help="profile name, id, or ALIAS:NAME")
    tunnel_edit.add_argument("--name", help="new profile name")
    tunnel_edit.add_argument("--server", help="move the profile to another host")
    tunnel_edit.add_argument("--note")
    tunnel_edit.add_argument(
        "--spec",
        help="new forwarding specification, interpreted with the profile's current kind",
    )
    tunnel_edit.add_argument("--kind", help="change the forwarding kind; needs --spec")
    add_json_option(tunnel_edit)
    tunnel_open = tunnel_commands.add_parser(
        "open",
        help="start a saved forward in the background",
        epilog="background opens are detached from this terminal; "
        "use 'serverctl tunnel close NAME' or --foreground to stop it.",
    )
    tunnel_open.add_argument("tunnel", help="profile name, id, or ALIAS:NAME")
    tunnel_open.add_argument("--timeout", type=int, default=20, help="connection timeout in seconds")
    tunnel_open.add_argument(
        "--foreground",
        action="store_true",
        help="hold the terminal until Ctrl+C instead of detaching",
    )
    add_json_option(tunnel_open)
    tunnel_close = tunnel_commands.add_parser("close", help="stop a running tunnel")
    tunnel_close.add_argument("tunnel", help="profile name, id, or ALIAS:NAME")
    add_json_option(tunnel_close)
    tunnel_status = tunnel_commands.add_parser(
        "status", help="report which saved forwards this machine is running"
    )
    tunnel_status.add_argument("--server", help="only profiles for this host")
    add_json_option(tunnel_status)
    tunnel_remove = tunnel_commands.add_parser(
        "remove", help="forget a forwarding profile, stopping it first if it is open"
    )
    tunnel_remove.add_argument("tunnel", help="profile name, id, or ALIAS:NAME")
    tunnel_remove.add_argument("--yes", action="store_true")
    add_json_option(tunnel_remove)

    export_parser = commands.add_parser(
        "export",
        help="write host metadata to a JSON file for versioning or another machine",
        epilog="secrets, credential identifiers and remote observations are never "
        "exported: a host carries its credential *label*, and the secret stays in "
        "this machine's keychain.",
    )
    export_parser.add_argument(
        "--output", type=Path, help="write to this file (default: standard output)"
    )
    add_json_option(export_parser)

    import_parser = commands.add_parser(
        "import",
        help="read a serverctl export file into this machine's inventory",
        epilog="prints what would change unless --apply is given; existing hosts are "
        "left alone unless --overwrite, and saved directories and tunnels are merged "
        "rather than replaced.",
    )
    import_parser.add_argument(
        "source", nargs="?", default="-", help="export file, or - for standard input"
    )
    import_parser.add_argument("--apply", action="store_true", help="write the changes")
    import_parser.add_argument(
        "--overwrite", action="store_true", help="update hosts that already exist"
    )
    add_json_option(import_parser)

    geo = commands.add_parser(
        "geo",
        help="report or fill host locations from a local database (never online)",
        epilog="lookups read a local file only: SSM_GEOIP_CSV (CIDR ranges) or "
        "SSM_GEOIP_MMDB with the optional geoip2 package. Nothing is sent anywhere, "
        "and reading the inventory never performs a lookup.",
    )
    geo_commands = geo.add_subparsers(dest="geo_command", required=True)
    geo_status = geo_commands.add_parser("status", help="show the configured local backend")
    add_json_option(geo_status)
    geo_resolve = geo_commands.add_parser(
        "resolve", help="look up one or more hosts and optionally store the result"
    )
    geo_resolve.add_argument(
        "servers", nargs="*", metavar="SERVER", help="alias or id"
    )
    geo_resolve.add_argument(
        "--all", action="store_true", help="every host in the inventory"
    )
    geo_resolve.add_argument(
        "--apply",
        action="store_true",
        help="store the result (a manual override is kept unless --overwrite)",
    )
    geo_resolve.add_argument(
        "--overwrite", action="store_true", help="replace an existing manual override"
    )
    add_json_option(geo_resolve)

    mcp = commands.add_parser(
        "mcp",
        help="serve the inventory to MCP-only agents, read-only by default",
        epilog="the Agent Skill is still the recommended path; this wrapper is for "
        "clients that cannot read one. It never reveals a secret.",
    )
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_commands.add_parser(
        "serve", help="speak MCP over stdio (stdout carries protocol messages only)"
    )
    mcp_serve.add_argument(
        "--allow-remote", action="store_true", help="enable test_host, which contacts a host"
    )
    mcp_serve.add_argument(
        "--allow-exec",
        action="store_true",
        help="enable run_command (needs at least one --allow-command)",
    )
    mcp_serve.add_argument(
        "--allow-command",
        action="append",
        default=[],
        metavar="PATTERN",
        help="glob over the command line (or argv[0]) that run_command may execute",
    )
    mcp_serve.add_argument(
        "--http",
        action="store_true",
        help="listen on loopback HTTP instead of stdio, for clients that cannot spawn a process",
    )
    mcp_serve.add_argument(
        "--port", type=int, default=0, help="loopback port for --http (0 chooses a free one)"
    )
    mcp_serve.add_argument(
        "--token-file",
        type=Path,
        help="write the bearer token to this mode-600 file instead of only printing it",
    )
    add_json_option(mcp_serve)
    mcp_tools = mcp_commands.add_parser(
        "tools", help="print the tools this policy would expose"
    )
    mcp_tools.add_argument("--allow-remote", action="store_true")
    mcp_tools.add_argument("--allow-exec", action="store_true")
    mcp_tools.add_argument("--allow-command", action="append", default=[], metavar="PATTERN")
    add_json_option(mcp_tools)

    audit = commands.add_parser(
        "audit",
        help="read the local, append-only record of reveals and remote commands",
        epilog="the log never contains a secret value: a reveal is recorded by "
        "credential label, and credentials inside a command line are masked.",
    )
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    audit_list = audit_commands.add_parser("list", help="show recorded events, newest first")
    audit_list.add_argument("--limit", type=int, default=50, help="how many events (default 50)")
    audit_list.add_argument("--alias", help="only events for this host")
    audit_list.add_argument(
        "--event",
        action="append",
        default=[],
        help="only these event kinds (exec, exec_result, reveal, reveal_denied, connect, tunnel_open, tunnel_close)",
    )
    audit_list.add_argument("--since", help="only events at or after this ISO-8601 timestamp")
    add_json_option(audit_list)
    audit_path_command = audit_commands.add_parser("path", help="print where the log lives")
    add_json_option(audit_path_command)
    audit_rotate = audit_commands.add_parser(
        "rotate",
        help="shift the live log into a rotated segment (also happens automatically)",
        epilog="rotation is automatic once the file passes its size limit; this command "
        "forces it, for example before archiving the segments.",
    )
    audit_rotate.add_argument("--force", action="store_true", help="rotate regardless of size")
    add_json_option(audit_rotate)

    credential = commands.add_parser("credential", help="manage reusable credentials")
    credential_commands = credential.add_subparsers(dest="credential_command", required=True)
    credential_list = credential_commands.add_parser("list")
    add_json_option(credential_list)
    credential_edit_hint = (
        "add only creates and refuses an existing label; "
        "to update a credential use 'serverctl credential edit LABEL' "
        "(--replace-secret for a new password)."
    )
    add_password = credential_commands.add_parser("add-password", epilog=credential_edit_hint)
    add_password.add_argument("label")
    add_json_option(add_password)
    add_key = credential_commands.add_parser("add-key", epilog=credential_edit_hint)
    add_key.add_argument("label")
    add_key.add_argument("--key-path", required=True)
    add_key.add_argument("--store-passphrase", action="store_true")
    add_json_option(add_key)
    add_agent = credential_commands.add_parser("add-agent", epilog=credential_edit_hint)
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

    connect = commands.add_parser(
        "connect",
        help="open an SSH session",
        epilog="--mosh needs the mosh client installed and mosh-server on the host; "
        "it uses the same managed config and host-key verification as connect.",
    )
    connect.add_argument("alias")
    connect.add_argument("--timeout", type=int, default=12)
    connect.add_argument(
        "--mosh",
        action="store_true",
        help="open a mosh session instead of plain ssh (needs a local mosh client)",
    )
    connect.add_argument(
        "--log",
        action="store_true",
        help="record the session to a mode-600 transcript under the data directory",
    )
    connect.add_argument(
        "--log-path",
        type=Path,
        help="record the session to this file (implies --log)",
    )

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


def resolve_credential(service: CredentialApplicationService, value: str | None) -> str | None:
    if not value or value.casefold() == "none":
        return None
    return service.get(value)["id"]


def require_new_credential_label(service: CredentialApplicationService, label: str) -> None:
    """Fail before any hidden prompt when a credential label is taken.

    Entering a password twice only to learn the label exists is a dead end
    for users who actually wanted to *update* the secret — point them at the
    edit command instead.
    """
    try:
        service.get(label)
    except NotFoundError:
        return
    raise ConflictError(
        f"credential label already exists: {label}; "
        f"to update it use 'serverctl credential edit {label}' "
        "(--replace-secret for a new password)"
    )


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


def skill_link_status(dirs: Sequence[Path] | None = None) -> dict[str, Any]:
    """Report agent skill links whose copy of the project is older than this CLI.

    A stale linked copy means agents keep following outdated SKILL.md guidance
    (the exact failure mode behind hand-rolled ssh/sshpass attempts), so doctor
    surfaces it with the update command. Advisory only: ok stays True.
    """
    if dirs is None:
        candidates = [Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills"]
        candidates += [Path(entry).expanduser() for entry in os.environ.get("SSM_SKILLS_DIRS", "").split() if entry]
    else:
        candidates = list(dirs)
    links: list[dict[str, str]] = []
    stale: list[dict[str, str]] = []
    for directory in candidates:
        marker = directory / "ssh-server-manager" / "scripts" / "ssh_server_manager" / "__init__.py"
        try:
            match = re.search(r'__version__\s*=\s*"([^"]+)"', marker.read_text(encoding="utf-8"))
        except OSError:
            continue
        entry = {"path": str(directory / "ssh-server-manager"), "version": match.group(1) if match else "unknown"}
        links.append(entry)
        if entry["version"] != __version__:
            stale.append(entry)
    status: dict[str, Any] = {"ok": True, "linked": len(links)}
    if stale:
        status["stale"] = stale
        status["message"] = (
            f"skill copy at {stale[0]['path']} is v{stale[0]['version']} but serverctl is "
            f"v{__version__}; re-run install.sh to update it, then restart the agent session"
        )
    return status


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
        "agent_skill": skill_link_status(),
        "mount": mount_capability(),
        "mosh": mosh_capability(),
        "geoip": geoip_capability(),
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
    services = ApplicationServices(
        database, runner_factory=lambda: SSHRunner(database),
        vault_factory=lambda: get_vault(), config_renderer=render_config,
    )
    if args.command == "doctor":
        result = run_doctor(database)
        emit(result, as_json=args.json)
        return 0 if result["ok"] else 1
    if args.command in {"web", "ui"}:
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
            launch_fragment=(
                f"servers/{services.hosts.get(args.credential)['alias']}/credential"
                if args.credential
                else None
            ),
        )
        return 0
    if args.command == "server":
        if args.server_command == "list":
            emit(services.hosts.list(), as_json=args.json)
        elif args.server_command == "show":
            emit(services.hosts.get(args.alias), as_json=args.json)
        elif args.server_command == "add":
            credential_id = resolve_credential(services.credentials, args.credential)
            if args.password_credential is not None:
                if not sys.stdin.isatty():
                    raise VaultError(
                        "--password-credential needs a local terminal for the hidden password "
                        "prompt; run this command in your own terminal, or add the server "
                        "without it and enter the password via 'serverctl ui --credential ALIAS'"
                    )
                require_new_credential_label(services.credentials, args.password_credential)
                first = getpass.getpass("Password: ")
                second = getpass.getpass("Confirm password: ")
                if first != second:
                    raise VaultError("password confirmation does not match")
                credential_id = (
                    services.credentials
                    .create_password(args.password_credential, first)["id"]
                )
            try:
                result = services.hosts.create(
                    alias=args.alias,
                    hostname=args.hostname,
                    port=args.port,
                    username=args.username,
                    credential_id=credential_id,
                    proxy_jumps=args.proxy_jump,
                    tags=args.tag,
                    notes=args.notes,
                )
            except ConflictError as exc:
                raise ConflictError(
                    f"{exc}; to update it use 'serverctl server edit {args.alias}'"
                ) from exc
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
                changes["credential_id"] = resolve_credential(services.credentials, args.credential)
            if args.proxy_jump is not None:
                changes["proxy_jumps"] = args.proxy_jump
            if args.clear_tags:
                changes["tags"] = []
            elif args.tag is not None:
                changes["tags"] = args.tag
            result = services.hosts.update(args.alias, **changes)
            emit(result, as_json=args.json)
        elif args.server_command == "remove":
            if not confirm(f"Remove server {args.alias}?", args.yes):
                raise DatabaseError("removal cancelled; use --yes for non-interactive removal")
            from . import tunnels

            # Stop this host's forwards first: deleting the profiles first would
            # leave the ssh processes holding their ports with nothing to close them.
            tunnels.close_for_server(database, args.alias)
            result = services.hosts.delete(args.alias)
            emit(result, as_json=args.json)
        elif args.server_command == "import":
            preview = services.imports.preview(config=args.config)
            result = services.imports.apply(preview, overwrite=args.overwrite) if args.apply else preview
            if args.json:
                emit(result, as_json=True)
            else:
                emit({key: value for key, value in result.items() if key != "warnings"})
                for warning in result.get("warnings", []):
                    print(f"Warning: {warning['alias']}: {warning['message']}")
                    print(f"  Source config: {warning['source_config']}")
                    if warning.get("fallback_config"):
                        print(f"  Authentication fallback: {warning['fallback_config']}")
        elif args.server_command == "test":
            result = services.diagnostics.test(args.alias, timeout=args.timeout)
            emit(result, as_json=args.json)
            return 0 if result["ok"] else 1
        elif args.server_command == "diagnose":
            result = services.diagnostics.diagnose(args.alias, timeout=args.timeout)
            emit(result, as_json=args.json)
            return 0 if result["overall"] != "failed" else 1
        elif args.server_command == "note":
            if args.append and args.text is None:
                raise ValidationError("--append requires --text")
            result = services.hosts.notes(args.alias, args.text or "", mode="append" if args.append else "set")
            emit(result, as_json=args.json)
            return 0
        return 0
    if args.command == "skill":
        service = services.skills
        if args.skill_command == "discover":
            result = service.discover()
        elif args.skill_command == "list":
            result = service.list(server_identifier=args.server)
        elif args.skill_command == "show":
            result = services.skills.get(args.skill)
        elif args.skill_command == "add":
            result = service.register(args.path, server_identifiers=args.server)
        elif args.skill_command == "refresh":
            result = service.refresh(args.skill, path=args.path)
        elif args.skill_command == "attach":
            result = services.skills.attach(args.skill, args.servers)
        elif args.skill_command == "detach":
            result = services.skills.detach(args.skill, args.servers)
        elif args.skill_command == "resolve":
            result = service.resolve(args.servers)
            emit(result, as_json=args.json)
            return 0 if result["ok"] else 1
        elif args.skill_command == "remove":
            if not confirm(f"Unregister Skill {args.skill}?", args.yes):
                raise DatabaseError("removal cancelled; use --yes for non-interactive removal")
            result = service.delete(args.skill)
        else:
            return 2
        emit(result, as_json=args.json)
        return 0
    if args.command == "path":
        if args.path_command == "list":
            result = services.files.list_paths(args.server)
        elif args.path_command == "add":
            result = services.files.create_path(
                args.server, label=args.label, path=args.path, notes=args.note
            )
        elif args.path_command == "edit":
            changes = {
                key: value
                for key, value in (
                    ("label", args.label),
                    ("path", args.path),
                    ("notes", args.note),
                )
                if value is not None
            }
            result = services.files.update_path(args.server, args.directory, **changes)
        elif args.path_command == "remove":
            if not confirm(f"Forget saved directory {args.directory}?", args.yes):
                raise DatabaseError("removal cancelled; use --yes for non-interactive removal")
            result = services.files.delete_path(args.server, args.directory)
        elif args.path_command == "resolve":
            result = services.files.resolve_paths(args.servers)
        else:
            return 2
        emit(result, as_json=args.json)
        return 0
    if args.command in {"cp", "get", "put"} and args.recursive:
        source, destination = args.source, args.destination
        if args.command == "get" and destination is None:
            destination = "."
        direction, alias, remote_path, local_path = resolve_transfer(source, destination)
        runner = services.files
        if direction == "download":
            result = runner.download_tree(
                alias, remote_path, local_path, timeout=args.timeout, stream=not args.json
            )
        else:
            result = runner.upload_tree(
                alias, local_path, remote_path, timeout=args.timeout, stream=not args.json
            )
        emit(result, as_json=args.json)
        return 0
    if args.command in {"cp", "get", "put"}:
        source, destination = args.source, args.destination
        if args.command == "get":
            # get always names the remote side first; default the local side to
            # the working directory so "get web1:/etc/hosts" does the obvious thing.
            if parse_remote_ref(source)[0] is None:
                raise ValidationError("get expects ALIAS:REMOTE as its source")
            destination = destination if destination is not None else "."
        elif args.command == "put" and parse_remote_ref(destination)[0] is None:
            raise ValidationError("put expects ALIAS:REMOTE as its destination")
        direction, alias, remote_path, local_path = resolve_transfer(source, destination)
        runner = services.files
        if direction == "download":
            target = validate_local_destination(
                local_path,
                remote_name=posixpath.basename(remote_path.rstrip("/")),
                force=args.force,
            )
            result = runner.download(
                alias, remote_path, target, timeout=args.timeout, stream=not args.json
            )
        else:
            source_file = validate_local_source(local_path)
            result = runner.upload(
                alias, source_file, remote_path, timeout=args.timeout, stream=not args.json
            )
        emit(result, as_json=args.json)
        return 0
    if args.command == "mount":
        if args.list:
            emit(list_mounts(), as_json=args.json)
            return 0
        if not args.server:
            raise ValidationError("mount expects ALIAS[:REMOTE], or --list")
        alias, remote = parse_remote_ref(args.server)
        if alias is None:
            alias, remote = args.server, None
        emit(
            mount_host(
                database,
                alias,
                remote,
                args.mountpoint,
                read_only=args.read_only,
            ),
            as_json=args.json,
        )
        return 0
    if args.command == "unmount":
        emit(unmount_path(args.target), as_json=args.json)
        return 0
    if args.command == "tunnel":
        from . import tunnels

        if args.tunnel_command == "list":
            emit(database.list_tunnels(args.server), as_json=args.json)
            return 0
        if args.tunnel_command == "show":
            emit(database.get_tunnel(args.tunnel), as_json=args.json)
            return 0
        if args.tunnel_command == "add":
            kind, forward = "local", None
            for candidate in ("local", "remote", "dynamic"):
                value = getattr(args, candidate, None)
                if value is not None:
                    kind, forward = candidate, value
            profile = database.create_tunnel(
                args.server, name=args.name, kind=kind, forward=forward, notes=args.note
            )
            emit_tunnel_line(
                f"saved {tunnel_label(profile)} for {profile['server']}", profile, as_json=args.json
            )
            return 0
        if args.tunnel_command == "edit":
            profile = database.update_tunnel(
                args.tunnel,
                name=args.name,
                kind=args.kind,
                forward=args.spec,
                notes=args.note,
                server_identifier=args.server,
            )
            emit_tunnel_line(
                f"updated {tunnel_label(profile)} for {profile['server']}", profile, as_json=args.json
            )
            return 0
        if args.tunnel_command == "open":
            result = tunnels.open_tunnel(
                database, args.tunnel, timeout=args.timeout, foreground=args.foreground
            )
            if args.foreground:
                return 0
            if result.get("already_open"):
                message = f"{tunnel_label(result)} is already open (pid {result.get('pid')})"
            else:
                message = f"opened {tunnel_label(result)} for {result['server']} (pid {result.get('pid')})"
            emit_tunnel_line(message, result, as_json=args.json)
            return 0
        if args.tunnel_command == "close":
            result = tunnels.close_tunnel(database, args.tunnel)
            if result.get("already_stopped"):
                message = f"{result.get('name')} was not running"
            else:
                message = f"closed {tunnel_label(result)} (pid {result.get('pid')})"
            emit_tunnel_line(message, result, as_json=args.json)
            return 0
        if args.tunnel_command == "status":
            emit(tunnels.summarize(database, server_identifier=args.server), as_json=args.json)
            return 0
        if args.tunnel_command == "remove":
            profile = database.get_tunnel(args.tunnel)
            if not confirm(f"Remove tunnel {profile['name']}?", args.yes):
                raise DatabaseError("removal cancelled; use --yes for non-interactive removal")
            try:
                tunnels.close_tunnel(database, profile["id"])
            except ValidationError:
                pass  # Not running: nothing to stop before forgetting the profile.
            removed = database.delete_tunnel(profile["id"])
            emit_tunnel_line(
                f"removed {tunnel_label(removed)} for {removed['server']}",
                removed,
                as_json=args.json,
            )
            return 0
        return 2
    if args.command == "export":
        from . import portable

        document = portable.export_document(database)
        if args.output is None:
            if args.json:
                emit(document, as_json=True)
            else:
                sys.stdout.write(portable.dump_document(document))
            return 0
        path = portable.write_document(document, args.output)
        emit({"ok": True, "path": str(path), "hosts": len(document["hosts"])}, as_json=args.json)
        return 0
    if args.command == "import":
        from . import portable

        if args.source == "-":
            text = sys.stdin.read()
            if not text.strip():
                raise ValidationError("no export document on standard input")
            try:
                document = portable.load_document_text(text)
            except ValidationError:
                raise
        else:
            document = portable.load_document(args.source)
        preview = portable.preview(document, database)
        if not args.apply:
            if args.json:
                emit(preview, as_json=True)
            else:
                print_import_plan(preview, applied=False)
            return 0 if not preview.get("errors") else 1
        result = portable.apply(document, database, overwrite=args.overwrite)
        if args.json:
            emit(result, as_json=True)
        else:
            print_import_plan(preview, applied=True, result=result)
        return 0 if result["ok"] else 1
    if args.command == "geo":
        from . import geoip

        if args.geo_command == "status":
            emit(geoip.capability(), as_json=args.json)
            return 0
        if args.geo_command == "resolve":
            if args.all:
                targets = [host["alias"] for host in database.list_servers()]
            else:
                targets = list(args.servers)
            if not targets:
                raise ValidationError("name a host, or pass --all")
            rows = []
            for target in targets:
                host = database.get_server(target)
                existing = host.get("geo") or {}
                address = host.get("last_resolved_ip")
                if not address:
                    # Resolving a name is a network call; this command is the
                    # explicit place where that is acceptable.
                    addresses = geoip.resolve_hostname(host["hostname"])
                    address = addresses[0] if addresses else None
                row: dict[str, Any] = {
                    "alias": host["alias"],
                    "address": address,
                    "current": existing or None,
                }
                if address:
                    try:
                        found = geoip.lookup(address)
                    except ValidationError as exc:
                        row["error"] = str(exc)
                        found = None
                    row["lookup"] = found
                    if args.apply and found:
                        try:
                            database.set_server_geo(
                                host["id"], found, overwrite_manual=args.overwrite
                            )
                            row["applied"] = True
                        except ConflictError as exc:
                            row["error"] = str(exc)
                            row["applied"] = False
                else:
                    row["error"] = "hostname did not resolve"
                rows.append(row)
            payload = {"ok": all("error" not in row for row in rows), "hosts": rows}
            if args.json:
                emit(payload, as_json=True)
            else:
                for row in rows:
                    current = row.get("current") or {}
                    found = row.get("lookup") or {}
                    detail = ""
                    if current:
                        detail = f"  current={current.get('country_code')}:{current.get('source')}"
                    if found:
                        detail += f"  lookup={found.get('country_code')} {found.get('city') or ''}".rstrip()
                    if row.get("applied"):
                        detail += "  applied"
                    if row.get("error"):
                        detail += f"  error={row['error']}"
                    print(f"{row['alias']:<22} {row.get('address') or '-':<18}{detail}")
            return 0 if payload["ok"] else 1
        return 2
    if args.command == "mcp":
        from . import mcp_server

        policy = mcp_server.Policy(
            allow_remote=args.allow_remote,
            allow_exec=args.allow_exec,
            allow_commands=args.allow_command,
        )
        if args.mcp_command == "tools":
            server = mcp_server.McpServer(database, policy=policy)
            tools = server.tools()
            if args.json:
                emit({"ok": True, "count": len(tools), "tools": tools}, as_json=True)
            else:
                for tool in tools:
                    print(f"{tool['name']:<22} {tool['description'].splitlines()[0]}")
            return 0
        if args.mcp_command == "serve":
            if args.json:
                # stdout is the protocol channel; a JSON extra would corrupt it.
                raise ValidationError("mcp serve does not take --json; stdout is the protocol channel")
            if policy.allow_exec and not policy.allow_commands:
                raise ValidationError(
                    "--allow-exec without --allow-command enables nothing; pass at least "
                    "one pattern, for example --allow-command 'uptime'"
                )
            if not args.http:
                if args.port or args.token_file:
                    raise ValidationError("--port and --token-file only apply with --http")
                return mcp_server.serve(database, policy=policy)

            import secrets

            token = secrets.token_urlsafe(32)
            if args.token_file:
                path = args.token_file.expanduser()
                descriptor = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(token + "\n")
                destination = f"and written to {path}"
            else:
                destination = "print it once more with --token-file"
            print(
                f"Bearer token: {token}\n"
                f"This listener is loopback-only and refuses browser origins. "
                f"Reading the token from this output is a disclosure: pass it directly, "
                f"{destination}.",
                file=sys.stderr,
            )

            def announce(url: str) -> None:
                print(f"MCP endpoint: {url}", file=sys.stderr)

            return mcp_server.serve_http(
                database, policy=policy, token=token, port=args.port, on_ready=announce
            )
        return 2
    if args.command == "audit":
        from . import audit as audit_module

        if args.audit_command == "path":
            emit({"path": str(audit_module.audit_path())}, as_json=args.json)
            return 0
        if args.audit_command == "rotate":
            result = audit_module.rotate(force=args.force)
            result["segments_on_disk"] = [
                str(path) for path in audit_module.log_paths() if path.exists()
            ]
            emit(result, as_json=args.json)
            return 0
        if args.audit_command == "list":
            events = audit_module.read_events(
                limit=args.limit,
                alias=args.alias,
                events=args.event or None,
                since=args.since,
            )
            payload = {
                "ok": True,
                "path": str(audit_module.audit_path()),
                "segments": [str(path) for path in audit_module.log_paths() if path.exists()],
                "count": len(events),
                "events": events,
            }
            if args.json:
                emit(payload, as_json=True)
            else:
                for entry in events:
                    print(format_audit_event(entry))
                if not events:
                    print(f"no audit events at {payload['path']}")
            return 0
        return 2
    if args.command == "credential":
        if args.credential_command == "list":
            emit(services.credentials.list(), as_json=args.json)
            return 0
        service = services.credentials
        if args.credential_command == "add-password":
            require_new_credential_label(services.credentials, args.label)
            first = getpass.getpass("Password: ")
            second = getpass.getpass("Confirm password: ")
            if first != second:
                raise VaultError("password confirmation does not match")
            result = service.create_password(args.label, first)
        elif args.credential_command == "add-key":
            require_new_credential_label(services.credentials, args.label)
            passphrase = getpass.getpass("Private-key passphrase: ") if args.store_passphrase else None
            result = service.create_key(args.label, args.key_path, passphrase or None)
        elif args.credential_command == "add-agent":
            require_new_credential_label(services.credentials, args.label)
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
        emit(result, as_json=args.json)
        return 0
    if args.command == "connect":
        # Flag contradictions are reported before the TTY check: an agent that
        # passes both gets an answer about the flags, not about its terminal.
        if args.mosh and (args.log or args.log_path):
            raise ValidationError(
                "--log cannot be combined with --mosh; record the separate terminal instead"
            )
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise SSHError(
                "connect opens an interactive shell and requires a terminal (TTY); "
                "run this command directly in your own terminal, or use "
                "'serverctl exec ALIAS -- COMMAND' to run commands non-interactively"
            )
        if args.mosh:
            from . import mosh

            return mosh.connect(database, args.alias, timeout=args.timeout)
        if args.log or args.log_path:
            from . import session_log

            session_log.require_support()
            target = args.log_path
            print(
                f"Recording this session to {target or 'a new file under ' + str(session_log.sessions_dir())}.\n"
                "Everything the host displays — including the commands you type — is saved. "
                "Redaction masks credential-shaped values but is best effort.\n"
                "Passwords typed at a no-echo prompt are not recorded.",
                file=sys.stderr,
            )
            result = session_log.record(database, args.alias, log_path=target, timeout=args.timeout)
            print(f"session recorded to {result['path']} (exit {result['exit_code']})", file=sys.stderr)
            return int(result["exit_code"])
        return services.connections.connect(args.alias, timeout=args.timeout)
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
        result = services.connections.execute(
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
        path = services.render_config()
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
