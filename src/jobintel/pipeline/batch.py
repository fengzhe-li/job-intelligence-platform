"""Batch application pipeline: orchestration over the single-application supervised path.

Nothing here applies, fills or submits by itself. Every step reuses the production component
that already does it for one application -- ranking (`rank_jobs`), eligibility
(`evaluate_job_eligibility`), the one-per-job application (`ensure_application`), role-specific CV
generation and attachment, the review gate and sealed package (`apply_one.preview`), and the
supervised execution (`apply_one.start` -> the submission service -> the stop-only runner). The
batch adds only:

* an append-only, hash-chained ledger (data/local/batch/events.jsonl) of the decisions no other
  store holds: which jobs a batch covers, the eligibility verdict and its exact source, the rank
  rationale, the CV input fingerprint, ATS routing, the human's execution approval of an exact
  package hash, and per-item failures;
* a derived state per item, computed from that ledger AND the existing stores (applications,
  CV artifacts, request ledger) -- never a second copy of their state;
* sequential automation with multi-tab human handoff: ONE executor at a time (an exclusive
  executor lock), each application in a NEW tab of one shared headed browser, handed to the
  human and never automated again.

Final Submit is never clicked; work-authorisation and AI-prohibited questions stay with the human
(enforced unchanged by the runner); unknown ATS hosts are routed to a visible backlog.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path

from jobintel.models.submission import canonical, fingerprint, utcnow
from jobintel.storage.locking import store_lock, sync_directory

BATCH_DIR = "batch"
LEDGER = "events.jsonl"
EXECUTOR_LOCK = "executor.lock"
EXECUTOR = "local_browser"

# Request statuses (SubmissionStore) -> batch state while that request is the item's latest attempt.
_REQUEST_STATE = {
    "submission_requested": "automation_active",
    "executing": "automation_active",
    "needs_human_input": "needs_human_input",
    "ready_for_human_review": "ready_for_manual_submit",
    "outcome_unknown": "needs_attention",
    "pending_verification": "needs_attention",
    "verified": "submitted",
    "submitted": "submitted",
    "closed_not_submitted": "closed_not_submitted",
    "failed": "failed",
    "cancelled": "closed_not_submitted",
}
STATES = ("discovered", "ineligible", "cv_preparing", "cv_review_required", "ready_for_execution",
          "unsupported_ats_manual", "automation_active", "handoff_open", "needs_human_input",
          "ready_for_manual_submit", "needs_attention", "closed_not_submitted", "submitted", "skipped", "failed",
          "removed")
# What the human / the operator does next, per state.
NEXT_ACTION = {
    "ineligible": "none (hard eligibility conflict recorded)",
    "cv_preparing": "re-run batch-prepare to finish preparation",
    "cv_review_required": "review the CV + package, then batch-approve",
    "ready_for_execution": "queued for automation: batch-run-next",
    "unsupported_ats_manual": "no reviewed ATS profile: ATS-support backlog",
    "automation_active": "wait: automation is driving this tab",
    "handoff_open": "human: answer the human-only questions in its tab, then submit or close",
    "needs_human_input": "human: tab closed/released -- record submission-human-submitted or submission-close",
    "ready_for_manual_submit": "human: review its tab and click Submit yourself, then record it",
    "needs_attention": "investigate this attempt; it is never retried automatically",
    "closed_not_submitted": "closed (not submitted); batch-approve again only for a deliberate new attempt",
    "submitted": "none (submitted)",
    "skipped": "none",
    "removed": "removed from this batch by a human (kept, with its CV and package, for later batches)",
    "failed": "see failure; re-run batch-prepare (preparation failures are safe to retry)",
}
# Files whose content decides what CV the generator produces (with the JD and the evidence).
_GENERATOR_FILES = ("matching/cv_generation.py", "matching/cv_render.py", "matching/project_selection.py",
                    "matching/impact_writing.py", "matching/cv_title.py", "matching/review_gate.py",
                    "matching/cv_document.py", "matching/experience_grounding.py")


class BatchError(ValueError):
    """A refused batch operation; the message is safe to show."""


# --- ledger ------------------------------------------------------------------------------------------

class BatchLedger:
    """Append-only, hash-chained journal. Malformed history fails closed; a replay of the same event
    id with the same content is a no-op, a conflicting one is refused."""

    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / BATCH_DIR
        self.path = self.directory / LEDGER

    def events(self, batch_id=None, application_id=None):
        with store_lock(self.root):
            records = self._read()
        return [e for e in records if (batch_id is None or e["batch_id"] == batch_id)
                and (application_id is None or e["application_id"] == application_id)]

    def _read(self):
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise BatchError("Incomplete batch journal; manual recovery required")
        records, previous, ids = [], "", set()
        for line in raw.splitlines():
            item = json.loads(line)
            checksum = item.pop("hash")
            if item["previous_hash"] != previous or fingerprint(item) != checksum or item["event_id"] in ids:
                raise BatchError("Invalid batch journal integrity")
            item["hash"] = previous = checksum
            ids.add(item["event_id"])
            records.append(item)
        return records

    def append(self, kind, batch_id, application_id, data, *, event_id, at=None):
        with store_lock(self.root):
            records = self._read()
            existing = next((r for r in records if r["event_id"] == event_id), None)
            if existing:
                if (existing["kind"], existing["batch_id"], existing["application_id"], canonical(existing["data"])) != (
                        kind, batch_id, application_id, canonical(data)):
                    raise BatchError(f"Conflicting replay of batch event {event_id}")
                return existing
            item = {"event_id": event_id, "kind": kind, "batch_id": batch_id, "application_id": application_id,
                    "at": at or utcnow().isoformat(), "data": data,
                    "previous_hash": records[-1]["hash"] if records else ""}
            item["hash"] = fingerprint(item)
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(self.directory)
            return item


# --- helpers -----------------------------------------------------------------------------------------

def _sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def generator_sha() -> str:
    base = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for name in _GENERATOR_FILES:
        path = base / name
        digest.update(name.encode() + b"\0" + (path.read_bytes() if path.exists() else b""))
    policies = base.parents[1] / "data" / "policies"
    for path in sorted(policies.glob("*.yaml")) if policies.exists() else ():
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def cv_input_fingerprint(root, job, max_projects) -> str:
    """Everything that decides this application's CV: the JD (title, company, text), the candidate's
    promoted evidence, the generator/policy code and the project budget."""
    profile = Path(root) / "profile" / "candidate_profile.json"
    from jobintel.matching.cv_generation import positioning_override
    inputs = {"job_id": job.id, "title": job.title, "company": job.company, "description": job.description or "",
              "profile_sha256": _sha256_file(profile) if profile.exists() else "",
              "generator_sha256": generator_sha(), "max_projects": max_projects}
    rule = positioning_override(job.id, root)
    if rule:  # a reviewed per-job positioning rule decides the CV too (absent for every other job)
        inputs["positioning_override"] = rule
    return fingerprint(inputs)


def jd_signature(job) -> str:
    """What the JD analysis currently extracts for this job (required / preferred skills and alternatives
    groups): when the analysis changes what a JD requires, an earlier CV is no longer the CV this JD gets."""
    from copy import deepcopy
    from jobintel.analysis.jd_requirements import extract_jd_requirements
    from jobintel.analysis.job_enrichment import enrich_job
    # The live analysis (as packages see it), never the job's stored fields, which a refresh may leave stale.
    jd = extract_jd_requirements(enrich_job(deepcopy(job), 2026))
    return fingerprint({"must_have": jd.must_have_skills, "preferred": jd.preferred_skills,
                        "alternatives": [g["members"] for g in jd.required_alternatives]})


def _safe(exc) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _latest(events, kind):
    return next((e for e in reversed(events) if e["kind"] == kind), None)


def _svc(root, config_path, *, shared_browser=False, local_browser_transport=None):
    from jobintel.submission.service import SubmissionService
    svc = SubmissionService(root, config_path)
    if local_browser_transport is not None:
        svc._local_browser_transport = local_browser_transport
    elif shared_browser:
        from jobintel.submission.local_browser import LocalBrowserTransport
        svc._local_browser_transport = LocalBrowserTransport(
            runtime_dir=svc.runtime_root / "local_browser", screenshot_dir=svc.root / "submissions/local_browser_evidence",
            legacy_runtime_dirs=(svc.root / "submissions/local_browser",), shared_browser=True)
    return svc


# --- derived item state -------------------------------------------------------------------------------

@dataclass
class ItemView:
    batch_id: str
    application_id: str
    job_id: str
    rank: int
    state: str
    data: dict

    @property
    def next_action(self) -> str:
        return NEXT_ACTION.get(self.state, "")


def _requests_for(svc, application_id):
    return [r for r in svc.store.requests().values() if r["application_id"] == application_id]


def _request_time(svc, request_id) -> str:
    return next((e["at"] for e in svc.store.events() if e["event_id"] == request_id), "")


def _tab(svc, request_id) -> dict:
    events = svc._handoff_events(request_id)
    session = (events["session"] or {}).get("session_id", "")
    evidence = events["evidence"] or {}
    return {"tab_session_id": session, "target_id": evidence.get("target_id", ""),
            "shared_browser_id": evidence.get("shared_browser_id", ""),
            "released": events["released"] is not None or bool(evidence.get("released"))}


def _derive(svc, events, application, *, check_tabs=True) -> tuple[str, dict]:
    """(state, detail) from this item's batch events + the application + its request ledger."""
    from jobintel.models.taxonomy import APPLICATION_TERMINAL_STATUSES
    detail: dict = {}
    if application is not None and application.is_submitted:
        return "submitted", {"submitted_cv": application.submitted_cv_version_id}
    approved = _latest(events, "approved")
    attempts = [e for e in events if e["kind"] in {"execution_attempted", "execution_result"}]
    requests = _requests_for(svc, application.id) if application is not None else []
    latest_request = requests[-1] if requests else None
    if latest_request is not None:
        state = _REQUEST_STATE.get(latest_request["status"], "needs_attention")
        detail = {"request_id": latest_request["id"], "request_status": latest_request["status"],
                  "package_hash": latest_request["package_hash"], **_tab(svc, latest_request["id"])}
        active = state in {"automation_active", "needs_human_input", "ready_for_manual_submit", "needs_attention"}
        if state in {"needs_human_input", "ready_for_manual_submit"} and check_tabs and detail["tab_session_id"] \
                and not detail["released"]:
            try:
                running = svc._browserbase_runtime(EXECUTOR)[0].session_status(detail["tab_session_id"]) == "RUNNING"
            except Exception:
                running = False
            detail["tab_open"] = running
            if state == "needs_human_input" and running:
                state = "handoff_open"
        # An active attempt always decides the state. An ENDED one (closed / failed) decides it only
        # if it belongs to this batch item's life: not if it predates the item joining the batch, and
        # not once the human approved a new attempt after it.
        requested_at = _request_time(svc, latest_request["id"])
        added = _latest(events, "item_added")
        superseded = approved is not None and approved["at"] > requested_at
        predates = added is not None and requested_at < added["at"]
        if active or state == "submitted" or not (superseded or predates):
            return state, detail
        detail = {"previous_request_id": latest_request["id"], "previous_request_status": latest_request["status"]}
    removed = _latest(events, "removed")
    if removed is not None and (approved is None or approved["at"] < removed["at"]):
        return "removed", removed["data"]
    if _latest(events, "ineligible"):
        return "ineligible", _latest(events, "ineligible")["data"]
    skipped = _latest(events, "skipped")
    if skipped:
        return "skipped", skipped["data"]
    if application is not None and application.status in APPLICATION_TERMINAL_STATUSES:
        return "skipped", {"reason": f"application is {application.status.value}"}
    failure = _latest(events, "prepare_failed")
    prepared = _latest(events, "package_previewed")
    if failure and (prepared is None or failure["at"] > prepared["at"]):
        return "failed", failure["data"]
    routed = _latest(events, "ats_routed")
    if prepared is None:
        return ("cv_preparing" if _latest(events, "cv_prepared") else "discovered"), {}
    if routed and not routed["data"]["supported"]:
        return "unsupported_ats_manual", routed["data"]
    if approved is not None and approved["data"]["package_hash"] == prepared["data"]["package_hash"]:
        consumed = any(e["at"] >= approved["at"] for e in attempts)
        if not consumed:
            return "ready_for_execution", approved["data"]
        last = attempts[-1]["data"]
        if last.get("error"):
            return "failed", last
    return "cv_review_required", prepared["data"]


