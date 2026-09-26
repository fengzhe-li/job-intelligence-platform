from __future__ import annotations

import unittest
from datetime import datetime, timezone

from jobintel.connectors.adzuna import AdzunaConnector, adzuna_job_url
from jobintel.connectors.base import RawJobPayload
from jobintel.dedup.v1 import deduplicate_jobs
from jobintel.models.job import Job, Location, SourceObservation

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)

# Regressions found by the 2026-09-23 live Adzuna validation, using the real
# payload shapes the /gb/ API returned.


def _normalise(location: dict) -> Job:
    payload = {"id": "5885931491", "title": "Software Engineer", "company": {"display_name": "Mercedes-AMG"}, "location": location, "description": "C++", "created": "2026-09-16T10:58:08Z"}
    raw = RawJobPayload("adzuna", "5885931491", adzuna_job_url("5885931491"), adzuna_job_url("5885931491"), payload, NOW)
    return AdzunaConnector("id", "key").normalise(raw)


class AdzunaUkLocationTests(unittest.TestCase):
    def test_uk_place_without_a_uk_token_is_still_uk(self) -> None:
        for display, area in (
            ("Silverstone, Towcester", ["UK", "East Midlands", "Northamptonshire", "Towcester", "Silverstone"]),
            ("Cheltenham, Gloucestershire", ["UK", "South West England", "Gloucestershire", "Cheltenham"]),
            ("Brixworth, Northampton", ["UK", "East Midlands", "Northamptonshire", "Northampton", "Brixworth"]),
        ):
            job = _normalise({"display_name": display, "area": area})
            self.assertTrue(job.locations[0].is_uk, display)
            self.assertEqual(job.locations[0].city, display.split(",")[0])

    def test_display_name_already_naming_the_uk_is_not_duplicated(self) -> None:
        job = _normalise({"display_name": "London, UK", "area": ["UK", "London"]})
        self.assertEqual(job.raw_location, "London, UK")

    def test_area_only_location_uses_the_structured_hierarchy(self) -> None:
        job = _normalise({"area": ["UK", "East Midlands", "Leicestershire"]})
        self.assertEqual(job.raw_location, "East Midlands, Leicestershire, UK")
        self.assertTrue(job.locations[0].is_uk)

    def test_location_without_area_is_unchanged(self) -> None:
        self.assertEqual(_normalise({"display_name": "Reading"}).raw_location, "Reading")
        self.assertEqual(_normalise({}).raw_location, "United Kingdom")


def _job(source: str, source_id: str, title: str, company: str = "Ilmor Engineering Ltd") -> Job:
    return Job(
        id=f"{source}:{source_id}",
        title=title,
        company=company,
        description="Engine design",
        locations=[Location(city="Brixworth", country="United Kingdom")],
        source_observations=[SourceObservation(source, source_id, f"https://x/{source_id}", NOW, NOW, NOW, "Engine design", f"https://x/{source}/{source_id}/apply")],
    )


class SeniorityAwareDedupTests(unittest.TestCase):
    def test_different_seniority_is_never_the_same_vacancy_across_sources(self) -> None:
        jobs = deduplicate_jobs([_job("greenhouse", "1", "Senior Design Engineer"), _job("adzuna", "2", "Design Engineer")])
        self.assertEqual(len(jobs), 2)
        jobs = deduplicate_jobs([_job("greenhouse", "1", "Senior Software Engineer"), _job("adzuna", "2", "Graduate Software Engineer")])
        self.assertEqual(len(jobs), 2, "a graduate vacancy must never be hidden behind a senior one")

    def test_same_graduate_vacancy_worded_differently_still_merges(self) -> None:
        jobs = deduplicate_jobs([_job("greenhouse", "1", "Graduate Software Engineer"), _job("adzuna", "2", "Software Engineer - Graduate Programme")])
        self.assertEqual(len(jobs), 1)
        jobs = deduplicate_jobs([_job("greenhouse", "1", "Design Engineer"), _job("adzuna", "2", "DESIGN ENGINEER")])
        self.assertEqual(len(jobs), 1)


if __name__ == "__main__":
    unittest.main()
