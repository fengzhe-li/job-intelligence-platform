from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jobintel.application_lifecycle import PRE_APPLICATION_WORKFLOW, overlay_lifecycle
from jobintel.company_registry import configured_identifier_counts
from jobintel.company_coverage import classify_registry, coverage_metrics, manual_watchlist
from jobintel.config import RankingConfig, default_ranking_config
from jobintel.models.application import Application, ApplicationStatusEvent
from jobintel.models.cv_artifact import GeneratedCVArtifact
from jobintel.models.job import Job
from jobintel.models.taxonomy import ApplicationStatus, ReviewGateStatus, WorkflowStatus
from jobintel.pipeline.ingestion import RankedJob, rank_jobs
from jobintel.profile_ingestion import load_candidate_profile
from jobintel.source_coverage import (
    MANUAL_FALLBACK_ONLY,
    PARTIAL_COVERAGE,
    all_profiles,
    generic_profile_for_connector_type,
)
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.storage.watchlist_store import WatchlistStore

# ---------------------------------------------------------------------------
# Shared lookups
# ---------------------------------------------------------------------------


def latest_application_by_job(applications: list[Application]) -> dict[str, Application]:
    """One application per job_id -- the most recently updated one, if a job
    somehow has more than one (shouldn't happen via the normal CLI/dashboard
    path, but this must not crash or pick arbitrarily if it does)."""
    by_job: dict[str, Application] = {}
    for application in applications:
        existing = by_job.get(application.job_id)
        if existing is None or (application.updated_at or datetime.min.replace(tzinfo=timezone.utc)) >= (existing.updated_at or datetime.min.replace(tzinfo=timezone.utc)):
            by_job[application.job_id] = application
    return by_job


def latest_cv_by_job(cv_artifacts: list[GeneratedCVArtifact]) -> dict[str, GeneratedCVArtifact]:
    by_job: dict[str, GeneratedCVArtifact] = {}
    for artifact in cv_artifacts:
        existing = by_job.get(artifact.job_id)
        if existing is None or artifact.generated_at >= existing.generated_at:
            by_job[artifact.job_id] = artifact
    return by_job


def _rank(store: LocalJobStore, config: RankingConfig, now: datetime) -> list[RankedJob]:
    candidate = load_candidate_profile(store.root)
    jobs = overlay_lifecycle(store.read_jobs(), ApplicationStore(store.root))
    return rank_jobs(jobs, candidate=candidate, config=config, now=now)


# ---------------------------------------------------------------------------
# Section E: explainable priority / action queue
# ---------------------------------------------------------------------------

# Deliberately small, fixed, documented adjustments on top of the existing
# candidate-fit ranking (`match.overall_priority`, itself already explainable
# via `match.components`/`match.explanation`) -- never a second, opaque score.
# Each adjustment has a name and a plain-English reason so "why is this high
# priority" is always answerable by listing which of these fired.
_DEADLINE_URGENT_BOOST = 3.0  # deadline within 2 days
_DEADLINE_SOON_BOOST = 1.5  # deadline within 7 days
_NOT_YET_ACTIONED_BOOST = 0.5
_CV_READY_BOOST = 0.3
_NEW_TODAY_BOOST = 0.3


@dataclass(frozen=True)
class ActionQueueItem:
    job_id: str
    title: str
    company: str
    base_priority: float
    priority_score: float
    reasons: list[str]
    deadline: datetime | None
    deadline_days_remaining: int | None
    application_status: str | None
    cv_status: str | None
    workflow_status: str


