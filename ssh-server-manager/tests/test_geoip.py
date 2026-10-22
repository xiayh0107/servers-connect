"""Local GeoIP lookups: from a local file, never the network, never uninvited.

The behaviour worth pinning is what the feature refuses to do: read path stays
free of lookups, a manual override survives a lookup, and no backend means a
clear message rather than a silent guess.
"""

from __future__ import annotations

import os

import pytest

from ssh_server_manager import geoip
from ssh_server_manager.db import ConflictError, Database
from ssh_server_manager.validation import ValidationError


@pytest.fixture()
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_MANAGED_SSH_CONFIG", str(tmp_path / "managed.conf"))
    monkeypatch.setenv("SSM_ORIGINAL_SSH_CONFIG", str(tmp_path / "missing-config"))
    monkeypatch.setenv("SSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSM_GEOIP_CSV", str(tmp_path / "geo.csv"))
    monkeypatch.delenv("SSM_GEOIP_MMDB", raising=False)
    return Database(tmp_path / "manager.db")


def _write_ranges(tmp_path, monkeypatch, text):
    path = tmp_path / "geo.csv"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("SSM_GEOIP_CSV", str(path))
    monkeypatch.delenv("SSM_GEOIP_MMDB", raising=False)
    geoip._index_cache.clear()
    return path


def test_no_backend_is_reported_with_a_next_step(monkeypatch, tmp_path):
    monkeypatch.setenv("SSM_GEOIP_CSV", str(tmp_path / "absent.csv"))
    monkeypatch.setenv("SSM_GEOIP_MMDB", str(tmp_path / "absent.mmdb"))
    capability = geoip.capability()
    assert capability["ok"] is False
    assert "SSM_GEOIP_CSV" in capability["message"]
    with pytest.raises(ValidationError, match="SSM_GEOIP_CSV"):
        geoip.lookup("198.51.100.1")


def test_cidr_ranges_resolve_country_and_city(tmp_path, monkeypatch):
    _write_ranges(tmp_path, monkeypatch, "cidr,country,city\n198.51.100.0/24,DE,Frankfurt\n")
    assert geoip.lookup("198.51.100.7") == {
        "country_code": "DE",
        "city": "Frankfurt",
        "source": "geoip",
    }
    assert geoip.lookup("198.51.100.255")["country_code"] == "DE"
    assert geoip.lookup("198.51.101.0") is None


def test_start_end_ranges_are_accepted(tmp_path, monkeypatch):
    _write_ranges(tmp_path, monkeypatch, "198.51.100.10,198.51.100.20,JP,Tokyo\n")
    assert geoip.lookup("198.51.100.15")["country_code"] == "JP"
    assert geoip.lookup("198.51.100.21") is None


def test_comments_blank_lines_and_a_bom_are_ignored(tmp_path, monkeypatch):
    _write_ranges(tmp_path, monkeypatch,
        "\ufeff# exported from somewhere\n\n198.51.100.0/24,DE,Frankfurt\n\n",
    )
    assert geoip.lookup("198.51.100.1")["country_code"] == "DE"


def test_ipv6_works(tmp_path, monkeypatch):
    _write_ranges(tmp_path, monkeypatch, "2001:db8::/32,FR,Paris\n")
    assert geoip.lookup("2001:db8::1")["country_code"] == "FR"


def test_a_malformed_row_names_the_line(tmp_path, monkeypatch):
    _write_ranges(tmp_path, monkeypatch, "198.51.100.0/24,DE,Frankfurt\nnot-a-range,DE\n")
    with pytest.raises(ValidationError, match="geo.csv:2"):
        geoip.lookup("198.51.100.1")


def test_a_reversed_range_is_refused(tmp_path, monkeypatch):
    _write_ranges(tmp_path, monkeypatch, "198.51.100.20,198.51.100.10,DE\n")
    with pytest.raises(ValidationError, match="ends before it starts"):
        geoip.lookup("198.51.100.15")


def test_a_hostname_is_not_accepted_as_an_address(tmp_path, monkeypatch):
    _write_ranges(tmp_path, monkeypatch, "198.51.100.0/24,DE\n")
    with pytest.raises(ValidationError, match="not an IP address"):
        geoip.lookup("web1.example")


