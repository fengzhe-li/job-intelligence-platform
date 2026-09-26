from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jobintel.config import default_ranking_config
from jobintel.connectors.base import ConnectorQuery
from jobintel.connectors.manual_source import ManualSourceConnector, ManualSourceSpec, prepare_single_url_import
from jobintel.pipeline.ingestion import ingest_from_connectors
from jobintel.storage.local_store import LocalJobStore

JOB_POSTING_HTML = """
<html><head>
<script type="application/ld+json">
{
 "@context": "http://schema.org",
 "@type": "JobPosting",
 "datePosted": "2026-09-01",
 "validThrough": "2026-10-20T23:59:00+00:00",
 "title": "Graduate Platform Engineer",
 "hiringOrganization": {"name": "Acme Robotics"},
 "description": "Build internal platform tooling as part of our 2026 graduate intake.",
 "jobLocation": {"address": {"addressLocality": "Bristol", "addressCountry": "United Kingdom"}}
}
</script>
</head><body></body></html>
"""

NO_JSON_LD_HTML = "<html><body>Careers page with no structured data.</body></html>"


class PrepareSingleUrlImportTests(unittest.TestCase):
    def test_auto_fetches_all_fields_from_job_posting_json_ld(self) -> None:
        result = prepare_single_url_import(
            "https://acme.example/careers/graduate-platform-engineer",
            fetch_text_func=lambda url: JOB_POSTING_HTML,
        )

        self.assertEqual(result.status, "auto_fetched")
        self.assertEqual(result.record["title"], "Graduate Platform Engineer")
        self.assertEqual(result.record["company"], "Acme Robotics")
        self.assertIn("Bristol", result.record["location"])
        self.assertEqual(result.record["deadline"], "2026-10-20T23:59:00+00:00")
        self.assertIn("title", result.auto_fetched_fields)

    def test_explicit_override_wins_over_auto_fetched_value(self) -> None:
        result = prepare_single_url_import(
            "https://acme.example/careers/graduate-platform-engineer",
            overrides={"title": "Graduate Platform Engineer (Manchester)"},
            fetch_text_func=lambda url: JOB_POSTING_HTML,
        )

        self.assertEqual(result.record["title"], "Graduate Platform Engineer (Manchester)")
        # title was overridden, so it must not be reported as auto-fetched.
        self.assertNotIn("title", result.auto_fetched_fields)
        self.assertIn("company", result.auto_fetched_fields)

    def test_no_json_ld_falls_back_to_manual_overrides_never_fabricates(self) -> None:
        result = prepare_single_url_import(
            "https://smallco.example/jobs/1",
            overrides={"title": "Graduate Software Engineer", "company": "Small Co"},
            fetch_text_func=lambda url: NO_JSON_LD_HTML,
        )

        self.assertEqual(result.status, "manual_fields_only")
        self.assertEqual(result.record["title"], "Graduate Software Engineer")
        self.assertEqual(result.record["company"], "Small Co")
        # No description was ever supplied by either auto-fetch or override --
        # must be absent/empty, never invented.
        self.assertFalse(result.record.get("description"))
        self.assertEqual(result.auto_fetched_fields, ())
        self.assertIn("no JobPosting JSON-LD", result.fetch_error)

    def test_missing_title_and_company_with_no_json_ld_is_reported_not_silently_dropped(self) -> None:
        result = prepare_single_url_import(
            "https://smallco.example/jobs/1",
            fetch_text_func=lambda url: NO_JSON_LD_HTML,
        )

        self.assertEqual(result.status, "missing_required_fields")
        self.assertIsNone(result.record.get("title") or None)

    def test_network_failure_during_auto_fetch_is_recorded_not_fatal(self) -> None:
        def failing_fetch(url: str) -> str:
            raise ConnectionError("timeout")

        result = prepare_single_url_import(
            "https://smallco.example/jobs/1",
            overrides={"title": "Graduate Software Engineer", "company": "Small Co"},
            fetch_text_func=failing_fetch,
        )

        self.assertEqual(result.status, "manual_fields_only")
        self.assertIn("ConnectionError", result.fetch_error)

    def test_skip_auto_fetch_uses_only_supplied_overrides(self) -> None:
        calls = []

        def spy_fetch(url: str) -> str:
            calls.append(url)
            return JOB_POSTING_HTML

        result = prepare_single_url_import(
            "https://acme.example/careers/graduate-platform-engineer",
            overrides={"title": "Graduate Software Engineer", "company": "Acme Robotics"},
            fetch_text_func=spy_fetch,
            skip_auto_fetch=True,
        )

        self.assertEqual(calls, [])
        self.assertEqual(result.status, "manual_fields_only")

    def test_imported_job_flows_through_the_canonical_pipeline(self) -> None:
        result = prepare_single_url_import(
            "https://acme.example/careers/graduate-platform-engineer",
            fetch_text_func=lambda url: JOB_POSTING_HTML,
        )
        spec = ManualSourceSpec(source_name="manual", display_name="manual", is_source_url=lambda _url: True)
        connector = ManualSourceConnector(spec, (result.record,))

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            jobs = ingest_from_connectors(
                [connector],
                query=ConnectorQuery(keywords=(), location="United Kingdom", limit=1),
                store=store,
                config=default_ranking_config(),
            )
            stored = store.read_jobs()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].title, "Graduate Platform Engineer")
        self.assertEqual(stored[0].source_observations[0].source_name, "manual")
        # Ran through real enrichment/ranking, not a bypass -- role track / skill
        # extraction populated exactly like every other source.
        self.assertTrue(stored[0].skill_requirements or stored[0].role_track_profile)


if __name__ == "__main__":
    unittest.main()
