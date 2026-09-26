from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobintel.ats_discovery import (
    discover_company_entry,
    discover_registry,
    exclude_company,
    exclude_company_entry,
    reinclude_company,
    reinclude_company_entry,
    verify_company_entry,
    verify_registry,
)
from jobintel.company_registry import TargetCompany, connectors_from_registry

# Regression coverage for the registry-state bug identified in the Phase 2.7
# report: a manually disabled/excluded company (Juniper Networks UK -- its
# careers page resolves to HPE's shared, unscoped Workday tenant
# post-acquisition) had no persistent, protected exclusion state, so a future
# discover_registry/verify_registry re-run would silently re-find the same
# Workday candidate and re-enable it. These tests exercise the fix directly.


def _entry(**overrides) -> dict:
    base = {
        "company_name": "Juniper Networks UK",
        "industry": "networking/infrastructure",
        "priority": 4,
        "careers_url": "https://jobs.juniper.net/",
        "source_connector_type": "pending_verification",
        "enabled": False,
        "notes": "",
        "verification_status": "unsupported_scope",
        "connector_type": "workday",
        "connector_token": "hpe.wd5/Jobsathpe",
    }
    base.update(overrides)
    return base


HPE_CAREERS_HTML = '<a href="https://hpe.wd5.myworkdayjobs.com/Jobsathpe">Careers</a>'


class ExcludeCompanyEntryTests(unittest.TestCase):
    def test_exclude_sets_persistent_fields_with_provenance(self) -> None:
        result = exclude_company_entry(_entry(manually_excluded=False), "Resolves to HPE's global job board.")

        self.assertTrue(result["manually_excluded"])
        self.assertEqual(result["exclusion_reason"], "Resolves to HPE's global job board.")
        self.assertIsNotNone(result["excluded_at"])
        self.assertFalse(result["enabled"])

    def test_exclude_requires_a_non_empty_reason(self) -> None:
        with self.assertRaises(ValueError):
            exclude_company_entry(_entry(), "")
        with self.assertRaises(ValueError):
            exclude_company_entry(_entry(), "   ")

    def test_exclude_forces_enabled_false_immediately(self) -> None:
        result = exclude_company_entry(_entry(enabled=True, verification_status="verified"), "Company asked to be delisted.")

        self.assertFalse(result["enabled"])
        self.assertTrue(result["manually_excluded"])


class ReincludeCompanyEntryTests(unittest.TestCase):
    def test_reinclude_clears_exclusion_and_resets_for_fresh_discovery(self) -> None:
        excluded = exclude_company_entry(_entry(), "temporary test reason")
        reincluded = reinclude_company_entry(excluded)

        self.assertFalse(reincluded["manually_excluded"])
        self.assertIsNone(reincluded["exclusion_reason"])
        self.assertIsNone(reincluded["excluded_at"])
        self.assertEqual(reincluded["verification_status"], "pending_ats_discovery")
        self.assertIsNone(reincluded["connector_type"])
        self.assertIsNone(reincluded["connector_token"])


class DiscoveryRespectsExclusionTests(unittest.TestCase):
    def test_discover_company_entry_leaves_an_excluded_entry_completely_untouched(self) -> None:
        excluded = exclude_company_entry(_entry(), "Resolves to HPE's global job board.")

        # Even though the careers_url would (if fetched) reveal the exact same
        # Workday candidate again, discovery must not touch this entry at all.
        result = discover_company_entry(excluded, fetch_text=lambda url: (_ for _ in ()).throw(AssertionError("must not fetch an excluded company")))

        self.assertEqual(result, excluded)

    def test_verify_company_entry_leaves_an_excluded_entry_completely_untouched(self) -> None:
        excluded = exclude_company_entry(_entry(), "Resolves to HPE's global job board.")

        result = verify_company_entry(excluded, fetch_json_func=lambda url: (_ for _ in ()).throw(AssertionError("must not verify an excluded company")))

        self.assertEqual(result, excluded)

    def test_full_discover_registry_cycle_preserves_exclusion_even_when_the_candidate_is_still_findable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            excluded = exclude_company_entry(_entry(), "Resolves to HPE's global job board.")
            path.write_text(json.dumps([excluded]), encoding="utf-8")

            # The real careers page would still resolve to the same Workday
            # tenant if fetched -- prove the guard is what's protecting this,
            # not merely that the fetch happens not to find anything.
            updated = discover_registry(path, write=True, fetch_text=lambda url: HPE_CAREERS_HTML)

        self.assertTrue(updated[0]["manually_excluded"])
        self.assertFalse(updated[0]["enabled"])
        self.assertEqual(updated[0]["verification_status"], "unsupported_scope")

    def test_full_verify_registry_cycle_never_re_enables_an_excluded_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            excluded = exclude_company_entry(_entry(), "Resolves to HPE's global job board.")
            path.write_text(json.dumps([excluded]), encoding="utf-8")

            # A live-looking successful Workday response -- if the guard were
            # missing, this would re-verify and re-enable the company.
            updated = verify_registry(path, write=True, fetch_json_func=lambda url: {"jobPostings": [{"externalPath": "/job/x/Title_1"}] * 50})

        self.assertTrue(updated[0]["manually_excluded"])
        self.assertFalse(updated[0]["enabled"])
        self.assertNotEqual(updated[0]["verification_status"], "verified")

    def test_repeated_discovery_and_verify_cycles_preserve_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            excluded = exclude_company_entry(_entry(), "Resolves to HPE's global job board.")
            path.write_text(json.dumps([excluded]), encoding="utf-8")

            for _ in range(3):
                discover_registry(path, write=True, fetch_text=lambda url: HPE_CAREERS_HTML)
                verify_registry(path, write=True, fetch_json_func=lambda url: {"jobPostings": [{"externalPath": "/job/x/Title_1"}]})

            final = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(final[0]["manually_excluded"])
        self.assertFalse(final[0]["enabled"])


