from __future__ import annotations

import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

from jobintel.config import default_ranking_config
from jobintel.connectors.base import ConnectorQuery
from jobintel.connectors.greenhouse import GreenhouseConnector
from jobintel.connectors.lever import LeverConnector
from jobintel.connectors.prospects import ProspectsConnector
from jobintel.connectors.smartrecruiters import SmartRecruitersConnector
from jobintel.connectors.welcome_to_the_jungle import WelcomeToTheJungleConnector, WelcomeToTheJungleManualConnector
from jobintel.connectors.workday import WorkdayConnector
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.pipeline.ingestion import ingest_from_connectors
from jobintel.pipeline.refresh import refresh_connector
from jobintel.storage.local_store import LocalJobStore

# THE invariant under test (connectors.base.SourceSnapshot): absence may only
# imply closure when the current observation is a complete snapshot of the
# relevant source scope. Every case below except the explicitly-complete ones
# must leave previously-seen jobs active.


def _gh_job(job_id: int, title: str = "Graduate Software Engineer") -> dict:
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/b/jobs/{job_id}",
        "updated_at": "2026-09-01T09:00:00+00:00",
        "content": "<p>Python backend.</p>",
        "offices": [{"location": "London, United Kingdom"}],
    }


def _boards(**boards):
    """fetch_json stub: board token -> list of jobs, or an Exception to raise."""

    def fake(url, *args, **kwargs):
        token = url.split("/boards/")[1].split("/")[0]
        value = boards[token]
        if isinstance(value, Exception):
            raise value
        return {"jobs": value}

    return fake


def _refresh(connector, store, limit=100, keywords=()):
    return refresh_connector(connector, store, default_ranking_config(), ConnectorQuery(keywords=keywords, limit=limit))


def _active(store: LocalJobStore, source: str) -> dict[str, bool]:
    return {
        observation.source_job_id: observation.active
        for job in store.read_jobs()
        for observation in job.source_observations
        if observation.source_name == source
    }


class TruncatedObservationTests(unittest.TestCase):
    def test_more_than_100_jobs_on_one_board_never_closes_jobs_beyond_the_cap(self) -> None:
        jobs = [_gh_job(i, f"Graduate Engineer {i}") for i in range(1, 151)]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(big=jobs)):
                _refresh(GreenhouseConnector(("big",)), store, limit=500)
                result = _refresh(GreenhouseConnector(("big",)), store, limit=100)
            active = _active(store, "greenhouse")

        self.assertEqual(len(active), 150)
        self.assertTrue(all(active.values()), "jobs beyond the limit were falsely closed")
        self.assertEqual(result.closed_observations, [])
        self.assertIn("big", result.incomplete_scopes)
        self.assertIn("truncated", result.incomplete_scopes["big"])

    def test_limit_is_per_board_so_a_large_board_cannot_hide_later_boards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            boards = _boards(big=[_gh_job(i, f"Engineer {i}") for i in range(1, 121)], small=[_gh_job(1001), _gh_job(1002, "Data Engineer")])
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=boards):
                result = _refresh(GreenhouseConnector(("big", "small")), store, limit=100)
            active = _active(store, "greenhouse")

        self.assertIn("1001", active)
        self.assertIn("1002", active)
        self.assertEqual(result.complete_scopes, ["small"])

    def test_multiple_boards_only_the_complete_board_can_close(self) -> None:
        big_first = [_gh_job(i, f"Engineer {i}") for i in range(1, 121)]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(big=big_first, small=[_gh_job(1001), _gh_job(1002, "Data Engineer")])):
                _refresh(GreenhouseConnector(("big", "small")), store, limit=500)
            # Second refresh: job 5 genuinely gone from `big` (but big is
            # truncated -> unknowable), job 1002 gone from complete `small`.
            big_second = [job for job in big_first if job["id"] != 5]
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(big=big_second, small=[_gh_job(1001)])):
                result = _refresh(GreenhouseConnector(("big", "small")), store, limit=100)
            active = _active(store, "greenhouse")

        self.assertTrue(active["5"], "truncated board must not close")
        self.assertTrue(active["119"])
        self.assertFalse(active["1002"], "complete board may close")
        self.assertEqual(result.closed_observations, [("greenhouse", "1002")])
        self.assertEqual(result.delta_counts["DISAPPEARED"], 1)


