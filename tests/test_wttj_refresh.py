from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jobintel.config import default_ranking_config
from jobintel.pipeline.refresh import refresh_sources
from jobintel.storage.local_store import LocalJobStore


class WelcomeToTheJungleRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)

    def test_successful_wttj_api_refresh_records_live_health_and_new_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_wttj_entry()])
            store = LocalJobStore(Path(tmp) / "store")

            with patch.dict("os.environ", {"WTTJ_API_KEY": "secret"}), patch(
                "jobintel.connectors.welcome_to_the_jungle.fetch_json",
                return_value={"jobs": [_wttj_payload("wk_1", description="Build Python APIs.")]},
            ):
                results = refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            jobs = store.read_jobs()
            health = store.read_source_health()["welcome_to_the_jungle"]
            registry_payload = json.loads(registry.read_text(encoding="utf-8"))

        self.assertEqual(results[0].status, "live_api")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source_observations[0].latest_observed_state, "NEW")
        self.assertTrue(jobs[0].source_observations[0].active)
        self.assertEqual(health["status"], "live_api")
        self.assertEqual(health["jobs_active"], 1)
        self.assertEqual(registry_payload[0]["verification_status"], "verified")
        self.assertEqual(registry_payload[0]["jobs_active"], 1)

    def test_repeated_wttj_refresh_marks_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_wttj_entry()])
            store = LocalJobStore(Path(tmp) / "store")
            payload = {"jobs": [_wttj_payload("wk_1", description="Build Python APIs.")]}

            with patch.dict("os.environ", {"WTTJ_API_KEY": "secret"}), patch(
                "jobintel.connectors.welcome_to_the_jungle.fetch_json",
                return_value=payload,
            ):
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            observation = store.read_jobs()[0].source_observations[0]

        self.assertEqual(observation.latest_observed_state, "UNCHANGED")
        self.assertGreaterEqual(observation.last_seen_at, observation.first_seen_at)

    def test_changed_wttj_job_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_wttj_entry()])
            store = LocalJobStore(Path(tmp) / "store")

            with patch.dict("os.environ", {"WTTJ_API_KEY": "secret"}), patch(
                "jobintel.connectors.welcome_to_the_jungle.fetch_json",
                side_effect=[
                    {"jobs": [_wttj_payload("wk_1", description="Build Python APIs.")]},
                    {"jobs": [_wttj_payload("wk_1", description="Build Python APIs and data services.")]},
                ],
            ):
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            job = store.read_jobs()[0]

        self.assertEqual(job.source_observations[0].latest_observed_state, "CHANGED")
        self.assertIn("data services", job.description)

    def test_disappeared_wttj_job_is_marked_inactive_after_reachable_empty_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_wttj_entry()])
            store = LocalJobStore(Path(tmp) / "store")

            with patch.dict("os.environ", {"WTTJ_API_KEY": "secret"}), patch(
                "jobintel.connectors.welcome_to_the_jungle.fetch_json",
                side_effect=[
                    {"jobs": [_wttj_payload("wk_1", description="Build Python APIs.")]},
                    {"jobs": []},
                ],
            ):
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            observation = store.read_jobs()[0].source_observations[0]

        self.assertFalse(observation.active)
        self.assertEqual(observation.latest_observed_state, "DISAPPEARED")

    def test_reappeared_wttj_job_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_wttj_entry()])
            store = LocalJobStore(Path(tmp) / "store")

            with patch.dict("os.environ", {"WTTJ_API_KEY": "secret"}), patch(
                "jobintel.connectors.welcome_to_the_jungle.fetch_json",
                side_effect=[
                    {"jobs": [_wttj_payload("wk_1", description="Build Python APIs.")]},
                    {"jobs": []},
                    {"jobs": [_wttj_payload("wk_1", description="Build Python APIs.")]},
                ],
            ):
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)
                refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            observation = store.read_jobs()[0].source_observations[0]

        self.assertTrue(observation.active)
        self.assertEqual(observation.latest_observed_state, "REAPPEARED")

    def test_missing_wttj_api_key_marks_auth_required_without_failing_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_wttj_entry()])
            store = LocalJobStore(Path(tmp) / "store")

            with patch.dict("os.environ", {}, clear=True):
                results = refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            health = store.read_source_health()["welcome_to_the_jungle"]
            registry_payload = json.loads(registry.read_text(encoding="utf-8"))

        self.assertEqual(results[0].status, "auth_required")
        self.assertEqual(health["status"], "auth_required")
        self.assertIn("WTTJ_API_KEY", health["last_error"])
        self.assertEqual(registry_payload[0]["verification_status"], "auth_required")

    def test_wttj_api_auth_failure_is_reported_as_auth_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_wttj_entry()])
            store = LocalJobStore(Path(tmp) / "store")
            error = urllib.error.HTTPError("https://www.welcomekit.co/api/v1/external/jobs", 401, "Unauthorized", None, None)

            with patch.dict("os.environ", {"WTTJ_API_KEY": "bad"}), patch(
                "jobintel.connectors.welcome_to_the_jungle.fetch_json",
                side_effect=error,
            ):
                results = refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            health = store.read_source_health()["welcome_to_the_jungle"]

        self.assertEqual(results[0].status, "auth_required")
        self.assertEqual(health["status"], "auth_required")
        self.assertEqual(store.read_jobs(), [])

    def test_other_sources_continue_when_wttj_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_wttj_entry(), _ashby_entry()])
            store = LocalJobStore(Path(tmp) / "store")
            error = urllib.error.HTTPError("https://www.welcomekit.co/api/v1/external/jobs", 403, "Forbidden", None, None)
            ashby_payload = {
                "jobs": [
                    {
                        "id": "ashby_1",
                        "title": "Junior Backend Engineer",
                        "organizationName": "Ashby Demo",
                        "jobUrl": "https://jobs.ashbyhq.com/ashby-demo/ashby_1",
                        "applicationUrl": "https://jobs.ashbyhq.com/ashby-demo/ashby_1/application",
                        "descriptionPlain": "Build Python services.",
                        "location": {"name": "London, United Kingdom"},
                    }
                ]
            }

            with patch.dict("os.environ", {"WTTJ_API_KEY": "bad"}), patch(
                "jobintel.connectors.welcome_to_the_jungle.fetch_json",
                side_effect=error,
            ), patch("jobintel.connectors.ashby.fetch_json", return_value=ashby_payload):
                results = refresh_sources(registry, store=store, config=default_ranking_config(), limit=10)

            jobs = store.read_jobs()
            statuses = {result.source_name: result.status for result in results}

        self.assertEqual(statuses["welcome_to_the_jungle"], "auth_required")
        self.assertEqual(statuses["ashby"], "live_api")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source_observations[0].source_name, "ashby")


