"""Supervised AirTap CV upload is an in-run human handoff, not a pre-launch blocker.

The task may start before the CV is on the cloud device; at a required upload
control the agent stops for the human, who uploads exactly the approved CV.
Only a confirmed upload may continue to ready_for_human_review; final submit
stays prohibited.
"""
from __future__ import annotations

import json
from pathlib import Path

from jobintel.submission.airtap import CV_UPLOAD_MODE, LIVE_BLOCKERS, MODE
from test_submission_airtap import escalated, report, start, status_of
from test_submission_service import setup  # noqa: F401  (pytest fixture)


def _task(handoff) -> str:
    return (Path(handoff.bundle_path) / "airtap_task.md").read_text()


def test_c_supervised_task_can_begin_before_cv_upload(setup):
    s = setup
    _, r, c, handoff = start(s)
    assert LIVE_BLOCKERS == () and CV_UPLOAD_MODE == "manual_human_upload"
    check = s.svc.check_airtap(r["id"], claim_token=c["claim_token"])
    assert check["status"] == "validated_for_supervised_handoff"
    assert check["live_execution"] == "supervised_only" and check["live_execution_blockers"] == []
    task = _task(handoff)
    assert "The CV does NOT need to be on the cloud device before starting." in task
    # The old contradictory pre-launch requirement is gone.
    for old in ("LIVE EXECUTION IS BLOCKED", "privately place cv.pdf", "device file SHA-256",
                "If transfer or hash cannot be confirmed", "pdf_transfer_unconfirmed"):
        assert old not in task, old
    # The local artifact hash is still verified before the handoff.
    assert "submission-airtap-check" in task and "SHA-256 of the sealed local cv.pdf" in task


def test_d_required_cv_upload_is_a_human_handoff_not_a_rejection(setup):
    s = setup
    a, r, c, handoff = start(s)
    task = _task(handoff)
    assert "At a required CV / resume upload control: STOP and ask the supervising human" in task
    assert "Asking is not uploading." in task
    assert "A browser upload provides no cloud-side SHA-256. Never report or imply one." in task
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(
        s, a, r, status="needs_human_input", cv_upload_confirmed=False,
        questions_escalated=[escalated("Resume / CV (required) — awaiting supervising human upload")]))
    assert out["status"] == "needs_human_input"
    assert status_of(s, r) == "needs_human_input"


def test_e_ready_for_human_review_requires_confirmed_cv_upload(setup):
    s = setup
    a, r, c, _ = start(s)
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, cv_upload_confirmed=False))
    assert out["status"] == "needs_human_input"
    assert out["result"]["unanswered_fields"] == ["cv_upload_unconfirmed"]


def test_e_confirmed_cv_upload_may_reach_ready_for_human_review(setup):
    s = setup
    a, r, c, _ = start(s)
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, cv_upload_confirmed=True))
    assert out["status"] == "ready_for_human_review"


def test_f_upload_instruction_names_the_exact_approved_cv(setup):
    s = setup
    _, _, _, handoff = start(s)
    package = s.svc.preview(s.app.id)
    task = _task(handoff)
    assert f"EXACTLY the approved CV: `cv.pdf` from this bundle = CV artifact {package.cv_artifact_id}" in task
    assert f"(stored as `{package.cv_artifact_id}.pdf`), SHA-256 {package.cv_pdf_sha256}. No other file." in task
    request = json.loads((Path(handoff.bundle_path) / "submission_request.json").read_text())
    assert request["cv_path"] == "cv.pdf" and request["cv_upload_mode"] == "manual_human_upload"
    assert (request["cv_artifact_id"], request["cv_sha256"]) == (package.cv_artifact_id, package.cv_pdf_sha256)


def test_g_final_submit_hard_stop_intact(setup):
    s = setup
    a, r, c, handoff = start(s)
    request = json.loads((Path(handoff.bundle_path) / "submission_request.json").read_text())
    assert request["execution_mode"] == MODE == "STOP_BEFORE_FINAL_SUBMIT"
    assert request["final_submit_allowed"] is False
    assert s.svc.check_airtap(r["id"], claim_token=c["claim_token"])["final_submit_allowed"] is False
    assert "Never click final Submit under any circumstances in this mode." in _task(handoff)
    schema = json.loads((Path(handoff.bundle_path) / "expected_result_schema.json").read_text())
    assert schema["properties"]["final_submit_clicked"] == {"const": False}
