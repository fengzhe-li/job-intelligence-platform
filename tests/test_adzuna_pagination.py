from __future__ import annotations

import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

from jobintel.connectors.adzuna import AdzunaConnector
from jobintel.connectors.base import ConnectorQuery, PartialFetchError


def _page(ids: list[str], count: int) -> dict:
    return {
        "count": count,
        "results": [
            {
                "id": job_id,
                "title": f"Graduate Software Engineer {job_id}",
                "company": {"display_name": "Demo Co"},
                "location": {"display_name": "London"},
                "redirect_url": f"https://adzuna.example/job/{job_id}",
                "created": "2026-09-01T09:00:00Z",
            }
            for job_id in ids
        ],
    }


class AdzunaPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connector = AdzunaConnector("id", "key")

    def test_single_page_when_results_fit_in_one_page(self) -> None:
        with patch("jobintel.connectors.adzuna.fetch_json", return_value=_page(["1", "2", "3"], 3)) as mock_fetch:
            jobs = self.connector.fetch_jobs(ConnectorQuery(limit=10))

        self.assertEqual(len(jobs), 3)
        self.assertEqual(mock_fetch.call_count, 1)

    def test_paginates_across_multiple_pages_to_reach_limit(self) -> None:
        pages = [
            _page([str(i) for i in range(50)], 120),
            _page([str(i) for i in range(50, 100)], 120),
            _page([str(i) for i in range(100, 120)], 120),
        ]

        def fake_fetch(url: str):
            page_num = int(url.split("/search/")[1].split("?")[0])
            return pages[page_num - 1]

        with patch("jobintel.connectors.adzuna.fetch_json", side_effect=fake_fetch) as mock_fetch:
            jobs = self.connector.fetch_jobs(ConnectorQuery(limit=120))

        self.assertEqual(len(jobs), 120)
        self.assertEqual(mock_fetch.call_count, 3)
        self.assertEqual({job.source_job_id for job in jobs}, {str(i) for i in range(120)})

    def test_stops_once_reported_total_count_is_reached(self) -> None:
        pages = [_page([str(i) for i in range(50)], 75), _page([str(i) for i in range(50, 75)], 75)]

        def fake_fetch(url: str):
            page_num = int(url.split("/search/")[1].split("?")[0])
            return pages[page_num - 1]

        with patch("jobintel.connectors.adzuna.fetch_json", side_effect=fake_fetch) as mock_fetch:
            jobs = self.connector.fetch_jobs(ConnectorQuery(limit=500))

        # Adzuna itself only has 75 matching jobs -- must not keep requesting
        # pages 3+ once count is satisfied.
        self.assertEqual(len(jobs), 75)
        self.assertEqual(mock_fetch.call_count, 2)

    def test_partial_page_failure_still_returns_earlier_pages_as_partial(self) -> None:
        def fake_fetch(url: str):
            page_num = int(url.split("/search/")[1].split("?")[0])
            if page_num == 1:
                return _page([str(i) for i in range(50)], 120)
            raise urllib.error.URLError("rate limited")

        with patch("jobintel.connectors.adzuna.fetch_json", side_effect=fake_fetch):
            with self.assertRaises(PartialFetchError) as ctx:
                self.connector.fetch_jobs(ConnectorQuery(limit=120))

        exc = ctx.exception
        # Page 1's results must not be lost just because later pages failed.
        self.assertEqual(len(exc.partial_payloads), 50)
        self.assertIn("page_2", exc.failures)

    def test_first_page_failure_is_a_total_failure_not_partial(self) -> None:
        with patch("jobintel.connectors.adzuna.fetch_json", side_effect=urllib.error.URLError("network down")):
            with self.assertRaises(PartialFetchError) as ctx:
                self.connector.fetch_jobs(ConnectorQuery(limit=50))

        exc = ctx.exception
        # No pages ever succeeded -- refresh.py must treat this as a total
        # failure (empty partial_payloads), never as "0 new jobs" on its own.
        self.assertEqual(exc.partial_payloads, [])
        self.assertIn("page_1", exc.failures)

    def test_cross_page_duplicate_ids_are_deduplicated_within_one_fetch(self) -> None:
        pages = [
            _page([str(i) for i in range(50)], 90),
            # Adzuna's own ordering can shift mid-pagination: page 2 repeats "49"
            # from page 1 alongside genuinely new ids.
            _page(["49"] + [str(i) for i in range(50, 90)], 90),
        ]

        def fake_fetch(url: str):
            page_num = int(url.split("/search/")[1].split("?")[0])
            return pages[page_num - 1]

        with patch("jobintel.connectors.adzuna.fetch_json", side_effect=fake_fetch):
            jobs = self.connector.fetch_jobs(ConnectorQuery(limit=200))

        ids = [job.source_job_id for job in jobs]
        self.assertEqual(len(ids), len(set(ids)))

    def test_stops_after_max_consecutive_page_failures_rather_than_hammering_the_api(self) -> None:
        call_count = 0

        def fake_fetch(url: str):
            nonlocal call_count
            call_count += 1
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)

        with patch("jobintel.connectors.adzuna.fetch_json", side_effect=fake_fetch):
            with self.assertRaises(PartialFetchError):
                self.connector.fetch_jobs(ConnectorQuery(limit=1000))

        # Capped at ADZUNA_MAX_CONSECUTIVE_PAGE_FAILURES (3), not one attempt per
        # of the up-to-20 pages a limit=1000 query could otherwise justify.
        self.assertEqual(call_count, 3)

    def test_empty_results_page_stops_pagination_without_extra_requests(self) -> None:
        # `count` is deliberately overstated (as Adzuna's own count field can lag
        # reality) so the only thing that can stop pagination here is noticing
        # page 2 came back empty, not the count-based early exit.
        pages = [_page([str(i) for i in range(10)], 999), _page([], 999)]

        def fake_fetch(url: str):
            page_num = int(url.split("/search/")[1].split("?")[0])
            if page_num <= 2:
                return pages[page_num - 1]
            raise AssertionError(f"should not have requested page {page_num}")

        with patch("jobintel.connectors.adzuna.fetch_json", side_effect=fake_fetch) as mock_fetch:
            jobs = self.connector.fetch_jobs(ConnectorQuery(limit=200))

        self.assertEqual(len(jobs), 10)
        self.assertEqual(mock_fetch.call_count, 2)

    def test_results_per_page_never_exceeds_adzuna_documented_cap(self) -> None:
        captured_urls: list[str] = []

        def fake_fetch(url: str):
            captured_urls.append(url)
            return _page(["1"], 1)

        with patch("jobintel.connectors.adzuna.fetch_json", side_effect=fake_fetch):
            self.connector.fetch_jobs(ConnectorQuery(limit=5000))

        self.assertIn("results_per_page=50", captured_urls[0])


if __name__ == "__main__":
    unittest.main()
