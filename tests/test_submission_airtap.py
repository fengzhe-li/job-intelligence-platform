import ast
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from jobintel.cli.main import main
from jobintel.submission import airtap
from jobintel.submission.airtap import (AirTapAuthorization, AirTapSubmissionExecutor, LIVE_BLOCKERS, MODE,
                                        escalation_reason, import_result, question)
from test_submission_service import approve, setup

SCREEN = "Final review page for Graduate Engineer at Example; Submit button visible, not pressed."
SENTINEL_TOKEN = "airtap-test-token-" + "9f3c" * 8


def start(s):
    a = approve(s)
    r = s.svc.enqueue(a["id"], executor="airtap")
    c = s.svc.claim(r["id"], worker="operator")
    handoff = s.svc.execute(r["id"], claim_token=c["claim_token"])
    return a, r, c, handoff


def report(s, a, r, **changes):
    package = s.svc.preview(s.app.id)
    value = {"request_id": r["id"], "approval_id": a["id"], "package_hash": r["package_hash"],
             "application_url": s.app.application_url, "cv_artifact_id": s.artifact.id,
             "cv_sha256": package.cv_pdf_sha256, "observed_company": "Example", "observed_role": "Graduate Engineer",
             "observed_url": s.app.application_url, "task_id": "task_abc123", "executor": "airtap",
             "execution_mode": MODE, "status": "ready_for_human_review", "final_submit_clicked": False,
             "cv_upload_confirmed": True, "duplicate_detected": False,
             "fields_filled": [{"field": "email", "value": "alex@example.com", "page_label": "Email"}],
             "questions_escalated": [],
             "evidence": [{"kind": "screen_state", "value": SCREEN, "sha256": "", "detail": "observed via task get-details"}]}
    value.update(changes)
    return value


def escalated(label):
    return {"status": "needs_human_input", "question": label, "page_context": "Application form step 2",
            "allowed_known_answers": [], "screenshot_or_evidence": "screen state recorded in task history"}


def status_of(s, r):
    return s.svc.store.requests()[r["id"]]["status"]


def authorization(package, **changes):
    values = dict(approval_id="approval:x", request_id="request:x", package_hash=package.package_hash,
                  expires_at="2026-09-29T00:00:00+00:00", human_reviewed=True, status="approved")
    values.update(changes)
    return AirTapAuthorization(**values)


def executor(s, auth):
    return AirTapSubmissionExecutor(s.root / "submissions/airtap", authorization=auth, clock=lambda: s.now[0])


def sealed_package(s):
    approve(s)
    return s.svc.preview(s.app.id)


# --- Authorization and hash binding -------------------------------------------------

@pytest.mark.parametrize("changes", [None, {"status": "revoked"}, {"human_reviewed": False},
                                     {"approval_id": ""}, {"expires_at": "2026-09-27T00:00:00+00:00"},
                                     {"request_id": "request:other"}])
def test_unapproved_package_rejected(setup, changes):
    package = sealed_package(setup)
    auth = None if changes is None else authorization(package, **changes)
    with pytest.raises(ValueError, match="approval"):
        executor(setup, auth).submit(package, idempotency_key="request:x")
    assert not (setup.root / "submissions/airtap").exists()


def test_airtap_request_cannot_accept_injected_executor(setup):
    a = approve(setup)
    r = setup.svc.enqueue(a["id"], executor="airtap")
    c = setup.svc.claim(r["id"], worker="operator")
    with pytest.raises(ValueError):
        setup.svc.execute(r["id"], claim_token=c["claim_token"], executor=executor(setup, None))


def test_package_hash_mismatch_rejected(setup):
    package = sealed_package(setup)
    with pytest.raises(ValueError, match="hash mismatch"):
        executor(setup, authorization(package, package_hash="0" * 64)).submit(package, idempotency_key="request:x")


def test_cv_hash_mismatch_rejected(setup):
    package = replace(sealed_package(setup), cv_pdf_sha256="0" * 64)
    with pytest.raises(ValueError, match="CV hash mismatch"):
        executor(setup, authorization(package)).submit(package, idempotency_key="request:x")