class PartialFailureTests(unittest.TestCase):
    def test_failed_board_never_closes_its_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=[_gh_job(1)], b=[_gh_job(2, "Data Engineer")])):
                _refresh(GreenhouseConnector(("a", "b")), store)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=urllib.error.URLError("down"), b=[_gh_job(2, "Data Engineer")])):
                result = _refresh(GreenhouseConnector(("a", "b")), store)
            active = _active(store, "greenhouse")

        self.assertEqual(result.status, "partial")
        self.assertTrue(active["1"])
        self.assertNotIn("a", result.complete_scopes)

    def test_total_failure_never_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=[_gh_job(1)])):
                _refresh(GreenhouseConnector(("a",)), store)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=urllib.error.URLError("down"))):
                result = _refresh(GreenhouseConnector(("a",)), store)
            active = _active(store, "greenhouse")

        self.assertEqual(result.status, "error")
        self.assertEqual(result.jobs_seen, 0)
        self.assertTrue(active["1"])

    def test_unrecognised_response_shape_is_a_failure_not_an_empty_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=[_gh_job(1)])):
                _refresh(GreenhouseConnector(("a",)), store)
            with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"error": "maintenance"}):
                result = _refresh(GreenhouseConnector(("a",)), store)
            active = _active(store, "greenhouse")

        self.assertEqual(result.status, "error")
        self.assertTrue(active["1"])


class FilteredQueryTests(unittest.TestCase):
    def test_keyword_filtered_refresh_never_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            jobs = [_gh_job(1, "Graduate Software Engineer"), _gh_job(2, "Graduate Accountant")]
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=jobs)):
                _refresh(GreenhouseConnector(("a",)), store)
                # Job 2's text no longer matches -- it was filtered, not closed.
                result = _refresh(GreenhouseConnector(("a",)), store, keywords=("software",))
            active = _active(store, "greenhouse")

        self.assertTrue(active["2"])
        self.assertEqual(result.complete_scopes, [])
        self.assertIn("keyword-filtered", result.incomplete_scopes["a"])


class CompleteSnapshotTests(unittest.TestCase):
    def test_genuinely_complete_snapshot_closes_the_absent_job_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=[_gh_job(1), _gh_job(2, "Data Engineer")])):
                _refresh(GreenhouseConnector(("a",)), store)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=[_gh_job(1)])):
                first = _refresh(GreenhouseConnector(("a",)), store)
                second = _refresh(GreenhouseConnector(("a",)), store)
            active = _active(store, "greenhouse")

        self.assertTrue(active["1"])
        self.assertFalse(active["2"])
        self.assertEqual(first.closed_observations, [("greenhouse", "2")])
        # Per-refresh delta, never cumulative: already-closed is not re-closed.
        self.assertEqual(second.closed_observations, [])
        self.assertEqual(second.delta_counts["DISAPPEARED"], 0)
        self.assertEqual(second.state_counts["DISAPPEARED"], 1)

    def test_suspicious_collapse_withholds_closure(self) -> None:
        many = [_gh_job(i, f"Engineer {i}") for i in range(1, 11)]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=many)):
                _refresh(GreenhouseConnector(("a",)), store)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=[])):
                result = _refresh(GreenhouseConnector(("a",)), store)
            active = _active(store, "greenhouse")

        self.assertTrue(all(active.values()))
        self.assertIsNotNone(result.closure_withheld)

    def test_legacy_observation_without_scope_tag_uses_its_board_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            now = datetime(2026, 9, 1, tzinfo=timezone.utc)
            legacy = _stored_job("greenhouse", "77", {"_board_token": "a"}, now)
            unknown = _stored_job("greenhouse", "78", {}, now)
            store.write_jobs([legacy, unknown])
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=[_gh_job(1)])):
                _refresh(GreenhouseConnector(("a",)), store)
            active = _active(store, "greenhouse")

        self.assertFalse(active["77"])
        self.assertTrue(active["78"], "an observation of unknown scope must never be closed")


class ProspectsNeverClosesTests(unittest.TestCase):
    def test_prospects_job_missing_from_a_later_listing_stays_active(self) -> None:
        listing_with = '<a href="/graduate-jobs/acme-graduate-software-engineer-111">x</a>'
        detail = '<script type="application/ld+json">{"@type": "JobPosting", "title": "Graduate Software Engineer", "hiringOrganization": {"name": "Acme"}, "description": "Python", "url": "https://acme.example/apply"}</script>'

        def fetch_first(url, *args, **kwargs):
            return listing_with if "browse-graduate-jobs" in url else detail

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.prospects.fetch_text", side_effect=fetch_first), patch("jobintel.connectors.prospects.time.sleep"):
                _refresh(ProspectsConnector(), store)
            with patch("jobintel.connectors.prospects.fetch_text", return_value="<html>no links</html>"):
                unfiltered = _refresh(ProspectsConnector(), store)
            with patch("jobintel.connectors.prospects.fetch_text", return_value="<html>no links</html>"):
                filtered = _refresh(ProspectsConnector(), store, keywords=("software",))
            active = _active(store, "prospects")

        self.assertEqual(active, {"111": True})
        self.assertEqual(unfiltered.complete_scopes, [])
        self.assertEqual(filtered.closed_observations, [])


class PaginationCapTests(unittest.TestCase):
    def test_workday_page_cap_makes_the_tenant_incomplete(self) -> None:
        def page(offset: int) -> dict:
            return {"jobPostings": [{"externalPath": f"/job/London/Eng_{offset + i}"} for i in range(20)]}

        def fake(url, data=None, **kwargs):
            if data is not None:
                return page(data["offset"])
            ident = url.rsplit("_", 1)[1]
            return {"jobPostingInfo": {"title": f"Graduate Engineer {ident}", "jobReqId": ident, "jobDescription": "Python", "location": "London"}}

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.workday.fetch_json", side_effect=fake), patch("jobintel.connectors.workday.time.sleep"), patch("jobintel.connectors.workday.WORKDAY_MAX_PAGES", 2):
                result = _refresh(WorkdayConnector(("acme.wd3/Ext",)), store, limit=1000)

        self.assertEqual(result.complete_scopes, [])
        self.assertIn("page cap", result.incomplete_scopes["acme.wd3/Ext"])

    def test_workday_detail_failure_blocks_closure_for_that_tenant(self) -> None:
        state = {"fail": False}

        def fake(url, data=None, **kwargs):
            if data is not None:
                return {"jobPostings": [{"externalPath": "/job/London/Eng_1"}, {"externalPath": "/job/London/Eng_2"}]}
            ident = url.rsplit("_", 1)[1]
            if state["fail"] and ident == "2":
                raise urllib.error.HTTPError(url, 500, "boom", {}, None)
            return {"jobPostingInfo": {"title": f"Graduate Engineer {ident}", "jobReqId": ident, "jobDescription": "Python", "location": "London"}}

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.workday.fetch_json", side_effect=fake), patch("jobintel.connectors.workday.time.sleep"):
                _refresh(WorkdayConnector(("acme.wd3/Ext",)), store)
                state["fail"] = True
                result = _refresh(WorkdayConnector(("acme.wd3/Ext",)), store)
            active = _active(store, "workday")

        self.assertEqual(result.status, "partial")
        self.assertTrue(active["acme:Ext:2"])

    def test_smartrecruiters_page_cap_makes_the_company_incomplete(self) -> None:
        def fake(url, *args, **kwargs):
            if "/postings/" in url:
                return {}
            return {"content": [{"id": f"p{i}", "name": "Graduate Engineer"} for i in range(10)], "totalFound": 10_000}

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.smartrecruiters.fetch_json", side_effect=fake), patch("jobintel.connectors.smartrecruiters.MAX_PAGES_PER_COMPANY", 1):
                result = _refresh(SmartRecruitersConnector(("acme",)), store, limit=10)

        self.assertEqual(result.complete_scopes, [])

    def test_lever_requests_the_full_listing_not_a_server_side_limit(self) -> None:
        seen_urls: list[str] = []

        def fake(url, *args, **kwargs):
            seen_urls.append(url)
            return [{"id": "x", "text": "Graduate Engineer", "hostedUrl": "https://jobs.lever.co/acme/x"}]

        with tempfile.TemporaryDirectory() as tmp:
            with patch("jobintel.connectors.lever.fetch_json", side_effect=fake):
                result = _refresh(LeverConnector(("acme",)), LocalJobStore(tmp), limit=10)

        self.assertNotIn("limit=", seen_urls[0])
        self.assertEqual(result.complete_scopes, ["acme"])


class NonRefreshPathsNeverCloseTests(unittest.TestCase):
    def test_wttj_api_refresh_never_closes_manual_wttj_imports(self) -> None:
        manual = {"url": "https://www.welcometothejungle.com/en/companies/acme/jobs/grad-dev", "title": "Graduate Developer", "company": "Acme", "description": "Python", "location": "London"}
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            ingest_from_connectors([WelcomeToTheJungleManualConnector((manual,))], ConnectorQuery(limit=5), store=store)
            with patch("jobintel.connectors.welcome_to_the_jungle.fetch_json", return_value={"jobs": []}):
                result = _refresh(WelcomeToTheJungleConnector(("acme-org",), "key"), store)
            active = _active(store, "welcome_to_the_jungle")

        self.assertEqual(result.complete_scopes, ["official_api:acme-org"])
        self.assertTrue(all(active.values()))
        self.assertEqual(len(active), 1)

    def test_one_shot_ingest_never_closes_anything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=[_gh_job(1)])):
                _refresh(GreenhouseConnector(("a",)), store)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(a=[])):
                ingest_from_connectors([GreenhouseConnector(("a",))], ConnectorQuery(limit=10), store=store)
            active = _active(store, "greenhouse")

        self.assertTrue(active["1"])


def _stored_job(source: str, job_id: str, raw_payload: dict, now: datetime) -> Job:
    return Job(
        id=f"{source}:{job_id}",
        title=f"Legacy Role {job_id}",
        company="Legacy Co",
        description="Python",
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name=source,
                source_job_id=job_id,
                original_url=f"https://example.com/{job_id}",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description="Python",
                canonical_application_url=f"https://example.com/{job_id}/apply",
                raw_payload=raw_payload,
            )
        ],
    )


if __name__ == "__main__":
    unittest.main()