def items(root, config_path, batch_id, *, check_tabs=True, svc=None) -> list[ItemView]:
    from jobintel.storage.application_store import ApplicationStore
    svc = svc or _svc(root, config_path, shared_browser=True)
    all_events = BatchLedger(root).events()
    events = [e for e in all_events if e["batch_id"] == batch_id]
    apps = {a.id: a for a in ApplicationStore(root).read_applications()}
    views = []
    for added in [e for e in events if e["kind"] == "item_added"]:
        app_id = added["application_id"]
        own = [e for e in events if e["application_id"] == app_id]
        state, detail = _derive(svc, own, apps.get(app_id), check_tabs=check_tabs)
        data = {**added["data"]}
        for kind in ("eligibility", "cv_prepared", "package_previewed", "ats_routed", "approved", "ranked"):
            latest = _latest(own, kind)
            if latest is None and kind in {"cv_prepared", "package_previewed", "ats_routed"}:
                # Not prepared in THIS batch (e.g. an attempt was still open): show the same application's
                # latest preparation from an earlier batch -- read-only, never this batch's own decision.
                latest = _latest([e for e in all_events if e["application_id"] == app_id], kind)
                if latest is not None:
                    data.setdefault("carried_from_batch", latest["batch_id"])
            if latest:
                data[kind] = latest["data"]
        data["detail"] = detail
        views.append(ItemView(batch_id, app_id, added["data"]["job_id"], added["data"]["rank"], state, data))
    return sorted(views, key=lambda v: v.rank)


