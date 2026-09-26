from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobintel.config import default_ranking_config
from jobintel.fixtures.sample_data import sample_candidate
from jobintel.dashboard.operations import (
    ACTIVE_AND_REFRESHED,
    ACTIVE_NOT_REFRESHED,
    NOT_CONFIGURED,
    SOURCE_FAILED,
    SOURCE_MANUAL_ONLY,
    SOURCE_STALE,
    build_action_queue,
    build_application_detail,
    build_company_health_view,
    build_deadline_view,
    build_source_health_view,
    build_today_summary,
    explain_priority,
)
from jobintel.matching.matcher import MatchResult, match_job
from jobintel.models.application import Application
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import ApplicationStatus, ReviewGateStatus, WorkflowStatus
from jobintel.pipeline.ingestion import RankedJob
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.local_store import LocalJobStore

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _job(job_id: str, title: str = "Graduate Software Engineer", deadline: datetime | None = None, first_seen: datetime | None = None, workflow_status: WorkflowStatus = WorkflowStatus.NEW) -> Job:
    seen = first_seen or (NOW - timedelta(days=3))
    return Job(
        id=job_id,
        title=title,
        company="Acme",
        description="Build Python backend services with AWS and Docker.",
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name="greenhouse",
                source_job_id=job_id,
                original_url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
                first_seen_at=seen,
                last_seen_at=seen,
                posted_at=seen,
                raw_description="Build Python backend services with AWS and Docker.",
                canonical_application_url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
                deadline=deadline,
            )
        ],
        workflow_status=workflow_status,
    )


def _ranked(job: Job) -> RankedJob:
    match = match_job(sample_candidate(), job, default_ranking_config(), NOW)
    return RankedJob(job=job, match=match)


class ExplainPriorityUnknownDeadlineTests(unittest.TestCase):
    def test_unknown_deadline_adds_no_boost_and_no_penalty(self) -> None:
        job_with_deadline = _job("1", deadline=NOW + timedelta(days=1))
        job_without_deadline = _job("2", deadline=None)

        score_with, reasons_with = explain_priority(_ranked(job_with_deadline), None, None, NOW)
        score_without, reasons_without = explain_priority(_ranked(job_without_deadline), None, None, NOW)

        self.assertGreater(score_with, score_without)
        self.assertFalse(any("deadline" in reason for reason in reasons_without))

    def test_never_fabricates_a_deadline_reason_when_none_exists(self) -> None:
        job = _job("1", deadline=None)
        _, reasons = explain_priority(_ranked(job), None, None, NOW)
        self.assertFalse(any("day" in reason.lower() for reason in reasons))


class ExplainPriorityDeadlineUrgencyTests(unittest.TestCase):
    def test_deadline_within_2_days_gets_the_urgent_boost(self) -> None:
        job = _job("1", deadline=NOW + timedelta(days=1))
        score, reasons = explain_priority(_ranked(job), None, None, NOW)
        self.assertTrue(any("deadline in 1 day" in r for r in reasons))

    def test_deadline_within_7_days_gets_a_smaller_boost_than_urgent(self) -> None:
        soon = _job("1", deadline=NOW + timedelta(days=1))
        later = _job("2", deadline=NOW + timedelta(days=6))
        score_soon, _ = explain_priority(_ranked(soon), None, None, NOW)
        score_later, _ = explain_priority(_ranked(later), None, None, NOW)
        self.assertGreater(score_soon, score_later)


class ExplainPriorityActionabilityTests(unittest.TestCase):
    def test_not_yet_applied_boosts_and_is_named(self) -> None:
        job = _job("1")
        _, reasons = explain_priority(_ranked(job), None, None, NOW)
        self.assertIn("not yet applied", reasons)

    def test_already_applied_does_not_get_the_not_yet_applied_boost(self) -> None:
        job = _job("1")
        application = Application(id="app-1", job_id="1", company="Acme", role_title="x", status=ApplicationStatus.APPLIED)
        _, reasons = explain_priority(_ranked(job), application, None, NOW)
        self.assertNotIn("not yet applied", reasons)

    def test_auto_prepare_cv_gets_ready_reason(self) -> None:
        from jobintel.models.cv_artifact import GeneratedCVArtifact

        job = _job("1")
        cv = GeneratedCVArtifact(
            id="cv-1", job_id="1", candidate_id="c", generated_at=NOW, jd_snapshot="", selected_project_ids=[], evidence_ids_used=[],
            bullets=[], skills_included=[], cv_text="", pdf_path=None, page_count=1, fits_one_page=True,
            render_margin_mm=18.0, render_body_pt=10.0, review_status=ReviewGateStatus.AUTO_PREPARE,
        )
        _, reasons = explain_priority(_ranked(job), None, cv, NOW)
        self.assertIn("CV ready", reasons)

    def test_review_required_cv_is_never_described_as_ready(self) -> None:
        from jobintel.models.cv_artifact import GeneratedCVArtifact

        job = _job("1")
        cv = GeneratedCVArtifact(
            id="cv-1", job_id="1", candidate_id="c", generated_at=NOW, jd_snapshot="", selected_project_ids=[], evidence_ids_used=[],
            bullets=[], skills_included=[], cv_text="", pdf_path=None, page_count=1, fits_one_page=True,
            render_margin_mm=18.0, render_body_pt=10.0, review_status=ReviewGateStatus.REVIEW_REQUIRED,
        )
        _, reasons = explain_priority(_ranked(job), None, cv, NOW)
        self.assertNotIn("CV ready", reasons)
        self.assertTrue(any("review required" in r for r in reasons))


