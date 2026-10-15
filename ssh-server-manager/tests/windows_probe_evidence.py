"""Bounded, sanitized diagnostics for a failed fixture's private Windows job.

Call only after a test has decided that authentication callbacks are missing,
before terminating its own job. This is diagnostic evidence, never an
authentication decision. No process identifier or image path leaves this module.
"""
from __future__ import annotations

import ctypes
import ntpath
import time
from ctypes import wintypes
from typing import Any


_SCAN_BUDGET = .15
_MAX_PROCESSES = 1024
_IMAGE_CAPACITY = 32768
_FAMILIES = ("ssh", "python", "powershell", "cmd", "other", "unknown")
_IMAGE_FAMILIES = {
    "ssh.exe": "ssh",
    "python.exe": "python",
    "pythonw.exe": "python",
    "powershell.exe": "powershell",
    "pwsh.exe": "powershell",
    "cmd.exe": "cmd",
}


def _bind(kernel: Any) -> None:
    """Bind native functions without imposing ctypes attributes on fake methods."""
    # This is the same kernel instance that production cleanup subsequently
    # uses. Match its ctypes identities, not just their native Windows widths.
    pointer = wintypes.HANDLE
    dword = wintypes.DWORD
    boolean = wintypes.BOOL
    signatures = {
        "QueryInformationJobObject": ([pointer, ctypes.c_int, pointer, dword,
                                       ctypes.POINTER(dword)], boolean),
        "OpenProcess": ([dword, boolean, dword], pointer),
        "IsProcessInJob": ([pointer, pointer, ctypes.POINTER(boolean)], boolean),
        "QueryFullProcessImageNameW": ([pointer, dword, wintypes.LPWSTR,
                                       ctypes.POINTER(dword)], boolean),
        "CloseHandle": ([pointer], boolean),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        if isinstance(function, ctypes._CFuncPtr):
            function.argtypes = arguments
            function.restype = result


def snapshot_job_process_families(kernel: Any, job: Any) -> dict[str, Any]:
    """Return fixed family counts/error codes using only the supplied private job.

    There are no waits, subprocesses, command-line/environment reads, or global
    process enumeration. A 150 ms deadline bounds work between native API calls;
    a native call itself cannot be preempted. Temporary handles are always closed,
    including after the deadline. Image basenames describe roles, not trust.
    """
    families = dict.fromkeys(_FAMILIES, 0)
    errors: list[str] = []
    result = {"families": families, "errors": errors, "complete": False}
    deadline = time.monotonic() + _SCAN_BUDGET

    def error(code: str) -> None:
        if code not in errors:
            errors.append(code)

    def within_budget() -> bool:
        if time.monotonic() >= deadline:
            error("budget_exhausted")
            return False
        return True

    # NULL would query the caller's job, rather than the failed request's job.
    if not job:
        error("invalid_job")
        return result

    operation = "api_unavailable"
    try:
        _bind(kernel)
        capacity = 32
        while within_budget():
            class ProcessList(ctypes.Structure):
                _fields_ = [("NumberOfAssignedProcesses", ctypes.c_uint32),
                            ("NumberOfProcessIdsInList", ctypes.c_uint32),
                            ("ProcessIdList", ctypes.c_size_t * capacity)]

            processes = ProcessList()
            operation = "job_query_failed"
            queried = kernel.QueryInformationJobObject(
                job, 3, ctypes.byref(processes), ctypes.sizeof(processes), None)
            assigned = processes.NumberOfAssignedProcesses
            returned = processes.NumberOfProcessIdsInList
            if not queried and getattr(ctypes, "get_last_error", lambda: 0)() != 234:
                error("job_query_failed")
                return result
            if assigned > _MAX_PROCESSES:
                error("process_limit")
                return result
            if returned > capacity or returned > assigned:
                error("invalid_process_list")
                return result
            if not queried or returned < assigned:
                if capacity == _MAX_PROCESSES:
                    error("incomplete_process_list")
                    return result
                capacity = min(_MAX_PROCESSES, max(capacity * 2, assigned))
                continue
            identifiers = [int(processes.ProcessIdList[index]) for index in range(returned)]
            if any(not 0 < identifier <= 0xffffffff for identifier in identifiers) or \
                    len(set(identifiers)) != len(identifiers):
                error("invalid_process_list")
                return result
            break
        else:
            return result

        for identifier in identifiers:
            if not within_budget():
                break
            operation = "process_open_failed"
            # QUERY_LIMITED_INFORMATION only: no SYNCHRONIZE or VM/command access.
            handle = kernel.OpenProcess(0x1000, False, identifier)
            if not handle:
                error("process_open_failed")
                continue
            try:
                if not within_budget():
                    break
                owned = wintypes.BOOL()
                operation = "membership_query_failed"
                if not kernel.IsProcessInJob(handle, job, ctypes.byref(owned)):
                    error("membership_query_failed")
                    continue
                if not owned.value:
                    # The identifier can have been reused since enumeration.
                    # Do not read or count an image outside this exact job.
                    error("membership_changed")
                    continue
                if not within_budget():
                    families["unknown"] += 1
                    break
                image = ctypes.create_unicode_buffer(_IMAGE_CAPACITY)
                length = wintypes.DWORD(_IMAGE_CAPACITY)
                operation = "image_query_failed"
                if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                    families["unknown"] += 1
                    error("image_query_failed")
                    continue
                if not 0 < length.value < _IMAGE_CAPACITY:
                    families["unknown"] += 1
                    error("invalid_image_length")
                    continue
                name = ntpath.basename(image[:length.value]).casefold()
                families[_IMAGE_FAMILIES.get(name, "other")] += 1
            finally:
                try:
                    if not kernel.CloseHandle(handle):
                        error("handle_close_failed")
                except Exception:
                    error("handle_close_failed")
        # Account for time spent in the last native call without doing more work.
        within_budget()
        result["complete"] = not errors
    except Exception:
        # Native/fake failures must never expose an exception containing a path,
        # process identifier, argument, or request environment value.
        error(operation)
    return result
