"""Cross-platform regression for failure-only Windows process-role evidence."""
from __future__ import annotations

import ctypes
import json
from ctypes import wintypes

import pytest

import windows_probe_evidence as evidence


class FakeKernel:
    def __init__(self, monkeypatch, *, images=None):
        self.images = images or {
            100001: r"C:\private-account\request-token\ssh.exe",
            100002: r"C:\private-account\request-token\PYTHON.EXE",
            100003: r"C:\private-account\request-token\powershell.exe",
            100004: r"C:\private-account\request-token\cmd.exe",
            100005: r"C:\private-account\request-token\unlisted-secret.exe",
        }
        self.job = object()
        self.last_error = 0
        self.handles = {}
        self.closed = []
        self.capacities = []
        self.image_reads = []
        monkeypatch.setattr(ctypes, "get_last_error", lambda: self.last_error, raising=False)

    def QueryInformationJobObject(self, job, kind, pointer, size, returned):
        assert job is self.job and kind == 3 and returned is None
        information = pointer._obj
        assert size == ctypes.sizeof(information)
        capacity = len(information.ProcessIdList)
        self.capacities.append(capacity)
        information.NumberOfAssignedProcesses = len(self.images)
        information.NumberOfProcessIdsInList = min(capacity, len(self.images))
        for index, identifier in enumerate(tuple(self.images)[:capacity]):
            information.ProcessIdList[index] = identifier
        self.last_error = 234 if len(self.images) > capacity else 0
        return not self.last_error

    def OpenProcess(self, access, inherit, identifier):
        assert access == 0x1000 and inherit is False
        handle = len(self.handles) + 200001
        self.handles[handle] = identifier
        return handle

    def IsProcessInJob(self, handle, job, owned):
        assert job is self.job and handle in self.handles
        owned._obj.value = True
        return True

    def QueryFullProcessImageNameW(self, handle, flags, image, length):
        assert flags == 0 and length._obj.value == 32768
        identifier = self.handles[handle]
        self.image_reads.append(identifier)
        image.value = self.images[identifier]
        length._obj.value = len(image.value)
        return True

    def CloseHandle(self, handle):
        assert handle in self.handles and handle not in self.closed
        self.closed.append(handle)
        return True


def assert_released(kernel):
    assert set(kernel.handles) == set(kernel.closed)
    assert kernel.job not in kernel.closed


def test_snapshot_returns_only_fixed_role_counts_and_no_private_data(monkeypatch):
    kernel = FakeKernel(monkeypatch)
    result = evidence.snapshot_job_process_families(kernel, kernel.job)
    assert result == {"families": {"ssh": 1, "python": 1, "powershell": 1, "cmd": 1,
                                   "other": 1, "unknown": 0}, "errors": [], "complete": True}
    encoded = json.dumps(result)
    for private in ("private-account", "request-token", "unlisted-secret", "100001", "200001", "C:"):
        assert private not in encoded
    assert_released(kernel)


def test_snapshot_excludes_reused_identifier_outside_exact_job(monkeypatch):
    class Kernel(FakeKernel):
        def IsProcessInJob(self, handle, job, owned):
            super().IsProcessInJob(handle, job, owned)
            owned._obj.value = self.handles[handle] != 100002
            return True

    kernel = Kernel(monkeypatch)
    result = evidence.snapshot_job_process_families(kernel, kernel.job)
    assert result["families"]["python"] == 0
    assert 100002 not in kernel.image_reads
    assert result["errors"] == ["membership_changed"] and result["complete"] is False
    assert_released(kernel)


@pytest.mark.parametrize("operation", ["open", "membership", "image"])
def test_access_failures_are_fixed_codes_and_release_handles(monkeypatch, operation):
    class Kernel(FakeKernel):
        def OpenProcess(self, *args):
            return 0 if operation == "open" else super().OpenProcess(*args)

        def IsProcessInJob(self, *args):
            return False if operation == "membership" else super().IsProcessInJob(*args)

        def QueryFullProcessImageNameW(self, *args):
            return False if operation == "image" else super().QueryFullProcessImageNameW(*args)

    kernel = Kernel(monkeypatch)
    result = evidence.snapshot_job_process_families(kernel, kernel.job)
    expected = {"open": "process_open_failed", "membership": "membership_query_failed",
                "image": "image_query_failed"}[operation]
    assert result["errors"] == [expected] and result["complete"] is False
    assert result["families"]["unknown"] == (5 if operation == "image" else 0)
    assert_released(kernel)