# --- prepare -------------------------------------------------------------------------------------------

def _rationale(ranked) -> str:
    parts = [f"{c.name} {c.score:+.2f}" for c in ranked.match.components if abs(c.score) >= 0.05][:6]
    return "; ".join(parts) or ranked.match.explanation[:200]


def prepare(root, config_path="config/personal_strategy.json", *, batch_id, job_ids=(), limit=5, max_projects=4,
            export_dir=None, profiles=None, svc=None, now=None, generate=None, regenerate=()) -> list[dict]:
    """Prepare up to `limit` worthwhile jobs (or exactly `job_ids`): eligibility, CV (reused when its
    input fingerprint is unchanged), sealed-package preview and ATS routing. One job's failure is
    recorded and never stops the others. Idempotent: re-running repeats nothing already done."""
    from jobintel.analysis.eligibility import evaluate_job_eligibility
    from jobintel.application_lifecycle import ensure_application
    from jobintel.config import load_ranking_config
    from jobintel.matching.cv_generation import generate_and_save_cv
    from jobintel.matching.cv_review_export import company_display_name, export_cv_for_review, review_filename
    from jobintel.models.eligibility import EligibilityState
    from jobintel.models.taxonomy import APPLICATION_TERMINAL_STATUSES, WorkflowStatus
    from jobintel.pipeline.ingestion import rank_jobs
    from jobintel.profile_ingestion import load_candidate_profile
    from jobintel.storage.application_store import ApplicationStore
    from jobintel.storage.cv_artifact_store import CVArtifactStore
    from jobintel.storage.local_store import LocalJobStore
    from jobintel.submission import apply_one
    from jobintel.submission.ats_profiles import UnknownExecutionProfile, profile_for_url
    from jobintel.submission.packages import BlockedPackageError

    root = Path(root)
    if not batch_id or "/" in batch_id or batch_id.startswith("."):
        raise BatchError("A plain batch id is required")
    generate = generate or generate_and_save_cv
    svc = svc or _svc(root, config_path)
    ledger, apps_store, cv_store = BatchLedger(root), ApplicationStore(root), CVArtifactStore(root)
    candidate = load_candidate_profile(root)
    if candidate is None:
        raise BatchError("No candidate profile in the store")
    config = load_ranking_config(config_path)
    jobs = LocalJobStore(root).read_jobs()
    if job_ids:
        wanted = set(job_ids)
        missing = wanted - {j.id for j in jobs}
        if missing:
            raise BatchError(f"Unknown job id(s): {sorted(missing)}")
        jobs = [j for j in jobs if j.id in wanted]
    ranked = rank_jobs(jobs, candidate=candidate, config=config, now=now or datetime.now(timezone.utc))
    existing = {e["application_id"]: e for e in ledger.events(batch_id) if e["kind"] == "item_added"}
    chosen, report = [], []
    for item in ranked:
        job = item.job
        if not job_ids:
            active = any(o.active for o in job.source_observations) if job.source_observations else True
            if not active or job.workflow_status == WorkflowStatus.IGNORE or item.match.excluded:
                continue
            app = apps_store.for_job(job.id)
            if app is not None and (app.is_submitted or app.status in APPLICATION_TERMINAL_STATUSES) \
                    and f"app:{job.id}" not in existing:
                continue
        chosen.append(item)
        if not job_ids and len(chosen) >= limit:
            break
    next_rank = max((e["data"]["rank"] for e in existing.values()), default=0)
    for item in chosen:
        job = item.job
        app_id = f"app:{job.id}"
        outcome = {"application_id": app_id, "job_id": job.id, "company": job.company, "role": job.title}
        stage = "application"
        try:
            app = ensure_application(job, apps_store, note=f"batch {batch_id}")
            app_id = outcome["application_id"] = app.id
            if app.id not in existing:  # the item's first record is written once; later runs never rewrite it
                next_rank += 1
                existing[app.id] = ledger.append("item_added", batch_id, app.id, {
                    "job_id": job.id, "company": job.company, "role": job.title,
                    "location": job.raw_location or "", "application_url": app.application_url or "",
                    "source_urls": sorted({o.original_url for o in job.source_observations if o.original_url}),
                    "score": round(item.match.overall_priority, 3), "rationale": _rationale(item),
                    "rank": next_rank}, event_id=f"item:{batch_id}:{app.id}")
            ranking = {"score": round(item.match.overall_priority, 3), "rationale": _rationale(item)}
            ledger.append("ranked", batch_id, app.id, ranking,  # refreshed whenever the ranking changes
                          event_id=f"rank:{batch_id}:{app.id}:{fingerprint(ranking)[:16]}")
            if app.is_submitted or app.status in APPLICATION_TERMINAL_STATUSES:
                reason = "already submitted" if app.is_submitted else f"application is {app.status.value}"
                ledger.append("skipped", batch_id, app.id, {"reason": reason}, event_id=f"skip:{batch_id}:{app.id}")
                report.append({**outcome, "state": "skipped", "reason": reason})
                continue
            stage = "eligibility"
            decision = evaluate_job_eligibility(job, candidate)
            verdict = {"state": decision.state.value, "reason": decision.reason,
                       "requirement": decision.extracted_requirement or "", "source_text": decision.source_text or "",
                       "candidate_fact": decision.candidate_fact or "", "source": "job_description"}
            ledger.append("eligibility", batch_id, app.id, verdict,
                          event_id=f"elig:{batch_id}:{app.id}:{fingerprint(verdict)[:16]}")
            from jobintel.analysis.application_restrictions import classify as classify_restrictions
            for found in classify_restrictions(job.description or "", source="job_description", job_id=job.id,
                                               company=job.company):
                ledger.append("restriction", batch_id, app.id, found.to_dict(),
                              event_id=f"restr:{app.id}:{fingerprint(found.to_dict())[:16]}")
            if decision.state == EligibilityState.HARD_INELIGIBLE:
                ledger.append("ineligible", batch_id, app.id, verdict, event_id=f"inelig:{batch_id}:{app.id}")
                report.append({**outcome, "state": "ineligible", "reason": decision.reason})
                continue
            if any(r["status"] in apply_one.ACTIVE_STATUSES for r in _requests_for(svc, app.id)):
                report.append({**outcome, "state": "in_progress", "reason": "an attempt is active; not touched"})
                continue
            stage = "cv"
            fp = cv_input_fingerprint(root, job, max_projects)
            signature = jd_signature(job)
            # Reuse needs the same inputs and (where recorded) the same JD analysis; `regenerate` is an
            # explicit, recorded request for a new CV for this job.
            prior = None if job.id in set(regenerate) else next(
                (e for e in reversed(ledger.events(application_id=app.id)) if e["kind"] == "cv_prepared"
                 and e["data"]["fingerprint"] == fp and e["data"].get("jd_signature", signature) == signature), None)
            artifact = None
            if prior is not None:
                artifact = next((a for a in cv_store.read_all() if a.id == prior["data"]["cv_artifact_id"]), None)
                if artifact is None or not artifact.pdf_path or not Path(artifact.pdf_path).exists() \
                        or _sha256_file(artifact.pdf_path) != artifact.pdf_sha256:
                    artifact = None
            reused = artifact is not None
            if artifact is None:
                artifact = generate(candidate, job, cv_store, max_projects=max_projects, application_id=app.id,
                                    store_root=str(root))
                ledger.append("cv_prepared", batch_id, app.id, {
                    "cv_artifact_id": artifact.id, "cv_sha256": artifact.pdf_sha256, "fingerprint": fp,
                    "jd_signature": signature, "regenerated_on_request": job.id in set(regenerate),
                    "selected_projects": [(p["project_id"], p.get("emphasis", ""), len(p["bullets"]))
                                          for p in artifact.document.get("projects", [])],
                    "title": artifact.document.get("headline", ""), "review_status": artifact.review_status.value,
                    "page_count": artifact.page_count,
                    "layout": f"{artifact.layout_preset} {artifact.render_margin_mm:g}/"
                              f"{(artifact.render_side_margin_mm or artifact.render_margin_mm):g}mm "
                              f"{artifact.render_body_pt:g}pt"},
                    event_id=f"cv:{app.id}:{artifact.id}")
            if apps_store.for_job(job.id).cv_version_id != artifact.id:
                apps_store.attach_cv(app.id, artifact.id, artifact.selected_project_ids, artifact.evidence_ids_used)
            if not reused:
                try:
                    from jobintel.profile_ingestion import load_candidate_profile as _load
                    name = (_load(root) or candidate).name
                    export_cv_for_review(root, artifact.id, review_filename(name, company_display_name(job.company), job.title),
                                         export_dir=export_dir, application_id=app.id)
                except (ValueError, OSError):
                    pass  # convenience copy only; the sealed artifact is authoritative
            stage = "cover_letter"
            # A role-specific cover letter for every prepared CV: composed from this
            # CV artifact + the JD, validated (one page, no duplicates, names, CV-consistent claims) and later
            # sealed with the package. Its failure is recorded and never blocks the CV or the package.
            try:
                from jobintel.matching import cover_letter as _cl
                from jobintel.submission.packages import expected_company_names as _names
                letter = _cl.generate_for_application(root, candidate, job, artifact,
                                                      _names(job.company, config_path)[-1], app.id)
                ledger.append("cover_letter_prepared", batch_id, app.id,
                              {k: letter.get(k) for k in ("status", "cv_artifact_id", "fingerprint", "pdf_sha256",
                                                          "docx_sha256", "page_count", "projects", "problems",
                                                          "acceptance_basis")},
                              event_id=f"cl:{app.id}:{artifact.id}:{letter['fingerprint'][:16]}:{letter['status']}")
                if letter.get("status") == "ready":
                    import shutil
                    from jobintel.matching.cv_review_export import default_export_dir
                    target = Path(export_dir) if export_dir is not None else default_export_dir()
                    try:
                        for kind in ("pdf", "docx"):
                            shutil.copyfile(letter[f"{kind}_path"], target / Path(letter[f"{kind}_path"]).name)
                    except OSError:
                        pass  # convenience copy only; the stored letter is authoritative
            except Exception as exc:  # isolate: the CV/package still proceed
                error = _safe(exc)
                ledger.append("cover_letter_failed", batch_id, app.id, {"cv_artifact_id": artifact.id, "error": error},
                              event_id=f"clfail:{batch_id}:{app.id}:{artifact.id}:{fingerprint(error)[:16]}")
            stage = "ats"
            try:
                profile = profile_for_url(app.application_url or "", profiles)
                routing = {"supported": True, "profile": profile.name, "host": profile.host}
                if not profile.live_validated:
                    routing["pending_live_validation"] = True
            except UnknownExecutionProfile as exc:
                from urllib.parse import urlsplit
                routing = {"supported": False, "host": urlsplit(app.application_url or "").hostname or "",
                           "reason": str(exc)[:300], "application_url": app.application_url or ""}
            ledger.append("ats_routed", batch_id, app.id, routing, event_id=f"ats:{batch_id}:{app.id}:{fingerprint(routing)[:16]}")
            stage = "package"
            if routing["supported"]:
                try:
                    shown = apply_one.preview(svc, app.id, profiles=profiles)
                    package = {"package_hash": shown["package_hash"], "gate_status": shown["gate_status"],
                               "review_reasons": shown["review_reasons"], "execution_mode": shown["execution_mode"],
                               "ack_required": False, "ack_bound": shown["acknowledgement"] is not None}
                except ValueError as exc:
                    try:  # apply_one reports a blocked package as text: get its exact reasons from the service
                        svc.preview(app.id)
                    except BlockedPackageError as blocked:
                        package = {"package_hash": None, "gate_status": "block_auto_submission",
                                   "review_reasons": list(blocked.reasons), "execution_mode": "SUPERVISED_EXECUTION_ONLY",
                                   "ack_required": True, "ack_bound": False}
                    else:
                        raise exc
            else:
                try:
                    pkg = svc.preview(app.id)
                    package = {"package_hash": pkg.package_hash, "gate_status": pkg.gate_status,
                               "review_reasons": list(pkg.gate_reasons), "execution_mode": pkg.execution_mode,
                               "ack_required": False, "ack_bound": False}
                except BlockedPackageError as exc:
                    package = {"package_hash": None, "gate_status": "block_auto_submission",
                               "review_reasons": list(exc.reasons), "execution_mode": "SUPERVISED_EXECUTION_ONLY",
                               "ack_required": True, "ack_bound": False}
            package.update(cv_artifact_id=artifact.id, cv_sha256=artifact.pdf_sha256)
            ledger.append("package_previewed", batch_id, app.id, package,
                          event_id=f"pkg:{batch_id}:{app.id}:{fingerprint(package)[:16]}")
            report.append({**outcome, "state": "unsupported_ats_manual" if not routing["supported"] else "cv_review_required",
                           "cv_artifact_id": artifact.id, "cv_reused": reused, "gate": package["gate_status"]})
        except Exception as exc:  # isolate: record this job's failure, continue with the next
            ledger.append("prepare_failed", batch_id, app_id, {"stage": stage, "error": _safe(exc)},
                          event_id=f"fail:{batch_id}:{app_id}:{utcnow().isoformat()}")
            report.append({**outcome, "state": "failed", "stage": stage, "error": _safe(exc)})
    return report


