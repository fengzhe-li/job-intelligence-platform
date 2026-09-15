from __future__ import annotations

import json
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

from jobintel.ats_discovery import (
    ATSCandidate,
    add_manual_ats_url,
    ats_summary,
    check_ats_url,
    detect_ats_candidates,
    discover_registry,
    import_manual_ats_urls,
    read_manual_ats_records,
    unresolved_entries,
    verify_candidate,
    verify_registry,
)


class ATSDiscoveryTests(unittest.TestCase):
    def test_greenhouse_url_detection_and_token_extraction(self) -> None:
        html = '<a href="https://boards.greenhouse.io/example/jobs/123">Jobs</a>'

        candidates = detect_ats_candidates(html, "https://example.com/careers")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].connector_type, "greenhouse")
        self.assertEqual(candidates[0].connector_token, "example")
        self.assertEqual(candidates[0].verification_url, "https://boards.greenhouse.io/example/jobs/123")

    def test_greenhouse_api_url_detection(self) -> None:
        html = 'https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true'

        candidates = detect_ats_candidates(html, "https://acme.example/jobs")

        self.assertEqual(candidates[0].connector_type, "greenhouse")
        self.assertEqual(candidates[0].connector_token, "acme")

    def test_lever_url_detection_and_token_extraction(self) -> None:
        html = '<script>window.jobsUrl = "https://jobs.lever.co/example?team=Engineering";</script>'

        candidates = detect_ats_candidates(html, "https://example.com/careers")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].connector_type, "lever")
        self.assertEqual(candidates[0].connector_token, "example")

    def test_ashby_url_detection_and_token_extraction(self) -> None:
        html = '<a href="https://jobs.ashbyhq.com/example">Jobs</a>'

        candidates = detect_ats_candidates(html, "https://example.com/careers")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].connector_type, "ashby")
        self.assertEqual(candidates[0].connector_token, "example")

    def test_workable_url_detection_and_token_extraction(self) -> None:
        html = '<a href="https://apply.workable.com/example/">Jobs</a>'

        candidates = detect_ats_candidates(html, "https://example.com/careers")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].connector_type, "workable")
        self.assertEqual(candidates[0].connector_token, "example")

    def test_smartrecruiters_url_detection_and_token_extraction(self) -> None:
        html = '<a href="https://careers.smartrecruiters.com/ExampleCompany">Jobs</a>'

        candidates = detect_ats_candidates(html, "https://example.com/careers")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].connector_type, "smartrecruiters")
        self.assertEqual(candidates[0].connector_token, "ExampleCompany")

    def test_invalid_token_handling(self) -> None:
        verification = verify_candidate(
            ATSCandidate("greenhouse", "bad", "https://boards.greenhouse.io/bad"),
            fetch_json_func=lambda _: {"not_jobs": []},
        )

        self.assertEqual(verification.status, "invalid_feed")
        self.assertEqual(verification.jobs_available, 0)
        self.assertEqual(verification.failure_kind, "unexpected_feed_shape")

    def test_network_unavailable_keeps_pending(self) -> None:
        verification = verify_candidate(
            ATSCandidate("lever", "example", "https://jobs.lever.co/example"),
            fetch_json_func=lambda _: (_ for _ in ()).throw(URLError("offline")),
        )

        self.assertEqual(verification.status, "pending_network_validation")
        self.assertEqual(verification.failure_kind, "url_error")
        self.assertEqual(verification.exception_type, "URLError")

    def test_valid_greenhouse_feed_uses_public_job_board_api_url(self) -> None:
        seen_urls = []

        verification = verify_candidate(
            ATSCandidate("greenhouse", "monzo", "https://job-boards.greenhouse.io/monzo"),
            fetch_json_func=lambda url: seen_urls.append(url) or {"jobs": [{"id": 1}, {"id": 2}]},
        )

        self.assertEqual(seen_urls, ["https://boards-api.greenhouse.io/v1/boards/monzo/jobs?content=true"])
        self.assertEqual(verification.status, "verified")
        self.assertEqual(verification.jobs_available, 2)
        self.assertEqual(verification.message, "valid_job_feed")
        self.assertEqual(verification.requested_url, seen_urls[0])

    def test_valid_lever_feed_uses_public_postings_api_url(self) -> None:
        seen_urls = []

        verification = verify_candidate(
            ATSCandidate("lever", "zopa", "https://jobs.lever.co/zopa"),
            fetch_json_func=lambda url: seen_urls.append(url) or [{"id": "job-1"}],
        )

        self.assertEqual(seen_urls, ["https://api.lever.co/v0/postings/zopa?mode=json"])
        self.assertEqual(verification.status, "verified")
        self.assertEqual(verification.jobs_available, 1)
        self.assertEqual(verification.message, "valid_job_feed")

    def test_http_404_marks_invalid_token_with_status_code(self) -> None:
        def raise_404(url: str):
            raise HTTPError(url, 404, "Not Found", None, None)

        verification = verify_candidate(
            ATSCandidate("greenhouse", "missing", "https://job-boards.greenhouse.io/missing"),
            fetch_json_func=raise_404,
        )

        self.assertEqual(verification.status, "invalid_token")
        self.assertEqual(verification.failure_kind, "invalid_board_or_site_token")
        self.assertEqual(verification.http_status_code, 404)
        self.assertEqual(verification.exception_type, "HTTPError")

    def test_dns_failure_is_diagnosed(self) -> None:
        verification = verify_candidate(
            ATSCandidate("lever", "example", "https://jobs.lever.co/example"),
            fetch_json_func=lambda _: (_ for _ in ()).throw(URLError(socket.gaierror("no address"))),
        )

        self.assertEqual(verification.status, "pending_network_validation")
        self.assertEqual(verification.failure_kind, "dns_error")
        self.assertEqual(verification.exception_type, "URLError")

    def test_ssl_failure_is_diagnosed(self) -> None:
        verification = verify_candidate(
            ATSCandidate("greenhouse", "example", "https://boards.greenhouse.io/example"),
            fetch_json_func=lambda _: (_ for _ in ()).throw(URLError(ssl.SSLError("certificate verify failed"))),
        )

        self.assertEqual(verification.status, "pending_network_validation")
        self.assertEqual(verification.failure_kind, "ssl_certificate_error")

    def test_timeout_is_diagnosed(self) -> None:
        verification = verify_candidate(
            ATSCandidate("lever", "example", "https://jobs.lever.co/example"),
            fetch_json_func=lambda _: (_ for _ in ()).throw(TimeoutError("timed out")),
        )

        self.assertEqual(verification.status, "pending_network_validation")
        self.assertEqual(verification.failure_kind, "timeout")
        self.assertEqual(verification.exception_type, "TimeoutError")

    def test_malformed_json_is_diagnosed_as_invalid_feed(self) -> None:
        verification = verify_candidate(
            ATSCandidate("greenhouse", "example", "https://boards.greenhouse.io/example"),
            fetch_json_func=lambda _: (_ for _ in ()).throw(json.JSONDecodeError("bad json", "x", 0)),
        )

        self.assertEqual(verification.status, "invalid_feed")
        self.assertEqual(verification.failure_kind, "malformed_json")
        self.assertEqual(verification.exception_type, "JSONDecodeError")

    def test_valid_empty_greenhouse_feed_is_verified_with_zero_jobs(self) -> None:
        verification = verify_candidate(
            ATSCandidate("greenhouse", "empty", "https://job-boards.greenhouse.io/empty"),
            fetch_json_func=lambda _: {"jobs": []},
        )

        self.assertEqual(verification.status, "verified")
        self.assertEqual(verification.jobs_available, 0)
        self.assertEqual(verification.message, "valid_empty_job_feed")

    def test_valid_empty_lever_feed_is_verified_with_zero_jobs(self) -> None:
        verification = verify_candidate(
            ATSCandidate("lever", "empty", "https://jobs.lever.co/empty"),
            fetch_json_func=lambda _: [],
        )

        self.assertEqual(verification.status, "verified")
        self.assertEqual(verification.jobs_available, 0)
        self.assertEqual(verification.message, "valid_empty_job_feed")

    def test_valid_ashby_feed_uses_public_posting_api_url(self) -> None:
        seen_urls = []

        verification = verify_candidate(
            ATSCandidate("ashby", "example", "https://jobs.ashbyhq.com/example"),
            fetch_json_func=lambda url: seen_urls.append(url) or {"jobs": [{"id": "job-1"}]},
        )

        self.assertEqual(seen_urls, ["https://api.ashbyhq.com/posting-api/job-board/example?includeCompensation=true"])
        self.assertEqual(verification.status, "verified")
        self.assertEqual(verification.jobs_available, 1)

    def test_valid_workable_feed_uses_public_account_api_url(self) -> None:
        seen_urls = []

        verification = verify_candidate(
            ATSCandidate("workable", "example", "https://apply.workable.com/example"),
            fetch_json_func=lambda url: seen_urls.append(url) or {"jobs": [{"id": "job-1"}]},
        )

        self.assertEqual(seen_urls, ["https://www.workable.com/api/accounts/example?details=true"])
        self.assertEqual(verification.status, "verified")
        self.assertEqual(verification.jobs_available, 1)

    def test_valid_smartrecruiters_feed_uses_public_postings_api_url(self) -> None:
        seen_urls = []

        verification = verify_candidate(
            ATSCandidate("smartrecruiters", "ExampleCompany", "https://careers.smartrecruiters.com/ExampleCompany"),
            fetch_json_func=lambda url: seen_urls.append(url) or {"content": [{"id": "job-1"}]},
        )

        self.assertEqual(seen_urls, ["https://api.smartrecruiters.com/v1/companies/ExampleCompany/postings?limit=100&offset=0&country=gb"])
        self.assertEqual(verification.status, "verified")
        self.assertEqual(verification.jobs_available, 1)

    def test_direct_ats_check_detects_url_and_verifies(self) -> None:
        verification = check_ats_url("https://job-boards.greenhouse.io/acme", fetch_json_func=lambda _: {"jobs": [{"id": 1}]})

        self.assertEqual(verification.candidate.connector_type, "greenhouse")
        self.assertEqual(verification.candidate.connector_token, "acme")
        self.assertEqual(verification.status, "verified")

    def test_registry_discovery_update_behavior_preserves_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(json.dumps([_company("Acme", "https://acme.example/careers")]), encoding="utf-8")

            entries = discover_registry(
                path,
                write=True,
                fetch_text=lambda _: '<a href="https://jobs.lever.co/acme">Jobs</a>',
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(entries[0]["connector_type"], "lever")
        self.assertEqual(entries[0]["connector_token"], "acme")
        self.assertEqual(entries[0]["verification_status"], "discovered_pending_verification")
        self.assertFalse(entries[0]["enabled"])
        self.assertEqual(persisted[0]["notes"], "keep this note")

    def test_discover_dry_run_does_not_write_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            original = [_company("Acme", "https://acme.example/careers")]
            path.write_text(json.dumps(original), encoding="utf-8")

            entries = discover_registry(
                path,
                write=False,
                fetch_text=lambda _: '<a href="https://boards.greenhouse.io/acme">Jobs</a>',
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(entries[0]["connector_type"], "greenhouse")
        self.assertNotIn("connector_type", persisted[0])
        self.assertEqual(persisted, original)

    def test_registry_verify_enables_only_valid_feed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            company = _company("Acme", "https://acme.example/careers")
            company.update({"connector_type": "greenhouse", "connector_token": "acme"})
            path.write_text(json.dumps([company]), encoding="utf-8")

            entries = verify_registry(path, write=True, fetch_json_func=lambda _: {"jobs": [{"id": 1}, {"id": 2}]})
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(entries[0]["enabled"])
        self.assertEqual(entries[0]["verification_status"], "verified")
        self.assertEqual(entries[0]["jobs_available"], 2)
        self.assertEqual(entries[0]["verification_requested_url"], "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true")
        self.assertEqual(entries[0]["verification_message"], "valid_job_feed")
        self.assertEqual(persisted[0]["notes"], "keep this note")

    def test_verify_dry_run_does_not_write_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            original = _company("Acme", "https://acme.example/careers")
            original.update({"connector_type": "lever", "connector_token": "acme"})
            path.write_text(json.dumps([original]), encoding="utf-8")

            entries = verify_registry(path, write=False, fetch_json_func=lambda _: [{"id": "job-1"}])
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(entries[0]["verification_status"], "verified")
        self.assertTrue(entries[0]["enabled"])
        self.assertEqual(persisted[0]["verification_status"], "pending_network_validation")
        self.assertFalse(persisted[0]["enabled"])

    def test_ats_summary_counts(self) -> None:
        entries = [
            {**_company("A", "https://a.example"), "connector_type": "greenhouse", "connector_token": "a", "verification_status": "verified", "enabled": True},
            {**_company("B", "https://b.example"), "connector_type": "lever", "connector_token": "b", "verification_status": "discovered_pending_verification", "enabled": False},
            {**_company("C", "https://c.example"), "connector_type": "ashby", "connector_token": "c", "verification_status": "verified", "enabled": True},
            {**_company("D", "https://d.example"), "connector_type": "workable", "connector_token": "d", "verification_status": "verified", "enabled": True},
            {**_company("E", "https://e.example"), "connector_type": "smartrecruiters", "connector_token": "e", "verification_status": "verified", "enabled": True},
        ]

        summary = ats_summary(entries)

        self.assertEqual(summary["companies_checked"], 5)
        self.assertEqual(summary["greenhouse_candidates_found"], 1)
        self.assertEqual(summary["lever_candidates_found"], 1)
        self.assertEqual(summary["ashby_candidates_found"], 1)
        self.assertEqual(summary["workable_candidates_found"], 1)
        self.assertEqual(summary["smartrecruiters_candidates_found"], 1)
        self.assertEqual(summary["successfully_verified_connectors"], 4)
        self.assertEqual(summary["pending_unresolved_companies"], 1)

    def test_manual_greenhouse_url_registration_verifies_and_enables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(json.dumps([_company("Acme", "https://acme.example/careers")]), encoding="utf-8")

            result = add_manual_ats_url(
                "Acme",
                "https://job-boards.greenhouse.io/acme",
                path,
                write=True,
                fetch_json_func=lambda _: {"jobs": [{"id": 1}]},
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(result["connector_type"], "greenhouse")
        self.assertEqual(result["connector_token"], "acme")
        self.assertEqual(result["greenhouse_board_token"], "acme")
        self.assertEqual(result["verification_status"], "verified")
        self.assertTrue(result["enabled"])
        self.assertEqual(result["jobs_available"], 1)
        self.assertEqual(persisted[0]["notes"], "keep this note")

    def test_manual_lever_url_registration_pending_when_network_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(json.dumps([_company("Acme", "https://acme.example/careers")]), encoding="utf-8")

            result = add_manual_ats_url(
                "Acme",
                "https://jobs.lever.co/acme",
                path,
                write=True,
                fetch_json_func=lambda _: (_ for _ in ()).throw(URLError("offline")),
            )

        self.assertEqual(result["connector_type"], "lever")
        self.assertEqual(result["connector_token"], "acme")
        self.assertEqual(result["verification_status"], "pending_network_validation")
        self.assertFalse(result["enabled"])

    def test_manual_ashby_url_registration_verifies_and_sets_source_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(json.dumps([_company("Acme", "https://acme.example/careers")]), encoding="utf-8")

            result = add_manual_ats_url(
                "Acme",
                "https://jobs.ashbyhq.com/acme",
                path,
                write=True,
                fetch_json_func=lambda _: {"jobs": [{"id": "job-1"}]},
            )

        self.assertEqual(result["connector_type"], "ashby")
        self.assertEqual(result["connector_token"], "acme")
        self.assertEqual(result["ashby_board_name"], "acme")
        self.assertEqual(result["verification_status"], "verified")
        self.assertTrue(result["enabled"])

    def test_manual_workable_url_registration_verifies_and_sets_source_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(json.dumps([_company("Acme", "https://acme.example/careers")]), encoding="utf-8")

            result = add_manual_ats_url(
                "Acme",
                "https://apply.workable.com/acme/",
                path,
                write=True,
                fetch_json_func=lambda _: {"jobs": [{"id": "job-1"}]},
            )

        self.assertEqual(result["connector_type"], "workable")
        self.assertEqual(result["connector_token"], "acme")
        self.assertEqual(result["workable_account"], "acme")
        self.assertEqual(result["verification_status"], "verified")
        self.assertTrue(result["enabled"])

    def test_manual_smartrecruiters_url_registration_verifies_and_sets_source_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(json.dumps([_company("Acme", "https://acme.example/careers")]), encoding="utf-8")

            result = add_manual_ats_url(
                "Acme",
                "https://careers.smartrecruiters.com/Acme",
                path,
                write=True,
                fetch_json_func=lambda _: {"content": [{"id": "job-1"}]},
            )

        self.assertEqual(result["connector_type"], "smartrecruiters")
        self.assertEqual(result["connector_token"], "Acme")
        self.assertEqual(result["smartrecruiters_company_identifier"], "Acme")
        self.assertEqual(result["verification_status"], "verified")
        self.assertTrue(result["enabled"])

    def test_manual_url_registration_rejects_unknown_ats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            path.write_text(json.dumps([_company("Acme", "https://acme.example/careers")]), encoding="utf-8")

            with self.assertRaises(ValueError):
                add_manual_ats_url("Acme", "https://example.com/jobs/acme", path, write=False)

    def test_manual_url_dry_run_does_not_write_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companies.json"
            original = [_company("Acme", "https://acme.example/careers")]
            path.write_text(json.dumps(original), encoding="utf-8")

            result = add_manual_ats_url(
                "Acme",
                "https://jobs.lever.co/acme",
                path,
                write=False,
                fetch_json_func=lambda _: [{"id": "job"}],
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(result["verification_status"], "verified")
        self.assertEqual(persisted, original)

    def test_batch_import_manual_ats_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "companies.json"
            csv_path = Path(tmp) / "ats.csv"
            registry.write_text(json.dumps([_company("Acme", "https://acme.example"), _company("Beta", "https://beta.example")]), encoding="utf-8")
            csv_path.write_text("company_name,ats_url\nAcme,https://jobs.lever.co/acme\nBeta,https://boards.greenhouse.io/beta\n", encoding="utf-8")

            results = import_manual_ats_urls(registry_path=registry, import_path=csv_path, write=True, fetch_json_func=_valid_feed)
            persisted = json.loads(registry.read_text(encoding="utf-8"))

        self.assertEqual(len(results), 2)
        self.assertEqual({entry["connector_type"] for entry in results}, {"lever", "greenhouse"})
        self.assertTrue(all(entry["enabled"] for entry in persisted))

    def test_batch_import_manual_ats_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "companies.json"
            import_path = Path(tmp) / "ats.json"
            registry.write_text(json.dumps([_company("Acme", "https://acme.example")]), encoding="utf-8")
            import_path.write_text(json.dumps([{"company_name": "Acme", "ats_url": "https://jobs.lever.co/acme"}]), encoding="utf-8")

            records = read_manual_ats_records(import_path)
            results = import_manual_ats_urls(import_path, registry, write=False, fetch_json_func=lambda _: [{"id": "job"}])
            persisted = json.loads(registry.read_text(encoding="utf-8"))

        self.assertEqual(records, [{"company_name": "Acme", "ats_url": "https://jobs.lever.co/acme"}])
        self.assertEqual(results[0]["verification_status"], "verified")
        self.assertFalse(persisted[0]["enabled"])

    def test_batch_import_adds_missing_company_conservatively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "companies.json"
            csv_path = Path(tmp) / "ats.csv"
            registry.write_text(json.dumps([_company("Acme", "https://acme.example")]), encoding="utf-8")
            csv_path.write_text("company_name,ats_url\nNewCo,https://jobs.lever.co/newco\n", encoding="utf-8")

            results = import_manual_ats_urls(
                import_path=csv_path,
                registry_path=registry,
                write=True,
                fetch_json_func=lambda _: (_ for _ in ()).throw(URLError("offline")),
            )
            persisted = json.loads(registry.read_text(encoding="utf-8"))

        self.assertEqual(results[0]["company_name"], "NewCo")
        self.assertEqual(results[0]["connector_type"], "lever")
        self.assertEqual(results[0]["connector_token"], "newco")
        self.assertEqual(results[0]["verification_status"], "pending_network_validation")
        self.assertFalse(results[0]["enabled"])
        self.assertEqual(len(persisted), 2)

    def test_unresolved_entries(self) -> None:
        entries = [
            {**_company("A", "https://a.example"), "verification_status": "verified", "enabled": True},
            {**_company("B", "https://b.example"), "verification_status": "pending_network_validation", "enabled": False},
        ]

        unresolved = unresolved_entries(entries)

        self.assertEqual([entry["company_name"] for entry in unresolved], ["B"])


def _company(name: str, careers_url: str) -> dict:
    return {
        "company_name": name,
        "industry": "SaaS",
        "priority": 4,
        "greenhouse_board_token": None,
        "lever_site_token": None,
        "ashby_board_name": None,
        "workable_account": None,
        "smartrecruiters_company_identifier": None,
        "careers_url": careers_url,
        "source_connector_type": "pending_verification",
        "enabled": False,
        "notes": "keep this note",
        "verification_status": "pending_network_validation",
    }


def _valid_feed(url: str):
    if "greenhouse" in url or "ashby" in url:
        return {"jobs": [{"id": 1}]}
    if "workable" in url:
        return {"jobs": [{"id": 1}]}
    if "smartrecruiters" in url:
        return {"content": [{"id": 1}]}
    return [{"id": 1}]


if __name__ == "__main__":
    unittest.main()
