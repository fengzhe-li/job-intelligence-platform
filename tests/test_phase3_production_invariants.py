from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.application_lifecycle import ensure_application, migrate_legacy_workflow_statuses, overlay_lifecycle, set_job_status
from jobintel.config import default_ranking_config
from jobintel.dashboard.operations import (
    ACTIVE_NOT_REFRESHED,
    NOT_CONFIGURED,
    SOURCE_FAILED,
    build_deadline_view,
    build_source_health_view,
)
from jobintel.dashboard.server import _handler
from jobintel.dashboard.service import DashboardFilters, build_dashboard_model
from jobintel.fixtures.sample_data import sample_candidate
from jobintel.matching.cv_generation import generate_and_save_cv, resolve_cv_pdf
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import ApplicationStatus, WorkflowStatus
from jobintel.pipeline.daily import OUTCOME_FAILED, OUTCOME_NOT_CONFIGURED, OUTCOME_SUCCEEDED_ZERO, run_daily_refresh
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.company_discovery_store import CompanyDiscoveryStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)


def _job(job_id: str = "greenhouse:1", source: str = "greenhouse", company: str = "Acme", title: str = "Graduate Software Engineer", deadline: datetime | None = None) -> Job:
    return Job(
        id=job_id,
        title=title,
        company=company,
        description="Graduate programme. Build Python backend services, SQL, REST APIs, cloud. Open to 2026 graduates.",
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name=source,
                source_job_id=job_id.split(":")[-1],
                original_url=f"https://example.com/{job_id}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                posted_at=NOW,
                raw_description="Build Python backend services.",
                canonical_application_url=f"https://example.com/{job_id}/apply",
                deadline=deadline,
            )
        ],
    )


def _registry(tmp: str, entries: list[dict]) -> Path:
    path = Path(tmp) / "target_companies.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _gh_entry(name: str = "Acme", token: str = "acme", **extra) -> dict:
    return {"company_name": name, "industry": "SaaS", "priority": 1, "connector_type": "greenhouse", "connector_token": token, "enabled": True, "verification_status": "verified", **extra}


def _gh(*ids: int) -> dict:
    return {
        "jobs": [
            {"id": i, "title": f"Graduate Software Engineer {i}", "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}", "updated_at": "2026-09-01T09:00:00+00:00", "content": "<p>Python backend.</p>", "offices": [{"location": "London, United Kingdom"}]}
            for i in ids
        ]
    }


def _daily(store: LocalJobStore, registry: Path, greenhouse):
    kwargs = {"side_effect": greenhouse} if callable(greenhouse) or isinstance(greenhouse, Exception) else {"return_value": greenhouse}
    with (
        patch("jobintel.connectors.greenhouse.fetch_json", **kwargs),
        patch("jobintel.connectors.prospects.fetch_text", return_value="<html></html>"),
        patch.dict(os.environ, {}, clear=True),
    ):
        return run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)


# ---------------------------------------------------------------------------
# Application lifecycle ownership boundary
# ---------------------------------------------------------------------------


class ApplicationLifecycleBoundaryTests(unittest.TestCase):
    def test_marking_applied_creates_exactly_one_submitted_application_and_no_workflow_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, apps = LocalJobStore(tmp), ApplicationStore(tmp)
            store.write_jobs([_job()])
            set_job_status("greenhouse:1", WorkflowStatus.APPLIED, store, apps)
            set_job_status("greenhouse:1", WorkflowStatus.APPLIED, store, apps)
            ensure_application(_job(), apps)
            applications = apps.read_applications()
            workflow = store.read_workflow_statuses()
            effective = overlay_lifecycle(store.read_jobs(), apps)[0].workflow_status

        self.assertEqual(len(applications), 1)
        self.assertTrue(applications[0].is_submitted)
        self.assertIsNotNone(applications[0].applied_at)
        self.assertNotIn("greenhouse:1", workflow, "post-submission state must not be duplicated into workflow_status.json")
        self.assertEqual(effective, WorkflowStatus.APPLIED)

    def test_application_snapshots_jd_url_sources_and_deadline(self) -> None:
        deadline = NOW + timedelta(days=5)
        with tempfile.TemporaryDirectory() as tmp:
            application = ensure_application(_job(deadline=deadline), ApplicationStore(tmp))

        self.assertIn("Python backend", application.jd_snapshot)
        self.assertEqual(application.application_url, "https://example.com/greenhouse:1/apply")
        self.assertEqual(application.sources, ["greenhouse"])
        self.assertEqual(application.deadline, deadline)

    def test_submitted_application_cannot_be_reset_to_a_triage_or_pre_submission_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, apps = LocalJobStore(tmp), ApplicationStore(tmp)
            store.write_jobs([_job()])
            set_job_status("greenhouse:1", WorkflowStatus.APPLIED, store, apps)
            with self.assertRaises(ValueError):
                set_job_status("greenhouse:1", WorkflowStatus.NEW, store, apps)
            application = apps.for_job("greenhouse:1")
            with self.assertRaises(ValueError):
                apps.update_status(application.id, ApplicationStatus.SHORTLISTED)
            apps.update_status(application.id, ApplicationStatus.ONLINE_ASSESSMENT, note="OA invite")
            effective = overlay_lifecycle(store.read_jobs(), apps)[0].workflow_status
            history = [(event.from_status, event.to_status) for event in apps.status_history_for(application.id)]

        self.assertEqual(effective, WorkflowStatus.OA)
        self.assertEqual(history[-1], (ApplicationStatus.APPLIED, ApplicationStatus.ONLINE_ASSESSMENT))

    def test_coarse_applied_from_the_inbox_never_regresses_a_later_stage(self) -> None:
        # Found by the live E2E run: re-posting "applied" moved an application
        # at online_assessment back to applied.
        with tempfile.TemporaryDirectory() as tmp:
            store, apps = LocalJobStore(tmp), ApplicationStore(tmp)
            store.write_jobs([_job()])
            set_job_status("greenhouse:1", WorkflowStatus.APPLIED, store, apps)
            set_job_status("greenhouse:1", WorkflowStatus.OA, store, apps)
            set_job_status("greenhouse:1", WorkflowStatus.APPLIED, store, apps)
            application = apps.for_job("greenhouse:1")
            events = apps.status_history_for(application.id)

        self.assertEqual(application.status, ApplicationStatus.ONLINE_ASSESSMENT)
        self.assertEqual(events[-1].to_status, ApplicationStatus.ONLINE_ASSESSMENT)

    def test_triage_statuses_stay_in_the_workflow_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, apps = LocalJobStore(tmp), ApplicationStore(tmp)
            store.write_jobs([_job()])
            set_job_status("greenhouse:1", WorkflowStatus.SAVED, store, apps)
            self.assertEqual(store.read_workflow_statuses(), {"greenhouse:1": "saved"})
            self.assertEqual(apps.read_applications(), [])

    def test_duplicate_cross_source_observation_never_creates_a_second_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, apps = LocalJobStore(tmp), ApplicationStore(tmp)
            store.write_jobs([_job()])
            set_job_status("greenhouse:1", WorkflowStatus.APPLIED, store, apps)
            # The same vacancy is later observed on an aggregator -> merges
            # into the same canonical job, never a new application.
            store.write_jobs([_job("adzuna:99", source="adzuna")])
            jobs = store.read_jobs()
            for job in jobs:
                set_job_status(job.id, WorkflowStatus.APPLIED, store, apps)
            applications = apps.read_applications()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(applications), 1)

    def test_legacy_workflow_applied_is_migrated_once_without_inventing_a_submission_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, apps = LocalJobStore(tmp), ApplicationStore(tmp)
            store.write_jobs([_job()])
            store.set_workflow_status("greenhouse:1", WorkflowStatus.APPLIED)  # pre-Phase-3 data shape
            first = migrate_legacy_workflow_statuses(store, apps)
            second = migrate_legacy_workflow_statuses(store, apps)
            applications = apps.read_applications()
            log = (Path(tmp) / "applications" / "legacy_workflow_migration.jsonl").read_text(encoding="utf-8").splitlines()
            effective = overlay_lifecycle(store.read_jobs(), apps)[0].workflow_status

        self.assertEqual(first, ["greenhouse:1"])
        self.assertEqual(second, [])
        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0].status, ApplicationStatus.APPLIED)
        self.assertIsNone(applications[0].applied_at)
        self.assertTrue(applications[0].is_submitted)
        self.assertEqual(len(log), 1)
        self.assertEqual(effective, WorkflowStatus.APPLIED)