# --- approve -------------------------------------------------------------------------------------------

def approve(root, config_path="config/personal_strategy.json", *, batch_id, application_id, reviewer, reviewed,
            acknowledge_review_reasons=False, acknowledge_blockers=False, ack_reason=None, profiles=None, svc=None,
            company_limit_decision=None) -> dict:
    """A named human approves ONE item's exact current package for one supervised attempt. An item under a
    one-active / company-wide / ambiguous application restriction needs the user's explicit
    `company_limit_decision`: "automation" (then approved as usual) or "manual" (recorded; never executed)."""
    from jobintel.submission import apply_one
    if reviewed is not True or not (reviewer or "").strip():
        raise BatchError("Approval needs --reviewed and a named --reviewer")
    svc = svc or _svc(root, config_path)
    view = next((v for v in items(root, config_path, batch_id, check_tabs=False, svc=svc)
                 if v.application_id == application_id), None)
    if view is None:
        raise BatchError(f"{application_id} is not in batch {batch_id}")
    if view.state not in {"cv_review_required", "ready_for_execution", "closed_not_submitted"}:
        raise BatchError(f"{application_id} is {view.state}; it cannot be approved for execution")
    findings = limit_findings(root, application_id, view.job_id, view.data.get("company"))
    if findings:
        context = limit_context(root, application_id, view.data.get("company"))
        if company_limit_decision not in LIMIT_DECISIONS:
            raise BatchError(limit_warning(findings, context) + "\n  Decide with --company-limit-decision "
                             "automation|manual (the choice is yours; nothing was approved)")
        BatchLedger(root).append("company_limit_decision", batch_id, application_id,
                                 {"decision": company_limit_decision, "reviewer": reviewer.strip(),
                                  "findings": findings, "context": context},
                                 event_id=f"limit:{batch_id}:{application_id}:{company_limit_decision}:{utcnow().isoformat()}")
        if company_limit_decision in {"manual", "defer"}:
            return {"application_id": application_id, "company_limit_decision": company_limit_decision,
                    "note": "recorded: you will apply manually; this item will never be automated"
                    if company_limit_decision == "manual" else "recorded: deferred; this item will not be automated"}
    shown = apply_one.preview(svc, application_id, reviewer=reviewer if acknowledge_blockers else None,
                              acknowledge_blockers=acknowledge_blockers,
                              ack_reason=ack_reason if acknowledge_blockers else None, profiles=profiles)
    if shown["gate_status"] == "review_required" and not acknowledge_review_reasons:
        raise BatchError("REVIEW_REQUIRED package: approve with --acknowledge-review-reasons after reading them")
    data = {"package_hash": shown["package_hash"], "reviewer": reviewer.strip(),
            "acknowledge_review_reasons": bool(acknowledge_review_reasons), "gate_status": shown["gate_status"],
            "cv_artifact_id": shown["cv_artifact_id"], "execution_mode": shown["execution_mode"],
            "expected_company_names": shown["expected_company_names"], "role": shown["role"], "host": shown["host"]}
    ledger = BatchLedger(root)
    preview_data = {k: shown[k] for k in ("package_hash", "gate_status", "review_reasons", "execution_mode")}
    preview_data.update(ack_required=False, ack_bound=shown["acknowledgement"] is not None,
                        cv_artifact_id=shown["cv_artifact_id"],
                        cv_sha256=(view.data.get("cv_prepared") or {}).get("cv_sha256", ""))
    ledger.append("package_previewed", batch_id, application_id, preview_data,
                  event_id=f"pkg:{batch_id}:{application_id}:{fingerprint(preview_data)[:16]}")
    attempts = len([e for e in ledger.events(batch_id, application_id) if e["kind"] == "approved"])
    ledger.append("approved", batch_id, application_id, data,
                  event_id=f"approve:{batch_id}:{application_id}:{shown['package_hash']}:{attempts}")
    return {"application_id": application_id, **data}


