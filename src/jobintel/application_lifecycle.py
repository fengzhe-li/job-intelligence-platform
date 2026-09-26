"""Single ownership boundary between job triage state and application lifecycle.

Two stores used to hold overlapping status: `workflow_status.json`
(LocalJobStore, per job: new/saved/applied/oa/interview/rejected/offer/ignore)
and ApplicationStore (applications.json + append-only status_history.jsonl).
Nothing kept them consistent, so a job could read APPLIED in one and have no
application -- or a REJECTED application -- in the other.

Ownership, now explicit:

* PRE-APPLICATION triage (new / saved / ignore) is owned by the job workflow
  store. CV-ready / review-required are derived from CV artifacts, never
  stored as a status.
* From the moment an application is SUBMITTED, ApplicationStore is the only
  authority for its lifecycle (applied, online assessment, interviews, offer,
  rejected, withdrawn, expired). Post-submission values are never written to
  workflow_status.json any more.
* `set_job_status` is the one entry point the dashboard uses: a pre-application
  value goes to the workflow store; a post-submission value creates (at most
  one, idempotently) the job's Application and appends a lifecycle transition.
* `effective_workflow_status` is the one read-side projection every view uses,
  so the inbox, priority queue and tracker can't disagree.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from jobintel.models.application import Application
from jobintel.models.job import Job
from jobintel.models.taxonomy import ApplicationStatus, WorkflowStatus
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.local_store import LocalJobStore

PRE_APPLICATION_WORKFLOW = frozenset({WorkflowStatus.NEW, WorkflowStatus.SAVED, WorkflowStatus.IGNORE})

# Coarse job-workflow value -> the ApplicationStore status it means. "interview"
# maps to the FIRST interview stage; finer stages are set in the tracker.
_WORKFLOW_TO_APPLICATION = {
    WorkflowStatus.APPLIED: ApplicationStatus.APPLIED,
    WorkflowStatus.OA: ApplicationStatus.ONLINE_ASSESSMENT,
    WorkflowStatus.INTERVIEW: ApplicationStatus.PHONE_SCREEN,
    WorkflowStatus.OFFER: ApplicationStatus.OFFER,
    WorkflowStatus.REJECTED: ApplicationStatus.REJECTED,
    WorkflowStatus.WITHDRAWN: ApplicationStatus.WITHDRAWN,
    WorkflowStatus.EXPIRED: ApplicationStatus.EXPIRED,
}

_APPLICATION_TO_WORKFLOW = {
    ApplicationStatus.APPLIED: WorkflowStatus.APPLIED,
    ApplicationStatus.ONLINE_ASSESSMENT: WorkflowStatus.OA,
    ApplicationStatus.PHONE_SCREEN: WorkflowStatus.INTERVIEW,
    ApplicationStatus.TECHNICAL_INTERVIEW: WorkflowStatus.INTERVIEW,
    ApplicationStatus.FINAL_INTERVIEW: WorkflowStatus.INTERVIEW,
    ApplicationStatus.OFFER: WorkflowStatus.OFFER,
    ApplicationStatus.REJECTED: WorkflowStatus.REJECTED,
    ApplicationStatus.WITHDRAWN: WorkflowStatus.WITHDRAWN,
    ApplicationStatus.EXPIRED: WorkflowStatus.EXPIRED,
}

MIGRATION_LOG_NAME = "legacy_workflow_migration.jsonl"


def application_id_for(job: Job) -> str:
    """Stable, one-per-canonical-vacancy id."""
    return f"app:{job.id}"


def ensure_application(job: Job, application_store: ApplicationStore, status: ApplicationStatus = ApplicationStatus.DISCOVERED, note: str = "") -> Application:
    """Get-or-create the ONE application for this canonical job, snapshotting
    the JD, application URL, source provenance and deadline at creation so
    they stay auditable even if the job later changes or disappears."""
    existing = application_store.for_job(job.id)
    if existing is not None:
        return existing
    return application_store.create_application(
        Application(
            id=application_id_for(job),
            job_id=job.id,
            company=job.company,
            role_title=job.title,
            status=status,
            sources=sorted({observation.source_name for observation in job.source_observations}),
            jd_snapshot=job.description,
            deadline=job.earliest_deadline,
            application_url=job.canonical_application_url or None,
        ),
        note=note,
    )


def set_job_status(job_id: str, status: WorkflowStatus, store: LocalJobStore, application_store: ApplicationStore, note: str = "") -> None:
    """The one write path for a job's status from the inbox/triage UI."""
    application = application_store.for_job(job_id)
    if status in PRE_APPLICATION_WORKFLOW:
        if application is not None and application.is_submitted:
            raise ValueError(
                f"Job {job_id} already has a submitted application ({application.status.value}); "
                "its lifecycle is tracked in the application tracker and cannot be reset to a triage status"
            )
        store.set_workflow_status(job_id, status)
        return
    target = _WORKFLOW_TO_APPLICATION[status]
    if application is None:
        job = next((item for item in store.read_jobs() if item.id == job_id), None)
        if job is None:
            raise KeyError(job_id)
        application = ensure_application(job, application_store)
    if _APPLICATION_TO_WORKFLOW.get(application.status) == status:
        return  # already there at this granularity (e.g. already in a later interview stage)
    if status == WorkflowStatus.APPLIED and application.is_submitted:
        return  # every later stage already implies "applied" -- never regress it
    application_store.update_status(application.id, target, note=note or f"set from job workflow: {status.value}")


def effective_workflow_status(job: Job, application: Application | None) -> WorkflowStatus:
    """What status to SHOW for a job. A submitted (or closed) application
    always wins; otherwise the pre-application triage value."""
    if application is not None and application.status in _APPLICATION_TO_WORKFLOW and (application.is_submitted or application.status in {ApplicationStatus.WITHDRAWN, ApplicationStatus.EXPIRED}):
        return _APPLICATION_TO_WORKFLOW[application.status]
    if job.workflow_status in PRE_APPLICATION_WORKFLOW:
        return job.workflow_status
    # A legacy post-submission value with no application behind it (should
    # not survive `migrate_legacy_workflow_statuses`); shown as-is rather than
    # silently downgraded.
    return job.workflow_status


def overlay_lifecycle(jobs: list[Job], application_store: ApplicationStore) -> list[Job]:
    by_job: dict[str, Application] = {}
    for application in application_store.read_applications():
        by_job.setdefault(application.job_id, application)
    return [replace(job, workflow_status=effective_workflow_status(job, by_job.get(job.id))) for job in jobs]


def migrate_legacy_workflow_statuses(store: LocalJobStore, application_store: ApplicationStore) -> list[str]:
    """One-time, idempotent migration of pre-Phase-3 data: any job whose
    workflow_status.json value is post-submission (applied/oa/interview/offer/
    rejected) gets its ONE Application created in the matching lifecycle
    status, and its workflow entry reset to `saved` (the true pre-application
    state). The original value is preserved in the application's history note
    and in an append-only migration log. `applied_at` is left unknown -- the
    real submission time was never recorded, and inventing one would be false.
    Returns migrated job ids."""
    statuses = store.read_workflow_statuses()
    jobs = {job.id: job for job in store.read_jobs()}
    migrated: list[str] = []
    for job_id, value in sorted(statuses.items()):
        status = WorkflowStatus(value)
        if status in PRE_APPLICATION_WORKFLOW:
            continue
        job = jobs.get(job_id)
        application = application_store.for_job(job_id)
        target = _WORKFLOW_TO_APPLICATION[status]
        if application is None:
            if job is None:
                continue  # can't snapshot a job that no longer exists; leave the entry untouched
            application = ensure_application(job, application_store, status=target, note=f"migrated from legacy workflow_status '{value}' (original submission time unknown)")
        _append_migration_log(application_store, {"job_id": job_id, "legacy_workflow_status": value, "application_id": application.id, "migrated_at": datetime.now(timezone.utc).isoformat()})
        store.set_workflow_status(job_id, WorkflowStatus.SAVED)
        migrated.append(job_id)
    return migrated


def _append_migration_log(application_store: ApplicationStore, record: dict) -> None:
    path = Path(application_store.applications_dir) / MIGRATION_LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
