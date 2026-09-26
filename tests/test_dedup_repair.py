from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobintel.dedup.repair import plan_repair, repair_historical_merges
from jobintel.models.application import Application
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import WorkflowStatus
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.local_store import LocalJobStore

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _ashby_obs(job_id: str, title: str, first: datetime = T0, active: bool = True) -> SourceObservation:
    payload = {"id": job_id, "title": title, "location": "London", "descriptionPlain": f"{title} building trading systems.", "_board_name": "transficc", "jobUrl": f"https://jobs.ashbyhq.com/transficc/{job_id}"}
    return SourceObservation("ashby", job_id, payload["jobUrl"], first, T1, first, payload["descriptionPlain"], payload["jobUrl"], raw_payload=payload, active=active, latest_observed_state="UNCHANGED" if active else "DISAPPEARED")


def _adzuna_obs(job_id: str, title: str, place: str, area: list[str], company: str = "Lloyds Bank") -> SourceObservation:
    payload = {"id": job_id, "title": title, "company": {"display_name": company}, "location": {"display_name": place, "area": area}, "description": f"{title} snippet", "created": "2026-09-10T09:00:00Z"}
    url = f"https://www.adzuna.co.uk/jobs/details/{job_id}"
    return SourceObservation("adzuna", job_id, url, T0, T1, T0, payload["description"], url, raw_payload=payload)


def _greenhouse_obs(job_id: str, title: str, board: str = "transficc") -> SourceObservation:
    payload = {"id": int(job_id), "title": title, "content": f"<p>{title}</p>", "offices": [{"location": "London, United Kingdom"}], "_board_token": board, "absolute_url": f"https://boards.greenhouse.io/{board}/jobs/{job_id}"}
    return SourceObservation("greenhouse", job_id, payload["absolute_url"], T0, T1, T0, title, payload["absolute_url"], raw_payload=payload)


def _merged(job_id: str, title: str, company: str, observations: list[SourceObservation]) -> Job:
    """A canonical job exactly as the OLD rules persisted it."""
    return Job(id=job_id, title=title, company=company, description="merged", locations=[Location(city="London", country="United Kingdom")], source_observations=observations)


def _transficc() -> Job:
    return _merged("ashby:a1", "Senior Software Engineer", "transficc", [_ashby_obs("a1", "Senior Software Engineer "), _ashby_obs("a2", "Software Engineer", first=T0 - timedelta(days=5)), _ashby_obs("a3", "Junior Software Engineer ", active=False)])


class SplitTests(unittest.TestCase):
    def test_junior_mid_senior_become_three_vacancies_with_provenance_verbatim(self) -> None:
        original = _transficc()
        repaired, cases = plan_repair([original], set())
        by_obs = {job.source_observations[0].source_job_id: job for job in repaired}

        self.assertEqual(len(repaired), 3)
        self.assertEqual(cases[0].action, "split")
        self.assertEqual(by_obs["a1"].id, "ashby:a1", "the original canonical id stays with its own observation")
        self.assertEqual(by_obs["a3"].title, "Junior Software Engineer")
        self.assertEqual(by_obs["a2"].title, "Software Engineer")
        for job in repaired:
            (observation,) = job.source_observations
            self.assertIs(observation, next(o for o in original.source_observations if o.source_job_id == observation.source_job_id))
        self.assertEqual(by_obs["a2"].source_observations[0].first_seen_at, T0 - timedelta(days=5))
        self.assertFalse(by_obs["a3"].source_observations[0].active, "known inactive state is preserved, not reset")
        self.assertEqual(by_obs["a3"].graduation_year is not None, True, "enrichment recomputed")

    def test_same_programme_in_two_locations_splits(self) -> None:
        job = _merged("adzuna:1", "Software Engineering Industrial Placement (Edinburgh)", "Lloyds Bank", [
            _adzuna_obs("1", "Software Engineering Industrial Placement (Edinburgh)", "Edinburgh", ["UK", "Scotland", "Edinburgh"]),
            _adzuna_obs("2", "Software Engineering Industrial Placement (Leeds)", "Leeds, West Yorkshire", ["UK", "Yorkshire And The Humber", "West Yorkshire", "Leeds"]),
        ])
        repaired, _ = plan_repair([job], set())
        self.assertEqual(sorted(j.id for j in repaired), ["adzuna:1", "adzuna:2"])
        self.assertTrue(all(j.locations[0].is_uk for j in repaired))

    def test_legitimate_cross_source_duplicate_is_left_untouched(self) -> None:
        job = _merged("greenhouse:10", "Graduate Software Engineer", "transficc", [_greenhouse_obs("10", "Graduate Software Engineer"), _ashby_obs("z9", "Graduate Software Engineer")])
        repaired, cases = plan_repair([job], set())
        self.assertIs(repaired[0], job)
        self.assertEqual(cases, [])