def test_sealed_cv_modified_after_approval_invalidates_before_bundle(setup):
    s = setup
    a = approve(s)
    r = s.svc.enqueue(a["id"], executor="airtap")
    c = s.svc.claim(r["id"], worker="operator")
    sealed = Path(s.svc.preview(s.app.id).cv_path)
    sealed.chmod(0o600)
    sealed.write_bytes(b"%PDF-1.4 tampered")
    with pytest.raises(ValueError):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert not (s.root / "submissions/airtap").exists()


# --- Bundle ---------------------------------------------------------------------------

def test_default_mode_is_stop_before_final_submit_and_supervised_only(setup):
    s = setup
    _, r, c, handoff = start(s)
    assert handoff.execution_mode == MODE == "STOP_BEFORE_FINAL_SUBMIT"
    assert handoff.runtime_started is False and handoff.live_execution_blockers == LIVE_BLOCKERS
    request = json.loads((Path(handoff.bundle_path) / "submission_request.json").read_text())
    assert request["execution_mode"] == MODE and request["final_submit_allowed"] is False
    # CV upload is a supervised in-run human step, not a pre-launch blocker.
    assert request["live_execution_blockers"] == [] and request["cv_upload_mode"] == "manual_human_upload"
    schema = json.loads((Path(handoff.bundle_path) / "expected_result_schema.json").read_text())
    assert schema["properties"]["final_submit_clicked"] == {"const": False}
    assert schema["properties"]["execution_mode"] == {"const": MODE}
    assert "submitted" not in schema["properties"]["status"]["enum"]
    check = s.svc.check_airtap(r["id"], claim_token=c["claim_token"])
    assert check["execution_mode"] == MODE and check["live_execution"] == "supervised_only"
    assert check["final_submit_allowed"] is False and check["cv_upload"] == "manual_human_upload"
    assert check["runtime_started"] is False


def test_bundle_binds_identity_and_leaks_nothing_else(setup):
    s = setup
    a, r, _, handoff = start(s)
    bundle = Path(handoff.bundle_path)
    assert sorted(p.name for p in bundle.iterdir()) == ["airtap_task.md", "cv.pdf", "expected_result_schema.json",
                                                        "manifest.json", "submission_request.json"]
    request = json.loads((bundle / "submission_request.json").read_text())
    package = s.svc.preview(s.app.id)
    assert set(request) == {"request_id", "approval_id", "package_hash", "job_id", "application_id",
                            "application_url", "company", "role", "cv_artifact_id", "cv_sha256", "cv_path",
                            "expires_at", "answers", "execution_mode", "executor", "receiver",
                            "final_submit_allowed", "live_execution_blockers", "cv_upload_mode"}
    assert (request["request_id"], request["approval_id"], request["package_hash"], request["job_id"],
            request["application_id"], request["application_url"], request["company"], request["role"],
            request["cv_artifact_id"], request["cv_sha256"]) == (
        r["id"], a["id"], package.package_hash, "j1", s.app.id, s.app.application_url, "Example",
        "Graduate Engineer", s.artifact.id, package.cv_pdf_sha256)
    assert request["cv_path"] == "cv.pdf" and request["receiver"] == "cloud"
    assert {x["field"] for x in request["answers"]} <= set(airtap.LABELS)
    assert hashlib.sha256((bundle / "cv.pdf").read_bytes()).hexdigest() == package.cv_pdf_sha256
    text = "".join(p.read_text() for p in bundle.iterdir() if p.suffix != ".pdf")
    for leaked in (str(s.root), "input_hash", "gate_reasons", "graduation_year", "candidate-1", s.job.description):
        assert leaked not in text
    assert all(os.stat(p).st_mode & 0o777 == 0o400 for p in bundle.iterdir())


def test_bundle_is_never_regenerated_or_repeated(setup):
    s = setup
    _, r, c, _ = start(s)
    with pytest.raises(ValueError):
        s.svc.execute(r["id"], claim_token=c["claim_token"])


