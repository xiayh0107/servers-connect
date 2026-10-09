#!/usr/bin/env python3
"""Export the API v1 contract without reading user data, vaults or SSH config."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ssh_server_manager.db import Database
from ssh_server_manager.webapp import create_app


def export_schema() -> dict:
    with tempfile.TemporaryDirectory(prefix="ssm-openapi-") as directory:
        app = create_app(Database(Path(directory) / "manager.db"), launch_token="schema-only", port=8765)
        schema = app.openapi()
    schema["info"]["title"] = "SSH Server Manager Local API"
    schema["info"]["version"] = "1"
    schema["info"]["description"] = (
        "Loopback-only API. Exchange the private single-use launch URL for an HttpOnly session cookie, "
        "then read /api/v1/runtime for the session CSRF token. Every mutation and transfer requires "
        "the X-CSRF-Token header. Secret reveal additionally requires credential-specific reauthentication."
    )
    schema["paths"] = {path: operations for path, operations in schema["paths"].items() if path.startswith("/api/v1/")}
    # OpenAPI's list of security objects means OR; cookie + CSRF are required
    # together, even though FastAPI discovers the two dependencies separately.
    for operations in schema["paths"].values():
        for method, operation in operations.items():
            required = {"APIKeyCookie": []}
            if method not in {"get", "head", "options"}:
                required["APIKeyHeader"] = []
            operation["security"] = [required]
    # Drop legacy-only components so downstream clients see only the v1 DTOs.
    components = schema["components"]["schemas"]
    referenced: set[str] = set()

    def visit(value):
        if isinstance(value, dict):
            reference = value.get("$ref", "")
            if reference.startswith("#/components/schemas/"):
                name = reference.rsplit("/", 1)[1]
                if name not in referenced:
                    referenced.add(name)
                    visit(components[name])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema["paths"])
    schema["components"]["schemas"] = {name: value for name, value in components.items() if name in referenced}
    return schema


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", type=Path, help="fail when a saved contract differs from the current schema")
    args = parser.parse_args()
    data = json.dumps(export_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.check:
        if not args.check.exists() or args.check.read_text(encoding="utf-8") != data:
            print("API v1 contract changed; regenerate with scripts/export-openapi.py --output contracts/api-v1.openapi.json", file=sys.stderr)
            return 1
    elif args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(data, encoding="utf-8")
    else:
        sys.stdout.write(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
