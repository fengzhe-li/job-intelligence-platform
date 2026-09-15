from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from difflib import SequenceMatcher

from jobintel.models.job import Job, SourceObservation

DIRECT_SOURCES = {"company", "greenhouse", "lever", "ashby", "workable", "smartrecruiters"}


def deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    canonical: list[Job] = []
    for job in jobs:
        match = _find_match(canonical, job)
        if match is None:
            canonical.append(job)
        else:
            merged = _merge(match, job)
            canonical[canonical.index(match)] = merged
    return canonical


def _find_match(candidates: list[Job], job: Job) -> Job | None:
    for candidate in candidates:
        if _same_source_id(candidate, job):
            return candidate
        if _same_application_url(candidate, job):
            return candidate
        if _same_company_title_location(candidate, job):
            return candidate
        if _fuzzy_company_title(candidate, job):
            return candidate
    return None


def _same_source_id(a: Job, b: Job) -> bool:
    ids_a = {(obs.source_name, obs.source_job_id) for obs in a.source_observations}
    ids_b = {(obs.source_name, obs.source_job_id) for obs in b.source_observations}
    return bool(ids_a & ids_b)


def _same_application_url(a: Job, b: Job) -> bool:
    urls_a = {_canonical_url(obs.canonical_application_url) for obs in a.source_observations}
    urls_b = {_canonical_url(obs.canonical_application_url) for obs in b.source_observations}
    return bool((urls_a - {""}) & (urls_b - {""}))


def _same_company_title_location(a: Job, b: Job) -> bool:
    if _norm(a.company) != _norm(b.company):
        return False
    if _norm_title(a.title) != _norm_title(b.title):
        return False
    return bool(_location_keys(a) & _location_keys(b))


def _fuzzy_company_title(a: Job, b: Job) -> bool:
    if _norm(a.company) != _norm(b.company):
        return False
    if not (_location_keys(a) & _location_keys(b)):
        return False
    return SequenceMatcher(None, _norm_title(a.title), _norm_title(b.title)).ratio() >= 0.88


def _merge(a: Job, b: Job) -> Job:
    observations = _merge_observations(a.source_observations + b.source_observations)
    preferred = _preferred_job(a, b)
    locations = a.locations + [location for location in b.locations if location not in a.locations]
    return replace(
        preferred,
        id=a.id,
        locations=locations,
        source_observations=observations,
        salary=preferred.salary or a.salary or b.salary,
        raw_location=preferred.raw_location or a.raw_location or b.raw_location,
    )


def _merge_observations(observations: list[SourceObservation]) -> list[SourceObservation]:
    merged: dict[tuple[str, str], SourceObservation] = {}
    for observation in observations:
        key = (observation.source_name, observation.source_job_id)
        if key not in merged:
            merged[key] = observation
            continue
        existing = merged[key]
        latest = observation if observation.last_seen_at >= existing.last_seen_at else existing
        merged[key] = replace(
            latest,
            first_seen_at=min(existing.first_seen_at, observation.first_seen_at),
            last_seen_at=max(existing.last_seen_at, observation.last_seen_at),
            active=latest.active,
            latest_observed_state=latest.latest_observed_state,
        )
    return sorted(merged.values(), key=lambda item: (item.source_name not in DIRECT_SOURCES, item.source_name))


def _preferred_job(a: Job, b: Job) -> Job:
    a_score = _directness_score(a)
    b_score = _directness_score(b)
    if a_score == b_score:
        return b if _latest_seen(b) > _latest_seen(a) else a
    return b if b_score > a_score else a


def _directness_score(job: Job) -> int:
    return max((2 if obs.source_name in DIRECT_SOURCES else 1 for obs in job.source_observations), default=0)


def _latest_seen(job: Job):
    return max((obs.last_seen_at for obs in job.source_observations), default=datetime.min.replace(tzinfo=timezone.utc))


def _location_keys(job: Job) -> set[str]:
    return {f"{_norm(location.city or '')}:{_norm(location.country)}" for location in job.locations}


def _canonical_url(url: str) -> str:
    return re.sub(r"[?#].*$", "", url or "").rstrip("/").casefold()


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _norm_title(value: str) -> str:
    value = re.sub(r"\b(graduate|junior|senior|mid|level|programme|program)\b", "", value.casefold())
    return re.sub(r"[^a-z0-9]+", "", value)