# --- run-next (sequential automation, multi-tab handoff) --------------------------------------------------

@contextmanager
def executor_lock(root, label):
    """Exactly one automation executor at a time, across processes. Handoff tabs never hold it."""
    directory = Path(root) / BATCH_DIR
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / EXECUTOR_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BatchError("Automation is already active in another process (one executor at a time)") from None
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "label": label, "since": utcnow().isoformat()}).encode())
        try:
            yield
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def executor_holder(root) -> dict | None:
    path = Path(root) / BATCH_DIR / EXECUTOR_LOCK
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raw = path.read_text() or "{}"
            return json.loads(raw)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def precheck_form(root, batch_id, application_id, job_id, company, url, form_text) -> str | None:
    """Read the real form's text BEFORE any tab opens and record its restriction wording (source
    "application_form (pre-check)"). Returns None when read, or a reason when the form could not be read."""
    from jobintel.analysis.application_restrictions import classify
    from jobintel.pipeline.restriction_precheck import PrecheckUnavailable
    try:
        text = form_text(url)
    except PrecheckUnavailable as exc:
        return f"application form could not be read before execution ({exc})"
    ledger = BatchLedger(root)
    for found in classify(text or "", source="application_form (pre-check)", job_id=job_id, company=company or ""):
        ledger.append("restriction", batch_id, application_id, found.to_dict(),
                      event_id=f"restr:{application_id}:{fingerprint(found.to_dict())[:16]}")
    providers = list(getattr(text, "verification_providers", ()) or ())
    ledger.append("restriction_precheck", batch_id, application_id,
                  {"url": url, "read": True, "human_verification_providers": providers},
                  event_id=f"precheck:{batch_id}:{application_id}:{utcnow().isoformat()}")
    return None


def human_verification_providers(root, batch_id, application_id) -> list[str]:
    """Providers the latest pre-check saw on this item's real form."""
    checks = [e for e in BatchLedger(root).events(batch_id, application_id) if e["kind"] == "restriction_precheck"]
    return list((checks[-1]["data"].get("human_verification_providers") or [])) if checks else []


HUMAN_VERIFICATION_WARNING = (
    "HUMAN VERIFICATION ON THE FORM — USER DECISION REQUIRED: the real application form loads {providers}. "
    "Its checks run as soon as the page loads, so in an automated session the verification can fail for the "
    "human afterwards. No tab was opened. Decide: "
    "MANUAL APPLICATION (batch-approve --company-limit-decision manual) or SUPERVISED AUTOMATION accepting that "
    "risk (--confirm-human-verification <application id>). Verification itself is never automated.")