def explain_priority(
    item: RankedJob,
    application: Application | None,
    cv_artifact: GeneratedCVArtifact | None,
    now: datetime,
) -> tuple[float, list[str]]:
    """Returns (priority_score, reasons). Every point of adjustment beyond the
    existing candidate-fit score (`match.overall_priority`) is named here --
    nothing is added silently. Unknown deadline contributes nothing, never a
    penalty (see Section F: unknown must stay unknown, never treated as far
    away or irrelevant).
    """
    job = item.job
    score = item.match.overall_priority
    reasons: list[str] = []

    if item.match.primary_role_tracks:
        reasons.append(f"{job.title} ({', '.join(t.value for t in item.match.primary_role_tracks)})")
    if item.match.strongest_supporting_evidence:
        reasons.append("strong evidence: " + "; ".join(item.match.strongest_supporting_evidence[:2]))

    deadline = job.earliest_deadline
    if deadline is not None:
        days_remaining = (deadline - now).days
        if days_remaining <= 2:
            score += _DEADLINE_URGENT_BOOST
            reasons.insert(0, f"deadline in {max(days_remaining, 0)} day(s)")
        elif days_remaining <= 7:
            score += _DEADLINE_SOON_BOOST
            reasons.insert(0, f"deadline in {days_remaining} days")
        else:
            reasons.append(f"deadline in {days_remaining} days")
    # else: genuinely no known deadline -- say nothing, never "no deadline
    # (treated as low urgency)" and never a fabricated distant date.

    submitted = application is not None and application.is_submitted
    if submitted:
        reasons.insert(0, f"already submitted ({application.status.value}) -- track it in the application tracker")
    elif job.workflow_status == WorkflowStatus.IGNORE:
        reasons.append("marked ignore")
    elif job.workflow_status in PRE_APPLICATION_WORKFLOW:
        score += _NOT_YET_ACTIONED_BOOST
        reasons.append("not yet applied")

    if cv_artifact is not None:
        if cv_artifact.review_status == ReviewGateStatus.AUTO_PREPARE:
            score += _CV_READY_BOOST
            reasons.append("CV ready")
        elif cv_artifact.review_status == ReviewGateStatus.REVIEW_REQUIRED:
            reasons.append("CV generated -- human review required before use")
        elif cv_artifact.review_status == ReviewGateStatus.BLOCK_AUTO_SUBMISSION:
            reasons.append("CV generated -- blocked, needs manual rework")
    else:
        reasons.append("CV not generated yet")

    first_seen = min((observation.first_seen_at for observation in job.source_observations), default=None)
    if first_seen is not None and (now - first_seen) < timedelta(days=1):
        score += _NEW_TODAY_BOOST
        reasons.append("newly discovered today")

    return round(score, 3), reasons


def build_action_queue(
    store: LocalJobStore | None = None,
    application_store: ApplicationStore | None = None,
    cv_store: CVArtifactStore | None = None,
    config: RankingConfig | None = None,
    now: datetime | None = None,
    limit: int = 50,
) -> list[ActionQueueItem]:
    store = store or LocalJobStore()
    application_store = application_store or ApplicationStore(store.root)
    cv_store = cv_store or CVArtifactStore(store.root)
    config = config or default_ranking_config()
    now = now or datetime.now(timezone.utc)

    ranked = [item for item in _rank(store, config, now) if item.match.overall_priority > -100.0 and _is_active(item.job)]
    applications = latest_application_by_job(application_store.read_applications())
    cvs = latest_cv_by_job(cv_store.read_all())

    items = []
    for ranked_item in ranked:
        application = applications.get(ranked_item.job.id)
        if (application is not None and application.is_submitted) or ranked_item.job.workflow_status == WorkflowStatus.IGNORE:
            continue  # nothing to action: lives in the tracker / was dismissed
        cv_artifact = cvs.get(ranked_item.job.id)
        score, reasons = explain_priority(ranked_item, application, cv_artifact, now)
        deadline = ranked_item.job.earliest_deadline
        items.append(
            ActionQueueItem(
                job_id=ranked_item.job.id,
                title=ranked_item.job.title,
                company=ranked_item.job.company,
                base_priority=ranked_item.match.overall_priority,
                priority_score=score,
                reasons=reasons,
                deadline=deadline,
                deadline_days_remaining=(deadline - now).days if deadline is not None else None,
                application_status=application.status.value if application else None,
                cv_status=cv_artifact.review_status.value if cv_artifact else None,
                workflow_status=ranked_item.job.workflow_status.value,
            )
        )
    items.sort(key=lambda entry: entry.priority_score, reverse=True)
    return items[:limit]


def _is_active(job: Job) -> bool:
    return any(observation.active for observation in job.source_observations)


# ---------------------------------------------------------------------------
# Section F: deadline system
# ---------------------------------------------------------------------------

