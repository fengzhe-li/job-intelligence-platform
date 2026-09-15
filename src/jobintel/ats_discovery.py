from __future__ import annotations

import json
import csv
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

FetchText = Callable[[str], str]
FetchJson = Callable[[str], Any]


@dataclass(frozen=True)
class ATSCandidate:
    connector_type: str
    connector_token: str
    verification_url: str


@dataclass(frozen=True)
class ATSVerification:
    candidate: ATSCandidate
    status: str
    jobs_available: int | None = None
    message: str = ""
    requested_url: str = ""
    exception_type: str | None = None
    http_status_code: int | None = None
    failure_kind: str | None = None


GREENHOUSE_PATTERNS = (
    re.compile(r"https?://boards\.greenhouse\.io/([^/?#\"'<>]+)(?:[/?#][^\"'<>]*)?", re.IGNORECASE),
    re.compile(r"https?://job-boards\.greenhouse\.io/([^/?#\"'<>]+)(?:[/?#][^\"'<>]*)?", re.IGNORECASE),
    re.compile(r"https?://boards-api\.greenhouse\.io/v1/boards/([^/?#\"'<>]+)/jobs(?:[?#][^\"'<>]*)?", re.IGNORECASE),
    re.compile(r"https?://api\.greenhouse\.io/[^\"'<>]*/boards/([^/?#\"'<>]+)/jobs(?:[?#][^\"'<>]*)?", re.IGNORECASE),
)
LEVER_PATTERNS = (
    re.compile(r"https?://jobs\.lever\.co/([^/?#\"'<>]+)(?:[/?#][^\"'<>]*)?", re.IGNORECASE),
    re.compile(r"https?://api\.lever\.co/v0/postings/([^/?#\"'<>]+)(?:[?#][^\"'<>]*)?", re.IGNORECASE),
)
ASHBY_PATTERNS = (
    re.compile(r"https?://jobs\.ashbyhq\.com/([^/?#\"'<>]+)(?:[/?#][^\"'<>]*)?", re.IGNORECASE),
    re.compile(r"https?://api\.ashbyhq\.com/posting-api/job-board/([^/?#\"'<>]+)(?:[?#][^\"'<>]*)?", re.IGNORECASE),
)
WORKABLE_PATTERNS = (
    re.compile(r"https?://apply\.workable\.com/([^/?#\"'<>]+)(?:[/?#][^\"'<>]*)?", re.IGNORECASE),
    re.compile(r"https?://www\.workable\.com/api/accounts/([^/?#\"'<>]+)(?:[?#][^\"'<>]*)?", re.IGNORECASE),
)
SMARTRECRUITERS_PATTERNS = (
    re.compile(r"https?://careers\.smartrecruiters\.com/([^/?#\"'<>]+)(?:[/?#][^\"'<>]*)?", re.IGNORECASE),
    re.compile(r"https?://jobs\.smartrecruiters\.com/([^/?#\"'<>]+)(?:[/?#][^\"'<>]*)?", re.IGNORECASE),
    re.compile(r"https?://api\.smartrecruiters\.com/v1/companies/([^/?#\"'<>]+)/postings(?:[/?#][^\"'<>]*)?", re.IGNORECASE),
)


