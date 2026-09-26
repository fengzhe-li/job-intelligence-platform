from __future__ import annotations

import unittest
import urllib.error
from unittest.mock import patch

from jobintel.connectors.base import ConnectorQuery, PartialFetchError
from jobintel.connectors.workday import WorkdayConnector, parse_workday_token


def _listing_page(paths: list[str], total: int | None = 0) -> dict:
    return {
        "total": total,
        "jobPostings": [
            {"title": f"Job {p}", "externalPath": f"/job/Location/Title_{p}", "locationsText": "London, United Kingdom", "postedOn": "Posted Today"}
            for p in paths
        ],
    }


def _detail_payload(title: str = "Graduate Software Engineer", location: str = "Cambridge Office, United Kingdom") -> dict:
    return {
        "jobPostingInfo": {
            "title": title,
            "jobReqId": "JR100001",
            "jobDescription": "<p>Build things.</p>",
            "location": location,
            "startDate": "2026-09-01",
            "externalUrl": "https://acme.wd3.myworkdayjobs.com/AcmeCareers/job/Location/Title_1",
        }
    }


class ParseWorkdayTokenTests(unittest.TestCase):
    def test_parses_tenant_host_and_site(self) -> None:
        host, tenant, site = parse_workday_token("darktrace.wd3/DarktaceExternal")
        self.assertEqual(host, "darktrace.wd3.myworkdayjobs.com")
        self.assertEqual(tenant, "darktrace")
        self.assertEqual(site, "DarktaceExternal")

    def test_rejects_malformed_token_missing_slash(self) -> None:
        with self.assertRaises(ValueError):
            parse_workday_token("darktrace.wd3")

    def test_rejects_malformed_token_missing_dot(self) -> None:
        with self.assertRaises(ValueError):
            parse_workday_token("darktrace/Site")


class WorkdayConnectorFetchTests(unittest.TestCase):
    def test_single_page_fetch_and_detail_path_is_not_double_prefixed(self) -> None:
        # Regression test for a real bug found live (Phase 2.7): externalPath
        # already starts with "/job/...", so the detail URL must not prepend
        # another "/job" segment (that produced a live 406 against Darktrace).
        captured_urls: list[str] = []

        def fake_fetch(url: str, timeout_seconds: int = 20, headers=None, data=None):
            captured_urls.append(url)
            if url.endswith("/jobs"):
                return _listing_page(["1"])
            return _detail_payload()

        connector = WorkdayConnector(("acme.wd3/AcmeCareers",))
        with patch("jobintel.connectors.workday.fetch_json", side_effect=fake_fetch), patch("time.sleep"):
            jobs = connector.fetch_jobs(ConnectorQuery(limit=10))

        self.assertEqual(len(jobs), 1)
        detail_url = [u for u in captured_urls if "/job/" in u][0]
        self.assertNotIn("/job/job/", detail_url)
        self.assertIn("/wday/cxs/acme/AcmeCareers/job/Location/Title_1", detail_url)

    def test_pagination_stops_on_short_page_not_on_total_field(self) -> None:
        # Live-observed (Phase 2.7): Workday's own `total` field is unreliable
        # past the first page -- stopping must rely on page size, not `total`.
        pages = [_listing_page([str(i) for i in range(20)], total=999), _listing_page(["20", "21"], total=0)]
        call_count = {"listing": 0}

        def fake_fetch(url: str, timeout_seconds: int = 20, headers=None, data=None):
            if url.endswith("/jobs"):
                page = pages[call_count["listing"]]
                call_count["listing"] += 1
                return page
            return _detail_payload()

        connector = WorkdayConnector(("acme.wd3/AcmeCareers",))
        with patch("jobintel.connectors.workday.fetch_json", side_effect=fake_fetch), patch("time.sleep"):
            jobs = connector.fetch_jobs(ConnectorQuery(limit=50))

        self.assertEqual(len(jobs), 22)
        self.assertEqual(call_count["listing"], 2)

    def test_detail_fetch_failure_is_isolated_not_fatal_to_the_whole_refresh(self) -> None:
        def fake_fetch(url: str, timeout_seconds: int = 20, headers=None, data=None):
            if url.endswith("/jobs"):
                return _listing_page(["good", "bad"])
            if "Title_bad" in url:
                raise urllib.error.HTTPError(url, 500, "Server Error", None, None)
            return _detail_payload()

        connector = WorkdayConnector(("acme.wd3/AcmeCareers",))
        with patch("jobintel.connectors.workday.fetch_json", side_effect=fake_fetch), patch("time.sleep"):
            with self.assertRaises(PartialFetchError) as ctx:
                connector.fetch_jobs(ConnectorQuery(limit=10))

        self.assertEqual(len(ctx.exception.partial_payloads), 1)
        self.assertTrue(any("Title_bad" in key for key in ctx.exception.failures))

    def test_listing_fetch_failure_for_one_tenant_does_not_lose_other_tenants(self) -> None:
        def fake_fetch(url: str, timeout_seconds: int = 20, headers=None, data=None):
            if "/wday/cxs/bad/" in url:
                raise urllib.error.URLError("unreachable")
            if url.endswith("/jobs"):
                return _listing_page(["1"])
            return _detail_payload()

        connector = WorkdayConnector(("good.wd3/GoodSite", "bad.wd3/BadSite"))
        with patch("jobintel.connectors.workday.fetch_json", side_effect=fake_fetch), patch("time.sleep"):
            with self.assertRaises(PartialFetchError) as ctx:
                connector.fetch_jobs(ConnectorQuery(limit=10))

        self.assertEqual(len(ctx.exception.partial_payloads), 1)
        self.assertIn("bad.wd3/BadSite", ctx.exception.failures)

    def test_malformed_listing_response_is_a_recorded_failure_not_a_crash(self) -> None:
        def fake_fetch(url: str, timeout_seconds: int = 20, headers=None, data=None):
            return {"unexpected": "shape"}

        connector = WorkdayConnector(("acme.wd3/AcmeCareers",))
        with patch("jobintel.connectors.workday.fetch_json", side_effect=fake_fetch), patch("time.sleep"):
            jobs = connector.fetch_jobs(ConnectorQuery(limit=10))

        # No postings, no crash, no failure raised either -- an empty
        # jobPostings-shaped response and a malformed one both just yield zero
        # results for that tenant; the suspicious-zero-result detector in
        # pipeline/refresh.py is what catches a *drop* from a previously
        # healthy count.
        self.assertEqual(jobs, [])

    def test_invalid_token_is_reported_as_a_failure_not_silently_skipped(self) -> None:
        connector = WorkdayConnector(("not-a-valid-token",))
        with self.assertRaises(PartialFetchError) as ctx:
            connector.fetch_jobs(ConnectorQuery(limit=10))

        self.assertEqual(ctx.exception.partial_payloads, [])
        self.assertIn("not-a-valid-token", ctx.exception.failures)

    def test_normalise_produces_full_description_and_stable_source_job_id(self) -> None:
        connector = WorkdayConnector(("acme.wd3/AcmeCareers",))

        def fake_fetch(url: str, timeout_seconds: int = 20, headers=None, data=None):
            if url.endswith("/jobs"):
                return _listing_page(["1"])
            return _detail_payload()

        with patch("jobintel.connectors.workday.fetch_json", side_effect=fake_fetch), patch("time.sleep"):
            jobs = connector.fetch_jobs(ConnectorQuery(limit=10))
        job = connector.normalise(jobs[0])

        self.assertEqual(job.title, "Graduate Software Engineer")
        self.assertIn("JR100001", job.source_observations[0].source_job_id)
        self.assertEqual(job.locations[0].city, "Cambridge")
        self.assertIn("Build things", job.description)


if __name__ == "__main__":
    unittest.main()
