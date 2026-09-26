from __future__ import annotations

import unittest

from jobintel.company_coverage import (
    AUTO_VERIFIED,
    MANUAL_REQUIRED,
    PARTIAL_AUTOMATION,
    TEMPORARILY_FAILED,
    UNKNOWN,
    classify_company,
    classify_registry,
    coverage_metrics,
    manual_watchlist,
)


def _entry(**overrides) -> dict:
    base = {
        "company_name": "Example Co",
        "careers_url": "https://example.com/careers",
        "verification_status": "pending_ats_discovery",
        "enabled": False,
        "connector_type": None,
        "jobs_available": None,
    }
    base.update(overrides)
    return base


class ClassifyCompanyTests(unittest.TestCase):
    def test_verified_and_enabled_is_auto_verified(self) -> None:
        result = classify_company(_entry(verification_status="verified", enabled=True, connector_type="greenhouse", jobs_available=42))

        self.assertEqual(result.state, AUTO_VERIFIED)
        self.assertFalse(result.manual_check_required)
        self.assertIn("42", result.reason)

    def test_verified_but_not_enabled_is_not_auto_verified(self) -> None:
        # A connector class existing / a candidate being found is not
        # sufficient -- Section F explicitly forbids treating "provider
        # identified" as AUTO_VERIFIED.
        result = classify_company(_entry(verification_status="verified", enabled=False))

        self.assertNotEqual(result.state, AUTO_VERIFIED)

    def test_pending_ats_discovery_is_manual_required(self) -> None:
        result = classify_company(_entry(verification_status="pending_ats_discovery"))

        self.assertEqual(result.state, MANUAL_REQUIRED)
        self.assertTrue(result.manual_check_required)

    def test_pending_network_validation_for_a_never_before_observed_company_is_temporarily_failed(self) -> None:
        result = classify_company(_entry(company_name="Some New Co", verification_status="pending_network_validation"))

        self.assertEqual(result.state, TEMPORARILY_FAILED)

    def test_confirmed_blocked_company_is_manual_required_even_with_pending_network_status(self) -> None:
        # Bloomberg showed a confirmed named bot-protection challenge page,
        # not just a bare HTTP error -- stronger evidence than a single failed
        # fetch, so it must not be softened to TEMPORARILY_FAILED.
        result = classify_company(_entry(company_name="Bloomberg", verification_status="pending_network_validation"))

        self.assertEqual(result.state, MANUAL_REQUIRED)
        self.assertIn("robot", result.reason.lower())

    def test_observed_flaky_company_is_temporarily_failed_with_flakiness_reason(self) -> None:
        result = classify_company(_entry(company_name="Revolut", verification_status="pending_network_validation"))

        self.assertEqual(result.state, TEMPORARILY_FAILED)
        self.assertIn("succeed on one run", result.reason)

    def test_deliberately_excluded_workday_match_is_manual_required_not_auto_verified(self) -> None:
        # Juniper Networks UK: a real Workday candidate WAS found (HPE's
        # tenant, post-acquisition) but deliberately not enabled via the
        # persistent manual-exclusion field -- must never report as
        # AUTO_VERIFIED just because a candidate/provider exists, even if
        # verification_status/connector_type still describe a working match.
        result = classify_company(
            _entry(
                company_name="Juniper Networks UK",
                verification_status="unsupported_scope",
                connector_type="workday",
                manually_excluded=True,
                exclusion_reason="Resolves to HPE's shared Workday tenant post-acquisition.",
                excluded_at="2026-09-17T16:36:46.372324+00:00",
            )
        )

        self.assertEqual(result.state, MANUAL_REQUIRED)
        self.assertIn("HPE", result.reason)
        self.assertTrue(result.manually_excluded)
        self.assertEqual(result.exclusion_reason, "Resolves to HPE's shared Workday tenant post-acquisition.")

    def test_manual_exclusion_overrides_even_a_verified_enabled_entry(self) -> None:
        # The strongest possible test of "overrides everything else": even an
        # entry that LOOKS fully AUTO_VERIFIED must not report as such once
        # manually_excluded is set.
        result = classify_company(
            _entry(
                verification_status="verified",
                enabled=True,
                connector_type="greenhouse",
                jobs_available=100,
                manually_excluded=True,
                exclusion_reason="Company asked to be removed from consideration.",
            )
        )

        self.assertEqual(result.state, MANUAL_REQUIRED)
        self.assertTrue(result.manually_excluded)

    def test_manual_exclusion_is_distinguishable_from_other_manual_required_reasons(self) -> None:
        excluded = classify_company(_entry(manually_excluded=True, exclusion_reason="Human decision."))
        unsupported_ats = classify_company(_entry(verification_status="pending_ats_discovery"))
        temporarily_failed = classify_company(_entry(company_name="Some New Co", verification_status="pending_network_validation"))

        self.assertTrue(excluded.manually_excluded)
        self.assertFalse(unsupported_ats.manually_excluded)
        self.assertFalse(temporarily_failed.manually_excluded)
        self.assertNotEqual(excluded.reason, unsupported_ats.reason)
        self.assertIn("MANUALLY EXCLUDED", excluded.reason)
        self.assertNotIn("MANUALLY EXCLUDED", unsupported_ats.reason)
        self.assertNotIn("MANUALLY EXCLUDED", temporarily_failed.reason)

    def test_unsupported_provider_is_manual_required(self) -> None:
        result = classify_company(_entry(verification_status="unsupported_provider", notes="Migrated to Workday, unsupported."))

        self.assertEqual(result.state, MANUAL_REQUIRED)

    def test_unmapped_status_is_unknown_not_silently_dropped(self) -> None:
        result = classify_company(_entry(verification_status="some_future_status_this_code_has_never_seen"))

        self.assertEqual(result.state, UNKNOWN)
        self.assertTrue(result.manual_check_required)