def discover_registry(
    registry_path: Path | str = "config/target_companies.json",
    write: bool = False,
    fetch_text: FetchText | None = None,
) -> list[dict[str, Any]]:
    path = Path(registry_path)
    entries = json.loads(path.read_text(encoding="utf-8"))
    fetch_text = fetch_text or _fetch_text
    updated: list[dict[str, Any]] = []
    for entry in entries:
        updated.append(discover_company_entry(entry, fetch_text))
    if write:
        path.write_text(json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return updated


def discover_company_entry(entry: dict[str, Any], fetch_text: FetchText) -> dict[str, Any]:
    if entry.get("verification_status") == "verified" and entry.get("enabled"):
        return entry
    careers_url = entry.get("careers_url")
    if not careers_url:
        return {**entry, "verification_status": entry.get("verification_status") or "pending_careers_url"}
    try:
        html = fetch_text(careers_url)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return {
            **entry,
            "enabled": False,
            "verification_status": "pending_network_validation",
            "verification_url": careers_url,
            "ats_discovery_error": type(exc).__name__,
        }
    candidates = detect_ats_candidates(html, careers_url)
    if not candidates:
        return {
            **entry,
            "enabled": False,
            "verification_status": "pending_ats_discovery",
            "verification_url": careers_url,
        }
    candidate = candidates[0]
    return _merge_candidate(entry, candidate, "discovered_pending_verification", enabled=False)


def verify_registry(
    registry_path: Path | str = "config/target_companies.json",
    write: bool = False,
    fetch_json_func: FetchJson | None = None,
) -> list[dict[str, Any]]:
    path = Path(registry_path)
    entries = json.loads(path.read_text(encoding="utf-8"))
    updated: list[dict[str, Any]] = []
    for entry in entries:
        updated.append(verify_company_entry(entry, fetch_json_func))
    if write:
        path.write_text(json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return updated


def add_manual_ats_url(
    company_name: str,
    ats_url: str,
    registry_path: Path | str = "config/target_companies.json",
    write: bool = True,
    fetch_json_func: FetchJson | None = None,
) -> dict[str, Any]:
    path = Path(registry_path)
    entries = json.loads(path.read_text(encoding="utf-8"))
    updated, result = add_manual_ats_url_to_entries(entries, company_name, ats_url, fetch_json_func)
    if write:
        path.write_text(json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def add_manual_ats_url_to_entries(
    entries: list[dict[str, Any]],
    company_name: str,
    ats_url: str,
    fetch_json_func: FetchJson | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates = detect_ats_candidates(ats_url, ats_url)
    if not candidates:
        raise ValueError(f"No supported ATS token found in URL: {ats_url}")
    candidate = candidates[0]
    matched = False
    updated: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    for entry in entries:
        if entry.get("company_name", "").casefold() == company_name.casefold():
            matched = True
            result = _verify_and_merge(entry, candidate, fetch_json_func)
            updated.append(result)
        else:
            updated.append(entry)
    if not matched:
        result = _verify_and_merge(_manual_company_entry(company_name, ats_url), candidate, fetch_json_func)
        updated.append(result)
    assert result is not None
    return updated, result


def import_manual_ats_urls(
    import_path: Path | str,
    registry_path: Path | str = "config/target_companies.json",
    write: bool = True,
    fetch_json_func: FetchJson | None = None,
) -> list[dict[str, Any]]:
    records = read_manual_ats_records(import_path)
    path = Path(registry_path)
    entries = json.loads(path.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    for record in records:
        entries, result = add_manual_ats_url_to_entries(entries, record["company_name"], record["ats_url"], fetch_json_func)
        results.append(result)
    if write:
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return results


def read_manual_ats_records(import_path: Path | str) -> list[dict[str, str]]:
    path = Path(import_path)
    if path.suffix.casefold() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [{"company_name": item["company_name"], "ats_url": item["ats_url"]} for item in payload]
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [{"company_name": row["company_name"], "ats_url": row["ats_url"]} for row in reader]


def verify_company_entry(entry: dict[str, Any], fetch_json_func: FetchJson | None) -> dict[str, Any]:
    candidate = candidate_from_entry(entry)
    if candidate is None:
        return {**entry, "enabled": False, "verification_status": entry.get("verification_status") or "pending_ats_discovery"}
    verification = verify_candidate(candidate, fetch_json_func)
    if verification.status == "verified":
        return {
            **_merge_candidate(entry, candidate, "verified", enabled=True),
            **_verification_metadata(verification),
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "jobs_available": verification.jobs_available,
            "ats_verification_error": None,
        }
    if verification.status == "pending_network_validation":
        return {
            **_merge_candidate(entry, candidate, "pending_network_validation", enabled=False),
            **_verification_metadata(verification),
            "ats_verification_error": verification.message,
        }
    return {
        **_merge_candidate(entry, candidate, verification.status, enabled=False),
        **_verification_metadata(verification),
        "jobs_available": verification.jobs_available,
        "ats_verification_error": verification.message,
    }


def unresolved_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in entries if entry.get("verification_status") != "verified" or not entry.get("enabled")]


def detect_ats_candidates(html: str, base_url: str) -> list[ATSCandidate]:
    text = urllib.parse.unquote(html)
    candidates: list[ATSCandidate] = []
    for pattern in GREENHOUSE_PATTERNS:
        for match in pattern.finditer(text):
            token = _clean_token(match.group(1))
            if token:
                candidates.append(ATSCandidate("greenhouse", token, _absolute(match.group(0), base_url)))
    for pattern in LEVER_PATTERNS:
        for match in pattern.finditer(text):
            token = _clean_token(match.group(1))
            if token:
                candidates.append(ATSCandidate("lever", token, _absolute(match.group(0), base_url)))
    for pattern in ASHBY_PATTERNS:
        for match in pattern.finditer(text):
            token = _clean_token(match.group(1))
            if token:
                candidates.append(ATSCandidate("ashby", token, _absolute(match.group(0), base_url)))
    for pattern in WORKABLE_PATTERNS:
        for match in pattern.finditer(text):
            token = _clean_token(match.group(1))
            if token:
                candidates.append(ATSCandidate("workable", token, _absolute(match.group(0), base_url)))
    for pattern in SMARTRECRUITERS_PATTERNS:
        for match in pattern.finditer(text):
            token = _clean_token(match.group(1))
            if token:
                candidates.append(ATSCandidate("smartrecruiters", token, _absolute(match.group(0), base_url)))
    return _dedupe_candidates(candidates)


def candidate_from_entry(entry: dict[str, Any]) -> ATSCandidate | None:
    connector_type = entry.get("connector_type")
    connector_token = entry.get("connector_token")
    if connector_type in {"greenhouse", "lever", "ashby", "workable", "smartrecruiters"} and connector_token:
        return ATSCandidate(connector_type, connector_token, entry.get("verification_url") or entry.get("careers_url") or "")
    if entry.get("greenhouse_board_token"):
        return ATSCandidate("greenhouse", entry["greenhouse_board_token"], entry.get("verification_url") or entry.get("careers_url") or "")
    if entry.get("lever_site_token"):
        return ATSCandidate("lever", entry["lever_site_token"], entry.get("verification_url") or entry.get("careers_url") or "")
    if entry.get("ashby_board_name"):
        return ATSCandidate("ashby", entry["ashby_board_name"], entry.get("verification_url") or entry.get("careers_url") or "")
    if entry.get("workable_account"):
        return ATSCandidate("workable", entry["workable_account"], entry.get("verification_url") or entry.get("careers_url") or "")
    if entry.get("smartrecruiters_company_identifier"):
        return ATSCandidate("smartrecruiters", entry["smartrecruiters_company_identifier"], entry.get("verification_url") or entry.get("careers_url") or "")
    return None


def verify_candidate(candidate: ATSCandidate, fetch_json_func: FetchJson | None = None) -> ATSVerification:
    fetch_json_func = fetch_json_func or _fetch_json_for_verification
    url = public_feed_url(candidate)
    try:
        payload = fetch_json_func(url)
    except Exception as exc:
        return _verification_from_exception(candidate, url, exc)
    if candidate.connector_type == "greenhouse":
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if isinstance(jobs, list):
            return ATSVerification(
                candidate,
                "verified",
                jobs_available=len(jobs),
                message="valid_empty_job_feed" if not jobs else "valid_job_feed",
                requested_url=url,
            )
    if candidate.connector_type == "lever" and isinstance(payload, list):
        return ATSVerification(
            candidate,
            "verified",
            jobs_available=len(payload),
            message="valid_empty_job_feed" if not payload else "valid_job_feed",
            requested_url=url,
        )
    if candidate.connector_type == "ashby":
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if isinstance(jobs, list):
            return ATSVerification(
                candidate,
                "verified",
                jobs_available=len(jobs),
                message="valid_empty_job_feed" if not jobs else "valid_job_feed",
                requested_url=url,
            )
    if candidate.connector_type == "workable":
        jobs = _workable_jobs(payload)
        if jobs is not None:
            return ATSVerification(
                candidate,
                "verified",
                jobs_available=len(jobs),
                message="valid_empty_job_feed" if not jobs else "valid_job_feed",
                requested_url=url,
            )
    if candidate.connector_type == "smartrecruiters":
        jobs = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(jobs, list):
            return ATSVerification(
                candidate,
                "verified",
                jobs_available=len(jobs),
                message="valid_empty_job_feed" if not jobs else "valid_job_feed",
                requested_url=url,
            )
    return ATSVerification(
        candidate,
        "invalid_feed",
        jobs_available=0,
        message="Unexpected feed shape",
        requested_url=url,
        failure_kind="unexpected_feed_shape",
    )


def check_ats_url(ats_url: str, fetch_json_func: FetchJson | None = None) -> ATSVerification:
    candidates = detect_ats_candidates(ats_url, ats_url)
    if not candidates:
        raise ValueError(f"No supported ATS token found in URL: {ats_url}")
    return verify_candidate(candidates[0], fetch_json_func)


def public_feed_url(candidate: ATSCandidate) -> str:
    if candidate.connector_type == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{candidate.connector_token}/jobs?content=true"
    if candidate.connector_type == "lever":
        return f"https://api.lever.co/v0/postings/{candidate.connector_token}?mode=json"
    if candidate.connector_type == "ashby":
        token = urllib.parse.quote(candidate.connector_token, safe="")
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    if candidate.connector_type == "workable":
        return f"https://www.workable.com/api/accounts/{candidate.connector_token}?details=true"
    if candidate.connector_type == "smartrecruiters":
        return f"https://api.smartrecruiters.com/v1/companies/{candidate.connector_token}/postings?limit=100&offset=0&country=gb"
    raise ValueError(f"Unsupported connector type: {candidate.connector_type}")


def ats_summary(entries: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "companies_checked": len(entries),
        "greenhouse_candidates_found": len([entry for entry in entries if candidate_from_entry(entry) and candidate_from_entry(entry).connector_type == "greenhouse"]),
        "lever_candidates_found": len([entry for entry in entries if candidate_from_entry(entry) and candidate_from_entry(entry).connector_type == "lever"]),
        "ashby_candidates_found": len([entry for entry in entries if candidate_from_entry(entry) and candidate_from_entry(entry).connector_type == "ashby"]),
        "workable_candidates_found": len([entry for entry in entries if candidate_from_entry(entry) and candidate_from_entry(entry).connector_type == "workable"]),
        "smartrecruiters_candidates_found": len([entry for entry in entries if candidate_from_entry(entry) and candidate_from_entry(entry).connector_type == "smartrecruiters"]),
        "successfully_verified_connectors": len([entry for entry in entries if entry.get("verification_status") == "verified" and entry.get("enabled")]),
        "pending_unresolved_companies": len([entry for entry in entries if entry.get("verification_status") != "verified"]),
    }


def _verify_and_merge(entry: dict[str, Any], candidate: ATSCandidate, fetch_json_func: FetchJson | None) -> dict[str, Any]:
    verification = verify_candidate(candidate, fetch_json_func)
    if verification.status == "verified":
        return {
            **_merge_candidate(entry, candidate, "verified", enabled=True),
            **_verification_metadata(verification),
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "jobs_available": verification.jobs_available,
            "ats_verification_error": None,
        }
    if verification.status == "pending_network_validation":
        return {
            **_merge_candidate(entry, candidate, "pending_network_validation", enabled=False),
            **_verification_metadata(verification),
            "ats_verification_error": verification.message,
        }
    return {
        **_merge_candidate(entry, candidate, verification.status, enabled=False),
        **_verification_metadata(verification),
        "jobs_available": verification.jobs_available,
        "ats_verification_error": verification.message,
    }


def _merge_candidate(entry: dict[str, Any], candidate: ATSCandidate, status: str, enabled: bool) -> dict[str, Any]:
    updated = {
        **entry,
        "connector_type": candidate.connector_type,
        "connector_token": candidate.connector_token,
        "verification_status": status,
        "verification_url": candidate.verification_url,
        "enabled": enabled,
    }
    if candidate.connector_type == "greenhouse":
        updated["greenhouse_board_token"] = candidate.connector_token
    elif candidate.connector_type == "lever":
        updated["lever_site_token"] = candidate.connector_token
    elif candidate.connector_type == "ashby":
        updated["ashby_board_name"] = candidate.connector_token
    elif candidate.connector_type == "workable":
        updated["workable_account"] = candidate.connector_token
    elif candidate.connector_type == "smartrecruiters":
        updated["smartrecruiters_company_identifier"] = candidate.connector_token
    return updated


def _manual_company_entry(company_name: str, ats_url: str) -> dict[str, Any]:
    return {
        "company_name": company_name,
        "industry": "manual ATS import",
        "priority": 3,
        "greenhouse_board_token": None,
        "lever_site_token": None,
        "ashby_board_name": None,
        "workable_account": None,
        "smartrecruiters_company_identifier": None,
        "careers_url": ats_url,
        "source_connector_type": "manual_ats_url",
        "enabled": False,
        "notes": "Added from manual ATS URL import.",
        "verification_status": "pending_network_validation",
    }


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "jobintel-ats-discovery/0.1", "Accept": "text/html,*/*"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8", errors="ignore")


def _fetch_json_for_verification(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "jobintel-ats-verifier/0.1 (+https://github.com/local/jobintel)",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8")
    return json.loads(body)


def _verification_metadata(verification: ATSVerification) -> dict[str, Any]:
    return {
        "verification_requested_url": verification.requested_url or public_feed_url(verification.candidate),
        "verification_exception_type": verification.exception_type,
        "verification_http_status_code": verification.http_status_code,
        "verification_failure_kind": verification.failure_kind,
        "verification_message": verification.message,
    }


def _verification_from_exception(candidate: ATSCandidate, url: str, exc: Exception) -> ATSVerification:
    failure_kind = _classify_exception(exc)
    http_status_code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
    status = "invalid_token" if failure_kind == "invalid_board_or_site_token" else "pending_network_validation"
    if failure_kind == "malformed_json":
        status = "invalid_feed"
    return ATSVerification(
        candidate,
        status,
        jobs_available=0 if status != "pending_network_validation" else None,
        message=_diagnostic_message(exc, failure_kind),
        requested_url=url,
        exception_type=type(exc).__name__,
        http_status_code=http_status_code,
        failure_kind=failure_kind,
    )


def _classify_exception(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in {404, 410}:
            return "invalid_board_or_site_token"
        return "http_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            return "dns_error"
        if isinstance(reason, ssl.SSLError):
            return "ssl_certificate_error"
        if isinstance(reason, TimeoutError | socket.timeout):
            return "timeout"
        lowered = str(reason).casefold()
        if "certificate" in lowered or "ssl" in lowered:
            return "ssl_certificate_error"
        if "timed out" in lowered or "timeout" in lowered:
            return "timeout"
        if "name or service not known" in lowered or "nodename nor servname" in lowered or "dns" in lowered:
            return "dns_error"
        return "url_error"
    if isinstance(exc, ssl.SSLError):
        return "ssl_certificate_error"
    if isinstance(exc, socket.gaierror):
        return "dns_error"
    if isinstance(exc, json.JSONDecodeError):
        return "malformed_json"
    return "unexpected_exception"


def _diagnostic_message(exc: Exception, failure_kind: str) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"{failure_kind}: HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"{failure_kind}: {exc.reason}"
    return f"{failure_kind}: {exc}"


def _clean_token(token: str) -> str:
    cleaned = token.strip().strip("/").split("/", 1)[0]
    if cleaned.casefold() in {"embed", "jobs", "job", "departments"}:
        return ""
    return cleaned


def _workable_jobs(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("jobs", "results", "positions"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return None


def _absolute(url: str, base_url: str) -> str:
    return urllib.parse.urljoin(base_url, url)


def _dedupe_candidates(candidates: list[ATSCandidate]) -> list[ATSCandidate]:
    seen: set[tuple[str, str]] = set()
    unique: list[ATSCandidate] = []
    for candidate in candidates:
        key = (candidate.connector_type, candidate.connector_token)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique
