from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from jobintel.analysis.evidence import detect_graduation_year
from jobintel.analysis.job_enrichment import enrich_job
from jobintel.config import default_ranking_config
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import GraduationYearState
from jobintel.profile_ingestion import build_candidate_profile


class GraduationYearVsIntakeYearTests(unittest.TestCase):
    """Covers the correction: the candidate's actual graduation year (2026) and a
    job's independent intake/start year (which may be 2026, 2027, 2028, another
    year, or unknown) are two distinct concepts. There is deliberately no single
    fixed "target intake year" used for filtering -- graduate opportunities from
    any intake year are relevant if the candidate is otherwise eligible. A job is
    only treated as excluding the candidate when the JD gives explicit,
    unambiguous evidence of that; anything else must be retained, not silently
    dropped.
    """

    CANDIDATE_GRADUATION_YEAR = 2026

    def test_config_default_is_the_actual_graduation_year_and_there_is_no_target_cycle_field(self) -> None:
        config = default_ranking_config()
        self.assertEqual(config.candidate_graduation_year, 2026)
        self.assertFalse(hasattr(config, "target_recruitment_cycle_year"))

    def test_profile_rebuild_default_uses_actual_graduation_year(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = build_candidate_profile(tmp, name="Test Candidate")
        self.assertEqual(profile.graduation_year, 2026)

    def test_range_eligibility_accepts_candidate_within_stated_window(self) -> None:
        evidence = detect_graduation_year(
            "We welcome applications from 2025-2027 graduates for our engineering programme.",
            self.CANDIDATE_GRADUATION_YEAR,
        )
        self.assertEqual(evidence.state, GraduationYearState.YEAR_2026_ACCEPTED)

    def test_range_eligibility_rejects_candidate_explicitly_outside_stated_window(self) -> None:
        evidence = detect_graduation_year(
            "We welcome applications from 2027-2028 graduates only.",
            self.CANDIDATE_GRADUATION_YEAR,
        )
        self.assertEqual(evidence.state, GraduationYearState.OTHER_YEAR_RESTRICTION)

    def test_strict_restriction_matching_candidates_own_year_is_accepted(self) -> None:
        evidence = detect_graduation_year("2026 graduates only need apply.", self.CANDIDATE_GRADUATION_YEAR)
        self.assertEqual(evidence.state, GraduationYearState.YEAR_2026_ACCEPTED)

    def test_strict_restriction_to_a_different_year_explicitly_excludes(self) -> None:
        evidence = detect_graduation_year("2024 graduates only need apply.", self.CANDIDATE_GRADUATION_YEAR)
        self.assertEqual(evidence.state, GraduationYearState.YEAR_2027_ONLY_STRICT)

    # --- Intake year is independent per-job metadata, never a filter ---

    def test_2026_intake_does_not_get_special_treatment_over_other_intake_years(self) -> None:
        evidence = detect_graduation_year("Join our 2026 Graduate Programme. Open to graduates of any year.", self.CANDIDATE_GRADUATION_YEAR)
        self.assertEqual(evidence.intake_year, 2026)
        self.assertNotEqual(evidence.state, GraduationYearState.OTHER_YEAR_RESTRICTION)

    def test_2027_intake_is_not_penalised_relative_to_candidates_2026_graduation(self) -> None:
        evidence = detect_graduation_year("Join our 2027 Graduate Programme. Open to graduates of any year.", self.CANDIDATE_GRADUATION_YEAR)
        self.assertEqual(evidence.intake_year, 2027)
        self.assertNotEqual(evidence.state, GraduationYearState.OTHER_YEAR_RESTRICTION)
        self.assertNotEqual(evidence.state, GraduationYearState.YEAR_2027_ONLY_STRICT)

    def test_2028_intake_is_not_penalised_either(self) -> None:
        evidence = detect_graduation_year("Applications now open for our 2028 intake.", self.CANDIDATE_GRADUATION_YEAR)
        self.assertEqual(evidence.intake_year, 2028)
        self.assertNotEqual(evidence.state, GraduationYearState.OTHER_YEAR_RESTRICTION)
        self.assertNotEqual(evidence.state, GraduationYearState.YEAR_2027_ONLY_STRICT)

    def test_job_with_unknown_intake_year_is_retained_not_filtered(self) -> None:
        evidence = detect_graduation_year("Build backend services with Python and PostgreSQL.", self.CANDIDATE_GRADUATION_YEAR)
        self.assertIsNone(evidence.intake_year)
        self.assertEqual(evidence.state, GraduationYearState.NO_YEAR_STATED)
        self.assertNotEqual(evidence.state, GraduationYearState.OTHER_YEAR_RESTRICTION)
        self.assertNotEqual(evidence.state, GraduationYearState.YEAR_2027_ONLY_STRICT)

    def test_explicit_graduation_year_restriction_overrides_a_differently_worded_intake_year(self) -> None:
        # A hard "2024 graduates only" restriction excludes a 2026 graduate even if
        # the same posting also mentions an unrelated intake year.
        evidence = detect_graduation_year(
            "2024 graduates only. This role is part of our 2027 Graduate Programme.",
            self.CANDIDATE_GRADUATION_YEAR,
        )
        self.assertEqual(evidence.state, GraduationYearState.YEAR_2027_ONLY_STRICT)

    # --- No silent false negatives: ambiguous/ordinary phrasing must not exclude ---

    def test_no_year_mentioned_at_all_is_retained_as_unclear_not_excluded(self) -> None:
        evidence = detect_graduation_year("We are hiring a software engineer to join our platform team.", self.CANDIDATE_GRADUATION_YEAR)
        self.assertEqual(evidence.state, GraduationYearState.NO_YEAR_STATED)

    def test_generic_graduate_friendly_phrasing_is_retained_and_positive(self) -> None:
        evidence = detect_graduation_year("We welcome recent graduates to apply for this role.", self.CANDIDATE_GRADUATION_YEAR)
        self.assertEqual(evidence.state, GraduationYearState.GRADUATE_FRIENDLY)

    def test_unrelated_year_mention_does_not_silently_exclude(self) -> None:
        # A year appearing in an unrelated context (e.g. company founding date)
        # must not be misread as a graduation-year restriction.
        evidence = detect_graduation_year("Founded in 2019, our engineering team builds backend services.", self.CANDIDATE_GRADUATION_YEAR)
        self.assertNotEqual(evidence.state, GraduationYearState.OTHER_YEAR_RESTRICTION)
        self.assertNotEqual(evidence.state, GraduationYearState.YEAR_2027_ONLY_STRICT)

    def test_ranking_weight_for_unknown_and_intake_only_jobs_is_never_negative(self) -> None:
        config = default_ranking_config()
        for description in (
            "Build backend services with Python and PostgreSQL.",
            "Join our 2027 Graduate Programme. Open to graduates of any year.",
            "Applications now open for our 2028 intake.",
        ):
            evidence = detect_graduation_year(description, self.CANDIDATE_GRADUATION_YEAR)
            self.assertGreaterEqual(
                config.graduation_year_weights[evidence.state],
                0.0,
                msg=f"{description!r} produced a negative ranking weight via state {evidence.state}",
            )

    def test_enrich_job_never_conflates_intake_year_with_graduation_year_eligibility(self) -> None:
        job = _job("Join our 2027 Graduate Programme. Open to graduates of any year.")
        enriched = enrich_job(job, self.CANDIDATE_GRADUATION_YEAR)
        self.assertEqual(enriched.graduation_year.intake_year, 2027)
        self.assertNotEqual(enriched.graduation_year.state, GraduationYearState.OTHER_YEAR_RESTRICTION)


def _job(description: str) -> Job:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return Job(
        id="job-1",
        title="Graduate Software Engineer",
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


if __name__ == "__main__":
    unittest.main()
