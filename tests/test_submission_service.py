from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from jobintel.models.application import Application
from jobintel.models.candidate import CandidateProfile
from jobintel.models.job import Job
from jobintel.models.submission import ExecutionResult, ReceiptEvidence
from jobintel.models.taxonomy import ApplicationStatus, ReviewGateStatus
from jobintel.profile_ingestion import _write_profile
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.submission.answers import AnswerBank
from jobintel.submission.service import SubmissionService
from test_cv_artifact_store import _artifact


# Synthetic values for the answers every AirTap handoff requires. The handoff
# never fills a missing answer itself, so fixtures must supply their own.
from synthetic_answer_bank import SYNTHETIC_REQUIRED_ANSWERS, write_synthetic_answer_bank  # noqa: F401  (re-exported)


@pytest.fixture
def setup(tmp_path, synthetic_answer_bank):
    now = [datetime(2026, 9, 28, tzinfo=timezone.utc)]
    candidate = CandidateProfile("candidate-1", "Alex Example", 2026, email="alex@example.com", phone="07000000000")
    _write_profile(tmp_path, candidate)
    write_synthetic_answer_bank(tmp_path)
    job = Job(id="j1", company="Example", title="Graduate Engineer", description="Graduate engineer role.", locations=[], source_observations=[])
    LocalJobStore(tmp_path).write_jobs([job])
    pdf = tmp_path / "cv_artifacts/pdfs/cv-0123456789ab.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 test fixture")
    artifact = replace(_artifact("cv-0123456789ab", "j1"), jd_snapshot=job.description,
                       pdf_path=str(pdf), pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest())
    cvs = CVArtifactStore(tmp_path)
    cvs.save(artifact)
    apps = ApplicationStore(tmp_path)
    app = apps.create_application(Application("app:j1", "j1", "Example", "Graduate Engineer", application_url="https://example.com/jobs/j1"))
    apps.attach_cv(app.id, artifact.id, artifact.selected_project_ids, artifact.evidence_ids_used)
    svc = SubmissionService(tmp_path, clock=lambda: now[0])
    return SimpleNamespace(root=tmp_path, svc=svc, apps=apps, app=app, candidate=candidate, job=job, pdf=pdf, artifact=artifact, cvs=cvs, now=now)


def approve(s, **kwargs):
    package = s.svc.preview(s.app.id)
    return s.svc.approve(s.app.id, package_hash=package.package_hash, reviewer="human", reviewed=True, **kwargs)


def attempt(s):
    a = approve(s)
    r = s.svc.enqueue(a["id"])
    c = s.svc.claim(r["id"], worker="human")
    handoff = s.svc.execute(r["id"], claim_token=c["claim_token"])
    return r, c, handoff


def result(s, request, evidence=None, **changes):
    return ExecutionResult(request["id"], request["package_hash"], s.job.id, s.artifact.id,
                           s.app.application_url, "manual", "reported_submitted",
                           tuple(evidence if evidence is not None else [ReceiptEvidence("manual_verified", "I personally saw the successful application confirmation.")]),
                           submitted_at=s.now[0].isoformat(), **changes)


def completed(s, evidence=None):
    r, c, _ = attempt(s)
    res = result(s, r, evidence)
    s.svc.complete(r["id"], claim_token=c["claim_token"], result=res)
    return r, c, res


def test_preview_is_deterministic_and_approval_package_immutable(setup):
    s = setup
    p = s.svc.preview(s.app.id)
    assert p.package_hash == s.svc.preview(s.app.id).package_hash
    a = approve(s)
    assert a["human_reviewed"] and a["package_hash"] == p.package_hash
    assert s.apps.for_job("j1").applied_at is None


@pytest.mark.parametrize("reviewed,reviewer", [(False, "human"), (True, "")])
def test_auto_prepare_needs_human_review(setup, reviewed, reviewer):
    p = setup.svc.preview(setup.app.id)
    with pytest.raises(ValueError):
        setup.svc.approve(setup.app.id, package_hash=p.package_hash, reviewed=reviewed, reviewer=reviewer)


