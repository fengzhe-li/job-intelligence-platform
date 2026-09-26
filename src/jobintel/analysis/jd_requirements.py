from __future__ import annotations

from dataclasses import dataclass, field

from jobintel.analysis.job_quality import (
    extract_required_experience,
    extract_seniority,
    infer_engineering_domain,
    infer_role_function,
)
from jobintel.models.job import Job
from jobintel.models.taxonomy import RoleTrack

# Aggregates the JD-analysis extractors that already exist across analysis/*
# (skill requirements, seniority, experience, role function/domain, sponsorship,
# graduation-year/intake, eligibility, role tracks -- all computed during
# `enrich_job`) into one structured object per job, per the JD -> requirement
# extraction step of the personalised application pipeline. Deliberately does not
# duplicate any extraction logic; this is a read-only view over an already-enriched
# Job plus two additional deterministic extractors (seniority/experience/role
# function/domain) that `enrich_job` doesn't persist onto the Job model itself.


@dataclass(frozen=True)
class JDRequirements:
    job_id: str
    must_have_skills: list[str]
    preferred_skills: list[str]
    role_family: str
    engineering_domain: str
    seniority: str
    required_experience_years: float | None
    education_requirements: list[str]
    other_eligibility: list[str]
    location_summary: str
    sponsorship_state: str
    graduation_year_state: str
    # Informational only, never a filtering/ranking input -- see
    # analysis.evidence.detect_graduation_year. A job's intake year differing from
    # any particular year is not evidence of ineligibility.
    detected_intake_year: int | None
    primary_role_track: RoleTrack | None
    keywords: list[str] = field(default_factory=list)


def extract_jd_requirements(job: Job) -> JDRequirements:
    seniority = extract_seniority(job.title, job.description)
    experience = extract_required_experience(job.title, job.description)
    role_function = infer_role_function(job.title, job.description)
    engineering_domain = infer_engineering_domain(job.title, job.description)

    must_have = [item.name for item in job.skill_requirements if item.required]
    preferred = [item.name for item in job.skill_requirements if not item.required]

    education_requirements = [item.evidence_text for item in job.eligibility if "degree" in item.label.casefold()]
    other_eligibility = [item.evidence_text for item in job.eligibility if "degree" not in item.label.casefold()]

    location_summary = "; ".join(location.raw or f"{location.city or ''}, {location.country}".strip(", ") for location in job.locations) or job.raw_location or "Unspecified"

    primary = job.role_track_profile.primary()
    keywords = sorted({*must_have, *preferred, *(evidence for score in job.role_track_profile.scores for evidence in score.evidence)})

    return JDRequirements(
        job_id=job.id,
        must_have_skills=must_have,
        preferred_skills=preferred,
        role_family=role_function.function,
        engineering_domain=engineering_domain.domain,
        seniority=seniority.level,
        required_experience_years=experience.years_required,
        education_requirements=education_requirements,
        other_eligibility=other_eligibility,
        location_summary=location_summary,
        sponsorship_state=job.sponsorship.state.value if job.sponsorship else "unknown",
        graduation_year_state=job.graduation_year.state.value if job.graduation_year else "unknown",
        detected_intake_year=job.graduation_year.intake_year if job.graduation_year else None,
        primary_role_track=primary.track if primary else None,
        keywords=list(keywords),
    )
