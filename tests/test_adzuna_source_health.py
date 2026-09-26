from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from jobintel.config import default_ranking_config
from jobintel.connectors.adzuna import AdzunaConnector
from jobintel.connectors.base import ConnectorQuery
from jobintel.pipeline.refresh import refresh_connector
from jobintel.source_coverage import coverage_today
from jobintel.storage.local_store import LocalJobStore

# Regression coverage for the Phase 2.7.3 fix: `ingest --adzuna` previously
# went through `ingest_from_connectors`, which never wrote a source_health
# record at all -- so `coverage-today` could report Adzuna as "never
# checked" indefinitely, regardless of whether a real refresh had just
# succeeded or failed. The fix routes Adzuna (and every other `ingest`
# connector) through the SAME `refresh_connector` used by the registry-based
# `refresh_sources` path, so this test suite exercises `refresh_connector`
# directly with an AdzunaConnector -- that IS the real fix, not a simulation
# of it.


def _page(ids: list[str], count: int) -> dict:
    return {
        "count": count,
        "results": [
            {
                "id": job_id,
                "title": "Graduate Software Engineer",
                "company": {"display_name": "Demo Co"},
                "location": {"display_name": "London"},
                "redirect_url": f"https://adzuna.example/job/{job_id}",
                "created": "2026-09-01T09:00:00Z",
            }
            for job_id in ids
        ],
    }


class AdzunaSuccessfulRefreshUpdatesHealthTests(unittest.TestCase):
    def test_successful_refresh_writes_live_api_health_with_real_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector("id", "key")
            with patch("jobintel.connectors.adzuna.fetch_json", return_value=_page(["1", "2", "3"], 3)):
                result = refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))

            health = store.read_source_health()["adzuna"]

        self.assertEqual(result.status, "live_api")
        self.assertEqual(health["status"], "live_api")
        self.assertIsNotNone(health["last_synced"])
        self.assertEqual(health["jobs_seen"], 3)
        self.assertEqual(health["last_successful_jobs_seen"], 3)
        self.assertIsNone(health["last_error"])


class AdzunaMissingCredentialsTests(unittest.TestCase):
    def test_missing_credentials_writes_explicit_auth_required_health_not_generic_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector(None, None)
            result = refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))

            health = store.read_source_health()["adzuna"]

        self.assertEqual(result.status, "auth_required")
        self.assertEqual(health["status"], "auth_required")
        self.assertIn("ADZUNA_APP_ID", health["last_error"])
        # Never silently 0 jobs -- the failure is visible, jobs_seen distinct
        # from a genuine empty-but-successful refresh.
        self.assertEqual(health["jobs_seen"], 0)
        self.assertIsNone(health["last_successful_jobs_seen"])

    def test_missing_credentials_never_crashes_without_recording_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector(None, None)
            # Must not raise -- the whole point of refresh_connector is to
            # convert this into a recorded health state, not propagate.
            refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))

            self.assertIn("adzuna", store.read_source_health())


class AdzunaApiNetworkFailureTests(unittest.TestCase):
    def test_total_api_failure_writes_error_health_distinct_from_auth_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector("id", "key")
            with patch("jobintel.connectors.adzuna.fetch_json", side_effect=urllib.error.URLError("network down")):
                result = refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))

            health = store.read_source_health()["adzuna"]

        self.assertEqual(result.status, "error")
        self.assertEqual(health["status"], "error")
        self.assertIn("network down", health["last_error"])

    def test_http_401_from_the_real_api_is_reported_as_auth_required(self) -> None:
        # A locally-missing env var isn't the only way credentials can be
        # invalid -- Adzuna itself could reject a configured-but-wrong
        # app_id/app_key with an HTTP 401. That's also a configuration
        # failure, not a generic network error.
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector("bad-id", "bad-key")

            def raise_401(url):
                raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

            with patch("jobintel.connectors.adzuna.fetch_json", side_effect=raise_401):
                result = refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))

        self.assertEqual(result.status, "auth_required")


