from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.company_registry import TargetCompany, connectors_from_registry, load_company_registry
from jobintel.config import RankingConfig, load_ranking_config
from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload
from jobintel.dedup.v1 import deduplicate_jobs
from jobintel.models.job import Job, SourceObservation
from jobintel.storage.local_store import LocalJobStore

# A "live_api" success whose jobs_seen drops to zero, or falls to <20% of the
# source's own last healthy jobs_seen, is treated as suspicious rather than
# accepted at face value -- most likely a parser/layout change silently
# returning nothing rather than an actual mass closure. Surfaced as a
# `warning` on the health record, not just swallowed into "0 new jobs".
SUSPICIOUS_DROP_RATIO = 0.2


@dataclass(frozen=True)
class SourceRefreshResult:
    source_name: str
    status: str
    jobs_seen: int = 0
    jobs_active: int = 0
    canonical_jobs_written: int = 0
    state_counts: dict[str, int] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    error: str | None = None
    failed_identifiers: dict[str, str] = field(default_factory=dict)
    warning: str | None = None
    # What happened IN THIS refresh (not cumulative store state, which is
    # `state_counts`): NEW/CHANGED/UNCHANGED/REAPPEARED among observations
    # seen now, DISAPPEARED = observations closed by this refresh.
    delta_counts: dict[str, int] = field(default_factory=dict)
    closed_observations: list[tuple[str, str]] = field(default_factory=list)
    complete_scopes: list[str] = field(default_factory=list)
    incomplete_scopes: dict[str, str] = field(default_factory=dict)
    closure_withheld: str | None = None
    # True when the source simply isn't set up (missing optional credentials)
    # -- an expected state, never to be counted or displayed as a failure.
    not_configured: bool = False


def refresh_sources(
    registry_path: str | Path = "config/target_companies.json",
    store: LocalJobStore | None = None,
    config: RankingConfig | None = None,
    limit: int = 100,
    where: str = "United Kingdom",
) -> list[SourceRefreshResult]:
    store = store or LocalJobStore()
    config = config or load_ranking_config()
    started_at = datetime.now(timezone.utc)
    store.write_refresh_summary({"status": "running", "started_at": started_at.isoformat(), "finished_at": None, "sources": []})
    companies = load_company_registry(registry_path)
    connectors = connectors_from_registry(companies)
    results = [
        refresh_connector(connector, store, config, ConnectorQuery(location=where, limit=limit))
        for connector in connectors
    ]
    _write_wttj_registry_sync_metadata(registry_path, companies, results)
    store.write_refresh_summary(_refresh_summary_payload(results, started_at, datetime.now(timezone.utc), store))
    return results


def refresh_connector(
    connector: JobSourceConnector,
    store: LocalJobStore,
    config: RankingConfig,
    query: ConnectorQuery,
) -> SourceRefreshResult:
    """Runs one connector through the full source-health lifecycle: fetch,
    normalise, dedup, persist, and -- regardless of outcome, including a
    crash before any of that -- write a `source_health` record so this
    source's actual execution state and its recorded state can never
    disagree. This is the ONE place that does this for every connector this
    project runs, whether it came from the company/ATS registry
    (`refresh_sources`) or from the generic `ingest` CLI command (Adzuna,
    Prospects, or any ad-hoc combination of connectors) -- there is no
    separate, connector-specific health-tracking path anywhere else.
    """
    started_at = datetime.now(timezone.utc)
    previous_health = store.read_source_health().get(connector.source_name, {})
    try:
        snapshot = connector.fetch_snapshot(query)
    except RuntimeError as exc:
        # Missing/unconfigured credentials (WTTJ_API_KEY, ADZUNA_APP_ID/
        # ADZUNA_APP_KEY) are a distinct, explicit configuration failure --
        # never silently folded into a generic "error", and never allowed to
        # crash without a health record at all.
        not_configured = _is_missing_credentials_message(connector.source_name, str(exc))
        status = "auth_required" if not_configured else "error"
        result = SourceRefreshResult(connector.source_name, status, started_at=started_at, finished_at=datetime.now(timezone.utc), error=str(exc), not_configured=not_configured)
        store.write_source_health(connector.source_name, _health_payload(result, status_label=status, previous=previous_health))
        return result
    except Exception as exc:
        status = "auth_required" if _is_auth_error(exc) and connector.source_name in _AUTH_AWARE_SOURCES else "error"
        result = SourceRefreshResult(connector.source_name, status, started_at=started_at, finished_at=datetime.now(timezone.utc), error=f"{type(exc).__name__}: {exc}")
        store.write_source_health(connector.source_name, _health_payload(result, status_label=status, previous=previous_health))
        return result

    failed_identifiers = dict(snapshot.failures)
    if failed_identifiers and not snapshot.payloads and not snapshot.complete_scopes:
        # Every configured identifier failed -- this is a total failure, not
        # a partial one, and must be reported with the same urgency as any
        # other total fetch failure (not softened to "partial").
        error_summary = "; ".join(f"{identifier}: {message}" for identifier, message in failed_identifiers.items())
        # Some connectors (Adzuna's own per-page failure isolation is the
        # motivating case) never let a raw exception reach the `except
        # Exception` branch above -- every failure, auth included, is
        # already stringified into `failures`. Check those messages too, so a
        # real 401/403 from the API itself is still reported as
        # `auth_required`, not folded into a generic `error`.
        status = "auth_required" if connector.source_name in _AUTH_AWARE_SOURCES and any(_is_auth_error_message(message) for message in failed_identifiers.values()) else "error"
        result = SourceRefreshResult(connector.source_name, status, started_at=started_at, finished_at=datetime.now(timezone.utc), error=error_summary, failed_identifiers=failed_identifiers, incomplete_scopes=dict(snapshot.incomplete_scopes))
        store.write_source_health(connector.source_name, _health_payload(result, status_label=status, previous=previous_health))
        return result

    # Some identifiers may have failed while others succeeded: still write
    # what succeeded. Closure is decided per scope by `snapshot.
    # complete_scopes` alone -- a failed identifier's scope is never in it,
    # so its previously-seen jobs can't be closed by this refresh.
    raw_payloads = snapshot.payloads
    is_partial = bool(failed_identifiers)
    existing_jobs = store.read_jobs()
    jobs = _normalise_payloads(connector, raw_payloads, config)
    deduped = deduplicate_jobs(jobs)
    deduped = _annotate_observation_states(deduped, existing_jobs, connector.source_name)
    if raw_payloads:
        store.write_raw_payloads(raw_payloads)
    warning = None if is_partial else _suspicious_drop_warning(connector.source_name, len(raw_payloads), previous_health)
    complete_scopes = frozenset(snapshot.complete_scopes)
    closure_withheld = None
    if warning and complete_scopes:
        # A sudden collapse in results is far more likely a parser/API change
        # than a real mass closure -- never let it close jobs this cycle.
        closure_withheld = f"closure inference withheld for {len(complete_scopes)} complete scope(s): {warning}"
        complete_scopes = frozenset()
    closed = store.write_snapshot(deduped, connector.source_name, complete_scopes, connector.closure_scope)
    after_jobs = store.read_jobs()
    active_jobs = _active_source_jobs(after_jobs, connector.source_name)
    state_counts = _source_state_counts(after_jobs, connector.source_name)
    delta_counts = _delta_counts(deduped, connector.source_name, len(closed))
    status = "partial" if is_partial else "live_api"
    error_summary = "; ".join(f"{identifier}: {message}" for identifier, message in failed_identifiers.items()) or None
    result = SourceRefreshResult(
        source_name=connector.source_name,
        status=status,
        jobs_seen=len(raw_payloads),
        jobs_active=active_jobs,
        canonical_jobs_written=len(deduped),
        state_counts=state_counts,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        error=error_summary,
        failed_identifiers=failed_identifiers,
        warning=warning,
        delta_counts=delta_counts,
        closed_observations=closed,
        complete_scopes=sorted(complete_scopes),
        incomplete_scopes=dict(snapshot.incomplete_scopes),
        closure_withheld=closure_withheld,
    )
    store.write_source_health(connector.source_name, _health_payload(result, status_label=status, previous=previous_health))
    return result


def _delta_counts(incoming: list[Job], source_name: str, closed: int) -> dict[str, int]:
    counts = {state: 0 for state in ("NEW", "CHANGED", "UNCHANGED", "DISAPPEARED", "REAPPEARED")}
    for job in incoming:
        for observation in job.source_observations:
            if observation.source_name == source_name and observation.latest_observed_state in counts:
                counts[observation.latest_observed_state] += 1
    counts["DISAPPEARED"] = closed
    return counts


def _suspicious_drop_warning(source_name: str, jobs_seen: int, previous_health: dict[str, Any]) -> str | None:
    if not isinstance(previous_health, dict):
        return None
    previous_jobs_seen = previous_health.get("last_successful_jobs_seen")
    if not isinstance(previous_jobs_seen, int) or previous_jobs_seen < 5:
        return None
    if jobs_seen == 0:
        return f"{source_name} returned 0 jobs this refresh after previously returning {previous_jobs_seen} -- likely a parser/layout change, not a real mass closure. Treat with suspicion."
    if jobs_seen < previous_jobs_seen * SUSPICIOUS_DROP_RATIO:
        return f"{source_name} returned {jobs_seen} jobs, down from {previous_jobs_seen} last successful refresh (>80% drop) -- verify this is real before trusting it."
    return None


