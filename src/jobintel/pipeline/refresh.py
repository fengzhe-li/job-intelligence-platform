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
        _refresh_connector(connector, store, config, ConnectorQuery(location=where, limit=limit))
        for connector in connectors
    ]
    _write_wttj_registry_sync_metadata(registry_path, companies, results)
    store.write_refresh_summary(_refresh_summary_payload(results, started_at, datetime.now(timezone.utc), store))
    return results


def _refresh_connector(
    connector: JobSourceConnector,
    store: LocalJobStore,
    config: RankingConfig,
    query: ConnectorQuery,
) -> SourceRefreshResult:
    started_at = datetime.now(timezone.utc)
    try:
        raw_payloads = connector.fetch_jobs(query)
    except RuntimeError as exc:
        status = "auth_required" if connector.source_name == "welcome_to_the_jungle" and "WTTJ_API_KEY" in str(exc) else "error"
        result = SourceRefreshResult(connector.source_name, status, started_at=started_at, finished_at=datetime.now(timezone.utc), error=str(exc))
        store.write_source_health(connector.source_name, _health_payload(result, status_label=status))
        return result
    except Exception as exc:
        status = "auth_required" if connector.source_name == "welcome_to_the_jungle" and _is_auth_error(exc) else "error"
        result = SourceRefreshResult(connector.source_name, status, started_at=started_at, finished_at=datetime.now(timezone.utc), error=f"{type(exc).__name__}: {exc}")
        store.write_source_health(connector.source_name, _health_payload(result, status_label=status))
        return result

    jobs = _normalise_payloads(connector, raw_payloads, config)
    deduped = deduplicate_jobs(jobs)
    deduped = _annotate_observation_states(deduped, store.read_jobs(), connector.source_name)
    if raw_payloads:
        store.write_raw_payloads(raw_payloads)
    store.write_jobs(deduped, mark_missing_inactive=True, refreshed_sources={connector.source_name})
    active_jobs = _active_source_jobs(store.read_jobs(), connector.source_name)
    state_counts = _source_state_counts(store.read_jobs(), connector.source_name)
    result = SourceRefreshResult(
        source_name=connector.source_name,
        status="live_api",
        jobs_seen=len(raw_payloads),
        jobs_active=active_jobs,
        canonical_jobs_written=len(deduped),
        state_counts=state_counts,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
    )
    store.write_source_health(connector.source_name, _health_payload(result, status_label="live_api"))
    return result


def _normalise_payloads(connector: JobSourceConnector, raw_payloads: list[RawJobPayload], config: RankingConfig) -> list[Job]:
    return [enrich_job(connector.normalise(raw), config.graduation_year) for raw in raw_payloads]


def _active_source_jobs(jobs: list[Job], source_name: str) -> int:
    return sum(
        1
        for job in jobs
        if any(observation.source_name == source_name and observation.active for observation in job.source_observations)
    )


def _health_payload(result: SourceRefreshResult, status_label: str) -> dict[str, Any]:
    return {
        "source_name": result.source_name,
        "status": status_label,
        "last_synced": result.finished_at.isoformat() if result.status == "live_api" and result.finished_at else None,
        "jobs_seen": result.jobs_seen,
        "jobs_active": result.jobs_active,
        "state_counts": result.state_counts,
        "last_error": result.error,
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
        "sources": [
            {
                "source_name": result.source_name,
                "status": result.status,
                "jobs_seen": result.jobs_seen,
                "jobs_active": result.jobs_active,
                "canonical_jobs_written": result.canonical_jobs_written,
                "state_counts": result.state_counts,
                "error": result.error,
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