class AdzunaPartialPaginationVisibilityTests(unittest.TestCase):
    def test_partial_page_failure_is_visible_as_partial_status_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector("id", "key")

            def fake_fetch(url: str):
                page = int(url.split("/search/")[1].split("?")[0])
                if page == 1:
                    return _page([str(i) for i in range(50)], 120)
                raise urllib.error.URLError("rate limited")

            with patch("jobintel.connectors.adzuna.fetch_json", side_effect=fake_fetch):
                result = refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=120))

            health = store.read_source_health()["adzuna"]

        self.assertEqual(result.status, "partial")
        self.assertEqual(health["status"], "partial")
        self.assertTrue(health["failed_identifiers"])
        # Page 1's 50 real jobs must not be discarded just because a later
        # page failed.
        self.assertEqual(health["jobs_seen"], 50)


class AdzunaZeroJobsIsDistinguishableFromFailureTests(unittest.TestCase):
    def test_genuinely_zero_results_after_success_is_not_confused_with_a_failed_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector("id", "key")
            with patch("jobintel.connectors.adzuna.fetch_json", return_value=_page([], 0)):
                result = refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))

            health = store.read_source_health()["adzuna"]

        self.assertEqual(result.status, "live_api")
        self.assertEqual(health["status"], "live_api")
        self.assertEqual(health["jobs_seen"], 0)
        self.assertIsNone(health["last_error"])
        # A genuine zero-result success still has a status field that a
        # failed refresh (status in {error, auth_required, partial}) never
        # shares -- the two are never representable identically.
        self.assertNotIn(health["status"], {"error", "auth_required", "partial"})


class AdzunaRepeatedRefreshTests(unittest.TestCase):
    def test_repeated_successful_refresh_updates_timestamp_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector("id", "key")
            with patch("jobintel.connectors.adzuna.fetch_json", return_value=_page(["1", "2"], 2)):
                refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))
            first_synced = store.read_source_health()["adzuna"]["last_synced"]

            with patch("jobintel.connectors.adzuna.fetch_json", return_value=_page(["1", "2", "3", "4"], 4)):
                refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))
            second_health = store.read_source_health()["adzuna"]

        self.assertEqual(second_health["jobs_seen"], 4)
        self.assertEqual(second_health["last_successful_jobs_seen"], 4)
        self.assertGreaterEqual(second_health["last_synced"], first_synced)

    def test_a_failed_refresh_after_a_successful_one_preserves_last_synced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector("id", "key")
            with patch("jobintel.connectors.adzuna.fetch_json", return_value=_page(["1"], 1)):
                refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))
            good_synced = store.read_source_health()["adzuna"]["last_synced"]

            with patch("jobintel.connectors.adzuna.fetch_json", side_effect=urllib.error.URLError("down")):
                refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))
            after_failure = store.read_source_health()["adzuna"]

        self.assertEqual(after_failure["status"], "error")
        # CRITICAL: must not regress to "never synced" just because today failed.
        self.assertEqual(after_failure["last_synced"], good_synced)
        self.assertEqual(after_failure["last_successful_jobs_seen"], 1)


class CoverageTodayReflectsAdzunaRefreshAttemptsTests(unittest.TestCase):
    def test_adzuna_is_no_longer_never_checked_after_a_successful_refresh_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector("id", "key")
            with patch("jobintel.connectors.adzuna.fetch_json", return_value=_page(["1"], 1)):
                refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))

            report = coverage_today(store, registry_entries=[])

        checked_names = {entry.source_name for entry in report.automatically_checked_today}
        failed_names = {entry.source_name for entry in report.failed_or_incomplete_today}
        self.assertIn("adzuna", checked_names)
        self.assertNotIn("adzuna", failed_names)

    def test_adzuna_shows_as_failed_today_not_never_checked_after_a_failed_refresh_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            connector = AdzunaConnector(None, None)
            refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(limit=10))

            report = coverage_today(store, registry_entries=[])

        failed_entries = {entry.source_name: entry.detail for entry in report.failed_or_incomplete_today}
        self.assertIn("adzuna", failed_entries)
        # Distinguishable from the generic "not refreshed today" message a
        # source that was never even attempted would show.
        self.assertNotEqual(failed_entries["adzuna"], "not refreshed today (no successful or failed run recorded for today)")


if __name__ == "__main__":
    unittest.main()
