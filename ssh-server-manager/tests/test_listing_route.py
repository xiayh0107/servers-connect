"""The listing route's bookkeeping: verdicts, back-off, expiring, single-flight.

These are the facts that decide whether a folder is listed through the shell or
through SFTP. They used to be two dictionaries on the runner with no lifetime,
which meant one transient failure could pin a host to the slow route for the
life of the process.

No host and no subprocess is involved: the route is policy, and the probe is a
callable the test controls.
"""
from __future__ import annotations

import threading

import pytest

from ssh_server_manager.remote_listing import (
    NAMESPACE_VERDICT_TTL_SECONDS, UNAVAILABLE_BACKOFF_SECONDS, ListingRoute, host_identity,
)


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def box(**changes):
    return {"id": "srv-1", "alias": "box", "hostname": "box.example", "port": 22,
            "username": "alice", "last_test_status": "ok", "last_test_error_code": None, **changes}


def counter(answer):
    """A probe that records each call."""
    calls = []

    def probe():
        calls.append(answer)
        return answer

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


def test_one_verdict_answers_every_folder_until_it_expires():
    clock = Clock()
    route = ListingRoute(clock=clock)
    probe = counter(True)
    for _ in range(5):
        assert route.namespace_matches(box(), probe) is True
    assert len(probe.calls) == 1
    clock.advance(NAMESPACE_VERDICT_TTL_SECONDS - 1)
    assert route.namespace_matches(box(), probe) is True
    assert len(probe.calls) == 1
    # The verdict is not permanent: an expired one is asked again, so a jail
    # that was lifted (or added) is noticed without restarting the app.
    clock.advance(2)
    assert route.namespace_matches(box(), probe) is True
    assert len(probe.calls) == 2


def test_a_repointed_host_or_user_is_a_different_question():
    clock = Clock()
    route = ListingRoute(clock=clock)
    probe = counter(True)
    assert route.namespace_matches(box(), probe) is True
    # The home directory is part of what was proven, so these invalidate it.
    for changes in ({"hostname": "other.example"}, {"port": 2222}, {"username": "bob"}):
        assert route.namespace_matches(box(**changes), probe) is True
    assert len(probe.calls) == 4


def test_a_definitive_mismatch_suspends_the_route_without_retrying_every_folder():
    clock = Clock()
    route = ListingRoute(clock=clock)
    probe = counter(False)
    assert route.namespace_matches(box(), probe) is False
    assert len(probe.calls) == 1
    # Knowing the answer means not even attempting the discarded exec.
    assert route.may_try(box()) is False
    for _ in range(3):
        assert route.namespace_matches(box(), probe) is False
    assert len(probe.calls) == 1
    clock.advance(NAMESPACE_VERDICT_TTL_SECONDS + 1)
    assert route.may_try(box()) is True


def test_an_unanswered_probe_is_deferred_but_never_remembered_as_a_verdict():
    clock = Clock()
    route = ListingRoute(clock=clock)
    probe = counter(None)
    # A timeout says something about the attempt, not about the host: it must
    # not become a cached "no".
    assert route.namespace_matches(box(), probe) is False
    assert route.may_try(box()) is False
    clock.advance(UNAVAILABLE_BACKOFF_SECONDS + 1)
    assert route.may_try(box()) is True
    assert route.namespace_matches(box(), probe) is False
    assert len(probe.calls) == 2


def test_a_recovered_host_is_retried_after_the_backoff_window():
    clock = Clock()
    route = ListingRoute(clock=clock)
    route.defer(box())
    assert route.may_try(box()) is False
    clock.advance(UNAVAILABLE_BACKOFF_SECONDS + 1)
    # Back-off expires on its own; no restart and no manual reset.
    assert route.may_try(box()) is True


def test_a_definitive_answer_clears_an_earlier_deferral():
    route = ListingRoute(clock=Clock())
    route.defer(box())
    assert route.may_try(box()) is False
    route.record(box(), matched=True)
    assert route.may_try(box()) is True


@pytest.mark.parametrize("code", ["ssh-failed", "connection-refused", "auth_failed", "ssh-not-found", "unreachable"])
def test_a_recorded_transport_failure_skips_the_route_without_remembering_anything(code):
    route = ListingRoute(clock=Clock())
    failed = box(last_test_status="failed", last_test_error_code=code)
    assert route.may_try(failed) is False
    # The next successful test changes the row, and the route is available
    # again immediately — the row is the only state involved.
    assert route.may_try(box()) is True


def test_other_failures_do_not_disable_the_route():
    route = ListingRoute(clock=Clock())
    # A remote "not found" was answered by the shell, so it says nothing about
    # whether the fast listing works.
    assert route.may_try(box(last_test_status="failed", last_test_error_code="timeout")) is True
    assert route.may_try(box(last_test_status="failed", last_test_error_code="not_found")) is True


def test_concurrent_folders_pay_for_one_probe():
    route = ListingRoute(clock=Clock())
    started = threading.Event()
    release = threading.Event()
    calls = []

    def probe():
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        return True

    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(route.namespace_matches(box(), probe)))
               for _ in range(8)]
    threads[0].start()
    started.wait(timeout=5)
    for thread in threads[1:]:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(timeout=10)
    # Eight concurrent requests, one SFTP+shell handshake.
    assert calls == [1]
    assert results == [True] * 8


def test_forget_drops_one_host_or_all_of_them():
    clock = Clock()
    route = ListingRoute(clock=clock)
    probe = counter(False)
    route.namespace_matches(box(), probe)
    route.namespace_matches(box(id="srv-2"), probe)
    assert route.may_try(box()) is False
    route.forget("srv-1")
    assert route.may_try(box()) is True
    assert route.may_try(box(id="srv-2")) is False
    route.forget()
    assert route.may_try(box(id="srv-2")) is True


def test_a_verdict_records_the_identity_it_was_proven_for():
    server = box()
    assert host_identity(server) == ("box.example", 22, "alice")
    # A row with missing values still yields a comparable identity.
    assert host_identity({"id": "x"}) == ("", 0, "")
