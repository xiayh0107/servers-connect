"""Fixed local helpers for credential probes; never run a remote command.

The manifest contains routing metadata only. The request capability stays in
the child environment, and vault values travel only over local IPC to AskPass.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


def _request(payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = os.environ["SSM_CREDENTIAL_PROBE_ENDPOINT"]
    payload["token"] = os.environ["SSM_CREDENTIAL_PROBE_TOKEN"]
    host, port = endpoint.rsplit(":", 1)
    if host != "127.0.0.1":
        raise ValueError("invalid probe endpoint")
    with socket.create_connection((host, int(port)), timeout=2) as connection:
        connection.settimeout(2)
        connection.sendall(json.dumps(payload).encode() + b"\n")
        with connection.makefile("rb") as response:
            data = response.readline(65537)
        if len(data) > 65536:
            raise ValueError("invalid probe reply")
        return json.loads(data)


def probe_askpass(prompt: str) -> int:
    # The broker decides the permitted slot from the request snapshot, never
    # from a credential id supplied by a remote server or by an AskPass prompt.
    from .askpass import choose_descriptor

    try:
        descriptors = json.loads(os.environ.get("SSM_ASKPASS_MAP", "[]"))
        descriptor = choose_descriptor(prompt, descriptors)
        if not descriptor:
            return 1
        result = _request({"op": "secret", "stage": int(os.environ["SSM_CREDENTIAL_PROBE_STAGE"]),
                           "slot": descriptor["slot"]})
        secret = result.get("secret")
        if not result.get("ok") or not isinstance(secret, str) or not secret:
            return 1
        sys.stdout.write(secret)
        return 0
    except Exception:
        # Neither prompts nor vault/backend error text may reach diagnostics.
        return 1


def main() -> int:
    try:
        if sys.argv[1] == "askpass":
            return probe_askpass(sys.argv[2] if len(sys.argv) > 2 else "")
        manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        stage = int(sys.argv[3])
        if sys.argv[1] == "callback":
            return 0 if _request({"op": "authenticated", "stage": stage}).get("ok") else 1
        if sys.argv[1] != "proxy" or not 0 <= stage < len(manifest["hops"]):
            return 1
        hop = manifest["hops"][stage]
        environment = os.environ.copy()
        environment["SSM_CREDENTIAL_PROBE_STAGE"] = str(stage)
        environment["SSM_ASKPASS_MAP"] = json.dumps(hop["descriptors"], separators=(",", ":"))
        command = [manifest["ssh_binary"], "-F", manifest["config"], "-E", hop["log"],
                   "-T", "-W", sys.argv[4], hop["probe_alias"]]
        # POSIX exec preserves the request process group; Windows child
        # processes inherit the request's kill-on-close job.
        if os.name != "nt":
            os.execvpe(command[0], command, environment)
        return subprocess.call(command, env=environment)
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
