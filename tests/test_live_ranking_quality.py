from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.analysis.job_quality import assess_uk_market, extract_required_experience, extract_seniority, infer_engineering_domain, infer_role_function
from jobintel.config import default_ranking_config
from jobintel.connectors.utils import normalise_location
from jobintel.matching.matcher import match_job
from jobintel.models.job import Job, SourceObservation
from jobintel.models.taxonomy import EvidenceSourceType, RoleTrack, WorkMode
from jobintel.profile_ingestion import add_profile_source, build_candidate_profile


class LiveRankingQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        self.config = default_ranking_config()
        self.candidate = _candidate()

    def test_fpa_data_analytics_manager_is_business_function_not_ai_ml_engineering(self) -> None:
        job = enrich_job(
            _job(
                "fpa",
                "FP&A Data & Analytics Manager",
                "Monzo",
                self.now,
                "Own FP&A reporting, finance analytics, stakeholder management, SQL dashboards and Python analysis.",
                "Cardiff, London or Remote (UK)",
            ),
            2026,
        )

        result = match_job(self.candidate, job, self.config, self.now)

        self.assertNotIn(RoleTrack.AI_ML_ENGINEERING, result.primary_role_tracks + result.secondary_role_tracks)
        self.assertLess(_component(result, "technical_match").score, 0.5)
        self.assertLess(_component(result, "seniority").score, 0)
        self.assertLess(_component(result, "role_function").score, 0)
        self.assertIn("fp&a finance analytics", result.explanation)

    def test_engineering_manager_is_heavily_downranked_below_graduate_engineer(self) -> None:
        manager = enrich_job(
            _job(
                "manager",
                "Engineering Manager - Distributed Storage",
                "Canonical",
                self.now,
                "Lead a team of engineers. Requires people management experience and 5+ years of software engineering.",
            ),
            2026,
        )
        graduate = enrich_job(
            _job(
                "graduate",
                "Graduate Software Engineer",
                "Canonical",
                self.now,
                "Graduate software engineer role building Python APIs. 0-2 years experience. UK remote.",
            ),
            2026,
        )

        manager_match = match_job(self.candidate, manager, self.config, self.now)
        graduate_match = match_job(self.candidate, graduate, self.config, self.now)

        self.assertLess(_component(manager_match, "seniority").score, -1.0)
        self.assertLess(_component(manager_match, "required_experience").score, 0)
        self.assertLess(manager_match.overall_priority, graduate_match.overall_priority)

    def test_senior_staff_principal_roles_are_strongly_downranked(self) -> None:
        titles = ("Senior Backend Engineer", "Staff Analytics Engineer", "Principal Platform Engineer")
        for title in titles:
            job = enrich_job(_job(title, title, "Monzo", self.now, "Build backend services with Python, SQL and AWS. 5+ years experience."), 2026)
            result = match_job(self.candidate, job, self.config, self.now)

            self.assertLess(_component(result, "seniority").score, 0)
            self.assertLess(_component(result, "required_experience").score, 0)
            self.assertLess(_component(result, "technical_match").score, 1.0)

    def test_graduate_and_junior_engineer_are_preferred(self) -> None:
        graduate = enrich_job(_job("grad", "Graduate Software Engineer", "Zopa", self.now, "Python backend APIs. 0-2 years experience."), 2026)
        junior = enrich_job(_job("junior", "Junior Data Engineer", "Zopa", self.now, "SQL, Python and data pipeline work. Entry-level welcome."), 2026)

        graduate_match = match_job(self.candidate, graduate, self.config, self.now)
        junior_match = match_job(self.candidate, junior, self.config, self.now)

        self.assertGreater(_component(graduate_match, "seniority").score, 0)
        self.assertGreater(_component(junior_match, "seniority").score, 0)
        self.assertGreater(graduate_match.overall_priority, 0)
        self.assertGreater(junior_match.overall_priority, 0)

    def test_network_software_engineer_remains_good_cross_track_match(self) -> None:
        job = enrich_job(
            _job(
                "network",
                "Network Software Engineer",
                "Cloudflare",
                self.now,
                "Build network software using Python, TCP/IP, BGP and Linux for edge network systems.",
            ),
            2026,
        )

        result = match_job(self.candidate, job, self.config, self.now)

        self.assertIn(RoleTrack.NETWORK_SOFTWARE, result.primary_role_tracks + result.secondary_role_tracks)
        self.assertGreater(_component(result, "role_function").score, 0)

    def test_uk_remote_role_gets_market_support(self) -> None:
        job = enrich_job(_job("uk-remote", "Backend Engineer", "Monzo", self.now, "Build Python services.", "UK Remote"), 2026)

        result = match_job(self.candidate, job, self.config, self.now)

        self.assertEqual(job.locations[0].work_mode, WorkMode.REMOTE)
        self.assertGreater(_component(result, "market_fit").score, 0)

    def test_non_uk_role_is_downranked_by_market_fit(self) -> None:
        uk_job = enrich_job(_job("uk", "Backend Engineer", "Monzo", self.now, "Build Python services.", "London"), 2026)
        non_uk = enrich_job(_job("us", "Backend Engineer", "Monzo", self.now, "Build Python services. Americas only.", "United States"), 2026)

        uk_match = match_job(self.candidate, uk_job, self.config, self.now)
        non_uk_match = match_job(self.candidate, non_uk, self.config, self.now)

        self.assertLess(assess_uk_market(non_uk).score, 0)
        self.assertLess(_component(non_uk_match, "market_fit").score, 0)
        self.assertLess(non_uk_match.overall_priority, uk_match.overall_priority)

    def test_signal_extractors_detect_required_levels_and_functions(self) -> None:
        title = "Fraud Team Leader - Investigations"
        description = "Lead a fraud investigations team with management experience and 3-5 years in operations."

        self.assertEqual(extract_seniority(title, description).level, "manager")
        self.assertTrue(extract_required_experience(title, description).management_required)
        self.assertEqual(infer_role_function(title, description).function, "operations_investigations")

    def test_numeric_engineering_ladders_are_conservatively_classified(self) -> None:
        cases = {
            "Backend Engineer I": ("junior", 0.75, "level I"),
            "Software Engineer 1": ("junior", 0.75, "level 1"),
            "Software Engineer II": ("mid-level", -0.25, "level II"),
            "Platform Engineer 2": ("mid-level", -0.25, "level 2"),
            "Backend Engineer III": ("mid-senior", -0.65, "level III"),
            "Software Engineer 3": ("mid-senior", -0.65, "level 3"),
            "Machine Learning Engineer IC4": ("mid-senior", -0.65, "level 4"),
        }
        for title, expected in cases.items():
            signal = extract_seniority(title, "")

            self.assertEqual(signal.level, expected[0])
            self.assertEqual(signal.score, expected[1])
            self.assertIn(expected[2], signal.evidence)

    def test_numeric_level_three_is_downranked_below_graduate_role(self) -> None:
        level_three = enrich_job(_job("iii", "Backend Engineer III", "Monzo", self.now, "Build Python services."), 2026)
        graduate = enrich_job(_job("grad", "Graduate Backend Engineer", "Monzo", self.now, "Build Python services. 0-2 years experience."), 2026)

        level_three_match = match_job(self.candidate, level_three, self.config, self.now)
        graduate_match = match_job(self.candidate, graduate, self.config, self.now)

        self.assertLess(_component(level_three_match, "seniority").score, 0)
        self.assertGreater(graduate_match.overall_priority, level_three_match.overall_priority)

    def test_edf_market_specialist_is_business_function_not_engineering_match(self) -> None:
        job = enrich_job(
            _job(
                "edf-market",
                "Market Specialist",
                "EDF UK",
                self.now,
                "Analyse energy markets, trading strategy, SQL dashboards, Python models, platform data and stakeholder reporting.",
                "London, England, gb",
            ),
            2026,
        )

        result = match_job(self.candidate, job, self.config, self.now)

        self.assertEqual(infer_role_function(job.title, job.description).function, "business_market")
        self.assertEqual(result.primary_role_tracks + result.secondary_role_tracks, [])
        self.assertLess(_component(result, "role_function").score, 0)
        self.assertLess(_component(result, "technical_match").score, 0.36)
        self.assertIn("business market", result.explanation)

    def test_alten_software_network_role_is_preserved_as_adjacent_good_match(self) -> None:
        job = enrich_job(
            _job(
                "alten-embedded",
                "Embedded Software Engineer",
                "ALTEN",
                self.now,
                "Develop embedded software using C, C++, Linux and TCP/IP networking for connected systems.",
                "Manchester, England, gb",
            ),
            2026,
        )

        result = match_job(self.candidate, job, self.config, self.now)

        self.assertIn(RoleTrack.EMBEDDED_SOFTWARE, result.primary_role_tracks + result.secondary_role_tracks)
        self.assertGreaterEqual(_component(result, "engineering_domain").score, 0)
        self.assertGreater(result.overall_priority, 0)

    def test_alten_mechanical_or_power_role_is_downranked_not_deleted(self) -> None:
        industrial = enrich_job(
            _job(
                "alten-mech",
                "Gas Turbine Performance Engineer",
                "ALTEN",
                self.now,
                "Mechanical engineering role for gas turbine performance, power systems and MATLAB analysis. Python is useful.",
                "Derby, England, gb",
            ),
            2026,
        )
        software = enrich_job(
            _job(
                "alten-software",
                "Embedded Software Engineer",
                "ALTEN",
                self.now,
                "Develop embedded software using C, C++, Linux and TCP/IP networking.",
                "Manchester, England, gb",
            ),
            2026,
        )

        industrial_match = match_job(self.candidate, industrial, self.config, self.now)
        software_match = match_job(self.candidate, software, self.config, self.now)

        self.assertLess(_component(industrial_match, "engineering_domain").score, 0)
        self.assertLess(_component(industrial_match, "technical_match").score, 0.46)
        self.assertLess(industrial_match.overall_priority, software_match.overall_priority)
        self.assertFalse(industrial_match.excluded)
        self.assertIn("Power/high-voltage engineering domain", industrial_match.explanation)

    def test_telecom_network_engineering_role_is_preserved(self) -> None:
        job = enrich_job(
            _job(
                "telecom",
                "Telecom Network Engineer",
                "NetworkCo",
                self.now,
                "Build routing, switching, TCP/IP and BGP automation for 5G telecom infrastructure using Python.",
                "London",
            ),
            2026,
        )

        result = match_job(self.candidate, job, self.config, self.now)

        self.assertIn(RoleTrack.NETWORK_ENGINEERING, result.primary_role_tracks + result.secondary_role_tracks)
        self.assertGreaterEqual(_component(result, "engineering_domain").score, 0)
        self.assertGreater(result.overall_priority, 0)

    def test_data_software_hybrid_role_is_preserved(self) -> None:
        job = enrich_job(
            _job(
                "hybrid",
                "Software Engineer (Data)",
                "Suade",
                self.now,
                "Build Python FastAPI services and data pipelines on PostgreSQL and Docker.",
                "London, England, United Kingdom",
            ),
            2026,
        )

        result = match_job(self.candidate, job, self.config, self.now)

        self.assertIn(RoleTrack.SOFTWARE_ENGINEERING, result.primary_role_tracks + result.secondary_role_tracks)
        self.assertIn(RoleTrack.DATA_ENGINEERING, result.primary_role_tracks + result.secondary_role_tracks)
        self.assertGreaterEqual(_component(result, "engineering_domain").score, 0)
        self.assertGreater(result.overall_priority, 0)


def _job(job_id: str, title: str, company: str, observed_at: datetime, description: str, raw_location: str = "London") -> Job:
    location = normalise_location(raw_location)
    return Job(
        id=job_id,
        title=title,
        company=company,
        description=description,
        locations=[location],
        source_observations=[
            SourceObservation(
                source_name="greenhouse",
                source_job_id=job_id,
                original_url=f"https://example.com/{job_id}",
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                posted_at=observed_at,
                raw_description=description,
                canonical_application_url=f"https://example.com/{job_id}",
                raw_payload={"id": job_id},
            )
        ],
        raw_location=raw_location,
    )


def _candidate():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "profile.md"
        source.write_text(
            "Graduate Software Engineer with Python, SQL, FastAPI, PostgreSQL, AWS, Docker, React, Linux, TCP/IP and BGP project evidence.",
            encoding="utf-8",
        )
        add_profile_source(tmp, source, EvidenceSourceType.MANUAL_PROJECT_DESCRIPTION, "Profile Project")
        return build_candidate_profile(tmp, "Me", 2026)


def _component(result, name: str):
    return next(component for component in result.components if component.name == name)


if __name__ == "__main__":
    unittest.main()
