from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jobintel.source_coverage import (
    ALWAYS_MANUAL,
    MANUAL_FALLBACK_ONLY,
    OPTIONAL_CROSS_CHECK,
    PARTIALLY_MANUAL,
    PARTIAL_COVERAGE,
    PRODUCTION_READY,
    STATIC_PROFILES_BY_NAME,
    TEMPORARILY_MANUAL,
    active_registry_connector_types,
    all_profiles,
    coverage_profiles,
    coverage_today,
    manual_check_category,
)
from jobintel.storage.local_store import LocalJobStore

ALL_CURATED_SOURCES = {
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "smartrecruiters",
    "workday",
    "welcome_to_the_jungle",
    "adzuna",
    "prospects",
    "trackr",
    "gradcracker",
    "bright_network",
}


class SourceCoverageStaticProfileTests(unittest.TestCase):
    def test_all_twelve_curated_sources_have_a_profile(self) -> None:
        self.assertEqual(set(STATIC_PROFILES_BY_NAME), ALL_CURATED_SOURCES)

    def test_every_source_supports_manual_import_after_phase_2_6(self) -> None:
        # The generic manual-job-import path (Section E) closed this gap for
        # every source, including the previously-automation-only ATS ones.
        for profile in STATIC_PROFILES_BY_NAME.values():
            self.assertTrue(profile.manual_import_supported, profile.source_name)

    def test_manual_fallback_only_sources_are_gradcracker_bright_network_trackr(self) -> None:
        manual_only = {name for name, profile in STATIC_PROFILES_BY_NAME.items() if profile.automation_status == MANUAL_FALLBACK_ONLY}
        self.assertEqual(manual_only, {"gradcracker", "bright_network", "trackr"})

    def test_every_profile_has_a_non_empty_manual_check_reason_when_required(self) -> None:
        for profile in STATIC_PROFILES_BY_NAME.values():
            if profile.manual_check_required:
                self.assertTrue(profile.manual_check_reason.strip(), profile.source_name)


class CoverageProfilesDynamicMergeTests(unittest.TestCase):
    def test_last_successful_automatic_check_reflects_actual_source_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_source_health("greenhouse", {"status": "live_api", "last_synced": "2026-09-15T10:00:00+00:00"})
            profiles = {profile.source_name: profile for profile in coverage_profiles(store)}

        self.assertEqual(profiles["greenhouse"].last_successful_automatic_check.isoformat(), "2026-09-15T10:00:00+00:00")
        # A source never refreshed in this store has no fabricated timestamp.
        self.assertIsNone(profiles["lever"].last_successful_automatic_check)


class ManualCheckCategoryDerivationTests(unittest.TestCase):
    def test_manual_fallback_only_source_is_always_manual_regardless_of_health(self) -> None:
        profile = STATIC_PROFILES_BY_NAME["gradcracker"]
        self.assertEqual(manual_check_category(profile, None), ALWAYS_MANUAL)
        self.assertEqual(manual_check_category(profile, {"status": "live_api"}), ALWAYS_MANUAL)

    def test_production_ready_source_with_a_failed_refresh_today_is_temporarily_manual(self) -> None:
        profile = STATIC_PROFILES_BY_NAME["greenhouse"]
        self.assertEqual(manual_check_category(profile, {"status": "error"}), TEMPORARILY_MANUAL)
        self.assertEqual(manual_check_category(profile, {"status": "partial"}), TEMPORARILY_MANUAL)

    def test_production_ready_source_healthy_today_is_optional_cross_check(self) -> None:
        profile = STATIC_PROFILES_BY_NAME["greenhouse"]
        self.assertEqual(manual_check_category(profile, {"status": "live_api"}), OPTIONAL_CROSS_CHECK)
        self.assertEqual(manual_check_category(profile, None), OPTIONAL_CROSS_CHECK)

    def test_partial_coverage_source_healthy_today_is_still_partially_manual(self) -> None:
        profile = STATIC_PROFILES_BY_NAME["adzuna"]
        self.assertEqual(profile.automation_status, PARTIAL_COVERAGE)
        self.assertEqual(manual_check_category(profile, {"status": "live_api"}), PARTIALLY_MANUAL)

    def test_partial_coverage_source_failing_today_is_temporarily_manual_not_partially(self) -> None:
        # A live failure today is a stronger, more urgent signal than the
        # source's structural partial-scope limitation -- must not be masked.
        profile = STATIC_PROFILES_BY_NAME["prospects"]
        self.assertEqual(manual_check_category(profile, {"status": "error"}), TEMPORARILY_MANUAL)


