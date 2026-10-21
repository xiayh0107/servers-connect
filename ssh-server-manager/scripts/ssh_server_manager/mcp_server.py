"""A thin MCP server for agents that only speak MCP.

The Agent Skill remains the recommended path: it is a document an agent reads, it
costs one tool call, and it can explain *why*. This wrapper exists for clients
that cannot read a Skill, so it is deliberately the smallest surface that is
still useful, and it is bounded by three rules:

* **Read-only by default.** Listing hosts, routes, saved directories and tunnel
  profiles never connects to anything. Reaching a host (`test_host`) needs
  `--allow-remote`; running a command needs `--allow-exec` *and* a matching
  `--allow-command` pattern.
* **No secret-bearing results.** There is no reveal tool, and every result is
  built from local metadata, so a tool call cannot hand a vault value to a model.
* **Every acting call is audited** under actor `mcp`, so a human can see what an
  agent did through this interface exactly as they can for the CLI.

Transport is the MCP stdio binding: one JSON-RPC 2.0 message per line on stdin,
one per line on stdout, logs on stderr only.
"""

from __future__ import annotations

import fnmatch
import json
import sys
from typing import Any, Callable, Iterable, TextIO

from . import __version__, audit
from .application import ApplicationServices
from .db import Database, DatabaseError
from .paths import managed_ssh_config_path
from .validation import ValidationError

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "ssh-server-manager"

MAX_TEXT_RESULT_BYTES = 256 * 1024


class ToolError(Exception):
    """A tool call failed; reported to the model, not to stderr."""


# --- tool definitions -------------------------------------------------------------


READ_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_hosts",
        "description": (
            "List managed SSH hosts from local metadata: alias, hostname, port, "
            "username, tags, notes, ProxyJump hops and the last check result. "
            "Contacts no host and reads no secret."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "case-insensitive filter"},
                "tag": {"type": "string", "description": "only hosts with this tag"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "host_route",
        "description": (
            "Resolve the ProxyJump hops a connection to one host would use, from "
            "saved metadata only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"host": {"type": "string", "description": "alias or id"}},
            "required": ["host"],
            "additionalProperties": False,
        },
    },
    {
        "name": "saved_paths",
        "description": (
            "Resolve the working directories the user saved for one or more hosts. "
            "These are metadata the user wrote, not proof a path exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hosts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "aliases or ids",
                }
            },
            "required": ["hosts"],
            "additionalProperties": False,
        },
    },
    {
        "name": "host_skills",
        "description": (
            "Resolve which local Agent Skills are bound to one or more hosts. "
            "Returns registry metadata and never a skill body."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hosts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "aliases or ids",
                }
            },
            "required": ["hosts"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_tunnels",
        "description": (
            "List saved port-forwarding profiles and whether this machine is "
            "currently running each one. Contacts no host."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "only profiles for this host"}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "runtime_capabilities",
        "description": (
            "Report which tools and capabilities this machine offers (mount, mosh, "
            "session logging) and whether remote and exec tools are enabled."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

REMOTE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "test_host",
        "description": (
            "Run the SSH connectivity and remote-system check for one host. This "
            "contacts the host; it cannot reveal a secret."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "alias or id"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 60},
            },
            "required": ["host"],
            "additionalProperties": False,
        },
    },
]

EXEC_TOOL: dict[str, Any] = {
    "name": "run_command",
    "description": (
        "Run one command on a host with argument-per-item quoting (no local "
        "shell). Disabled unless the server was started with --allow-exec and the "
        "command matches an allowed pattern."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "alias or id"},
            "command": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "executable and arguments, not a shell string",
            },
            "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
        },
        "required": ["host", "command"],
        "additionalProperties": False,
    },
}


class Policy:
    """What this server instance is allowed to do."""

    def __init__(
        self,
        *,
        allow_remote: bool = False,
        allow_exec: bool = False,
        allow_commands: Iterable[str] = (),
    ) -> None:
        self.allow_remote = allow_remote
        self.allow_exec = allow_exec
        self.allow_commands = tuple(pattern for pattern in allow_commands if pattern)

    def command_allowed(self, command: list[str]) -> bool:
        if not self.allow_exec or not self.allow_commands or not command:
            return False
        text = " ".join(command)
        return any(
            fnmatch.fnmatchcase(text, pattern) or fnmatch.fnmatchcase(command[0], pattern)
            for pattern in self.allow_commands
        )


