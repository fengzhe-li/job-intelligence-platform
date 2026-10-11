"""Browserbase as a second stop-only executor through the real SubmissionService lifecycle.

approve -> enqueue("browserbase") -> claim -> execute (service-built executor, sealed
bundle, one transport run) -> strict stop-only import -> ready_for_human_review /
needs_human_input / outcome_unknown. Never a receipt. All synthetic, no internet: the
browser is local Chrome (or no browser at all for tampered-result transports), and the
cloud transport is exercised with a fake Browserbase API + a local CDP endpoint.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jobintel.models.application import Application
from jobintel.models.candidate import CandidateProfile
from jobintel.models.job import Job
from jobintel.profile_ingestion import _write_profile
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.submission import browserbase_service
from jobintel.submission.browserbase_cloud import CloudTransport
from jobintel.submission.browserbase_runner import RunResult, run as runner_run
from jobintel.submission.executors import STOP_ONLY_EXECUTORS
from jobintel.submission.service import SubmissionService
from synthetic_answer_bank import SYNTHETIC_REQUIRED_ANSWERS, write_synthetic_answer_bank
from test_cv_artifact_store import _artifact
from test_handoff_fail_closed import _canonical_bank_values
from test_browserbase_runner_offline import browser  # noqa: F401  (fixture)
from test_supervised_execution import _ack, _approve, _store

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

HOST = "jobs.example.test"
CLOUD_URL = f"https://{HOST}/cloud/apply.html"
SENTINEL_KEY = "bb_live_SENTINEL_" + "51c3" * 8
SCREEN = "Final review page for Graduate Engineer at ExampleCo; Submit application visible, not pressed."


# --- transports -------------------------------------------------------------------------------

LIVE_URL = "https://www.browserbase.example/devtools-fullscreen/inspector.html?wss=live-SECRET-TOKEN-" + "9d" * 8


class FakeTransport:
    """No browser: returns a crafted RunResult (used for tamper, failure and handoff cases).
    Models a keep-alive provider session: RUNNING until released or expired."""

    def __init__(self, make_result=None, *, raise_before_session=None, raise_after_session=None, preflight_error=None,
                 session_id="fake-session-1", release_errors=0, live_view_error=None):
        self.make_result, self.calls = make_result, 0
        self.raise_before_session, self.raise_after_session = raise_before_session, raise_after_session
        self.preflight_error, self.session_id = preflight_error, session_id
        self.provider_status, self.release_calls, self.release_errors = {}, [], release_errors
        self.live_view_calls, self.live_view_error, self.keep_alive_requests = [], live_view_error, []

    def preflight(self):
        if self.preflight_error:
            raise ValueError(self.preflight_error)

    def run(self, bundle, *, write_allowlist, on_session, keep_alive=False, review_seconds=0):
        self.calls += 1
        self.keep_alive_requests.append((keep_alive, review_seconds))
        if self.raise_before_session:
            raise self.raise_before_session
        on_session(self.session_id)
        if self.raise_after_session:
            raise self.raise_after_session
        result = self.make_result(bundle)
        kept = keep_alive and result.status in {"ready_for_human_review", "needs_human_input"}
        self.provider_status[self.session_id] = "RUNNING" if kept else "COMPLETED"
        return result, {"session_id": self.session_id, "released": not kept, "handoff_kept": kept}

    def session_status(self, session_id):
        return self.provider_status.get(session_id, "")

    def live_view(self, session_id, *, expires_in):
        self.live_view_calls.append((session_id, expires_in))
        if self.live_view_error:
            raise self.live_view_error
        return {"live_view_url": LIVE_URL, "expires_in": expires_in, "page_urls": ["https://jobs.example.test/x"]}

    def release(self, session_id):
        self.release_calls.append(session_id)
        if self.release_errors:
            self.release_errors -= 1
            raise RuntimeError("provider unavailable")
        self.provider_status[session_id] = "COMPLETED"


class LocalChromeTransport:
    """Local headless Chrome + in-process synthetic server, driving the unchanged runner."""

    def __init__(self, browser, server):
        self.browser, self.server, self.calls = browser, server, 0

    def preflight(self):
        pass

    def run(self, bundle, *, write_allowlist, on_session, keep_alive=False, review_seconds=0):
        self.calls += 1
        on_session("local-session-1")
        context = self.browser.new_context()
        context.route("**/*", self.server.handle)
        try:
            result = runner_run(context.new_page(), bundle, write_allowlist=write_allowlist)
        finally:
            context.close()
        return result, {"session_id": "local-session-1", "released": True, "final_url": result.current_url}


def ready_result(bundle, **changes):
    values = dict(status="ready_for_human_review", stop_reason="final_page_reached_stop_before_submit",
                  start_identity={"url": bundle.application_url, "title": "Graduate Engineer — ExampleCo",
                                  "company_seen": True, "role_seen": True, "state": "matched", "page": 1},
                  cv_upload={"dom_matches_sealed_file": True}, final_submit_seen=True,
                  evidence=[{"kind": "screen_state", "value": SCREEN, "sha256": "", "detail": "observed"}])
    values.update(changes)
    return RunResult(**values)


# --- synthetic store ------------------------------------------------------------------------------

def build(root: Path, transport=None, *, url=CLOUD_URL, answers=SYNTHETIC_REQUIRED_ANSWERS):
    candidate = CandidateProfile("candidate-1", "Alex Example", 2026, email="alex@example.com", phone="07000000000")
    _write_profile(root, candidate)
    write_synthetic_answer_bank(root, answers)
    job = Job(id="j1", company="ExampleCo", title="Graduate Engineer", description="Graduate engineer role.",
              locations=[], source_observations=[])
    LocalJobStore(root).write_jobs([job])
    pdf = root / "cv_artifacts/pdfs/cv-0123456789ab.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 synthetic browserbase service CV")
    artifact = replace(_artifact("cv-0123456789ab", "j1"), jd_snapshot=job.description, pdf_path=str(pdf),
                       pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest())
    CVArtifactStore(root).save(artifact)
    apps = ApplicationStore(root)
    app = apps.create_application(Application("app:j1", "j1", "ExampleCo", "Graduate Engineer", application_url=url))
    apps.attach_cv(app.id, artifact.id, artifact.selected_project_ids, artifact.evidence_ids_used)
    now = [datetime.now(timezone.utc)]
    svc = SubmissionService(root, clock=lambda: now[0], browserbase_transport=transport)
    return SimpleNamespace(root=root, svc=svc, apps=apps, app=app, artifact=artifact, now=now, transport=transport)


def approve(s):
    package = s.svc.preview(s.app.id)
    return s.svc.approve(s.app.id, package_hash=package.package_hash, reviewer="human", reviewed=True)


def queued(s):
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase")
    c = s.svc.claim(r["id"], worker="runner")
    return r, c


def status(s, r):
    return s.svc.store.requests()[r["id"]]["status"]


def kinds(s, r):
    return [e["kind"] for e in s.svc.store.events() if e["entity_id"] == r["id"]]


def no_receipt_and_not_applied(s):
    assert s.svc.store.receipts() == []
    assert s.svc.reconcile() == []
    application = s.apps.for_job("j1")
    assert not application.is_submitted and application.applied_at is None


# --- 1. registration & happy path ------------------------------------------------------------------

def test_browserbase_is_registered_as_a_stop_only_executor():
    assert STOP_ONLY_EXECUTORS == frozenset({"airtap", "browserbase", "local_browser"})


def test_full_lifecycle_stops_at_ready_for_human_review(tmp_path, browser):
    from test_browserbase_runner_offline import Server
    server = Server()
    s = build(tmp_path, LocalChromeTransport(browser, server))
    r, c = queued(s)
    out = s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert out["status"] == "ready_for_human_review" and out["final_submit_clicked"] is False
    assert out["session_id"] == "local-session-1" and out["session_released"] is True
    assert status(s, r) == "ready_for_human_review"
    assert kinds(s, r) == ["enqueued", "claimed", "handoff_started", "browserbase_bundle_prepared",
                           "browserbase_session_started", "browserbase_evidence", "browserbase_result",
                           "ready_for_human_review"]
    report = next(e["data"] for e in s.svc.store.events() if e["kind"] == "browserbase_result")
    assert report["executor"] == "browserbase" and report["final_submit_clicked"] is False
    assert report["cv_upload_confirmed"] is True
    assert {(f["page_label"], f["value"]) for f in report["fields_filled"]} == {
        ("first name", "Alex"), ("email", "alex@example.com"), ("last name", "Example"), ("country", "United Kingdom")}
    assert [x for x in server.requests if x[0] != "GET" or x[1].endswith("/submit")] == [] and server.external == []
    bundle = json.loads((Path(out["bundle_path"]) / "browserbase_request.json").read_text())
    assert bundle["final_submit_allowed"] is False and bundle["request_id"] == r["id"]
    no_receipt_and_not_applied(s)
    with pytest.raises(ValueError, match="No reported execution result to verify"):
        s.svc.verify(r["id"], reviewer="human", confirmed=True)


# --- 2-6. enqueue gates ----------------------------------------------------------------------------

def test_enqueue_requires_a_live_valid_approval(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result))
    with pytest.raises(ValueError, match="Unknown approval"):
        s.svc.enqueue("approval:missing", executor="browserbase")
    approval = approve(s)
    s.svc.revoke(approval["id"], reviewer="human", reason="changed my mind")
    with pytest.raises(ValueError, match="not active"):
        s.svc.enqueue(approval["id"], executor="browserbase")
    assert s.svc.store.requests() == {}


def test_enqueue_refuses_non_synthetic_hosts_while_live_execution_is_disabled(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result), url="https://careers.realemployer.co.uk/apply/123")
    with pytest.raises(ValueError, match="live execution is not enabled"):
        s.svc.enqueue(approve(s)["id"], executor="browserbase")
    assert s.svc.store.requests() == {}


def test_required_answer_gate_applies(tmp_path):
    key = "sponsorship.now_or_future_sponsorship_required_answer"
    s = build(tmp_path, FakeTransport(ready_result), answers={k: v for k, v in SYNTHETIC_REQUIRED_ANSWERS.items() if k != key})
    with pytest.raises(ValueError, match="require_sponsorship_now_or_future"):
        s.svc.enqueue(approve(s)["id"], executor="browserbase")
    assert s.svc.store.requests() == {}


def test_hard_ineligible_and_protected_career_stage_are_unchanged(tmp_path):
    hard = _store(tmp_path / "hard", title="Infrastructure Engineer",
                  description="Requirements: Candidates must be a Sole British National due to government security requirements.",
                  nationality="Chinese")
    with pytest.raises(ValueError, match="HARD_INELIGIBLE"):
        hard.svc.preview(hard.app.id, _ack(hard))
    protected = _store(tmp_path / "protected", title="Graduate Software Engineer",
                       description="Our Graduate Programme welcomes candidates completing their degree.")
    with pytest.raises(ValueError, match="not available under this application policy"):
        protected.svc.preview(protected.app.id, _ack(protected))
    assert hard.svc.store.approvals() == {} and protected.svc.store.approvals() == {}


def test_supervised_acknowledgement_binding_is_carried_and_enforced(tmp_path):
    s = _store(tmp_path)
    s.svc._browserbase_transport = FakeTransport(lambda b: ready_result(
        b, start_identity={"url": b.application_url, "title": "x", "company_seen": True, "role_seen": True,
                           "state": "matched", "page": 1}))
    ack = _ack(s)
    _, approval = _approve(s, ack)
    r = s.svc.enqueue(approval["id"], executor="browserbase")
    c = s.svc.claim(r["id"], worker="runner")
    out = s.svc.execute(r["id"], claim_token=c["claim_token"])
    bundle = json.loads((Path(out["bundle_path"]) / "browserbase_request.json").read_text())
    assert bundle["review"]["authorization"] == "SUPERVISED_EXECUTION_ONLY"
    assert bundle["supervised_acknowledgement"]["final_submit_allowed"] is False
    with pytest.raises(ValueError, match="bound to a different application/job/CV"):
        s.svc.preview(s.app.id, _ack(s, cv_artifact_id="cv-ffffffffffff"))


# --- 7-12. lifecycle guards ------------------------------------------------------------------------

def test_generic_complete_is_rejected(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result))
    r, c = queued(s)
    s.svc.execute(r["id"], claim_token=c["claim_token"])
    from jobintel.models.submission import ExecutionResult, ReceiptEvidence
    forged = ExecutionResult(r["id"], r["package_hash"], "j1", "cv-0123456789ab", CLOUD_URL, "browserbase",
                             "reported_submitted", (ReceiptEvidence("manual_verified", "I saw it submit."),),
                             submitted_at=s.now[0].isoformat())
    with pytest.raises(ValueError, match="generic completion is forbidden"):
        s.svc.complete(r["id"], claim_token=c["claim_token"], result=forged)
    no_receipt_and_not_applied(s)


def test_caller_injected_executor_cannot_bypass_service_construction(tmp_path):
    transport = FakeTransport(ready_result)
    s = build(tmp_path, transport)
    r, c = queued(s)

    class Impostor:
        name = "browserbase"

        def submit(self, package, *, idempotency_key):
            raise AssertionError("must never run")

    with pytest.raises(ValueError, match="constructed only from a live service approval"):
        s.svc.execute(r["id"], claim_token=c["claim_token"], executor=Impostor())
    assert transport.calls == 0 and "handoff_started" not in kinds(s, r)


def test_stale_cv_or_expired_approval_invalidates_execution(tmp_path):
    transport = FakeTransport(ready_result)
    s = build(tmp_path, transport)
    r, c = queued(s)
    sealed = next((s.root / "submissions/packages").rglob("cv.pdf"))
    sealed.chmod(0o600)
    sealed.write_bytes(b"%PDF-1.4 edited after approval")
    with pytest.raises(ValueError, match="Approval invalidated"):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert transport.calls == 0 and status(s, r) == "outcome_unknown"

    s2 = build(tmp_path / "expired", FakeTransport(ready_result))
    r2, c2 = queued(s2)
    s2.now[0] += timedelta(days=2)
    with pytest.raises(ValueError):
        s2.svc.execute(r2["id"], claim_token=c2["claim_token"])
    assert s2.transport.calls == 0 and "handoff_started" not in kinds(s2, r2)


def test_claim_and_lease_are_enforced(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result))
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase")
    with pytest.raises(ValueError, match="Invalid claim token"):
        s.svc.execute(r["id"], claim_token="not-a-token")
    c = s.svc.claim(r["id"], worker="runner", lease_seconds=60)
    s.now[0] += timedelta(seconds=61)
    with pytest.raises(ValueError, match="Claim expired"):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert status(s, r) == "outcome_unknown" and s.transport.calls == 0


def test_one_request_per_approval_and_no_retry_after_unknown(tmp_path):
    s = build(tmp_path, FakeTransport(raise_before_session=RuntimeError("boom")))
    approval = approve(s)
    r = s.svc.enqueue(approval["id"], executor="browserbase")
    assert s.svc.enqueue(approval["id"], executor="browserbase")["id"] == r["id"]
    with pytest.raises(ValueError, match="cannot change executor"):
        s.svc.enqueue(approval["id"], executor="airtap")
    c = s.svc.claim(r["id"], worker="runner")
    with pytest.raises(ValueError, match="Executor interrupted"):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert status(s, r) == "outcome_unknown"
    with pytest.raises(ValueError, match="cannot be retried automatically"):
        s.svc.enqueue(approval["id"], executor="browserbase")
    with pytest.raises(ValueError):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert s.transport.calls == 1
    no_receipt_and_not_applied(s)


# --- 13-15. credentials and the shared importer ------------------------------------------------------

def test_missing_credentials_fail_before_anything_is_journaled(tmp_path):
    s = build(tmp_path)  # default transport: the real CloudTransport
    s.svc._browserbase_transport = CloudTransport(environ={}, post=lambda *a: pytest.fail("API must not be called"))
    r, c = queued(s)
    with pytest.raises(ValueError, match="BROWSERBASE_API_KEY is not set"):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert status(s, r) == "executing" and "handoff_started" not in kinds(s, r)
    assert not (s.root / "submissions/browserbase").exists()


@pytest.mark.parametrize("tamper,expected", [
    (dict(final_submit_clicked=True), "invalid"),
    (dict(status="submitted"), "invalid"),
    (dict(evidence=[{"kind": "confirmation_text", "value": "Thank you, your application was submitted",
                     "sha256": "", "detail": "page text"}]), "outcome_unknown"),
])
def test_shared_importer_rejects_any_claim_of_submission(tmp_path, tamper, expected):
    s = build(tmp_path, FakeTransport(lambda b: ready_result(b, **tamper)))
    r, c = queued(s)
    if expected == "invalid":
        with pytest.raises(ValueError, match="Invalid Browserbase report"):
            s.svc.execute(r["id"], claim_token=c["claim_token"])
    else:
        assert s.svc.execute(r["id"], claim_token=c["claim_token"])["status"] == "outcome_unknown"
    assert status(s, r) == "outcome_unknown"
    no_receipt_and_not_applied(s)


def test_ready_without_confirmed_cv_is_needs_human_input(tmp_path):
    s = build(tmp_path, FakeTransport(lambda b: ready_result(b, cv_upload={})))
    r, c = queued(s)
    assert s.svc.execute(r["id"], claim_token=c["claim_token"])["status"] == "needs_human_input"


# --- recovery ----------------------------------------------------------------------------------------

def test_interruption_before_session_creation_is_outcome_unknown(tmp_path):
    s = build(tmp_path, FakeTransport(raise_before_session=RuntimeError("network down")))
    r, c = queued(s)
    with pytest.raises(ValueError, match="Executor interrupted"):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert "browserbase_session_started" not in kinds(s, r) and status(s, r) == "outcome_unknown"
    no_receipt_and_not_applied(s)


def test_process_death_after_session_start_stays_unknown_never_submitted(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result, raise_after_session=KeyboardInterrupt()))
    r, c = queued(s)
    with pytest.raises(KeyboardInterrupt):  # simulates the process dying mid-run
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert status(s, r) == "executing" and "browserbase_session_started" in kinds(s, r)
    s.now[0] += timedelta(hours=1)
    s.svc.list()  # recovery pass
    assert status(s, r) == "outcome_unknown"
    with pytest.raises(ValueError):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    no_receipt_and_not_applied(s)


def test_malformed_result_is_outcome_unknown(tmp_path):
    s = build(tmp_path, FakeTransport(lambda b: ready_result(b, status="all good")))
    r, c = queued(s)
    with pytest.raises(ValueError, match="Invalid Browserbase report"):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert status(s, r) == "outcome_unknown"
    reasons = [e["data"].get("reason") for e in s.svc.store.events() if e["kind"] == "outcome_unknown"]
    assert reasons == ["invalid_browserbase_report"]


# --- CLI and hygiene ----------------------------------------------------------------------------------

def test_cli_has_no_final_submit_and_browserbase_execute_fails_closed_without_credentials(tmp_path, capsys, monkeypatch):
    from jobintel.cli.main import main
    import argparse
    from jobintel.submission.cli import add_submission_commands
    parser = argparse.ArgumentParser()
    add_submission_commands(parser.add_subparsers(dest="command"))
    options = {opt for action in parser._subparsers._group_actions for sub in action.choices.values()
               for a in sub._actions for opt in a.option_strings}
    assert not any("final" in o or "force" in o or "submit-now" in o for o in options)

    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    s = build(tmp_path)
    approval = approve(s)

    def cli(*args):
        with patch("sys.argv", ["jobintel", *args, "--store", str(s.root)]):
            main()
        return json.loads(capsys.readouterr().out)

    r = cli("submission-enqueue", "--approval-id", approval["id"], "--executor", "browserbase")
    c = cli("submission-claim", "--request-id", r["id"], "--worker", "cli")
    with patch("sys.argv", ["jobintel", "submission-execute", "--request-id", r["id"], "--claim-token",
                            c["claim_token"], "--store", str(s.root)]), pytest.raises(SystemExit):
        main()
    assert "BROWSERBASE_API_KEY is not set" in capsys.readouterr().err
    assert "handoff_started" not in kinds(s, r)


def test_no_candidate_literals_in_browserbase_service_modules():
    from jobintel.submission import browserbase_cloud
    reference = _canonical_bank_values()
    texts = [inspect.getsource(browserbase_service), inspect.getsource(browserbase_cloud)]
    assert sum(1 for v in reference for t in texts if v in t) == 0
