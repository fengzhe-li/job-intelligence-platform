from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.dashboard.i18n import labels
from jobintel.dashboard.server import _refresh_redirect, _render_dashboard, _render_detail, _run_dashboard_refresh, _save_dashboard_search
from jobintel.dashboard.service import DashboardFilters, build_dashboard_model, build_job_detail_model, filters_from_params, is_new_today, saved_searches, save_search, update_workflow_status
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import LocationMode, RoleTrack, SponsorshipFilterMode, SponsorshipState, WorkflowStatus, WorkMode
from jobintel.storage.local_store import LocalJobStore


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)

    def test_dashboard_filters_by_sponsorship_role_seniority_company_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs(
                [
                    enrich_job(_job("sponsor", "Junior Backend Engineer", "GoodCo", self.now, "greenhouse", "Python backend APIs. Skilled worker visa sponsorship available."), 2026),
                    enrich_job(_job("unknown", "Senior Business Analyst", "OtherCo", self.now, "lever", "SQL dashboards and stakeholder reporting."), 2026),
                ]
            )

            model = build_dashboard_model(
                store=store,
                filters=DashboardFilters(
                    sponsorship=SponsorshipFilterMode.SPONSOR_ONLY,
                    role_track=RoleTrack.BACKEND_ENGINEERING,
                    seniority="junior",
                    company="Good",
                    source="greenhouse",
                ),
                now=self.now,
            )

        self.assertEqual(len(model["jobs"]), 1)
        self.assertEqual(model["jobs"][0]["id"], "sponsor")
        self.assertEqual(model["jobs"][0]["sponsorship_state"], SponsorshipState.EXPLICIT_SPONSOR.value)

    def test_workflow_status_persists_outside_canonical_job_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs([enrich_job(_job("job-1", "Backend Engineer", "Demo", self.now, "ashby", "Python APIs."), 2026)])

            update_workflow_status("job-1", WorkflowStatus.APPLIED.value, store)
            jobs = store.read_jobs()

        self.assertEqual(jobs[0].workflow_status, WorkflowStatus.APPLIED)

    def test_bilingual_labels_are_available(self) -> None:
        self.assertEqual(labels("en")["daily_shortlist"], "Daily Shortlist")
        self.assertEqual(labels("zh")["daily_shortlist"], "每日候选清单")
        self.assertEqual(labels("zh")["direct_apply"], "直接申请")

    def test_job_detail_model_contains_original_jd_and_explainability_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            description = "Build Python data pipelines. No sponsorship available."
            store.write_jobs([enrich_job(_job("detail", "Software Engineer (Data)", "Suade", self.now, "workable", description), 2026)])

            detail = build_job_detail_model("detail", store=store, now=self.now, lang="zh")

        self.assertEqual(detail["original_jd"], description)
        self.assertEqual(detail["normalized"]["title"], "Software Engineer (Data)")
        self.assertIn("No sponsorship available", detail["sponsorship_evidence"])
        self.assertIn("role_track_explanation", detail)
        self.assertEqual(detail["source_observations"][0]["label"], "Workable")
        self.assertTrue(detail["job"]["english_summary"])
        self.assertTrue(detail["job"]["chinese_summary"])

    def test_new_today_logic_uses_first_seen_date(self) -> None:
        today = enrich_job(_job("today", "Backend Engineer", "Demo", self.now, "lever", "Python APIs."), 2026)
        yesterday = enrich_job(_job("old", "Backend Engineer", "Demo", self.now - timedelta(days=1), "lever", "Python APIs."), 2026)

        self.assertTrue(is_new_today(today, self.now))
        self.assertFalse(is_new_today(yesterday, self.now))

    def test_new_today_preset_limits_dashboard_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs(
                [
                    enrich_job(_job("today", "Backend Engineer", "Demo", self.now, "lever", "Python APIs."), 2026),
                    enrich_job(_job("old", "Backend Engineer", "Demo", self.now - timedelta(days=2), "lever", "Python APIs."), 2026),
                ]
            )

            model = build_dashboard_model(store=store, filters=DashboardFilters(new_today=True), now=self.now)

        self.assertEqual([job["id"] for job in model["jobs"]], ["today"])

    def test_wttj_source_badge_filter_and_preset_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs(
                [
                    enrich_job(_job("wttj", "Junior Backend Engineer", "Demo", self.now, "welcome_to_the_jungle", "Python APIs."), 2026),
                    enrich_job(_job("lever", "Backend Engineer", "Demo", self.now, "lever", "Python APIs."), 2026),
                ]
            )

            preset_model = build_dashboard_model(store=store, filters=filters_from_params({"preset": "wttj_jobs"}), now=self.now)
            all_model = build_dashboard_model(store=store, now=self.now)

        self.assertEqual([job["id"] for job in preset_model["jobs"]], ["wttj"])
        self.assertEqual(preset_model["counts"]["wttj_jobs"], 1)
        self.assertIn("welcome_to_the_jungle", all_model["options"]["sources"])
        self.assertEqual(preset_model["jobs"][0]["source_badges"][0]["label"], "Welcome to the Jungle")

    def test_job_detail_preserves_wttj_source_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs([enrich_job(_job("wttj-detail", "Junior Backend Engineer", "Demo", self.now, "welcome_to_the_jungle", "Python APIs."), 2026)])

            detail = build_job_detail_model("wttj-detail", store=store, now=self.now)

        self.assertEqual(detail["source_observations"][0]["source"], "welcome_to_the_jungle")
        self.assertEqual(detail["source_observations"][0]["label"], "Welcome to the Jungle")

    def test_dashboard_shows_and_filters_wttj_enrichment_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            discovery = enrich_job(_job("wttj-discovery", "Junior Backend Engineer", "Demo", self.now, "welcome_to_the_jungle", ""), 2026)
            discovery.source_observations[0].raw_payload["_enrichment_state"] = "discovery_only"
            store.write_jobs([discovery])

            model = build_dashboard_model(store=store, filters=filters_from_params({"enrichment_state": "discovery_only"}), now=self.now)
            detail = build_job_detail_model("wttj-discovery", store=store, now=self.now)

        self.assertEqual(model["counts"]["wttj_jobs"], 1)
        self.assertEqual(model["jobs"][0]["enrichment_state"], "discovery_only")
        self.assertEqual(model["jobs"][0]["enrichment_label"], "Needs JD enrichment")
        self.assertEqual(detail["source_observations"][0]["enrichment_state"], "discovery_only")

    def test_dashboard_exposes_wttj_source_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_source_health(
                "welcome_to_the_jungle",
                {
                    "status": "auth_required",
                    "last_synced": None,
                    "jobs_active": 0,
                    "last_error": "Missing WTTJ_API_KEY",
                },
            )

            model = build_dashboard_model(store=store, now=self.now)

        self.assertEqual(model["source_health"]["welcome_to_the_jungle"]["status"], "Auth required")
        self.assertEqual(model["source_health"]["welcome_to_the_jungle"]["last_synced"], "never")
        self.assertEqual(model["source_health"]["welcome_to_the_jungle"]["last_error"], "Missing WTTJ_API_KEY")

    def test_search_filters_ranked_jobs_across_title_location_skills_and_jd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs(
                [
                    enrich_job(_job("backend", "Software Engineer", "Demo", self.now, "lever", "Build Python backend APIs in London."), 2026),
                    enrich_job(_job("frontend", "Frontend Engineer", "Demo", self.now, "lever", "Build React interfaces."), 2026),
                ]
            )

            model = build_dashboard_model(store=store, filters=DashboardFilters(search="Python London"), now=self.now)

        self.assertEqual([job["id"] for job in model["jobs"]], ["backend"])

    def test_excluded_occupation_job_does_not_crash_dashboard_or_detail_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs(
                [enrich_job(_job("sales-role", "Sales Executive", "Demo", self.now, "lever", "Drive new business sales pipeline."), 2026)]
            )

            model = build_dashboard_model(store=store, now=self.now)
            detail = build_job_detail_model("sales-role", store=store, now=self.now)

        self.assertEqual(model["jobs"][0]["technical_fit"], 0.0)
        self.assertIn("role_track_explanation", detail)

    def test_saved_searches_are_persisted_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            save_search("London Backend", DashboardFilters(search="backend", location_mode=LocationMode.LONDON_ONLY), store)

            stored = saved_searches(store)

        saved = next(item for item in stored if item["name"] == "London Backend")
        self.assertEqual(saved["params"]["search"], "backend")
        self.assertEqual(saved["params"]["location_mode"], LocationMode.LONDON_ONLY.value)

    def test_new_since_refresh_filter_uses_latest_observed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            new_job = enrich_job(_job("new", "Backend Engineer", "Demo", self.now, "lever", "Python APIs.", state="NEW"), 2026)
            unchanged = enrich_job(_job("old", "Backend Engineer", "Demo", self.now, "lever", "Python APIs.", state="UNCHANGED"), 2026)
            store.write_jobs([new_job, unchanged], mark_missing_inactive=False)

            model = build_dashboard_model(store=store, filters=DashboardFilters(freshness_window="new_since_last_refresh"), now=self.now)

        self.assertEqual([job["id"] for job in model["jobs"]], ["new"])

    def test_refresh_summary_counts_are_shown_in_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs([enrich_job(_job("new", "Backend Engineer", "Demo", self.now, "lever", "Python APIs.", state="NEW"), 2026)])
            store.write_refresh_summary(
                {
                    "status": "finished",
                    "started_at": self.now.isoformat(),
                    "finished_at": self.now.isoformat(),
                    "total_jobs_seen": 3,
                    "active_jobs": 1,
                    "state_counts": {"NEW": 1, "CHANGED": 1, "UNCHANGED": 1, "DISAPPEARED": 0, "REAPPEARED": 0},
                    "sources": [],
                }
            )

            model = build_dashboard_model(store=store, now=self.now)

        self.assertEqual(model["refresh_summary"]["total_jobs_seen"], 3)
        self.assertEqual(model["refresh_summary"]["state_counts"]["NEW"], 1)
        self.assertEqual(model["refresh_summary"]["changed"], 0)

    def test_workflow_persistence_survives_refresh_style_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs([enrich_job(_job("job-1", "Backend Engineer", "Demo", self.now, "lever", "Python APIs."), 2026)])
            update_workflow_status("job-1", WorkflowStatus.APPLIED.value, store)
            refreshed = enrich_job(_job("job-1", "Backend Engineer", "Demo", self.now + timedelta(hours=1), "lever", "Python APIs.", state="UNCHANGED"), 2026)
            store.write_jobs([refreshed], refreshed_sources={"lever"})

            jobs = store.read_jobs()

        self.assertEqual(jobs[0].workflow_status, WorkflowStatus.APPLIED)

    def test_refresh_endpoint_calls_existing_refresh_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "target_companies.json"
            registry.write_text("[]", encoding="utf-8")
            store = LocalJobStore(tmp)
            lock = threading.Lock()
            with patch("jobintel.dashboard.server.refresh_sources") as mocked_refresh:
                refreshed = _run_dashboard_refresh(store, object(), str(registry), lock)

        self.assertTrue(refreshed)
        mocked_refresh.assert_called_once()

    def test_refresh_action_does_not_start_duplicate_run_when_locked(self) -> None:
        lock = threading.Lock()
        lock.acquire()
        try:
            with patch("jobintel.dashboard.server.refresh_sources") as mocked_refresh:
                refreshed = _run_dashboard_refresh(LocalJobStore("/tmp/nonexistent-dashboard-test"), object(), "config/target_companies.json", lock)
        finally:
            lock.release()

        self.assertFalse(refreshed)
        mocked_refresh.assert_not_called()

    def test_saved_search_endpoint_persists_current_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            redirect = _save_dashboard_search({"name": "Python London", "search": "Python", "location_mode": LocationMode.LONDON_ONLY.value}, store)
            stored = saved_searches(store)

        self.assertEqual(redirect, "/?sponsorship=all&location_mode=london_only&limit=50&search=Python&lang=en")
        saved = next(item for item in stored if item["name"] == "Python London")
        self.assertEqual(saved["params"]["search"], "Python")

    def test_language_switch_english_to_chinese_preserves_current_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs([enrich_job(_job("job-1", "Backend Engineer", "Demo", self.now, "lever", "Python APIs."), 2026)])
            model = build_dashboard_model(store=store, filters=DashboardFilters(search="Python", location_mode=LocationMode.LONDON_ONLY), now=self.now, lang="en")

            html = _render_dashboard(model)

        self.assertIn("EN", html)
        self.assertIn("中文", html)
        self.assertIn("search=Python", html)
        self.assertIn("location_mode=london_only", html)
        self.assertIn("lang=zh", html)

    def test_chinese_main_page_renders_translated_dashboard_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs([enrich_job(_job("job-1", "Backend Engineer", "Demo", self.now, "lever", "Python APIs.", state="NEW"), 2026)])
            model = build_dashboard_model(store=store, now=self.now, lang="zh")

            html = _render_dashboard(model)

        self.assertIn("每日候选清单", html)
        self.assertIn("刷新岗位", html)
        self.assertIn("本次刷新新增", html)
        self.assertIn("岗位方向", html)
        self.assertIn("新增", html)

    def test_chinese_job_detail_page_keeps_detail_and_original_jd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            description = "Build Python APIs. Original English JD should stay English."
            store.write_jobs([enrich_job(_job("detail", "Backend Engineer", "Demo", self.now, "lever", description), 2026)])
            model = build_job_detail_model("detail", store=store, now=self.now, lang="zh")

            html = _render_detail(model)

        self.assertIn('lang="zh"', html)
        self.assertIn("来源记录", html)
        self.assertIn("返回", html)
        self.assertIn("/job?id=detail&lang=en", html)
        self.assertIn("/job?id=detail&lang=zh", html)
        self.assertIn("Original English JD should stay English", html)

    def test_chinese_search_filter_form_preserves_lang(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            model = build_dashboard_model(store=store, filters=DashboardFilters(search="backend"), now=self.now, lang="zh")

            html = _render_dashboard(model)

        self.assertIn('name="lang" value="zh"', html)
        self.assertIn('name="search" value="backend"', html)
        self.assertIn("应用筛选", html)

    def test_chinese_workflow_update_return_preserves_lang_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            store.write_jobs([enrich_job(_job("job-1", "Backend Engineer", "Demo", self.now, "lever", "Python APIs."), 2026)])
            model = build_dashboard_model(store=store, filters=DashboardFilters(search="Python"), now=self.now, lang="zh")

            html = _render_dashboard(model)

        self.assertIn("已申请", html)
        self.assertIn("return_to", html)
        self.assertIn("lang=zh", html)
        self.assertIn("search=Python", html)

    def test_chinese_saved_search_redirect_and_links_preserve_lang(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            redirect = _save_dashboard_search({"name": "后端", "search": "backend", "lang": "zh"}, store)
            model = build_dashboard_model(store=store, now=self.now, lang="zh")

            html = _render_dashboard(model)

        self.assertIn("lang=zh", redirect)
        self.assertIn("后端", html)
        self.assertIn("lang=zh", html)

    def test_chinese_refresh_redirect_preserves_lang(self) -> None:
        self.assertEqual(_refresh_redirect("zh"), "/?lang=zh&freshness_window=new_since_last_refresh")
        self.assertEqual(_refresh_redirect("zh", already_running=True), "/?lang=zh&refresh=already_running")

    def test_switching_back_to_english_renders_english_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            model = build_dashboard_model(store=store, now=self.now, lang="en")

            html = _render_dashboard(model)

        self.assertIn("Daily Shortlist", html)
        self.assertIn("Refresh Jobs", html)
        self.assertIn("Apply Filters", html)


def _job(job_id: str, title: str, company: str, observed_at: datetime, source: str, description: str, state: str = "active") -> Job:
    location = Location(city="London", country="United Kingdom", region="Greater London", work_mode=WorkMode.HYBRID, raw="London Hybrid")
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
                canonical_application_url=f"https://example.com/{job_id}/apply",
                raw_payload={"id": job_id},
                latest_observed_state=state,
            )
        ],
        raw_location="London Hybrid",
    )


if __name__ == "__main__":
    unittest.main()
