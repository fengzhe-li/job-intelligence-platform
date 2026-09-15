from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from jobintel.analysis.occupation_filter import is_excluded_occupation
from jobintel.company_registry import load_company_registry, registry_summary
from jobintel.config import RankingConfig, default_ranking_config
from jobintel.models.job import Job
from jobintel.storage.local_store import LocalJobStore


@dataclass(frozen=True)
class ValidationReport:
    companies_configured: int
    working_source_connections: int
    live_jobs_fetched: int
    relevant_technical_jobs_retained: int
    jobs_excluded_as_unrelated: int
    duplicate_jobs_merged: int
    distribution_by_role_track: dict[str, int]
    sponsorship_state_distribution: dict[str, int]
    graduation_year_state_distribution: dict[str, int]
    london_vs_non_london_uk_distribution: dict[str, int]
    cross_track_examples: list[str]


def build_validation_report(
    store: LocalJobStore | None = None,
    registry_path: Path | str = "config/target_companies.json",
    config: RankingConfig | None = None,
) -> ValidationReport:
    store = store or LocalJobStore()
    config = config or default_ranking_config()
    jobs = store.read_jobs()
    companies = load_company_registry(registry_path)
    summary = registry_summary(companies)
    excluded_count = sum(1 for job in jobs if is_excluded_occupation(job.title, job.description, config)[0])
    role_counts: Counter[str] = Counter()
    sponsorship_counts: Counter[str] = Counter()
    graduation_counts: Counter[str] = Counter()
    location_counts: Counter[str] = Counter()
    cross_track_examples: list[str] = []
    source_observation_count = 0

    for job in jobs:
        source_observation_count += len(job.source_observations)
        for score in job.role_track_profile.scores:
            role_counts[score.track.value] += 1
        sponsorship_counts[job.sponsorship.state.value if job.sponsorship else "unknown"] += 1
        graduation_counts[job.graduation_year.state.value if job.graduation_year else "unknown"] += 1
        has_london = any(location.is_london and location.is_uk for location in job.locations)
        has_uk = any(location.is_uk for location in job.locations)
        if has_london:
            location_counts["london"] += 1
        elif has_uk:
            location_counts["non_london_uk"] += 1
        else:
            location_counts["outside_uk"] += 1
        if len(job.role_track_profile.scores) > 1 and len(cross_track_examples) < 5:
            tracks = ", ".join(score.track.value for score in job.role_track_profile.scores[:3])
            cross_track_examples.append(f"{job.title} at {job.company}: {tracks}")

    duplicate_jobs_merged = max(source_observation_count - len(jobs), 0)
    return ValidationReport(
        companies_configured=summary["companies_configured"],
        working_source_connections=summary["greenhouse_connections"] + summary["lever_connections"],
        live_jobs_fetched=len(jobs),
        relevant_technical_jobs_retained=max(len(jobs) - excluded_count, 0),
        jobs_excluded_as_unrelated=excluded_count,
        duplicate_jobs_merged=duplicate_jobs_merged,
        distribution_by_role_track=dict(role_counts),
        sponsorship_state_distribution=dict(sponsorship_counts),
        graduation_year_state_distribution=dict(graduation_counts),
        london_vs_non_london_uk_distribution=dict(location_counts),
        cross_track_examples=cross_track_examples,
    )


def write_validation_report(report: ValidationReport, path: Path | str = "docs/PHASE3_VALIDATION.md") -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Phase 3 Validation",
        "",
        "Measured from the local processed job store. If no live ingest has been run, live job counts remain zero.",
        "",
        f"- companies configured: {report.companies_configured}",
        f"- working source connections configured: {report.working_source_connections}",
        f"- live jobs fetched: {report.live_jobs_fetched}",
        f"- relevant technical jobs retained: {report.relevant_technical_jobs_retained}",
        f"- jobs excluded as unrelated: {report.jobs_excluded_as_unrelated}",
        f"- duplicate jobs merged: {report.duplicate_jobs_merged}",
        "",
        "## Role Track Distribution",
        *[f"- {key}: {value}" for key, value in sorted(report.distribution_by_role_track.items())],
        "",
        "## Sponsorship Distribution",
        *[f"- {key}: {value}" for key, value in sorted(report.sponsorship_state_distribution.items())],
        "",
        "## Graduation-Year Distribution",
        *[f"- {key}: {value}" for key, value in sorted(report.graduation_year_state_distribution.items())],
        "",
        "## London Vs Non-London UK",
        *[f"- {key}: {value}" for key, value in sorted(report.london_vs_non_london_uk_distribution.items())],
        "",
        "## Cross-Track Examples",
        *[f"- {example}" for example in report.cross_track_examples],
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output
