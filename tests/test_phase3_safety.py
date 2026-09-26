from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jobintel.config import default_ranking_config
from jobintel.dashboard.operations import build_deadline_view
from jobintel.matching.cv_generation import generate_and_save_cv
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.pipeline.daily import run_daily_refresh
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.storage.watchlist_store import WatchlistStore
from jobintel.fixtures.sample_data import sample_candidate

# Phase 3 Section O: dedicated regression coverage for the explicit
# production-safety interactions this phase called out, focused on the NEW
# Phase 3 code paths specifically (daily-refresh orchestration, the manual
# watchlist, the CV workbench) -- the underlying guarantees these build on
# (PartialFetchError isolation, manual-exclusion persistence, graduation-year
# permissiveness) already have dedicated coverage from earlier phases
# (test_source_health.py, test_manual_exclusion.py,
# test_graduation_year_distinction.py) and are not re-tested here.


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


def _greenhouse_job(job_id: str, title: str) -> dict:
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/demo/jobs/{job_id}",
        "updated_at": "2026-09-01T09:00:00+00:00",
        "content": "<p>Build Python backend services.</p>",
        "offices": [{"location": "London, United Kingdom"}],
    }


def _job(job_id: str = "1") -> Job:
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return Job(
        id=job_id,
        title="Graduate Software Engineer",
        company="Example Co",
        description="Build backend services using Python.",
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name="company",
                source_job_id=job_id,
                original_url="https://example.com/jobs/1",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description="Build backend services using Python.",
                canonical_application_url="https://example.com/jobs/1/apply",
            )
        ],
    )


class FailedRefreshDoesNotCloseJobsViaDailyRefreshTests(unittest.TestCase):
    def test_a_failed_source_within_daily_refresh_never_closes_that_sources_previously_seen_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")

            with (
                patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": [_greenhouse_job("1", "Graduate Software Engineer")]}),
                patch("jobintel.connectors.adzuna.fetch_json", return_value={"count": 0, "results": []}),
                patch("jobintel.connectors.prospects.fetch_text", return_value="<html><body></body></html>"),
                patch.dict("os.environ", {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}),
            ):
                run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)

            with (
                patch("jobintel.connectors.greenhouse.fetch_json", side_effect=urllib.error.URLError("network down")),
                patch("jobintel.connectors.adzuna.fetch_json", return_value={"count": 0, "results": []}),
                patch("jobintel.connectors.prospects.fetch_text", return_value="<html><body></body></html>"),
                patch.dict("os.environ", {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}),
            ):
                report = run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)

            jobs = store.read_jobs()

        self.assertIn("greenhouse", report.sources_failed)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].source_observations[0].active)


class ManualExclusionSurvivesDailyRefreshTests(unittest.TestCase):
    def test_a_manually_excluded_company_is_never_re_enabled_by_daily_refresh(self) -> None:
        from jobintel.ats_discovery import exclude_company_entry

        with tempfile.TemporaryDirectory() as tmp:
            excluded = exclude_company_entry(_greenhouse_entry("Excluded Co", "excluded"), "test exclusion reason")
            registry = _registry(tmp, [excluded])
            store = LocalJobStore(Path(tmp) / "store")

            def fail_if_fetched(url):
                raise AssertionError("must never fetch a manually excluded company, even via daily-refresh")

            with (
                patch("jobintel.connectors.greenhouse.fetch_json", side_effect=fail_if_fetched),
                patch("jobintel.connectors.adzuna.fetch_json", return_value={"count": 0, "results": []}),
                patch("jobintel.connectors.prospects.fetch_text", return_value="<html><body></body></html>"),
                patch.dict("os.environ", {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}),
            ):
                run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)

            final = json.loads(registry.read_text(encoding="utf-8"))

        self.assertTrue(final[0]["manually_excluded"])
        self.assertFalse(final[0]["enabled"])


class DuplicateObservationDoesNotDuplicateApplicationTests(unittest.TestCase):
    def test_discovering_the_same_job_twice_never_auto_creates_or_duplicates_an_application(self) -> None:
        # Applications are only ever created by an explicit human action (the
        # dashboard's "Create application record" button / `application-
        # create` CLI command) -- discovery/re-discovery must never silently
        # create or multiply one.
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_greenhouse_entry("Demo Co", "demo")])
            store = LocalJobStore(Path(tmp) / "store")
            app_store = ApplicationStore(store.root)

            for _ in range(2):
                with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": [_greenhouse_job("1", "Graduate Software Engineer")]}):
                    from jobintel.pipeline.refresh import refresh_sources

                    refresh_sources(registry_path=registry, store=store, config=default_ranking_config(), limit=10)

            applications = app_store.read_applications()

        self.assertEqual(applications, [])

    def test_attaching_a_cv_twice_to_the_same_application_does_not_create_a_second_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            app_store = ApplicationStore(store.root)
            cv_store = CVArtifactStore(store.root)
            from jobintel.models.application import Application

            app_store.create_application(Application(id="app-1", job_id="1", company="Example Co", role_title="Graduate Software Engineer"))
            first_cv = generate_and_save_cv(sample_candidate(), _job(), cv_store, store_root=store.root)
            second_cv = generate_and_save_cv(sample_candidate(), _job(), cv_store, store_root=store.root)
            app_store.attach_cv("app-1", first_cv.id, [], [])
            app_store.attach_cv("app-1", second_cv.id, [], [])

            applications = app_store.read_applications()

        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0].cv_version_id, second_cv.id)