class SubmittedCvImmutabilityTests(unittest.TestCase):
    def test_manual_application_without_system_cv_is_explicit_and_never_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            apps, cvs = ApplicationStore(tmp), CVArtifactStore(tmp)
            job = enrich_job(_job(), 2026)
            application = ensure_application(job, apps)
            draft = generate_and_save_cv(sample_candidate(), job, cvs, store_root=tmp)
            # A draft was attached but the user applied with their own CV.
            apps.attach_cv(application.id, draft.id, draft.selected_project_ids, draft.evidence_ids_used)
            submitted = apps.update_status(application.id, ApplicationStatus.APPLIED, note="used my own CV")
            with self.assertRaises(ValueError):
                apps.attach_cv(application.id, draft.id, draft.selected_project_ids, draft.evidence_ids_used)
            later = generate_and_save_cv(sample_candidate(), job, cvs, store_root=tmp)
            with self.assertRaises(ValueError):
                apps.attach_cv(application.id, later.id, later.selected_project_ids, later.evidence_ids_used)
            submission = [event for event in apps.status_history_for(application.id) if event.marks_submission]

        self.assertIsNone(submitted.submitted_cv_version_id, "an attached draft is never assumed to be the submitted CV")
        self.assertEqual(len(submission), 1)
        self.assertIsNone(submission[0].submitted_cv_version_id)
        self.assertEqual(submission[0].note, "used my own CV")

    def test_submitted_cv_must_be_attached_and_only_recorded_at_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            apps = ApplicationStore(tmp)
            application = ensure_application(_job(), apps)
            with self.assertRaises(ValueError):
                apps.update_status(application.id, ApplicationStatus.APPLIED, submitted_cv_version_id="cv-0123456789ab")
            apps.update_status(application.id, ApplicationStatus.APPLIED)
            with self.assertRaises(ValueError):
                apps.update_status(application.id, ApplicationStatus.ONLINE_ASSESSMENT, submitted_cv_version_id="cv-0123456789ab")

    def test_submitted_cv_version_is_frozen_and_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            apps, cvs = ApplicationStore(tmp), CVArtifactStore(tmp)
            job = enrich_job(_job(), 2026)
            application = ensure_application(job, apps)
            first = generate_and_save_cv(sample_candidate(), job, cvs, store_root=tmp)
            second = generate_and_save_cv(sample_candidate(), job, cvs, store_root=tmp)
            apps.attach_cv(application.id, first.id, first.selected_project_ids, first.evidence_ids_used)
            submitted = apps.update_status(application.id, ApplicationStatus.APPLIED, submitted_cv_version_id=first.id)
            with self.assertRaises(ValueError):
                apps.attach_cv(application.id, second.id, second.selected_project_ids, second.evidence_ids_used)
            apps.attach_cv(application.id, first.id, first.selected_project_ids, first.evidence_ids_used)  # same version: harmless
            stored = apps.for_job(job.id)
            notes = [event.note for event in apps.status_history_for(application.id)]

        self.assertEqual(submitted.submitted_cv_version_id, first.id)
        self.assertEqual(stored.cv_version_id, first.id)
        self.assertEqual(stored.submitted_cv_version_id, first.id)
        self.assertIn(f"CV attached: {first.id}", notes)
        self.assertIsNotNone(first.pdf_sha256)