DEADLINE_BUCKETS = ("overdue", "today", "within_48h", "within_7d", "later", "unknown")


@dataclass(frozen=True)
class DeadlineEntry:
    job_id: str
    title: str
    company: str
    deadline: datetime | None
    deadline_conflict: bool
    deadline_observations: dict[str, str]


def build_deadline_view(
    store: LocalJobStore | None = None,
    config: RankingConfig | None = None,
    now: datetime | None = None,
) -> dict[str, list[DeadlineEntry]]:
    store = store or LocalJobStore()
    config = config or default_ranking_config()
    now = now or datetime.now(timezone.utc)
    ranked = [item for item in _rank(store, config, now) if item.match.overall_priority > -100.0 and _is_active(item.job)]

    buckets: dict[str, list[DeadlineEntry]] = {name: [] for name in DEADLINE_BUCKETS}
    for item in ranked:
        job = item.job
        deadline = job.earliest_deadline
        entry = DeadlineEntry(
            job_id=job.id,
            title=job.title,
            company=job.company,
            deadline=deadline,
            deadline_conflict=job.deadline_conflict,
            deadline_observations={source: value.isoformat() for source, value in job.deadline_observations.items()},
        )
        if deadline is None:
            buckets["unknown"].append(entry)
            continue
        remaining = deadline - now
        if remaining.total_seconds() < 0:
            buckets["overdue"].append(entry)
        elif remaining <= timedelta(hours=24) and deadline.date() == now.date():
            buckets["today"].append(entry)
        elif remaining <= timedelta(hours=48):
            buckets["within_48h"].append(entry)
        elif remaining <= timedelta(days=7):
            buckets["within_7d"].append(entry)
        else:
            buckets["later"].append(entry)
    for name in buckets:
        buckets[name].sort(key=lambda entry: entry.deadline or datetime.max.replace(tzinfo=timezone.utc))
    return buckets


# ---------------------------------------------------------------------------
# Section C: Today view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TodaySummary:
    generated_at: datetime
    new_jobs_today: int
    high_priority_not_actioned: int
    cvs_ready: int
    cvs_needing_review: int
    applications_submitted_today: int
    applications_awaiting_response: int
    online_assessments: int
    interviews: int
    offers: int
    rejections: int
    deadlines_within_48h: int
    manual_checks_due: int
    source_failures: int
    company_failures: int
    deadlines_overdue: int = 0
    # Persisted results of the most recent daily refresh (None if it never
    # ran) and whether that is fresh -- Today reports what the refresh
    # actually did, it never recomputes contradictory numbers of its own.
    last_daily_refresh: dict[str, Any] | None = None
    daily_refresh_freshness: str = "never"  # never | fresh | stale
    # (source, state, detail) for every source whose automation did not do its
    # job -- failed, partial, stale, degraded, or configured-but-never-run.
    sources_needing_attention: list[tuple[str, str, str]] = field(default_factory=list)