def test_the_index_is_rebuilt_when_the_file_changes(tmp_path, monkeypatch):
    _write_ranges(tmp_path, monkeypatch, "198.51.100.0/24,DE\n")
    assert geoip.lookup("198.51.100.1")["country_code"] == "DE"
    path = tmp_path / "geo.csv"
    path.write_text("198.51.100.0/24,FR\n", encoding="utf-8")
    # "DE" and "FR" are the same length, so the size cannot distinguish these two
    # files and the cache key rests on the timestamp alone. Filesystems with a
    # coarse timestamp (and CI runners, which write both within one tick) can
    # reuse the old value, so set a clearly newer mtime rather than relying on
    # the clock moving between two writes.
    later = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(later, later))
    assert geoip.lookup("198.51.100.1")["country_code"] == "FR"


def test_a_lookup_can_fill_a_host_without_touching_a_manual_override(database):
    database.create_server(alias="web1", hostname="web1.example", username="deploy")
    database.set_server_geo("web1", {"country_code": "DE", "city": "Frankfurt", "source": "geoip"})
    assert database.get_server("web1")["geo"] == {
        "country_code": "DE",
        "city": "Frankfurt",
        "source": "geoip",
    }

    database.set_server_geo("web1", {"country_code": "FR", "city": "Paris", "source": "manual"})
    with pytest.raises(ConflictError, match="manual location override"):
        database.set_server_geo("web1", {"country_code": "JP", "city": "Tokyo", "source": "geoip"})
    assert database.get_server("web1")["geo"]["country_code"] == "FR"

    forced = database.set_server_geo(
        "web1", {"country_code": "JP", "city": "Tokyo", "source": "geoip"}, overwrite_manual=True
    )
    assert forced["geo"] == {"country_code": "JP", "city": "Tokyo", "source": "geoip"}


def test_a_lookup_cannot_claim_to_be_manual(database):
    database.create_server(alias="web1", hostname="web1.example", username="deploy")
    # The ordinary profile write path still refuses a geoip-sourced value, so a
    # lookup can only arrive through the resolver's own entry point.
    with pytest.raises(ValidationError, match="only manual"):
        database.create_server(
            alias="web2",
            hostname="web2.example",
            username="deploy",
            geo={"country_code": "DE", "source": "geoip"},
        )


def test_resolving_a_hostname_is_explicit_and_never_raises(database, monkeypatch):
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "web1.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.7", 0))]
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert geoip.resolve_hostname("web1.example") == ["198.51.100.7"]
    assert geoip.resolve_hostname("nope.invalid") == []


def test_reading_the_inventory_never_looks_a_host_up(database, monkeypatch):
    """No DNS and no geo file read on a plain read: that is the whole contract."""

    def explode(*args, **kwargs):
        raise AssertionError("a read path must not resolve or look up")

    monkeypatch.setattr(geoip, "lookup", explode)
    monkeypatch.setattr(geoip, "resolve_hostname", explode)
    database.create_server(alias="web1", hostname="web1.example", username="deploy")
    hosts = database.list_servers()
    assert hosts[0]["geo"] is None


def test_editing_an_unrelated_field_keeps_a_looked_up_location(database):
    """A stored geoip value must not make every later edit fail.

    update_server re-validates the whole row, so the stored value is preserved as
    it is rather than re-checked against the manual-only rule.
    """
    database.create_server(alias="web1", hostname="web1.example", username="deploy")
    database.set_server_geo("web1", {"country_code": "DE", "city": "Frankfurt", "source": "geoip"})

    updated = database.update_server("web1", notes="primary web node", tags=["prod"])

    assert updated["notes"] == "primary web node"
    assert updated["tags"] == ["prod"]
    assert updated["geo"] == {"country_code": "DE", "city": "Frankfurt", "source": "geoip"}


def test_a_profile_write_cannot_mint_a_new_looked_up_value(database):
    database.create_server(alias="web1", hostname="web1.example", username="deploy")
    with pytest.raises(ValidationError, match="only manual"):
        database.update_server("web1", geo={"country_code": "JP", "source": "geoip"})
