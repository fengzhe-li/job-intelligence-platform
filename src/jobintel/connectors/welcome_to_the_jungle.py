from __future__ import annotations

import csv
import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload, ScopeCollector, SourceHealth, SourceSnapshot
from jobintel.connectors.utils import build_url, fetch_json, normalise_location, now_utc, parse_datetime, strip_html
from jobintel.models.job import Job, SourceObservation


WTTJ_SOURCE_NAME = "welcome_to_the_jungle"
WELCOMEKIT_JOBS_URL = "https://www.welcomekit.co/api/v1/external/jobs"
# Safety cap per organization; hitting it makes that organization's snapshot
# incomplete (never closure-eligible) rather than silently "complete".
WTTJ_MAX_PAGES = 50
WTTJ_JOB_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?welcometothejungle\.com/(?:[a-z]{2}/)?companies/([^/?#]+)/jobs/([^/?#]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WTTJManualImportItem:
    row_number: int
    url: str
    status: str
    record: dict[str, Any] | None = None
    error: str = ""


class WelcomeToTheJungleConnector(JobSourceConnector):
    """Credentialed WTTJ/WelcomeKit Jobs API connector.

    This connector only uses documented HTTPS JSON access. The official Jobs
    API requires a WelcomeKit API key with jobs_r or jobs_rw scope and one or
    more organization refs.
    """

    source_name = WTTJ_SOURCE_NAME

    def __init__(self, organization_references: tuple[str, ...], api_key: str | None) -> None:
        self.organization_references = organization_references
        self.api_key = api_key

    supports_closure_inference = True

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        return self.fetch_snapshot(query).payloads_or_raise()

    def fetch_snapshot(self, query: ConnectorQuery) -> SourceSnapshot:
        # One organization reference = one closure scope, prefixed so it can
        # never collide with manual WTTJ imports (which share this source_name
        # but carry no scope, so an API refresh can never close them).
        if not self.api_key:
            raise RuntimeError("Welcome to the Jungle official API ingestion requires WTTJ_API_KEY")
        observed_at = now_utc()
        collector = ScopeCollector(query)
        per_page = max(1, min(query.limit, 100))
        for organization_reference in self.organization_references:
            scope = f"official_api:{organization_reference}"
            listed: list[RawJobPayload] = []
            filtered_out = 0
            exhausted = False
            try:
                for page in range(1, WTTJ_MAX_PAGES + 1):
                    url = build_url(
                        WELCOMEKIT_JOBS_URL,
                        {
                            "organization_reference": organization_reference,
                            "status": "published",
                            "per_page": per_page,
                            "page": page,
                        },
                    )
                    payload = fetch_json(
                        url,
                        headers={
                            "Accept": "application/json",
                            "Authorization": f"Bearer {self.api_key}",
                            "User-Agent": "jobintel-wttj/0.1",
                        },
                    )
                    items = _jobs(payload)
                    for item in items:
                        enriched = {**item, "_organization_reference": organization_reference, "_ingestion_method": "official_api"}
                        if not _is_relevant(enriched, query):
                            filtered_out += 1
                            continue
                        source_url = _wttj_url(enriched) or _application_url(enriched)
                        apply_url = _application_url(enriched) or source_url
                        source_id = _source_job_id(enriched) or _id_from_url(source_url) or f"{organization_reference}-{len(listed) + 1}"
                        listed.append(
                            RawJobPayload(
                                source_name=self.source_name,
                                source_job_id=source_id,
                                source_url=source_url,
                                canonical_application_url=apply_url,
                                raw_payload=enriched,
                                observed_at=observed_at,
                                posted_at=parse_datetime(enriched.get("published_at") or enriched.get("created_at") or enriched.get("updated_at")),
                            )
                        )
                    if len(items) < per_page:
                        exhausted = True
                        break
                    if len(listed) > query.limit:
                        # Enough to prove truncation; ScopeCollector marks it.
                        exhausted = True
                        break
            except Exception as exc:
                collector.fail(scope, organization_reference, f"{type(exc).__name__}: {exc}")
                continue
            collector.add_scope(scope, listed, filtered_out=filtered_out, exhausted=exhausted)
        return collector.snapshot()

    def closure_scope(self, raw_payload: dict[str, Any]) -> str | None:
        scope = super().closure_scope(raw_payload)
        if scope:
            return scope
        if raw_payload.get("_ingestion_method") == "official_api" and raw_payload.get("_organization_reference"):
            return f"official_api:{raw_payload['_organization_reference']}"
        return None

    def health_check(self) -> SourceHealth:
        if not self.api_key:
            return SourceHealth(self.source_name, False, "Missing WTTJ_API_KEY for official WelcomeKit Jobs API", now_utc())
        if not self.organization_references:
            return SourceHealth(self.source_name, False, "No WTTJ organization references configured", now_utc())
        return SourceHealth(self.source_name, True, f"{len(self.organization_references)} organization reference(s) configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        return _normalise_wttj_payload(raw)


class WelcomeToTheJungleManualConnector(JobSourceConnector):
    source_name = WTTJ_SOURCE_NAME

    def __init__(self, records: tuple[dict[str, Any], ...]) -> None:
        self.records = records

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        observed_at = now_utc()
        raw_jobs: list[RawJobPayload] = []
        for index, record in enumerate(self.records, start=1):
            payload = {**record, "_ingestion_method": record.get("_ingestion_method") or "manual_import"}
            if not _is_relevant(payload, query):
                continue
            source_id = _source_job_id(payload) or f"manual-{index}"
            source_url = _wttj_url(payload) or _application_url(payload) or str(payload.get("url") or "")
            apply_url = _application_url(payload) or source_url
            raw_jobs.append(
                RawJobPayload(
                    source_name=self.source_name,
                    source_job_id=source_id,
                    source_url=source_url,
                    canonical_application_url=apply_url,
                    raw_payload=payload,
                    observed_at=observed_at,
                    posted_at=parse_datetime(payload.get("posted_at") or payload.get("published_at") or payload.get("created_at")),
                )
            )
        return raw_jobs[: query.limit]

    def health_check(self) -> SourceHealth:
        return SourceHealth(self.source_name, bool(self.records), f"{len(self.records)} manual WTTJ record(s) configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        return _normalise_wttj_payload(raw)


def read_wttj_manual_records(import_path: Path | str) -> list[dict[str, Any]]:
    return [item.record for item in prepare_wttj_manual_import(import_path) if item.record is not None and item.status == "imported"]


def read_wttj_discovery_records(import_path: Path | str) -> list[dict[str, Any]]:
    return [item.record for item in prepare_wttj_discovery_import(import_path) if item.record is not None and item.status == "imported"]


def prepare_wttj_discovery_import(import_path: Path | str) -> list[WTTJManualImportItem]:
    path = Path(import_path)
    if path.suffix.casefold() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("jobs") or payload.get("records") or []
        if not isinstance(payload, list):
            raise ValueError("WTTJ discovery JSON import must contain a list, jobs, or records")
        return [_manual_import_item(item, "discovery_import", row_number=index) for index, item in enumerate(payload, start=1) if isinstance(item, dict)]
    with path.open(newline="", encoding="utf-8") as handle:
        return [_manual_import_item(row, "discovery_import", row_number=index) for index, row in enumerate(csv.DictReader(handle), start=2)]


def prepare_wttj_manual_import(import_path: Path | str) -> list[WTTJManualImportItem]:
    path = Path(import_path)
    if path.suffix.casefold() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("jobs") or payload.get("records") or []
        if not isinstance(payload, list):
            raise ValueError("WTTJ JSON import must contain a list, jobs, or records")
        return [_manual_import_item(item, "manual_json", row_number=index) for index, item in enumerate(payload, start=1) if isinstance(item, dict)]
    if path.suffix.casefold() in {".txt", ".urls"}:
        return [_manual_import_item({"wttj_url": line.strip()}, "manual_url", row_number=index) for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1) if line.strip()]
    with path.open(newline="", encoding="utf-8") as handle:
        return [_manual_import_item(row, "manual_csv", row_number=index) for index, row in enumerate(csv.DictReader(handle), start=2)]


def _normalise_wttj_payload(raw: RawJobPayload) -> Job:
    payload = raw.raw_payload
    description = _description(payload)
    location_text = _location_text(payload)
    locations = [normalise_location(location_text or "United Kingdom")]
    observation = SourceObservation(
        source_name=raw.source_name,
        source_job_id=raw.source_job_id,
        original_url=raw.source_url,
        first_seen_at=raw.first_seen_at or raw.observed_at,
        last_seen_at=raw.last_seen_at or raw.observed_at,
        posted_at=raw.posted_at,
        raw_description=description,
        canonical_application_url=raw.canonical_application_url,
        raw_payload={**payload, "_enrichment_state": wttj_enrichment_state(payload)},
    )
    return Job(
        id=f"wttj:{raw.source_job_id}",
        title=_first_text(payload.get("name"), payload.get("title"), payload.get("job_title"), _title_from_url(raw.source_url), "WTTJ imported job"),
        company=_company(payload),
        description=description,
        locations=locations,
        source_observations=[observation],
        salary=_salary(payload),
        raw_location=location_text or "United Kingdom",
    )


def _jobs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("jobs", "data", "results"):
        if isinstance(payload.get(key), list):
            return [item for item in payload[key] if isinstance(item, dict)]
    return []


def _manual_import_item(record: dict[str, Any], ingestion_method: str, row_number: int) -> WTTJManualImportItem:
    url = _first_text(record.get("wttj_url"), record.get("job_url"), record.get("url"), record.get("source_url"), record.get("ats_url"))
    if not url:
        return WTTJManualImportItem(row_number=row_number, url="", status="missing_required_fields", error="missing WTTJ job URL")
    if not is_wttj_job_url(url):
        if _is_wttj_url(url):
            return WTTJManualImportItem(row_number=row_number, url=url, status="unsupported_page_structure", error="WTTJ URL is not a supported job URL")
        return WTTJManualImportItem(row_number=row_number, url=url, status="invalid_url", error="not a supported Welcome to the Jungle job URL")
    normalised = _normalise_manual_record(record, ingestion_method)
    missing = []
    if ingestion_method in {"manual_csv", "manual_json", "discovery_import"} and not _first_text(record.get("title"), record.get("name"), record.get("job_title")):
        missing.append("title")
    if ingestion_method in {"manual_csv", "manual_json", "discovery_import"} and not _first_text(record.get("company"), record.get("company_name"), record.get("organization_name")):
        missing.append("company")
    if missing:
        return WTTJManualImportItem(row_number=row_number, url=url, status="missing_required_fields", record=normalised, error=f"missing {', '.join(missing)}")
    return WTTJManualImportItem(row_number=row_number, url=url, status="imported", record=normalised)


def is_wttj_job_url(url: str) -> bool:
    return bool(WTTJ_JOB_URL_PATTERN.match(url.strip()))


def _is_wttj_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url.strip())
    return parsed.netloc.casefold() in {"welcometothejungle.com", "www.welcometothejungle.com"}


def _normalise_manual_record(record: dict[str, Any], ingestion_method: str = "manual_import") -> dict[str, Any]:
    url = _first_text(record.get("wttj_url"), record.get("job_url"), record.get("url"), record.get("source_url"), record.get("ats_url"))
    apply_url = _first_text(record.get("application_url"), record.get("apply_url"), record.get("direct_apply_url"), url)
    return {
        **record,
        "_imported_payload": dict(record),
        "name": _first_text(record.get("name"), record.get("title"), record.get("job_title"), _title_from_url(url)),
        "organization_name": _first_text(record.get("company"), record.get("company_name"), record.get("organization_name"), _company_from_url(url)),
        "location": _first_text(record.get("location"), record.get("raw_location"), _location_from_url(url)),
        "description": _first_text(record.get("description"), record.get("job_description"), record.get("summary")),
        "requirements": _first_text(record.get("requirements"), record.get("requirement"), record.get("profile")),
        "work_mode": _first_text(record.get("work_mode"), record.get("remote_policy"), record.get("remote")),
        "remote_policy": _first_text(record.get("remote_policy"), record.get("remote")),
        "salary": _first_text(record.get("salary")),
        "source_category": _first_text(record.get("source_category")),
        "discovery_url": _first_text(record.get("discovery_url")),
        "contract_type": _first_text(record.get("contract_type")),
        "employment_type": _first_text(record.get("employment_type")),
        "notes": _first_text(record.get("notes")),
        "apply_url": apply_url,
        "url": url,
        "reference": _first_text(record.get("source_job_id"), record.get("job_id"), record.get("reference"), _id_from_url(url)),
        "_ingestion_method": ingestion_method,
}


def wttj_enrichment_state(payload: dict[str, Any]) -> str:
    has_jd = bool(_first_text(payload.get("description"), payload.get("description_html"), payload.get("requirements"), payload.get("profile"), payload.get("summary")))
    has_apply = bool(_first_text(payload.get("direct_apply_url"), payload.get("apply_url"), payload.get("application_url")))
    has_posted = bool(_first_text(payload.get("posted_at"), payload.get("published_at"), payload.get("created_at")))
    if not has_jd:
        return "discovery_only"
    if has_apply and has_posted:
        return "fully_enriched"
    return "partially_enriched"


def _description(payload: dict[str, Any]) -> str:
    parts = [
        payload.get("description"),
        payload.get("description_html"),
        payload.get("requirements"),
        payload.get("profile"),
        payload.get("profile_description"),
        payload.get("responsibilities"),
        payload.get("benefits"),
        payload.get("company_description"),
        payload.get("summary"),
    ]
    return strip_html("\n".join(_text_value(part) for part in parts if _text_value(part)))


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_text_value(item) for item in value.values() if _text_value(item))
    if isinstance(value, list):
        return "\n".join(_text_value(item) for item in value if _text_value(item))
    return str(value)


def _company(payload: dict[str, Any]) -> str:
    organization = payload.get("organization") or payload.get("company")
    if isinstance(organization, dict):
        return _first_text(organization.get("name"), organization.get("reference"), payload.get("organization_name"), payload.get("_organization_reference"), "Unknown company")
    return _first_text(organization, payload.get("organization_name"), payload.get("company_name"), payload.get("_organization_reference"), "Unknown company")


def _location_text(payload: dict[str, Any]) -> str:
    direct = _first_text(payload.get("location"), payload.get("raw_location"), payload.get("office_name"))
    if direct:
        return _with_remote(direct, payload)
    office = payload.get("office") or payload.get("office_address")
    if isinstance(office, dict):
        return _with_remote(
            ", ".join(part for part in (_first_text(office.get("city")), _first_text(office.get("country")), _first_text(office.get("name"))) if part),
            payload,
        )
    if isinstance(office, str):
        return _with_remote(office, payload)
    offices = payload.get("offices")
    if isinstance(offices, list) and offices:
        return _location_text({"office": offices[0], "remote": payload.get("remote")})
    return _with_remote("Remote" if payload.get("remote") is True else "United Kingdom", payload)


def _with_remote(location: str, payload: dict[str, Any]) -> str:
    remote_value = payload.get("remote")
    if remote_value is True and "remote" not in location.casefold():
        return f"{location} Remote".strip()
    remote = _first_text(remote_value, payload.get("remote_policy"), payload.get("work_mode"))
    mode = remote.casefold()
    lowered = location.casefold()
    if mode in {"fulltime", "full_time", "remote"} and "remote" not in lowered:
        return f"{location} Remote".strip()
    if mode == "hybrid" and "hybrid" not in lowered:
        return f"{location} Hybrid".strip()
    if mode in {"onsite", "on-site"} and "site" not in lowered:
        return f"{location} On-site".strip()
    return location


def _source_job_id(payload: dict[str, Any]) -> str:
    return _first_text(payload.get("source_job_id"), payload.get("job_id"), payload.get("reference"), payload.get("external_reference"), payload.get("id"), payload.get("uuid"), _id_from_url(_wttj_url(payload)))


def _wttj_url(payload: dict[str, Any]) -> str:
    for key in ("wttj_url", "job_url", "source_url", "url", "website_url"):
        text = _first_text(payload.get(key))
        if text:
            return text
    websites = payload.get("websites") or payload.get("websites_urls")
    if isinstance(websites, list):
        for item in websites:
            text = _first_text(item.get("url") if isinstance(item, dict) else item)
            if text:
                return text
    return ""


def _application_url(payload: dict[str, Any]) -> str:
    return _first_text(payload.get("apply_url"), payload.get("application_url"), payload.get("direct_apply_url"), _wttj_url(payload))


def _salary(payload: dict[str, Any]) -> str | None:
    salary = payload.get("salary")
    if isinstance(salary, str):
        return salary
    if not isinstance(salary, dict):
        return None
    text = _first_text(salary.get("text"), salary.get("minimum"), salary.get("min"))
    currency = _first_text(salary.get("currency"))
    if text and currency and currency not in text:
        return f"{currency} {text}"
    return text or None


def _title_from_url(url: str) -> str:
    slug = _id_from_url(url)
    if not slug:
        return ""
    title_slug = slug.split("_", 1)[0]
    words = title_slug.replace("_", "-").split("-")
    cleaned = " ".join(word for word in words if not word.isdigit())
    return cleaned.title()


def _company_from_url(url: str) -> str:
    match = WTTJ_JOB_URL_PATTERN.match(url.strip())
    if not match:
        return ""
    return urllib.parse.unquote(match.group(1)).replace("-", " ").replace("_", " ").title()


def _location_from_url(url: str) -> str:
    slug = _id_from_url(url)
    if "_" not in slug:
        return ""
    location = slug.rsplit("_", 1)[-1].replace("-", " ").replace("_", " ").strip()
    return location.title() if location else ""


def _id_from_url(url: str) -> str:
    if not url:
        return ""
    path = urllib.parse.unquote(url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])
    return path or ""


def _is_relevant(payload: dict[str, Any], query: ConnectorQuery) -> bool:
    if not query.keywords:
        return True
    haystack = f"{payload.get('name', '')} {payload.get('title', '')}\n{_description(payload)}".casefold()
    return any(keyword.casefold() in haystack for keyword in query.keywords)


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