def test_wrong_review_hash_rejected(setup):
    with pytest.raises(ValueError):
        setup.svc.approve(setup.app.id, package_hash="wrong", reviewer="human", reviewed=True)


def change_artifact(s, **changes):
    artifact = replace(s.artifact, **changes)
    data = asdict(artifact)
    data["generated_at"] = artifact.generated_at.isoformat()
    data["review_status"] = artifact.review_status.value
    s.cvs.path.write_text(json.dumps(data) + "\n")


def test_block_gate_cannot_be_approved(setup):
    change_artifact(setup, review_status=ReviewGateStatus.BLOCK_AUTO_SUBMISSION)
    with pytest.raises(ValueError, match="BLOCK_AUTO"):
        setup.svc.preview(setup.app.id)


def test_review_required_needs_reason_acknowledgement(setup):
    change_artifact(setup, review_status=ReviewGateStatus.REVIEW_REQUIRED, review_reasons=["Check eligibility manually"])
    with pytest.raises(ValueError):
        approve(setup)
    assert approve(setup, acknowledge_reasons=True)["acknowledged_reasons"] == ["Check eligibility manually"]


@pytest.mark.parametrize("change", ["pdf", "cv_id", "url", "answer", "profile", "jd", "gate", "config"])
def test_changes_invalidate_approval_append_only(setup, change):
    s = setup
    a = approve(s)
    before = (s.root / "submissions/events.jsonl").read_bytes()
    if change == "pdf":
        s.pdf.write_bytes(b"changed")
    elif change == "cv_id":
        s.apps.attach_cv(s.app.id, "cv-ffffffffffff", [], [])
    elif change == "url":
        s.apps._write_applications([replace(s.apps.for_job("j1"), application_url="https://example.com/other")])
    elif change == "answer":
        AnswerBank(s.root).set("email", "other@example.com", reviewed_by="human", provenance="explicit user input")
    elif change == "profile":
        s.candidate.graduation_year = 2025
        _write_profile(s.root, s.candidate)
    elif change == "jd":
        LocalJobStore(s.root)._persist([replace(s.job, description="Different JD")])
    elif change == "gate":
        change_artifact(s, review_status=ReviewGateStatus.REVIEW_REQUIRED, review_reasons=["Changed reason"])
    else:
        path = s.root / "config.json"
        path.write_text('{"freshness_half_life_days": 7}')
        s.svc.config_path = path
    with pytest.raises(ValueError):
        s.svc.enqueue(a["id"])
    assert s.svc.store.approvals()[a["id"]]["status"] == "revoked"
    assert (s.root / "submissions/events.jsonl").read_bytes().startswith(before)


def test_expired_approval_records_event(setup):
    a = approve(setup, ttl_seconds=1)
    setup.now[0] += timedelta(seconds=2)
    with pytest.raises(ValueError):
        setup.svc.enqueue(a["id"])
    assert setup.svc.store.approvals()[a["id"]]["status"] == "expired"


def test_revoke_cancels_only_unstarted_queue(setup):
    a = approve(setup)
    r = setup.svc.enqueue(a["id"])
    setup.svc.revoke(a["id"], reviewer="human", reason="Changed my mind")
    assert setup.svc.store.requests()[r["id"]]["status"] == "cancelled"
    with pytest.raises(ValueError):
        setup.svc.claim(r["id"], worker="human")


def test_revoke_inflight_preserves_unknown_outcome(setup):
    r, c, _ = attempt(setup)
    setup.svc.revoke(r["approval_id"], reviewer="human", reason="Stop")
    assert setup.svc.store.requests()[r["id"]]["status"] == "outcome_unknown"
    with pytest.raises(ValueError):
        setup.svc.claim(r["id"], worker="human")


def test_enqueue_and_approval_idempotency(setup):
    a = approve(setup)
    assert approve(setup)["id"] == a["id"]
    r = setup.svc.enqueue(a["id"])
    assert setup.svc.enqueue(a["id"])["id"] == r["id"]


