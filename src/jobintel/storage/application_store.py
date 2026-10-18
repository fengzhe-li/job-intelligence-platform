from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobintel.storage.locking import locked, sync_directory
from jobintel.storage.submission_store import SubmissionStore
from jobintel.models.submission import instant

from jobintel.models.application import Application, ApplicationStatusEvent
from jobintel.models.taxonomy import APPLICATION_POST_SUBMISSION_STATUSES, ApplicationStatus

_PRE_SUBMISSION_STATUSES = frozenset({ApplicationStatus.DISCOVERED, ApplicationStatus.SHORTLISTED, ApplicationStatus.MATERIALS_READY})


class ApplicationStore:
    """Application lifecycle tracker: current state plus append-only status history.

    Mirrors the LocalJobStore/HistoricalJobStore convention already used for jobs:
    current state lives in a JSON file that gets overwritten, status transitions are
    appended to a JSONL file that is never mutated or deleted, so "what exact status
    was this application in on a given date" stays answerable.
    """

    def __init__(self, root: Path | str = "data/local") -> None:
        self.root = Path(root)
        self.applications_dir = self.root / "applications"
        self.applications_path = self.applications_dir / "applications.json"
        self.status_history_path = self.applications_dir / "status_history.jsonl"

    @locked
    def create_application(self, application: Application, note: str = "") -> Application:
        """Idempotent per canonical vacancy: if an application already exists
        for `application.job_id` (or with the same id), that existing record is
        returned untouched -- a second click, a re-discovered duplicate
        observation, or a replayed request can never create a second
        application or overwrite the first one's history."""
        applications = self.read_applications()
        existing = next((item for item in applications if item.job_id == application.job_id or item.id == application.id), None)
        if existing is not None:
            return existing
        now = datetime.now(timezone.utc)
        if application.discovered_at is None:
            application = replace(application, discovered_at=now)
        # Created already-submitted (e.g. migrated legacy data): never invent
        # an applied_at or a submitted CV the caller didn't provide.
        submitted = application.status in APPLICATION_POST_SUBMISSION_STATUSES
        application = replace(application, updated_at=now)
        applications.append(application)
        self._write_applications(applications)
        self._append_status_event(ApplicationStatusEvent(application.id, None, application.status, now, note, marks_submission=submitted, submitted_cv_version_id=application.submitted_cv_version_id if submitted else None))
        return application

    def for_job(self, job_id: str) -> Application | None:
        return next((item for item in self.read_applications() if item.job_id == job_id), None)

    @locked
    def update_status(self, application_id: str, status: ApplicationStatus, note: str = "", submitted_cv_version_id: str | None = None) -> Application:
        """Append-only lifecycle transition. A submitted application can never
        move back to a pre-submission status (discovered/shortlisted/
        materials_ready) -- that would make "have I applied?" contradict the
        recorded history. Repeating the current status with no note is a
        no-op, so a double-submitted form doesn't duplicate history.

        On the transition INTO submission, `submitted_cv_version_id` names the
        system CV that was actually sent; it must already be attached. When
        omitted, the application is recorded as submitted with NO system CV
        -- an attached-but-unconfirmed draft is never assumed to be the one
        sent."""
        applications = self.read_applications()
        matches = [item for item in applications if item.id == application_id]
        if not matches:
            raise ValueError(f"No application with id {application_id}")
        existing = matches[0]
        SubmissionStore(self.root).assert_application_idle(application_id)
        if existing.is_submitted and status in _PRE_SUBMISSION_STATUSES:
            raise ValueError(f"Application {application_id} was already submitted ({existing.status.value}); it cannot move back to {status.value}")
        submitting = status in APPLICATION_POST_SUBMISSION_STATUSES and not existing.is_submitted
        if submitted_cv_version_id is not None:
            if not submitting:
                raise ValueError(f"Application {application_id}: a submitted CV can only be recorded at the moment of submission")
            if submitted_cv_version_id != existing.cv_version_id:
                raise ValueError(f"Application {application_id}: attach CV {submitted_cv_version_id} before recording it as submitted")
        if status == existing.status and not note:
            return existing
        if not existing.is_submitted:
            SubmissionStore(self.root).revoke_unqueued(application_id, "application_status_changed")
        now = datetime.now(timezone.utc)
        updated = replace(
            existing,
            status=status,
            updated_at=now,
            applied_at=now if submitting and existing.applied_at is None else existing.applied_at,
            submitted_cv_version_id=submitted_cv_version_id if submitting else existing.submitted_cv_version_id,
        )
        remaining = [item for item in applications if item.id != application_id]
        remaining.append(updated)
        self._write_applications(remaining)
        self._append_status_event(
            ApplicationStatusEvent(application_id, existing.status, status, now, note, marks_submission=submitting, submitted_cv_version_id=submitted_cv_version_id if submitting else None)
        )
        return updated

    @locked
    def update_application_url(self, application_id: str, new_url: str, note: str = "",
                               workable_account: str | None = None) -> Application:
        """Move an unsubmitted, idle application to the URL its hosted ATS really serves the form on --
        only the exact mapping submission.hosted_urls.hosted_application_url allows; recorded in history."""
        from jobintel.submission.hosted_urls import hosted_application_url
        applications = self.read_applications()
        existing = next((item for item in applications if item.id == application_id), None)
        if existing is None:
            raise ValueError(f"No application with id {application_id}")
        if existing.is_submitted:
            raise ValueError(f"Application {application_id} was already submitted; its URL is history")
        if new_url == existing.application_url:
            return existing
        if hosted_application_url(existing.application_url, workable_account=workable_account) != new_url:
            raise ValueError(f"Application {application_id}: {new_url!r} is not the hosted form URL of its recorded URL")
        SubmissionStore(self.root).assert_application_idle(application_id)
        SubmissionStore(self.root).revoke_unqueued(application_id, "application_url_changed")
        now = datetime.now(timezone.utc)
        updated = replace(existing, application_url=new_url, updated_at=now)
        self._write_applications([item for item in applications if item.id != application_id] + [updated])
        self._append_status_event(ApplicationStatusEvent(
            application_id, existing.status, existing.status, now,
            (note + " " if note else "") + f"application URL {existing.application_url} -> {new_url} (hosted form URL)"))
        return updated

    @locked
    def attach_cv(self, application_id: str, cv_version_id: str, selected_project_ids: list[str], evidence_ids_used: list[str]) -> Application:
        """Record which exact generated CV (and evidence) is intended for this
        application. Allowed freely BEFORE submission; after submission the CV
        that was actually sent is immutable, so any attempt to swap it raises
        (re-attaching the same version is a harmless no-op)."""
        applications = self.read_applications()
        matches = [item for item in applications if item.id == application_id]
        if not matches:
            raise ValueError(f"No application with id {application_id}")
        existing = matches[0]
        SubmissionStore(self.root).assert_application_idle(application_id)
        if existing.is_submitted:
            if cv_version_id == existing.cv_version_id and existing.submitted_cv_version_id == cv_version_id:
                return existing
            raise ValueError(
                f"Application {application_id} was already submitted "
                + (f"with system CV {existing.submitted_cv_version_id}" if existing.submitted_cv_version_id else "with no system CV attached")
                + "; a CV cannot be attached or replaced after submission"
            )
        if cv_version_id != existing.cv_version_id:
            SubmissionStore(self.root).revoke_unqueued(application_id, "attached_cv_changed")
        now = datetime.now(timezone.utc)
        updated = replace(
            existing,
            cv_version_id=cv_version_id,
            selected_project_ids=selected_project_ids,
            evidence_ids_used=evidence_ids_used,
            updated_at=now,
        )
        remaining = [item for item in applications if item.id != application_id]
        remaining.append(updated)
        self._write_applications(remaining)
        # Auditable: attaching a CV is recorded in the append-only history as
        # a same-status event, so the history shows every version considered.
        self._append_status_event(ApplicationStatusEvent(application_id, existing.status, existing.status, now, f"CV attached: {cv_version_id}"))
        return updated

    @locked
    def _apply_verified_receipt(self, receipt_id: str) -> Application:
        """Replay a durable verified receipt, never an executor-supplied dict.

        Receipt -> durable status event -> current-state projection. A crash at
        either boundary can be recovered using the stable receipt event ID.
        """
        receipt_event = next((e for e in SubmissionStore(self.root).receipts() if e["event_id"] == receipt_id), None)
        if receipt_event is None or receipt_event["kind"] != "receipt_verified":
            raise ValueError("A persisted, verified receipt is required")
        receipt = receipt_event["data"]
        applications = self.read_applications()
        existing = next((a for a in applications if a.id == receipt["application_id"]), None)
        if existing is None or existing.job_id != receipt["job_id"] or existing.cv_version_id != receipt["cv_artifact_id"] or existing.application_url != receipt["application_url"]:
            raise ValueError("Verified receipt conflicts with application identity or CV")
        if existing.is_submitted and existing.submission_receipt_id != receipt_id:
            raise ValueError("Application was already submitted through another path")
        if not existing.is_submitted and existing.status not in _PRE_SUBMISSION_STATUSES:
            raise ValueError("Application is no longer eligible for submission reconciliation")
        event_id = "submission:" + receipt_id
        events = self.status_history_for(existing.id)
        event = next((e for e in events if e.event_id == event_id), None)
        if event and (event.receipt_id != receipt_id or event.submitted_cv_version_id != receipt["cv_artifact_id"] or not event.marks_submission):
            raise ValueError("Conflicting submission history event")
        if existing.is_submitted:
            if event is None:
                raise ValueError("Submitted receipt has no durable history event")
            return existing  # Never regress an interview/offer on reconciliation.
        submitted_at = instant(receipt["submitted_at"])
        if event is None:
            self._append_status_event(ApplicationStatusEvent(
                existing.id, existing.status, ApplicationStatus.APPLIED, submitted_at,
                "Confirmed submission receipt " + receipt_id, marks_submission=True,
                submitted_cv_version_id=receipt["cv_artifact_id"], event_id=event_id, receipt_id=receipt_id))
        updated = replace(existing, status=ApplicationStatus.APPLIED, applied_at=submitted_at,
                          submitted_cv_version_id=receipt["cv_artifact_id"], submission_receipt_id=receipt_id,
                          updated_at=datetime.now(timezone.utc))
        self._write_applications([updated if a.id == existing.id else a for a in applications])
        return updated

    @locked
    def read_applications(self) -> list[Application]:
        if not self.applications_path.exists():
            return []
        payload = json.loads(self.applications_path.read_text(encoding="utf-8"))
        return [_application_from_dict(item) for item in payload]

    @locked
    def status_history_for(self, application_id: str) -> list[ApplicationStatusEvent]:
        return [event for event in self._read_status_history() if event.application_id == application_id]

    def _write_applications(self, applications: list[Application]) -> None:
        self.applications_dir.mkdir(parents=True, exist_ok=True)
        payload = [_application_to_dict(application) for application in applications]
        tmp = self.applications_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.applications_path)
        sync_directory(self.applications_dir)

    def _append_status_event(self, event: ApplicationStatusEvent) -> None:
        self.applications_dir.mkdir(parents=True, exist_ok=True)
        with self.status_history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_event_to_dict(event), ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(self.applications_dir)

    def _read_status_history(self) -> list[ApplicationStatusEvent]:
        if not self.status_history_path.exists():
            return []
        events = []
        with self.status_history_path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(_event_from_dict(json.loads(line)))
        return events


