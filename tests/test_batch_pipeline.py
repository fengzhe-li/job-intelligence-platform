"""Batch pipeline (synthetic, offline): ledger, prepare, queue, approve, run-next, status.

Synthetic candidates, jobs and hosts only. CV generation is a deterministic fake that writes a
real registered artifact (the generator itself is covered by its own suites); the multi-tab case
drives the REAL shared local browser (headless Chrome) against the synthetic hosted form served
in-process, through the unchanged apply_one.start -> service -> stop-only runner.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import threading

import pytest

from jobintel.models.candidate import CandidateProfile
from jobintel.models.job import Job, SourceObservation
from jobintel.pipeline import batch
from jobintel.profile_ingestion import _write_profile
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.submission.local_browser import LocalBrowserTransport
from jobintel.submission.service import SubmissionService
from synthetic_answer_bank import write_synthetic_answer_bank
from test_apply_one import SYNTHETIC, profile_for
from test_browserbase_graphql_and_identity import HOST
from test_cv_artifact_store import _artifact
from test_local_browser_executor import OFFLINE
from test_local_cv_upload_and_fields import HostedServer
from test_local_cv_upload_service import BANK

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
CONFIG = "config/personal_strategy.json"


def _job(job_id, *, url, title="Graduate Engineer", company="ExampleCo", description="Graduate engineer role."):
    obs = SourceObservation(source_name="ashby", source_job_id=job_id, original_url=f"https://src.example.test/{job_id}",
                            first_seen_at=NOW, last_seen_at=NOW, posted_at=NOW, raw_description=description,
                            canonical_application_url=url)
    return Job(id=job_id, company=company, title=title, description=description, locations=[], source_observations=[obs])


def supported(job_id):
    return f"https://{HOST}/{job_id}/hosted_form.html?mode=ok"


class FakeGenerator:
    """Writes a real, registered, hash-verified artifact bound to the job/JD/candidate."""

    def __init__(self, fail_for=()):
        self.calls, self.fail_for = [], set(fail_for)

    def __call__(self, candidate, job, cv_store, *, max_projects, application_id, store_root):
        self.calls.append(job.id)
        if job.id in self.fail_for:
            raise RuntimeError(f"synthetic generator failure for {job.id}")
        artifact_id = f"cv-{hashlib.sha256((job.id + str(len(self.calls))).encode()).hexdigest()[:12]}"
        pdf = Path(store_root) / "cv_artifacts/pdfs" / f"{artifact_id}.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(f"%PDF-1.4 synthetic batch CV {job.id} {len(self.calls)}".encode())
        artifact = replace(_artifact(artifact_id, job.id, application_id), jd_snapshot=job.description, pdf_path=str(pdf),
                           pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(), candidate_id=candidate.id,
                           document={"headline": job.title, "projects": [
                               {"project_id": "p1", "emphasis": "focus", "bullets": [{"text": "a"}, {"text": "b"}]}]})
        cv_store.save(artifact)
        return artifact


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "store"
    candidate = CandidateProfile("candidate-1", "Alex Example", 2026, email="alex@example.com", phone="07000000000",
                                 citizenship="Samplish")
    _write_profile(root, candidate)
    write_synthetic_answer_bank(root, BANK)
    jobs = [_job("j1", url=supported("j1")), _job("j2", url=supported("j2")),
            _job("j5", url=supported("j5"), title="Junior Engineer", description="Junior engineer role. Required: Python."),
            _job("j3", url="https://careers.unreviewed.test/apply/j3"),
            _job("j4", url=supported("j4"), description="Graduate engineer role. Must be a sole British national.")]
    LocalJobStore(root).write_jobs(jobs)
    return root


NO_FORM_RESTRICTIONS = lambda url: ""  # the pre-tab form check reads no restriction wording


def svc_for(root, transport=None):
    return SubmissionService(root, CONFIG, local_browser_transport=transport)


def prepare(root, gen=None, **kw):
    kw.setdefault("job_ids", ("j1", "j2", "j3", "j4", "j5"))
    return batch.prepare(root, CONFIG, batch_id="b1", export_dir=root / "exports", profiles=SYNTHETIC,
                         svc=svc_for(root), generate=gen or FakeGenerator(), **kw)


def states(root):
    return {v.application_id: v.state for v in batch.items(root, CONFIG, "b1", svc=svc_for(root), check_tabs=False)}


# --- ledger -------------------------------------------------------------------------------------------

def test_ledger_is_hash_chained_idempotent_and_fails_closed(tmp_path):
    ledger = batch.BatchLedger(tmp_path)
    first = ledger.append("item_added", "b1", "app:x", {"rank": 1}, event_id="e1")
    assert ledger.append("item_added", "b1", "app:x", {"rank": 1}, event_id="e1") == first  # replay: no-op
    with pytest.raises(batch.BatchError, match="Conflicting replay"):
        ledger.append("item_added", "b1", "app:x", {"rank": 2}, event_id="e1")
    raw = ledger.path.read_text().replace('"rank": 1', '"rank": 9')
    ledger.path.write_text(raw)
    with pytest.raises(batch.BatchError, match="integrity"):
        ledger.events()


# --- prepare: eligibility, routing, per-job CV binding, isolation, idempotency ---------------------------

def test_prepare_routes_each_job_and_binds_its_own_cv(store):
    gen = FakeGenerator()
    report = {r["job_id"]: r for r in prepare(store, gen)}
    assert report["j4"]["state"] == "ineligible" and "sole British" in report["j4"]["reason"]
    assert report["j3"]["state"] == "unsupported_ats_manual"
    assert {report["j1"]["state"], report["j2"]["state"]} == {"cv_review_required"}
    assert sorted(gen.calls) == ["j1", "j2", "j3", "j5"]  # never a CV for an ineligible job
    assert report["j5"]["gate"] == "block_auto_submission"  # Python required, no evidence: needs an acknowledgement
    apps = {a.id: a for a in ApplicationStore(store).read_applications()}
    cvs = {a.id: a for a in CVArtifactStore(store).read_all()}
    for job_id in ("j1", "j2", "j3", "j5"):  # no cross-contamination: each application has its own job's CV
        app = apps[f"app:{job_id}"]
        assert cvs[app.cv_version_id].job_id == job_id and cvs[app.cv_version_id].jd_snapshot == app.jd_snapshot
    rows = {r["application_id"]: r for r in batch.queue_rows(store, CONFIG, "b1", svc=svc_for(store))}
    assert rows["app:j3"]["ats_supported"] is False and "careers.unreviewed.test" in rows["app:j3"]["ats"]
    assert rows["app:j4"]["eligibility"] == "hard_ineligible" and rows["app:j4"]["eligibility_reason"]
    assert rows["app:j1"]["package_hash"] and rows["app:j1"]["cv_sha256"] == cvs[apps["app:j1"].cv_version_id].pdf_sha256


def test_rerun_is_idempotent_and_reuses_unchanged_cvs(store):
    gen = FakeGenerator()
    prepare(store, gen)
    events_before = batch.BatchLedger(store).events()
    artifacts_before = len(CVArtifactStore(store).read_all())
    report = prepare(store, gen)
    assert len(gen.calls) == 4  # nothing regenerated
    assert len(CVArtifactStore(store).read_all()) == artifacts_before
    assert all(r.get("cv_reused") in (True, None) for r in report)
    assert len(batch.BatchLedger(store).events()) == len(events_before)  # no duplicate events
    assert len([a for a in ApplicationStore(store).read_applications()]) == 5  # one application per job


def test_a_changed_jd_gets_a_new_cv_and_old_artifacts_stay_immutable(store):
    gen = FakeGenerator()
    prepare(store, gen)
    old = {a.id: (a.pdf_sha256, open(a.pdf_path, "rb").read()) for a in CVArtifactStore(store).read_all()}
    jobs = [replace(j, description=j.description + " Also Kubernetes.") if j.id == "j1" else j
            for j in LocalJobStore(store).read_jobs()]
    LocalJobStore(store)._persist(jobs)  # the JD changed at the source
    prepare(store, gen)
    assert gen.calls.count("j1") == 2 and gen.calls.count("j2") == 1
    for artifact_id, (sha, data) in old.items():  # historical artifacts are never modified
        artifact = next(a for a in CVArtifactStore(store).read_all() if a.id == artifact_id)
        assert artifact.pdf_sha256 == sha and open(artifact.pdf_path, "rb").read() == data


def test_one_failed_job_does_not_stop_or_corrupt_the_others(store):
    report = {r["job_id"]: r for r in prepare(store, FakeGenerator(fail_for={"j1"}))}
    assert report["j1"]["state"] == "failed" and report["j1"]["stage"] == "cv" and "synthetic" in report["j1"]["error"]
    assert report["j2"]["state"] == "cv_review_required"
    assert states(store)["app:j1"] == "failed"
    # resume: the failure is safe to retry and nothing else is redone
    gen = FakeGenerator()
    prepare(store, gen)
    assert gen.calls == ["j1"] and states(store)["app:j1"] == "cv_review_required"


def test_an_application_with_an_active_attempt_is_never_touched(store):
    prepare(store)
    batch.approve(store, CONFIG, batch_id="b1", application_id="app:j1", reviewer="human", reviewed=True,
                  acknowledge_review_reasons=True, profiles=SYNTHETIC, svc=svc_for(store))

    def started(svc, application_id, **kw):
        svc.enqueue(svc.approve(application_id, package_hash=kw["package_hash"], reviewer="human", reviewed=True,
                                acknowledge_reasons=True)["id"], executor="local_browser", live_host=HOST)
        return {"request_id": None, "status": "submission_requested"}

    batch.run_next(store, CONFIG, form_text=NO_FORM_RESTRICTIONS, batch_id="b1", profiles=SYNTHETIC, svc=svc_for(store), start=started)
    gen = FakeGenerator()
    report = {r["job_id"]: r for r in prepare(store, gen)}
    assert report["j1"]["state"] == "in_progress" and "j1" not in gen.calls
    assert states(store)["app:j1"] == "automation_active"


def test_resume_after_a_hard_interruption_reuses_the_cv_and_finishes(store, monkeypatch):
    from jobintel.submission import apply_one
    real, calls = apply_one.preview, []

    def interrupted(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt  # the process is killed after the CV, before the package preview
        return real(*args, **kwargs)

    monkeypatch.setattr(apply_one, "preview", interrupted)
    gen = FakeGenerator()
    with pytest.raises(KeyboardInterrupt):
        prepare(store, gen, job_ids=("j1",))
    assert gen.calls == ["j1"] and states(store)["app:j1"] == "cv_preparing"
    prepare(store, gen, job_ids=("j1",))
    assert gen.calls == ["j1"]  # resumed: the CV was not regenerated
    assert states(store)["app:j1"] == "cv_review_required"


# --- approve -------------------------------------------------------------------------------------------

def test_approve_binds_the_exact_package_and_refuses_unsupported_or_ineligible(store):
    prepare(store)
    with pytest.raises(batch.BatchError, match="--reviewed"):
        batch.approve(store, CONFIG, batch_id="b1", application_id="app:j1", reviewer="human", reviewed=False,
                      profiles=SYNTHETIC, svc=svc_for(store))
    for app_id, state in (("app:j3", "unsupported_ats_manual"), ("app:j4", "ineligible")):
        with pytest.raises(batch.BatchError, match=state):
            batch.approve(store, CONFIG, batch_id="b1", application_id=app_id, reviewer="human", reviewed=True,
                          acknowledge_review_reasons=True, profiles=SYNTHETIC, svc=svc_for(store))
    # a blocked package needs the human's explicit acknowledgement of exactly its reasons
    with pytest.raises(ValueError, match="acknowledge"):
        batch.approve(store, CONFIG, batch_id="b1", application_id="app:j5", reviewer="human", reviewed=True,
                      acknowledge_review_reasons=True, profiles=SYNTHETIC, svc=svc_for(store))
    blocked = batch.approve(store, CONFIG, batch_id="b1", application_id="app:j5", reviewer="human", reviewed=True,
                            acknowledge_review_reasons=True, acknowledge_blockers=True, ack_reason="Reviewed.",
                            profiles=SYNTHETIC, svc=svc_for(store))
    assert blocked["execution_mode"] == "SUPERVISED_EXECUTION_ONLY" and blocked["package_hash"]
    out = batch.approve(store, CONFIG, batch_id="b1", application_id="app:j1", reviewer="human", reviewed=True,
                        acknowledge_review_reasons=True, profiles=SYNTHETIC, svc=svc_for(store))
    assert out["package_hash"] and out["expected_company_names"] and out["host"] == HOST
    st = states(store)
    assert st["app:j1"] == st["app:j5"] == "ready_for_execution" and st["app:j2"] == "cv_review_required"


def test_a_review_required_package_needs_the_review_reasons_acknowledged(store, monkeypatch):
    from jobintel.submission import apply_one
    prepare(store)
    real = apply_one.preview
    monkeypatch.setattr(apply_one, "preview", lambda *a, **k: {**real(*a, **k), "gate_status": "review_required"})
    with pytest.raises(batch.BatchError, match="acknowledge-review-reasons"):
        batch.approve(store, CONFIG, batch_id="b1", application_id="app:j1", reviewer="human", reviewed=True,
                      profiles=SYNTHETIC, svc=svc_for(store))


# --- run-next: sequential, isolated, never a retry, never an unapproved item ----------------------------

def _approve_all(store, *app_ids):
    for app_id in app_ids:
        blocked = app_id == "app:j5"  # j5's JD requires Python the synthetic candidate has no evidence for
        batch.approve(store, CONFIG, batch_id="b1", application_id=app_id, reviewer="human", reviewed=True,
                      acknowledge_review_reasons=True, acknowledge_blockers=blocked,
                      ack_reason="Reviewed the blocker; supervised attempt only." if blocked else None,
                      profiles=SYNTHETIC, svc=svc_for(store))


def test_run_next_executes_only_approved_items_one_after_another_and_isolates_failures(store):
    prepare(store)
    _approve_all(store, "app:j1", "app:j2")
    calls = []

    def fake_start(svc, application_id, **kw):
        calls.append((application_id, kw["package_hash"], kw["reviewed"]))
        if application_id == "app:j1":
            raise RuntimeError("synthetic executor failure")
        return {"request_id": None, "status": "needs_human_input", "handoff": "local_browser_active",
                "final_submit_clicked": False}

    results = batch.run_next(store, CONFIG, form_text=NO_FORM_RESTRICTIONS, batch_id="b1", max_items=5, profiles=SYNTHETIC, svc=svc_for(store),
                             start=fake_start)
    assert [c[0] for c in calls] == ["app:j1", "app:j2"] and all(c[2] is True for c in calls)
    assert "synthetic executor failure" in results[0]["error"] and results[1]["status"] == "needs_human_input"
    assert states(store)["app:j1"] == "failed"  # recorded, never retried
    assert batch.run_next(store, CONFIG, form_text=NO_FORM_RESTRICTIONS, batch_id="b1", max_items=5, profiles=SYNTHETIC, svc=svc_for(store),
                          start=fake_start) == []
    assert len(calls) == 2


def test_only_one_executor_at_a_time(store):
    prepare(store)
    _approve_all(store, "app:j1")
    entered, release = threading.Event(), threading.Event()

    def slow_start(svc, application_id, **kw):
        entered.set()
        release.wait(10)
        return {"request_id": None, "status": "needs_human_input"}

    worker = threading.Thread(target=lambda: batch.run_next(store, CONFIG, form_text=NO_FORM_RESTRICTIONS, batch_id="b1", profiles=SYNTHETIC,
                                                            svc=svc_for(store), start=slow_start))
    worker.start()
    assert entered.wait(10)
    holder = batch.executor_holder(store)
    assert holder and holder["pid"] == os.getpid() and holder["label"] == "batch b1"
    with pytest.raises(batch.BatchError, match="already active"):
        batch.run_next(store, CONFIG, form_text=NO_FORM_RESTRICTIONS, batch_id="b1", profiles=SYNTHETIC, svc=svc_for(store), start=slow_start)
    release.set()
    worker.join(10)
    assert batch.executor_holder(store) is None


def test_queue_and_status_files_are_written_and_explain_next_actions(store):
    prepare(store)
    path, rows = batch.write_queue(store, CONFIG, "b1", svc=svc_for(store))
    text = path.read_text()
    for needle in ("ExampleCo", "Graduate Engineer", "Review gate", "UNSUPPORTED", "Acknowledgement required",
                   "Package hash", "Eligibility: hard_ineligible"):
        assert needle in text
    st = batch.status(store, CONFIG, "b1", svc=svc_for(store))
    assert st["executor"] is None and st["counts"]["cv_review_required"] == 3
    assert all(item["next_action"] for item in st["items"])


# --- REAL multi-tab lifecycle: one shared headed-mode browser, one tab per application -------------------

def apply_one_status(svc, request_id):
    from jobintel.submission import apply_one
    return apply_one.status(svc, request_id=request_id)


def _shared(root, server):
    return LocalBrowserTransport(runtime_dir=root.parent / "runtime" / "local_browser", headless=True,
                                 serve=server.handle, extra_args=OFFLINE, shared_browser=True)


def test_sequential_automation_with_multi_tab_handoff(store, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from jobintel.submission import browserbase_service
    monkeypatch.setattr(browserbase_service, "is_synthetic_host", lambda url: False)
    server = HostedServer()
    transport = _shared(store, server)
    svc = svc_for(store, transport)
    prepare(store)
    _approve_all(store, "app:j1", "app:j2")
    try:
        results = batch.run_next(store, CONFIG, form_text=NO_FORM_RESTRICTIONS, batch_id="b1", max_items=5, profiles=SYNTHETIC, svc=svc)
        assert [r["application_id"] for r in results] == ["app:j1", "app:j2"]
        a, b = results
        for r in results:  # each stopped at its own human handoff; Submit never clicked
            assert r["status"] == "needs_human_input" and r["final_submit_clicked"] is False
            assert r["handoff"] == "local_browser_active" and r["cv_upload_status"] == "uploaded"
        assert "ApiSubmitApplication" not in server.operations and server.external == []
        for r in (svc.store.requests()[x["request_id"]] for x in results):  # human-only fields stayed untouched
            filled = {f["field"] for f in apply_one_status(svc, r["id"])["fields_filled"]}
            escalated = apply_one_status(svc, r["id"])["fields_escalated"]
            assert not filled & {"require_sponsorship_now_or_future", "permanent_or_unrestricted_right_to_work",
                                 "right_to_work_uk"}
            assert escalated  # the open free-text questions are left for the human
        # one shared browser, two different tabs, each bound to its own request
        assert a["shared_browser_id"] == b["shared_browser_id"] and a["shared_browser_id"]
        assert a["target_id"] != b["target_id"] and a["tab_session_id"] != b["tab_session_id"]
        assert transport.session_status(a["tab_session_id"]) == "RUNNING"
        assert transport.session_status(b["tab_session_id"]) == "RUNNING"
        requests = svc.store.requests()
        assert requests[a["request_id"]]["application_id"] == "app:j1"
        assert requests[b["request_id"]]["application_id"] == "app:j2"
        assert requests[a["request_id"]]["package_hash"] != requests[b["request_id"]]["package_hash"]
        view = {v.application_id: v for v in batch.items(store, CONFIG, "b1", svc=svc)}
        assert view["app:j1"].state == view["app:j2"].state == "handoff_open"
        # the human finishes A (release its tab): B's tab and the shared browser are untouched
        from jobintel.submission import apply_one
        released = apply_one.release(svc, request_id=a["request_id"])
        assert released["released"] is True
        assert transport.session_status(a["tab_session_id"]) == "COMPLETED"
        assert transport.session_status(b["tab_session_id"]) == "RUNNING"
        assert transport.shared_browser_state()["running"] is True
        assert {v.application_id: v.state for v in batch.items(store, CONFIG, "b1", svc=svc)}["app:j1"] == "needs_human_input"
        # a later batch command never resumes a handed-off tab
        assert batch.run_next(store, CONFIG, form_text=NO_FORM_RESTRICTIONS, batch_id="b1", max_items=5, profiles=SYNTHETIC, svc=svc) == []
        svc.close_not_submitted(a["request_id"], reviewer="human", confirmed_not_submitted=True, reason="test")
        assert {v.application_id: v.state for v in batch.items(store, CONFIG, "b1", svc=svc)}["app:j1"] == \
            "closed_not_submitted"
    finally:
        record = transport._shared_path()
        if record.exists():
            pid = json.loads(record.read_text())["pid"]
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


# --- employer restrictions: surfaced, scoped, never a rejection -------------------------------------------

def test_a_previous_application_to_the_same_company_never_rejects_another_role(store):
    prepare(store)
    st = states(store)
    assert st["app:j1"] == st["app:j2"] == "cv_review_required"  # same company, both stay eligible


def test_a_reported_exact_role_warning_flags_only_that_role(store):
    prepare(store)
    out = batch.note_restriction(store, batch_id="b1", application_id="app:j1",
                                 wording="You can reapply to this role after 90 days.")
    assert out["classified"][0]["kind"] == "exact_role" and out["classified"][0]["company_wide"] is False
    assert {h["job_id"] for h in out["company_history"]} >= {"j1", "j2"}  # compared by exact job id
    rows = {r["application_id"]: r for r in batch.queue_rows(store, CONFIG, "b1", svc=svc_for(store))}
    assert rows["app:j1"]["restrictions"][0]["binding"] is True
    assert rows["app:j2"]["restrictions"] == []  # never widened to another role
    assert states(store)["app:j2"] == "cv_review_required"


def test_a_company_wide_limit_is_surfaced_on_every_same_company_item(store):
    prepare(store)
    batch.note_restriction(store, batch_id="b1", application_id="app:j1",
                           wording="Candidates may submit one application every 6 months.")
    rows = {r["application_id"]: r for r in batch.queue_rows(store, CONFIG, "b1", svc=svc_for(store))}
    assert rows["app:j2"]["restrictions"][0]["kind"] == "company_frequency"
    assert rows["app:j2"]["restrictions"][0]["binding"] is True
    st = batch.status(store, CONFIG, "b1", svc=svc_for(store))
    assert st["company_wide_restrictions"][0]["wording"] == "Candidates may submit one application every 6 months."
    assert "COMPANY-WIDE" in batch.write_queue(store, CONFIG, "b1", svc=svc_for(store))[0].read_text()


def test_unclassifiable_reported_wording_is_kept_as_ambiguous(store):
    prepare(store)
    out = batch.note_restriction(store, batch_id="b1", application_id="app:j1", wording="Something odd happened.")
    assert out["classified"][0]["kind"] == "ambiguous" and out["classified"][0]["wording"] == "Something odd happened."


def test_a_human_can_remove_an_item_which_is_kept_and_never_executed(store):
    prepare(store)
    out = batch.remove(store, batch_id="b1", application_id="app:j2", reviewer="human", reason="weak fit")
    assert out["state"] == "removed" and states(store)["app:j2"] == "removed"
    assert any(a.id == "app:j2" for a in ApplicationStore(store).read_applications())  # kept
    with pytest.raises(batch.BatchError, match="removed"):
        batch.approve(store, CONFIG, batch_id="b1", application_id="app:j2", reviewer="human", reviewed=True,
                      acknowledge_review_reasons=True, profiles=SYNTHETIC, svc=svc_for(store))


def test_ambiguous_wording_is_shown_as_a_non_binding_note_on_same_company_items(store):
    prepare(store)
    batch.note_restriction(store, batch_id="b1", application_id="app:j1", wording="Something about 90 days.")
    rows = {r["application_id"]: r for r in batch.queue_rows(store, CONFIG, "b1", svc=svc_for(store))}
    note = rows["app:j2"]["restrictions"][0]
    assert note["kind"] == "ambiguous" and note["binding"] is False and note["observed_on_this_job"] is False
    assert states(store)["app:j2"] == "cv_review_required"


def test_a_changed_jd_analysis_regenerates_and_an_explicit_regenerate_is_recorded(store, monkeypatch):
    gen = FakeGenerator()
    prepare(store, gen)
    calls = len(gen.calls)
    prepare(store, gen, regenerate=("j2",))
    assert gen.calls[calls:] == ["j2"]
    latest = [e for e in batch.BatchLedger(store).events(application_id="app:j2") if e["kind"] == "cv_prepared"][-1]
    assert latest["data"]["regenerated_on_request"] is True
    monkeypatch.setattr(batch, "jd_signature", lambda job: "changed-analysis")
    prepare(store, gen)
    assert sorted(gen.calls[calls + 1:]) == ["j1", "j2", "j3", "j5"]  # the JD analysis changed for every job
