"""Human-attested submission after a stop-only handoff (synthetic, offline; 2026-10-13).

The stop-only runner never clicks final Submit. When the named human does, after the handoff,
`record_human_submission` records a human-attested receipt for exactly the approved package and CV;
`reconcile` then marks the application APPLIED with that CV. Every precondition fails closed.
"""
from __future__ import annotations

import pytest

from jobintel.models.taxonomy import ApplicationStatus
from jobintel.submission.service import HUMAN_SUBMISSION_STRATEGY
from test_browserbase_service import kinds, status
from test_submission_close_not_submitted import close, forge, released, reviewable

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

REVIEWER = "Sample Reviewer"
CONFIRMATION = "Your application successfully reached us! We'll contact you if there are next steps."


def cv_of(s, r):
    return s.svc.store.approvals()[r["approval_id"]]["package"]["cv_artifact_id"]


def record(s, r, **changes):
    args = dict(reviewer=REVIEWER, confirmed_submitted=True, submitted_cv=cv_of(s, r),
                confirmation_text=CONFIRMATION)
    args.update(changes)
    return s.svc.record_human_submission(r["id"], **args)


def test_released_handoff_is_recorded_as_a_human_attested_receipt_and_reconciles_to_applied(tmp_path):
    s, r, _ = released(tmp_path)
    receipt = record(s, r)
    assert receipt["verification_strategy"] == HUMAN_SUBMISSION_STRATEGY
    assert (receipt["submitted"], receipt["final_submit_clicked_by"], receipt["verified_by"]) == (True, "human", REVIEWER)
    assert receipt["cv_artifact_id"] == cv_of(s, r) and receipt["package_hash"] == r["package_hash"]
    assert receipt["evidence"][0]["kind"] == "manual_verified" and receipt["evidence"][0]["value"] == CONFIRMATION
    assert status(s, r) == "verified"
    assert s.apps.for_job("j1").status == ApplicationStatus.DISCOVERED  # recording never touches applications
    out = s.svc.reconcile()
    assert [o["status"] for o in out if o["request_id"] == r["id"]] == ["submitted"]
    app = s.apps.for_job("j1")
    assert app.status == ApplicationStatus.APPLIED and app.submitted_cv_version_id == cv_of(s, r)
    assert app.submission_receipt_id == "receipt:" + r["id"]
    assert status(s, r) == "submitted"
    # Idempotent: the same confirmation returns the same receipt; reconcile adds no second submission.
    assert record(s, r) == receipt
    s.svc.reconcile()
    history = [e for e in s.apps.status_history_for(app.id) if e.marks_submission]
    assert len(history) == 1


def test_an_active_handoff_must_be_released_first(tmp_path):
    s, r, _ = reviewable(tmp_path)
    with pytest.raises(ValueError, match="release it"):
        record(s, r)
    assert status(s, r) == "needs_human_input"


@pytest.mark.parametrize("changes,match", [
    (dict(confirmed_submitted=False), "confirmed-submitted"),
    (dict(confirmed_submitted="yes"), "confirmed-submitted"),
    (dict(reviewer=" "), "named reviewer"),
    (dict(confirmation_text=""), "confirmation text"),
    (dict(confirmation_text="x" * 2001), "confirmation text"),
    (dict(submitted_cv="cv-ffffffffffff"), "sealed CV"),
])
def test_missing_or_wrong_human_input_is_refused(tmp_path, changes, match):
    s, r, _ = released(tmp_path)
    with pytest.raises(ValueError, match=match):
        record(s, r, **changes)
    assert status(s, r) == "needs_human_input" and s.svc.store.receipts() == []


def test_a_request_closed_as_not_submitted_is_refused(tmp_path):
    s, r, _ = released(tmp_path)
    close(s, r)
    with pytest.raises(ValueError):
        record(s, r)
    assert s.svc.store.receipts() == []


def test_a_runner_final_submit_click_is_refused(tmp_path):
    s, r, _ = released(tmp_path)
    forge(s, r, "browserbase_result", {"final_submit_clicked": True, "evidence": []})
    with pytest.raises(ValueError, match="final-submit click"):
        record(s, r)


def test_a_different_second_confirmation_is_refused(tmp_path):
    s, r, _ = released(tmp_path)
    record(s, r)
    with pytest.raises(ValueError, match="different receipt"):
        record(s, r, reviewer="Someone Else")
    assert len(s.svc.store.receipts()) == 1
    assert "closed_not_submitted" not in kinds(s, r)