def _normalise_payloads(connector: JobSourceConnector, raw_payloads: list[RawJobPayload], config: RankingConfig) -> list[Job]:
    return [enrich_job(connector.normalise(raw), config.candidate_graduation_year) for raw in raw_payloads]


def _active_source_jobs(jobs: list[Job], source_name: str) -> int:
    return sum(
        1
        for job in jobs
        if any(observation.source_name == source_name and observation.active for observation in job.source_observations)
    )


def _health_payload(result: SourceRefreshResult, status_label: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    # `last_synced` (and `last_successful_jobs_seen`) must survive a failed or
    # partial refresh -- CRITICAL: without this, a source that fails today would
    # show "last synced: never" even though it worked fine yesterday, which is
    # exactly the "failure silently looks like 0 new jobs" problem this whole
    # health record exists to prevent.
    previous = previous if isinstance(previous, dict) else {}
    fully_successful = status_label == "live_api"
    last_synced = result.finished_at.isoformat() if fully_successful and result.finished_at else previous.get("last_synced")
    last_successful_jobs_seen = result.jobs_seen if fully_successful else previous.get("last_successful_jobs_seen")
    return {
        "source_name": result.source_name,
        "status": status_label,
        "last_synced": last_synced,
        "last_successful_jobs_seen": last_successful_jobs_seen,
        "jobs_seen": result.jobs_seen,
        "jobs_active": result.jobs_active,
        "state_counts": result.state_counts,
        "last_error": result.error,
        "failed_identifiers": result.failed_identifiers,
        "warning": result.warning,
        "delta_counts": result.delta_counts,
        "closed_this_refresh": len(result.closed_observations),
        "complete_scopes": result.complete_scopes,
        "incomplete_scopes": result.incomplete_scopes,
        "closure_withheld": result.closure_withheld,
        "not_configured": result.not_configured,
        "checked_at": (result.finished_at or datetime.now(timezone.utc)).isoformat(),
    }


def _refresh_summary_payload(results: list[SourceRefreshResult], started_at: datetime, finished_at: datetime, store: LocalJobStore) -> dict[str, Any]:
    aggregate_counts = _aggregate_state_counts(results)
    return {
        "status": "finished",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "total_jobs_seen": sum(result.jobs_seen for result in results),
        "state_counts": aggregate_counts,
        "delta_counts": _aggregate_delta_counts(results),
        "sources": [
            {
                "source_name": result.source_name,
                "status": result.status,
                "jobs_seen": result.jobs_seen,
                "jobs_active": result.jobs_active,
                "canonical_jobs_written": result.canonical_jobs_written,
                "state_counts": result.state_counts,
                "error": result.error,
                "failed_identifiers": result.failed_identifiers,
                "warning": result.warning,
                "delta_counts": result.delta_counts,
                "closed_this_refresh": len(result.closed_observations),
                "incomplete_scopes": result.incomplete_scopes,
                "closure_withheld": result.closure_withheld,
                "started_at": result.started_at.isoformat(),
                "finished_at": result.finished_at.isoformat() if result.finished_at else None,
            }
            for result in results
        ],
        "active_jobs": sum(1 for job in store.read_jobs() if any(observation.active for observation in job.source_observations)),
    }


def _aggregate_state_counts(results: list[SourceRefreshResult]) -> dict[str, int]:
    counts = {state: 0 for state in ("NEW", "CHANGED", "UNCHANGED", "DISAPPEARED", "REAPPEARED")}
    for result in results:
        for state, value in result.state_counts.items():
            counts[state] = counts.get(state, 0) + int(value)
    return counts


def _aggregate_delta_counts(results: list[SourceRefreshResult]) -> dict[str, int]:
    counts = {state: 0 for state in ("NEW", "CHANGED", "UNCHANGED", "DISAPPEARED", "REAPPEARED")}
    for result in results:
        for state, value in result.delta_counts.items():
            counts[state] = counts.get(state, 0) + int(value)
    return counts


def _source_state_counts(jobs: list[Job], source_name: str) -> dict[str, int]:
    counts = {state: 0 for state in ("NEW", "CHANGED", "UNCHANGED", "DISAPPEARED", "REAPPEARED")}
    for job in jobs:
        for observation in job.source_observations:
            if observation.source_name == source_name:
                state = observation.latest_observed_state
                if state in counts:
                    counts[state] += 1
    return counts


def _annotate_observation_states(incoming: list[Job], existing: list[Job], source_name: str) -> list[Job]:
    previous = {
        (observation.source_name, observation.source_job_id): (job, observation)
        for job in existing
        for observation in job.source_observations
        if observation.source_name == source_name
    }
    annotated: list[Job] = []
    for job in incoming:
        observations = []
        for observation in job.source_observations:
            if observation.source_name != source_name:
                observations.append(observation)
                continue
            previous_item = previous.get((observation.source_name, observation.source_job_id))
            state = "NEW"
            if previous_item is not None:
                previous_job, previous_observation = previous_item
                if not previous_observation.active:
                    state = "REAPPEARED"
                elif _field_hash(previous_job, previous_observation) != _field_hash(job, observation):
                    state = "CHANGED"
                else:
                    state = "UNCHANGED"
            observations.append(replace(observation, active=True, latest_observed_state=state))
        annotated.append(replace(job, source_observations=observations))
    return annotated


def _field_hash(job: Job, observation: SourceObservation) -> str:
    payload = {
        "title": job.title,
        "company": job.company,
        "description": job.description,
        "raw_location": job.raw_location,
        "locations": [
            {
                "city": location.city,
                "country": location.country,
                "region": location.region,
                "work_mode": location.work_mode.value,
                "raw": location.raw,
            }
            for location in job.locations
        ],
        "posted_at": observation.posted_at.isoformat() if observation.posted_at else None,
        "raw_description": observation.raw_description,
        "canonical_application_url": observation.canonical_application_url,
        "original_url": observation.original_url,
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _is_auth_error(exc: Exception) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 403}


def _is_auth_error_message(message: str) -> bool:
    """Text-based counterpart to `_is_auth_error` for connectors (Adzuna's
    per-page isolation is the motivating case) that stringify every failure
    into `PartialFetchError.failures` rather than letting an HTTPError
    propagate -- by the time it reaches here it's already `"HTTPError: HTTP
    Error 401: Unauthorized"`, not an exception object.
    """
    return "401" in message or "403" in message or "unauthorized" in message.casefold() or "forbidden" in message.casefold()


# Sources whose connector can distinguish "missing/invalid credentials" from
# a generic failure -- an HTTP 401/403 from one of these is reported as
# `auth_required`, never folded into a generic `error`. Extending this set is
# the ONLY change needed to give a new credentialed source the same explicit
# configuration-failure visibility Welcome to the Jungle already had -- no
# source-specific branch required.
_AUTH_AWARE_SOURCES = {"welcome_to_the_jungle", "adzuna"}

# Substrings each credentialed connector's own RuntimeError(...) message is
# known to contain when required credentials are missing -- matched against
# `connector.source_name` so a missing-credentials RuntimeError is reported
# as `auth_required` (an explicit configuration failure) rather than a
# generic `error`, for any source in this table, not just one hardcoded name.
_MISSING_CREDENTIALS_MARKERS = {
    "welcome_to_the_jungle": "WTTJ_API_KEY",
    "adzuna": "ADZUNA_APP_ID",
}


def _is_missing_credentials_message(source_name: str, message: str) -> bool:
    marker = _MISSING_CREDENTIALS_MARKERS.get(source_name)
    return marker is not None and marker in message


def _write_wttj_registry_sync_metadata(registry_path: str | Path, companies: list[TargetCompany], results: list[SourceRefreshResult]) -> None:
    wttj = next((result for result in results if result.source_name == "welcome_to_the_jungle"), None)
    if wttj is None:
        return
    path = Path(registry_path)
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return
    updated = []
    for entry in payload:
        if not isinstance(entry, dict):
            updated.append(entry)
            continue
        connector_type = entry.get("connector_type")
        has_wttj_ref = bool(entry.get("welcome_to_the_jungle_organization_reference") or entry.get("wttj_organization_reference") or entry.get("organization_reference"))
        if connector_type == "welcome_to_the_jungle" or has_wttj_ref:
            entry = {
                **entry,
                "last_successful_sync": wttj.finished_at.isoformat() if wttj.status == "live_api" and wttj.finished_at else entry.get("last_successful_sync"),
                "last_error": wttj.error,
                "jobs_seen": wttj.jobs_seen,
                "jobs_active": wttj.jobs_active,
                "verification_status": _wttj_registry_status(wttj, entry.get("verification_status", "pending_verification")),
            }
        updated.append(entry)
    path.write_text(json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _wttj_registry_status(result: SourceRefreshResult, current: str) -> str:
    if result.status == "live_api":
        return "verified"
    if result.status == "auth_required":
        return "auth_required"
    if result.status == "error":
        return "sync_error"
    return current
