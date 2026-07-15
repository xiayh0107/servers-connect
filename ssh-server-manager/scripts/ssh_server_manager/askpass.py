from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def load_descriptors() -> list[dict[str, Any]]:
    try:
        value = json.loads(os.environ.get("SSM_ASKPASS_MAP", "[]"))
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def choose_descriptor(prompt: str, descriptors: list[dict[str, Any]]) -> dict[str, Any] | None:
    lowered = prompt.casefold()
    if "yes/no" in lowered or ("fingerprint" in lowered and "continue connecting" in lowered):
        return None
    passphrases = [item for item in descriptors if item.get("slot") == "passphrase"]
    if "passphrase" in lowered:
        for item in passphrases:
            key_path = str(item.get("key_path", ""))
            if key_path.casefold() in lowered or Path(key_path).name.casefold() in lowered:
                return item
        return passphrases[0] if len(passphrases) == 1 else None
    passwords = [item for item in descriptors if item.get("slot") == "password"]
    if "password" in lowered:
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in passwords:
            score = sum(
                bool(value and str(value).casefold() in lowered)
                for value in (item.get("username"), item.get("hostname"), item.get("alias"))
            )
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if scored and (scored[0][0] > 0 or len(scored) == 1):
            return scored[0][1]
    return None


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    item = choose_descriptor(prompt, load_descriptors())
    if not item:
        return 1
    try:
        from .vault import get_vault

        secret = get_vault().get_secret(item["credential_id"], item["slot"])
    except Exception:
        return 1
    if not secret:
        return 1
    sys.stdout.write(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