class AmbiguityTests(unittest.TestCase):
    def test_referenced_job_is_never_split(self) -> None:
        job = _transficc()
        repaired, cases = plan_repair([job], {job.id})
        self.assertEqual(repaired, [job])
        self.assertEqual(cases[0].action, "ambiguous")
        self.assertIn("references", cases[0].reason)

    def test_split_off_matching_another_existing_job_is_left_for_review(self) -> None:
        job = _transficc()
        elsewhere = _merged("greenhouse:77", "Junior Software Engineer", "transficc", [_greenhouse_obs("77", "Junior Software Engineer")])
        repaired, cases = plan_repair([job, elsewhere], set())
        self.assertEqual(len(repaired), 2)
        self.assertEqual(cases[0].action, "ambiguous")
        self.assertIn("greenhouse:77", cases[0].reason)

    def test_unknown_source_is_never_guessed(self) -> None:
        other = SourceObservation("mystery_board", "x2", "u", T0, T1, T0, "d", "u2")
        job = _merged("ashby:a1", "Engineer", "transficc", [_ashby_obs("a1", "Engineer"), other, SourceObservation("mystery_board", "x3", "u", T0, T1, T0, "d", "u3")])
        repaired, cases = plan_repair([job], set())
        self.assertEqual(repaired, [job])
        self.assertEqual(cases[0].action, "ambiguous")


class RealStoreFlowTests(unittest.TestCase):
    def test_backup_split_log_and_idempotent_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "local")
            store.write_jobs([_transficc(), _merged("greenhouse:10", "Graduate Software Engineer", "transficc", [_greenhouse_obs("10", "Graduate Software Engineer"), _ashby_obs("z9", "Graduate Software Engineer")])])

            dry = repair_historical_merges(store.root, Path(tmp) / "backups", dry_run=True)
            self.assertEqual((dry.jobs_after, dry.backup_path), (4, None))
            self.assertEqual(len(store.read_jobs()), 2, "dry run writes nothing")

            first = repair_historical_merges(store.root, Path(tmp) / "backups")
            backup_jobs = LocalJobStore(first.backup_path).read_jobs()
            second = repair_historical_merges(store.root, Path(tmp) / "backups")
            ids = [o.source_job_id for j in store.read_jobs() for o in j.source_observations]
            log_lines = (store.processed_dir / "dedup_repair_log.jsonl").read_text().splitlines()

        self.assertEqual((first.jobs_before, first.jobs_after, len(first.splits)), (2, 4, 1))
        self.assertEqual(len(backup_jobs), 2, "backup holds the pre-migration store")
        self.assertEqual((second.jobs_after, second.cases, second.backup_path), (4, [], None), "rerun is a no-op")
        self.assertEqual(len(ids), len(set(ids)), "no observation duplicated")
        self.assertEqual(len(log_lines), 1)

    def test_workflow_or_application_reference_blocks_the_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "local")
            store.write_jobs([_transficc()])
            store.set_workflow_status("ashby:a1", WorkflowStatus.SAVED)
            report = repair_historical_merges(store.root, Path(tmp) / "backups")
            self.assertEqual((len(report.splits), len(report.ambiguous), len(store.read_jobs())), (0, 1, 1))

            store2 = LocalJobStore(Path(tmp) / "local2")
            store2.write_jobs([_transficc()])
            ApplicationStore(store2.root).create_application(Application(id="app:ashby:a1", job_id="ashby:a1", company="transficc", role_title="x"))
            report2 = repair_historical_merges(store2.root, Path(tmp) / "backups")
            self.assertEqual(len(report2.ambiguous), 1)
            self.assertEqual(json.loads((store2.root / "applications" / "applications.json").read_text())[0]["job_id"], "ashby:a1")

    def test_later_refresh_does_not_re_merge_split_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "local")
            store.write_jobs([_transficc()])
            repair_historical_merges(store.root, Path(tmp) / "backups")
            refreshed = _transficc().source_observations[2]
            store.write_jobs([Job(id="ashby:a3", title="Junior Software Engineer", company="transficc", description="d", locations=[Location(city="London", country="United Kingdom")], source_observations=[refreshed])])
            self.assertEqual(len(store.read_jobs()), 3)


if __name__ == "__main__":
    unittest.main()
