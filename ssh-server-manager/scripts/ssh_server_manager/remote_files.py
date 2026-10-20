"""Bounded metadata enrichment for explicit remote file operations.

The optional Python probe reads metadata, never file contents. SFTP remains the
transfer and listing authority, and hosts without a usable Python shell retain
the ordinary SFTP listing with nullable enrichment fields.
"""
from __future__ import annotations

import json
import posixpath
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any

from .validation import ValidationError, validate_remote_path
from .host_metadata import sanitize_text
from .process_args import literal_config_args


METADATA_PREFIX = "__ssm_files__="
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_METADATA_ENTRIES = 5000


def clean_remote_text(value: Any, limit: int = 512) -> str:
    return sanitize_text(value, limit=limit)


def validate_web_file_path(value: str) -> str:
    """Require a literal web file destination; CLI directory targets stay valid."""
    path = validate_remote_path(value)
    if path == "~" or path.endswith("/") or any(part in {".", ".."} for part in path.split("/")):
        raise ValidationError("path must name a file without dot or parent-directory segments")
    return path


def file_error_code(stderr: str, returncode: int = 0) -> str:
    """Classify file failures without returning arbitrary remote diagnostics."""
    value = stderr.casefold()
    if "timed out" in value or "timeout" in value:
        return "timeout"
    if "not a directory" in value:
        return "not_a_directory"
    if "no such file" in value or "does not exist" in value:
        return "not_found"
    if "file exists" in value or "already exists" in value:
        return "exists"
    if "does not support" in value or "unsupported" in value:
        return "unsupported"
    if "permission denied" in value or "authentication failed" in value:
        # OpenSSH transport failures use 255; SFTP operation errors often exit 0.
        return "auth_failed" if returncode == 255 or "publickey" in value else "permission_denied"
    return "unreachable"


