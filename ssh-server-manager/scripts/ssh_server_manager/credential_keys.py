"""Bounded local key snapshots; private bytes never leave this module."""
from __future__ import annotations

import base64
import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import ConflictError
from .validation import ValidationError

MAX_KEY_BYTES = 1024 * 1024


def _windows_permissions(path: Path) -> dict[str, Any]:
    """Conservative DACL assessment: unknown ACEs never mean 'secure'."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    def sid_string(pointer):
        value = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(pointer, ctypes.byref(value)):
            raise OSError("SID unavailable")
        try:
            return value.value
        finally:
            kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))
    result = {"method": "windows_acl", "assessment": "unknown", "mode": None}
    try:
        if advapi.GetNamedSecurityInfoW(str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None,
                ctypes.byref(acl), None, ctypes.byref(descriptor)):
            return result
        if not acl.value:
            return {**result, "assessment": "unsafe"}
        class AclSize(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]
        info = AclSize()
        advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
        advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        if not advapi.GetAclInformation(acl, ctypes.byref(info), ctypes.sizeof(info), 2):
            return result
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            return result
        try:
            size = wintypes.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                return result
            current_sid = sid_string(ctypes.c_void_p.from_buffer(buffer).value)
        finally:
            kernel.CloseHandle(token)
        allowed = {current_sid, "S-1-5-18", "S-1-5-32-544"}
        if sid_string(owner) not in allowed:
            return {**result, "assessment": "unsafe"}
        unknown = False
        for index in range(info.count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(acl, index, ctypes.byref(ace)):
                return result
            ace_type = ctypes.c_ubyte.from_address(ace.value).value
            if ace_type == 1:  # Denials only tighten this conservative check.
                continue
            if ace_type != 0:
                unknown = True
                continue
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            if mask & 0xF00D0007 and sid_string(ace.value + 8) not in allowed:
                return {**result, "assessment": "unsafe"}
        return {**result, "assessment": "unknown" if unknown else "secure"}
    except (OSError, ValueError):
        return result
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)


def key_permissions(path: Path, info: os.stat_result) -> dict[str, Any]:
    if os.name == "nt":
        return _windows_permissions(path)
    mode = stat.S_IMODE(info.st_mode)
    secure = info.st_uid == os.getuid() and not mode & 0o077
    return {"method": "posix_mode", "assessment": "secure" if secure else "unsafe", "mode": f"{mode:04o}"}


@dataclass(repr=False)
class KeySnapshot:
    path: Path
    data: bytes
    revision: str
    metadata: dict[str, Any]

    def confirm(self) -> None:
        current = read_snapshot(str(self.path))
        if current.revision != self.revision:
            raise ConflictError("private key changed while it was being checked; inspect it again")


def read_snapshot(value: str) -> KeySnapshot:
    if not value or any(ord(char) < 32 for char in value):
        raise ValidationError("private-key path is invalid")
    try:
        path = Path(value).expanduser().resolve(strict=True)
        # O_NONBLOCK stops a substituted FIFO/device from hanging before fstat.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValidationError("private-key path must be a regular file")
            if before.st_size > MAX_KEY_BYTES:
                raise ValidationError("private-key file exceeds the size limit")
            parts: list[bytes] = []
            remaining = MAX_KEY_BYTES + 1
            while remaining:
                part = os.read(fd, min(65536, remaining))
                if not part:
                    break
                parts.append(part)
                remaining -= len(part)
            data = b"".join(parts)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if len(data) > MAX_KEY_BYTES:
            raise ValidationError("private-key file exceeds the size limit")
        fields = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        # Windows stat(path) reports creation time as ctime, while fstat(fd)
        # may report change time. Keep descriptor ctime in both the read check
        # and revision, but compare the shared birthtime across fd/path.
        path_fields = (lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                                    getattr(item, "st_birthtime_ns", None))) if os.name == "nt" else fields
        if fields(before) != fields(after) or path_fields(after) != path_fields(path.stat()):
            raise ConflictError("private key changed while it was being read")
        identity = repr((str(path), fields(after))).encode()
        revision = hashlib.sha256(identity + b"\0" + data).hexdigest()
        metadata = {"path": str(path), "status": "unverified", "format": None, "encrypted": None,
            "algorithm": None, "fingerprint": None, "bits": None,
            "permissions": key_permissions(path, after),
            "observed_at": datetime.now(timezone.utc).isoformat()}
        return KeySnapshot(path, data, revision, metadata)
    except ConflictError:
        raise
    except FileNotFoundError:
        raise ValidationError("private-key file was not found") from None
    except PermissionError:
        raise ValidationError("private-key file is not readable") from None
    except (OSError, RuntimeError):
        raise ValidationError("private-key file could not be read") from None


def load_snapshot(snapshot: KeySnapshot, passphrase: str | None = None, *, allow_locked: bool = False) -> dict[str, Any]:
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, ec, ed25519
    metadata = dict(snapshot.metadata)
    password = passphrase.encode("utf-8") if passphrase else None
    if snapshot.data.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
        loader = serialization.load_ssh_private_key
        metadata["format"] = "openssh"
    elif snapshot.data.startswith(b"-----BEGIN "):
        loader = serialization.load_pem_private_key
        metadata["format"] = "pem"
    else:
        raise ValidationError("unsupported private-key format; use OpenSSH or PEM")
    try:
        # User-supplied RSA key validation intentionally remains enabled.
        key = loader(snapshot.data, password=password)
    except TypeError:
        if password is None and allow_locked:
            return {**metadata, "encrypted": True, "status": "passphrase_required"}
        raise ValidationError("private key requires a passphrase, or the supplied passphrase is not applicable") from None
    except ValueError:
        raise ValidationError("private key could not be unlocked or its format is invalid") from None
    except UnsupportedAlgorithm:
        raise ValidationError("private-key type or encryption is not supported") from None
    if isinstance(key, rsa.RSAPrivateKey):
        algorithm, bits = "RSA", key.key_size
    elif isinstance(key, ec.EllipticCurvePrivateKey):
        if key.curve.name not in {"secp256r1", "secp384r1", "secp521r1"}:
            raise ValidationError("private-key curve is not supported by OpenSSH")
        algorithm, bits = "ECDSA", key.key_size
    elif isinstance(key, ed25519.Ed25519PrivateKey):
        algorithm, bits = "ED25519", 256
    else:
        raise ValidationError("private-key type is not supported for SSH")
    fingerprint = "SHA256:" + base64.b64encode(serialization.ssh_key_fingerprint(key.public_key(), hashes.SHA256())).decode().rstrip("=")
    snapshot.confirm()
    return {**metadata, "encrypted": password is not None, "status": "valid",
        "algorithm": algorithm, "bits": bits, "fingerprint": fingerprint}
