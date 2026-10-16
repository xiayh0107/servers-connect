from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


HOST_KEY_CONFIRM_ENV = "SSM_ASKPASS_HOST_KEY_CONFIRM"
_HOST_KEY_FINGERPRINT = re.compile(
    r"^(?:RSA|DSA|ECDSA|ED25519|ECDSA-SK|ED25519-SK) key fingerprint is:? "
    r"(SHA256:[A-Za-z0-9+/]{43}|MD5:(?:[0-9a-f]{2}:){15}[0-9a-f]{2})\.?$",
    re.MULTILINE,
)


def _is_host_key_prompt(prompt: str) -> bool:
    lowered = prompt.casefold()
    return ("yes/no" in lowered or "continue connecting" in lowered
            or lowered.startswith("the authenticity of host ")
            or "remote host identification has changed" in lowered
            or "revoked host key" in lowered)


def _host_key_fingerprint(prompt: str) -> str | None:
    """Recognize a complete OpenSSH first-host prompt, never a bare yes/no."""
    if len(prompt) > 16384 or any(not char.isprintable() and char not in "\r\n\t" for char in prompt):
        return None
    normalized = prompt.replace("\r\n", "\n")
    if "\r" in normalized or not re.match(
            r"\AThe authenticity of host '[^\n]+' can't be established(?:\.|\n"
            r"but keys of different type are already known for this host\.)\n", normalized):
        return None
    if not normalized.endswith((
            "\nAre you sure you want to continue connecting (yes/no/[fingerprint])? ",
            "\nAre you sure you want to continue connecting (yes/no)? ")):
        return None
    lowered = normalized.casefold()
    if "remote host identification has changed" in lowered or "revoked host key" in lowered:
        return None
    fingerprints = _HOST_KEY_FINGERPRINT.findall(normalized)
    if len(fingerprints) != 1 or lowered.count("key fingerprint is") != 1:
        return None
    return fingerprints[0]


def _read_posix_confirmation(prompt: str) -> str | None:
    descriptor = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    try:
        if not os.isatty(descriptor):
            return None
        content = prompt.encode("utf-8")
        while content:
            written = os.write(descriptor, content)
            if written <= 0:
                return None
            content = content[written:]
        response = bytearray()
        while len(response) < 1024:
            char = os.read(descriptor, 1)
            if not char:
                return None
            response.extend(char)
            if char == b"\n":
                return response.decode("ascii", "strict")
        return None
    finally:
        os.close(descriptor)


def _windows_console_kernel():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _read_windows_confirmation(prompt: str) -> str | None:
    # AskPass stdout is SSH's private response pipe, not the user's console.
    # CONIN$/CONOUT$ address the attached console even with redirected stdio.
    import ctypes
    from ctypes import wintypes

    kernel = _windows_console_kernel()
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetConsoleMode.restype = wintypes.BOOL
    for name in ("ReadConsoleW", "WriteConsoleW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    handles = []
    try:
        for device, access in (("CONIN$", 0x80000000), ("CONOUT$", 0xC0000000)):
            handle = kernel.CreateFileW(device, access, 3, None, 3, 0, None)
            if handle in (None, 0, invalid):
                return None
            handles.append(handle)
            mode = wintypes.DWORD()
            if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                return None
            if device == "CONIN$" and not mode.value & 2:  # ENABLE_LINE_INPUT
                return None
        count = wintypes.DWORD()
        units = len(prompt.encode("utf-16-le")) // 2
        if (not kernel.WriteConsoleW(handles[1], ctypes.c_wchar_p(prompt), units, ctypes.byref(count), None)
                or count.value != units):
            return None
        buffer = ctypes.create_unicode_buffer(1024)
        if not kernel.ReadConsoleW(handles[0], buffer, len(buffer) - 1, ctypes.byref(count), None):
            return None
        if not 0 < count.value < len(buffer) or not buffer[:count.value].endswith("\n"):
            return None
        return buffer[:count.value]
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def _confirm_host_key(prompt: str) -> str | None:
    if os.environ.get(HOST_KEY_CONFIRM_ENV) != "terminal":
        return None
    fingerprint = _host_key_fingerprint(prompt)
    if fingerprint is None:
        return None
    try:
        response = (_read_windows_confirmation(prompt) if os.name == "nt"
                    else _read_posix_confirmation(prompt))
    except (OSError, UnicodeError, KeyboardInterrupt):
        return None
    if response is None:
        return None
    answer = response.strip(" \t\r\n")
    return answer if answer in ("yes", "no", fingerprint) else None


def load_descriptors() -> list[dict[str, Any]]:
    try:
        value = json.loads(os.environ.get("SSM_ASKPASS_MAP", "[]"))
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def choose_descriptor(prompt: str, descriptors: list[dict[str, Any]]) -> dict[str, Any] | None:
    lowered = prompt.casefold()
    if _is_host_key_prompt(prompt):
        return None
    passphrases = [item for item in descriptors if item.get("slot") == "passphrase"]
    if "passphrase" in lowered:
        for item in passphrases:
            key_path = str(item.get("key_path", ""))
            if key_path.casefold() in lowered or Path(key_path).name.casefold() in lowered:
                return item
        return passphrases[0] if len(passphrases) == 1 else None
    passwords = [item for item in descriptors if item.get("slot") == "password"]
    if "password" in lowered:
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in passwords:
            score = sum(
                bool(value and str(value).casefold() in lowered)
                for value in (item.get("username"), item.get("hostname"), item.get("alias"))
            )
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if scored and (scored[0][0] > 0 or len(scored) == 1):
            return scored[0][1]
    return None


def main() -> int:
    if os.environ.get("SSM_CREDENTIAL_PROBE_ENDPOINT"):
        from .credential_probe_helper import probe_askpass

        return probe_askpass(sys.argv[1] if len(sys.argv) > 1 else "")
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if _is_host_key_prompt(prompt):
        answer = _confirm_host_key(prompt)
        if answer is None:
            return 1
        sys.stdout.write(answer)
        return 0
    item = choose_descriptor(prompt, load_descriptors())
    if not item:
        return 1
    try:
        from .vault import get_vault

        secret = get_vault().get_secret(item["credential_id"], item["slot"])
    except Exception:
        return 1
    if not secret:
        return 1
    sys.stdout.write(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