class DeadlineViewBucketingTests(unittest.TestCase):
    def test_jobs_without_deadline_go_to_unknown_never_a_guessed_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_jobs([_job("1", deadline=None)])
            buckets = build_deadline_view(store, now=NOW)

        self.assertEqual(len(buckets["unknown"]), 1)
        for name in ("overdue", "today", "within_48h", "within_7d", "later"):
            self.assertEqual(len(buckets[name]), 0)

    def test_overdue_deadline_is_bucketed_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_jobs([_job("1", deadline=NOW - timedelta(days=1))])
            buckets = build_deadline_view(store, now=NOW)

        self.assertEqual(len(buckets["overdue"]), 1)

    def test_deadline_conflict_is_surfaced_not_silently_resolved(self) -> None:
        job = Job(
            id="1",
            title="Graduate Software Engineer",
            company="Acme",
            description="x",
            locations=[Location(city="London", country="United Kingdom")],
            source_observations=[
                SourceObservation(source_name="trackr", source_job_id="1", original_url="u1", first_seen_at=NOW, last_seen_at=NOW, posted_at=NOW, raw_description="x", canonical_application_url="a1", deadline=NOW + timedelta(days=10)),
                SourceObservation(source_name="greenhouse", source_job_id="1", original_url="u2", first_seen_at=NOW, last_seen_at=NOW, posted_at=NOW, raw_description="x", canonical_application_url="a2", deadline=NOW + timedelta(days=20)),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_jobs([job])
            buckets = build_deadline_view(store, now=NOW)

        all_entries = [entry for entries in buckets.values() for entry in entries]
        self.assertEqual(len(all_entries), 1)
        self.assertTrue(all_entries[0].deadline_conflict)
        self.assertEqual(len(all_entries[0].deadline_observations), 2)


class TodaySummaryUsesRealPersistedStateTests(unittest.TestCase):
    def test_new_jobs_today_reflects_actual_first_seen_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_jobs([_job("1", first_seen=NOW), _job("2", first_seen=NOW - timedelta(days=10))])
            summary = build_today_summary(store, now=NOW)

        self.assertEqual(summary.new_jobs_today, 1)

    def test_applications_submitted_today_counts_real_applied_at_only(self) -> None:
        # ApplicationStore.update_status stamps `applied_at` with the real
        # wall-clock time internally (not an injectable `now`), so this must
        # compare against the real current time too -- a fixed constant here
        # would silently break the day after it was written (confirmed: this
        # test broke exactly that way across a real session/day boundary).
        real_now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            app_store = ApplicationStore(store.root)
            app_store.create_application(Application(id="a1", job_id="1", company="Acme", role_title="x"))
            app_store.update_status("a1", ApplicationStatus.APPLIED)
            summary = build_today_summary(store, application_store=app_store, now=real_now)

        self.assertEqual(summary.applications_submitted_today, 1)

    def test_never_invents_counts_for_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            summary = build_today_summary(store, now=NOW)

        self.assertEqual(summary.cvs_ready, 0)
        self.assertEqual(summary.offers, 0)


class ApplicationDetailAnswersExactCvSubmittedTests(unittest.TestCase):
    def test_current_cv_matches_the_applications_recorded_cv_version_id(self) -> None:
        from jobintel.models.cv_artifact import GeneratedCVArtifact
        from jobintel.storage.cv_artifact_store import CVArtifactStore

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_jobs([_job("1")])
            app_store = ApplicationStore(store.root)
            cv_store = CVArtifactStore(store.root)
            app_store.create_application(Application(id="a1", job_id="1", company="Acme", role_title="x"))

            cv1 = GeneratedCVArtifact(id="cv-1", job_id="1", candidate_id="c", generated_at=NOW - timedelta(days=2), jd_snapshot="", selected_project_ids=[], evidence_ids_used=[], bullets=[], skills_included=[], cv_text="v1", pdf_path=None, page_count=1, fits_one_page=True, render_margin_mm=18.0, render_body_pt=10.0, review_status=ReviewGateStatus.AUTO_PREPARE)
            cv2 = GeneratedCVArtifact(id="cv-2", job_id="1", candidate_id="c", generated_at=NOW - timedelta(days=1), jd_snapshot="", selected_project_ids=[], evidence_ids_used=[], bullets=[], skills_included=[], cv_text="v2", pdf_path=None, page_count=1, fits_one_page=True, render_margin_mm=18.0, render_body_pt=10.0, review_status=ReviewGateStatus.AUTO_PREPARE)
            cv_store.save(cv1)
            cv_store.save(cv2)
            # The application was actually submitted with the FIRST cv, even
            # though a later cv-2 exists for the same job (e.g. regenerated
            # afterwards) -- must answer with the one actually attached, not
            # just "the latest one generated".
            app_store.attach_cv("a1", "cv-1", [], [])

            detail = build_application_detail("a1", store, app_store, cv_store)

        self.assertEqual(detail.current_cv.id, "cv-1")
        self.assertEqual(detail.current_cv.cv_text, "v1")

    def test_timeline_is_chronological_and_derived_from_persisted_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_jobs([_job("1", first_seen=NOW - timedelta(days=3))])
            app_store = ApplicationStore(store.root)
            app_store.create_application(Application(id="a1", job_id="1", company="Acme", role_title="x"))
            app_store.update_status("a1", ApplicationStatus.APPLIED, note="Submitted via Greenhouse")

            detail = build_application_detail("a1", store, app_store)

        occurred_ats = [event.occurred_at for event in detail.timeline]
        self.assertEqual(occurred_ats, sorted(occurred_ats))
        self.assertTrue(any("discovered" in event.label.lower() for event in detail.timeline))


class SourceHealthNeverAppearsAsZeroJobsTests(unittest.TestCase):
    def test_not_configured_source_is_distinct_from_active_and_from_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            rows = {row.source_name: row for row in build_source_health_view(store, registry_path="/nonexistent/registry.json")}

        self.assertEqual(rows["adzuna"].state, NOT_CONFIGURED)
        self.assertEqual(rows["welcome_to_the_jungle"].state, NOT_CONFIGURED)
        self.assertIsNone(rows["adzuna"].jobs_observed)

    def test_manual_only_source_is_never_reported_as_active_or_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            rows = {row.source_name: row for row in build_source_health_view(store, registry_path="/nonexistent/registry.json")}

        self.assertEqual(rows["trackr"].state, SOURCE_MANUAL_ONLY)

    def test_a_failed_refresh_is_reported_as_failed_not_as_zero_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_source_health("greenhouse", {"status": "error", "last_error": "network down", "jobs_seen": 0, "checked_at": NOW.isoformat()})
            rows = {row.source_name: row for row in build_source_health_view(store, registry_path="/nonexistent/registry.json")}

        self.assertEqual(rows["greenhouse"].state, SOURCE_FAILED)
        self.assertEqual(rows["greenhouse"].error, "network down")

    def test_a_genuinely_successful_zero_job_refresh_is_active_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_source_health("greenhouse", {"status": "live_api", "jobs_seen": 0, "checked_at": NOW.isoformat(), "last_synced": NOW.isoformat()})
            rows = {row.source_name: row for row in build_source_health_view(store, registry_path="/nonexistent/registry.json", now=NOW)}

        self.assertEqual(rows["greenhouse"].state, ACTIVE_AND_REFRESHED)
        self.assertIsNone(rows["greenhouse"].error)

    def test_an_old_success_is_stale_never_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_source_health("greenhouse", {"status": "live_api", "jobs_seen": 12, "checked_at": (NOW - timedelta(days=3)).isoformat(), "last_synced": (NOW - timedelta(days=3)).isoformat()})
            rows = {row.source_name: row for row in build_source_health_view(store, registry_path="/nonexistent/registry.json", now=NOW)}

        self.assertEqual(rows["greenhouse"].state, SOURCE_STALE)


class CompanyHealthViewTests(unittest.TestCase):
    def test_counts_match_the_real_registry(self) -> None:
        summary = build_company_health_view()
        self.assertEqual(summary.total, 83)
        self.assertEqual(sum(summary.counts.values()), 83)


if __name__ == "__main__":
    unittest.main()