def build_today_summary(
    store: LocalJobStore | None = None,
    application_store: ApplicationStore | None = None,
    cv_store: CVArtifactStore | None = None,
    config: RankingConfig | None = None,
    registry_path: Path | str | None = None,
    now: datetime | None = None,
) -> TodaySummary:
    store = store or LocalJobStore()
    application_store = application_store or ApplicationStore(store.root)
    cv_store = cv_store or CVArtifactStore(store.root)
    config = config or default_ranking_config()
    now = now or datetime.now(timezone.utc)

    ranked = [item for item in _rank(store, config, now) if item.match.overall_priority > -100.0 and _is_active(item.job)]
    applications = application_store.read_applications()
    applications_by_job = latest_application_by_job(applications)
    cvs = cv_store.read_all()
    # "CV ready / needs review" are ACTION items: only for jobs not yet
    # submitted (a submitted application's CV is history, not a to-do).
    cvs_by_job = {job_id: artifact for job_id, artifact in latest_cv_by_job(cvs).items() if not (applications_by_job.get(job_id) and applications_by_job[job_id].is_submitted)}

    new_today = [item for item in ranked if _first_seen_today(item.job, now)]
    action_queue = build_action_queue(store, application_store, cv_store, config, now, limit=10_000)
    high_priority_not_actioned = len(
        [entry for entry in action_queue if entry.priority_score >= 4.0 and entry.application_status in (None, ApplicationStatus.DISCOVERED.value)]
    )

    cvs_ready = len([artifact for artifact in cvs_by_job.values() if artifact.review_status == ReviewGateStatus.AUTO_PREPARE])
    cvs_needing_review = len([artifact for artifact in cvs_by_job.values() if artifact.review_status != ReviewGateStatus.AUTO_PREPARE])

    applications_today = [application for application in applications if application.applied_at is not None and application.applied_at.date() == now.date()]
    awaiting = [application for application in applications if application.status in {ApplicationStatus.APPLIED, ApplicationStatus.ONLINE_ASSESSMENT, ApplicationStatus.PHONE_SCREEN, ApplicationStatus.TECHNICAL_INTERVIEW, ApplicationStatus.FINAL_INTERVIEW}]
    online_assessments = len([application for application in applications if application.status == ApplicationStatus.ONLINE_ASSESSMENT])
    interviews = len([application for application in applications if application.status in {ApplicationStatus.PHONE_SCREEN, ApplicationStatus.TECHNICAL_INTERVIEW, ApplicationStatus.FINAL_INTERVIEW}])
    offers = len([application for application in applications if application.status == ApplicationStatus.OFFER])
    rejections = len([application for application in applications if application.status == ApplicationStatus.REJECTED])

    deadline_buckets = build_deadline_view(store, config, now)
    deadlines_soon = len(deadline_buckets["today"]) + len(deadline_buckets["within_48h"])

    entries = _registry_entries(registry_path)
    results = classify_registry(entries)
    # The unified watchlist (companies + explicitly-tracked manual/partial
    # sources, Section J) filtered to what's actually DUE right now -- not
    # just "on the watchlist at all" -- so this count matches what a user
    # opening the watchlist page today would actually need to act on.
    watchlist_items = build_manual_watchlist_view(store, registry_path=registry_path, watchlist_store=WatchlistStore(store.root), now=now)
    manual_checks_due = len([item for item in watchlist_items if item.is_due])

    # Same derivation as the Source Health page -- one source of truth.
    health_rows = build_source_health_view(store, registry_path, now=now)
    attention = [(row.source_name, row.state, row.error or row.warning or "") for row in health_rows if row.state in SOURCE_ATTENTION_STATES]
    source_failures = len([row for row in health_rows if row.state in {SOURCE_FAILED, SOURCE_PARTIAL_REFRESH}])
    company_failures = len([result for result in results if result.state == "TEMPORARILY_FAILED"])

    last_daily = store.read_daily_report()
    finished = _parse_iso(last_daily.get("finished_at")) if last_daily else None
    freshness = "never" if finished is None else ("fresh" if now - finished <= STALE_AFTER else "stale")

    return TodaySummary(
        generated_at=now,
        new_jobs_today=len(new_today),
        high_priority_not_actioned=high_priority_not_actioned,
        cvs_ready=cvs_ready,
        cvs_needing_review=cvs_needing_review,
        applications_submitted_today=len(applications_today),
        applications_awaiting_response=len(awaiting),
        online_assessments=online_assessments,
        interviews=interviews,
        offers=offers,
        rejections=rejections,
        deadlines_within_48h=deadlines_soon,
        manual_checks_due=manual_checks_due,
        source_failures=source_failures,
        company_failures=company_failures,
        deadlines_overdue=len(deadline_buckets["overdue"]),
        last_daily_refresh=last_daily,
        daily_refresh_freshness=freshness,
        sources_needing_attention=attention,
    )


def _first_seen_today(job: Job, now: datetime) -> bool:
    first_seen = min((observation.first_seen_at for observation in job.source_observations), default=None)
    if first_seen is None:
        return False
    return first_seen.astimezone(timezone.utc).date() == now.astimezone(timezone.utc).date()


# ---------------------------------------------------------------------------
# Section H / I: application tracker + detail/timeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplicationTimelineEvent:
    occurred_at: datetime
    label: str
    detail: str = ""