class CoverageTodayReportTests(unittest.TestCase):
    def test_manual_fallback_only_sources_never_appear_in_checked_or_failed_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            report = coverage_today(store, registry_entries=[])

        checked_or_failed = {e.source_name for e in report.automatically_checked_today} | {e.source_name for e in report.failed_or_incomplete_today}
        self.assertNotIn("trackr", checked_or_failed)
        self.assertNotIn("gradcracker", checked_or_failed)
        self.assertNotIn("bright_network", checked_or_failed)
        manual_required = {e.source_name for e in report.manual_check_still_required}
        self.assertIn("trackr", manual_required)
        self.assertIn("gradcracker", manual_required)

    def test_source_refreshed_successfully_today_is_reported_as_checked_today(self) -> None:
        now = datetime(2026, 9, 17, 15, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_source_health("greenhouse", {"status": "live_api", "checked_at": "2026-09-17T09:00:00+00:00", "jobs_seen": 12, "jobs_active": 12})
            report = coverage_today(store, now=now, registry_entries=[])

        checked = {e.source_name for e in report.automatically_checked_today}
        self.assertIn("greenhouse", checked)
        failed = {e.source_name for e in report.failed_or_incomplete_today}
        self.assertNotIn("greenhouse", failed)

    def test_source_that_failed_today_is_reported_as_failed_not_silently_missing(self) -> None:
        now = datetime(2026, 9, 17, 15, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_source_health("lever", {"status": "error", "checked_at": "2026-09-17T09:00:00+00:00", "last_error": "network down"})
            report = coverage_today(store, now=now, registry_entries=[])

        failed_entries = {e.source_name: e.detail for e in report.failed_or_incomplete_today}
        self.assertIn("lever", failed_entries)
        self.assertIn("network down", failed_entries["lever"])

    def test_source_never_refreshed_is_reported_as_incomplete_not_silently_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            report = coverage_today(store, registry_entries=[])

        failed_names = {e.source_name for e in report.failed_or_incomplete_today}
        # ashby was never given a health record in this fresh store -- must
        # still show up as "not automatically checked today", not vanish.
        self.assertIn("ashby", failed_names)

    def test_source_refreshed_successfully_on_a_previous_day_is_not_todays_check(self) -> None:
        now = datetime(2026, 9, 17, 15, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalJobStore(Path(tmp) / "store")
            store.write_source_health("workable", {"status": "live_api", "checked_at": "2026-09-15T09:00:00+00:00", "jobs_seen": 5, "jobs_active": 5})
            report = coverage_today(store, now=now, registry_entries=[])

        checked = {e.source_name for e in report.automatically_checked_today}
        self.assertNotIn("workable", checked)
        failed = {e.source_name for e in report.failed_or_incomplete_today}
        self.assertIn("workable", failed)


def _entry(company_name: str, connector_type: str, enabled: bool = True, manually_excluded: bool = False) -> dict:
    return {
        "company_name": company_name,
        "connector_type": connector_type,
        "connector_token": "token",
        "enabled": enabled,
        "manually_excluded": manually_excluded,
        "verification_status": "verified" if enabled else "pending_ats_discovery",
    }


class ActiveRegistryConnectorTypesTests(unittest.TestCase):
    def test_workday_appears_automatically_once_any_company_is_enabled(self) -> None:
        types = active_registry_connector_types([_entry("Darktrace", "workday")])
        self.assertIn("workday", types)

    def test_disabled_entries_do_not_count_as_active(self) -> None:
        types = active_registry_connector_types([_entry("Some Co", "greenhouse", enabled=False)])
        self.assertEqual(types, set())

    def test_manually_excluded_entries_do_not_count_as_active(self) -> None:
        # Juniper Networks UK's workday match specifically -- enabled would
        # normally be forced False for an excluded entry anyway, but this
        # guards against ever trusting a stale enabled=True on an excluded
        # entry to still light up the source as "active".
        types = active_registry_connector_types([_entry("Juniper Networks UK", "workday", enabled=True, manually_excluded=True)])
        self.assertEqual(types, set())

    def test_a_brand_new_never_seen_connector_type_is_detected_too(self) -> None:
        # Simulates a future ATS family this module has no curated knowledge
        # of yet -- must still be detected dynamically, not silently ignored.
        types = active_registry_connector_types([_entry("Future Co", "some_future_ats_nobody_has_heard_of")])
        self.assertIn("some_future_ats_nobody_has_heard_of", types)


class AllProfilesDynamicDiscoveryTests(unittest.TestCase):
    def test_a_brand_new_connector_family_gets_a_generic_profile_automatically(self) -> None:
        profiles = {p.source_name: p for p in all_profiles(registry_entries=[_entry("Future Co", "some_future_ats")])}

        self.assertIn("some_future_ats", profiles)
        self.assertEqual(profiles["some_future_ats"].automation_status, PRODUCTION_READY)
        self.assertTrue(profiles["some_future_ats"].manual_check_required)
        # No dashboard/source-list code edit was needed for this to appear --
        # confirmed by the fact this test never touches STATIC_PROFILES_BY_NAME.
        self.assertNotIn("some_future_ats", STATIC_PROFILES_BY_NAME)

    def test_curated_profile_is_used_instead_of_the_generic_fallback_when_both_exist(self) -> None:
        profiles = {p.source_name: p for p in all_profiles(registry_entries=[_entry("Darktrace", "workday")])}

        self.assertEqual(profiles["workday"].display_name, "Workday")
        self.assertIn("CXS", profiles["workday"].coverage_scope)

    def test_explicitly_tracked_manual_sources_remain_present_with_zero_active_connectors(self) -> None:
        # Trackr/Gradcracker/Bright Network are never registry connector
        # types at all -- they must still always appear.
        profiles = {p.source_name for p in all_profiles(registry_entries=[])}

        self.assertIn("trackr", profiles)
        self.assertIn("gradcracker", profiles)
        self.assertIn("bright_network", profiles)
        self.assertIn("prospects", profiles)
        self.assertIn("adzuna", profiles)

    def test_curated_sources_remain_present_even_with_no_currently_enabled_companies(self) -> None:
        # A named source must not vanish from the coverage view just because
        # the registry temporarily has zero enabled companies of that type.
        profiles = {p.source_name for p in all_profiles(registry_entries=[])}

        self.assertIn("greenhouse", profiles)
        self.assertIn("workday", profiles)

    def test_missing_registry_file_does_not_crash(self) -> None:
        profiles = all_profiles(registry_path="/nonexistent/path/companies.json")
        self.assertTrue(any(p.source_name == "greenhouse" for p in profiles))


if __name__ == "__main__":
    unittest.main()