def test_concurrent_claim_single_winner(setup):
    r = setup.svc.enqueue(approve(setup)["id"])
    def claim(i):
        try:
            return setup.svc.claim(r["id"], worker=str(i))
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        winners = [v for v in pool.map(claim, range(4)) if v]
    assert len(winners) == 1


def test_manual_executor_only_handoff_and_no_application_write(setup):
    r, c, h = attempt(setup)
    assert h.executor == "manual" and "No browser" in h.action
    assert setup.apps.for_job("j1").applied_at is None
    with pytest.raises(ValueError):
        setup.svc.execute(r["id"], claim_token=c["claim_token"])


def test_test_executor_receives_package_only(setup):
    class Fake:
        name = "manual"
        def submit(self, package, *, idempotency_key):
            assert package.cv_artifact_id == setup.artifact.id
            assert not hasattr(package, "store")
            return "test handoff"
    r = setup.svc.enqueue(approve(setup)["id"])
    c = setup.svc.claim(r["id"], worker="test")
    assert setup.svc.execute(r["id"], claim_token=c["claim_token"], executor=Fake()) == "test handoff"
    assert not setup.apps.for_job("j1").is_submitted


def test_expired_claim_unknown_and_not_reclaimable(setup):
    r, c, _ = attempt(setup)
    setup.now[0] += timedelta(seconds=901)
    setup.svc.list()
    assert setup.svc.store.requests()[r["id"]]["status"] == "outcome_unknown"
    with pytest.raises(ValueError):
        setup.svc.claim(r["id"], worker="retry")


@pytest.mark.parametrize("field", ["sponsorship", "right_to_work", "visa_status", "nationality", "security_clearance", "criminal", "disability", "salary", "notice_period", "relocation", "conflict_of_interest", "cover_letter", "motivation", "unknown"])
def test_sensitive_or_unknown_answers_never_inferred(setup, field):
    with pytest.raises(ValueError):
        AnswerBank(setup.root).set(field, "guessed", reviewed_by="human", provenance="test")


def test_answer_versions_append_and_job_scope(setup):
    bank = AnswerBank(setup.root)
    a = bank.set("email", "first@example.com", reviewed_by="human", provenance="user")
    bank.set("email", "other@example.com", reviewed_by="human", provenance="user", job_id="unrelated")
    p = setup.svc.preview(setup.app.id)
    email = next(v for v in p.answers if v.field == "email")
    assert email.value == "first@example.com" and email.version == a["event_id"]


@pytest.mark.parametrize("kind,value", [
    ("confirmation_id", "ATS-12345"),
    ("confirmation_page", "https://example.com/confirmation"),
    ("confirmation_text", "Thank you, your application has been submitted."),
    ("application_portal_status", "https://example.com/applications/j1"),
    ("manual_verified", "I independently checked that this application was submitted."),
])
def test_varied_receipt_evidence_without_mandatory_confirmation_id(setup, kind, value):
    r, c, res = completed(setup, [ReceiptEvidence(kind, value, detail="Your application has been submitted successfully.")])
    receipt = setup.svc.verify(r["id"], reviewer="human", confirmed=True)
    assert receipt["submitted"] and receipt["evidence"][0]["kind"] == kind
    assert not setup.apps.for_job("j1").is_submitted
    assert len(setup.svc.store.receipts()) == 1
    assert setup.svc.verify(r["id"], reviewer="human", confirmed=True) == receipt


def test_screenshot_strategy_checks_bytes_and_path(setup):
    directory = setup.root / "submissions/evidence"
    directory.mkdir(parents=True)
    data = b"\x89PNG\r\n\x1a\nscreenshot fixture"
    (directory / "capture.png").write_bytes(data)
    r, _, _ = completed(setup, [ReceiptEvidence("screenshot_evidence", "capture.png", hashlib.sha256(data).hexdigest())])
    assert setup.svc.verify(r["id"], reviewer="human", confirmed=True)["submitted"]


