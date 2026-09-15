from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from jobintel.analysis.role_tracks import extract_skill_requirements, infer_role_track_profile
from jobintel.config import default_ranking_config
from jobintel.fixtures.sample_data import sample_candidate, sample_jobs
from jobintel.matching.matcher import match_job
from jobintel.models.taxonomy import (
    GraduationYearState,
    LocationMode,
    RoleTrack,
    SponsorshipState,
)


class Phase1FoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = sample_candidate()
        self.jobs = {job.id: job for job in sample_jobs()}
        self.config = default_ranking_config()
        self.now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    def test_capability_provenance_is_preserved(self) -> None:
        postgres = self.candidate.capabilities["postgresql"]

        self.assertGreater(postgres.confidence, 0)
        self.assertEqual(postgres.evidence[0].source.title, "Financial Knowledge Intelligence Platform")
        self.assertIn("PostgreSQL persistence", postgres.evidence[0].quote)

    def test_multi_track_role_classification(self) -> None:
        profile = infer_role_track_profile(
            "Software Engineer - Data Platform",
            "Build Python software for a data platform with Spark, SQL and Kubernetes.",
        )
        tracks = {score.track for score in profile.scores}

        self.assertIn(RoleTrack.SOFTWARE_ENGINEERING, tracks)
        self.assertIn(RoleTrack.DATA_PLATFORM, tracks)
        self.assertIn(RoleTrack.DATA_ENGINEERING, tracks)

    def test_sponsorship_evidence_preserved(self) -> None:
        job = self.jobs["software-data-platform"]

        self.assertEqual(job.sponsorship.state, SponsorshipState.EXPLICIT_SPONSOR)
        self.assertIn("Skilled worker visa sponsorship", job.sponsorship.evidence_text)

    def test_graduation_year_downranking(self) -> None:
        strict = match_job(self.candidate, self.jobs["strict-2027-grad"], self.config, self.now)
        unrestricted = match_job(self.candidate, self.jobs["backend-london-sponsor"], self.config, self.now)

        self.assertEqual(self.jobs["strict-2027-grad"].graduation_year.state, GraduationYearState.YEAR_2027_ONLY_STRICT)
        self.assertLess(strict.overall_priority, unrestricted.overall_priority)
        self.assertTrue(any(component.name == "graduation_year" and component.score < 0 for component in strict.components))

    def test_london_first_vs_uk_wide_behaviour(self) -> None:
        london_job = self.jobs["backend-london-sponsor"]
        manchester_job = self.jobs["data-engineer-manchester"]

        london_first_london = match_job(self.candidate, london_job, self.config, self.now)
        london_first_manchester = match_job(self.candidate, manchester_job, self.config, self.now)
        uk_wide_config = replace(self.config, location_mode=LocationMode.UK_WIDE)
        uk_wide_manchester = match_job(self.candidate, manchester_job, uk_wide_config, self.now)

        london_first_location = _component(london_first_london, "location").score
        manchester_location = _component(london_first_manchester, "location").score
        uk_wide_location = _component(uk_wide_manchester, "location").score

        self.assertGreater(london_first_location, manchester_location)
        self.assertGreater(uk_wide_location, manchester_location)

    def test_candidate_job_matching_has_evidence_and_gaps(self) -> None:
        result = match_job(self.candidate, self.jobs["software-data-platform"], self.config, self.now)

        self.assertGreater(result.overall_priority, 0)
        self.assertTrue(result.strongest_supporting_evidence)
        self.assertIn(RoleTrack.DATA_PLATFORM, result.primary_role_tracks + result.secondary_role_tracks)
        self.assertTrue(any(component.name == "technical_match" for component in result.components))

    def test_hybrid_role_detection(self) -> None:
        result = match_job(self.candidate, self.jobs["software-data-platform"], self.config, self.now)

        self.assertTrue(result.hybrid_cv_recommended)
        self.assertIn(result.best_existing_cv_category, {"Software/Backend", "Data", "Cloud/Platform"})

    def test_excluded_unrelated_occupation_handling(self) -> None:
        result = match_job(self.candidate, self.jobs["sales-engineer-excluded"], self.config, self.now)

        self.assertTrue(result.excluded)
        self.assertLess(result.overall_priority, 0)

    def test_skill_extraction_handles_symbol_skills(self) -> None:
        skills = {item.name for item in extract_skill_requirements("Embedded firmware in C++ with MQTT.")}

        self.assertIn("C++", skills)
        self.assertIn("MQTT", skills)


def _component(result, name: str):
    return next(component for component in result.components if component.name == name)


if __name__ == "__main__":
    unittest.main()

