from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from jobintel.models.taxonomy import APPLICATION_POST_SUBMISSION_STATUSES, ApplicationStatus


@dataclass
class Application:
    id: str
    job_id: str
    company: str
    role_title: str
    status: ApplicationStatus = ApplicationStatus.DISCOVERED
    sources: list[str] = field(default_factory=list)
    jd_snapshot: str = ""
    discovered_at: datetime | None = None
    deadline: datetime | None = None
    applied_at: datetime | None = None
    next_action: str | None = None
    next_action_deadline: datetime | None = None
    notes: str = ""
    application_url: str | None = None
    cv_version_id: str | None = None
    selected_project_ids: list[str] = field(default_factory=list)
    evidence_ids_used: list[str] = field(default_factory=list)
    updated_at: datetime | None = None
    # The system-generated CV version that was actually submitted, recorded
    # ONLY via an explicit "submitted with this exact version" action. None on
    # a submitted application means no system CV was attached (e.g. applied
    # manually with the candidate's own CV) -- never "lost", and never filled
    # in retroactively (ApplicationStore.attach_cv refuses after submission).
    submitted_cv_version_id: str | None = None

    @property
    def is_submitted(self) -> bool:
        return self.applied_at is not None or self.status in APPLICATION_POST_SUBMISSION_STATUSES


@dataclass(frozen=True)
class ApplicationStatusEvent:
    application_id: str
    from_status: ApplicationStatus | None
    to_status: ApplicationStatus
    occurred_at: datetime
    note: str = ""
    # Set on the event that moved the application into submission: records,
    # in the append-only history itself, which system CV was submitted --
    # or, with submitted_cv_version_id None, that none was attached.
    marks_submission: bool = False
    submitted_cv_version_id: str | None = None
