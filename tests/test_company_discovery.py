from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jobintel.company_discovery import observe_companies_from_jobs, promote_candidate
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.storage.company_discovery_store import CompanyDiscoveryStore

NOW = datetime(2026, 9, 17, tzinfo=timezone.utc)


def _job(company: str, source_name: str, application_url: str = "https://example.com/jobs/1", job_id: str = "1") -> Job:
    return Job(
        id=f"{source_name}:{job_id}",
        title="Graduate Software Engineer",
        company=company,
        description="Build things.",
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name=source_name,
                source_job_id=job_id,
                original_url=f"https://example.com/{source_name}/{job_id}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                posted_at=NOW,
                raw_description="Build things.",
                canonical_application_url=application_url,
            )
        ],
    )


REGISTRY = [{"company_name": "Palantir"}, {"company_name": "Monzo"}]


class ObserveCompaniesFromJobsTests(unittest.TestCase):
    def test_new_company_from_a_non_registry_source_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CompanyDiscoveryStore(tmp)
            observations = observe_companies_from_jobs([_job("Newco", "prospects")], REGISTRY, store)

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].company_name, "Newco")
        self.assertEqual(observations[0].observed_via_source, "prospects")

    def test_company_already_in_the_registry_is_not_observed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CompanyDiscoveryStore(tmp)
            observations = observe_companies_from_jobs([_job("Palantir", "prospects")], REGISTRY, store)

        self.assertEqual(observations, [])

    def test_company_from_an_already_registry_scoped_source_is_not_observed(self) -> None:
        # A Greenhouse job is BY DEFINITION from a company already in the
        # registry -- "new company" discovery only makes sense for
        # aggregator/manual sources.
        with tempfile.TemporaryDirectory() as tmp:
            store = CompanyDiscoveryStore(tmp)
            observations = observe_companies_from_jobs([_job("Some Registry Co", "greenhouse")], REGISTRY, store)

        self.assertEqual(observations, [])

    def test_same_company_observed_twice_is_only_logged_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CompanyDiscoveryStore(tmp)
            observe_companies_from_jobs([_job("Newco", "prospects", job_id="1")], REGISTRY, store)
            second = observe_companies_from_jobs([_job("Newco", "prospects", job_id="2")], REGISTRY, store)

            self.assertEqual(second, [])
            self.assertEqual(len(store.latest_per_company()), 1)

    def test_application_url_pointing_directly_at_an_ats_yields_a_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CompanyDiscoveryStore(tmp)
            observations = observe_companies_from_jobs(
                [_job("Newco", "adzuna", application_url="https://boards.greenhouse.io/newco/jobs/123")], REGISTRY, store
            )

        self.assertEqual(observations[0].suggested_connector_type, "greenhouse")
        self.assertEqual(observations[0].suggested_connector_token, "newco")

    def test_same_company_seen_through_two_different_sources_in_one_scan_is_logged_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CompanyDiscoveryStore(tmp)
            jobs = [
                _job("Newco", "prospects", job_id="p1"),
                _job("Newco", "adzuna", job_id="a1"),
            ]
            observations = observe_companies_from_jobs(jobs, REGISTRY, store)

            self.assertEqual(len(observations), 1)
            self.assertEqual(len(store.latest_per_company()), 1)

    def test_provenance_is_preserved_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CompanyDiscoveryStore(tmp)
            observe_companies_from_jobs([_job("Newco", "prospects")], REGISTRY, store)
            reloaded = CompanyDiscoveryStore(tmp)
            latest = reloaded.latest_per_company()

        self.assertIn("newco", latest)
        self.assertEqual(latest["newco"].example_job_title, "Graduate Software Engineer")


class PromoteCandidateTests(unittest.TestCase):
    def test_promoting_a_company_with_a_suggested_ats_verifies_and_can_enable_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CompanyDiscoveryStore(tmp)
            observations = observe_companies_from_jobs(
                [_job("Newco", "adzuna", application_url="https://boards.greenhouse.io/newco/jobs/123")], REGISTRY, store
            )
            registry_path = Path(tmp) / "registry.json"
            registry_path.write_text("[]", encoding="utf-8")

            import urllib.error

            def fake_fetch_json(url: str):
                raise urllib.error.URLError("no network in this test")

            from unittest.mock import patch

            with patch("jobintel.ats_discovery._fetch_json_for_verification", fake_fetch_json):
                result = promote_candidate(observations[0], registry_path, write=True)

        self.assertEqual(result["company_name"], "Newco")
        self.assertEqual(result["connector_type"], "greenhouse")

    def test_promoting_a_company_with_no_suggestion_adds_a_bare_pending_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CompanyDiscoveryStore(tmp)
            observations = observe_companies_from_jobs([_job("Newco", "prospects")], REGISTRY, store)
            registry_path = Path(tmp) / "registry.json"
            registry_path.write_text("[]", encoding="utf-8")

            result = promote_candidate(observations[0], registry_path, write=True)

        self.assertEqual(result["company_name"], "Newco")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["verification_status"], "pending_ats_discovery")
        self.assertIn("prospects", result["notes"])

    def test_promoting_an_already_registered_company_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CompanyDiscoveryStore(tmp)
            observations = observe_companies_from_jobs([_job("Newco", "prospects")], REGISTRY, store)
            registry_path = Path(tmp) / "registry.json"
            registry_path.write_text('[{"company_name": "Newco"}]', encoding="utf-8")

            with self.assertRaises(ValueError):
                promote_candidate(observations[0], registry_path, write=True)


if __name__ == "__main__":
    unittest.main()
