"""Bounded host observations and address classification, without network I/O."""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from .validation import ValidationError


OS_LOGOS = frozenset({"debian", "ubuntu", "rocky", "alpine", "rhel", "fedora", "arch", "macos", "freebsd", "windows", "unknown"})
FACT_FIELDS = frozenset({"hostname", "os", "os_pretty", "os_id", "os_version", "os_like", "os_family", "package_manager", "kernel", "arch", "uptime", "load", "cpu_cores", "cpu_model", "memory", "disk"})
COUNTRY_CODES = frozenset("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW".split())
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


def sanitize_text(value: Any, *, limit: int = 500) -> str:
    """Strip remote control/format characters and bound every display value."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return "".join(character for character in str(value) if not unicodedata.category(character).startswith("C")).strip()[:limit]


def address_scope(hostname: str) -> str:
    name = hostname.strip().strip("[]").rstrip(".").casefold()
    if name == "localhost" or name.endswith(".localhost"):
        return "loopback"
    if name.endswith((".lan", ".local", ".home.arpa")):
        return "lan"
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return "unresolved"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return "loopback"
    if any(address in network for network in _PRIVATE_NETWORKS if address.version == network.version):
        return "private"
    if address.is_link_local:
        return "lan"
    return "public" if address.is_global else "unresolved"


def observation_time(value: str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValidationError("observation time must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError("observation time must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _os_logo(facts: dict[str, str]) -> str:
    aliases = {"linuxmint": "debian", "raspbian": "debian", "pop": "debian", "kali": "debian", "almalinux": "rhel", "centos": "rhel", "ol": "rhel", "amzn": "rhel", "manjaro": "arch", "endeavouros": "arch", "darwin": "macos"}
    for candidate in (facts.get("os_id", ""), *facts.get("os_like", "").split(), facts.get("os_family", ""), facts.get("kernel", "").split(" ")[0]):
        candidate = candidate.casefold()
        logo = aliases.get(candidate, candidate)
        if logo in OS_LOGOS - {"unknown"}:
            return logo
    return "unknown"


def sanitize_facts(value: dict[str, Any], *, observed_at: str | None = None) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValidationError("host facts must be an object")
    facts = {key: cleaned for key in FACT_FIELDS if key in value and (cleaned := sanitize_text(value[key]))}
    facts["os_logo"] = _os_logo(facts)
    facts["observed_at"] = observation_time(observed_at)
    return facts


def validate_resolved_ip(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("resolved IP must be an IPv4 or IPv6 address")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise ValidationError("resolved IP must be an IPv4 or IPv6 address") from exc


def sanitize_host_key(value: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("host key observation must be an object")
    state = value.get("state", "unknown")
    if state not in {"verified", "unknown", "changed", "revoked"}:
        raise ValidationError("invalid host key match state")
    fingerprint = value.get("fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}=?", fingerprint)):
        raise ValidationError("host key fingerprint must be SHA256")
    if state == "verified" and fingerprint is None:
        raise ValidationError("a verified host key requires a fingerprint")
    return {"fingerprint": fingerprint, "state": state, "observed_at": observed_at}


def validate_geo(
    value: dict[str, Any] | None, *, allow_geoip: bool = False
) -> dict[str, Any] | None:
    """Normalize a geo value.

    ``source`` records where the value came from. A user typing an override
    produces ``manual``; only the local GeoIP resolver may write ``geoip``, so a
    looked-up guess can never be mistaken for something a human asserted.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() - {"country_code", "city", "source"}:
        raise ValidationError("geo must contain country_code and optional city")
    country = value.get("country_code")
    if not isinstance(country, str) or country.upper() not in COUNTRY_CODES:
        raise ValidationError("geo country_code must be an ISO 3166-1 alpha-2 code")
    source = value.get("source", "manual")
    if source != "manual" and not (allow_geoip and source == "geoip"):
        raise ValidationError("only manual geo overrides can be saved")
    if allow_geoip and source == "geoip":
        city = value.get("city")
        if city is not None and (not isinstance(city, str) or not city.isprintable()):
            raise ValidationError("geo city must be printable")
        return {"country_code": country.upper(), "city": (city or None), "source": "geoip"}
    city = value.get("city")
    if city is not None:
        if not isinstance(city, str) or len(city) > 120 or not city.isprintable():
            raise ValidationError("geo city must be at most 120 printable characters")
        city = city.strip() or None
    return {"country_code": country.upper(), "city": city, "source": "manual"}
