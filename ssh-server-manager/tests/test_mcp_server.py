"""The optional MCP wrapper.

Two properties carry the risk here and both are tested explicitly: nothing
read-only may contact a host, and no tool may hand back credential material.
Everything else is protocol shape — handshake, tool listing, and the policy that
decides which tools exist at all.
"""

from __future__ import annotations

import io
import json

import pytest

from ssh_server_manager import mcp_server
from ssh_server_manager.db import Database


@pytest.fixture()
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    database = Database(tmp_path / "manager.db")
    database.create_server(
        alias="web1",
        hostname="web1.example",
        port=2222,
        username="deploy",
        tags=["prod"],
        notes="primary",
    )
    database.create_server(
        alias="db1", hostname="db1.example", username="alice", proxy_jumps=["web1"]
    )
    database.create_server_path("web1", label="app", path="/srv/app")
    return database


def call(server, method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    return server.handle_message(message)


def tool_result(response):
    return json.loads(response["result"]["content"][0]["text"])


# --- handshake and listing ---------------------------------------------------------


def test_initialize_reports_the_server_and_a_version_the_client_asked_for(database):
    server = mcp_server.McpServer(database)
    response = call(server, "initialize", {"protocolVersion": "2024-11-05"})
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["serverInfo"]["name"] == "ssh-server-manager"
    assert response["result"]["capabilities"]["tools"] == {"listChanged": False}
    # The instructions say plainly what this server will not do.
    assert "no way to reveal a stored secret" in response["result"]["instructions"].lower()


def test_initialize_falls_back_to_the_current_version_when_unknown(database):
    server = mcp_server.McpServer(database)
    response = call(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert response["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_a_notification_gets_no_response(database):
    server = mcp_server.McpServer(database)
    assert call(server, "notifications/initialized", request_id=None) is None


def test_read_only_is_the_default_tool_set(database):
    server = mcp_server.McpServer(database)
    names = {tool["name"] for tool in call(server, "tools/list")["result"]["tools"]}
    assert names == {tool["name"] for tool in mcp_server.READ_TOOLS}
    assert "test_host" not in names
    assert "run_command" not in names


def test_policy_adds_the_acting_tools(database):
    server = mcp_server.McpServer(
        database, policy=mcp_server.Policy(allow_remote=True, allow_exec=True, allow_commands=["uptime"])
    )
    names = {tool["name"] for tool in call(server, "tools/list")["result"]["tools"]}
    assert {"test_host", "run_command"} <= names


def test_unknown_tool_and_unknown_method_are_errors(database):
    server = mcp_server.McpServer(database)
    assert call(server, "tools/call", {"name": "reveal", "arguments": {}})["error"]["code"] == -32602
    assert call(server, "resources/list")["error"]["code"] == -32601
    assert call(server, "ping")["result"] == {}


def test_malformed_requests_are_rejected_without_killing_the_server(database):
    server = mcp_server.McpServer(database)
    assert server.handle_message({"jsonrpc": "2.0", "id": 5})["error"]["code"] == -32600
    assert call(server, "tools/call", {"name": "list_hosts", "arguments": []})["error"]["code"] == -32602


# --- read tools -------------------------------------------------------------------


def test_list_hosts_reads_local_metadata_only(database):
    server = mcp_server.McpServer(database)
    payload = tool_result(call(server, "tools/call", {"name": "list_hosts", "arguments": {}}))
    assert payload["count"] == 2
    web1 = next(host for host in payload["hosts"] if host["alias"] == "web1")
    assert web1["hostname"] == "web1.example"
    assert web1["tags"] == ["prod"]
    # No credential material of any kind crosses this boundary.
    assert "credential_id" not in web1
    assert "has_secret" not in json.dumps(payload)


def test_list_hosts_filters(database):
    server = mcp_server.McpServer(database)
    payload = tool_result(
        call(server, "tools/call", {"name": "list_hosts", "arguments": {"tag": "prod"}})
    )
    assert [host["alias"] for host in payload["hosts"]] == ["web1"]


def test_host_route_explains_the_jump(database):
    server = mcp_server.McpServer(database)
    payload = tool_result(
        call(server, "tools/call", {"name": "host_route", "arguments": {"host": "db1"}})
    )
    assert [hop["alias"] for hop in payload["hops"]] == ["web1", "db1"]


def test_saved_paths_and_skills_need_a_host_list(database):
    server = mcp_server.McpServer(database)
    payload = tool_result(
        call(server, "tools/call", {"name": "saved_paths", "arguments": {"hosts": ["web1"]}})
    )
    assert payload["ok"] is True
    assert any(entry["path"] == "/srv/app" for entry in payload["paths"])

    error = call(server, "tools/call", {"name": "saved_paths", "arguments": {"hosts": []}})
    assert "non-empty array" in error["error"]["message"]


def test_list_tunnels_reports_running_state(database):
    database.create_tunnel("web1", name="web1-local", kind="local", forward="8080:localhost:80")
    server = mcp_server.McpServer(database)
    payload = tool_result(call(server, "tools/call", {"name": "list_tunnels", "arguments": {}}))
    assert payload["tunnels"][0]["name"] == "web1-local"
    assert payload["tunnels"][0]["running"] is False


def test_runtime_capabilities_reports_the_policy(database):
    server = mcp_server.McpServer(database, policy=mcp_server.Policy(allow_remote=True))
    payload = tool_result(
        call(server, "tools/call", {"name": "runtime_capabilities", "arguments": {}})
    )
    assert payload["remote_tools"] is True
    assert payload["exec_tools"] is False
    assert "mount" in payload and "mosh" in payload


# --- acting tools -----------------------------------------------------------------


def test_test_host_is_not_callable_until_enabled(database):
    server = mcp_server.McpServer(database)
    assert call(server, "tools/call", {"name": "test_host", "arguments": {"host": "web1"}})["error"][
        "message"
    ].startswith("unknown or disabled tool")


def test_run_command_requires_an_allowlist_match(database):
    server = mcp_server.McpServer(
        database, policy=mcp_server.Policy(allow_exec=True, allow_commands=["uptime*"])
    )
    denied = call(
        server,
        "tools/call",
        {"name": "run_command", "arguments": {"host": "web1", "command": ["rm", "-rf", "/"]}},
    )
    assert "not on the allowlist" in denied["error"]["message"]


def test_an_allowlist_can_cover_an_executable_or_a_whole_line(database, monkeypatch):
    policy = mcp_server.Policy(allow_exec=True, allow_commands=["uptime", "systemctl status *"])
    assert policy.command_allowed(["uptime"]) is True
    assert policy.command_allowed(["systemctl", "status", "nginx"]) is True
    assert policy.command_allowed(["systemctl", "restart", "nginx"]) is False
    # Without --allow-exec nothing is allowed, whatever the patterns say.
    assert mcp_server.Policy(allow_commands=["uptime"]).command_allowed(["uptime"]) is False
    assert mcp_server.Policy(allow_exec=True).command_allowed(["uptime"]) is False


def test_an_allowed_command_runs_and_is_audited(database, monkeypatch):
    server = mcp_server.McpServer(
        database, policy=mcp_server.Policy(allow_exec=True, allow_commands=["uptime"])
    )
    observed = {}

    class FakeRunner:
        def execute(self, identifier, command, **options):
            observed["identifier"] = identifier
            observed["command"] = command
            return {"ok": True, "alias": "web1", "returncode": 0, "stdout": "up\n", "stderr": ""}

    server.services.connections.runner = lambda: FakeRunner()
    payload = tool_result(
        call(
            server,
            "tools/call",
            {"name": "run_command", "arguments": {"host": "web1", "command": ["uptime"]}},
        )
    )
    assert payload["ok"] is True
    assert observed["command"] == ["uptime"]
    # Acting calls are attributed to the MCP interface in the audit log.
    from ssh_server_manager import audit

    events = audit.read_events(events=["exec"])
    assert events and events[0]["actor"] == "mcp"


def test_the_secret_value_never_appears_in_a_result(database):
    database.create_credential(label="root password", kind="agent")
    server = mcp_server.McpServer(
        database, policy=mcp_server.Policy(allow_remote=True, allow_exec=True, allow_commands=["*"])
    )
    listing = call(server, "tools/call", {"name": "list_hosts", "arguments": {}})
    text = json.dumps(listing)
    assert "reveal" not in json.dumps([tool["name"] for tool in server.tools()])
    assert "value" not in text


# --- transport --------------------------------------------------------------------


def test_serve_reads_line_delimited_messages_and_writes_one_response_each(database):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(item) for item in requests) + "\n")
    stdout = io.StringIO()
    assert mcp_server.serve(database, stdin=stdin, stdout=stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [item["id"] for item in lines] == [1, 2, 3]
    assert all(item["jsonrpc"] == "2.0" for item in lines)


def test_serve_reports_a_parse_error_and_keeps_going(database):
    stdin = io.StringIO("not json\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n")
    stdout = io.StringIO()
    mcp_server.serve(database, stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["id"] == 9


def test_oversized_results_are_truncated(database, monkeypatch):
    monkeypatch.setattr(mcp_server, "MAX_TEXT_RESULT_BYTES", 120)
    server = mcp_server.McpServer(database)
    response = call(server, "tools/call", {"name": "list_hosts", "arguments": {}})
    assert response["result"]["content"][0]["text"].endswith("… truncated")
