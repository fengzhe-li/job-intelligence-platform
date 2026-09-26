from __future__ import annotations

import unittest
from datetime import datetime, timezone

from jobintel.analysis.jd_requirements import extract_jd_requirements
from jobintel.analysis.job_enrichment import enrich_job
from jobintel.matching.review_gate import evaluate_review_gate
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import ReviewGateStatus


def _job(description: str) -> Job:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    job = Job(
        id="job-1",
        title="Graduate Backend Engineer",
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


class ReviewGateTests(unittest.TestCase):
    def test_all_required_skills_matched_is_auto_prepare(self) -> None:
        job = _job("Required: Python, PostgreSQL.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python", "PostgreSQL"], missing_required_skills=[])
        self.assertEqual(decision.status, ReviewGateStatus.AUTO_PREPARE)
        self.assertEqual(decision.reasons, [])

    def test_minority_missing_required_skills_is_review_required(self) -> None:
        job = _job("Required: Python, PostgreSQL, Docker.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python", "PostgreSQL"], missing_required_skills=["Docker"])
        self.assertEqual(decision.status, ReviewGateStatus.REVIEW_REQUIRED)
        self.assertTrue(decision.reasons)

    def test_majority_missing_required_skills_is_blocked(self) -> None:
        job = _job("Required: Python, PostgreSQL, Docker, Kubernetes, Terraform.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python"], missing_required_skills=["PostgreSQL", "Docker", "Kubernetes", "Terraform"])
        self.assertEqual(decision.status, ReviewGateStatus.BLOCK_AUTO_SUBMISSION)

    def test_explicit_graduation_year_exclusion_blocks_regardless_of_skill_match(self) -> None:
        job = _job("2024 graduates only. Required: Python.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python"], missing_required_skills=[])
        self.assertEqual(decision.status, ReviewGateStatus.BLOCK_AUTO_SUBMISSION)

    def test_unclear_graduation_year_does_not_block(self) -> None:
        job = _job("Required: Python. Build backend services.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python"], missing_required_skills=[])
        self.assertEqual(decision.status, ReviewGateStatus.AUTO_PREPARE)

    def test_intake_year_alone_never_triggers_block(self) -> None:
        job = _job("Join our 2028 Graduate Programme. Required: Python.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python"], missing_required_skills=[])
        self.assertEqual(decision.status, ReviewGateStatus.AUTO_PREPARE)

    def test_explicit_no_sponsorship_downgrades_auto_prepare_to_review(self) -> None:
        job = _job("Required: Python. Applicants must have the right to work in the UK without sponsorship.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python"], missing_required_skills=[])
        self.assertEqual(decision.status, ReviewGateStatus.REVIEW_REQUIRED)

    def test_partial_required_skill_downgrades_auto_prepare_to_review(self) -> None:
        job = _job("Required: Python.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python"], missing_required_skills=[], partial_required_skills=["Python"])
        self.assertEqual(decision.status, ReviewGateStatus.REVIEW_REQUIRED)
        self.assertTrue(any("partial evidence" in reason.casefold() for reason in decision.reasons))

    def test_missing_required_contact_fields_downgrades_auto_prepare_to_review(self) -> None:
        job = _job("Required: Python.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(
            jd, job, matched_required_skills=["Python"], missing_required_skills=[], missing_required_contact_fields=["email", "phone"]
        )
        self.assertEqual(decision.status, ReviewGateStatus.REVIEW_REQUIRED)
        self.assertTrue(any("contact" in reason.casefold() for reason in decision.reasons))

    def test_one_extra_page_downgrades_auto_prepare_to_review(self) -> None:
        job = _job("Required: Python.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python"], missing_required_skills=[], page_count=2)
        self.assertEqual(decision.status, ReviewGateStatus.REVIEW_REQUIRED)

    def test_many_extra_pages_blocks_auto_submission(self) -> None:
        job = _job("Required: Python.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python"], missing_required_skills=[], page_count=3)
        self.assertEqual(decision.status, ReviewGateStatus.BLOCK_AUTO_SUBMISSION)

    def test_single_page_never_triggers_a_page_related_reason(self) -> None:
        job = _job("Required: Python.")
        jd = extract_jd_requirements(job)
        decision = evaluate_review_gate(jd, job, matched_required_skills=["Python"], missing_required_skills=[], page_count=1)
        self.assertEqual(decision.status, ReviewGateStatus.AUTO_PREPARE)
        self.assertEqual(decision.reasons, [])


if __name__ == "__main__":
    unittest.main()
