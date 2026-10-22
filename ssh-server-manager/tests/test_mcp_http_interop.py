"""End-to-end interoperability: a real MCP client over the HTTP transport.

The unit tests in ``test_mcp_server.py`` drive the transport; this file behaves
like an actual MCP client would, in order, and asserts the conversation a client
depends on: negotiate a protocol version, complete the initialized handshake,
enumerate tools, call a read tool, then confirm a tool outside the policy is
refused. If any of that drifts, a client breaks silently — which is the failure
mode worth a test.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from ssh_server_manager import mcp_server
from ssh_server_manager.db import Database


TOKEN = "interop-fixture-token"


class McpHttpClient:
    """Minimal MCP-over-HTTP client: JSON-RPC in, JSON-RPC out."""

    def __init__(self, server, token: str = TOKEN):
        host, port = server.server_address[0], server.server_address[1]
        self.url = f"http://{host}:{port}/mcp"
        self.token = token
        self.next_id = 0

    def send(self, method: str, params: dict | None = None, *, notification: bool = False):
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            self.next_id += 1
            payload["id"] = self.next_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
        if notification:
            return None
        return json.loads(raw)

    def call(self, name: str, arguments: dict | None = None):
        return self.send("tools/call", {"name": name, "arguments": arguments or {}})


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    database = Database(tmp_path / "manager.db")
    database.create_server(alias="box", hostname="box.example", username="alice")
    return database


@pytest.fixture
def live_server(database):
    server = mcp_server.make_http_server(database, token=TOKEN, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_client_can_complete_the_whole_conversation(live_server):
    """initialize -> initialized -> tools/list -> tools/call, in that order."""
    client = McpHttpClient(live_server)

    handshake = client.send(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "interop-test", "version": "1.0.0"},
        },
    )
    result = handshake["result"]
    # The server must speak the version the client asked for rather than a
    # surprise downgrade, and must advertise the capability it implements.
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == mcp_server.SERVER_NAME
    assert result["serverInfo"]["version"]
    # Instructions are part of the contract: they tell the model what is refused.
    assert "no way to reveal a stored secret" in result["instructions"]

    assert client.send("notifications/initialized", notification=True) is None

    listing = client.send("tools/list")["result"]["tools"]
    names = {tool["name"] for tool in listing}
    assert {"list_hosts", "host_route", "saved_paths", "host_skills", "list_tunnels", "runtime_capabilities"} <= names
    for tool in listing:
        assert tool["description"], tool["name"]
        assert tool["inputSchema"]["type"] == "object"
    # Every declared tool must be callable, so nothing is advertised that the
    # dispatcher does not know.
    declared = {tool["name"] for tool in mcp_server.READ_TOOLS}
    for name in names:
        assert name in declared, f"{name} is advertised but not dispatched"

    inventory = client.call("list_hosts")["result"]
    assert inventory["isError"] is False
    payload = json.loads(inventory["content"][0]["text"])
    assert payload["ok"] is True
    assert [row["alias"] for row in payload["hosts"]] == ["box"]


def test_the_default_policy_refuses_acting_tools(live_server):
    """A read-only server must not act, and must say so rather than error out."""
    client = McpHttpClient(live_server)
    client.send("initialize", {"protocolVersion": "2025-06-18"})
    client.send("notifications/initialized", notification=True)

    for name in ("test_host", "run_command"):
        response = client.call(name, {"host": "box"} if name == "test_host" else {"host": "box", "command": ["uptime"]})
        assert "error" in response, name
        assert "unknown or disabled tool" in response["error"]["message"]


def test_an_unknown_client_version_still_gets_a_usable_server(live_server):
    """Version drift should degrade to the server's version, not fail."""
    client = McpHttpClient(live_server)
    result = client.send("initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert client.send("tools/list")["result"]["tools"]


def test_the_server_survives_a_malformed_request(live_server):
    """One bad message must not take the listener down for the next call."""
    request = urllib.request.Request(
        f"{client_url(live_server)}",
        data=b"{not json",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read())
    assert body["error"]["code"] == -32700

    # Still healthy afterwards.
    client = McpHttpClient(live_server)
    assert client.send("ping")["result"] == {}


def test_a_revoked_token_stops_working(live_server):
    """Tokens are per launch; a wrong one gets nothing back."""
    client = McpHttpClient(live_server, token="not-the-token")
    with pytest.raises(urllib.error.HTTPError) as caught:
        client.send("tools/list")
    assert caught.value.code == 401


def client_url(server) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}/mcp"


def test_the_official_sdk_client_can_drive_the_server(live_server):
    """The check that matters most: a real MCP client, not our own test client.

    The official SDK is an optional development dependency, so this skips when it
    is absent rather than becoming a hard requirement of the suite. When it is
    present it exercises the real Streamable HTTP transport, which is stricter
    about framing than the hand-written client above.
    """
    mcp = pytest.importorskip("mcp", reason="official MCP SDK is not installed")
    httpx2 = pytest.importorskip("httpx2", reason="the SDK's HTTP client is not installed")
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = client_url(live_server)

    async def conversation():
        client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=20)
        try:
            async with streamable_http_client(url, http_client=client, terminate_on_close=False) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    info = getattr(init, "server_info", None) or getattr(init, "serverInfo")
                    assert info.name == mcp_server.SERVER_NAME
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert "list_hosts" in names
                    result = await session.call_tool("list_hosts", {})
                    payload = json.loads(result.content[0].text)
                    assert payload["ok"] is True
                    assert [row["alias"] for row in payload["hosts"]] == ["box"]
                    # The default policy must hold through a real client too.
                    with pytest.raises(Exception):
                        await session.call_tool("run_command", {"host": "box", "command": ["uptime"]})
        finally:
            await client.aclose()

    asyncio.run(conversation())
