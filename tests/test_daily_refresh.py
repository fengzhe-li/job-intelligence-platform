from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from jobintel.config import default_ranking_config
from jobintel.pipeline.daily import run_daily_refresh
from jobintel.storage.local_store import LocalJobStore

NO_JSON_LD_HTML = "<html><body>no structured data</body></html>"


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


def _greenhouse_job(job_id: str, title: str) -> dict:
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/demo/jobs/{job_id}",
        "updated_at": "2026-09-01T09:00:00+00:00",
        "content": "<p>Build Python backend services.</p>",
        "offices": [{"location": "London, United Kingdom"}],
    }


class DailyRefreshOrchestratesEverySourceTests(unittest.TestCase):
    def test_successful_registry_and_adzuna_and_prospects_all_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with (
                patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": [_greenhouse_job("1", "Graduate Software Engineer")]}),
                patch("jobintel.connectors.adzuna.fetch_json", return_value={"count": 0, "results": []}),
                patch("jobintel.connectors.prospects.fetch_text", return_value="<html><body></body></html>"),
                patch.dict("os.environ", {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}),
            ):
                report = run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)

        self.assertIn("greenhouse", report.sources_succeeded)
        self.assertIn("adzuna", report.sources_succeeded)
        self.assertIn("prospects", report.sources_succeeded)
        self.assertEqual(report.sources_failed, [])
        self.assertEqual(report.sources_not_configured, [])
        self.assertGreaterEqual(report.new_jobs, 1)

    def test_manual_only_sources_never_appear_in_any_automated_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [])
            store = LocalJobStore(Path(tmp) / "store")

            with (
                patch("jobintel.connectors.adzuna.fetch_json", return_value={"count": 0, "results": []}),
                patch("jobintel.connectors.prospects.fetch_text", return_value="<html><body></body></html>"),
                patch.dict("os.environ", {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}),
            ):
                report = run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)

        all_named_sources = set(report.sources_succeeded) | set(report.sources_partial) | set(report.sources_failed) | set(report.sources_not_configured)
        for manual_only in ("trackr", "gradcracker", "bright_network"):
            self.assertNotIn(manual_only, all_named_sources)


class DailyRefreshHandlesMissingCredentialsHonestlyTests(unittest.TestCase):
    def test_unconfigured_adzuna_is_not_configured_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [])
            store = LocalJobStore(Path(tmp) / "store")

            with (
                patch("jobintel.connectors.prospects.fetch_text", return_value="<html><body></body></html>"),
                patch.dict("os.environ", {}, clear=True),
            ):
                report = run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)

        self.assertIn("adzuna", report.sources_not_configured)
        self.assertNotIn("adzuna", report.sources_failed)


class DailyRefreshToleratesOneSourceFailingTests(unittest.TestCase):
    def test_one_failing_registry_source_does_not_stop_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Good Co", "good")])
            store = LocalJobStore(Path(tmp) / "store")

            with (
                patch("jobintel.connectors.greenhouse.fetch_json", side_effect=urllib.error.URLError("network down")),
                patch("jobintel.connectors.adzuna.fetch_json", return_value={"count": 1, "results": [{"id": "a1", "title": "Graduate Software Engineer", "company": {"display_name": "Acme"}, "location": {"display_name": "London"}, "redirect_url": "https://adzuna.example/a1", "created": "2026-09-01T09:00:00Z"}]}),
                patch("jobintel.connectors.prospects.fetch_text", return_value="<html><body></body></html>"),
                patch.dict("os.environ", {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}),
            ):
                report = run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)

        self.assertIn("greenhouse", report.sources_failed)
        self.assertIn("adzuna", report.sources_succeeded)
        self.assertIn("prospects", report.sources_succeeded)


class DailyRefreshSummaryReflectsRealStateTests(unittest.TestCase):
    def test_manual_checks_due_reflects_the_real_registry_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [{"company_name": "Unresolved Co", "industry": "SaaS", "priority": 1, "connector_type": None, "enabled": False, "verification_status": "pending_ats_discovery"}])
            store = LocalJobStore(Path(tmp) / "store")

            with (
                patch("jobintel.connectors.adzuna.fetch_json", return_value={"count": 0, "results": []}),
                patch("jobintel.connectors.prospects.fetch_text", return_value="<html><body></body></html>"),
                patch.dict("os.environ", {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}),
            ):
                report = run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)

        # 1 unresolved company + trackr/gradcracker/bright_network (always
        # manual) + prospects/adzuna (partial-coverage sources needing
        # periodic cross-check) = 6.
        self.assertEqual(report.manual_checks_due, 6)

    def test_report_has_a_finished_at_after_started_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [])
            store = LocalJobStore(Path(tmp) / "store")

            with (
                patch("jobintel.connectors.adzuna.fetch_json", return_value={"count": 0, "results": []}),
                patch("jobintel.connectors.prospects.fetch_text", return_value="<html><body></body></html>"),
                patch.dict("os.environ", {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}),
            ):
                report = run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)

        self.assertGreaterEqual(report.finished_at, report.started_at)


if __name__ == "__main__":
    unittest.main()
