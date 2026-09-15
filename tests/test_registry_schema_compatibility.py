from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobintel.company_registry import connectors_from_registry, load_company_registry, registry_summary


class RegistrySchemaCompatibilityTests(unittest.TestCase):
    def test_load_current_target_companies_json(self) -> None:
        companies = load_company_registry("config/target_companies.json")

        self.assertGreaterEqual(len(companies), 1)
        self.assertTrue(any(company.company_name == "Monzo" for company in companies))

    def test_load_entry_containing_ats_errors_and_future_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "company_name": "Acme",
                            "industry": "SaaS",
                            "priority": 4,
                            "greenhouse_board_token": "acme",
                            "lever_site_token": None,
                            "careers_url": "https://acme.example/careers",
                            "source_connector_type": "greenhouse",
                            "connector_type": "greenhouse",
                            "connector_token": "acme",
                            "verification_status": "pending_network_validation",
                            "verification_url": "https://job-boards.greenhouse.io/acme",
                            "enabled": False,
                            "notes": "preserve me",
                            "ats_discovery_error": "URLError",
                            "ats_verification_error": "URLError",
                            "future_ats_metadata": {"status_code": 503},
                        }
                    ]
                ),
                encoding="utf-8",
            )

            companies = load_company_registry(path)

        self.assertEqual(companies[0].ats_discovery_error, "URLError")
        self.assertEqual(companies[0].ats_verification_error, "URLError")
        self.assertEqual(companies[0].extra_metadata["future_ats_metadata"], {"status_code": 503})
        self.assertEqual(companies[0].notes, "preserve me")

    def test_load_older_entry_without_ats_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "company_name": "OldCo",
                            "industry": "FinTech",
                            "priority": 5,
                            "greenhouse_board_token": None,
                            "lever_site_token": None,
                            "careers_url": "https://oldco.example/careers",
                            "source_connector_type": "pending_verification",
                            "enabled": False,
                            "notes": "old schema",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            companies = load_company_registry(path)

        self.assertEqual(companies[0].verification_status, "pending_verification")
        self.assertIsNone(companies[0].connector_type)
        self.assertEqual(companies[0].notes, "old schema")

    def test_registry_summary_and_ingest_connector_loading_share_schema(self) -> None:
        companies = load_company_registry("config/target_companies.json")

        summary = registry_summary(companies)
        connectors = connectors_from_registry(companies)

        self.assertEqual(summary["companies_configured"], len(companies))
        self.assertIsInstance(connectors, list)

    def test_registry_supports_new_public_source_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "company_name": "AshbyCo",
                            "industry": "SaaS",
                            "priority": 4,
                            "source_connector_type": "ashby",
                            "connector_type": "ashby",
                            "connector_token": "ashbyco",
                            "verification_status": "verified",
                            "enabled": True,
                            "notes": "keep",
                        },
                        {
                            "company_name": "WorkableCo",
                            "industry": "SaaS",
                            "priority": 4,
                            "workable_account": "workableco",
                            "verification_status": "verified",
                            "enabled": True,
                        },
                        {
                            "company_name": "SmartCo",
                            "industry": "SaaS",
                            "priority": 4,
                            "smartrecruiters_company_identifier": "SmartCo",
                            "verification_status": "verified",
                            "enabled": True,
                        },
                    ]
                ),
                encoding="utf-8",
            )

            companies = load_company_registry(path)
            summary = registry_summary(companies)
            connectors = connectors_from_registry(companies)

        self.assertEqual(summary["ashby_connections"], 1)
        self.assertEqual(summary["workable_connections"], 1)
        self.assertEqual(summary["smartrecruiters_connections"], 1)
        self.assertEqual({connector.source_name for connector in connectors}, {"ashby", "workable", "smartrecruiters"})


if __name__ == "__main__":
    unittest.main()