@dataclass(frozen=True)
class ApplicationDetail:
    application: Application
    job: Job | None
    cv_artifacts: list[GeneratedCVArtifact]
    current_cv: GeneratedCVArtifact | None
    timeline: list[ApplicationTimelineEvent]


def build_application_detail(
    application_id: str,
    store: LocalJobStore | None = None,
    application_store: ApplicationStore | None = None,
    cv_store: CVArtifactStore | None = None,
) -> ApplicationDetail:
    store = store or LocalJobStore()
    application_store = application_store or ApplicationStore(store.root)
    cv_store = cv_store or CVArtifactStore(store.root)

    applications = {application.id: application for application in application_store.read_applications()}
    application = applications.get(application_id)
    if application is None:
        raise KeyError(application_id)

    jobs = {job.id: job for job in store.read_jobs()}
    job = jobs.get(application.job_id)
    cv_artifacts = sorted(cv_store.for_application(application_id) or cv_store.for_job(application.job_id), key=lambda artifact: artifact.generated_at)
    # The CV to show is the one actually recorded on the application -- the
    # frozen submitted version if submitted, else the attached one. Never
    # silently substitute "the latest generated" for what was sent.
    # Submitted: only the CV explicitly recorded as submitted (possibly none).
    # Not yet submitted: the attached draft, if any.
    recorded_cv_id = application.submitted_cv_version_id if application.is_submitted else application.cv_version_id
    current_cv = next((artifact for artifact in cv_artifacts if artifact.id == recorded_cv_id), None) or (cv_store.get(recorded_cv_id) if recorded_cv_id else None)

    timeline: list[ApplicationTimelineEvent] = []
    if job is not None:
        first_seen = min((observation.first_seen_at for observation in job.source_observations), default=None)
        if first_seen is not None:
            sources = ", ".join(sorted({observation.source_name for observation in job.source_observations}))
            timeline.append(ApplicationTimelineEvent(first_seen, "Job discovered", f"via {sources}"))
    for artifact in cv_artifacts:
        timeline.append(ApplicationTimelineEvent(artifact.generated_at, f"Tailored CV generated ({artifact.id})", f"review: {artifact.review_status.value}, {artifact.page_count} page(s)"))
    for event in application_store.status_history_for(application_id):
        if event.from_status is None:
            label = f"Application record created ({event.to_status.value})"
        elif event.from_status == event.to_status:
            label = "Event"
        else:
            label = f"Status: {event.from_status.value} \u2192 {event.to_status.value}"
        detail = event.note
        if event.marks_submission:
            cv_text = f"submitted with system CV {event.submitted_cv_version_id}" if event.submitted_cv_version_id else "No system CV attached -- applied manually"
            detail = f"{cv_text}{'; ' + event.note if event.note else ''}"
        timeline.append(ApplicationTimelineEvent(event.occurred_at, label, detail))
    timeline.sort(key=lambda item: item.occurred_at)

    return ApplicationDetail(application=application, job=job, cv_artifacts=cv_artifacts, current_cv=current_cv, timeline=timeline)


def build_application_list(
    store: LocalJobStore | None = None,
    application_store: ApplicationStore | None = None,
    status: ApplicationStatus | None = None,
) -> list[Application]:
    application_store = application_store or ApplicationStore((store or LocalJobStore()).root)
    applications = application_store.read_applications()
    if status is not None:
        applications = [application for application in applications if application.status == status]
    return sorted(applications, key=lambda application: application.updated_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)


# ---------------------------------------------------------------------------
# Section L: source & company health
# ---------------------------------------------------------------------------

ACTIVE_AND_REFRESHED = "ACTIVE_AND_REFRESHED"
ACTIVE_NOT_REFRESHED = "ACTIVE_NOT_REFRESHED"  # configured, but no refresh ever recorded
NOT_CONFIGURED = "NOT_CONFIGURED"
SOURCE_PARTIAL_COVERAGE = "PARTIAL_COVERAGE"  # kept for backward-compatible imports
SOURCE_PARTIAL_REFRESH = "PARTIAL_REFRESH"  # last refresh: some identifiers failed
SOURCE_MANUAL_ONLY = "MANUAL_ONLY"
SOURCE_FAILED = "FAILED"
SOURCE_STALE = "STALE"  # last successful evidence is older than STALE_AFTER
SOURCE_DEGRADED = "DEGRADED"  # refreshed, but the result looks wrong (suspicious drop)