class CoverageMetricsTests(unittest.TestCase):
    def test_metrics_sum_to_total_and_percentages_are_registry_scoped(self) -> None:
        entries = [
            _entry(company_name="A", verification_status="verified", enabled=True, connector_type="greenhouse", jobs_available=10),
            _entry(company_name="B", verification_status="verified", enabled=True, connector_type="lever", jobs_available=5),
            _entry(company_name="C", verification_status="pending_ats_discovery"),
            _entry(company_name="D", verification_status="pending_network_validation"),
        ]
        results = classify_registry(entries)
        metrics = coverage_metrics(results)

        self.assertEqual(metrics["total_companies"], 4)
        self.assertEqual(sum(metrics["counts"].values()), 4)
        self.assertEqual(metrics["counts"][AUTO_VERIFIED], 2)
        self.assertAlmostEqual(metrics["automatic_verified_coverage_of_registry"], 0.5)
        self.assertAlmostEqual(metrics["automatic_or_partial_coverage_of_registry"], 0.5)
        self.assertEqual(metrics["counts"][PARTIAL_AUTOMATION], 0)


class ManualWatchlistTests(unittest.TestCase):
    def test_watchlist_excludes_auto_verified_companies(self) -> None:
        entries = [
            _entry(company_name="Verified Co", verification_status="verified", enabled=True, connector_type="greenhouse"),
            _entry(company_name="Unresolved Co", verification_status="pending_ats_discovery"),
        ]
        results = classify_registry(entries)
        watchlist = manual_watchlist(results)

        names = {row["company"] for row in watchlist}
        self.assertNotIn("Verified Co", names)
        self.assertIn("Unresolved Co", names)

    def test_watchlist_rows_are_dashboard_ready_data_not_prose(self) -> None:
        entries = [_entry(company_name="Unresolved Co", verification_status="pending_ats_discovery")]
        watchlist = manual_watchlist(classify_registry(entries))

        row = watchlist[0]
        self.assertEqual(set(row), {"company", "careers_url", "reason", "state", "provider", "recommended_manual_check_frequency"})


if __name__ == "__main__":
    unittest.main()
