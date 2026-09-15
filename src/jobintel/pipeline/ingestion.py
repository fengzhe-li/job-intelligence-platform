from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.config import RankingConfig, default_ranking_config
from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload
from jobintel.dedup.v1 import deduplicate_jobs
from jobintel.fixtures.sample_data import sample_candidate
from jobintel.matching.matcher import MatchResult, match_job
from jobintel.models.candidate import CandidateProfile
from jobintel.models.job import Job
from jobintel.storage.local_store import LocalJobStore


DEFAULT_TECH_KEYWORDS = (
    "software engineer",
    "backend",
    "frontend",
    "full-stack",
    "python",
    "data engineer",
    "data platform",
    "cloud engineer",
    "platform engineer",
    "devops",
    "sre",
    "machine learning",
    "network engineer",
    "network software",
    "telecommunications",
    "iot",
    "embedded",
)


@dataclass(frozen=True)
class RankedJob:
    job: Job
    match: MatchResult


def ingest_from_connectors(
    connectors: list[JobSourceConnector],
    query: ConnectorQuery | None = None,
    store: LocalJobStore | None = None,
    config: RankingConfig | None = None,
    mark_missing_inactive: bool = True,
    refreshed_sources: set[str] | None = None,
) -> list[Job]:
    query = query or ConnectorQuery(keywords=DEFAULT_TECH_KEYWORDS, location="United Kingdom", limit=50)
    store = store or LocalJobStore()
    config = config or default_ranking_config()
    raw_payloads: list[RawJobPayload] = []
    for connector in connectors:
        raw_payloads.extend(connector.fetch_changed_jobs(query))
    if raw_payloads:
        store.write_raw_payloads(raw_payloads)
    jobs = []
    for raw in raw_payloads:
        connector = next(item for item in connectors if item.source_name == raw.source_name)
        jobs.append(enrich_job(connector.normalise(raw), config.graduation_year))
    deduped = deduplicate_jobs(jobs)
    store.write_jobs(deduped, mark_missing_inactive=mark_missing_inactive, refreshed_sources=refreshed_sources)
    return deduped


def rank_jobs(
    jobs: list[Job],
    candidate: CandidateProfile | None = None,
    config: RankingConfig | None = None,
    now: datetime | None = None,
) -> list[RankedJob]:
    candidate = candidate or sample_candidate()
    config = config or default_ranking_config()
    enriched = [enrich_job(job, config.graduation_year) for job in jobs]
    results = [RankedJob(job=job, match=match_job(candidate, job, config, now)) for job in enriched]
    return sorted(results, key=lambda item: item.match.overall_priority, reverse=True)