def run_next(root, config_path="config/personal_strategy.json", *, batch_id, max_items=1, profiles=None,
             svc=None, start=None, allow_unvalidated_profile=False, confirm_company_limit=(),
             form_text=None, confirm_human_verification=()) -> list[dict]:
    """Automate up to `max_items` approved items, strictly one after another, each in a NEW tab of
    the shared headed browser, through the unchanged `apply_one.start`. Each item stops at its human
    handoff (its tab stays open, bound to its request, and is never automated again); then the next
    one starts. A failure is recorded and the next item still runs. Never retries an attempt."""
    from jobintel.storage.application_store import ApplicationStore
    from jobintel.submission import apply_one
    svc = svc or _svc(root, config_path, shared_browser=True)
    start = start or apply_one.start
    if form_text is None:
        from jobintel.pipeline.restriction_precheck import fetch_form_text as form_text
    ledger, results, prechecked = BatchLedger(root), [], {}
    with executor_lock(root, f"batch {batch_id}"):
        for _ in range(max_items):
            if any(r["status"] == "executing" and r.get("executor") == EXECUTOR for r in svc.store.requests().values()):
                raise BatchError("A local_browser attempt is executing (claimed) elsewhere; not starting another")
            ready = [v for v in items(root, config_path, batch_id, check_tabs=False, svc=svc)
                     if v.state == "ready_for_execution"]
            held = []
            for v in ready:
                decision = _limit_decision(root, v.application_id)
                if decision in {"manual", "defer"}:
                    held.append(v)
                    continue
                # BEFORE any tab: read the real form once and record its restriction wording (fail closed).
                if v.application_id not in prechecked:
                    app = next((a for a in ApplicationStore(root).read_applications() if a.id == v.application_id), None)
                    prechecked[v.application_id] = precheck_form(
                        root, batch_id, v.application_id, v.job_id, v.data.get("company"),
                        (app.application_url if app else None) or v.data.get("application_url") or "", form_text)
                unread = prechecked[v.application_id]
                findings = limit_findings(root, v.application_id, v.job_id, v.data.get("company"))
                # Exact-role wording on THIS job's own form ("you have already applied for this position") may mean
                # a duplicate: held for the user too (it never binds any other job).
                findings += [{**x, "category": "EXACT_ROLE", "scope": "this exact job (possible duplicate)",
                              "window": ""} for x in restrictions_for(root, v.application_id, v.job_id, v.data.get("company"))
                             if x["kind"] == "exact_role" and x["observed_on_this_job"]
                             and x["source"] == "application_form (pre-check)"]
                confirmed = v.application_id in set(confirm_company_limit)
                if (findings or unread) and not confirmed:
                    held.append(v)
                    if not any(r.get("application_id") == v.application_id for r in results):
                        warning = (limit_warning(findings, limit_context(root, v.application_id, v.data.get("company")),
                                                 execution=True) if findings else "")
                        results.append({"application_id": v.application_id, "status":
                                        "company_limit_confirmation_required" if findings else "restriction_precheck_unavailable",
                                        "warning": (warning + ("\n" if warning and unread else "") + (unread or "")
                                                    + "\n  No tab was opened. Decide: MANUAL APPLICATION (batch-approve "
                                                    "--company-limit-decision manual), SUPERVISED AUTOMATION "
                                                    "(--confirm-company-limit <application id>) or DEFER "
                                                    "(--company-limit-decision defer).")})
                    continue
                providers = [] if unread else human_verification_providers(root, batch_id, v.application_id)
                if providers and v.application_id not in set(confirm_human_verification):
                    held.append(v)
                    if not any(r.get("application_id") == v.application_id for r in results):
                        results.append({"application_id": v.application_id, "status": "human_verification_required",
                                        "providers": providers,
                                        "warning": HUMAN_VERIFICATION_WARNING.format(providers=", ".join(providers))})
                    continue
                if (findings or unread) and confirmed and decision != "automation":
                    ledger.append("company_limit_decision", batch_id, v.application_id,
                                  {"decision": "automation", "reviewer": v.data["approved"]["reviewer"],
                                   "at_execution": True, "findings": findings, "precheck_unavailable": unread},
                                  event_id=f"limit:{batch_id}:{v.application_id}:automation:{utcnow().isoformat()}")
            ready = [v for v in ready if v not in held]
            if not ready:
                break
            view = ready[0]
            approval = view.data["approved"]
            ledger.append("execution_attempted", batch_id, view.application_id,
                          {"package_hash": approval["package_hash"], "reviewer": approval["reviewer"]},
                          event_id=f"attempt:{batch_id}:{view.application_id}:{approval['package_hash']}:{utcnow().isoformat()}")
            try:
                kwargs = {"allow_unvalidated_profile": True} if allow_unvalidated_profile else {}
                out = start(svc, view.application_id, package_hash=approval["package_hash"], reviewer=approval["reviewer"],
                            reviewed=True, acknowledge_review_reasons=approval["acknowledge_review_reasons"],
                            profiles=profiles, **kwargs)
                request_id = out.get("request_id")
                tab = _tab(svc, request_id) if request_id else {}
                result = {"request_id": request_id, "status": out.get("status"), "handoff": out.get("handoff"),
                          "final_submit_clicked": out.get("final_submit_clicked"),
                          "cv_upload_status": out.get("cv_upload_status"), **tab}
            except Exception as exc:  # isolate: this item is recorded as failed; the batch goes on
                result = {"error": _safe(exc)}
            ledger.append("execution_result", batch_id, view.application_id, result,
                          event_id=f"result:{batch_id}:{view.application_id}:{utcnow().isoformat()}")
            results.append({"application_id": view.application_id, **result})
    return results


def remove(root, *, batch_id, application_id, reviewer, reason) -> dict:
    """A named human removes one item from this batch (never deleted: its CV, package and history stay,
    and it can join a later batch). Refused for an item with an active attempt."""
    if not (reviewer or "").strip() or not (reason or "").strip():
        raise BatchError("Removing an item needs a named reviewer and a reason")
    ledger = BatchLedger(root)
    if not any(e["kind"] == "item_added" for e in ledger.events(batch_id, application_id)):
        raise BatchError(f"{application_id} is not in batch {batch_id}")
    svc = _svc(root, "config/personal_strategy.json")
    from jobintel.submission.apply_one import ACTIVE_STATUSES
    if any(r["status"] in ACTIVE_STATUSES for r in _requests_for(svc, application_id)):
        raise BatchError(f"{application_id} has an active attempt; it cannot be removed")
    data = {"reviewer": reviewer.strip(), "reason": reason.strip()}
    ledger.append("removed", batch_id, application_id, data, event_id=f"remove:{batch_id}:{application_id}:{utcnow().isoformat()}")
    return {"application_id": application_id, "state": "removed", **data}


