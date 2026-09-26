from __future__ import annotations

import tempfile
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from jobintel.config import default_ranking_config
from jobintel.connectors.adzuna import AdzunaConnector
from jobintel.connectors.base import ConnectorQuery
from jobintel.connectors.workday import WorkdayConnector
from jobintel.dedup.v1 import DIRECT_SOURCES, deduplicate_jobs
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.pipeline.refresh import refresh_connector
from jobintel.storage.local_store import LocalJobStore

FULL_JD = "Join our graduate programme building Python data platforms. Requirements: degree in Computer Science, SQL, cloud. Full description with responsibilities and benefits."
SNIPPET = "Join our graduate programme building Python data..."
WORKDAY_URL = "https://acme.wd3.myworkdayjobs.com/Ext/job/London/Graduate-Software-Engineer_R123"


def _workday_fetch(url, data=None, **kwargs):
    if data is not None:
        return {"jobPostings": [{"externalPath": "/job/London/Graduate-Software-Engineer_R123"}]}
    return {"jobPostingInfo": {"title": "Graduate Software Engineer", "jobReqId": "R123", "jobDescription": f"<p>{FULL_JD}</p>", "location": "London, United Kingdom", "externalUrl": WORKDAY_URL}}


def _adzuna_fetch(url, *args, **kwargs):
    return {
        "count": 1,
        "results": [
            {
                "id": "4455",
                "title": "Graduate Software Engineer",
                "company": {"display_name": "Acme"},
                "location": {"display_name": "London"},
                "description": SNIPPET,
                "redirect_url": "https://www.adzuna.co.uk/jobs/land/ad/4455",
                "created": "2026-09-01T09:00:00Z",
            }
        ],
    }


class WorkdayBeatsAggregatorTests(unittest.TestCase):
    def test_workday_is_a_direct_source(self) -> None:
        self.assertIn("workday", DIRECT_SOURCES)
        self.assertNotIn("adzuna", DIRECT_SOURCES)

    def _run(self, order: tuple[str, ...]):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(tmp)
            config = default_ranking_config()
            with (
                patch("jobintel.connectors.workday.fetch_json", side_effect=_workday_fetch),
                patch("jobintel.connectors.workday.time.sleep"),
                patch("jobintel.connectors.adzuna.fetch_json", side_effect=_adzuna_fetch),
            ):
                for name in order:
                    if name == "workday":
                        refresh_connector(WorkdayConnector(("acme.wd3/Ext",)), store, config, ConnectorQuery(limit=50))
                    else:
                        refresh_connector(AdzunaConnector("id", "key"), store, config, ConnectorQuery(keywords=("graduate",), limit=10))
            return store.read_jobs()

    def _assert_canonical(self, jobs) -> None:
        self.assertEqual(len(jobs), 1, "the same vacancy must collapse into one canonical job")
        job = jobs[0]
        sources = sorted(observation.source_name for observation in job.source_observations)
        self.assertEqual(sources, ["adzuna", "workday"], "both provenance records must be retained")
        self.assertEqual(job.canonical_application_url, WORKDAY_URL)
        self.assertIn("Full description with responsibilities", job.description)
        self.assertEqual(job.source_observations[0].source_name, "workday", "direct observation listed first")
        adzuna = next(observation for observation in job.source_observations if observation.source_name == "adzuna")
        self.assertEqual(adzuna.raw_description, SNIPPET, "aggregator provenance kept verbatim")

    def test_workday_first_then_adzuna(self) -> None:
        self._assert_canonical(self._run(("workday", "adzuna")))

    def test_adzuna_first_then_workday(self) -> None:
        self._assert_canonical(self._run(("adzuna", "workday")))



class SameSourceDistinctPostingsAreNeverMergedTests(unittest.TestCase):
    def _job(self, source: str, source_id: str, title: str):
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        return Job(
            id=f"{source}:{source_id}",
            title=title,
            company="Acme",
            description="Python",
            locations=[Location(city="London", country="United Kingdom")],
            source_observations=[SourceObservation(source, source_id, f"https://x/{source_id}", now, now, now, "Python", f"https://x/{source}/{source_id}/apply")],
        )

    def test_two_ids_from_one_board_with_near_identical_titles_stay_two_jobs(self) -> None:
        jobs = deduplicate_jobs([self._job("greenhouse", "1", "Graduate Software Engineer 1"), self._job("greenhouse", "2", "Graduate Software Engineer 2")])
        self.assertEqual(len(jobs), 2)

    def test_cross_source_duplicate_still_merges(self) -> None:
        jobs = deduplicate_jobs([self._job("greenhouse", "1", "Graduate Software Engineer"), self._job("adzuna", "77", "Graduate Software Engineer")])
        self.assertEqual(len(jobs), 1)


if __name__ == "__main__":
    unittest.main()