# ---------------------------------------------------------------------------
# Daily refresh persistence + per-refresh deltas + outcome distinctions
# ---------------------------------------------------------------------------


class DailyReportDeltaTests(unittest.TestCase):
    def test_report_is_persisted_and_counts_are_per_refresh_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_gh_entry()])
            store = LocalJobStore(Path(tmp) / "store")
            first = _daily(store, registry, _gh(1, 2))
            second = _daily(store, registry, _gh(1, 2))
            third = _daily(store, registry, _gh(1))  # job 2 gone from a complete board
            fourth = _daily(store, registry, _gh(1))
            latest = store.read_daily_report()
            history = store.read_daily_report_history()

        self.assertEqual((first.new_jobs, first.closed_jobs), (2, 0))
        self.assertEqual((second.new_jobs, second.closed_jobs), (0, 0))
        self.assertEqual((third.new_jobs, third.closed_jobs), (0, 1))
        self.assertEqual(third.closed_job_ids, ["greenhouse:2"])
        self.assertEqual((fourth.new_jobs, fourth.closed_jobs), (0, 0), "closed_jobs must never be cumulative")
        self.assertEqual(len(history), 4)
        self.assertEqual(latest["closed_jobs"], 0)
        self.assertEqual(latest["finished_at"], fourth.finished_at.isoformat())

    def test_failure_zero_results_and_not_configured_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_gh_entry()])
            store = LocalJobStore(Path(tmp) / "store")
            _daily(store, registry, _gh(1))
            report = _daily(store, registry, urllib.error.URLError("network down"))
            outcomes = {item.source_name: item.outcome for item in report.source_outcomes}
            jobs = store.read_jobs()

        self.assertEqual(outcomes["greenhouse"], OUTCOME_FAILED)
        self.assertEqual(outcomes["prospects"], OUTCOME_SUCCEEDED_ZERO)
        self.assertEqual(outcomes["adzuna"], OUTCOME_NOT_CONFIGURED)
        self.assertNotIn("adzuna", report.sources_failed)
        self.assertEqual(report.status, "partial")
        self.assertTrue(jobs[0].source_observations[0].active, "a failed source must never close jobs")
        self.assertEqual(report.closed_jobs, 0)

    def test_github_sync_unconfigured_is_not_a_failure_and_does_not_block_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [_gh_entry()])
            store = LocalJobStore(Path(tmp) / "store")
            report = _daily(store, registry, _gh(1))
        stages = {stage.name: stage.outcome for stage in report.stages}
        self.assertEqual(stages["github_sync"], OUTCOME_NOT_CONFIGURED)
        self.assertEqual(report.new_jobs, 1)

    def test_manual_only_sources_are_listed_never_attempted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            report = _daily(store, _registry(tmp, []), _gh())
        attempted = {item.source_name for item in report.source_outcomes}
        self.assertEqual(set(report.manual_only_sources), {"trackr", "gradcracker", "bright_network"})
        self.assertFalse(attempted & set(report.manual_only_sources))


class ManualExclusionSurvivesDiscoveryTests(unittest.TestCase):
    def test_company_discovery_never_resurrects_or_edits_an_excluded_company(self) -> None:
        from jobintel.ats_discovery import exclude_company_entry

        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [exclude_company_entry(_gh_entry("Excluded Co", "excluded"), "acquired, shared tenant")])
            before = registry.read_text(encoding="utf-8")
            store = LocalJobStore(Path(tmp) / "store")
            adzuna_payload = {"count": 1, "results": [{"id": "a1", "title": "Graduate Engineer", "company": {"display_name": "Excluded Co"}, "location": {"display_name": "London"}, "redirect_url": "https://boards.greenhouse.io/excluded/jobs/1", "description": "Python", "created": "2026-09-01T09:00:00Z"}]}
            with (
                patch("jobintel.connectors.greenhouse.fetch_json", side_effect=AssertionError("excluded company fetched")),
                patch("jobintel.connectors.adzuna.fetch_json", return_value=adzuna_payload),
                patch("jobintel.connectors.prospects.fetch_text", return_value="<html></html>"),
                patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}, clear=True),
            ):
                run_daily_refresh(store=store, config=default_ranking_config(), registry_path=registry)
            observed = CompanyDiscoveryStore(store.root).read_all()
            after = registry.read_text(encoding="utf-8")

        self.assertEqual(after, before)
        self.assertEqual(observed, [])


