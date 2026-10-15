"""Bounded metadata enrichment for explicit remote file operations.

The optional Python probe reads metadata, never file contents. SFTP remains the
transfer and listing authority, and hosts without a usable Python shell retain
the ordinary SFTP listing with nullable enrichment fields.
"""
from __future__ import annotations

import subprocess
import threading
from datetime import datetime, timezone
from typing import Any

from .validation import ValidationError, validate_remote_path
from .host_metadata import sanitize_text


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
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
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