# --- employer application restrictions ------------------------------------------------------------------

def restrictions_for(root, application_id, job_id, company) -> list[dict]:
    """Restrictions binding THIS application: its own exact-role ones plus any company-wide one observed
    on any posting of the same company (any batch). Advisory / ambiguous ones observed on this posting
    are shown too, flagged as not binding. Never a rejection by itself."""
    from jobintel.analysis.application_restrictions import Restriction, applies_to
    out, seen = [], set()
    for e in BatchLedger(root).events():
        if e["kind"] != "restriction":
            continue
        r = Restriction(**e["data"])
        own = e["application_id"] == application_id or r.job_id == job_id
        binding = applies_to(r, job_id=job_id, company=company)
        # Unclassified wording seen on another posting of the same company is shown as a NOTE (never binding,
        # never widened) so a human can decide with it in view.
        same_company_note = r.kind == "ambiguous" and r.company.casefold() == (company or "").casefold()
        if not (binding or own or same_company_note):
            continue
        key = (r.kind, r.wording, r.job_id)
        if key in seen:
            continue
        seen.add(key)
        out.append({**r.to_dict(), "binding": binding, "observed_on_this_job": r.job_id == job_id})
    return out


LIMIT_BANNERS = {"COMPANY_WIDE_LIMIT": "COMPANY APPLICATION LIMIT — USER DECISION REQUIRED",
                 "EXACT_ROLE": "THIS FORM SAYS YOU MAY ALREADY HAVE APPLIED — USER DECISION REQUIRED",
                 "ONE_ACTIVE_APPLICATION": "ONE-ACTIVE-APPLICATION LIMIT — USER DECISION REQUIRED",
                 "AMBIGUOUS": "AMBIGUOUS APPLICATION RESTRICTION — USER DECISION REQUIRED"}
LIMIT_DECISIONS = ("automation", "manual", "defer")


def limit_findings(root, application_id, job_id, company) -> list[dict]:
    """Restrictions on this application whose category needs the user's own decision (one active application,
    a company-wide limit, or ambiguous wording), each with its category, scope and stated time window."""
    from jobintel.analysis.application_restrictions import DECISION_CATEGORIES, category, window
    out = []
    for x in restrictions_for(root, application_id, job_id, company):
        cat = category(x["kind"])
        if cat in DECISION_CATEGORIES and (x["binding"] or x["observed_on_this_job"] or cat == "AMBIGUOUS"):
            scope = {"COMPANY_WIDE_LIMIT": "every role at this company", "ONE_ACTIVE_APPLICATION":
                     "one active application at a time at this company", "AMBIGUOUS": "unclear"}[cat]
            out.append({**x, "category": cat, "scope": scope, "window": window(x["wording"])})
    return out


def limit_context(root, application_id, company) -> dict:
    """What a company-limit decision needs in view: the candidate's own applications to the company, and the
    company's roles prepared in any batch, strongest (highest score) first."""
    from jobintel.analysis.application_restrictions import company_history
    from jobintel.storage.application_store import ApplicationStore
    history = company_history(ApplicationStore(root).read_applications(), company)
    roles, seen = [], set()
    for e in BatchLedger(root).events():
        if e["kind"] == "item_added" and (e["data"].get("company") or "").casefold() == (company or "").casefold() \
                and e["application_id"] not in seen:
            seen.add(e["application_id"])
            roles.append({"application_id": e["application_id"], "role": e["data"].get("role"),
                          "score": e["data"].get("score")})
    roles.sort(key=lambda r: -(r["score"] or 0))
    return {"existing_applications": history, "same_company_roles": roles,
            "strongest_role": roles[0] if roles else None,
            "this_is_strongest": bool(roles) and roles[0]["application_id"] == application_id}


def _limit_decision(root, application_id) -> str | None:
    decision = next((e for e in reversed(BatchLedger(root).events(application_id=application_id))
                     if e["kind"] == "company_limit_decision"), None)
    return decision["data"]["decision"] if decision else None


def limit_warning(findings, context, *, execution=False) -> str:
    lines = []
    for f in findings:
        lines.append(f"*** {LIMIT_BANNERS[f['category']]} ***")
        lines.append(f'  wording: "{f["wording"]}"  (source: {f["source"]}; observed on {f["job_id"]})')
        lines.append(f"  scope: {f['scope']}; time window: {f['window'] or 'not stated'}")
    if context["existing_applications"]:
        lines.append("  your applications to this company: " + "; ".join(
            f"{a['role']} ({a['status']})" for a in context["existing_applications"]))
    if len(context["same_company_roles"]) > 1:
        lines.append("  same-company roles (strongest first): " + "; ".join(
            f"{r['role']} [{r['score']}]" for r in context["same_company_roles"]))
    if execution:
        lines.append("  This company has an application restriction. You previously chose this role. Confirm "
                     "whether you want supervised automation (--confirm-company-limit <application id>) or will apply manually.")
    return "\n".join(lines)


def note_restriction(root, *, batch_id, application_id, wording, source="page_warning (human-reported)") -> dict:
    """Record an employer warning a human saw (exact wording), classify it, and compare it with the
    candidate's own applications to that company by exact job id. Changes no item state."""
    from jobintel.analysis.application_restrictions import Restriction, classify, company_history
    from jobintel.storage.application_store import ApplicationStore
    if not (wording or "").strip():
        raise BatchError("The exact employer wording is required")
    apps = ApplicationStore(root).read_applications()
    app = next((a for a in apps if a.id == application_id), None)
    if app is None:
        raise BatchError(f"Unknown application {application_id}")
    found = classify(wording, source=source, job_id=app.job_id, company=app.company) or [
        Restriction("ambiguous", wording.strip(), source, app.job_id, app.company, company_wide=False)]
    ledger = BatchLedger(root)
    for r in found:
        ledger.append("restriction", batch_id, application_id, r.to_dict(),
                      event_id=f"restr:{application_id}:{fingerprint(r.to_dict())[:16]}")
    return {"application_id": application_id, "job_id": app.job_id, "company": app.company,
            "classified": [r.to_dict() for r in found], "company_history": company_history(apps, app.company)}


# --- views ---------------------------------------------------------------------------------------------

def _cover_letter_row(root, cv_artifact_id) -> str:
    if not cv_artifact_id:
        return ""
    from jobintel.matching.cover_letter import load_for_artifact
    rec = load_for_artifact(root, cv_artifact_id)
    if not rec:
        return ""
    if rec.get("status") != "ready":
        return f"{rec.get('status')} {'; '.join(rec.get('problems') or []) or rec.get('acceptance_basis', '')}".strip()
    return f"{Path(rec['pdf_path']).name} (+ .docx) pdf sha256 {rec['pdf_sha256']}, {rec.get('page_count')} page"