class McpServer:
    def __init__(
        self,
        database: Database,
        *,
        policy: Policy | None = None,
        services: ApplicationServices | None = None,
    ) -> None:
        self.database = database
        self.policy = policy or Policy()
        self.services = services or ApplicationServices(database)
        self.initialized = False

    # --- protocol ------------------------------------------------------------------

    def tools(self) -> list[dict[str, Any]]:
        available = list(READ_TOOLS)
        if self.policy.allow_remote:
            available.extend(REMOTE_TOOLS)
        if self.policy.allow_exec:
            available.append(EXEC_TOOL)
        return available

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one JSON-RPC message; returns a response or None."""
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message
        if not isinstance(method, str):
            if is_notification:
                return None
            return _error(request_id, -32600, "invalid request: method must be a string")
        try:
            if method == "initialize":
                params = message.get("params")
                result = self._initialize(params if isinstance(params, dict) else {})
            elif method == "notifications/initialized":
                self.initialized = True
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                result = self._call_tool(message.get("params"))
            else:
                if is_notification:
                    return None
                return _error(request_id, -32601, f"method not found: {method}")
        except ToolError as exc:
            if is_notification:
                return None
            return _error(request_id, -32602, str(exc))
        except (ValidationError, DatabaseError) as exc:
            if is_notification:
                return None
            return _error(request_id, -32602, str(exc))
        except Exception as exc:  # noqa: BLE001 - a server must not die on one call
            print(f"mcp: internal error in {method}: {exc}", file=sys.stderr)
            if is_notification:
                return None
            return _error(request_id, -32603, "internal error")
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "version": __version__,
                "title": "SSH Server Manager",
            },
            # Nothing here is a secret; state plainly what the server will not do.
            "instructions": (
                "Local SSH inventory for this machine. Tools are read-only unless the "
                "server was started with --allow-remote or --allow-exec. There is no "
                "way to reveal a stored secret through this server."
            ),
        }

    def _call_tool(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ToolError("tools/call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        # An empty list is falsy but is not "no arguments"; treating it as such
        # would quietly accept a malformed request.
        if not isinstance(arguments, dict):
            raise ToolError("tool arguments must be an object")
        handler = self._handlers().get(str(name))
        if handler is None:
            raise ToolError(f"unknown or disabled tool: {name}")
        payload = handler(arguments)
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if len(text.encode("utf-8")) > MAX_TEXT_RESULT_BYTES:
            text = text.encode("utf-8")[:MAX_TEXT_RESULT_BYTES].decode("utf-8", "ignore")
            text += "\n… truncated"
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "list_hosts": self._list_hosts,
            "host_route": self._host_route,
            "saved_paths": self._saved_paths,
            "host_skills": self._host_skills,
            "list_tunnels": self._list_tunnels,
            "runtime_capabilities": self._runtime_capabilities,
        }
        if self.policy.allow_remote:
            handlers["test_host"] = self._test_host
        if self.policy.allow_exec:
            handlers["run_command"] = self._run_command
        return handlers

    # --- read-only tools -----------------------------------------------------------

    @staticmethod
    def _host_summary(host: dict[str, Any]) -> dict[str, Any]:
        """Only what an agent needs to choose a host; never credential material."""
        return {
            "alias": host["alias"],
            "hostname": host["hostname"],
            "port": host["port"],
            "username": host["username"],
            "tags": host.get("tags", []),
            "notes": host.get("notes", ""),
            "proxy_jumps": host.get("proxy_jumps", []),
            "credential": host.get("credential_label"),
            "last_test_status": host.get("last_test_status"),
            "last_test_at": host.get("last_test_at"),
            "last_success_at": host.get("last_success_at"),
            "facts": host.get("facts"),
            "geo": host.get("geo"),
        }

    def _list_hosts(self, arguments: dict[str, Any]) -> Any:
        hosts = self.services.hosts.list(
            query=_string_or_none(arguments.get("query")),
            tag=_string_or_none(arguments.get("tag")),
        )
        return {"ok": True, "count": len(hosts), "hosts": [self._host_summary(h) for h in hosts]}

    def _host_route(self, arguments: dict[str, Any]) -> Any:
        return self.services.hosts.route(_required_host(arguments))

    def _saved_paths(self, arguments: dict[str, Any]) -> Any:
        return self.services.files.resolve_paths(_required_hosts(arguments))

    def _host_skills(self, arguments: dict[str, Any]) -> Any:
        return self.services.skills.resolve(_required_hosts(arguments))

    def _list_tunnels(self, arguments: dict[str, Any]) -> Any:
        from . import tunnels

        rows = tunnels.summarize(
            self.database, server_identifier=_string_or_none(arguments.get("host"))
        )
        return {"ok": True, "count": len(rows), "tunnels": rows}

    def _runtime_capabilities(self, _arguments: dict[str, Any]) -> Any:
        from .mosh import mosh_capability
        from .mounts import mount_capability

        return {
            "ok": True,
            "version": __version__,
            "database": str(self.database.path),
            "managed_config": str(managed_ssh_config_path()),
            "mount": mount_capability(),
            "mosh": mosh_capability(),
            "remote_tools": self.policy.allow_remote,
            "exec_tools": self.policy.allow_exec,
            "allowed_commands": list(self.policy.allow_commands),
        }

    # --- acting tools ---------------------------------------------------------------

    def _test_host(self, arguments: dict[str, Any]) -> Any:
        host = _required_host(arguments)
        timeout = _bounded_int(arguments.get("timeout"), default=10, maximum=60)
        audit.record("mcp_test", actor="mcp", host=host)
        result = self.services.diagnostics.test(host, timeout=timeout)
        # A check result describes reachability, never credentials.
        return {
            "ok": bool(result.get("ok")),
            "alias": result.get("alias"),
            "latency_ms": result.get("latency_ms"),
            "error_code": result.get("error_code"),
            "message": result.get("message"),
        }

    def _run_command(self, arguments: dict[str, Any]) -> Any:
        host = _required_host(arguments)
        command = arguments.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) for item in command
        ):
            raise ToolError("command must be a non-empty array of strings")
        if not self.policy.command_allowed(command):
            raise ToolError(
                "this command is not on the allowlist; start the server with "
                "--allow-command PATTERN covering it"
            )
        timeout = _bounded_int(arguments.get("timeout"), default=30, maximum=600)
        result = self.services.connections.execute(
            host, command, timeout=timeout, capture=True, actor="mcp"
        )
        return {
            "ok": bool(result.get("ok")),
            "alias": result.get("alias"),
            "returncode": result.get("returncode"),
            "latency_ms": result.get("latency_ms"),
            "stdout": result.get("stdout"),
            "stderr": result.get("stderr"),
            "error_code": result.get("error_code"),
        }


# --- helpers -----------------------------------------------------------------------


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError("expected a string")
    return value or None


def _required_host(arguments: dict[str, Any]) -> str:
    value = arguments.get("host")
    if not isinstance(value, str) or not value.strip():
        raise ToolError("'host' is required")
    return value.strip()


def _required_hosts(arguments: dict[str, Any]) -> list[str]:
    value = arguments.get("hosts")
    if not isinstance(value, list) or not value:
        raise ToolError("'hosts' must be a non-empty array of aliases")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ToolError("each entry of 'hosts' must be a non-empty string")
    return [item.strip() for item in value]


def _bounded_int(value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolError("timeout must be an integer")
    if value < 1:
        raise ToolError("timeout must be positive")
    return min(value, maximum)


# --- transport ---------------------------------------------------------------------


def serve(
    database: Database,
    *,
    policy: Policy | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run the stdio transport until stdin closes."""
    reader = stdin if stdin is not None else sys.stdin
    writer = stdout if stdout is not None else sys.stdout
    server = McpServer(database, policy=policy)
    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = _error(None, -32700, "parse error")
            writer.write(json.dumps(response, ensure_ascii=False) + "\n")
            writer.flush()
            continue
        if not isinstance(message, dict):
            response = _error(None, -32600, "invalid request")
            writer.write(json.dumps(response, ensure_ascii=False) + "\n")
            writer.flush()
            continue
        response = server.handle_message(message)
        if response is not None:
            writer.write(json.dumps(response, ensure_ascii=False) + "\n")
            writer.flush()
    return 0
