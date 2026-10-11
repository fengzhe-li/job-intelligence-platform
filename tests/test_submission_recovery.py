from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from jobintel.models.application import Application
from jobintel.models.taxonomy import ApplicationStatus
from jobintel.storage.application_store import ApplicationStore
from jobintel.submission.service import SubmissionService
from test_submission_service import setup, approve, attempt, completed


def verified(s):
    r, c, res = completed(s)
    receipt = s.svc.verify(r["id"], reviewer="human", confirmed=True)
    return r, receipt


def test_verified_receipt_then_idempotent_reconcile_freezes_cv(setup):
    s = setup
    r, receipt = verified(s)
    assert not s.apps.for_job("j1").is_submitted
    history_before = s.apps.status_history_path.read_bytes()
    assert s.svc.reconcile()[0]["status"] == "submitted"
    app = s.apps.for_job("j1")
    assert app.submitted_cv_version_id == s.artifact.id
    assert app.submission_receipt_id == receipt["receipt_id"]
    after = s.apps.status_history_path.read_bytes()
    assert after.startswith(history_before)
    s.svc.reconcile()
    assert s.apps.status_history_path.read_bytes() == after
    with pytest.raises(ValueError):
        s.apps.attach_cv(app.id, "different", [], [])
    with pytest.raises(ValueError):
        s.apps.update_status(app.id, ApplicationStatus.MATERIALS_READY)


def test_no_receipt_no_application_update(setup):
    completed(setup)
    assert setup.svc.reconcile() == []
    assert not setup.apps.for_job("j1").is_submitted
    with pytest.raises(ValueError):
        setup.apps._apply_verified_receipt("invented")


@pytest.mark.parametrize("operation", ["attach", "applied", "withdraw", "new_approval"])
def test_legacy_paths_cannot_bypass_active_queue(setup, operation):
    r, _, _ = attempt(setup)
    with pytest.raises(ValueError):
        if operation == "attach":
            setup.apps.attach_cv(setup.app.id, "other", [], [])
        elif operation == "applied":
            setup.apps.update_status(setup.app.id, ApplicationStatus.APPLIED)
        elif operation == "withdraw":
            setup.apps.update_status(setup.app.id, ApplicationStatus.WITHDRAWN)
        else:
            approve(setup)
    assert not setup.apps.for_job("j1").is_submitted


@pytest.mark.parametrize("point", ["before_history", "after_history", "after_projection"])
def test_crash_recovery_at_commit_boundaries(setup, point):
    s = setup
    r, receipt = verified(s)
    if point == "before_history":
        with patch.object(ApplicationStore, "_append_status_event", side_effect=OSError("crash")):
            with pytest.raises(OSError):
                s.svc.reconcile()
    elif point == "after_history":
        with patch.object(ApplicationStore, "_write_applications", side_effect=OSError("crash")):
            with pytest.raises(OSError):
                s.svc.reconcile()
        assert not s.apps.for_job("j1").is_submitted
        assert sum(e.marks_submission for e in s.apps.status_history_for(s.app.id)) == 1
    else:
        original = s.svc.store.append
        def fail(kind, *args, **kwargs):
            if kind == "reconciled":
                raise OSError("crash")
            return original(kind, *args, **kwargs)
        with patch.object(s.svc.store, "append", side_effect=fail):
            with pytest.raises(OSError):
                s.svc.reconcile()
        assert s.apps.for_job("j1").is_submitted
    restarted = SubmissionService(s.root, clock=lambda: s.now[0])
    assert restarted.reconcile()[0]["status"] == "submitted"
    assert sum(e.marks_submission for e in s.apps.status_history_for(s.app.id)) == 1
    assert len(s.svc.store.receipts()) == 1


def test_reconcile_does_not_regress_later_interview(setup):
    verified(setup)
    setup.svc.reconcile()
    setup.apps.update_status(setup.app.id, ApplicationStatus.TECHNICAL_INTERVIEW)
    setup.svc.reconcile()
    assert setup.apps.for_job("j1").status == ApplicationStatus.TECHNICAL_INTERVIEW


def test_concurrent_application_creation_stays_unique(setup):
    def create(i):
        return setup.apps.create_application(Application(f"different-{i}", "j1", "Example", "Engineer"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(create, range(8)))
    assert {a.id for a in values} == {setup.app.id}
    assert len(setup.apps.read_applications()) == 1


def test_snapshot_is_separate_and_read_only(setup):
    a = approve(setup)
    path = Path(a["package"]["cv_path"])
    assert path != setup.pdf and path.read_bytes() == setup.pdf.read_bytes()
    assert path.stat().st_mode & 0o222 == 0


def test_tampered_snapshot_invalidates_approval(setup):
    a = approve(setup)
    path = Path(a["package"]["cv_path"])
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        setup.svc.enqueue(a["id"])
    assert setup.svc.store.approvals()[a["id"]]["status"] == "revoked"


def test_executor_exception_is_unknown_and_no_retry(setup):
    class Broken:
        name = "manual"
        def submit(self, package, *, idempotency_key):
            raise TimeoutError("private error")
    r = setup.svc.enqueue(approve(setup)["id"])
    c = setup.svc.claim(r["id"], worker="human")
    with pytest.raises(ValueError, match="interrupted"):
        setup.svc.execute(r["id"], claim_token=c["claim_token"], executor=Broken())
    assert setup.svc.store.requests()[r["id"]]["status"] == "outcome_unknown"
    with pytest.raises(ValueError):
        setup.svc.execute(r["id"], claim_token=c["claim_token"])


def test_manual_legacy_path_without_queue_still_works(setup):
    app = setup.apps.update_status(setup.app.id, ApplicationStatus.APPLIED)
    assert app.is_submitted and app.submitted_cv_version_id is None
    with pytest.raises(ValueError):
        setup.apps.attach_cv(app.id, setup.artifact.id, [], [])


def test_reconcile_finishes_revocation_interrupted_before_queue_update(setup):
    a = approve(setup)
    r = setup.svc.enqueue(a["id"])
    with patch.object(setup.svc, "_request_event", side_effect=OSError("crash")):
        with pytest.raises(OSError):
            setup.svc.revoke(a["id"], reviewer="human", reason="stop")
    assert setup.svc.store.approvals()[a["id"]]["status"] == "revoked"
    restarted = SubmissionService(setup.root, clock=lambda: setup.now[0])
    restarted.reconcile()
    assert restarted.store.requests()[r["id"]]["status"] == "cancelled"
    assert sum(e["kind"] == "revoked" for e in restarted.store.events()) == 1


def test_unknown_outcome_cannot_be_downgraded_to_retryable_failure(setup):
    from test_submission_service import result
    r, c, _ = attempt(setup)
    unknown = replace(result(setup, r), outcome="outcome_unknown")
    setup.svc.complete(r["id"], claim_token=c["claim_token"], result=unknown)
    failed = replace(unknown, outcome="failed")
    setup.svc.complete(r["id"], claim_token=c["claim_token"], result=failed)
    assert setup.svc.store.requests()[r["id"]]["status"] == "outcome_unknown"
    with pytest.raises(ValueError):
        approve(setup)
