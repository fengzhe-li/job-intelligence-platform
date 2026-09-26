from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from jobintel.connectors.base import ConnectorQuery, PartialFetchError
from jobintel.connectors.prospects import ProspectsConnector

LISTING_HTML = """
<html><body>
<a href="/graduate-jobs/graduate-software-engineer-2706308">Graduate Software Engineer</a>
<a href="/graduate-jobs/graduate-cloud-engineer-2706400">Graduate Cloud Engineer</a>
</body></html>
"""

DETAIL_HTML = """
<html><head>
<script type="application/ld+json">
{
 "@context": "http://schema.org",
 "@type": "JobPosting",
 "datePosted": "2026-08-21",
 "validThrough": "2026-11-15T23:59:00+00:00",
 "title": "Graduate Software Engineer",
 "hiringOrganization": {"name": "Sinara"},
 "description": "Build Python backend services as part of our 2026 graduate intake.",
 "jobLocation": {"address": {"addressLocality": "London", "addressCountry": "United Kingdom"}},
 "url": "https://www.prospects.ac.uk/employer-profiles/sinara-21131/jobs/graduate-software-engineer-2706308"
}
</script>
</head><body></body></html>
"""

DETAIL_HTML_NO_LD = "<html><body>no structured data here</body></html>"


class ProspectsConnectorTests(unittest.TestCase):
    def test_fetch_jobs_parses_json_ld_job_posting(self) -> None:
        connector = ProspectsConnector(category_slugs=("information-technology-69",))
        with patch("jobintel.connectors.prospects.fetch_text", side_effect=[LISTING_HTML, DETAIL_HTML, DETAIL_HTML]), patch("time.sleep"):
            raw_jobs = connector.fetch_jobs(ConnectorQuery(limit=10))

        self.assertEqual(len(raw_jobs), 2)
        job = connector.normalise(raw_jobs[0])
        self.assertEqual(job.title, "Graduate Software Engineer")
        self.assertEqual(job.company, "Sinara")
        self.assertIn("Python backend services", job.description)
        self.assertEqual(job.source_observations[0].deadline, datetime(2026, 11, 15, 23, 59, tzinfo=timezone.utc))
        self.assertEqual(job.source_observations[0].posted_at, datetime(2026, 8, 21, tzinfo=timezone.utc))
        self.assertEqual(job.id, "prospects:2706308")

    def test_missing_json_ld_on_a_detail_page_is_a_recorded_partial_failure_not_silent(self) -> None:
        connector = ProspectsConnector(category_slugs=("information-technology-69",))
        with patch("jobintel.connectors.prospects.fetch_text", side_effect=[LISTING_HTML, DETAIL_HTML, DETAIL_HTML_NO_LD]), patch("time.sleep"):
            with self.assertRaises(PartialFetchError) as ctx:
                connector.fetch_jobs(ConnectorQuery(limit=10))

        self.assertEqual(len(ctx.exception.partial_payloads), 1)
        self.assertTrue(any("JobPosting" in message for message in ctx.exception.failures.values()))

    def test_listing_fetch_failure_is_a_partial_failure(self) -> None:
        connector = ProspectsConnector(category_slugs=("information-technology-69", "engineering-and-manufacturing-172"))

        def fake_fetch(url: str) -> str:
            if "engineering" in url:
                raise ConnectionError("timeout")
            return LISTING_HTML

        with patch("jobintel.connectors.prospects.fetch_text", side_effect=[LISTING_HTML, ConnectionError("timeout"), DETAIL_HTML, DETAIL_HTML]), patch("time.sleep"):
            with self.assertRaises(PartialFetchError) as ctx:
                connector.fetch_jobs(ConnectorQuery(limit=10))

        self.assertTrue(any("listing:" in key for key in ctx.exception.failures))
        self.assertEqual(len(ctx.exception.partial_payloads), 2)

    def test_health_check_never_requires_credentials(self) -> None:
        connector = ProspectsConnector()
        health = connector.health_check()
        self.assertTrue(health.ok)

    def test_a_job_listed_under_two_categories_is_only_fetched_and_emitted_once(self) -> None:
        # Prospects cross-lists some postings under more than one sector category
        # (e.g. an embedded-software role tagged both IT and
        # Engineering-and-manufacturing). Both category listings link to the
        # SAME numeric job id -- the detail page must only be fetched once, and
        # the job must only appear once in the result, not once per category.
        connector = ProspectsConnector(category_slugs=("information-technology-69", "engineering-and-manufacturing-172"))
        with patch("jobintel.connectors.prospects.fetch_text", side_effect=[LISTING_HTML, LISTING_HTML, DETAIL_HTML, DETAIL_HTML]) as mock_fetch, patch("time.sleep"):
            raw_jobs = connector.fetch_jobs(ConnectorQuery(limit=10))

        job_ids = [job.source_job_id for job in raw_jobs]
        self.assertEqual(len(job_ids), len(set(job_ids)))
        # 2 listing fetches (one per category) + 2 detail fetches (one per
        # unique job id, not one per category*job) = 4 total, not 6.
        self.assertEqual(mock_fetch.call_count, 4)


if __name__ == "__main__":
    unittest.main()
