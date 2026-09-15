from __future__ import annotations

from jobintel.analysis.evidence import detect_eligibility, detect_graduation_year, detect_sponsorship
from jobintel.analysis.role_tracks import extract_skill_requirements, infer_role_track_profile
from jobintel.models.job import Job


def enrich_job(job: Job, candidate_graduation_year: int = 2026) -> Job:
    job.role_track_profile = infer_role_track_profile(job.title, job.description)
    job.skill_requirements = extract_skill_requirements(job.description)
    job.sponsorship = detect_sponsorship(job.description)
    job.graduation_year = detect_graduation_year(job.description, candidate_graduation_year)
    job.eligibility = detect_eligibility(job.description)
    return job

