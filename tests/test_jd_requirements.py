from __future__ import annotations

import unittest
from datetime import datetime, timezone

from jobintel.analysis.jd_requirements import extract_jd_requirements
from jobintel.analysis.job_enrichment import enrich_job
from jobintel.models.job import Job, Location, SourceObservation


def _job(description: str, title: str = "Graduate Backend Engineer") -> Job:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    job = Job(
        id="job-1",
        title=title,
        company="Example Co",
        description=description,
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name="company",
                source_job_id="job-1",
                original_url="https://example.com/jobs/1",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description=description,
                canonical_application_url="https://example.com/jobs/1/apply",
            )
        ],
    )
    return enrich_job(job, 2026)


class JDRequirementsTests(unittest.TestCase):
    def test_splits_must_have_and_preferred_skills(self) -> None:
        job = _job("Required: Python, PostgreSQL. Docker is nice to have. AWS preferred.")
        jd = extract_jd_requirements(job)

        self.assertIn("Python", jd.must_have_skills)
        self.assertIn("PostgreSQL", jd.must_have_skills)
        self.assertIn("Docker", jd.preferred_skills)
        self.assertIn("AWS", jd.preferred_skills)

    def test_extracts_seniority_and_experience(self) -> None:
        job = _job("Graduate role. 0-2 years experience. Build Python backend APIs.")
        jd = extract_jd_requirements(job)

        self.assertEqual(jd.seniority, "graduate")

    def test_extracts_role_family_and_domain(self) -> None:
        job = _job("We need a backend engineer to build Python microservices.")
        jd = extract_jd_requirements(job)

        self.assertEqual(jd.role_family, "software_engineering")

    def test_detected_intake_year_is_informational_only(self) -> None:
        job = _job("Join our 2028 Graduate Programme building Python backend services.")
        jd = extract_jd_requirements(job)

        self.assertEqual(jd.detected_intake_year, 2028)
        # Informational only -- must not appear as a rejection/graduation-year mismatch.
        self.assertNotEqual(jd.graduation_year_state, "other_year_restriction")

    def test_education_requirements_are_separated_from_other_eligibility(self) -> None:
        job = _job("Requires a degree in computer science. Right to work in the UK required.")
        jd = extract_jd_requirements(job)

        self.assertTrue(any("degree" in item.casefold() for item in jd.education_requirements))
        self.assertTrue(any("right to work" in item.casefold() for item in jd.other_eligibility))


if __name__ == "__main__":
    unittest.main()
