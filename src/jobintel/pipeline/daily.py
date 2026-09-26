from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobintel.company_discovery import observe_companies_from_jobs
from jobintel.config import RankingConfig, load_ranking_config
from jobintel.connectors.adzuna import ADZUNA_GRADUATE_KEYWORDS, AdzunaConnector
from jobintel.connectors.base import ConnectorQuery
from jobintel.connectors.prospects import ProspectsConnector
from jobintel.dashboard.operations import build_action_queue, build_manual_watchlist_view
from jobintel.github_sync import resolve_github_username, sync_repositories
from jobintel.models.job import Job
from jobintel.pipeline.ingestion import DEFAULT_TECH_KEYWORDS
from jobintel.pipeline.refresh import SourceRefreshResult, refresh_connector, refresh_sources
from jobintel.profile_ingestion import load_candidate_profile
from jobintel.source_coverage import MANUAL_FALLBACK_ONLY, all_profiles
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.company_discovery_store import CompanyDiscoveryStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.storage.watchlist_store import WatchlistStore

# Optional incremental GitHub evidence sync runs only when this is set --
# unset is "not configured", never a failure.
GITHUB_USERNAME_ENV = "JOBINTEL_GITHUB_USERNAME"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

# Per-source outcome of one daily refresh -- the distinctions the dashboard
# must never blur: a successful empty result is not a failure, a failure is
# not "0 new jobs", and an unconfigured optional source is neither.
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_SUCCEEDED_ZERO = "succeeded_zero_results"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILED = "failed"
OUTCOME_NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class SourceOutcome:
    source_name: str
    outcome: str
    status: str
    jobs_seen: int
    new: int
    changed: int
    closed: int
    error: str | None = None
    warning: str | None = None
    closure_withheld: str | None = None
    incomplete_scopes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StageOutcome:
    """An auxiliary daily stage (company discovery, GitHub sync) -- recorded
    separately so its failure can never be mistaken for a job-source result
    or block job discovery."""

    name: str
    outcome: str
    detail: str = ""


@dataclass(frozen=True)
class DailyRefreshReport:
    started_at: datetime
    finished_at: datetime
    sources_succeeded: list[str] = field(default_factory=list)
    sources_partial: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    sources_not_configured: list[str] = field(default_factory=list)
    # Deltas for THIS refresh only (never cumulative store totals):
    # canonical jobs that did not exist before this refresh ...
    new_jobs: int = 0
    # ... source observations that changed content ...
    changed_jobs: int = 0
    # ... and canonical jobs that were active before and fully closed after.
    closed_jobs: int = 0
    closed_observations: int = 0
    high_priority_jobs: int = 0
    manual_checks_due: int = 0
    source_results: list[SourceRefreshResult] = field(default_factory=list)
    status: str = "complete"
    source_outcomes: list[SourceOutcome] = field(default_factory=list)
    stages: list[StageOutcome] = field(default_factory=list)
    manual_only_sources: list[str] = field(default_factory=list)
    new_job_ids: list[str] = field(default_factory=list)
    closed_job_ids: list[str] = field(default_factory=list)

    @property
    def sources_zero_results(self) -> list[str]:
        return [item.source_name for item in self.source_outcomes if item.outcome == OUTCOME_SUCCEEDED_ZERO]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("source_results")  # full per-source detail lives in source_outcomes
        payload["sources_zero_results"] = self.sources_zero_results
        return payload