def test_modified_bundle_blocks_supervised_handoff(setup):
    s = setup
    _, r, c, handoff = start(s)
    target = Path(handoff.bundle_path) / "submission_request.json"
    target.chmod(0o600)
    target.write_text(target.read_text().replace(MODE, "SUBMIT"))
    with pytest.raises(ValueError, match="Bundle changed"):
        s.svc.check_airtap(r["id"], claim_token=c["claim_token"])
    assert status_of(s, r) == "outcome_unknown"


# --- Escalation -----------------------------------------------------------------------

@pytest.mark.parametrize("label", ["Do you require visa sponsorship?", "Right to work in the UK", "Nationality",
                                   "Do you hold security clearance?", "Salary expectation", "Notice period",
                                   "Are you willing to relocate?", "Relocation", "Unspent criminal convictions",
                                   "Disability or reasonable adjustments", "Conflict of interest",
                                   "Cover letter", "Motivation", "Why do you want to join us?", "Visa status"])
def test_sensitive_questions_always_escalate(setup, label):
    package = sealed_package(setup)
    assert escalation_reason(label).startswith(("sensitive:", "human_only:work_authorisation:"))
    for field in airtap.LABELS:
        assert question(package, field, label, "form", "screen")["status"] == "needs_human_input"


@pytest.mark.parametrize("label", ["Favourite colour", "Name of your referee", "Email (secondary)", "Other"])
def test_unknown_or_ambiguous_questions_escalate(setup, label):
    package = sealed_package(setup)
    assert escalation_reason(label) == "unknown_field"
    assert question(package, "email", label, "form", "screen") == {
        "status": "needs_human_input", "question": label, "page_context": "form",
        "allowed_known_answers": [], "screenshot_or_evidence": "screen"}


def test_exact_permitted_label_uses_only_the_approved_snapshot(setup):
    package = sealed_package(setup)
    assert escalation_reason(" Email  Address ") is None
    assert question(package, "email", "Email address", "form", "screen") == {"value": "alex@example.com"}


@pytest.mark.parametrize("label", ["Visa status", "Unknown question"])
def test_escalated_question_imports_as_needs_human_input(setup, label):
    s = setup
    a, r, c, _ = start(s)
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"],
                                value=report(s, a, r, status="needs_human_input", questions_escalated=[escalated(label)]))
    assert out["status"] == "needs_human_input" and label in out["result"]["unanswered_fields"]
    assert not s.apps.for_job("j1").is_submitted


def test_ready_report_with_escalations_is_downgraded(setup):
    s = setup
    a, r, c, _ = start(s)
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"],
                                value=report(s, a, r, questions_escalated=[escalated("Salary expectation")]))
    assert out["status"] == "needs_human_input"


@pytest.mark.parametrize("filled", [
    {"field": "name", "value": "Alex Example", "page_label": "Visa sponsorship"},
    {"field": "email", "value": "other@example.com", "page_label": "Email"},
    {"field": "email", "value": "alex@example.com", "page_label": "Email (secondary)"},
])
def test_filled_answer_must_be_exact_approved_answer(setup, filled):
    s = setup
    a, r, c, _ = start(s)
    with pytest.raises(ValueError):
        s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, fields_filled=[filled]))
    assert status_of(s, r) == "outcome_unknown"


def test_cv_upload_unconfirmed_needs_human_input(setup):
    s = setup
    a, r, c, _ = start(s)
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, cv_upload_confirmed=False))
    assert out["status"] == "needs_human_input" and out["result"]["unanswered_fields"] == ["cv_upload_unconfirmed"]


# --- Page identity, duplicates --------------------------------------------------------

@pytest.mark.parametrize("changes", [{"observed_company": "Other Corp"}, {"observed_role": "Senior Engineer"},
                                     {"observed_url": "https://example.com/jobs/other"},
                                     {"status": "aborted_wrong_job"}])
def test_wrong_company_role_or_url_aborts(setup, changes):
    s = setup
    a, r, c, _ = start(s)
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, **changes))
    assert out["status"] == "failed" and out["result"]["outcome"] == "failed"
    assert not s.svc.store.receipts() and not s.apps.for_job("j1").is_submitted
    with pytest.raises(ValueError):
        s.svc.enqueue(a["id"], executor="airtap")


