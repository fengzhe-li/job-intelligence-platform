from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.config import default_ranking_config
from jobintel.connectors.adzuna import AdzunaConnector
from jobintel.connectors.ashby import AshbyConnector
from jobintel.connectors.base import ConnectorQuery, RawJobPayload
from jobintel.connectors.greenhouse import GreenhouseConnector
from jobintel.connectors.lever import LeverConnector
from jobintel.connectors.smartrecruiters import SmartRecruitersConnector
from jobintel.connectors.workable import WorkableConnector
from jobintel.dedup.v1 import deduplicate_jobs
from jobintel.fixtures.sample_data import sample_candidate
from jobintel.matching.matcher import match_job
from jobintel.models.taxonomy import GraduationYearState, LocationMode, SponsorshipState


class Phase2SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        self.config = default_ranking_config()

    def test_greenhouse_normalisation(self) -> None:
        connector = GreenhouseConnector(("demo",))
        job = enrich_job(connector.normalise(_greenhouse_raw(self.now)), 2026)

        self.assertEqual(job.title, "Software Engineer - Data Platform")
        self.assertEqual(job.company, "demo")
        self.assertEqual(job.locations[0].city, "London")
        self.assertIn("Python", {skill.name for skill in job.skill_requirements})
        self.assertEqual(job.sponsorship.state, SponsorshipState.EXPLICIT_SPONSOR)
        self.assertIn("Visa sponsorship is available", job.sponsorship.evidence_text)
        self.assertTrue(job.source_observations[0].raw_payload)

    def test_lever_normalisation(self) -> None:
        connector = LeverConnector(("demo",))
        job = enrich_job(connector.normalise(_lever_raw(self.now)), 2026)

        self.assertEqual(job.title, "Backend Engineer")
        self.assertEqual(job.company, "demo")
        self.assertEqual(job.locations[0].city, "Manchester")
        self.assertEqual(job.sponsorship.state, SponsorshipState.EXPLICIT_NO_SPONSOR)
        self.assertIn("without sponsorship", job.sponsorship.evidence_text)

    def test_adzuna_normalisation(self) -> None:
        connector = AdzunaConnector("id", "key")
        job = enrich_job(connector.normalise(_adzuna_raw(self.now)), 2026)

        self.assertEqual(job.title, "Cloud Platform Engineer")
        self.assertEqual(job.company, "Energy Tech")
        self.assertEqual(job.locations[0].city, "Edinburgh")
        self.assertEqual(job.salary, "GBP 35000-45000")
        self.assertEqual(job.graduation_year.state, GraduationYearState.GRADUATE_FRIENDLY)

    def test_cross_source_deduplication_and_direct_url_preference(self) -> None:
        greenhouse = GreenhouseConnector(("demo",)).normalise(_greenhouse_raw(self.now))
        adzuna = AdzunaConnector("id", "key").normalise(
            replace(
                _adzuna_raw(self.now),
                source_job_id="duplicate-1",
                canonical_application_url=greenhouse.canonical_application_url,
                source_url="https://adzuna.example/duplicate",
                raw_payload={
                    **_adzuna_raw(self.now).raw_payload,
                    "title": "Software Engineer Data Platform",
                    "company": {"display_name": "demo"},
                    "redirect_url": greenhouse.canonical_application_url,
                    "location": {"display_name": "London, UK", "area": ["UK", "London"]},
                },
            )
        )

        deduped = deduplicate_jobs([adzuna, greenhouse])

        self.assertEqual(len(deduped), 1)
        self.assertEqual({obs.source_name for obs in deduped[0].source_observations}, {"adzuna", "greenhouse"})
        self.assertEqual(deduped[0].canonical_application_url, greenhouse.canonical_application_url)

    def test_freshness_handling(self) -> None:
        candidate = sample_candidate()
        recent = enrich_job(GreenhouseConnector(("demo",)).normalise(_greenhouse_raw(self.now)), 2026)
        old_raw = replace(_greenhouse_raw(self.now), posted_at=self.now - timedelta(days=90))
        old = enrich_job(GreenhouseConnector(("demo",)).normalise(old_raw), 2026)

        recent_match = match_job(candidate, recent, self.config, self.now)
        old_match = match_job(candidate, old, self.config, self.now)

        self.assertGreater(_component(recent_match, "freshness").score, _component(old_match, "freshness").score)

    def test_real_job_wording_preserves_sponsorship_and_graduation(self) -> None:
        job = enrich_job(GreenhouseConnector(("demo",)).normalise(_strict_2027_raw(self.now)), 2026)

        self.assertEqual(job.sponsorship.state, SponsorshipState.UNKNOWN)
        self.assertEqual(job.graduation_year.state, GraduationYearState.YEAR_2027_ONLY_STRICT)
        self.assertIn("2027 graduates only", job.graduation_year.evidence_text)

    def test_london_vs_uk_wide_modes_on_real_normalised_job(self) -> None:
        candidate = sample_candidate()
        job = enrich_job(LeverConnector(("demo",)).normalise(_lever_raw(self.now)), 2026)

        london_only = match_job(candidate, job, replace(self.config, location_mode=LocationMode.LONDON_ONLY), self.now)
        uk_wide = match_job(candidate, job, replace(self.config, location_mode=LocationMode.UK_WIDE), self.now)

        self.assertLess(_component(london_only, "location").score, 0)
        self.assertGreater(_component(uk_wide, "location").score, 0)

    def test_lever_description_preserves_string_content_items(self) -> None:
        raw = _lever_raw_with_payload(
            self.now,
            {
                "descriptionPlain": None,
                "description": "<p>Overview paragraph.</p>",
                "lists": [{"text": "Responsibilities", "content": ["Build Python APIs.", "Operate PostgreSQL services."]}],
            },
        )

        job = LeverConnector(("demo",)).normalise(raw)

        self.assertIn("Overview paragraph", job.description)
        self.assertIn("Responsibilities", job.description)
        self.assertIn("Build Python APIs", job.description)
        self.assertIn("Operate PostgreSQL services", job.description)

    def test_lever_description_preserves_dict_content_items(self) -> None:
        raw = _lever_raw_with_payload(
            self.now,
            {
                "descriptionPlain": "Join the backend team.",
                "lists": [{"text": "Requirements", "content": [{"text": "Python experience."}, {"text": "Cloud experience."}]}],
            },
        )

        job = LeverConnector(("demo",)).normalise(raw)

        self.assertIn("Join the backend team", job.description)
        self.assertIn("Requirements", job.description)
        self.assertIn("Python experience", job.description)
        self.assertIn("Cloud experience", job.description)

    def test_lever_description_preserves_mixed_and_nested_content_items(self) -> None:
        raw = _lever_raw_with_payload(
            self.now,
            {
                "lists": [
                    "Plain list preface.",
                    {
                        "text": "What you will do",
                        "content": [
                            "Frontend work with React.",
                            [{"text": "Full-stack delivery."}, {"content": ["Nested platform tooling."]}],
                            {"unexpected": "ignored"},
                            None,
                        ],
                    },
                ],
            },
        )

        job = LeverConnector(("demo",)).normalise(raw)

        self.assertIn("Plain list preface", job.description)
        self.assertIn("Frontend work with React", job.description)
        self.assertIn("Full-stack delivery", job.description)
        self.assertIn("Nested platform tooling", job.description)

    def test_lever_normalisation_tolerates_empty_content(self) -> None:
        raw = _lever_raw_with_payload(self.now, {"descriptionPlain": None, "description": None, "lists": []})

        job = LeverConnector(("demo",)).normalise(raw)

        self.assertEqual(job.description, "")
        self.assertEqual(job.locations[0].country, "United Kingdom")

    def test_lever_normalisation_tolerates_missing_optional_fields(self) -> None:
        payload = {"id": "minimal", "text": "Software Engineer", "_site": "demo"}
        raw = RawJobPayload("lever", "minimal", "https://jobs.lever.co/demo/minimal", "https://jobs.lever.co/demo/minimal", payload, self.now, self.now)

        job = LeverConnector(("demo",)).normalise(raw)

        self.assertEqual(job.title, "Software Engineer")
        self.assertEqual(job.company, "demo")
        self.assertEqual(job.raw_location, "United Kingdom")
        self.assertEqual(job.source_observations[0].canonical_application_url, "https://jobs.lever.co/demo/minimal")

    def test_lever_fetch_jobs_tolerates_non_dict_and_missing_optional_fields(self) -> None:
        payload = [
            "malformed",
            {
                "id": "ok",
                "text": "Python Engineer",
                "hostedUrl": "https://jobs.lever.co/demo/ok",
                "createdAt": int(self.now.timestamp() * 1000),
                "categories": None,
                "lists": [{"content": ["Build Python services."]}],
            },
        ]

        with patch("jobintel.connectors.lever.fetch_json", return_value=payload):
            raw_jobs = LeverConnector(("demo",)).fetch_jobs(ConnectorQuery(keywords=("Python",), limit=10))

        self.assertEqual(len(raw_jobs), 1)
        self.assertEqual(raw_jobs[0].source_job_id, "ok")
        self.assertEqual(raw_jobs[0].canonical_application_url, "https://jobs.lever.co/demo/ok")

    def test_ashby_fetch_and_normalisation_preserve_public_payload(self) -> None:
        payload = {
            "jobs": [
                {
                    "id": "ash-1",
                    "title": "Graduate Software Engineer",
                    "organizationName": "Demo Ashby",
                    "location": "London, United Kingdom",
                    "secondaryLocations": [{"location": "Remote - UK"}],
                    "descriptionHtml": "<p>Build Python APIs with React.</p>",
                    "jobUrl": "https://jobs.ashbyhq.com/demo/ash-1",
                    "applicationUrl": "https://jobs.ashbyhq.com/demo/ash-1/application",
                    "publishedAt": self.now.isoformat(),
                }
            ]
        }

        with patch("jobintel.connectors.ashby.fetch_json", return_value=payload):
            raw_jobs = AshbyConnector(("demo",)).fetch_jobs(ConnectorQuery(keywords=("Python",), limit=10))
        job = AshbyConnector(("demo",)).normalise(raw_jobs[0])

        self.assertEqual(len(raw_jobs), 1)
        self.assertEqual(job.title, "Graduate Software Engineer")
        self.assertEqual(job.company, "Demo Ashby")
        self.assertEqual(job.locations[0].city, "London")
        self.assertEqual(job.locations[1].work_mode.value, "remote")
        self.assertEqual(job.canonical_application_url, "https://jobs.ashbyhq.com/demo/ash-1/application")
        self.assertIn("Build Python APIs", job.source_observations[0].raw_description)
        self.assertTrue(job.source_observations[0].raw_payload)

    def test_workable_fetch_and_normalisation_preserve_public_payload(self) -> None:
        payload = {
            "name": "Demo Workable",
            "jobs": [
                {
                    "id": "wrk-1",
                    "title": "Junior Data Engineer",
                    "url": "https://apply.workable.com/demo/j/wrk-1/",
                    "application_url": "https://apply.workable.com/demo/j/wrk-1/apply/",
                    "published_on": self.now.isoformat(),
                    "location": {"city": "Manchester", "country_name": "United Kingdom"},
                    "description": "<p>Python and SQL data pipeline role.</p>",
                    "requirements": "<p>0-2 years experience.</p>",
                }
            ],
        }

        with patch("jobintel.connectors.workable.fetch_json", return_value=payload):
            raw_jobs = WorkableConnector(("demo",)).fetch_jobs(ConnectorQuery(keywords=("data pipeline",), limit=10))
        job = WorkableConnector(("demo",)).normalise(raw_jobs[0])

        self.assertEqual(job.title, "Junior Data Engineer")
        self.assertEqual(job.company, "Demo Workable")
        self.assertEqual(job.locations[0].city, "Manchester")
        self.assertEqual(job.canonical_application_url, "https://apply.workable.com/demo/j/wrk-1/apply/")
        self.assertIn("0-2 years", job.description)
        self.assertTrue(job.source_observations[0].raw_payload)

    def test_smartrecruiters_fetch_and_normalisation_preserve_public_payload(self) -> None:
        list_payload = {
            "content": [
                {
                    "id": "smr-1",
                    "name": "Network Software Engineer",
                    "ref": "https://jobs.smartrecruiters.com/Demo/123-network-software-engineer",
                    "releasedDate": self.now.isoformat(),
                    "location": {"city": "London", "country": "United Kingdom"},
                    "company": {"name": "Demo SmartRecruiters"},
                }
            ]
        }
        detail_payload = {
            "id": "smr-1",
            "name": "Network Software Engineer",
            "applyUrl": "https://jobs.smartrecruiters.com/Demo/123/apply",
            "jobAd": {"sections": {"jobDescription": "<p>Build TCP/IP and BGP systems in Python.</p>"}},
        }

        with patch("jobintel.connectors.smartrecruiters.fetch_json", side_effect=[list_payload, detail_payload]):
            raw_jobs = SmartRecruitersConnector(("Demo",)).fetch_jobs(ConnectorQuery(keywords=("TCP/IP",), limit=10))
        job = SmartRecruitersConnector(("Demo",)).normalise(raw_jobs[0])

        self.assertEqual(job.title, "Network Software Engineer")
        self.assertEqual(job.company, "Demo SmartRecruiters")
        self.assertEqual(job.locations[0].city, "London")
        self.assertEqual(job.canonical_application_url, "https://jobs.smartrecruiters.com/Demo/123/apply")
        self.assertIn("Build TCP/IP", job.description)
        self.assertIn("_list_payload", job.source_observations[0].raw_payload)


