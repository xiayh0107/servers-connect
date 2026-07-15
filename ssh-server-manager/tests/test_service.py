from __future__ import annotations

from ssh_server_manager.db import Database
from ssh_server_manager.service import CredentialService
from ssh_server_manager.vault import MemoryVault


def test_password_never_enters_database(tmp_path):
    database = Database(tmp_path / "manager.db")
    vault = MemoryVault()
    service = CredentialService(database, vault)
    credential = service.create_password("Production password", "do-not-persist")
    assert credential["has_secret"] is True
    assert "do-not-persist" not in (tmp_path / "manager.db").read_bytes().decode("utf-8", errors="ignore")
    assert service.reveal(credential["id"])["value"] == "do-not-persist"


def test_key_path_and_passphrase(tmp_path):
    key = tmp_path / "id_test"
    key.write_text("fixture", encoding="utf-8")
    database = Database(tmp_path / "manager.db")
    vault = MemoryVault()
    service = CredentialService(database, vault)
    credential = service.create_key("Test key", str(key), "passphrase")
    assert credential["key_path"] == str(key)
    assert credential["has_passphrase"] is True
    assert service.reveal(credential["id"])["slot"] == "passphrase"

