from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from jobintel.company_registry import connectors_from_registry, load_company_registry
from jobintel.config import default_ranking_config
from jobintel.connectors.base import ConnectorQuery
from jobintel.pipeline.refresh import refresh_connector
from jobintel.storage.local_store import LocalJobStore

# Regression coverage for the final pre-Phase-3 consistency fix: `ingest-
# registry` had the exact same bug `ingest --adzuna` had before Phase 2.7.3 --
# it called `ingest_from_connectors` directly and never wrote a source_health
# record, so `coverage-today` could show a registry-configured source as
# "never checked" even after a real `ingest-registry` run. These tests
# exercise the fixed path directly: `connectors_from_registry` +
# `refresh_connector`, exactly what `_ingest_registry` (cli/main.py) now
# does, rather than going through argv/main() (this codebase's tests don't
# exercise the CLI parsing layer directly anywhere else either).


def _registry(tmp: str, entries: list[dict]) -> Path:
    path = Path(tmp) / "target_companies.json"
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def _greenhouse_entry(company_name: str, token: str, **overrides) -> dict:
    entry = {
        "company_name": company_name,
        "industry": "SaaS",
        "priority": 1,
        "connector_type": "greenhouse",
        "connector_token": token,
        "enabled": True,
        "verification_status": "verified",
    }
    entry.update(overrides)
    return entry


def _greenhouse_job(job_id: str, title: str = "Graduate Software Engineer") -> dict:
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/demo/jobs/{job_id}",
        "updated_at": "2026-09-01T09:00:00+00:00",
        "content": "<p>Build Python backend services.</p>",
        "offices": [{"location": "London, United Kingdom"}],
    }


def _run_ingest_registry(registry_path: Path, store: LocalJobStore, limit: int = 100):
    # Mirrors _ingest_registry in cli/main.py exactly.
    companies = load_company_registry(registry_path)
    connectors = connectors_from_registry(companies)
    query = ConnectorQuery(location="United Kingdom", limit=limit)
    return [refresh_connector(connector, store, default_ranking_config(), query) for connector in connectors]


class SuccessfulRegistryIngestUpdatesHealthTests(unittest.TestCase):
    def test_successful_run_writes_live_api_health_with_real_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch(
                "jobintel.connectors.greenhouse.fetch_json",
                return_value={"jobs": [_greenhouse_job("1", "Graduate Software Engineer"), _greenhouse_job("2", "Graduate Data Engineer")]},
            ):
                results = _run_ingest_registry(registry, store)

            health = store.read_source_health()["greenhouse"]
            persisted_count = len(store.read_jobs())

        self.assertEqual(results[0].status, "live_api")
        self.assertEqual(health["status"], "live_api")
        self.assertIsNotNone(health["last_synced"])
        self.assertEqual(health["jobs_seen"], 2)
        self.assertEqual(persisted_count, 2)


class FailedRegistryIngestUpdatesHealthTests(unittest.TestCase):
    def test_total_fetch_failure_writes_error_health_not_zero_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=urllib.error.URLError("network down")):
                results = _run_ingest_registry(registry, store)

            health = store.read_source_health()["greenhouse"]

        self.assertEqual(results[0].status, "error")
        self.assertEqual(health["status"], "error")
        self.assertIn("network down", health["last_error"])
        self.assertNotEqual(health["status"], "live_api")

    def test_never_remains_never_checked_after_a_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=urllib.error.URLError("down")):
                _run_ingest_registry(registry, store)

            self.assertIn("greenhouse", store.read_source_health())


class ZeroJobsDistinguishableFromFailureTests(unittest.TestCase):
    def test_genuine_empty_successful_refresh_is_not_confused_with_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": []}):
                results = _run_ingest_registry(registry, store)

            health = store.read_source_health()["greenhouse"]

        self.assertEqual(results[0].status, "live_api")
        self.assertEqual(health["status"], "live_api")
        self.assertEqual(health["jobs_seen"], 0)
        self.assertIsNone(health["last_error"])


class RepeatedRegistryIngestTests(unittest.TestCase):
    def test_repeated_run_updates_counts_and_preserves_first_seen_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": [_greenhouse_job("1", "Graduate Software Engineer")]}):
                _run_ingest_registry(registry, store)
            first_seen = store.read_jobs()[0].source_observations[0].first_seen_at
            first_synced = store.read_source_health()["greenhouse"]["last_synced"]

            with patch(
                "jobintel.connectors.greenhouse.fetch_json",
                return_value={"jobs": [_greenhouse_job("1", "Graduate Software Engineer"), _greenhouse_job("2", "Graduate Data Engineer")]},
            ):
                _run_ingest_registry(registry, store)

            jobs = store.read_jobs()
            health = store.read_source_health()["greenhouse"]

        self.assertEqual(len(jobs), 2)
        self.assertEqual(health["jobs_seen"], 2)
        self.assertGreaterEqual(health["last_synced"], first_synced)
        job_one = next(job for job in jobs if job.source_observations[0].source_job_id == "1")
        # Existing ingestion semantics preserved: first_seen_at unchanged,
        # last_seen_at advanced, not re-created as a new observation.
        self.assertEqual(job_one.source_observations[0].first_seen_at, first_seen)
        self.assertGreaterEqual(job_one.source_observations[0].last_seen_at, first_seen)
        self.assertEqual(job_one.source_observations[0].latest_observed_state, "UNCHANGED")


class ExcludedCompaniesRemainExcludedTests(unittest.TestCase):
    def test_manually_excluded_company_is_never_fetched_via_ingest_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(
                tmp,
                [
                    _greenhouse_entry(
                        "Excluded Co",
                        "excluded",
                        manually_excluded=True,
                        exclusion_reason="test exclusion",
                        excluded_at="2026-09-17T00:00:00+00:00",
                    )
                ],
            )
            store = LocalJobStore(Path(tmp) / "store")

            def fail_if_called(url):
                raise AssertionError("must not fetch a manually excluded company via ingest-registry")

            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=fail_if_called):
                companies = load_company_registry(registry)
                connectors = connectors_from_registry(companies)

        self.assertEqual(connectors, [])


class OneCompanyFailureDoesNotHideAnotherCompanysSuccessTests(unittest.TestCase):
    def test_partial_failure_across_two_companies_on_the_same_ats_preserves_the_good_ones_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Good Co", "good"), _greenhouse_entry("Bad Co", "bad")])
            store = LocalJobStore(Path(tmp) / "store")

            def fake_fetch(url: str):
                if "/bad/" in url:
                    raise urllib.error.URLError("bad board unreachable")
                return {"jobs": [_greenhouse_job("1")]}

            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=fake_fetch):
                results = _run_ingest_registry(registry, store)

            health = store.read_source_health()["greenhouse"]
            jobs = store.read_jobs()

        # Status is PARTIAL, not a misleadingly healthy "live_api" that hides
        # Bad Co's failure, and not a total "error" that hides Good Co's
        # success either.
        self.assertEqual(results[0].status, "partial")
        self.assertEqual(health["status"], "partial")
        self.assertIn("bad", health["failed_identifiers"])
        # Good Co's job is still persisted, not silently dropped because a
        # sibling company on the same ATS connector failed.
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].source_observations[0].active)


if __name__ == "__main__":
    unittest.main()