@pytest.mark.parametrize("changes", [{"status": "already_applied"}, {"status": "duplicate_detected"},
                                     {"duplicate_detected": True}])
def test_already_applied_or_duplicate_stops_and_blocks_new_attempts(setup, changes):
    s = setup
    a, r, c, _ = start(s)
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, **changes))
    assert out["status"] == "needs_human_input"
    assert any("human must check the ATS portal" in q for q in out["result"]["unanswered_fields"])
    assert not s.apps.for_job("j1").is_submitted
    with pytest.raises(ValueError):
        s.svc.store.assert_application_idle(s.app.id)
    with pytest.raises(ValueError):
        s.svc.approve(s.app.id, package_hash=r["package_hash"], reviewer="human", reviewed=True)


def test_one_request_per_approval(setup):
    s = setup
    a, r, _, _ = start(s)
    assert s.svc.enqueue(a["id"], executor="airtap")["id"] == r["id"]
    with pytest.raises(ValueError):
        s.svc.enqueue(a["id"], executor="manual")


# --- Evidence -------------------------------------------------------------------------

@pytest.mark.parametrize("evidence", [
    [],
    [{"kind": "screen_state", "value": "done", "sha256": "", "detail": ""}],
    [{"kind": "screen_state", "value": "Completed successfully.", "sha256": "", "detail": ""}],
    [{"kind": "screen_state", "value": SCREEN, "sha256": "", "detail": "success"}],
    [{"kind": "task_history", "value": "taskState COMPLETED: the agent says it finished everything", "sha256": "", "detail": ""}],
    [{"kind": "screenshot_evidence", "value": "shot.png", "sha256": "", "detail": "final page"}],
    [{"kind": "screenshot_evidence", "value": "/tmp/shot.png", "sha256": "a" * 64, "detail": "final page"}],
    [{"kind": "application_portal_status", "value": "https://evil.example.net/status", "sha256": "", "detail": "final page"}],
])
def test_agent_done_without_page_evidence_is_outcome_unknown(setup, evidence):
    s = setup
    a, r, c, _ = start(s)
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, evidence=evidence))
    assert out["status"] == "outcome_unknown"


def test_screenshot_page_evidence_is_carried_into_receipt_model(setup):
    s = setup
    a, r, c, _ = start(s)
    shot = {"kind": "screenshot_evidence", "value": "airtap/final.png", "sha256": "b" * 64, "detail": "final page"}
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, evidence=[shot]))
    assert out["status"] == "ready_for_human_review"
    assert out["result"]["evidence"] == [shot] and out["result"]["outcome"] == "ready_for_human_review"


@pytest.mark.parametrize("kind", ["confirmation_page", "confirmation_text", "confirmation_id", "manual_verified"])
def test_submission_confirmation_in_stop_mode_is_outcome_unknown(setup, kind):
    s = setup
    a, r, c, _ = start(s)
    evidence = [{"kind": "screen_state", "value": SCREEN, "sha256": "", "detail": ""},
                {"kind": kind, "value": "Your application has been received, confirmation 123", "sha256": "", "detail": ""}]
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, evidence=evidence))
    assert out["status"] == "outcome_unknown"
    assert any(e["event_id"].startswith("airtap-result:") for e in s.svc.store.events())


def test_ready_for_human_review_is_not_a_submission_receipt(setup):
    s = setup
    a, r, c, _ = start(s)
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r))
    assert out["status"] == "ready_for_human_review"
    with pytest.raises(ValueError):
        s.svc.verify(r["id"], reviewer="human", confirmed=True)
    assert s.svc.reconcile() == [] and not s.svc.store.receipts()
    assert not s.apps.for_job("j1").is_submitted and s.apps.for_job("j1").applied_at is None


def test_generic_completion_forbidden_for_airtap(setup):
    s = setup
    a, r, c, _ = start(s)
    with pytest.raises(ValueError, match="strict AirTap"):
        s.svc.complete(r["id"], claim_token=c["claim_token"], result=import_result(
            report(s, a, r), package=s.svc.preview(s.app.id), request_id=r["id"], approval_id=a["id"]))


