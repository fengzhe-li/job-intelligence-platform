"""Apply-one: a supervised local_browser application attempt for ONE application id.

Generic orchestration over the existing SubmissionService. It adds no new permission: the
service, runner, network guard, CV-upload window and handoff are used unchanged. What it adds
is what previously needed a hand-edited acknowledgement file and a per-company helper script:

1. `preview`  -- loads the application, checks its CV, eligibility and review gate through the
   normal package preparation, matches the reviewed ATS execution profile by the URL's exact
   host (unknown hosts are refused), refuses while another attempt is active, and prints the
   package hash. A BLOCK_AUTO_SUBMISSION package needs a named human's explicit acknowledgement
   of exactly the current review reasons: given with --acknowledge-blockers, --reviewer and
   --ack-reason it is written as a structured acknowledgement file, or an existing one that still
   binds is reused. Nothing is approved, queued or started.
2. `start`    -- needs the exact hash preview showed, the reviewer and an explicit --reviewed.
   It re-prepares the package (refusing any change), supersedes an approval already consumed by
   an earlier attempt, approves, queues one local_browser request for the profile's exact host,
   claims it and executes it ONCE with the profile's policy and a human handoff. No retry.
3. `status` / `release` -- read the sanitized outcome; end the handed-off browser this attempt
   started. `submission-close` (unchanged) records that nothing was submitted.

The claim token lives only in this process (from claim to execute, or read back from the local
store for release) and is never printed, logged or returned. Output is a sanitized summary:
no token, keys, cookies, headers, form values, debug URLs or raw exception text.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path

from jobintel.models.submission import SupervisedReviewAcknowledgement
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.submission.ats_profiles import profile_for_url
from jobintel.submission.packages import BlockedPackageError
from jobintel.submission.stop_only import missing_required_answers

EXECUTOR = "local_browser"
DEFAULT_WORKER = "local-browser"
LEASE_SECONDS = 3600
REVIEW_SECONDS = 30 * 60
ACK_DIR = "supervised_acks"
MAX_ACK_BYTES = 100_000
# Request states that make an application busy (SubmissionStore.assert_application_idle).
ACTIVE_STATUSES = frozenset({"submission_requested", "executing", "ready_for_human_review", "needs_human_input",
                             "pending_verification", "outcome_unknown", "verified"})
BLOCKED_KEYS = ("kind", "method", "host", "path", "resource_type", "graphql_operation_name", "graphql_operation_type")


# --- lookups --------------------------------------------------------------------------------------------

def _application(svc, application_id):
    app = next((a for a in ApplicationStore(svc.root).read_applications() if a.id == application_id), None)
    if app is None:
        raise ValueError(f"Unknown application {application_id}")
    return app


def _refuse_active(svc, application_id):
    active = [r for r in svc.store.requests().values()
              if r["application_id"] == application_id and r["status"] in ACTIVE_STATUSES]
    if active:
        r = active[-1]
        raise ValueError(f"Application already has an active attempt {r['id']} ({r['status']}); inspect it with "
                         "submission-apply-status, end its browser with submission-apply-release and record it with "
                         "submission-close before starting another")


def _request_for(svc, application_id=None, request_id=None):
    if (application_id is None) == (request_id is None):
        raise ValueError("Give exactly one of --application-id or --request-id")
    requests = svc.store.requests()
    if request_id is not None:
        r = requests.get(request_id)
    else:
        r = next((r for r in reversed(list(requests.values()))
                  if r["application_id"] == application_id and r["executor"] == EXECUTOR), None)
    if r is None or r["executor"] != EXECUTOR:
        raise ValueError("No local_browser attempt found")
    return r


# --- acknowledgement ------------------------------------------------------------------------------------

def _ack_dir(svc) -> Path:
    return svc.root / ACK_DIR


def find_acknowledgement(svc, app, cv_sha256, reasons):
    """The newest stored acknowledgement that binds exactly this application/job/CV/PDF and exactly
    these review reasons, else None. Anything unreadable or not binding is ignored."""
    found = []
    for path in sorted(_ack_dir(svc).glob("*.json")) if _ack_dir(svc).is_dir() else ():
        try:
            if path.is_symlink() or path.stat().st_size > MAX_ACK_BYTES:
                continue
            ack = SupervisedReviewAcknowledgement.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError, KeyError, OSError):
            continue
        if ((ack.application_id, ack.job_id, ack.cv_artifact_id, ack.cv_pdf_sha256)
                == (app.id, app.job_id, app.cv_version_id, cv_sha256) and set(ack.acknowledged_blockers) == set(reasons)):
            found.append(ack)
    return max(found, key=lambda a: a.acknowledged_at) if found else None


def _write_acknowledgement(svc, ack) -> None:
    directory = _ack_dir(svc)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = ack.acknowledged_at.replace(":", "").replace("-", "").split(".")[0].split("+")[0]
    target = directory / f"{ack.cv_artifact_id}-{stamp}.json"
    data = asdict(ack)
    data["acknowledged_blockers"] = list(ack.acknowledged_blockers)
    with target.open("x", encoding="utf-8") as handle:  # never overwrite an existing acknowledgement
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


def _cv_sha256(svc, app):
    artifact = next((a for a in CVArtifactStore(svc.root).read_all() if a.id == app.cv_version_id), None)
    if artifact is None or not artifact.pdf_sha256:
        raise ValueError("The application has no attached CV with a recorded SHA-256")
    return artifact.pdf_sha256


def _package(svc, app, *, reviewer=None, acknowledge_blockers=False, ack_reason=None):
    """(package, acknowledgement source) through the normal package preparation."""
    try:
        package = svc.preview(app.id)
    except BlockedPackageError as blocked:
        ack = find_acknowledgement(svc, app, _cv_sha256(svc, app), blocked.reasons)
        if ack is not None:
            return svc.preview(app.id, ack), "existing"
        if not acknowledge_blockers:
            raise ValueError(f"{blocked}. Review them; to authorize ONE supervised attempt (never final submit) "
                             "rerun submission-apply-preview with --acknowledge-blockers --reviewer NAME "
                             "--ack-reason TEXT") from None
        if not (reviewer or "").strip() or not (ack_reason or "").strip():
            raise ValueError("--acknowledge-blockers needs --reviewer and --ack-reason") from None
        ack = SupervisedReviewAcknowledgement(
            reviewer=reviewer.strip(), reviewed=True, acknowledged_at=svc.clock().isoformat(), reason=ack_reason.strip(),
            acknowledged_blockers=tuple(blocked.reasons), application_id=app.id, job_id=app.job_id,
            cv_artifact_id=app.cv_version_id, cv_pdf_sha256=_cv_sha256(svc, app))
        package = svc.preview(app.id, ack)  # every acknowledgement rule applies before anything is written
        _write_acknowledgement(svc, ack)
        return package, "created"
    if acknowledge_blockers:
        raise ValueError("This package is not blocked; there is nothing to acknowledge")
    return package, None


def _check_answers(package):
    missing = missing_required_answers(package)
    if missing:
        raise ValueError(f"Approved package would be missing required candidate answers: {missing}; resolve them in "
                         "the authoritative answer bank first")


# --- the workflow ---------------------------------------------------------------------------------------

def preview(svc, application_id, *, reviewer=None, acknowledge_blockers=False, ack_reason=None, profiles=None):
    """Validate one application for a supervised attempt and show the exact package hash. Writes at
    most an explicitly requested acknowledgement; approves, queues and starts nothing."""
    if not acknowledge_blockers and (reviewer is not None or ack_reason is not None):
        raise ValueError("--reviewer and --ack-reason are only used with --acknowledge-blockers")
    app = _application(svc, application_id)
    profile = profile_for_url(app.application_url, profiles)
    _refuse_active(svc, application_id)
    package, ack_source = _package(svc, app, reviewer=reviewer, acknowledge_blockers=acknowledge_blockers,
                                   ack_reason=ack_reason)
    _check_answers(package)
    ack = package.supervised_acknowledgement
    return {
        "application_id": package.application_id,
        "company": package.company,
        "expected_company_names": list(package.expected_company_names),
        "role": package.role,
        "host": profile.host,
        "execution_profile": profile.name,
        "execution_profile_live_validated": profile.live_validated,
        "executor": EXECUTOR,
        "package_hash": package.package_hash,
        "gate_status": package.gate_status,
        "review_reasons": list(package.gate_reasons),
        "eligibility_state": package.eligibility_state,
        "execution_mode": package.execution_mode,
        "cv_artifact_id": package.cv_artifact_id,
        "cv_pdf_sha256_prefix": package.cv_pdf_sha256[:12],
        "answer_fields": sorted(a.field for a in package.answers),
        "acknowledgement": None if ack is None else {"source": ack_source, "reviewer": ack.reviewer,
                                                     "acknowledged_at": ack.acknowledged_at},
        "final_submit_allowed": False,
        "next": f"submission-apply-start --application-id {package.application_id} --package-hash "
                f"{package.package_hash} --reviewer NAME --reviewed",
    }


def _supersede_consumed_approvals(svc, application_id, package_hash, reviewer):
    """An approval that already backs a request (of any status) is never reused for a new attempt:
    it is revoked as superseded, visibly, so the service approves afresh."""
    requests = list(svc.store.requests().values())
    superseded = []
    for a in list(svc.store.approvals().values()):
        if a["package"]["application_id"] != application_id or a["status"] != "approved" or a["package_hash"] != package_hash:
            continue
        used = [r for r in requests if r["approval_id"] == a["id"]]
        if used:
            svc.revoke(a["id"], reviewer=reviewer, reason=f"superseded: approval already used by {used[-1]['id']} "
                                                          f"({used[-1]['status']}); fresh approval for a new attempt")
            superseded.append(a["id"])
    return superseded


def start(svc, application_id, *, package_hash, reviewed, reviewer, acknowledge_review_reasons=False,
          worker=DEFAULT_WORKER, profiles=None, lease_seconds=LEASE_SECONDS, review_seconds=REVIEW_SECONDS,
          allow_unvalidated_profile=False):
    """Approve the exact previewed package and run ONE supervised local_browser attempt. A profile not
    yet validated live runs only with the explicit `allow_unvalidated_profile` opt-in (its one reviewed
    validation run); it is never the default."""
    if reviewed is not True:
        raise ValueError("Start requires --reviewed: the human has reviewed this exact package")
    if not (reviewer or "").strip():
        raise ValueError("Start requires --reviewer")
    app = _application(svc, application_id)
    profile = profile_for_url(app.application_url, profiles)
    if not profile.live_validated and allow_unvalidated_profile is not True:
        raise ValueError(f"Execution profile {profile.name} is not yet validated live ({profile.validated}); "
                         "only its one explicitly approved validation run may use it")
    _refuse_active(svc, application_id)
    package, _ = _package(svc, app)  # reuses only an existing binding acknowledgement; never creates one
    if package.package_hash != package_hash:
        raise ValueError("Package changed since preview (hash mismatch); run submission-apply-preview again and review")
    _check_answers(package)
    superseded = _supersede_consumed_approvals(svc, application_id, package_hash, reviewer)
    approval = svc.approve(application_id, package_hash=package_hash, reviewer=reviewer, reviewed=True,
                           acknowledge_reasons=acknowledge_review_reasons,
                           supervised_acknowledgement=package.supervised_acknowledgement)
    request = svc.enqueue(approval["id"], executor=EXECUTOR, live_host=profile.host)
    if request["status"] != "submission_requested":
        raise ValueError(f"Request {request['id']} is {request['status']}, not queued; nothing was started")
    request_id = request["id"]
    claim_token = svc.claim(request_id, worker=worker, lease_seconds=lease_seconds)["claim_token"]
    out, failure = {}, None
    try:
        out = svc.execute(request_id, claim_token=claim_token, human_handoff=True, review_seconds=review_seconds,
                          **profile.execute_options())
    except Exception as exc:  # report the journaled state; never retry, never echo raw exception text
        failure = _failure(svc, request_id, exc)
    finally:
        del claim_token
    summary = run_summary(svc, request_id, out)
    summary.update({"approval_id": approval["id"], "superseded_approvals": superseded,
                    "execution_profile": profile.name})
    if failure:
        summary.update(failure)
    return summary


def status(svc, *, application_id=None, request_id=None):
    """The sanitized outcome of the latest (or given) local_browser attempt. Changes nothing."""
    return run_summary(svc, _request_for(svc, application_id, request_id)["id"], {})


def release(svc, *, application_id=None, request_id=None):
    """End the handed-off browser of this attempt: only the session journaled for it, using the claim
    token stored for it in the local store (never printed). Records no result; close separately."""
    r = _request_for(svc, application_id, request_id)
    session_id = (svc._handoff_events(r["id"])["session"] or {}).get("session_id")
    if not session_id:
        raise ValueError("This attempt has no journaled browser session to release")
    claim_token = r.get("claim_token")
    if not claim_token:
        raise ValueError("No stored claim token for this attempt; nothing was released")
    transport = svc._browserbase_runtime(EXECUTOR)[0]
    before = transport.session_status(session_id)
    try:
        out = svc.release_handoff(r["id"], session_id=session_id, claim_token=claim_token)
    finally:
        del claim_token
    after = transport.session_status(session_id)
    return {
        "request_id": out.get("request_id"),
        "session_id": out.get("session_id"),
        "browser_before": "open" if before == "RUNNING" else "closed",
        "released": out.get("released"),
        "already_released": out.get("already_released"),
        "reason": out.get("reason"),
        "browser_after": "open" if after == "RUNNING" else "closed",
        "request_status": out.get("request_status"),
        "next": f"submission-close --request-id {r['id']} --reviewer NAME --confirmed-not-submitted --reason TEXT "
                "(only if you did NOT submit)",
    }


# --- sanitized reporting --------------------------------------------------------------------------------

def _evidence(svc, request_id):
    events = [e for e in svc.store.events() if e["entity_id"] == request_id]
    return next((e["data"] for e in events if e["kind"] == "browserbase_evidence"), {}) or {}


def _result(svc, request_id):
    events = [e for e in svc.store.events() if e["entity_id"] == request_id]
    return next((e["data"] for e in events if e["kind"] == "browserbase_result"), {}) or {}


def _browser_state(svc, request, ev):
    session_id = ev.get("session_id")
    if request.get("executor") != EXECUTOR or not session_id:
        return None
    try:
        state = svc._browserbase_runtime(EXECUTOR)[0].session_status(session_id)
    except Exception:
        return "unknown"
    return "open" if state == "RUNNING" else "closed"


def run_summary(svc, request_id, out):
    """Whitelisted fields only (field names, labels, reasons, counts, statuses)."""
    ev = _evidence(svc, request_id)
    live = ev.get("live") or {}
    form = live.get("form") or {}
    request = svc.store.requests().get(request_id) or {}
    handoff = svc._handoff_events(request_id)
    final_submit = out.get("final_submit_clicked", _result(svc, request_id).get("final_submit_clicked"))
    return {
        "request_id": request_id,
        "status": out.get("status") or request.get("status"),
        "identity_state": ev.get("identity_state"),
        "stop_reason": live.get("stop_reason") or form.get("stop_reason"),
        "fields_filled": [{"field": f.get("field"), "page_label": f.get("page_label")}
                          for f in form.get("fields_filled", [])],
        "fields_skipped": [{"label": f.get("label"), "reason": f.get("reason")} for f in form.get("fields_skipped", [])],
        "fields_escalated": [{"label": f.get("label"), "reason": f.get("reason")}
                             for f in form.get("fields_escalated", [])],
        "controls_seen_count": form.get("controls_seen_count"),
        "nonfatal_blocked_requests": [{k: r.get(k) for k in BLOCKED_KEYS if k in r}
                                      for r in live.get("nonfatal_blocked_requests", [])],
        "cv_upload_status": form.get("cv_upload_status"),
        "cv_artifact_id": form.get("cv_artifact_id"),
        "cv_upload_visible_in_page": form.get("cv_upload_visible_in_page"),
        "cv_upload_steps": form.get("cv_upload_steps"),
        "advance_clicks": form.get("advance_clicks"),
        "blocked_fatal_operations": [{k: r.get(k) for k in BLOCKED_KEYS if k in r}
                                     for r in live.get("blocked_requests", [])],
        "final_submit_clicked": final_submit,
        "handoff": out.get("handoff") or ("local_browser_active" if handoff["handoff"] and not handoff["released"]
                                          else "released" if handoff["released"] else None),
        "page_guard_released": out.get("page_guard_released", ev.get("page_guard_released")),
        "browser_process_state": _browser_state(svc, request, ev),
        "review_expires_at": out.get("review_expires_at"),
        "error_category": None,
        "error_stage": None,
        "http_status": None,
    }


def _failure(svc, request_id, exc):
    """Sanitized failure fields only: the journaled category/stage/HTTP status, never raw text."""
    failure = dict(getattr(exc, "failure", None) or {})
    if not failure:
        failure = (svc.store.requests().get(request_id) or {}).get("failure") or {}
    out = {"error": "executor_interrupted" if failure else type(exc).__name__,
           "error_category": failure.get("category"), "error_stage": failure.get("stage")}
    if isinstance(failure.get("http_status"), int):
        out["http_status"] = failure["http_status"]
    return out
