"""Supervised-execution exception for BLOCK_AUTO_SUBMISSION packages.

A structured human acknowledgement may authorize SUPERVISED_EXECUTION_ONLY for
an otherwise eligible, non-protected application. It never changes the review
result, never satisfies a missing requirement, and never permits final submit.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobintel.models.application import Application
from jobintel.models.candidate import CandidateProfile
from jobintel.models.job import Job
from jobintel.models.submission import (
    SUPERVISED_EXECUTION_ONLY,
    ApprovedSubmissionPackage,
    SupervisedReviewAcknowledgement,
)
from jobintel.models.taxonomy import ReviewGateStatus
from jobintel.profile_ingestion import _write_profile
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.submission.airtap import MODE
from jobintel.submission.handoff import generate_airtap_task_markdown
from jobintel.submission.service import SubmissionService
from test_cv_artifact_store import _artifact
from test_submission_service import write_synthetic_answer_bank

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

UNSUPPORTED = "No evidence for a majority of must-have requirements: Rust"
ACK_REASON = ("Candidate reviewed the unsupported must-have and explicitly authorizes a supervised "
              "application attempt. It remains unsupported. This does not authorize final submission.")


def _store(tmp_path: Path, *, title="Junior Software Engineer", description="Build backend services in a small team.",
           job_id="j1", app_id="app:j1", cv_id="cv-0123456789ab", nationality=None, review_reasons=(UNSUPPORTED,)):
    now = [datetime(2026, 10, 2, tzinfo=timezone.utc)]
    candidate = CandidateProfile("candidate-1", "Alex Example", 2026, email="alex@example.com", phone="07000000000")
    if nationality:
        candidate.nationality = candidate.citizenship = nationality
    _write_profile(tmp_path, candidate)
    write_synthetic_answer_bank(tmp_path)
    jobs = LocalJobStore(tmp_path)
    job = Job(id=job_id, company="ExampleCo", title=title, description=description, locations=[], source_observations=[])
    jobs.write_jobs([*(j for j in jobs.read_jobs() if j.id != job_id), job])
    pdf = tmp_path / f"cv_artifacts/pdfs/{cv_id}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 supervised fixture " + cv_id.encode())
    artifact = replace(_artifact(cv_id, job_id), jd_snapshot=description, pdf_path=str(pdf),
                       pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
                       review_status=ReviewGateStatus.BLOCK_AUTO_SUBMISSION, review_reasons=list(review_reasons))
    CVArtifactStore(tmp_path).save(artifact)
    apps = ApplicationStore(tmp_path)
    app = apps.create_application(Application(app_id, job_id, "ExampleCo", title, application_url=f"https://example.com/jobs/{job_id}"))
    apps.attach_cv(app.id, artifact.id, artifact.selected_project_ids, artifact.evidence_ids_used)
    svc = SubmissionService(tmp_path, clock=lambda: now[0])
    return SimpleNamespace(root=tmp_path, svc=svc, app=app, artifact=artifact, job=job, now=now)


def _ack(s, **changes) -> SupervisedReviewAcknowledgement:
    values = dict(reviewer="candidate", reviewed=True, acknowledged_at="2026-10-02T09:00:00+00:00", reason=ACK_REASON,
                  acknowledged_blockers=(UNSUPPORTED,), application_id=s.app.id, job_id=s.job.id,
                  cv_artifact_id=s.artifact.id, cv_pdf_sha256=s.artifact.pdf_sha256)
    values.update(changes)
    return SupervisedReviewAcknowledgement(**values)


def _approve(s, ack, reviewer="candidate"):
    package = s.svc.preview(s.app.id, ack)
    return package, s.svc.approve(s.app.id, package_hash=package.package_hash, reviewer=reviewer, reviewed=True,
                                  supervised_acknowledgement=ack)


def _bundle(s, ack):
    _, approval = _approve(s, ack)
    request = s.svc.enqueue(approval["id"], executor="airtap")
    claim = s.svc.claim(request["id"], worker="supervisor")
    handoff = s.svc.execute(request["id"], claim_token=claim["claim_token"])
    path = Path(handoff.bundle_path)
    return request, json.loads((path / "submission_request.json").read_text()), (path / "airtap_task.md").read_text()


@pytest.fixture
def s(tmp_path):
    return _store(tmp_path)


# A. the standard (non-supervised) path still rejects block_auto_submission
def test_a_blocked_package_cannot_enter_standard_execution(s) -> None:
    with pytest.raises(ValueError, match="BLOCK_AUTO_SUBMISSION") as error:
        s.svc.preview(s.app.id)
    assert UNSUPPORTED in str(error.value)  # the human can see exactly what must be acknowledged
    with pytest.raises(ValueError, match="BLOCK_AUTO_SUBMISSION"):
        s.svc.approve(s.app.id, package_hash="0" * 64, reviewer="candidate", reviewed=True)
    assert s.svc.store.approvals() == {}


def test_a_airtap_executor_rejects_blocked_package_without_acknowledgement(s) -> None:
    from jobintel.submission.airtap import AirTapAuthorization, AirTapSubmissionExecutor
    package = replace(s.svc.preview(s.app.id, _ack(s)), supervised_acknowledgement=None)
    auth = AirTapAuthorization("approval:x", "request:x", package.package_hash, "2026-10-03T00:00:00+00:00", True, "approved")
    executor = AirTapSubmissionExecutor(s.root / "submissions/airtap", authorization=auth, clock=lambda: s.now[0])
    with pytest.raises(ValueError, match="permitted gate"):
        executor.submit(package, idempotency_key="request:x")


# B. no supervised execution without a complete, explicit acknowledgement
@pytest.mark.parametrize("changes, message", [
    ({"reviewed": False}, "reviewed=True"),
    ({"reviewer": " "}, "reviewer"),
    ({"reason": ""}, "reason"),
    ({"acknowledged_blockers": ()}, "blockers"),
    ({"acknowledged_at": "2026-10-02T09:00:00"}, "timezone"),
    ({"final_submit_allowed": True}, "final-submit"),
    ({"execution_mode": "AUTO"}, "SUPERVISED_EXECUTION_ONLY"),
])
def test_b_incomplete_acknowledgement_is_rejected(s, changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        _ack(s, **changes)


def test_b_acknowledgement_must_cover_exactly_the_current_blockers(s) -> None:
    with pytest.raises(ValueError, match="exactly the current review reasons"):
        s.svc.preview(s.app.id, _ack(s, acknowledged_blockers=("Some other reason",)))
    with pytest.raises(ValueError, match="exactly the current review reasons"):
        s.svc.preview(s.app.id, _ack(s, acknowledged_blockers=(UNSUPPORTED, "Extra unrelated reason")))


def test_b_approver_must_be_the_acknowledging_reviewer(s) -> None:
    with pytest.raises(ValueError, match="reviewer"):
        _approve(s, _ack(s), reviewer="someone-else")


# C/D/E. acknowledgement authorizes SUPERVISED_EXECUTION_ONLY; review result and blockers unchanged
def test_c_d_e_acknowledged_package_is_supervised_and_keeps_review_result(s) -> None:
    package, approval = _approve(s, _ack(s))
    assert package.execution_mode == SUPERVISED_EXECUTION_ONLY
    assert approval["execution_mode"] == SUPERVISED_EXECUTION_ONLY
    assert package.gate_status == "block_auto_submission"
    assert package.gate_reasons == (UNSUPPORTED,)
    request = s.svc.enqueue(approval["id"], executor="manual")
    assert request["execution_mode"] == SUPERVISED_EXECUTION_ONLY
    # The CV artifact itself is untouched.
    stored = CVArtifactStore(s.root).get(s.artifact.id)
    assert stored.review_status == ReviewGateStatus.BLOCK_AUTO_SUBMISSION
    assert stored.review_reasons == [UNSUPPORTED]
    # Approved package round-trips with the acknowledgement and the same hash.
    restored = ApprovedSubmissionPackage.from_dict(approval["package"])
    assert restored == package and restored.package_hash == approval["package_hash"]


def test_d_e_bundle_carries_original_review_result_and_acknowledgement(s) -> None:
    request, bundle, _ = _bundle(s, _ack(s))
    assert bundle["authorization"] == SUPERVISED_EXECUTION_ONLY
    assert bundle["review_status"] == "block_auto_submission"
    assert bundle["review_reasons"] == [UNSUPPORTED]
    assert bundle["supervised_acknowledgement"]["acknowledged_blockers"] == [UNSUPPORTED]
    assert bundle["supervised_acknowledgement"]["reason"] == ACK_REASON


# F/G. never final-submit permission; the hard stop is always present
def test_f_g_supervised_bundle_never_permits_final_submit_and_keeps_hard_stop(s) -> None:
    _, bundle, task = _bundle(s, _ack(s))
    assert bundle["final_submit_allowed"] is False
    assert bundle["supervised_acknowledgement"]["final_submit_allowed"] is False
    assert bundle["execution_mode"] == MODE == "STOP_BEFORE_FINAL_SUBMIT"
    assert "Do NOT click Submit" in task
    assert "Never click final Submit under any circumstances" in task
    assert "never authorizes" in task and "final submission" in task
    assert "block_auto_submission" in task and UNSUPPORTED in task
    assert "does NOT mean any listed requirement is satisfied" in task


def test_g_handoff_markdown_keeps_hard_stop_for_supervised_package(s) -> None:
    package, _ = _approve(s, _ack(s))
    task = generate_airtap_task_markdown(package, bank=None, root=s.root)
    assert "STOP_BEFORE_FINAL_SUBMIT" in task
    assert "Do NOT click Submit" in task
    assert "Supervised Execution Only" in task and UNSUPPORTED in task


def test_f_final_submit_cannot_be_enabled_through_the_package() -> None:
    with pytest.raises(ValueError, match="final-submit"):
        SupervisedReviewAcknowledgement("r", True, "2026-10-02T09:00:00+00:00", "reason", ("x",), "a", "j", "c", "h",
                                        final_submit_allowed=True)


# H. an acknowledgement is bound to one application/job/CV/PDF
@pytest.mark.parametrize("field", ["application_id", "job_id", "cv_artifact_id", "cv_pdf_sha256"])
def test_h_acknowledgement_cannot_authorize_another_target(s, field) -> None:
    with pytest.raises(ValueError, match="bound to a different"):
        s.svc.preview(s.app.id, _ack(s, **{field: "other"}))


def test_h_acknowledgement_for_one_application_cannot_be_reused_for_another(tmp_path) -> None:
    first = _store(tmp_path)
    second = _store(tmp_path, job_id="j2", app_id="app:j2", cv_id="cv-0123456789ac")
    ack_for_first = _ack(first)
    with pytest.raises(ValueError, match="bound to a different"):
        second.svc.preview(second.app.id, ack_for_first)


def test_h_changed_cv_invalidates_supervised_approval(s) -> None:
    _, approval = _approve(s, _ack(s))
    Path(s.artifact.pdf_path).chmod(0o600)
    Path(s.artifact.pdf_path).write_bytes(b"%PDF-1.4 a different CV")
    with pytest.raises(ValueError):
        s.svc.enqueue(approval["id"], executor="airtap")
    assert s.svc.store.approvals()[approval["id"]]["status"] == "revoked"


# I. protected Graduate/New Grad/Early Career review is not weakened
@pytest.mark.parametrize("title, description", [
    ("Graduate Software Engineer", "Our Graduate Programme welcomes candidates completing their degree."),
    ("New Grad Software Development Engineer", "Join our team as a New Grad Software Development Engineer."),
    ("Software Engineer, Early Careers", "Our Early Careers programme offers mentoring and software projects."),
])
def test_i_supervised_exception_is_unavailable_for_protected_career_stage(tmp_path, title, description) -> None:
    s = _store(tmp_path, title=title, description=description)
    with pytest.raises(ValueError, match="not available under this application policy"):
        s.svc.preview(s.app.id, _ack(s))
    with pytest.raises(ValueError, match="BLOCK_AUTO_SUBMISSION"):
        s.svc.preview(s.app.id)


# J. supervised review cannot override HARD_INELIGIBLE
def test_j_hard_ineligible_cannot_be_overridden(tmp_path) -> None:
    s = _store(tmp_path, title="Infrastructure Engineer",
               description="Requirements: Candidates must be a Sole British National due to government security requirements.",
               nationality="Chinese")
    with pytest.raises(ValueError, match="HARD_INELIGIBLE"):
        s.svc.preview(s.app.id, _ack(s))


def test_supervised_acknowledgement_only_applies_to_blocked_packages(tmp_path) -> None:
    s = _store(tmp_path, review_reasons=())
    CVArtifactStore(s.root).path.write_text(
        json.dumps({**json.loads(CVArtifactStore(s.root).path.read_text()), "review_status": "auto_prepare"}) + "\n")
    with pytest.raises(ValueError, match="only applies to a BLOCK_AUTO_SUBMISSION"):
        s.svc.preview(s.app.id, _ack(s))