class SubmittedCvArtifactsAreImmutableTests(unittest.TestCase):
    def test_cv_artifact_store_exposes_no_update_or_delete_method(self) -> None:
        # Structural guarantee: append-only by construction, not just by
        # convention -- there is no method that could overwrite history.
        public_methods = {name for name in dir(CVArtifactStore) if not name.startswith("_") and callable(getattr(CVArtifactStore, name))}
        self.assertEqual(public_methods, {"save", "get", "for_job", "for_application", "read_all"})

    def test_regenerating_a_cv_for_the_same_job_does_not_touch_the_earlier_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cv_store = CVArtifactStore(tmp)
            first = generate_and_save_cv(sample_candidate(), _job(), cv_store, store_root=tmp)
            generate_and_save_cv(sample_candidate(), _job(), cv_store, store_root=tmp)

            still_there = cv_store.get(first.id)

        self.assertIsNotNone(still_there)
        self.assertEqual(still_there.cv_text, first.cv_text)


class ApplicationHistoryIsAppendOnlyTests(unittest.TestCase):
    def test_application_store_status_history_has_no_delete_or_update_method(self) -> None:
        public_methods = {name for name in dir(ApplicationStore) if not name.startswith("_") and callable(getattr(ApplicationStore, name))}
        # create_application/update_status/attach_cv all APPEND a history
        # event; for_job/read_applications/status_history_for are read-only.
        # No method lets a past event be edited or removed.
        self.assertEqual(public_methods, {"create_application", "update_status", "attach_cv", "for_job", "read_applications", "status_history_for"})

    def test_every_status_transition_is_preserved_not_just_the_latest(self) -> None:
        from jobintel.models.application import Application
        from jobintel.models.taxonomy import ApplicationStatus

        with tempfile.TemporaryDirectory() as tmp:
            app_store = ApplicationStore(tmp)
            app_store.create_application(Application(id="app-1", job_id="1", company="Example Co", role_title="x"))
            app_store.update_status("app-1", ApplicationStatus.APPLIED)
            app_store.update_status("app-1", ApplicationStatus.ONLINE_ASSESSMENT)
            app_store.update_status("app-1", ApplicationStatus.REJECTED)

            history = app_store.status_history_for("app-1")

        self.assertEqual([event.to_status.value for event in history], ["discovered", "applied", "online_assessment", "rejected"])


class UnknownDeadlineNeverGuessedTests(unittest.TestCase):
    def test_a_store_with_zero_deadlines_recorded_anywhere_does_not_crash_and_buckets_everything_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_jobs([_job("1"), _job("2")])
            buckets = build_deadline_view(store, default_ranking_config())

        self.assertEqual(len(buckets["unknown"]), 2)
        for bucket in ("overdue", "today", "within_48h", "within_7d", "later"):
            self.assertEqual(buckets[bucket], [])


class ManualCheckDoesNotImplyAutomationSuccessTests(unittest.TestCase):
    def test_marking_a_company_checked_on_the_watchlist_never_touches_its_registry_verification_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [{"company_name": "Unresolved Co", "industry": "SaaS", "priority": 1, "connector_type": None, "enabled": False, "verification_status": "pending_ats_discovery"}])
            store = LocalJobStore(Path(tmp) / "store")
            watchlist_store = WatchlistStore(store.root)

            before = json.loads(registry.read_text(encoding="utf-8"))
            watchlist_store.record_check("Unresolved Co", "no relevant jobs found by me manually")
            after = json.loads(registry.read_text(encoding="utf-8"))

        # The registry file itself -- the source of truth for automation
        # state -- is completely untouched by a manual watchlist check.
        self.assertEqual(before, after)
        self.assertEqual(after[0]["verification_status"], "pending_ats_discovery")
        self.assertFalse(after[0]["enabled"])

    def test_marking_a_source_checked_never_touches_source_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            watchlist_store = WatchlistStore(store.root)

            watchlist_store.record_check("trackr", "browsed the-trackr.com manually, nothing relevant")
            health = store.read_source_health()

        # A manual check of Trackr must never be recorded as if Trackr's own
        # automation succeeded -- source_health stays completely separate
        # from watchlist check state.
        self.assertNotIn("trackr", health)


if __name__ == "__main__":
    unittest.main()
