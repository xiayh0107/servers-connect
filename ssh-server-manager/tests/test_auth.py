from __future__ import annotations

import pytest

pytest.importorskip("webauthn")

from ssh_server_manager.auth import RevealAuth
from ssh_server_manager.db import Database


def test_webauthn_registration_options_require_user_verification(tmp_path):
    database = Database(tmp_path / "manager.db")
    auth = RevealAuth(database, origin="http://localhost:8765")
    options = auth.begin_registration("session")
    assert options["rp"]["id"] == "localhost"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["challenge"]