def test_process_list_resizes_only_within_1024_member_limit(monkeypatch):
    kernel = FakeKernel(monkeypatch, images={index: r"C:\isolated\ssh.exe" for index in range(1, 38)})
    result = evidence.snapshot_job_process_families(kernel, kernel.job)
    assert kernel.capacities == [32, 64]
    assert result["complete"] and result["families"]["ssh"] == 37
    assert_released(kernel)


def test_oversized_job_never_opens_any_process(monkeypatch):
    kernel = FakeKernel(monkeypatch, images={index: "private.exe" for index in range(1, 1026)})
    result = evidence.snapshot_job_process_families(kernel, kernel.job)
    assert kernel.capacities == [32] and not kernel.handles
    assert result["errors"] == ["process_limit"] and result["complete"] is False


def test_deadline_stops_work_and_closes_already_opened_handle(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(evidence.time, "monotonic", lambda: clock[0])

    class Kernel(FakeKernel):
        def IsProcessInJob(self, *args):
            result = super().IsProcessInJob(*args)
            clock[0] = .15
            return result

    kernel = Kernel(monkeypatch)
    result = evidence.snapshot_job_process_families(kernel, kernel.job)
    assert len(kernel.handles) == 1 and not kernel.image_reads
    assert result["errors"] == ["budget_exhausted"] and result["complete"] is False
    assert result["families"]["unknown"] == 1
    assert_released(kernel)


@pytest.mark.parametrize("operation", ["query", "membership", "image", "close"])
def test_private_exception_details_never_leave_snapshot(monkeypatch, operation):
    class Kernel(FakeKernel):
        def QueryInformationJobObject(self, *args):
            if operation == "query":
                raise OSError("private-account request-token 100001 C:\\private-path")
            return super().QueryInformationJobObject(*args)

        def IsProcessInJob(self, *args):
            if operation == "membership":
                raise OSError("private-account request-token 100001 C:\\private-path")
            return super().IsProcessInJob(*args)

        def QueryFullProcessImageNameW(self, *args):
            if operation == "image":
                raise OSError("private-account request-token 100001 C:\\private-path")
            return super().QueryFullProcessImageNameW(*args)

        def CloseHandle(self, *args):
            result = super().CloseHandle(*args)
            if operation == "close":
                raise OSError("private-account request-token 100001 C:\\private-path")
            return result

    kernel = Kernel(monkeypatch)
    result = evidence.snapshot_job_process_families(kernel, kernel.job)
    expected = {"query": "job_query_failed", "membership": "membership_query_failed",
                "image": "image_query_failed", "close": "handle_close_failed"}[operation]
    assert result["errors"] == [expected] and result["complete"] is False
    assert "private" not in json.dumps(result) and "100001" not in json.dumps(result)
    assert_released(kernel)


def test_null_job_cannot_fall_back_to_the_current_process_job(monkeypatch):
    kernel = FakeKernel(monkeypatch)
    result = evidence.snapshot_job_process_families(kernel, None)
    assert result["errors"] == ["invalid_job"] and not kernel.capacities and not kernel.handles


def test_native_ctypes_membership_binding_still_accepts_production_cleanup(monkeypatch):
    from ssh_server_manager.credential_probe import _terminate_windows_job

    class Kernel(FakeKernel):
        def __init__(self, monkeypatch):
            super().__init__(monkeypatch)
            self.job = 0x1234
            self.terminated = False
            self.waited = []
            signature = ctypes.CFUNCTYPE(
                wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))

            def membership(handle, job, owned):
                assert job == self.job and handle in self.handles
                owned.contents.value = True
                return True

            self.IsProcessInJob = signature(membership)

        def QueryInformationJobObject(self, job, kind, information, size, returned):
            if kind == 1:
                assert self.terminated and set(self.handles) == set(self.closed)
                information._obj.ActiveProcesses = 0
                return True
            return super().QueryInformationJobObject(job, kind, information, size, returned)

        def OpenProcess(self, access, inherit, identifier):
            assert access in (0x1000, 0x00101000)
            return super().OpenProcess(0x1000, inherit, identifier)

        def TerminateJobObject(self, job, status):
            assert job == self.job and status == 1
            self.terminated = True
            return True

        def WaitForSingleObject(self, handle, milliseconds):
            assert self.terminated and handle not in self.closed
            self.waited.append(handle)
            return 0

    kernel = Kernel(monkeypatch)
    result = evidence.snapshot_job_process_families(kernel, kernel.job)
    assert result["complete"]
    assert kernel.IsProcessInJob.argtypes[-1] is ctypes.POINTER(wintypes.BOOL)
    # This calls the same bound native function with production's BOOL pointer.
    # A c_int32/c_long mismatch raises ctypes.ArgumentError even at equal width.
    _terminate_windows_job(kernel, kernel.job)
    assert kernel.terminated and len(kernel.waited) == 5
    assert_released(kernel)