def run_daily_refresh(
    store: LocalJobStore | None = None,
    config: RankingConfig | None = None,
    registry_path: Path | str = "config/target_companies.json",
    where: str = "United Kingdom",
    limit: int = 100,
) -> DailyRefreshReport:
    """Orchestrates every AUTOMATED discovery mechanism into one daily command
    and persists the result (latest + append-only history) so the dashboard
    reports what actually happened rather than recomputing its own numbers.

    Order and isolation:
    1. Registry direct-ATS connectors (incl. Workday, and WTTJ when
       WTTJ_API_KEY is set) via `refresh_sources`.
    2. Adzuna (when ADZUNA_APP_ID/KEY are set; otherwise recorded as
       not_configured) and Prospects (PARTIAL_COVERAGE -- can never close
       jobs). Every connector goes through the health-tracked
       `refresh_connector`; one failing never stops the others, and closure is
       only ever inferred from a proven-complete snapshot scope.
    3. Company discovery from newly observed non-registry jobs (offline,
       append-only log; never edits the registry or its manual exclusions).
    4. Optional incremental GitHub evidence sync -- last, and isolated, so
       GitHub being down can never block or taint job discovery.

    Manual-only sources (Trackr/Gradcracker/Bright Network) are never run --
    nothing automated exists for them; they appear in `manual_only_sources`
    and the watchlist, never as "attempted".
    """
    store = store or LocalJobStore()
    config = config or load_ranking_config()
    started_at = datetime.now(timezone.utc)
    before = {job.id: _is_active(job) for job in store.read_jobs()}

    registry_results = refresh_sources(registry_path=registry_path, store=store, config=config, limit=limit, where=where)
    query = ConnectorQuery(keywords=DEFAULT_TECH_KEYWORDS, location=where, limit=limit)
    extra_results = [
        refresh_connector(AdzunaConnector(os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")), store, config, replace(query, keywords=ADZUNA_GRADUATE_KEYWORDS)),
        refresh_connector(ProspectsConnector(), store, config, query),
    ]
    all_results = list(registry_results) + extra_results
    outcomes = [_outcome(result) for result in all_results]

    after_jobs = store.read_jobs()
    new_job_ids = sorted(job.id for job in after_jobs if job.id not in before)
    closed_job_ids = sorted(job.id for job in after_jobs if before.get(job.id) and not _is_active(job))

    stages = [_company_discovery_stage(store, registry_path, [job for job in after_jobs if job.id in set(new_job_ids)])]
    stages.append(_github_sync_stage(store))

    now = datetime.now(timezone.utc)
    action_queue = build_action_queue(store, ApplicationStore(store.root), CVArtifactStore(store.root), config, now, limit=10_000)
    high_priority_jobs = len([item for item in action_queue if item.priority_score >= 4.0])
    # Reuses the SAME unified watchlist view the dashboard shows, so this
    # count can never drift from what the watchlist page lists as due.
    watchlist_items = build_manual_watchlist_view(store, registry_path=registry_path, watchlist_store=WatchlistStore(store.root), now=now)
    manual_checks_due_count = len([item for item in watchlist_items if item.is_due])

    by_outcome = lambda *wanted: [item.source_name for item in outcomes if item.outcome in wanted]  # noqa: E731
    failed = by_outcome(OUTCOME_FAILED)
    partial = by_outcome(OUTCOME_PARTIAL)
    succeeded = by_outcome(OUTCOME_SUCCEEDED, OUTCOME_SUCCEEDED_ZERO)
    status = "failed" if failed and not succeeded and not partial else ("partial" if failed or partial else "complete")

    report = DailyRefreshReport(
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        sources_succeeded=succeeded,
        sources_partial=partial,
        sources_failed=failed,
        sources_not_configured=by_outcome(OUTCOME_NOT_CONFIGURED),
        new_jobs=len(new_job_ids),
        changed_jobs=sum(item.changed for item in outcomes),
        closed_jobs=len(closed_job_ids),
        closed_observations=sum(item.closed for item in outcomes),
        high_priority_jobs=high_priority_jobs,
        manual_checks_due=manual_checks_due_count,
        source_results=all_results,
        status=status,
        source_outcomes=outcomes,
        stages=stages,
        manual_only_sources=[profile.source_name for profile in all_profiles(registry_path=registry_path) if profile.automation_status == MANUAL_FALLBACK_ONLY],
        new_job_ids=new_job_ids,
        closed_job_ids=closed_job_ids,
    )
    store.write_daily_report(report.to_dict())
    return report


def _outcome(result: SourceRefreshResult) -> SourceOutcome:
    if result.not_configured:
        outcome = OUTCOME_NOT_CONFIGURED
    elif result.status in {"error", "auth_required"}:
        outcome = OUTCOME_FAILED
    elif result.status == "partial":
        outcome = OUTCOME_PARTIAL
    elif result.jobs_seen == 0:
        outcome = OUTCOME_SUCCEEDED_ZERO
    else:
        outcome = OUTCOME_SUCCEEDED
    return SourceOutcome(
        source_name=result.source_name,
        outcome=outcome,
        status=result.status,
        jobs_seen=result.jobs_seen,
        new=result.delta_counts.get("NEW", 0),
        changed=result.delta_counts.get("CHANGED", 0),
        closed=len(result.closed_observations),
        error=result.error,
        warning=result.warning,
        closure_withheld=result.closure_withheld,
        incomplete_scopes=dict(result.incomplete_scopes),
    )


def _company_discovery_stage(store: LocalJobStore, registry_path: Path | str, new_jobs: list[Job]) -> StageOutcome:
    try:
        path = Path(registry_path)
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        observed = observe_companies_from_jobs(new_jobs, entries, CompanyDiscoveryStore(store.root))
    except Exception as exc:  # never let an auxiliary stage mask job results
        return StageOutcome("company_discovery", OUTCOME_FAILED, f"{type(exc).__name__}: {exc}")
    names = ", ".join(item.company_name for item in observed[:10])
    return StageOutcome("company_discovery", OUTCOME_SUCCEEDED, f"{len(observed)} new candidate company(ies) logged for review{': ' + names if names else ''} (registry unchanged)")


def _github_sync_stage(store: LocalJobStore) -> StageOutcome:
    profile = load_candidate_profile(store.root)
    username = resolve_github_username(candidate=profile, store_root=store.root)
    if not username:
        return StageOutcome(
            "github_sync",
            OUTCOME_NOT_CONFIGURED,
            f"set {GITHUB_USERNAME_ENV} or candidate profile github_url to enable incremental GitHub evidence sync",
        )
    if profile is None:
        # A sync rebuilds the candidate profile; without an existing one it
        # would be created under a placeholder name that then lands on CVs.
        return StageOutcome("github_sync", OUTCOME_NOT_CONFIGURED, "no candidate profile yet -- run profile-rebuild once before enabling daily GitHub sync")
    try:
        result = sync_repositories(store.root, username, token=os.getenv(GITHUB_TOKEN_ENV), name=profile.name, graduation_year=profile.graduation_year)
    except Exception as exc:
        return StageOutcome("github_sync", OUTCOME_FAILED, f"{type(exc).__name__}: {exc}")
    return StageOutcome("github_sync", OUTCOME_SUCCEEDED, f"{result.repositories_synced} repo(s) synced, {result.repositories_unchanged} unchanged")


def _is_active(job: Job) -> bool:
    return any(observation.active for observation in job.source_observations)