@pytest.mark.parametrize("evidence", [[], [ReceiptEvidence("confirmation_text", "done")], [ReceiptEvidence("unknown_strategy", "done")], [ReceiptEvidence("screenshot_evidence", "../../profile/candidate_profile.json", "fake")]])
def test_insufficient_evidence_unknown_no_automatic_retry(setup, evidence):
    r, _, _ = completed(setup, evidence)
    assert setup.svc.verify(r["id"], reviewer="human", confirmed=True)["status"] == "outcome_unknown"
    assert not setup.svc.store.receipts() and not setup.apps.for_job("j1").is_submitted
    with pytest.raises(ValueError):
        setup.svc.claim(r["id"], worker="retry")


def test_executor_done_alone_is_not_verified(setup):
    r, _, _ = completed(setup)
    with pytest.raises(ValueError):
        setup.svc.verify(r["id"], reviewer="", confirmed=False)
    assert not setup.apps.for_job("j1").is_submitted


@pytest.mark.parametrize("field,value", [("job_id", "other"), ("cv_artifact_id", "other"), ("package_hash", "wrong"), ("application_url", "https://other.com/"), ("executor", "airtap")])
def test_result_identity_must_match(setup, field, value):
    r, c, _ = attempt(setup)
    with pytest.raises(ValueError):
        setup.svc.complete(r["id"], claim_token=c["claim_token"], result=replace(result(setup, r), **{field: value}))


def test_unknown_question_needs_human(setup):
    r, c, _ = attempt(setup)
    res = replace(result(setup, r), unanswered_fields=("visa_status",))
    out = setup.svc.complete(r["id"], claim_token=c["claim_token"], result=res)
    assert out["status"] == "needs_human_input"
    with pytest.raises(ValueError):
        setup.svc.verify(r["id"], reviewer="human", confirmed=True)


def test_duplicate_completion_is_noop(setup):
    r, c, res = completed(setup)
    count = len(setup.svc.store.events())
    setup.svc.complete(r["id"], claim_token=c["claim_token"], result=res)
    assert len(setup.svc.store.events()) == count


@pytest.mark.parametrize("evidence", [
    ReceiptEvidence("confirmation_page", "https://example.com/jobs/j1"),
    ReceiptEvidence("application_portal_status", "https://example.com/status", detail="Application not submitted"),
    ReceiptEvidence("confirmation_text", "Your application was not submitted successfully."),
])
def test_a_url_or_negative_status_does_not_prove_submission(setup, evidence):
    r, _, _ = completed(setup, [evidence])
    assert setup.svc.verify(r["id"], reviewer="human", confirmed=True)["status"] == "outcome_unknown"


def test_bad_claim_token_never_records_completion(setup):
    r, _, _ = attempt(setup)
    with pytest.raises(ValueError):
        setup.svc.complete(r["id"], claim_token="wrong", result=result(setup, r))
    assert "result" not in setup.svc.store.requests()[r["id"]]


def test_changed_gate_after_enqueue_is_checked_again_at_claim(setup):
    r = setup.svc.enqueue(approve(setup)["id"])
    change_artifact(setup, review_status=ReviewGateStatus.BLOCK_AUTO_SUBMISSION)
    with pytest.raises(ValueError):
        setup.svc.claim(r["id"], worker="human")
    assert setup.svc.store.requests()[r["id"]]["status"] == "cancelled"


def test_custom_evidence_strategy_still_requires_human_confirmation(setup):
    class Custom:
        def verify(self, evidence, *, package, root):
            return evidence.value == "checked by test strategy"
    r, _, _ = completed(setup, [ReceiptEvidence("custom", "checked by test strategy")])
    with pytest.raises(ValueError):
        setup.svc.verify(r["id"], reviewer="", confirmed=False, strategies={"custom": Custom()})
    assert setup.svc.verify(r["id"], reviewer="human", confirmed=True, strategies={"custom": Custom()})["submitted"]