def _greenhouse_raw(now: datetime) -> RawJobPayload:
    payload = {
        "id": 101,
        "title": "Software Engineer - Data Platform",
        "absolute_url": "https://boards.greenhouse.io/demo/jobs/101",
        "content": "<p>Build Python and SQL services for a data platform. Visa sponsorship is available. Graduating in 2026 welcome.</p>",
        "offices": [{"name": "London", "location": "London, United Kingdom"}],
        "updated_at": now.isoformat(),
        "_board_token": "demo",
    }
    return RawJobPayload("greenhouse", "101", payload["absolute_url"], payload["absolute_url"], payload, now, now)


def _strict_2027_raw(now: datetime) -> RawJobPayload:
    raw = _greenhouse_raw(now)
    payload = {**raw.raw_payload, "id": 202, "content": "<p>Python graduate programme. 2027 graduates only.</p>"}
    return RawJobPayload("greenhouse", "202", "https://boards.greenhouse.io/demo/jobs/202", "https://boards.greenhouse.io/demo/jobs/202", payload, now, now)


def _lever_raw(now: datetime) -> RawJobPayload:
    payload = {
        "id": "abc",
        "text": "Backend Engineer",
        "hostedUrl": "https://jobs.lever.co/demo/abc",
        "applyUrl": "https://jobs.lever.co/demo/abc/apply",
        "createdAt": int(now.timestamp() * 1000),
        "categories": {"location": "Manchester, United Kingdom"},
        "descriptionPlain": "Build backend APIs with Python and PostgreSQL. Must have the right to work in the UK without sponsorship.",
        "lists": [],
        "_site": "demo",
    }
    return RawJobPayload("lever", "abc", payload["hostedUrl"], payload["applyUrl"], payload, now, now)


def _lever_raw_with_payload(now: datetime, overrides: dict) -> RawJobPayload:
    raw = _lever_raw(now)
    payload = {**raw.raw_payload, **overrides}
    return RawJobPayload("lever", payload.get("id", "abc"), payload.get("hostedUrl", raw.source_url), payload.get("applyUrl", raw.canonical_application_url), payload, now, now)


def _adzuna_raw(now: datetime) -> RawJobPayload:
    payload = {
        "id": "adz-1",
        "title": "Cloud Platform Engineer",
        "company": {"display_name": "Energy Tech"},
        "description": "Graduate programme for recent graduates. Work on AWS, Kubernetes and Terraform.",
        "redirect_url": "https://adzuna.example/job/adz-1",
        "created": now.isoformat(),
        "location": {"display_name": "Edinburgh, United Kingdom", "area": ["UK", "Scotland", "Edinburgh"]},
        "salary_min": 35000,
        "salary_max": 45000,
        "salary_currency": "GBP",
    }
    return RawJobPayload("adzuna", "adz-1", payload["redirect_url"], payload["redirect_url"], payload, now, now)


def _component(result, name: str):
    return next(component for component in result.components if component.name == name)


if __name__ == "__main__":
    unittest.main()
