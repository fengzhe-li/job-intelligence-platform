from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from jobintel.config import default_ranking_config
from jobintel.connectors.base import ConnectorQuery
from jobintel.connectors.graduate_sources import BRIGHT_NETWORK, GRADCRACKER, GRADUATE_SOURCE_SPECS, PROSPECTS, TRACKR
from jobintel.connectors.manual_source import (
    ManualSourceConnector,
    ManualSourceDiscoveryConnector,
    prepare_discovery_import,
    prepare_manual_import,
)
from jobintel.pipeline.ingestion import ingest_from_connectors
from jobintel.storage.local_store import LocalJobStore


class GraduateSourceSpecTests(unittest.TestCase):
    def test_all_four_sources_registered(self) -> None:
        self.assertEqual(set(GRADUATE_SOURCE_SPECS), {"trackr", "gradcracker", "bright_network", "prospects"})

    def test_url_matchers_accept_own_domain_and_reject_others(self) -> None:
        # the-trackr.com (hyphenated) is the real site -- thetrackr.com (no
        # hyphen) is an unrelated Shopify storefront, confirmed during the
        # Phase 2.5 source audit; must NOT match.
        self.assertTrue(TRACKR.is_source_url("https://app.the-trackr.com/programme/example-grad-role"))
        self.assertTrue(TRACKR.is_source_url("https://the-trackr.com/uk-technology/example-grad-role"))
        self.assertFalse(TRACKR.is_source_url("https://www.thetrackr.com/jobs/example-grad-role"))
        self.assertFalse(TRACKR.is_source_url("https://gradcracker.com/jobs/example-grad-role"))
        self.assertTrue(GRADCRACKER.is_source_url("https://www.gradcracker.com/jobs/example-role"))
        self.assertTrue(BRIGHT_NETWORK.is_source_url("https://www.brightnetwork.co.uk/graduate-jobs/example/"))
        self.assertTrue(PROSPECTS.is_source_url("https://www.prospects.ac.uk/jobs-and-work-experience/example"))

    def test_source_brand_does_not_get_a_ranking_boost_over_other_aggregators(self) -> None:
        config = default_ranking_config()
        for source_name in ("trackr", "gradcracker", "bright_network", "prospects"):
            self.assertEqual(config.source_weights[source_name], config.source_weights["adzuna"])


class GraduateSourceManualImportTests(unittest.TestCase):
    def test_csv_import_missing_required_fields_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gradcracker.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["job_url", "title", "company"])
                writer.writeheader()
                writer.writerow({"job_url": "https://www.gradcracker.com/jobs/graduate-software-engineer", "title": "", "company": "Example Co"})

            prepared = prepare_manual_import(GRADCRACKER, path)

        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].status, "missing_required_fields")

    def test_json_import_normalises_and_feeds_pipeline(self) -> None:
        record = {
            "job_url": "https://app.the-trackr.com/programme/graduate-software-engineer-london",
            "title": "Graduate Software Engineer",
            "company": "Example Co",
            "location": "London, United Kingdom",
            "description": "Build Python and PostgreSQL backend services. 2027 graduates welcome.",
            "posted_at": "2026-08-01T09:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as tmp:
            import_path = Path(tmp) / "trackr.json"
            import_path.write_text(json.dumps([record]), encoding="utf-8")
            prepared = prepare_manual_import(TRACKR, import_path)
            self.assertEqual(prepared[0].status, "imported")

            connector = ManualSourceConnector(TRACKR, tuple(item.record for item in prepared if item.record))
            store = LocalJobStore(Path(tmp) / "store")
            jobs = ingest_from_connectors(
                [connector],
                query=ConnectorQuery(keywords=(), limit=10),
                store=store,
            )

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.company, "Example Co")
        self.assertTrue(any(observation.source_name == "trackr" for observation in job.source_observations))
        self.assertIsNotNone(job.graduation_year)

    def test_discovery_import_allows_missing_description(self) -> None:
        record = {
            "job_url": "https://www.prospects.ac.uk/jobs-and-work-experience/graduate-role",
            "title": "Graduate Scheme",
            "company": "Example Co",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prospects_discovery.json"
            path.write_text(json.dumps([record]), encoding="utf-8")
            prepared = prepare_discovery_import(PROSPECTS, path)
            self.assertEqual(prepared[0].status, "imported")

            connector = ManualSourceDiscoveryConnector(PROSPECTS, tuple(item.record for item in prepared if item.record))
            store = LocalJobStore(Path(tmp) / "store")
            jobs = ingest_from_connectors(
                [connector],
                query=ConnectorQuery(keywords=(), limit=10),
                store=store,
            )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source_observations[0].raw_payload["_enrichment_state"], "discovery_only")


if __name__ == "__main__":
    unittest.main()