def _application_to_dict(application: Application) -> dict[str, Any]:
    return _jsonable(asdict(application))


def _application_from_dict(payload: dict[str, Any]) -> Application:
    return Application(
        id=payload["id"],
        job_id=payload["job_id"],
        company=payload["company"],
        role_title=payload["role_title"],
        status=ApplicationStatus(payload.get("status", ApplicationStatus.DISCOVERED.value)),
        sources=payload.get("sources", []),
        jd_snapshot=payload.get("jd_snapshot", ""),
        discovered_at=_datetime(payload.get("discovered_at")),
        deadline=_datetime(payload.get("deadline")),
        applied_at=_datetime(payload.get("applied_at")),
        next_action=payload.get("next_action"),
        next_action_deadline=_datetime(payload.get("next_action_deadline")),
        notes=payload.get("notes", ""),
        application_url=payload.get("application_url"),
        cv_version_id=payload.get("cv_version_id"),
        selected_project_ids=payload.get("selected_project_ids", []),
        evidence_ids_used=payload.get("evidence_ids_used", []),
        updated_at=_datetime(payload.get("updated_at")),
        submitted_cv_version_id=payload.get("submitted_cv_version_id"),
        submission_receipt_id=payload.get("submission_receipt_id"),
    )


def _event_to_dict(event: ApplicationStatusEvent) -> dict[str, Any]:
    return {
        "application_id": event.application_id,
        "from_status": event.from_status.value if event.from_status else None,
        "to_status": event.to_status.value,
        "occurred_at": event.occurred_at.isoformat(),
        "note": event.note,
        "marks_submission": event.marks_submission,
        "submitted_cv_version_id": event.submitted_cv_version_id,
        "event_id": event.event_id,
        "receipt_id": event.receipt_id,
    }


def _event_from_dict(payload: dict[str, Any]) -> ApplicationStatusEvent:
    return ApplicationStatusEvent(
        application_id=payload["application_id"],
        from_status=ApplicationStatus(payload["from_status"]) if payload.get("from_status") else None,
        to_status=ApplicationStatus(payload["to_status"]),
        occurred_at=_datetime(payload["occurred_at"]),
        note=payload.get("note", ""),
        marks_submission=bool(payload.get("marks_submission", False)),
        submitted_cv_version_id=payload.get("submitted_cv_version_id"),
        event_id=payload.get("event_id"),
        receipt_id=payload.get("receipt_id"),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
