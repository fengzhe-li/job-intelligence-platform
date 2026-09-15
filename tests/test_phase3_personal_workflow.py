from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.company_registry import connectors_from_registry, load_company_registry, registry_summary
from jobintel.config import default_ranking_config
from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload, SourceHealth
from jobintel.connectors.utils import normalise_location
from jobintel.matching.matcher import match_job
from jobintel.models.job import Job, SourceObservation
from jobintel.models.taxonomy import EvidenceSourceType, LocationMode, RoleTrack, SponsorshipState, WorkMode
from jobintel.pipeline.ingestion import ingest_from_connectors
from jobintel.profile_ingestion import add_profile_source, build_candidate_profile
from jobintel.storage.local_store import LocalJobStore


class Phase3PersonalWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    def test_company_registry_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "company_name": "Demo",
                            "industry": "FinTech",
                            "priority": 5,
                            "greenhouse_board_token": "demo",
                            "lever_site_token": None,
                            "careers_url": "https://example.com",
                            "source_connector_type": "greenhouse",
                            "enabled": True,
                            "notes": "test",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            companies = load_company_registry(path)

        self.assertEqual(companies[0].company_name, "Demo")
        self.assertEqual(registry_summary(companies)["greenhouse_connections"], 1)
        self.assertEqual(len(connectors_from_registry(companies)), 1)

    def test_multi_company_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            jobs = ingest_from_connectors([FakeConnector("greenhouse", "A"), FakeConnector("lever", "B")], ConnectorQuery(limit=10), store)

            self.assertEqual(len(jobs), 2)
            self.assertEqual(len(store.read_jobs()), 2)

    def test_persistent_first_last_seen_and_new_job_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            first_time = self.now - timedelta(days=1)
            second_time = self.now
            first_job = _job("job-1", "Backend Engineer", "Demo", first_time)
            second_job = _job("job-1", "Backend Engineer", "Demo", second_time)

            store.write_jobs([first_job])
            store.write_jobs([second_job])
            stored = store.read_jobs()[0]

            self.assertEqual(stored.source_observations[0].first_seen_at, first_time)
            self.assertEqual(stored.source_observations[0].last_seen_at, second_time)
            self.assertEqual(store.jobs_first_seen_since(self.now - timedelta(hours=1)), [])

    def test_inactive_state_when_job_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs([_job("job-1", "Backend Engineer", "Demo", self.now)])
            store.write_jobs([])
            stored = store.read_jobs()[0]

            self.assertFalse(stored.source_observations[0].active)
            self.assertEqual(stored.source_observations[0].latest_observed_state, "DISAPPEARED")

    def test_uk_location_normalisation(self) -> None:
        cases = {
            "Greater London": ("London", "Greater London", WorkMode.UNKNOWN),
            "City of London": ("London", "Greater London", WorkMode.UNKNOWN),
            "Remote - UK": (None, None, WorkMode.REMOTE),
            "UK Remote": (None, None, WorkMode.REMOTE),
            "Hybrid London": ("London", "Greater London", WorkMode.HYBRID),
            "Reading, England": ("Reading", "England", WorkMode.UNKNOWN),
        }
        for raw, expected in cases.items():
            location = normalise_location(raw)
            self.assertEqual((location.city, location.region, location.work_mode), expected)
            self.assertEqual(location.country, "United Kingdom")

    def test_london_first_ranking_does_not_hide_non_london(self) -> None:
        candidate = _profile_candidate(tmp_text="Python PostgreSQL backend APIs")
        config = default_ranking_config()
        london = enrich_job(_job("london", "Backend Engineer", "Demo", self.now, raw_location="London"), 2026)
        manchester = enrich_job(_job("manchester", "Backend Engineer", "Demo", self.now, raw_location="Manchester"), 2026)

        london_match = match_job(candidate, london, config, self.now)
        manchester_match = match_job(candidate, manchester, config, self.now)
        london_only_match = match_job(candidate, manchester, replace(config, location_mode=LocationMode.LONDON_ONLY), self.now)

        self.assertGreater(london_match.overall_priority, manchester_match.overall_priority)
        self.assertGreater(manchester_match.overall_priority, 0)
        self.assertLess(next(component.score for component in london_only_match.components if component.name == "location"), 0)

    def test_real_candidate_evidence_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "project.md"
            source.write_text("Implemented Python APIs with PostgreSQL, Spark pipelines and Airflow orchestration.", encoding="utf-8")
            add_profile_source(tmp, source, EvidenceSourceType.MANUAL_PROJECT_DESCRIPTION, "Data Platform Project")
            profile = build_candidate_profile(tmp, "Me", 2026)

            self.assertIn("python", profile.capabilities)
            self.assertIn("spark", profile.capabilities)
            self.assertEqual(profile.capabilities["python"].evidence[0].source.title, "Data Platform Project")

    def test_broad_technical_role_inclusion_and_unrelated_exclusion(self) -> None:
        candidate = _profile_candidate("Python TCP/IP 5G C++ MQTT PostgreSQL")
        network = enrich_job(_job("network", "Network Software Engineer", "InfraCo", self.now, description="Python TCP/IP BGP network automation."), 2026)
        sales = enrich_job(_job("sales", "Graduate Sales Engineer", "SalesCo", self.now, description="Sales and account development."), 2026)

        network_match = match_job(candidate, network, default_ranking_config(), self.now)
        sales_match = match_job(candidate, sales, default_ranking_config(), self.now)

        self.assertIn(RoleTrack.NETWORK_SOFTWARE, network_match.primary_role_tracks + network_match.secondary_role_tracks)
        self.assertFalse(network_match.excluded)
        self.assertTrue(sales_match.excluded)


class FakeConnector(JobSourceConnector):
    def __init__(self, source_name: str, company: str) -> None:
        self.source_name = source_name
        self.company = company

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        return [
            RawJobPayload(
                source_name=self.source_name,
                source_job_id=f"{self.company}-1",
                source_url=f"https://example.com/{self.company}/1",
                canonical_application_url=f"https://example.com/{self.company}/1",
                raw_payload={"title": "Backend Engineer", "company": self.company, "description": "Python PostgreSQL backend APIs."},
                observed_at=now,
                posted_at=now,
            )
        ]

    def health_check(self) -> SourceHealth:
        return SourceHealth(self.source_name, True, "ok", datetime(2026, 8, 20, tzinfo=timezone.utc))

    def normalise(self, raw: RawJobPayload) -> Job:
        return _job(raw.source_job_id, raw.raw_payload["title"], raw.raw_payload["company"], raw.observed_at, description=raw.raw_payload["description"], source=self.source_name)


def _job(
    job_id: str,
    title: str,
    company: str,
    observed_at: datetime,
    description: str = "Python PostgreSQL backend APIs.",
    raw_location: str = "London",
    source: str = "greenhouse",
) -> Job:
    location = normalise_location(raw_location)
    return Job(
        id=job_id,
        title=title,
        company=company,
        description=description,
        locations=[location],
        source_observations=[
            SourceObservation(
                source_name=source,
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


def _profile_candidate(tmp_text: str):
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "profile.md"
        source.write_text(tmp_text, encoding="utf-8")
        add_profile_source(tmp, source, EvidenceSourceType.MANUAL_PROJECT_DESCRIPTION, "Profile Project")
        return build_candidate_profile(tmp, "Me", 2026)


if __name__ == "__main__":
    unittest.main()
