from __future__ import annotations

from ssh_server_manager import importer
from ssh_server_manager.db import Database


def test_import_preview_and_apply(tmp_path, monkeypatch):
    config = tmp_path / "config"
    included = tmp_path / "extra.conf"
    included.write_text("Host compute\n  HostName 10.0.0.8\n", encoding="utf-8")
    config.write_text(f"Host login\n  HostName gateway.example\nInclude {included}\nHost *.wild\n", encoding="utf-8")

    def resolve(alias, _config, *, ssh_binary="ssh"):
        values = {
            "login": {"hostname": "gateway.example", "port": 2222, "username": "alice", "proxy_jumps": []},
            "compute": {"hostname": "10.0.0.8", "port": 22, "username": "alice", "proxy_jumps": ["login"]},
        }[alias]
        return {"alias": alias, "credential_id": None, "notes": "imported", "source": "ssh-config-import", **values}

    monkeypatch.setattr(importer, "_resolve_alias", resolve)
    database = Database(tmp_path / "manager.db")
    preview = importer.preview_import(database, config=config)
    assert [item["server"]["alias"] for item in preview["items"]] == ["login", "compute"]
    assert preview["skipped_patterns"] == ["*.wild"]
    result = importer.apply_import(database, preview)
    assert result["added"] == ["login", "compute"]
    assert database.get_server("compute")["proxy_jumps"] == ["login"]