def run_metadata_command(command: list[str], *, request: str, env: dict[str, str], timeout: int):
    """Capture bounded pipes while the process runs, including hostile output.

    Separate readers work on Windows pipes as well as POSIX. The main thread
    enforces the deadline even if SSH never consumes the bounded stdin request.
    """
    process = subprocess.Popen(literal_config_args(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    output, diagnostics = bytearray(), bytearray()
    exceeded = threading.Event()

    def read(pipe, destination, limit):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                remaining = limit - len(destination)
                destination.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    exceeded.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break
        except (OSError, ValueError):
            pass
        finally:
            pipe.close()

    def write():
        try:
            process.stdin.write(request.encode("utf-8"))
            process.stdin.close()
        except (OSError, ValueError):
            pass

    readers = [
        threading.Thread(target=read, args=(process.stdout, output, MAX_METADATA_BYTES), daemon=True),
        threading.Thread(target=read, args=(process.stderr, diagnostics, 65536), daemon=True),
        threading.Thread(target=write, daemon=True),
    ]
    for thread in readers:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.wait()
        raise
    finally:
        for thread in readers:
            thread.join(timeout=1)
    result = subprocess.CompletedProcess(
        command, process.returncode,
        bytes(output).decode("utf-8", errors="replace"),
        bytes(diagnostics).decode("utf-8", errors="replace"),
    )
    return result, exceeded.is_set()


def normalize_metadata(raw: Any) -> dict[str, Any] | None:
    """Whitelist and validate the optional remote metadata document."""
    if not isinstance(raw, dict):
        return None
    size, uid, gid = (raw.get(key) for key in ("size", "uid", "gid"))
    kind = raw.get("type")
    if kind not in {"file", "directory", "symlink", "special"}:
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    value = {"size": size, "type": kind}
    for key, number in (("uid", uid), ("gid", gid)):
        value[key] = number if isinstance(number, int) and not isinstance(number, bool) and number >= 0 else None
    for key in ("owner", "group"):
        value[key] = clean_remote_text(raw[key]) if isinstance(raw.get(key), str) else None
    value["link_target"] = clean_remote_text(raw["link_target"], 1024) if isinstance(raw.get("link_target"), str) else None
    value["modified_at"] = None
    modified = raw.get("modified_at")
    if isinstance(modified, str):
        try:
            parsed = datetime.fromisoformat(modified.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                value["modified_at"] = parsed.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            pass
    return value


# The request is JSON over stdin, keeping file names out of shell syntax and
# avoiding command-line length limits for a large visible directory page.
METADATA_SCRIPT = r'''
import datetime, errno, json, os, stat, sys, unicodedata
try:
    import pwd, grp
except ImportError:
    pwd = grp = None
def text(value, limit=512):
    return ''.join(c for c in str(value) if not unicodedata.category(c).startswith('C')).strip()[:limit]
def error(exc):
    return {errno.ENOENT: 'not_found', errno.ENOTDIR: 'not_a_directory',
            errno.EACCES: 'permission_denied', errno.EPERM: 'permission_denied',
            errno.EEXIST: 'exists'}.get(exc.errno, 'unreachable')
def metadata(path, follow=False):
    item = os.stat(path) if follow else os.lstat(path)
    kind = ('file' if stat.S_ISREG(item.st_mode) else 'directory' if stat.S_ISDIR(item.st_mode)
            else 'symlink' if stat.S_ISLNK(item.st_mode) else 'special')
    owner, group = str(item.st_uid), str(item.st_gid)
    if pwd is not None:
        if item.st_uid not in owners:
            try: owners[item.st_uid] = pwd.getpwuid(item.st_uid).pw_name
            except KeyError: owners[item.st_uid] = owner
        if item.st_gid not in groups:
            try: groups[item.st_gid] = grp.getgrgid(item.st_gid).gr_name
            except KeyError: groups[item.st_gid] = group
        owner, group = owners[item.st_uid], groups[item.st_gid]
    target = None
    if kind == 'symlink':
        try: target = text(os.readlink(path), 1024)
        except OSError: pass
    return {'type': kind, 'size': item.st_size, 'uid': item.st_uid, 'gid': item.st_gid,
            'owner': text(owner), 'group': text(group), 'link_target': target,
            'modified_at': datetime.datetime.fromtimestamp(item.st_mtime, datetime.timezone.utc).isoformat()}
try:
    owners, groups = {}, {}
    request = json.loads(sys.stdin.read(2 * 1024 * 1024))
    path = request['path']
    if path == '~' or path.startswith('~/'): path = os.path.expanduser(path)
    if request['mode'] == 'stat':
        result = {'ok': True, 'entry': metadata(path, request.get('follow', True))}
    else:
        entries = {}
        used = 128
        for name in request['names'][:5000]:
            if not isinstance(name, str) or name in ('.', '..') or '/' in name or any(ord(c) < 32 for c in name): continue
            try:
                item = metadata(os.path.join(path, name))
                cost = len(json.dumps({name: item}, ensure_ascii=True))
                if used + cost > 8 * 1024 * 1024: break
                entries[name] = item
                used += cost
            except (OSError, ValueError, OverflowError): pass
        result = {'ok': True, 'entries': entries}
except OSError as exc:
    result = {'ok': False, 'error_code': error(exc)}
except (KeyError, ValueError, TypeError, OverflowError):
    result = {'ok': False, 'error_code': 'unreachable'}
print('__ssm_files__=' + json.dumps(result, ensure_ascii=True, separators=(',', ':')))
'''


LIST_PREFIX = "__ssm_list__="
MAX_LIST_BYTES = 8 * 1024 * 1024

# One exec returns the whole directory: the SFTP subsystem costs a handshake and
# then one lstat round trip per entry, which dominates browsing on a high-latency
# link. This reads the same facts through os.scandir, so the listing and its
# precise metadata arrive together instead of in two round trips.
#
# The request is JSON over stdin so a path never reaches shell syntax.
LIST_SCRIPT = r'''
import datetime, errno, json, os, stat, sys, unicodedata
try:
    import pwd, grp
except ImportError:
    pwd = grp = None
def text(value, limit=512):
    return ''.join(c for c in str(value) if not unicodedata.category(c).startswith('C')).strip()[:limit]
def error(exc):
    return {errno.ENOENT: 'not_found', errno.ENOTDIR: 'not_a_directory',
            errno.EACCES: 'permission_denied', errno.EPERM: 'permission_denied'}.get(exc.errno, 'unreachable')
def name_of(entry):
    # A name that cannot round-trip as UTF-8 or carries control characters is
    # counted, never returned: the caller renders these names in a browser.
    try: entry.name.encode('utf-8')
    except UnicodeEncodeError: return None
    if entry.name in ('.', '..') or '/' in entry.name: return None
    if any(ord(c) < 32 or ord(c) == 127 for c in entry.name): return None
    return entry.name
try:
    owners, groups = {}, {}
    request = json.loads(sys.stdin.read(65536))
    path = request['path']
    if path == '~' or path.startswith('~/'): path = os.path.expanduser(path)
    entries, unparsed, truncated, used = [], 0, False, 128
    with os.scandir(path) as scan:
        for item in scan:
            name = name_of(item)
            if name is None:
                unparsed += 1
                continue
            if len(entries) >= 5000:
                truncated = True
                break
            try: info = item.stat(follow_symlinks=False)
            except OSError:
                unparsed += 1
                continue
            kind = ('file' if stat.S_ISREG(info.st_mode) else 'directory' if stat.S_ISDIR(info.st_mode)
                    else 'symlink' if stat.S_ISLNK(info.st_mode) else 'special')
            owner, group = str(info.st_uid), str(info.st_gid)
            if pwd is not None:
                if info.st_uid not in owners:
                    try: owners[info.st_uid] = pwd.getpwuid(info.st_uid).pw_name
                    except KeyError: owners[info.st_uid] = owner
                if info.st_gid not in groups:
                    try: groups[info.st_gid] = grp.getgrgid(info.st_gid).gr_name
                    except KeyError: groups[info.st_gid] = group
                owner, group = owners[info.st_uid], groups[info.st_gid]
            target = None
            if kind == 'symlink':
                try: target = text(os.readlink(item.path), 1024)
                except OSError: pass
            item_out = {'name': name, 'type': kind, 'size': info.st_size,
                        'uid': info.st_uid, 'gid': info.st_gid,
                        'owner': text(owner), 'group': text(group),
                        'permissions': stat.filemode(info.st_mode), 'link_target': target,
                        'modified_at': datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc).isoformat()}
            cost = len(json.dumps(item_out, ensure_ascii=True))
            if used + cost > 7 * 1024 * 1024:
                truncated = True
                break
            entries.append(item_out)
            used += cost
    result = {'ok': True, 'path': os.path.realpath(path), 'entries': entries,
              'truncated': truncated, 'unparsed': unparsed}
except OSError as exc:
    result = {'ok': False, 'error_code': error(exc)}
except (KeyError, ValueError, TypeError, OverflowError, UnicodeError):
    result = {'ok': False, 'error_code': 'unreachable'}
print('__ssm_list__=' + json.dumps(result, ensure_ascii=True, separators=(',', ':')))
'''


def listing_entry(raw: Any, directory: str) -> dict[str, Any] | None:
    """Whitelist one fast-listing entry; the probe's output is untrusted.

    Returns None for anything the browser could not render safely or that
    disagrees with the shape the SFTP parser produces.
    """
    if not isinstance(raw, dict):
        return None
    name, kind = raw.get("name"), raw.get("type")
    size = raw.get("size")
    if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name:
        return None
    if kind not in {"file", "directory", "symlink", "special"}:
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    permissions = raw.get("permissions")
    if not isinstance(permissions, str) or len(permissions) != 10:
        return None
    entry: dict[str, Any] = {
        "name": name,
        "type": kind,
        "size": size,
        "modified": "",
        "permissions": clean_remote_text(permissions, 10),
        "owner": clean_remote_text(raw.get("owner")) if isinstance(raw.get("owner"), str) else "",
        "group": clean_remote_text(raw.get("group")) if isinstance(raw.get("group"), str) else "",
        "uid": raw["uid"] if isinstance(raw.get("uid"), int) and not isinstance(raw["uid"], bool) and raw["uid"] >= 0 else None,
        "gid": raw["gid"] if isinstance(raw.get("gid"), int) and not isinstance(raw["gid"], bool) and raw["gid"] >= 0 else None,
        "modified_at": None,
        "link_target": clean_remote_text(raw["link_target"], 1024) if isinstance(raw.get("link_target"), str) else None,
        "hidden": name.startswith("."),
        "path": posixpath.join(directory, name),
    }
    modified = raw.get("modified_at")
    if isinstance(modified, str):
        try:
            parsed = datetime.fromisoformat(modified.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                entry["modified_at"] = parsed.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            pass
    return entry


def parse_listing_document(stdout: str) -> tuple[str, list[dict[str, Any]], int, bool] | None:
    """Parse one ``LIST_SCRIPT`` document, or None when there is no usable one.

    None means "this route did not answer" and sends the caller back to SFTP; it
    never means an empty directory.
    """
    for line in reversed(stdout.splitlines()):
        if not line.startswith(LIST_PREFIX):
            continue
        try:
            document = json.loads(line[len(LIST_PREFIX):])
        except (ValueError, TypeError):
            return None
        if not isinstance(document, dict) or document.get("ok") is not True:
            return None
        raw_entries = document.get("entries")
        directory = document.get("path")
        if not isinstance(raw_entries, list) or not isinstance(directory, str) or not directory:
            return None
        entries = [entry for entry in (listing_entry(raw, directory) for raw in raw_entries) if entry is not None]
        unparsed = document.get("unparsed")
        unparsed = unparsed if isinstance(unparsed, int) and not isinstance(unparsed, bool) else 0
        # Same ordering as the SFTP parser so both routes render identically.
        entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
        return directory, entries, max(unparsed, 0), document.get("truncated") is True
    return None
