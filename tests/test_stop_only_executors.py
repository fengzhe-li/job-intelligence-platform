"""Stop-only executor semantics are keyed on STOP_ONLY_EXECUTORS, not on "airtap".

Every stop-only executor: generic complete() is forbidden, the required-answer
gate applies at enqueue, and a caller-injected executor is refused. Any executor
other than "manual" must be stop-only to be enqueueable at all, so a future
backend cannot fall through to manual completion semantics.

Construction from the approval ledger (AirTapAuthorization) is AirTap-specific
and is covered by test_submission_airtap.py; it is not parameterised here.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jobintel.models.submission import ExecutionResult, ReceiptEvidence
from jobintel.submission import service as service_module
from jobintel.submission.executors import STOP_ONLY_EXECUTORS
from test_submission_service import SYNTHETIC_REQUIRED_ANSWERS, approve, setup, write_synthetic_answer_bank  # noqa: F401


class _InjectedExecutor:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def submit(self, package, *, idempotency_key):
        self.calls += 1
        raise AssertionError("an injected stop-only executor must never be invoked")


def _submitted_result(s, request, executor):
    return ExecutionResult(request["id"], request["package_hash"], s.job.id, s.artifact.id, s.app.application_url,
                           executor, "reported_submitted",
                           (ReceiptEvidence("manual_verified", "I personally saw the successful application confirmation."),),
                           submitted_at=datetime.now(timezone.utc).isoformat())


def _drop_required_answer(root):
    key = "sponsorship.now_or_future_sponsorship_required_answer"
    write_synthetic_answer_bank(root, {k: v for k, v in SYNTHETIC_REQUIRED_ANSWERS.items() if k != key},
                                human_required=(key,))


def test_stop_only_executors_are_exactly_airtap_and_browserbase():
    assert STOP_ONLY_EXECUTORS == frozenset({"airtap", "browserbase", "local_browser"})


@pytest.mark.parametrize("name", sorted(STOP_ONLY_EXECUTORS))
def test_generic_completion_cannot_report_submitted(setup, name):
    s = setup
    s.now[0] = datetime.now(timezone.utc)
    if name in {"browserbase", "local_browser"}:  # no browser needed: a transport reporting a clean stop-only run
        from test_browserbase_service import FakeTransport, ready_result
        s.svc._browserbase_transport = s.svc._local_browser_transport = FakeTransport(ready_result)
    r = s.svc.enqueue(approve(s)["id"], executor=name)
    c = s.svc.claim(r["id"], worker="operator")
    s.svc.execute(r["id"], claim_token=c["claim_token"])
    before = s.svc.store.requests()[r["id"]]["status"]
    with pytest.raises(ValueError, match="generic completion is forbidden"):
        s.svc.complete(r["id"], claim_token=c["claim_token"], result=_submitted_result(s, r, name))
    assert s.svc.store.requests()[r["id"]]["status"] == before != "verified"
    assert s.svc.store.receipts() == []
    assert not s.apps.for_job(s.job.id).is_submitted


@pytest.mark.parametrize("name", sorted(STOP_ONLY_EXECUTORS))
def test_required_answer_gate_applies_at_enqueue(setup, name):
    s = setup
    _drop_required_answer(s.root)
    approval = approve(s)
    with pytest.raises(ValueError, match="require_sponsorship_now_or_future"):
        s.svc.enqueue(approval["id"], executor=name)
    assert s.svc.store.requests() == {}


@pytest.mark.parametrize("name", sorted(STOP_ONLY_EXECUTORS))
def test_caller_injected_executor_is_refused(setup, name):
    s = setup
    r = s.svc.enqueue(approve(s)["id"], executor=name)
    c = s.svc.claim(r["id"], worker="operator")
    injected = _InjectedExecutor(name)  # even with a matching name
    with pytest.raises(ValueError, match="constructed only from a live service approval"):
        s.svc.execute(r["id"], claim_token=c["claim_token"], executor=injected)
    assert injected.calls == 0
    assert not s.svc.store.requests()[r["id"]].get("handoff_started")


def test_non_manual_executor_outside_the_stop_only_set_cannot_be_enqueued(setup):
    s = setup
    approval = approve(s)
    with pytest.raises(ValueError, match="Unknown executor"):
        s.svc.enqueue(approval["id"], executor="unregistered_backend")
    assert s.svc.store.requests() == {}


def test_a_hypothetical_new_stop_only_executor_inherits_every_guard(setup, monkeypatch):
    """A future member added only to STOP_ONLY_EXECUTORS (no construction code yet)
    gets the gate, the injection refusal and the completion ban, and execute fails
    closed before any handoff instead of falling through to the manual executor."""
    s = setup
    monkeypatch.setattr(service_module, "STOP_ONLY_EXECUTORS", STOP_ONLY_EXECUTORS | {"future_backend"})
    r = s.svc.enqueue(approve(s)["id"], executor="future_backend")
    c = s.svc.claim(r["id"], worker="operator")

    with pytest.raises(ValueError, match="constructed only from a live service approval"):
        s.svc.execute(r["id"], claim_token=c["claim_token"], executor=_InjectedExecutor("future_backend"))
    with pytest.raises(ValueError, match="Executor identity does not match request"):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert not s.svc.store.requests()[r["id"]].get("handoff_started")
    with pytest.raises(ValueError, match="generic completion is forbidden"):
        s.svc.complete(r["id"], claim_token=c["claim_token"], result=_submitted_result(s, r, "future_backend"))


def test_hypothetical_stop_only_executor_is_still_required_answer_gated(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(service_module, "STOP_ONLY_EXECUTORS", STOP_ONLY_EXECUTORS | {"future_backend"})
    _drop_required_answer(s.root)
    approval = approve(s)
    with pytest.raises(ValueError, match="require_sponsorship_now_or_future"):
        s.svc.enqueue(approval["id"], executor="future_backend")
    assert s.svc.store.requests() == {}