# ---------------------------------------------------------------------------
# Unknown deadlines, dynamic health
# ---------------------------------------------------------------------------


class UnknownDeadlineStaysUnknownTests(unittest.TestCase):
    def test_unknown_deadline_is_unknown_in_inbox_view_and_sorts_after_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs([_job("greenhouse:1", title="Graduate Data Engineer"), _job("greenhouse:2", title="Graduate Platform Engineer", deadline=NOW + timedelta(days=30))])
            model = build_dashboard_model(store=store, config=default_ranking_config(), filters=DashboardFilters(sort="deadline"), now=NOW)
            buckets = build_deadline_view(store, default_ranking_config(), NOW)

        self.assertEqual([card["id"] for card in model["jobs"]], ["greenhouse:2", "greenhouse:1"])
        self.assertIsNone(model["jobs"][1]["deadline"])
        self.assertEqual([entry.job_id for entry in buckets["unknown"]], ["greenhouse:1"])


class DynamicSourceHealthTests(unittest.TestCase):
    def test_health_reflects_configuration_and_evidence_for_every_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp, [{"company_name": "Darktrace", "industry": "Security", "priority": 1, "connector_type": "workday", "connector_token": "darktrace.wd3/Ext", "enabled": True, "verification_status": "verified"}])
            store = LocalJobStore(Path(tmp) / "store")
            store.write_source_health("some_future_ats", {"status": "error", "last_error": "boom", "checked_at": NOW.isoformat()})
            rows = {row.source_name: row for row in build_source_health_view(store, registry, now=NOW, environ={})}

        self.assertEqual(rows["workday"].state, ACTIVE_NOT_REFRESHED)
        self.assertEqual(rows["workday"].configured_identifiers, 1)
        self.assertEqual(rows["greenhouse"].state, NOT_CONFIGURED, "no enabled Greenhouse company = not configured, not 'healthy'")
        self.assertEqual(rows["adzuna"].state, NOT_CONFIGURED)
        self.assertEqual(rows["some_future_ats"].state, SOURCE_FAILED, "evidence for an unprofiled source must still be shown")


# ---------------------------------------------------------------------------
# CV PDF serving + end-to-end dashboard HTTP routes
# ---------------------------------------------------------------------------


class DashboardHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.registry = _registry(str(self.tmp), [])
        self.store = LocalJobStore(self.tmp / "store")
        self.store.write_jobs([enrich_job(_job(), 2026)])
        self.profile_patch = patch("jobintel.dashboard.server.load_candidate_profile", return_value=sample_candidate())
        self.profile_patch.start()
        handler = _handler(str(self.store.root), "config/personal_strategy.json", str(self.registry))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.profile_patch.stop()
        self._tmp.cleanup()

    def _request(self, method: str, path: str, form: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, {key.lower(): value for key, value in response.getheaders()}, data)
        connection.close()
        return result

    def test_every_get_view_renders(self) -> None:
        for path in ("/today", "/inbox", "/inbox?sort=deadline", "/priority", "/deadlines", "/applications", "/watchlist", "/manual-import", "/health", "/cv?job_id=greenhouse%3A1", "/job?id=greenhouse%3A1"):
            status, _, body = self._request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(b"<html", body, path)

    def test_root_keeps_inbox_filters(self) -> None:
        status, headers, _ = self._request("GET", "/?search=python&lang=en")
        self.assertEqual(status, 303)
        self.assertTrue(headers["location"].startswith("/inbox?"))
        status, headers, _ = self._request("GET", "/?lang=en")
        self.assertTrue(headers["location"].startswith("/today"))

    def test_generate_review_submit_and_serve_pdf_end_to_end(self) -> None:
        status, _, _ = self._request("POST", "/cv/generate", {"job_id": "greenhouse:1"})
        self.assertEqual(status, 303)
        artifact = CVArtifactStore(self.store.root).for_job("greenhouse:1")[0]

        status, headers, pdf = self._request("GET", f"/cv/pdf?id={artifact.id}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/pdf")
        self.assertTrue(pdf.startswith(b"%PDF"))

        # Human-review gate: submission without confirming review is refused.
        status, _, _ = self._request("POST", "/application/submit", {"job_id": "greenhouse:1", "cv_version_id": artifact.id})
        self.assertEqual(status, 400)
        self.assertIsNone(ApplicationStore(self.store.root).for_job("greenhouse:1"))

        status, headers, _ = self._request("POST", "/application/submit", {"job_id": "greenhouse:1", "cv_version_id": artifact.id, "reviewed": "yes", "note": "via portal"})
        self.assertEqual(status, 303)
        application = ApplicationStore(self.store.root).for_job("greenhouse:1")
        self.assertEqual(application.status, ApplicationStatus.APPLIED)
        self.assertEqual(application.submitted_cv_version_id, artifact.id)

        status, _, body = self._request("GET", headers["location"])
        self.assertEqual(status, 200)
        self.assertIn(artifact.id.encode(), body)
        self.assertIn(f"/cv/pdf?id={artifact.id}".encode(), body)

        # Swapping the submitted CV is refused; lifecycle continues append-only.
        second = generate_and_save_cv(sample_candidate(), enrich_job(_job(), 2026), CVArtifactStore(self.store.root), store_root=self.store.root)
        status, _, _ = self._request("POST", "/cv/attach", {"job_id": "greenhouse:1", "cv_version_id": second.id})
        self.assertEqual(status, 409)
        status, _, _ = self._request("POST", "/application/status", {"application_id": application.id, "status": "discovered"})
        self.assertEqual(status, 409)
        status, _, _ = self._request("POST", "/application/status", {"application_id": application.id, "status": "online_assessment", "note": "OA received"})
        self.assertEqual(status, 303)
        status, _, _ = self._request("POST", "/workflow", {"job_id": "greenhouse:1", "status": "new", "return_to": "/inbox"})
        self.assertEqual(status, 409)
        status, _, body = self._request("GET", "/inbox")
        self.assertIn(b"online_assessment", body)
        self.assertEqual(len(ApplicationStore(self.store.root).read_applications()), 1)

    def test_manual_application_without_system_cv_full_lifecycle(self) -> None:
        # A generated draft exists and is attached, but the user applies with
        # their own CV: the application must say "no system CV", never the draft.
        self._request("POST", "/cv/generate", {"job_id": "greenhouse:1"})
        draft = CVArtifactStore(self.store.root).for_job("greenhouse:1")[0]
        self._request("POST", "/application/create", {"job_id": "greenhouse:1"})
        self._request("POST", "/cv/attach", {"job_id": "greenhouse:1", "cv_version_id": draft.id})

        status, headers, _ = self._request("POST", "/application/submit", {"job_id": "greenhouse:1", "note": "own CV via company portal"})
        self.assertEqual(status, 303)
        application = ApplicationStore(self.store.root).for_job("greenhouse:1")
        self.assertIsNone(application.submitted_cv_version_id)

        for next_status in ("online_assessment", "technical_interview", "offer"):
            self.assertEqual(self._request("POST", "/application/status", {"application_id": application.id, "status": next_status})[0], 303)
        # Retroactively attaching the draft is refused.
        self.assertEqual(self._request("POST", "/cv/attach", {"job_id": "greenhouse:1", "cv_version_id": draft.id})[0], 409)

        _, _, detail = self._request("GET", headers["location"])
        _, _, tracker = self._request("GET", "/applications")
        _, _, workbench = self._request("GET", "/cv?job_id=greenhouse%3A1")
        self.assertIn(b"No system CV attached", detail)
        self.assertIn(b"applied manually", detail)
        self.assertIn(b"No system CV attached", tracker)
        self.assertIn(b"No system CV attached", workbench)
        history = ApplicationStore(self.store.root).status_history_for(application.id)
        self.assertEqual([event.to_status.value for event in history if event.from_status != event.to_status], ["discovered", "applied", "online_assessment", "technical_interview", "offer"])
        submission = [event for event in history if event.marks_submission]
        self.assertEqual(len(submission), 1)
        self.assertIsNone(submission[0].submitted_cv_version_id)
        self.assertIsNone(ApplicationStore(self.store.root).for_job("greenhouse:1").submitted_cv_version_id)

    def test_pdf_route_cannot_reach_arbitrary_files(self) -> None:
        secret = self.tmp / "secret.pdf"
        secret.write_bytes(b"%PDF-secret")
        for bad in ("../../secret", "..%2F..%2Fsecret.pdf", str(secret), "cv-000000000000", "cv-ZZZ", "/etc/passwd", ""):
            status, _, body = self._request("GET", f"/cv/pdf?id={urllib.parse.quote(bad, safe='%')}")
            self.assertEqual(status, 404, bad)
            self.assertNotIn(b"secret", body)

    def test_pdf_route_ignores_a_tampered_stored_path_and_refuses_modified_bytes(self) -> None:
        from dataclasses import replace

        cvs = CVArtifactStore(self.store.root)
        artifact = generate_and_save_cv(sample_candidate(), enrich_job(_job(), 2026), cvs, store_root=self.store.root)
        forged = replace(artifact, id="cv-aaaaaaaaaaaa", pdf_path=str(self.tmp / "secret.pdf"))
        (self.tmp / "secret.pdf").write_bytes(b"%PDF-secret")
        cvs.save(forged)
        self.assertIsNone(resolve_cv_pdf(cvs, forged.id))
        status, _, _ = self._request("GET", f"/cv/pdf?id={forged.id}")
        self.assertEqual(status, 404)

        Path(artifact.pdf_path).write_bytes(b"%PDF-tampered")
        status, _, _ = self._request("GET", f"/cv/pdf?id={artifact.id}")
        self.assertEqual(status, 409)

    def test_triage_watchlist_and_manual_import_posts(self) -> None:
        status, _, _ = self._request("POST", "/workflow", {"job_id": "greenhouse:1", "status": "saved", "return_to": "https://evil.example/"})
        self.assertEqual(status, 303)
        self.assertEqual(self.store.read_workflow_statuses()["greenhouse:1"], "saved")

        self.assertEqual(self._request("POST", "/watchlist/check", {"target": "trackr", "result": "nothing new"})[0], 303)
        self.assertEqual(self._request("POST", "/watchlist/snooze", {"target": "gradcracker", "days": "3"})[0], 303)

        with patch("jobintel.connectors.manual_source.fetch_text", side_effect=urllib.error.URLError("offline")):
            status, _, body = self._request("POST", "/manual-import", {"url": "https://www.gradcracker.com/job/1", "source_name": "gradcracker", "title": "Graduate Embedded Engineer", "company": "Beta Ltd", "location": "Bristol"})
        self.assertEqual(status, 200)
        self.assertIn(b"Graduate Embedded Engineer", body)
        self.assertEqual(len(self.store.read_jobs()), 2)

    def test_workflow_redirect_rejects_off_site_targets(self) -> None:
        status, headers, _ = self._request("POST", "/workflow", {"job_id": "greenhouse:1", "status": "saved", "return_to": "//evil.example/x"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/inbox")


if __name__ == "__main__":
    unittest.main()
