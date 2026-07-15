from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .db import Database
from .vault import VaultProtocol


class CredentialService:
    def __init__(self, database: Database, vault: VaultProtocol) -> None:
        self.database = database
        self.vault = vault

    def create_password(self, label: str, secret: str) -> dict[str, Any]:
        identifier = str(uuid.uuid4())
        credential = self.database.create_credential(
            credential_id=identifier, label=label, kind="password", has_secret=False
        )
        try:
            self.vault.set_secret(identifier, "password", secret)
            return self.database.update_credential(identifier, has_secret=True)
        except Exception:
            self.vault.delete_secret(identifier, "password")
            self.database.delete_credential(identifier)
            raise

    def create_key(self, label: str, key_path: str, passphrase: str | None = None) -> dict[str, Any]:
        identifier = str(uuid.uuid4())
        credential = self.database.create_credential(
            credential_id=identifier,
            label=label,
            kind="key",
            key_path=key_path,
            has_passphrase=False,
        )
        if not passphrase:
            return credential
        try:
            self.vault.set_secret(identifier, "passphrase", passphrase)
            return self.database.update_credential(identifier, has_passphrase=True)
        except Exception:
            self.vault.delete_secret(identifier, "passphrase")
            self.database.delete_credential(identifier)
            raise

    def create_agent(self, label: str) -> dict[str, Any]:
        return self.database.create_credential(label=label, kind="agent")

    def update(
        self,
        identifier: str,
        *,
        label: str | None = None,
        key_path: str | None = None,
        secret: str | None = None,
        passphrase: str | None = None,
        clear_passphrase: bool = False,
    ) -> dict[str, Any]:
        credential = self.database.get_credential(identifier)
        changes: dict[str, Any] = {}
        if label is not None:
            changes["label"] = label
        if key_path is not None:
            changes["key_path"] = key_path
        if secret is not None:
            if credential["kind"] != "password":
                raise ValueError("only password credentials have a password secret")
            self.vault.set_secret(credential["id"], "password", secret)
            changes["has_secret"] = True
        if passphrase is not None:
            if credential["kind"] != "key":
                raise ValueError("only key credentials have a passphrase")
            self.vault.set_secret(credential["id"], "passphrase", passphrase)
            changes["has_passphrase"] = True
        if clear_passphrase:
            self.vault.delete_secret(credential["id"], "passphrase")
            changes["has_passphrase"] = False
        return self.database.update_credential(credential["id"], **changes) if changes else credential

    def delete(self, identifier: str) -> dict[str, Any]:
        credential = self.database.get_credential(identifier)
        # The database enforces that referenced credentials cannot be removed.
        removed = self.database.delete_credential(credential["id"])
        self.vault.delete_secret(credential["id"], "password")
        self.vault.delete_secret(credential["id"], "passphrase")
        return removed

    def reveal(self, identifier: str) -> dict[str, Any]:
        credential = self.database.get_credential(identifier)
        if credential["kind"] == "password":
            slot = "password"
        elif credential["kind"] == "key" and credential["has_passphrase"]:
            slot = "passphrase"
        else:
            raise ValueError("this credential has no revealable secret")
        value = self.vault.get_secret(credential["id"], slot)
        if value is None:
            raise ValueError("the OS credential vault has no matching secret")
        return {"credential_id": credential["id"], "slot": slot, "value": value}

