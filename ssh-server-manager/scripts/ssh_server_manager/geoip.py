"""Optional local GeoIP: where a host is, without telling anyone we asked.

Two rules decide the design.

* **Never on a read path.** Opening the inventory, a host's details, or a route
  must not resolve a name or read a geo database: it is network or disk work the
  user did not ask for. Lookups happen only through the explicit `geo resolve`
  command (or a diagnose the user already started), and only `--apply` writes.
* **Never over the network.** There is no call to a GeoIP web service and no
  bundled database. A lookup uses a file the user already has: a MaxMind-style
  `.mmdb` when the optional `geoip2` package is installed, or a plain CSV of
  CIDR ranges that any source can produce.

A manual override always wins. A geo value the user typed is a statement about
their own infrastructure ("this flaky box is really in the Frankfurt cage"), and
a lookup guessing something else must not overwrite it.
"""

from __future__ import annotations

import bisect
import csv
import ipaddress
import os
from pathlib import Path
from typing import Any

from .validation import ValidationError

DEFAULT_CSV_NAME = "geoip.csv"
DEFAULT_MMDB_NAME = "geoip.mmdb"
MAX_CSV_BYTES = 64 * 1024 * 1024

_index_cache: dict[tuple[str, int, int], list[tuple[int, int, str, str | None]]] = {}


def csv_path() -> Path:
    override = os.environ.get("SSM_GEOIP_CSV")
    if override:
        return Path(override).expanduser()
    from .paths import data_dir

    return data_dir() / DEFAULT_CSV_NAME


def mmdb_path() -> Path:
    override = os.environ.get("SSM_GEOIP_MMDB")
    if override:
        return Path(override).expanduser()
    from .paths import data_dir

    return data_dir() / DEFAULT_MMDB_NAME


def capability() -> dict[str, Any]:
    """Report which local backend, if any, can answer a lookup."""
    csv_file = csv_path()
    if csv_file.is_file():
        return {
            "ok": True,
            "backend": "csv",
            "path": str(csv_file),
            "message": "lookups read this local range file; nothing leaves this machine",
        }
    mmdb_file = mmdb_path()
    if mmdb_file.is_file():
        try:
            import geoip2.database  # noqa: F401
        except ImportError:
            return {
                "ok": False,
                "backend": "mmdb",
                "path": str(mmdb_file),
                "message": "install the optional 'geoip2' package to read this .mmdb file",
            }
        return {
            "ok": True,
            "backend": "mmdb",
            "path": str(mmdb_file),
            "message": "lookups read this local database; nothing leaves this machine",
        }
    return {
        "ok": False,
        "backend": None,
        "path": str(csv_file),
        "message": (
            "no local geo database configured; set SSM_GEOIP_CSV to a CIDR range "
            "file (or SSM_GEOIP_MMDB with the optional geoip2 package)"
        ),
    }


def _load_csv(path: Path) -> list[tuple[int, int, str, str | None]]:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _index_cache.get(key)
    if cached is not None:
        return cached
    if stat.st_size > MAX_CSV_BYTES:
        raise ValidationError(f"geo range file is too large: {path}")
    entries: list[tuple[int, int, str, str | None]] = []
    # utf-8-sig: a spreadsheet export usually starts with a BOM.
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for number, row in enumerate(csv.reader(handle), start=1):
            if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            if row[0].strip().casefold() == "cidr":
                continue  # header row
            try:
                entries.append(_parse_row(row, number))
            except ValidationError as exc:
                raise ValidationError(f"{path}:{number}: {exc}") from exc
    entries.sort(key=lambda item: item[0])
    if len(_index_cache) > 4:
        _index_cache.clear()
    _index_cache[key] = entries
    return entries


def _parse_row(row: list[str], number: int) -> tuple[int, int, str, str | None]:
    first = row[0].strip()
    try:
        if "/" in first:
            network = ipaddress.ip_network(first, strict=False)
            country = _field(row, 1, number)
            city = _field(row, 2, number, optional=True)
        else:
            start = ipaddress.ip_address(first)
            end = ipaddress.ip_address(_field(row, 1, number))
            if start.version != end.version:
                raise ValidationError("range mixes IPv4 and IPv6")
            if int(end) < int(start):
                raise ValidationError("range ends before it starts")
            country = _field(row, 2, number)
            city = _field(row, 3, number, optional=True)
            return int(start), int(end), country, city
    except ValueError as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(f"invalid address or range: {first}") from exc
    return int(network.network_address), int(network.broadcast_address), country, city


def _field(row: list[str], index: int, number: int, *, optional: bool = False) -> str:
    value = row[index].strip() if index < len(row) else ""
    if not value:
        if optional:
            return ""
        raise ValidationError(f"row {number} is missing a column")
    return value


def _lookup_csv(path: Path, address: ipaddress._BaseAddress) -> dict[str, Any] | None:
    entries = _load_csv(path)
    if not entries:
        return None
    value = int(address)
    starts = [entry[0] for entry in entries]
    position = bisect.bisect_right(starts, value) - 1
    # Ranges can nest or overlap; walk back a little rather than assume the last
    # start is the only candidate.
    for index in range(max(position, 0), max(position - 8, -1), -1):
        start, end, country, city = entries[index]
        if start <= value <= end:
            return {
                "country_code": country.upper(),
                "city": city or None,
                "source": "geoip",
            }
    return None


def _lookup_mmdb(path: Path, address: ipaddress._BaseAddress) -> dict[str, Any] | None:
    import geoip2.database

    with geoip2.database.Reader(str(path)) as reader:
        try:
            record = reader.city(str(address))
        except Exception:  # noqa: BLE001 - geoip2 raises several address/shape errors
            return None
    country = getattr(record.country, "iso_code", None) or getattr(
        record.registered_country, "iso_code", None
    )
    if not country:
        return None
    return {
        "country_code": str(country).upper(),
        "city": getattr(record.city, "name", None),
        "source": "geoip",
    }


def lookup(address: str) -> dict[str, Any] | None:
    """Resolve one literal IP address from the local database.

    A hostname is deliberately not accepted: resolving it is a network operation,
    and the caller decides when to spend one (the CLI resolves and passes the
    address in).
    """
    try:
        parsed = ipaddress.ip_address(str(address).strip().strip("[]"))
    except ValueError as exc:
        raise ValidationError(f"not an IP address: {address}") from exc
    csv_file = csv_path()
    if csv_file.is_file():
        return _lookup_csv(csv_file, parsed)
    mmdb_file = mmdb_path()
    if mmdb_file.is_file():
        return _lookup_mmdb(mmdb_file, parsed)
    raise ValidationError(capability()["message"])


def resolve_hostname(host: str, *, timeout: float = 3.0) -> list[str]:
    """Resolve a hostname to addresses. Explicitly a network call; callers know."""
    import socket

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses
