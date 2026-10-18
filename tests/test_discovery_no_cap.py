"""Direct-ATS discovery never silently truncates at the query's result limit (a 100-job default once
dropped 212 of Palantir's 312 Lever postings and 51 of Octopus Energy's). Listings are fetched until
exhausted; a posting repeated across pages is kept once; a scope that cannot be fully fetched is
marked incomplete with its missing range and never closes stored jobs; reruns are stable."""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from jobintel.connectors.base import SCOPE_SAFETY_CAP, ConnectorQuery
from jobintel.connectors.greenhouse import GreenhouseConnector
from jobintel.connectors.lever import LeverConnector
from jobintel.connectors.smartrecruiters import SmartRecruitersConnector
from jobintel.storage.local_store import LocalJobStore
from test_snapshot_closure_invariant import _active, _boards, _gh_job, _refresh


def _lever(n, prefix="p"):
    return [{"id": f"{prefix}{i}", "text": f"Software Engineer {i}", "hostedUrl": f"https://jobs.lever.co/acme/{prefix}{i}",
             "categories": {"location": "London, United Kingdom"}, "descriptionPlain": "Python"} for i in range(n)]


def _sr_pages(pages, total):
    """fetch_json stub for SmartRecruiters: offset -> page of ids (detail calls answer {})."""
    def fake(url, *args, **kwargs):
        if "/postings/" in url:
            return {}
        offset = int(url.split("offset=")[1].split("&")[0])
        ids = pages.get(offset, [])
        return {"content": [{"id": i, "name": f"Engineer {i}"} for i in ids], "totalFound": total}
    return fake


class NoSilentTruncationTests(unittest.TestCase):
    def test_more_than_100_listed_jobs_are_all_kept_under_the_default_query_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.lever.fetch_json", return_value=_lever(312)):
                result = _refresh(LeverConnector(("acme",)), store, limit=100)
            self.assertEqual(len(_active(store, "lever")), 312)
            self.assertEqual(result.complete_scopes, ["acme"])
            self.assertEqual(result.incomplete_scopes, {})

    def test_smartrecruiters_paginates_past_100_and_deduplicates_across_pages(self):
        pages = {0: [f"p{i}" for i in range(100)], 100: [f"p{i}" for i in range(95, 150)]}  # p95-p99 repeated
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.smartrecruiters.fetch_json", side_effect=_sr_pages(pages, 155)):
                result = _refresh(SmartRecruitersConnector(("acme",)), store, limit=10)
            active = _active(store, "smartrecruiters")
            self.assertEqual(sorted(active), sorted(f"p{i}" for i in range(150)))  # 150 unique, none twice
            self.assertEqual(result.complete_scopes, ["acme@gb"])

    def test_a_page_cap_marks_the_scope_incomplete_with_the_missing_range(self):
        pages = {0: [f"p{i}" for i in range(100)], 100: [f"p{i}" for i in range(100, 200)]}
        with tempfile.TemporaryDirectory() as tmp:
            with patch("jobintel.connectors.smartrecruiters.fetch_json", side_effect=_sr_pages(pages, 450)), \
                    patch("jobintel.connectors.smartrecruiters.MAX_PAGES_PER_COMPANY", 2):
                result = _refresh(SmartRecruitersConnector(("acme",)), LocalJobStore(tmp))
        self.assertEqual(result.complete_scopes, [])
        self.assertIn("positions 201-450 were not fetched", result.incomplete_scopes["acme@gb"])

    def test_the_safety_cap_is_technical_and_reports_the_missing_positions(self):
        self.assertGreaterEqual(SCOPE_SAFETY_CAP, 5000)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("jobintel.connectors.lever.fetch_json", return_value=_lever(30)):
                result = _refresh(LeverConnector(("acme",)), LocalJobStore(tmp), scope_cap=20)
        self.assertIn("positions 21-30 not kept", result.incomplete_scopes["acme"])

    def test_an_incomplete_refresh_never_closes_previously_stored_jobs(self):
        jobs = [_gh_job(i, f"Graduate Engineer {i}") for i in range(1, 313)]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(big=jobs)):
                _refresh(GreenhouseConnector(("big",)), store)
            with patch("jobintel.connectors.greenhouse.fetch_json", side_effect=_boards(big=jobs[:150])):
                result = _refresh(GreenhouseConnector(("big",)), store, scope_cap=100)  # truncated observation
            active = _active(store, "greenhouse")
        self.assertEqual(len(active), 312)
        self.assertTrue(all(active.values()))
        self.assertEqual(result.closed_observations, [])

    def test_a_rerun_with_the_same_listing_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            with patch("jobintel.connectors.lever.fetch_json", return_value=_lever(150)):
                first = _refresh(LeverConnector(("acme",)), store)
                ids_first = sorted(j.id for j in store.read_jobs())
                second = _refresh(LeverConnector(("acme",)), store)
            ids_second = sorted(j.id for j in store.read_jobs())
            self.assertEqual(ids_first, ids_second)
            self.assertEqual(len(ids_second), 150)
            self.assertEqual(second.closed_observations, [])
            self.assertTrue(all(_active(store, "lever").values()))
            self.assertEqual(first.complete_scopes, second.complete_scopes)


if __name__ == "__main__":
    unittest.main()