def _registry(tmp: str, entries: list[dict]) -> Path:
    path = Path(tmp) / "target_companies.json"
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def _wttj_entry() -> dict:
    return {
        "company_name": "WTTJ Demo",
        "industry": "SaaS",
        "priority": 1,
        "connector_type": "welcome_to_the_jungle",
        "connector_token": "demo-org",
        "welcome_to_the_jungle_organization_reference": "demo-org",
        "enabled": True,
        "verification_status": "verified",
        "notes": "preserve this note",
    }


def _ashby_entry() -> dict:
    return {
        "company_name": "Ashby Demo",
        "industry": "SaaS",
        "priority": 1,
        "connector_type": "ashby",
        "connector_token": "ashby-demo",
        "enabled": True,
        "verification_status": "verified",
    }


def _wttj_payload(reference: str, description: str) -> dict:
    return {
        "reference": reference,
        "name": "Junior Backend Engineer",
        "organization_reference": "demo-org",
        "organization": {"name": "WTTJ Demo"},
        "apply_url": f"https://apply.example/{reference}",
        "url": f"https://www.welcometothejungle.com/en/companies/demo/jobs/{reference}_london",
        "description": description,
        "profile": "Graduate or junior engineers welcome.",
        "remote": "hybrid",
        "office": {"city": "London", "country": "United Kingdom"},
        "published_at": "2026-09-01T09:00:00+00:00",
    }


if __name__ == "__main__":
    unittest.main()