# A daily-refreshed source with no attempt in this long is never shown as
# healthy, whatever its last status was.
STALE_AFTER = timedelta(hours=36)

# Health states that mean "automation did NOT do its job for this source".
SOURCE_ATTENTION_STATES = frozenset({SOURCE_FAILED, SOURCE_PARTIAL_REFRESH, SOURCE_STALE, SOURCE_DEGRADED, ACTIVE_NOT_REFRESHED})

# Legacy health records (written before the explicit `not_configured` flag)
# are recognised by the missing-credential messages connectors raise.
_LEGACY_NOT_CONFIGURED_MARKERS = ("requires ADZUNA_APP_ID", "requires WTTJ_API_KEY", "No Adzuna", "No Workday tenant")

# Optional credentialed sources: configured iff these env vars are all set.
_CREDENTIAL_ENV = {"adzuna": ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"), "welcome_to_the_jungle": ("WTTJ_API_KEY",)}
# Sources that need no per-company configuration or credentials at all.
_ALWAYS_CONFIGURED = frozenset({"prospects"})


@dataclass(frozen=True)
class SourceHealthRow:
    source_name: str
    display_name: str
    state: str
    last_attempted_refresh: str | None
    last_successful_refresh: str | None
    jobs_observed: int | None
    new_jobs: int | None
    changed_jobs: int | None
    error: str | None
    warning: str | None
    coverage_limitation: str
    automation_status: str = ""
    configured_identifiers: int | None = None
    closed_last_refresh: int | None = None
    incomplete_scopes: dict[str, str] = field(default_factory=dict)


def build_source_health_view(
    store: LocalJobStore | None = None,
    registry_path: Path | str | None = None,
    now: datetime | None = None,
    environ: dict[str, str] | None = None,
) -> list[SourceHealthRow]:
    """Every source family that exists -- curated profiles, any connector
    family live in the registry (Workday and future ones included), and any
    source that has actually written a health record -- with a state derived
    ONLY from persisted refresh evidence + current configuration. Nothing is
    ever shown healthy by default: no record means not configured or never
    refreshed, and an old success is STALE."""
    import os

    store = store or LocalJobStore()
    now = now or datetime.now(timezone.utc)
    environ = dict(os.environ) if environ is None else environ
    entries = _registry_entries(registry_path)
    health = store.read_source_health()
    identifiers = configured_identifier_counts(entries)

    profiles = list(all_profiles(registry_entries=entries))
    known = {profile.source_name for profile in profiles}
    for source_name in sorted(name for name, record in health.items() if name not in known and isinstance(record, dict)):
        # A source with real refresh evidence but no profile (e.g. a connector
        # run ad hoc via `ingest`) is shown, never silently dropped.
        profiles.append(generic_profile_for_connector_type(source_name))

    rows = []
    for profile in profiles:
        record = health.get(profile.source_name) if isinstance(health.get(profile.source_name), dict) else None
        configured = _is_configured(profile.source_name, identifiers, environ)
        state = _derive_source_state(profile, record, configured, now)
        deltas = record.get("delta_counts") if record and isinstance(record.get("delta_counts"), dict) else None
        rows.append(
            SourceHealthRow(
                source_name=profile.source_name,
                display_name=profile.display_name,
                state=state,
                last_attempted_refresh=record.get("checked_at") if record else None,
                last_successful_refresh=record.get("last_synced") if record else None,
                jobs_observed=record.get("jobs_seen") if record else None,
                # Per-refresh deltas only; legacy records without them show "-"
                # rather than a misleading cumulative total.
                new_jobs=deltas.get("NEW") if deltas else None,
                changed_jobs=deltas.get("CHANGED") if deltas else None,
                error=record.get("last_error") if record else None,
                warning=_combined_warning(record),
                coverage_limitation=profile.coverage_scope,
                automation_status=profile.automation_status,
                configured_identifiers=identifiers.get(profile.source_name),
                closed_last_refresh=record.get("closed_this_refresh") if record else None,
                incomplete_scopes=dict(record.get("incomplete_scopes") or {}) if record else {},
            )
        )
    return rows


def _derive_source_state(profile, record: dict[str, Any] | None, configured: bool, now: datetime) -> str:
    if profile.automation_status == MANUAL_FALLBACK_ONLY:
        return SOURCE_MANUAL_ONLY
    status = record.get("status") if record else None
    error = str(record.get("last_error") or "") if record else ""
    if record is not None and (record.get("not_configured") or (status == "auth_required" and any(marker in error for marker in _LEGACY_NOT_CONFIGURED_MARKERS))):
        return NOT_CONFIGURED
    if record is None:
        return ACTIVE_NOT_REFRESHED if configured else NOT_CONFIGURED
    if status in {"error", "auth_required"}:
        return SOURCE_FAILED
    checked_at = _parse_iso(record.get("checked_at"))
    if checked_at is None or now - checked_at > STALE_AFTER:
        return SOURCE_STALE
    if status == "partial":
        return SOURCE_PARTIAL_REFRESH
    if status == "live_api":
        return SOURCE_DEGRADED if record.get("warning") or record.get("closure_withheld") else ACTIVE_AND_REFRESHED
    return SOURCE_STALE


def _combined_warning(record: dict[str, Any] | None) -> str | None:
    if not record:
        return None
    parts = [record.get("warning"), record.get("closure_withheld")]
    incomplete = record.get("incomplete_scopes") or {}
    if incomplete:
        parts.append(f"{len(incomplete)} scope(s) not fully observed (no closure inferred for them): " + "; ".join(f"{scope}: {reason}" for scope, reason in list(incomplete.items())[:5]))
    text = " | ".join(str(part) for part in parts if part)
    return text or None


def _is_configured(source_name: str, identifiers: dict[str, int], environ: dict[str, str]) -> bool:
    if source_name in _ALWAYS_CONFIGURED:
        return True
    if source_name in _CREDENTIAL_ENV and not all(environ.get(name) for name in _CREDENTIAL_ENV[source_name]):
        return False
    if source_name == "adzuna":
        return True  # search source: credentials are its only configuration
    return identifiers.get(source_name, 0) > 0


def _registry_entries(registry_path: Path | str | None) -> list[dict[str, Any]]:
    path = Path(registry_path or "config/target_companies.json")
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CompanyHealthSummary:
    total: int
    counts: dict[str, int]
    coverage_metrics: dict[str, Any]
    watchlist: list[dict[str, Any]]


def build_company_health_view(registry_path: Path | str | None = None) -> CompanyHealthSummary:
    path = Path(registry_path or "config/target_companies.json")
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    results = classify_registry(entries)
    metrics = coverage_metrics(results)
    watchlist = manual_watchlist(results)
    return CompanyHealthSummary(total=metrics["total_companies"], counts=metrics["counts"], coverage_metrics=metrics, watchlist=watchlist)


# ---------------------------------------------------------------------------
# Section G: CV workbench
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CvWorkbenchView:
    job_id: str
    job: Job | None
    artifacts: list[GeneratedCVArtifact]  # oldest -> newest, every generation ever produced for this job
    latest: GeneratedCVArtifact | None
    application: Application | None
    # Explicit, never-omitted reminder: AUTO_PREPARE means the automated
    # safety checks passed, NOT "safe to submit without a human looking at
    # it" -- every generated CV is a draft for review, always.
    review_disclaimer: str = "Human review required before submitting, regardless of review status. AUTO_PREPARE means the automated checks passed -- it does not mean 'safe to submit unread'."


def build_cv_workbench(
    job_id: str,
    store: LocalJobStore | None = None,
    cv_store: CVArtifactStore | None = None,
    application_store: ApplicationStore | None = None,
) -> CvWorkbenchView:
    store = store or LocalJobStore()
    cv_store = cv_store or CVArtifactStore(store.root)
    application_store = application_store or ApplicationStore(store.root)

    jobs = {job.id: job for job in store.read_jobs()}
    job = jobs.get(job_id)
    artifacts = sorted(cv_store.for_job(job_id), key=lambda artifact: artifact.generated_at)
    latest = artifacts[-1] if artifacts else None
    application = latest_application_by_job(application_store.read_applications()).get(job_id)

    return CvWorkbenchView(job_id=job_id, job=job, artifacts=artifacts, latest=latest, application=application)


# ---------------------------------------------------------------------------
# Section J: manual watchlist workflow
# ---------------------------------------------------------------------------

WATCHLIST_ALWAYS_MANUAL = "ALWAYS_MANUAL"
WATCHLIST_PARTIALLY_MANUAL = "PARTIALLY_MANUAL"


@dataclass(frozen=True)
class WatchlistItem:
    target: str  # stable key: company_name for a company row, source_name for a source row
    display_name: str
    target_type: str  # "company" | "source"
    reason: str
    careers_url: str | None
    state: str
    last_checked_at: str | None
    next_check_due: str | None
    check_frequency_days: int
    check_result: str
    notes: str
    is_due: bool
    is_snoozed: bool


def _watchlist_item(target: str, display_name: str, target_type: str, reason: str, careers_url: str | None, state: str, check_record: dict[str, Any] | None, now: datetime) -> WatchlistItem:
    check_record = check_record or {}
    next_due_raw = check_record.get("next_check_due")
    next_due = datetime.fromisoformat(next_due_raw) if next_due_raw else None
    snoozed_until_raw = check_record.get("snoozed_until")
    snoozed_until = datetime.fromisoformat(snoozed_until_raw) if snoozed_until_raw else None
    is_snoozed = snoozed_until is not None and snoozed_until > now
    is_due = not is_snoozed and (next_due is None or next_due <= now)
    return WatchlistItem(
        target=target,
        display_name=display_name,
        target_type=target_type,
        reason=reason,
        careers_url=careers_url,
        state=state,
        last_checked_at=check_record.get("last_checked_at"),
        next_check_due=next_due_raw,
        check_frequency_days=check_record.get("check_frequency_days", 30),
        check_result=check_record.get("check_result", ""),
        notes=check_record.get("notes", ""),
        is_due=is_due,
        is_snoozed=is_snoozed,
    )


def build_manual_watchlist_view(
    store: LocalJobStore | None = None,
    registry_path: Path | str | None = None,
    watchlist_store: WatchlistStore | None = None,
    now: datetime | None = None,
) -> list[WatchlistItem]:
    """The single place answering "which of the manual-required companies and
    manual/partial-coverage sources do I still need to check, and when did I
    last check each one" -- so the user never has to remember which of 46+
    companies they already looked at. Combines `company_coverage.
    manual_watchlist` (MANUAL_REQUIRED/TEMPORARILY_FAILED companies) with the
    explicitly-tracked manual/partial sources (Trackr/Gradcracker/Bright
    Network always-manual; Prospects PARTIAL_COVERAGE needing periodic
    cross-check, never presented as if it were manual-only) plus this
    session's/previous sessions' recorded check state.
    """
    store = store or LocalJobStore()
    watchlist_store = watchlist_store or WatchlistStore(store.root)
    now = now or datetime.now(timezone.utc)
    path = Path(registry_path or "config/target_companies.json")
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    results = classify_registry(entries)
    company_rows = manual_watchlist(results)
    checks = watchlist_store.read_all()

    items = [
        _watchlist_item(row["company"], row["company"], "company", row["reason"], row.get("careers_url"), row["state"], checks.get(row["company"]), now)
        for row in company_rows
    ]
    for profile in all_profiles(registry_entries=entries):
        if profile.automation_status == MANUAL_FALLBACK_ONLY:
            items.append(_watchlist_item(profile.source_name, profile.display_name, "source", profile.manual_check_reason, None, WATCHLIST_ALWAYS_MANUAL, checks.get(profile.source_name), now))
        elif profile.automation_status == PARTIAL_COVERAGE:
            items.append(_watchlist_item(profile.source_name, profile.display_name, "source", profile.manual_check_reason, None, WATCHLIST_PARTIALLY_MANUAL, checks.get(profile.source_name), now))

    items.sort(key=lambda item: (not item.is_due, item.next_check_due or ""))
    return items
