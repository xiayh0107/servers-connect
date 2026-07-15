from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol


SERVICE_NAME = "ssh-server-manager"


class VaultError(RuntimeError):
    pass


class VaultProtocol(Protocol):
    def set_secret(self, credential_id: str, slot: str, value: str) -> None: ...

    def get_secret(self, credential_id: str, slot: str) -> str | None: ...

    def delete_secret(self, credential_id: str, slot: str) -> None: ...

    def diagnose(self) -> dict[str, object]: ...


def account_name(credential_id: str, slot: str) -> str:
    if slot not in {"password", "passphrase"}:
        raise VaultError(f"unsupported secret slot: {slot}")
    return f"credential:{credential_id}:{slot}"


class KeyringVault:
    def __init__(self) -> None:
        try:
            import keyring
            from keyring import errors
        except ImportError as exc:
            raise VaultError("Python keyring is not installed; run scripts/bootstrap") from exc
        self.keyring = keyring
        self.errors = errors
        self.backend = keyring.get_keyring()
        self._require_safe_backend()

    def _require_safe_backend(self) -> None:
        unsafe = ("null", "fail", "plaintext", "keyrings.alt", "filekeyring")

        def check(backend: object) -> None:
            backend_type = f"{backend.__class__.__module__}.{backend.__class__.__name__}".lower()
            priority = getattr(backend, "priority", 0)
            if any(marker in backend_type for marker in unsafe) or priority <= 0:
                hint = ""
                if sys.platform.startswith("linux"):
                    hint = (
                        "; on Linux install and unlock a Secret Service provider"
                        " (gnome-keyring, KWallet, or KeePassXC with Secret Service enabled)"
                        " — see docs/platforms.md for headless servers"
                    )
                raise VaultError(f"no safe OS credential backend is available ({backend_type}){hint}")
            for child in getattr(backend, "backends", ()):
                check(child)

        check(self.backend)

    def set_secret(self, credential_id: str, slot: str, value: str) -> None:
        if not value:
            raise VaultError("secret must not be empty")
        try:
            self.keyring.set_password(SERVICE_NAME, account_name(credential_id, slot), value)
        except self.errors.KeyringError as exc:
            raise VaultError(f"credential vault rejected the secret: {exc}") from exc

    def get_secret(self, credential_id: str, slot: str) -> str | None:
        try:
            return self.keyring.get_password(SERVICE_NAME, account_name(credential_id, slot))
        except self.errors.KeyringError as exc:
            raise VaultError(f"credential vault could not read the secret: {exc}") from exc

    def delete_secret(self, credential_id: str, slot: str) -> None:
        try:
            self.keyring.delete_password(SERVICE_NAME, account_name(credential_id, slot))
        except self.errors.PasswordDeleteError:
            return
        except self.errors.KeyringError as exc:
            raise VaultError(f"credential vault could not delete the secret: {exc}") from exc

    def diagnose(self) -> dict[str, object]:
        backend_type = f"{self.backend.__class__.__module__}.{self.backend.__class__.__name__}"
        return {"available": True, "backend": backend_type, "priority": getattr(self.backend, "priority", None)}


@dataclass
class MemoryVault:
    """In-memory vault used only by tests."""

    values: dict[tuple[str, str], str]

    def __init__(self) -> None:
        self.values = {}

    def set_secret(self, credential_id: str, slot: str, value: str) -> None:
        self.values[(credential_id, slot)] = value

    def get_secret(self, credential_id: str, slot: str) -> str | None:
        return self.values.get((credential_id, slot))

    def delete_secret(self, credential_id: str, slot: str) -> None:
        self.values.pop((credential_id, slot), None)

    def diagnose(self) -> dict[str, object]:
        return {"available": True, "backend": "memory-test-only", "priority": 1}


def get_vault() -> VaultProtocol:
    return KeyringVault()
