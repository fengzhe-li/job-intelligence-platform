from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jobintel.config import default_ranking_config
from jobintel.connectors.adzuna import AdzunaConnector
from jobintel.connectors.base import ConnectorQuery
from jobintel.pipeline.refresh import refresh_sources
from jobintel.storage.local_store import LocalJobStore


def _registry(tmp: str, entries: list[dict]) -> Path:
    path = Path(tmp) / "target_companies.json"
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def _greenhouse_entry(company_name: str, token: str) -> dict:
    return {
        "company_name": company_name,
        "industry": "SaaS",
        "priority": 1,
        "connector_type": "greenhouse",
        "connector_token": token,
        "enabled": True,
        "verification_status": "verified",
    }


def _greenhouse_job(job_id: str, title: str = "Graduate Software Engineer") -> dict:
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/demo/jobs/{job_id}",
        "updated_at": "2026-09-01T09:00:00+00:00",
        "content": "<p>Build Python backend services.</p>",
        "offices": [{"location": "London, United Kingdom"}],
    }


class TotalFetchFailureSafetyTests(unittest.TestCase):
    """Section F: a failed refresh must never close previously-seen jobs, and
    must never be silently represented as '0 new jobs'."""

    def test_total_fetch_failure_does_not_close_previously_seen_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": [_greenhouse_job("1")]}):
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=urllib.error.URLError("network down")):
                results = refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            job = store.read_jobs()[0]

        self.assertEqual(results[0].status, "error")
        self.assertIn("network down", results[0].error)
        # CRITICAL: the previously-seen job must remain active, not silently closed.
        self.assertTrue(job.source_observations[0].active)

    def test_malformed_response_is_reported_as_error_not_silently_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=json.JSONDecodeError("bad json", "", 0)):
                results = refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

        self.assertEqual(results[0].status, "error")
        self.assertIsNotNone(results[0].error)
        self.assertEqual(results[0].jobs_seen, 0)

    def test_last_synced_is_preserved_across_a_failed_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": [_greenhouse_job("1")]}):
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)
            first_last_synced = store.read_source_health()["greenhouse"]["last_synced"]
            self.assertIsNotNone(first_last_synced)

            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=urllib.error.URLError("network down")):
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)
            health = store.read_source_health()["greenhouse"]

        # CRITICAL: must NOT regress to "never synced" just because today failed.
        self.assertEqual(health["last_synced"], first_last_synced)
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["last_successful_jobs_seen"], 1)


class PartialFetchFailureTests(unittest.TestCase):
    """One broken company/board must not silently drop every other configured
    company's results for the same connector, and must not trigger closure
    inference for identifiers we couldn't even check this cycle."""

    def test_one_bad_board_does_not_lose_the_other_boards_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Good Co", "good"), _greenhouse_entry("Bad Co", "bad")])
            store = LocalJobStore(Path(tmp) / "store")

            def fake_fetch(url: str):
                if "/bad/" in url:
                    raise urllib.error.URLError("bad board unreachable")
                return {"jobs": [_greenhouse_job("1")]}

            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=fake_fetch):
                results = refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            jobs = store.read_jobs()

        self.assertEqual(results[0].status, "partial")
        self.assertIn("bad", results[0].failed_identifiers)
        # The good board's job must still have been written.
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].source_observations[0].active)

    def test_partial_failure_does_not_close_the_unreachable_boards_prior_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Good Co", "good"), _greenhouse_entry("Flaky Co", "flaky")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch(
                "jobintel.connectors.greenhouse.fetch_json",
                side_effect=lambda url: {"jobs": [_greenhouse_job("good-1")]} if "/good/" in url else {"jobs": [_greenhouse_job("flaky-1")]},
            ):
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            def flaky_fails(url: str):
                if "/flaky/" in url:
                    raise urllib.error.URLError("timeout")
                return {"jobs": [_greenhouse_job("good-1")]}

            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=flaky_fails):
                results = refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            jobs = {job.source_observations[0].source_job_id: job for job in store.read_jobs()}

        self.assertEqual(results[0].status, "partial")
        # CRITICAL: flaky-1 was never re-checked this cycle -- it must not be
        # silently marked closed just because "good" boards were refreshed.
        self.assertTrue(jobs["flaky-1"].source_observations[0].active)
        self.assertTrue(jobs["good-1"].source_observations[0].active)