# --- Retry ----------------------------------------------------------------------------

@pytest.mark.parametrize("changes", [{"status": "outcome_unknown"}, {"status": "failed"}, {"evidence": []}])
def test_outcome_unknown_is_never_retried_automatically(setup, changes):
    s = setup
    a, r, c, _ = start(s)
    out = s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, **changes))
    assert out["status"] == "outcome_unknown"
    with pytest.raises(ValueError):
        s.svc.enqueue(a["id"], executor="airtap")
    with pytest.raises(ValueError):
        s.svc.claim(r["id"], worker="retry")
    with pytest.raises(ValueError):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    with pytest.raises(ValueError):
        s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, task_id="task_new"))
    assert len(list((s.root / "submissions/airtap").iterdir())) == 1


# --- Malformed / mismatched results ---------------------------------------------------

@pytest.mark.parametrize("mutate", [
    lambda v: v.pop("evidence"),
    lambda v: v.update(extra="field"),
    lambda v: v.update(cv_upload_confirmed="yes"),
    lambda v: v.update(status="submitted"),
    lambda v: v.update(status="done"),
    lambda v: v.update(executor="manual"),
    lambda v: v.update(execution_mode="FINAL_SUBMIT"),
    lambda v: v.update(questions_escalated=[{**escalated("Visa"), "allowed_known_answers": ["Yes"]}]),
    lambda v: v.update(evidence=[{"kind": "screenshot", "value": SCREEN, "sha256": "", "detail": ""}]),
    lambda v: v.update(fields_filled=[{"field": "visa", "value": "Yes", "page_label": "Visa"}]),
])
def test_malformed_result_import_rejected(setup, mutate):
    s = setup
    a, r, c, _ = start(s)
    value = report(s, a, r)
    mutate(value)
    with pytest.raises(ValueError, match="Invalid AirTap report"):
        s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=value)
    assert status_of(s, r) == "outcome_unknown" and not s.svc.store.receipts()


def test_non_object_result_rejected(setup):
    s = setup
    _, r, c, _ = start(s)
    for value in (None, [], "done"):
        with pytest.raises(ValueError):
            s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=value)


@pytest.mark.parametrize("field,value", [("request_id", "request:other"), ("approval_id", "approval:other"),
                                         ("package_hash", "0" * 64), ("cv_sha256", "0" * 64),
                                         ("cv_artifact_id", "cv-other"),
                                         ("application_url", "https://example.com/jobs/other")])
def test_result_referencing_wrong_package_rejected(setup, field, value):
    s = setup
    a, r, c, _ = start(s)
    with pytest.raises(ValueError):
        s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, **{field: value}))
    assert status_of(s, r) == "outcome_unknown"


def test_final_submit_clicked_true_rejected_and_left_unknown(setup):
    s = setup
    a, r, c, _ = start(s)
    with pytest.raises(ValueError):
        s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value=report(s, a, r, final_submit_clicked=True))
    assert status_of(s, r) == "outcome_unknown"
    assert not s.svc.store.receipts() and not s.apps.for_job("j1").is_submitted
    with pytest.raises(ValueError):
        s.svc.verify(r["id"], reviewer="human", confirmed=True)


def test_bad_claim_token_rejected(setup):
    s = setup
    a, r, _, _ = start(s)
    with pytest.raises(ValueError):
        s.svc.complete_airtap(r["id"], claim_token="wrong", value=report(s, a, r))
    assert status_of(s, r) == "executing"


# --- Isolation: token, ApplicationStore -----------------------------------------------