class ExplicitReenableRequiresHumanActionTests(unittest.TestCase):
    def test_reinclude_company_is_the_only_way_to_undo_an_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            excluded = exclude_company_entry(_entry(), "Resolves to HPE's global job board.")
            path.write_text(json.dumps([excluded]), encoding="utf-8")

            # Automatic cycles alone never undo it.
            discover_registry(path, write=True, fetch_text=lambda url: HPE_CAREERS_HTML)
            verify_registry(path, write=True, fetch_json_func=lambda url: {"jobPostings": []})
            still_excluded = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(still_excluded[0]["manually_excluded"])

            # Only an explicit reinclude_company call undoes it.
            reinclude_company("Juniper Networks UK", path, write=True)
            after_reinclude = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(after_reinclude[0]["manually_excluded"])
        self.assertIsNone(after_reinclude[0]["exclusion_reason"])
        self.assertEqual(after_reinclude[0]["verification_status"], "pending_ats_discovery")

    def test_after_reinclude_normal_discovery_can_find_the_company_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            excluded = exclude_company_entry(_entry(), "temporary test reason")
            path.write_text(json.dumps([excluded]), encoding="utf-8")

            reinclude_company("Juniper Networks UK", path, write=True)
            updated = discover_registry(path, write=True, fetch_text=lambda url: HPE_CAREERS_HTML)

        # Now that it's no longer excluded, discovery is free to find the
        # candidate again -- this is expected and correct once a human has
        # explicitly decided to reconsider the company.
        self.assertEqual(updated[0]["connector_type"], "workday")
        self.assertFalse(updated[0]["manually_excluded"])

    def test_exclude_company_and_reinclude_company_raise_for_an_unknown_company_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(json.dumps([_entry()]), encoding="utf-8")

            with self.assertRaises(ValueError):
                exclude_company("Totally Unknown Co", "reason", path, write=True)
            with self.assertRaises(ValueError):
                reinclude_company("Totally Unknown Co", path, write=True)


class DoesNotInferOtherExclusionsTests(unittest.TestCase):
    def test_excluding_one_company_does_not_affect_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            other = _entry(company_name="Some Other Co", careers_url="https://other.example/careers", connector_type=None, connector_token=None, verification_status="pending_ats_discovery")
            path.write_text(json.dumps([_entry(), other]), encoding="utf-8")

            exclude_company("Juniper Networks UK", "Resolves to HPE's global job board.", path, write=True)
            entries = json.loads(path.read_text(encoding="utf-8"))

        other_after = next(e for e in entries if e["company_name"] == "Some Other Co")
        self.assertFalse(other_after.get("manually_excluded", False))
        self.assertIsNone(other_after.get("exclusion_reason"))


class ConnectorsFromRegistrySkipsExcludedCompaniesTests(unittest.TestCase):
    def test_manually_excluded_company_is_never_included_even_if_enabled_is_true(self) -> None:
        # Defense in depth: even if `enabled` were somehow left True on an
        # excluded entry, the live connector set must not include it.
        company = TargetCompany(
            company_name="Juniper Networks UK",
            industry="networking",
            priority=4,
            enabled=True,
            connector_type="workday",
            connector_token="hpe.wd5/Jobsathpe",
            verification_status="verified",
            manually_excluded=True,
            exclusion_reason="Resolves to HPE's global job board.",
        )

        connectors = connectors_from_registry([company])

        self.assertEqual(connectors, [])


class DistinguishableFromOtherManualStatesTests(unittest.TestCase):
    def test_exclusion_fields_are_absent_for_a_temporary_fetch_failure(self) -> None:
        entry = _entry(company_name="Some Co", manually_excluded=False, verification_status="pending_network_validation")
        self.assertFalse(entry.get("manually_excluded", False))

    def test_exclusion_fields_are_absent_for_an_unsupported_ats_entry(self) -> None:
        entry = _entry(company_name="Some Co", manually_excluded=False, verification_status="unsupported_provider")
        self.assertFalse(entry.get("manually_excluded", False))

    def test_excluded_entry_keeps_its_original_verification_status_for_audit(self) -> None:
        # Manual exclusion must be readable as a DISTINCT dimension, not a
        # relabeling of verification_status -- an excluded entry's own
        # verification_status (what discovery/verification found before the
        # human decision) stays intact for audit purposes.
        result = exclude_company_entry(_entry(verification_status="verified"), "reason")
        self.assertEqual(result["verification_status"], "verified")
        self.assertTrue(result["manually_excluded"])


if __name__ == "__main__":
    unittest.main()