class SuspiciousZeroResultTests(unittest.TestCase):
    def test_zero_results_after_previously_healthy_refresh_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": [_greenhouse_job(str(i)) for i in range(10)]}):
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=50)

            with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": []}):
                results = refresh_sources(registry, store=store, config=default_ranking_config(), limit=50)

        self.assertEqual(results[0].status, "live_api")
        self.assertIsNotNone(results[0].warning)
        self.assertIn("0 jobs", results[0].warning)

    def test_normal_result_count_produces_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": [_greenhouse_job(str(i)) for i in range(10)]}):
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=50)
                results = refresh_sources(registry, store=store, config=default_ranking_config(), limit=50)

        self.assertIsNone(results[0].warning)


class MissingCredentialsTests(unittest.TestCase):
    def test_adzuna_without_credentials_raises_rather_than_returning_silently_empty(self) -> None:
        connector = AdzunaConnector(app_id=None, app_key=None)
        with self.assertRaises(RuntimeError):
            connector.fetch_jobs(ConnectorQuery(limit=10))
        health = connector.health_check()
        self.assertFalse(health.ok)
        self.assertIn("ADZUNA", health.message)


class DeadlineSafetyTests(unittest.TestCase):
    def test_manual_import_preserves_explicit_deadline_with_provenance(self) -> None:
        from jobintel.connectors.graduate_sources import TRACKR
        from jobintel.connectors.manual_source import ManualSourceConnector

        record = {
            "job_url": "https://app.the-trackr.com/programme/graduate-role",
            "title": "Graduate Role",
            "company": "Example Co",
            "deadline": "2026-10-15T23:59:00+00:00",
        }
        connector = ManualSourceConnector(TRACKR, (record,))
        raw = connector.fetch_jobs(ConnectorQuery(limit=10))[0]
        job = connector.normalise(raw)

        self.assertEqual(job.source_observations[0].deadline, datetime(2026, 10, 15, 23, 59, tzinfo=timezone.utc))
        self.assertEqual(job.deadline_observations, {"trackr": datetime(2026, 10, 15, 23, 59, tzinfo=timezone.utc)})

    def test_missing_deadline_is_stored_as_unknown_never_invented(self) -> None:
        from jobintel.connectors.graduate_sources import TRACKR
        from jobintel.connectors.manual_source import ManualSourceConnector

        record = {"job_url": "https://app.the-trackr.com/programme/graduate-role", "title": "Graduate Role", "company": "Example Co"}
        connector = ManualSourceConnector(TRACKR, (record,))
        raw = connector.fetch_jobs(ConnectorQuery(limit=10))[0]
        job = connector.normalise(raw)

        self.assertIsNone(job.source_observations[0].deadline)
        self.assertEqual(job.deadline_observations, {})
        self.assertIsNone(job.earliest_deadline)

    def test_conflicting_deadlines_from_two_sources_are_both_preserved_not_silently_resolved(self) -> None:
        from jobintel.models.job import Job, Location, SourceObservation

        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        job = Job(
            id="job-1",
            title="Graduate Role",
            company="Example Co",
            description="",
            locations=[Location(city="London", country="United Kingdom")],
            source_observations=[
                SourceObservation(
                    source_name="trackr",
                    source_job_id="1",
                    original_url="https://app.the-trackr.com/programme/1",
                    first_seen_at=now,
                    last_seen_at=now,
                    posted_at=now,
                    raw_description="",
                    canonical_application_url="https://example.com/apply",
                    deadline=datetime(2026, 10, 15, tzinfo=timezone.utc),
                ),
                SourceObservation(
                    source_name="gradcracker",
                    source_job_id="1",
                    original_url="https://www.gradcracker.com/jobs/1",
                    first_seen_at=now,
                    last_seen_at=now,
                    posted_at=now,
                    raw_description="",
                    canonical_application_url="https://example.com/apply",
                    deadline=datetime(2026, 10, 20, tzinfo=timezone.utc),
                ),
            ],
        )

        self.assertTrue(job.deadline_conflict)
        self.assertEqual(job.deadline_observations, {"trackr": datetime(2026, 10, 15, tzinfo=timezone.utc), "gradcracker": datetime(2026, 10, 20, tzinfo=timezone.utc)})
        self.assertEqual(job.earliest_deadline, datetime(2026, 10, 15, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