def test_adapter_has_no_store_network_or_secret_access():
    tree = ast.parse(inspect.getsource(airtap))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert imported <= {"dataclasses", "dataclass", "hashlib", "os", "pathlib", "Path", "re", "urllib.parse", "urlsplit",
                        "jobintel.models.submission", "ApprovedSubmissionPackage", "ExecutionResult",
                        "ReceiptEvidence", "canonical", "instant", "utcnow",
                        # the shared, equally pure stop-only contract (purity tested in test_stop_only_contract.py)
                        "jobintel.submission", "stop_only", "jobintel.submission.stop_only", "BARE_ASSERTIONS",
                        "EVIDENCE_KINDS", "LABELS", "MODE", "PAGE_EVIDENCE", "REQUIRED_HANDOFF_ANSWERS", "SENSITIVE",
                        "STATUSES", "SUBMISSION_EVIDENCE", "check_page", "escalation_reason",
                        "missing_required_answers", "question", "validate_schema", "package_answer"}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & {"environ", "getenv", "putenv", "system", "popen"}


def test_executor_does_not_write_applications(setup):
    s = setup
    package = sealed_package(s)
    before = {p: p.read_bytes() for p in s.root.rglob("*") if p.is_file() and "submissions" not in p.parts}
    executor(s, authorization(package)).submit(package, idempotency_key="request:x")
    after = {p: p.read_bytes() for p in s.root.rglob("*") if p.is_file() and "submissions" not in p.parts}
    assert before == after


def test_token_never_reaches_bundle_journal_logs_or_output(setup, monkeypatch, capsys, caplog):
    s = setup
    s.now[0] = datetime.now(timezone.utc)  # CLI commands use the real clock
    monkeypatch.setenv("AIRTAP_PERSONAL_ACCESS_TOKEN", SENTINEL_TOKEN)
    caplog.set_level(logging.DEBUG)
    a, r, c, handoff = start(s)
    with patch("sys.argv", ["jobintel", "submission-airtap-check", "--store", str(s.root),
                            "--request-id", r["id"], "--claim-token", c["claim_token"]]):
        main()
    path = s.root / "airtap_result.json"
    path.write_text(json.dumps(report(s, a, r)))
    with patch("sys.argv", ["jobintel", "submission-airtap-import", "--store", str(s.root), "--request-id", r["id"],
                            "--claim-token", c["claim_token"], "--result-file", str(path)]):
        main()
    captured = capsys.readouterr()
    assert SENTINEL_TOKEN not in captured.out + captured.err + caplog.text + repr(handoff)
    for p in s.root.rglob("*"):
        if p.is_file():
            assert SENTINEL_TOKEN.encode() not in p.read_bytes(), p
    assert "AIRTAP_PERSONAL_ACCESS_TOKEN" not in (Path(handoff.bundle_path) / "submission_request.json").read_text()


def test_repository_fixtures_contain_no_airtap_token():
    root = Path(__file__).resolve().parents[1]
    # An assigned value (not a mention of the variable name) would be a committed secret.
    pattern = re.compile("AIRTAP_PERSONAL" + r"_ACCESS_TOKEN\s*[=:]\s*['\"]?[A-Za-z0-9_.\-]{8,}")
    for p in list((root / "tests").rglob("*")) + list((root / "src").rglob("*")) + list((root / "config").rglob("*")):
        if p.is_file() and p.suffix in {".py", ".json", ".md", ".txt", ".env", ".toml", ".yaml", ".yml"}:
            assert not pattern.search(p.read_text(errors="ignore")), p
    assert not list(root.glob("**/.env"))


# --- CLI ------------------------------------------------------------------------------

def test_cli_airtap_workflow_stops_at_human_review(setup, capsys):
    s = setup
    s.now[0] = datetime.now(timezone.utc)  # CLI commands use the real clock
    a, r, c, _ = start(s)

    def invoke(*args):
        with patch("sys.argv", ["jobintel", *args, "--store", str(s.root)]):
            main()
        return json.loads(capsys.readouterr().out)

    check = invoke("submission-airtap-check", "--request-id", r["id"], "--claim-token", c["claim_token"])
    assert check["live_execution_blockers"] == [] and check["live_execution"] == "supervised_only"
    path = s.root / "airtap_result.json"
    path.write_text(json.dumps(report(s, a, r)))
    out = invoke("submission-airtap-import", "--request-id", r["id"], "--claim-token", c["claim_token"],
                 "--result-file", str(path))
    assert out["status"] == "ready_for_human_review"
    assert invoke("submission-reconcile") == []
    assert not s.apps.for_job("j1").is_submitted
