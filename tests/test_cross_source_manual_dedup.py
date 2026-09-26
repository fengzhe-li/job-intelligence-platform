from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jobintel.config import default_ranking_config
from jobintel.connectors.base import ConnectorQuery
from jobintel.connectors.graduate_sources import TRACKR
from jobintel.connectors.greenhouse import GreenhouseConnector
from jobintel.connectors.manual_source import ManualSourceConnector
from jobintel.pipeline.ingestion import ingest_from_connectors
from jobintel.storage.local_store import LocalJobStore

# Regression test for Phase 2.6 Section F: a job discovered by two different
# routes -- a company's own Greenhouse board (automated) and a manual Trackr
# import (the user found the same graduate scheme listed there too) -- must
# collapse into ONE canonical Job with BOTH source observations preserved,
# never two separate jobs/applications for what is really one vacancy.

WILLIAMS_GREENHOUSE_JOB = {
    "id": "wr-1",
    "title": "Graduate Software Engineer",
    "absolute_url": "https://boards.greenhouse.io/williamsracing/jobs/wr-1",
    "updated_at": "2026-09-01T09:00:00+00:00",
    "content": "<p>Build race-team software at Williams Racing.</p>",
    "offices": [{"location": "Oxford, United Kingdom"}],
}


class CrossSourceManualImportDedupTests(unittest.TestCase):
    def test_manual_trackr_import_merges_with_existing_greenhouse_observation(self) -> None:
        greenhouse = GreenhouseConnector(("williamsracing",))
        trackr_record = {
            "job_url": "https://app.the-trackr.com/programme/williams-racing-graduate-software-engineer",
            "title": "Graduate Software Engineer",
            "company": "Williams Racing",
            "location": "Oxford, United Kingdom",
            "description": "Graduate software engineering scheme at Williams Racing, as listed on The Trackr.",
            "deadline": "2026-11-30T23:59:00+00:00",
        }
        manual = ManualSourceConnector(TRACKR, (trackr_record,))

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": [WILLIAMS_GREENHOUSE_JOB]}):
                ingest_from_connectors(
                    [greenhouse, manual],
                    query=ConnectorQuery(keywords=(), location="United Kingdom", limit=50),
                    store=store,
                    config=default_ranking_config(),
                )
            jobs = store.read_jobs()

        # Exactly one canonical job, not two.
        matching = [job for job in jobs if "williams" in job.company.casefold()]
        self.assertEqual(len(matching), 1)
        job = matching[0]

        source_names = {obs.source_name for obs in job.source_observations}
        self.assertEqual(source_names, {"greenhouse", "trackr"})

        # Both observations' own facts survive the merge -- the Trackr deadline
        # is not lost just because Greenhouse (a DIRECT_SOURCES source) won
        # preference for the merged title/description.
        trackr_obs = next(obs for obs in job.source_observations if obs.source_name == "trackr")
        self.assertEqual(trackr_obs.deadline, datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc))
        self.assertEqual(job.deadline_observations.get("trackr"), datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc))

        greenhouse_obs = next(obs for obs in job.source_observations if obs.source_name == "greenhouse")
        self.assertEqual(greenhouse_obs.source_job_id, "wr-1")

    def test_merged_job_produces_exactly_one_trackable_application_id(self) -> None:
        # Applications are keyed by job_id (see cli _application_create), so a
        # single merged canonical Job id naturally prevents two applications
        # for the same underlying vacancy -- confirm the merge really does
        # produce one job id, not two ids that happen to look similar.
        greenhouse = GreenhouseConnector(("williamsracing",))
        trackr_record = {
            "job_url": "https://app.the-trackr.com/programme/williams-racing-graduate-software-engineer",
            "title": "Graduate Software Engineer",
            "company": "Williams Racing",
            "location": "Oxford, United Kingdom",
            "description": "Graduate software engineering scheme at Williams Racing, as listed on The Trackr.",
        }
        manual = ManualSourceConnector(TRACKR, (trackr_record,))

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            with patch("jobintel.connectors.greenhouse.fetch_json", return_value={"jobs": [WILLIAMS_GREENHOUSE_JOB]}):
                ingest_from_connectors(
                    [greenhouse, manual],
                    query=ConnectorQuery(keywords=(), location="United Kingdom", limit=50),
                    store=store,
                    config=default_ranking_config(),
                )
            job_ids = {job.id for job in store.read_jobs() if "williams" in job.company.casefold()}

        self.assertEqual(len(job_ids), 1)


if __name__ == "__main__":
    unittest.main()
