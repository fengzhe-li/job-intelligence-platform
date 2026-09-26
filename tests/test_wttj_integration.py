from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jobintel.connectors.base import ConnectorQuery, RawJobPayload
from jobintel.connectors.welcome_to_the_jungle import (
    WELCOMEKIT_JOBS_URL,
    WelcomeToTheJungleConnector,
    WelcomeToTheJungleManualConnector,
    is_wttj_job_url,
    prepare_wttj_discovery_import,
    prepare_wttj_manual_import,
    read_wttj_discovery_records,
    read_wttj_manual_records,
)
from jobintel.cli.main import _changed_wttj_fields, _wttj_import_summary, _wttj_observations_by_source_id
from jobintel.dedup.v1 import deduplicate_jobs
from jobintel.analysis.job_enrichment import enrich_job
from jobintel.config import default_ranking_config
from jobintel.fixtures.sample_data import sample_candidate
from jobintel.matching.matcher import match_job
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import RoleTrack, SponsorshipState, WorkMode
from jobintel.pipeline.ingestion import ingest_from_connectors
from jobintel.pipeline.ingestion import rank_jobs
from jobintel.storage.local_store import LocalJobStore


class WelcomeToTheJungleIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)

    def test_wttj_official_api_fetch_and_normalisation(self) -> None:
        payload = {
            "jobs": [
                {
                    "reference": "wk_123",
                    "name": "Junior Backend Engineer",
                    "organization_reference": "demo-org",
                    "organization": {"name": "Demo WTTJ"},
                    "apply_url": "https://apply.example/wk_123",
                    "url": "https://www.welcometothejungle.com/en/companies/demo/jobs/junior-backend-engineer_london",
                    "description": "<p>Build Python APIs and PostgreSQL services.</p>",
                    "profile": "Graduate or junior engineers welcome.",
                    "remote": True,
                    "office": {"city": "London", "country": "United Kingdom"},
                    "published_at": self.now.isoformat(),
                }
            ]
        }

        with patch("jobintel.connectors.welcome_to_the_jungle.fetch_json", return_value=payload) as mocked_fetch:
            raw_jobs = WelcomeToTheJungleConnector(("demo-org",), "secret").fetch_jobs(ConnectorQuery(keywords=("Python",), limit=10))

        self.assertEqual(len(raw_jobs), 1)
        self.assertTrue(mocked_fetch.call_args.args[0].startswith(WELCOMEKIT_JOBS_URL))
        job = WelcomeToTheJungleConnector(("demo-org",), "secret").normalise(raw_jobs[0])

        self.assertEqual(job.title, "Junior Backend Engineer")
        self.assertEqual(job.company, "Demo WTTJ")
        self.assertEqual(job.source_observations[0].source_name, "welcome_to_the_jungle")
        self.assertEqual(job.source_observations[0].source_job_id, "wk_123")
        self.assertEqual(job.canonical_application_url, "https://apply.example/wk_123")
        self.assertEqual(job.locations[0].city, "London")
        self.assertEqual(job.locations[0].work_mode, WorkMode.REMOTE)
        self.assertIn("Build Python APIs", job.description)
        self.assertIn("Graduate or junior", job.description)
        self.assertEqual(job.source_observations[0].raw_payload["_ingestion_method"], "official_api")

    def test_wttj_manual_csv_import_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wttj_jobs.csv"
            path.write_text(
                "company_name,title,location,description,wttj_url,apply_url,posted_at\n"
                "Demo WTTJ,Data Platform Engineer,London UK,Build SQL data pipelines,"
                "https://www.welcometothejungle.com/en/companies/demo/jobs/data-platform-engineer_london,"
                f"https://apply.example/data,{self.now.isoformat()}\n",
                encoding="utf-8",
            )

            records = read_wttj_manual_records(path)
            raw_jobs = WelcomeToTheJungleManualConnector(tuple(records)).fetch_jobs(ConnectorQuery(keywords=("SQL",), limit=5))

        self.assertEqual(len(raw_jobs), 1)
        job = WelcomeToTheJungleManualConnector(tuple(records)).normalise(raw_jobs[0])
        self.assertEqual(job.source_observations[0].source_name, "welcome_to_the_jungle")
        self.assertEqual(job.company, "Demo WTTJ")
        self.assertEqual(job.title, "Data Platform Engineer")
        self.assertEqual(job.canonical_application_url, "https://apply.example/data")
        self.assertEqual(job.source_observations[0].raw_payload["_ingestion_method"], "manual_csv")

    def test_wttj_minimal_csv_row_imports_required_fields_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wttj_jobs.csv"
            path.write_text(
                "wttj_url,title,company\n"
                "https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer_london,Backend Engineer,Demo\n",
                encoding="utf-8",
            )

            records = read_wttj_manual_records(path)
            raw_jobs = WelcomeToTheJungleManualConnector(tuple(records)).fetch_jobs(ConnectorQuery(keywords=(), limit=5))

        job = WelcomeToTheJungleManualConnector(tuple(records)).normalise(raw_jobs[0])
        self.assertEqual(job.title, "Backend Engineer")
        self.assertEqual(job.company, "Demo")
        self.assertEqual(job.description, "")
        self.assertIsNone(job.posted_at)
        self.assertEqual(job.source_observations[0].raw_payload["_ingestion_method"], "manual_csv")

    def test_wttj_rich_csv_feeds_existing_analysis_pipeline(self) -> None:
        record = {
            "wttj_url": "https://www.welcometothejungle.com/en/companies/demo/jobs/junior-data-engineer_london",
            "source_job_id": "wttj-rich-1",
            "title": "Junior Data Engineer",
            "company": "Demo",
            "location": "London, United Kingdom",
            "work_mode": "hybrid",
            "posted_at": self.now.isoformat(),
            "description": "Build Python and SQL data pipelines for analytics platforms.",
            "requirements": "0-2 years experience. Skilled Worker visa sponsorship available. 2026 graduates welcome.",
            "direct_apply_url": "https://apply.example/wttj-rich-1",
            "salary": "GBP 40000",
        }

        raw = WelcomeToTheJungleManualConnector((record,)).fetch_jobs(ConnectorQuery(keywords=(), limit=5))[0]
        job = enrich_job(WelcomeToTheJungleManualConnector((record,)).normalise(raw), 2026)
        match = match_job(sample_candidate(), job, default_ranking_config(), self.now)

        self.assertEqual(job.source_observations[0].raw_payload["_ingestion_method"], "manual_import")
        self.assertIn("0-2 years", job.description)
        self.assertEqual(job.locations[0].work_mode, WorkMode.HYBRID)
        self.assertEqual(job.sponsorship.state, SponsorshipState.EXPLICIT_SPONSOR)
        self.assertIn(RoleTrack.DATA_ENGINEERING, [item.track for item in job.role_track_profile.scores if item.score > 0])
        self.assertGreater(match.overall_priority, 0)

    def test_wttj_discovery_import_accepts_100_lightweight_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wttj_discovery.csv"
            path.write_text(
                "wttj_url,title,company,location,work_mode,posted_at,employment_type,salary,source_category,discovery_url\n"
                + "\n".join(
                    f"https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer-{i}_london,Backend Engineer {i},Demo,London,hybrid,,full_time,,software,https://www.welcometothejungle.com/en/jobs?query=backend"
                    for i in range(100)
                )
                + "\n",
                encoding="utf-8",
            )

            records = read_wttj_discovery_records(path)
            raw_jobs = WelcomeToTheJungleManualConnector(tuple(records)).fetch_jobs(ConnectorQuery(keywords=(), limit=150))

        self.assertEqual(len(records), 100)
        self.assertEqual(len(raw_jobs), 100)
        self.assertTrue(all(job.raw_payload["_ingestion_method"] == "discovery_import" for job in raw_jobs))

    def test_wttj_discovery_only_job_ranks_conservatively_without_jd(self) -> None:
        record = {
            "wttj_url": "https://www.welcometothejungle.com/en/companies/demo/jobs/junior-backend-engineer_london",
            "title": "Junior Backend Engineer",
            "company": "Demo",
            "location": "London",
            "_ingestion_method": "discovery_import",
        }
        raw = WelcomeToTheJungleManualConnector((record,)).fetch_jobs(ConnectorQuery(keywords=(), limit=5))[0]
        job = enrich_job(WelcomeToTheJungleManualConnector((record,)).normalise(raw), 2026)
        ranked = rank_jobs([job], candidate=sample_candidate(), config=default_ranking_config(), now=self.now)[0]
        technical = next(component.score for component in ranked.match.components if component.name == "technical_match")

        self.assertEqual(job.source_observations[0].raw_payload["_enrichment_state"], "discovery_only")
        self.assertEqual(job.sponsorship.state, SponsorshipState.UNKNOWN)
        self.assertEqual(technical, 0.0)
        self.assertIn("No direct skill evidence", ranked.match.explanation)

    def test_wttj_discovery_only_can_be_enriched_later_without_duplicate(self) -> None:
        discovery = {
            "wttj_url": "https://www.welcometothejungle.com/en/companies/demo/jobs/junior-backend-engineer_london",
            "title": "Junior Backend Engineer",
            "company": "Demo",
            "location": "London",
            "_ingestion_method": "discovery_import",
        }
        enriched = {
            **discovery,
            "_ingestion_method": "manual_csv",
            "description": "Build Python APIs.",
            "requirements": "0-2 years experience. Visa sponsorship available.",
            "direct_apply_url": "https://apply.example/backend",
            "posted_at": self.now.isoformat(),
        }

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            ingest_from_connectors([WelcomeToTheJungleManualConnector((discovery,))], ConnectorQuery(keywords=(), limit=5), store=store)
            ingest_from_connectors([WelcomeToTheJungleManualConnector((enriched,))], ConnectorQuery(keywords=(), limit=5), store=store)
            jobs = store.read_jobs()

        self.assertEqual(len(jobs), 1)
        observation = jobs[0].source_observations[0]
        self.assertEqual(observation.raw_payload["_enrichment_state"], "fully_enriched")
        self.assertEqual(observation.raw_payload["_ingestion_method"], "manual_csv")
        self.assertIn("Visa sponsorship", jobs[0].description)
        self.assertEqual(jobs[0].canonical_application_url, "https://apply.example/backend")

    def test_wttj_manual_url_list_import_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wttj_urls.txt"
            path.write_text(
                "https://www.welcometothejungle.com/en/companies/demo/jobs/junior-software-engineer_london\n",
                encoding="utf-8",
            )

            records = read_wttj_manual_records(path)
            raw_jobs = WelcomeToTheJungleManualConnector(tuple(records)).fetch_jobs(ConnectorQuery(keywords=(), limit=5))

        job = WelcomeToTheJungleManualConnector(tuple(records)).normalise(raw_jobs[0])
        self.assertEqual(job.source_observations[0].source_name, "welcome_to_the_jungle")
        self.assertEqual(job.source_observations[0].raw_payload["_ingestion_method"], "manual_url")
        self.assertEqual(job.company, "Demo")
        self.assertEqual(job.raw_location, "London")
        self.assertIn("Junior Software Engineer", job.title)
        self.assertEqual(job.canonical_application_url, "https://www.welcometothejungle.com/en/companies/demo/jobs/junior-software-engineer_london")

    def test_wttj_json_import_uses_manual_json_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wttj_jobs.json"
            path.write_text(
                """
[
  {
    "wttj_url": "https://www.welcometothejungle.com/en/companies/demo/jobs/frontend-engineer_london",
    "title": "Frontend Engineer",
    "company": "Demo",
    "location": "London, United Kingdom",
    "description": "Build React interfaces.",
    "requirements": "JavaScript and TypeScript."
  }
]
""".strip(),
                encoding="utf-8",
            )

            records = read_wttj_manual_records(path)
            raw_jobs = WelcomeToTheJungleManualConnector(tuple(records)).fetch_jobs(ConnectorQuery(keywords=(), limit=5))

        job = WelcomeToTheJungleManualConnector(tuple(records)).normalise(raw_jobs[0])
        self.assertEqual(job.source_observations[0].raw_payload["_ingestion_method"], "manual_json")
        self.assertIn("JavaScript and TypeScript", job.description)

    def test_wttj_url_validation_distinguishes_valid_invalid_and_unsupported(self) -> None:
        self.assertTrue(is_wttj_job_url("https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer_london"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wttj_urls.txt"
            path.write_text(
                "https://example.com/en/companies/demo/jobs/backend-engineer_london\n"
                "https://www.welcometothejungle.com/en/companies/demo\n",
                encoding="utf-8",
            )

            results = prepare_wttj_manual_import(path)

        self.assertEqual([item.status for item in results], ["invalid_url", "unsupported_page_structure"])

    def test_wttj_rich_import_requires_title_and_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wttj_jobs.csv"
            path.write_text(
                "wttj_url,title,company\n"
                "https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer_london,,\n",
                encoding="utf-8",
            )

            results = prepare_wttj_manual_import(path)

        self.assertEqual(results[0].status, "missing_required_fields")
        self.assertIn("title", results[0].error)
        self.assertIn("company", results[0].error)

    def test_wttj_discovery_import_requires_title_and_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wttj_discovery.csv"
            path.write_text(
                "wttj_url,title,company,location\n"
                "https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer_london,,Demo,London\n",
                encoding="utf-8",
            )

            results = prepare_wttj_discovery_import(path)

        self.assertEqual(results[0].status, "missing_required_fields")
        self.assertIn("title", results[0].error)

    def test_wttj_and_ats_duplicate_preserves_wttj_but_prefers_ats_apply_url(self) -> None:
        ats = _job(
            "greenhouse:123",
            "Backend Engineer",
            "Demo",
            "greenhouse",
            "123",
            "https://boards.greenhouse.io/demo/jobs/123",
            "https://boards.greenhouse.io/demo/jobs/123",
        )
        wttj = _job(
            "wttj:wk_123",
            "Backend Engineer",
            "Demo",
            "welcome_to_the_jungle",
            "wk_123",
            "https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer_london",
            "https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer_london",
        )

        deduped = deduplicate_jobs([wttj, ats])

        self.assertEqual(len(deduped), 1)
        self.assertEqual({obs.source_name for obs in deduped[0].source_observations}, {"greenhouse", "welcome_to_the_jungle"})
        self.assertEqual(deduped[0].canonical_application_url, "https://boards.greenhouse.io/demo/jobs/123")

    def test_wttj_only_vacancy_remains_canonical_wttj_job(self) -> None:
        wttj = _job(
            "wttj:wk_999",
            "Junior Data Engineer",
            "Demo",
            "welcome_to_the_jungle",
            "wk_999",
            "https://www.welcometothejungle.com/en/companies/demo/jobs/junior-data-engineer_london",
            "https://www.welcometothejungle.com/en/companies/demo/jobs/junior-data-engineer_london",
        )

        deduped = deduplicate_jobs([wttj])

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].source_observations[0].source_name, "welcome_to_the_jungle")
        self.assertEqual(deduped[0].canonical_application_url, wttj.canonical_application_url)

    def test_wttj_import_summary_reports_imported_duplicate_failed_and_retained(self) -> None:
        existing_ids = {"greenhouse:123"}
        after_jobs = [
            _job(
                "greenhouse:123",
                "Backend Engineer",
                "Demo",
                "greenhouse",
                "123",
                "https://boards.greenhouse.io/demo/jobs/123",
                "https://boards.greenhouse.io/demo/jobs/123",
            ),
            _job(
                "wttj:wk_999",
                "Junior Data Engineer",
                "Demo",
                "welcome_to_the_jungle",
                "wk_999",
                "https://www.welcometothejungle.com/en/companies/demo/jobs/junior-data-engineer_london",
                "https://www.welcometothejungle.com/en/companies/demo/jobs/junior-data-engineer_london",
            ),
        ]
        after_jobs[0].source_observations.append(
            SourceObservation(
                source_name="welcome_to_the_jungle",
                source_job_id="wk_123",
                original_url="https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer_london",
                first_seen_at=self.now,
                last_seen_at=self.now,
                posted_at=self.now,
                raw_description="Build Python backend services.",
                canonical_application_url="https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer_london",
                raw_payload={"_ingestion_method": "manual_url"},
            )
        )
        prepared = [
            _prepared("https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer_london"),
            _prepared("https://www.welcometothejungle.com/en/companies/demo/jobs/junior-data-engineer_london"),
            type("Prepared", (), {"row_number": 3, "url": "https://example.com/job", "status": "invalid_url"})(),
        ]

        summary = _wttj_import_summary(prepared, after_jobs, existing_ids)

        self.assertEqual(summary["imported"], 1)
        self.assertEqual(summary["deduplicated"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["canonical_wttj_jobs"], 2)
        self.assertEqual(summary["wttj_observations_retained"], 2)
        self.assertEqual(summary["missing_description"], 2)
        self.assertEqual(summary["missing_direct_apply_url"], 2)

    def test_repeated_wttj_import_updates_last_seen_and_preserves_first_seen(self) -> None:
        first_record = {
            "wttj_url": "https://www.welcometothejungle.com/en/companies/demo/jobs/backend-engineer_london",
            "source_job_id": "wttj-repeat",
            "title": "Backend Engineer",
            "company": "Demo",
            "description": "Build APIs.",
        }
        second_record = {**first_record, "description": "Build APIs and data services."}

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            ingest_from_connectors([WelcomeToTheJungleManualConnector((first_record,))], ConnectorQuery(keywords=(), limit=5), store=store)
            first_job = store.read_jobs()[0]
            before = _wttj_observations_by_source_id(store.read_jobs())
            ingest_from_connectors([WelcomeToTheJungleManualConnector((second_record,))], ConnectorQuery(keywords=(), limit=5), store=store)
            jobs = store.read_jobs()

        self.assertEqual(len(jobs), 1)
        observation = jobs[0].source_observations[0]
        self.assertEqual(observation.first_seen_at, first_job.source_observations[0].first_seen_at)
        self.assertGreaterEqual(observation.last_seen_at, observation.first_seen_at)
        self.assertIn("data services", jobs[0].description)
        self.assertEqual(_changed_wttj_fields(before[("welcome_to_the_jungle", "wttj-repeat")], second_record), ["description"])

    def test_missing_wttj_credentials_do_not_fetch(self) -> None:
        connector = WelcomeToTheJungleConnector(("demo",), None)

        with self.assertRaisesRegex(RuntimeError, "WTTJ_API_KEY"):
            connector.fetch_jobs(ConnectorQuery(limit=1))

    def test_wttj_remote_no_does_not_pollute_location(self) -> None:
        raw = RawJobPayload(
            "welcome_to_the_jungle",
            "wk_no_remote",
            "https://www.welcometothejungle.com/en/companies/demo/jobs/backend_london",
            "https://apply.example/backend",
            {
                "reference": "wk_no_remote",
                "name": "Backend Engineer",
                "organization": {"name": "Demo WTTJ"},
                "office": {"city": "London", "country": "United Kingdom"},
                "remote": "no",
                "description": "Build APIs.",
            },
            self.now,
            self.now,
        )

        job = WelcomeToTheJungleConnector(("demo",), "secret").normalise(raw)

        self.assertEqual(job.raw_location, "London, United Kingdom")
        self.assertEqual(job.locations[0].work_mode, WorkMode.UNKNOWN)


def _job(job_id: str, title: str, company: str, source: str, source_id: str, original_url: str, apply_url: str) -> Job:
    now = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    return Job(
        id=job_id,
        title=title,
        company=company,
        description="Build Python backend services.",
        locations=[Location(city="London", country="United Kingdom", region="Greater London", work_mode=WorkMode.HYBRID, raw="London Hybrid")],
        source_observations=[
            SourceObservation(
                source_name=source,
                source_job_id=source_id,
                original_url=original_url,
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description="Build Python backend services.",
                canonical_application_url=apply_url,
                raw_payload={"source": source},
            )
        ],
        raw_location="London Hybrid",
    )


def _prepared(url: str):
    return type("Prepared", (), {"row_number": 1, "url": url, "status": "imported", "record": {"url": url, "reference": url.rsplit("/", 1)[-1]}})()


if __name__ == "__main__":
    unittest.main()