def queue_rows(root, config_path, batch_id, *, svc=None) -> list[dict]:
    rows = []
    for v in items(root, config_path, batch_id, svc=svc):
        d = v.data
        cv, pkg, ats, elig = (d.get("cv_prepared") or {}), (d.get("package_previewed") or {}), (d.get("ats_routed") or {}), \
            (d.get("eligibility") or {})
        rows.append({
            "rank": v.rank, "application_id": v.application_id, "company": d.get("company"), "role": d.get("role"),
            "location": d.get("location"), "application_url": d.get("application_url"),
            "eligibility": elig.get("state", ""), "eligibility_reason": elig.get("reason", ""),
            "score": (d.get("ranked") or d).get("score"), "rationale": (d.get("ranked") or d).get("rationale"),
            "cv_artifact_id": pkg.get("cv_artifact_id") or cv.get("cv_artifact_id", ""),
            "cv_sha256": pkg.get("cv_sha256") or cv.get("cv_sha256", ""),
            "cv_title": cv.get("title", ""), "selected_projects": cv.get("selected_projects", []),
            "layout": cv.get("layout", ""), "gate_status": pkg.get("gate_status", ""),
            "review_reasons": pkg.get("review_reasons", []), "package_hash": pkg.get("package_hash") or "",
            "ats_supported": ats.get("supported"), "ats": ats.get("profile") or ats.get("host", ""),
            "ats_reason": ats.get("reason", ""), "ats_pending_validation": bool(ats.get("pending_live_validation")),
            "ack_required": bool(pkg.get("ack_required")) or pkg.get("gate_status") == "block_auto_submission",
            "ack_bound": bool(pkg.get("ack_bound")),
            "cover_letter": _cover_letter_row(root, pkg.get("cv_artifact_id") or cv.get("cv_artifact_id", "")),
            "state": v.state, "next_action": v.next_action, "detail": d.get("detail", {}),
            "restrictions": restrictions_for(root, v.application_id, v.job_id, d.get("company")),
            "limit_findings": limit_findings(root, v.application_id, v.job_id, d.get("company")),
            "limit_context": limit_context(root, v.application_id, d.get("company")),
        })
    return rows


def render_queue(rows, batch_id) -> str:
    lines = [f"# Batch review queue: {batch_id}", ""]
    for r in rows:
        projects = ", ".join(f"{p} ({tier}, {n})" for p, tier, n in r["selected_projects"]) or "(none)"
        lines += [f"## {r['rank']}. {r['company']} — {r['role']}  [{r['state']}]",
                  f"- Location: {r['location'] or '?'}",
                  f"- Application URL: {r['application_url'] or '?'}",
                  f"- Eligibility: {r['eligibility'] or '?'} — {r['eligibility_reason']}",
                  f"- Rank score: {r['score']} — {r['rationale']}",
                  f"- CV: {r['cv_artifact_id'] or '-'} sha256 {r['cv_sha256'] or '-'}",
                  f"- CV title: {r['cv_title'] or '-'}; layout {r['layout'] or '-'}",
                  f"- Projects: {projects}",
                  f"- Review gate: {r['gate_status'] or '-'}",
                  *[f"  - {reason}" for reason in r["review_reasons"]],
                  f"- ATS: {'supported' if r['ats_supported'] else 'UNSUPPORTED'} {r['ats']} {r['ats_reason']}"
                  f"{' (PENDING LIVE VALIDATION)' if r.get('ats_pending_validation') else ''}".rstrip(),
                  f"- Acknowledgement required: {'yes' if r['ack_required'] else 'no'}"
                  + (" (bound)" if r["ack_bound"] else ""),
                  f"- Package hash: {r['package_hash'] or '(needs acknowledgement first)'}",
                  f"- Cover letter: {r.get('cover_letter') or 'none'}",
                  *[line for f in r.get("limit_findings", []) for line in (f"**{LIMIT_BANNERS[f['category']]}**",
                    f"  - wording: \"{f['wording']}\" (source: {f['source']}); scope: {f['scope']}; "
                    f"time window: {f['window'] or 'not stated'}")],
                  *([f"  - your applications to this company: " + "; ".join(
                      f"{a['role']} ({a['status']})" for a in r["limit_context"]["existing_applications"])]
                    if r.get("limit_findings") and r["limit_context"]["existing_applications"] else []),
                  *([f"  - same-company roles (strongest first): " + "; ".join(
                      f"{x['role']} [{x['score']}]" for x in r["limit_context"]["same_company_roles"])]
                    if r.get("limit_findings") and len(r["limit_context"]["same_company_roles"]) > 1 else []),
                  *([f"- Employer restrictions:"] + [
                      f"  - [{x['kind']}{' — COMPANY-WIDE' if x['company_wide'] else ''}"
                      f"{'' if x['binding'] else ', not binding here'}] \"{x['wording']}\" "
                      f"(source: {x['source']}; observed on {x['job_id']})" for x in r["restrictions"]]
                    if r["restrictions"] else ["- Employer restrictions: none found"]),
                  f"- Next: {r['next_action']}", ""]
    return "\n".join(lines)


def write_queue(root, config_path, batch_id, *, svc=None) -> tuple[Path, list[dict]]:
    rows = queue_rows(root, config_path, batch_id, svc=svc)
    path = Path(root) / BATCH_DIR / batch_id / "review_queue.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_queue(rows, batch_id), encoding="utf-8")
    return path, rows


def status(root, config_path, batch_id, *, svc=None) -> dict:
    views = items(root, config_path, batch_id, svc=svc)
    counts: dict[str, int] = {}
    for v in views:
        counts[v.state] = counts.get(v.state, 0) + 1
    events = BatchLedger(root).events(batch_id)
    company_wide = []
    for v in views:
        for x in restrictions_for(root, v.application_id, v.job_id, v.data.get("company")):
            if x["company_wide"] and x not in company_wide:
                company_wide.append(x)
    return {
        "batch_id": batch_id,
        "company_wide_restrictions": company_wide,  # surfaced before choosing more roles at that company
        "executor": executor_holder(root),  # what is running now (None = no automation active)
        "counts": counts,
        "items": [{"rank": v.rank, "application_id": v.application_id, "company": v.data.get("company"),
                   "role": v.data.get("role"), "state": v.state, "next_action": v.next_action,
                   "request_id": v.data.get("detail", {}).get("request_id"),
                   "tab_open": v.data.get("detail", {}).get("tab_open"),
                   "failure": v.data.get("detail") if v.state == "failed" else None} for v in views],
        "failures": [{"application_id": e["application_id"], "at": e["at"], **e["data"]}
                     for e in events if e["kind"] == "prepare_failed" or (e["kind"] == "execution_result"
                                                                          and e["data"].get("error"))],
    }
