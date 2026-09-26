from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload, SourceHealth
from jobintel.connectors.utils import (
    extract_json_ld,
    fetch_text,
    json_ld_location_text,
    json_ld_organisation_name,
    normalise_location,
    now_utc,
    parse_datetime,
    strip_html,
)
from jobintel.models.job import Job, SourceObservation


@dataclass(frozen=True)
class ManualSourceSpec:
    """Describes a job-board family with no documented public API.

    Several UK graduate-focused boards (Trackr, Gradcracker, Bright Network,
    Prospects, ...) don't expose a compliant public jobs API the way Greenhouse,
    Lever, or WelcomeKit/WTTJ do, and this project does not scrape application
    pages. `manual_source.py` generalises the manual/discovery-import mechanism
    first built for `welcome_to_the_jungle.py` so each such source can be added
    as a thin `ManualSourceSpec` instead of a bespoke ~400-line connector.
    """

    source_name: str
    display_name: str
    is_source_url: Callable[[str], bool]


@dataclass(frozen=True)
class ManualImportItem:
    row_number: int
    url: str
    status: str
    record: dict[str, Any] | None = None
    error: str = ""


class ManualSourceConnector(JobSourceConnector):
    """Rich manual import: CSV/JSON/URL-list records, feeds normal enrichment/ranking."""

    def __init__(self, spec: ManualSourceSpec, records: tuple[dict[str, Any], ...]) -> None:
        self.spec = spec
        self.source_name = spec.source_name
        self.records = records

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        observed_at = now_utc()
        raw_jobs: list[RawJobPayload] = []
        for index, record in enumerate(self.records, start=1):
            payload = {**record, "_ingestion_method": record.get("_ingestion_method") or "manual_import"}
            if not _is_relevant(payload, query):
                continue
            source_id = _source_job_id(payload) or f"manual-{index}"
            source_url = _source_url(payload)
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
        return SourceHealth(self.source_name, bool(self.records), f"{len(self.records)} manual {self.spec.display_name} record(s) configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        return _normalise_manual_payload(self.spec, raw)


class ManualSourceDiscoveryConnector(JobSourceConnector):
    """Lighter bulk discovery import for list-page metadata (no full JD yet)."""

    def __init__(self, spec: ManualSourceSpec, records: tuple[dict[str, Any], ...]) -> None:
        self.spec = spec
        self.source_name = spec.source_name
        self.records = records

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        observed_at = now_utc()
        raw_jobs: list[RawJobPayload] = []
        for index, record in enumerate(self.records, start=1):
            payload = {**record, "_ingestion_method": "discovery_import"}
            if not _is_relevant(payload, query):
                continue
            source_id = _source_job_id(payload) or f"discovery-{index}"
            source_url = _source_url(payload)
            apply_url = _application_url(payload) or source_url
            raw_jobs.append(
                RawJobPayload(
                    source_name=self.source_name,
                    source_job_id=source_id,
                    source_url=source_url,
                    canonical_application_url=apply_url,
                    raw_payload=payload,
                    observed_at=observed_at,
                    posted_at=parse_datetime(payload.get("posted_at")),
                )
            )
        return raw_jobs[: query.limit]

    def health_check(self) -> SourceHealth:
        return SourceHealth(self.source_name, bool(self.records), f"{len(self.records)} discovery {self.spec.display_name} record(s) configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        return _normalise_manual_payload(self.spec, raw)


@dataclass(frozen=True)
class SingleUrlImportResult:
    """Result of importing one job the user found themselves at a specific URL.

    Any of Trackr/Gradcracker/Bright Network/Prospects/WTTJ (or any other
    source, including a job on a company site not yet in the ATS registry)
    can be imported this way -- unlike the batch CSV/JSON/URL-list import
    above, this path is for "I found this one specific posting, add it."
    """

    url: str
    status: str  # "auto_fetched" | "auto_fetched_partial" | "manual_fields_only" | "missing_required_fields"
    record: dict[str, Any] | None
    auto_fetched_fields: tuple[str, ...] = ()
    fetch_error: str = ""


_AUTO_FETCHABLE_FIELDS = ("title", "company", "location", "description", "posted_at", "deadline")


def prepare_single_url_import(
    url: str,
    overrides: dict[str, Any] | None = None,
    fetch_text_func: Callable[[str], str] | None = None,
    skip_auto_fetch: bool = False,
) -> SingleUrlImportResult:
    """Build one manual-import record for a single job URL the user found.

    Attempts to auto-fetch schema.org `JobPosting` JSON-LD from the URL first
    -- the same compliant, publicly-published structured-data mechanism
    `connectors/prospects.py` uses for its live discovery, applicable to any
    site that publishes it for search-engine indexing, not just Prospects.
    Any field not recoverable that way, or explicitly overridden via
    `overrides`, falls back to `overrides`. A field present in neither is left
    out entirely -- never invented, never defaulted to placeholder text.
    """
    overrides = overrides or {}
    fetch_text_func = fetch_text_func or fetch_text
    auto: dict[str, Any] = {}
    fetch_error = ""
    if skip_auto_fetch:
        fetch_error = "auto-fetch skipped (--no-auto-fetch)"
    else:
        try:
            html_text = fetch_text_func(url)
            posting = extract_json_ld(html_text, "JobPosting")
            if posting is not None:
                auto = {
                    "title": _first_text(posting.get("title")),
                    "company": json_ld_organisation_name(posting.get("hiringOrganization")),
                    "location": json_ld_location_text(posting.get("jobLocation")),
                    "description": strip_html(_first_text(posting.get("description"))),
                    "posted_at": _first_text(posting.get("datePosted")),
                    "deadline": _first_text(posting.get("validThrough")),
                }
            else:
                fetch_error = "page fetched but no JobPosting JSON-LD found (site may not publish structured data, or its layout differs)"
        except Exception as exc:  # noqa: BLE001 -- auto-fetch is best-effort; a failure here means "no auto-fetched fields", not a fatal import error
            fetch_error = f"{type(exc).__name__}: {exc}"

    merged: dict[str, Any] = {"job_url": url}
    auto_fetched_fields: list[str] = []
    for field in _AUTO_FETCHABLE_FIELDS:
        override_value = overrides.get(field)
        if override_value:
            merged[field] = override_value
        elif auto.get(field):
            merged[field] = auto[field]
            auto_fetched_fields.append(field)
    for field in ("salary", "application_url", "source_job_id"):
        if overrides.get(field):
            merged[field] = overrides[field]

    ingestion_method = "manual_single_url_auto_fetched" if auto_fetched_fields else "manual_single_url"
    normalised = _normalise_manual_record(merged, ingestion_method)
    missing = [field for field in ("title", "company") if not normalised.get(field)]
    if missing:
        reason = fetch_error or f"missing {', '.join(missing)} -- not found via auto-fetch and not supplied manually"
        return SingleUrlImportResult(url=url, status="missing_required_fields", record=normalised, auto_fetched_fields=tuple(auto_fetched_fields), fetch_error=reason)
    if len(auto_fetched_fields) == len(_AUTO_FETCHABLE_FIELDS):
        status = "auto_fetched"
    elif auto_fetched_fields:
        status = "auto_fetched_partial"
    else:
        status = "manual_fields_only"
    return SingleUrlImportResult(url=url, status=status, record=normalised, auto_fetched_fields=tuple(auto_fetched_fields), fetch_error=fetch_error)


def read_manual_records(spec: ManualSourceSpec, import_path: Path | str) -> list[dict[str, Any]]:
    return [item.record for item in prepare_manual_import(spec, import_path) if item.record is not None and item.status == "imported"]


def read_discovery_records(spec: ManualSourceSpec, import_path: Path | str) -> list[dict[str, Any]]:
    return [item.record for item in prepare_discovery_import(spec, import_path) if item.record is not None and item.status == "imported"]


def prepare_manual_import(spec: ManualSourceSpec, import_path: Path | str) -> list[ManualImportItem]:
    path = Path(import_path)
    if path.suffix.casefold() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("jobs") or payload.get("records") or []
        if not isinstance(payload, list):
            raise ValueError(f"{spec.display_name} JSON import must contain a list, jobs, or records")
        return [_manual_import_item(spec, item, "manual_json", row_number=index) for index, item in enumerate(payload, start=1) if isinstance(item, dict)]
    if path.suffix.casefold() in {".txt", ".urls"}:
        return [
            _manual_import_item(spec, {"job_url": line.strip()}, "manual_url", row_number=index)
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if line.strip()
        ]
    with path.open(newline="", encoding="utf-8") as handle:
        return [_manual_import_item(spec, row, "manual_csv", row_number=index) for index, row in enumerate(csv.DictReader(handle), start=2)]


def prepare_discovery_import(spec: ManualSourceSpec, import_path: Path | str) -> list[ManualImportItem]:
    path = Path(import_path)
    if path.suffix.casefold() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("jobs") or payload.get("records") or []
        if not isinstance(payload, list):
            raise ValueError(f"{spec.display_name} discovery JSON import must contain a list, jobs, or records")
        return [_manual_import_item(spec, item, "discovery_import", row_number=index) for index, item in enumerate(payload, start=1) if isinstance(item, dict)]
    with path.open(newline="", encoding="utf-8") as handle:
        return [_manual_import_item(spec, row, "discovery_import", row_number=index) for index, row in enumerate(csv.DictReader(handle), start=2)]


def enrichment_state(payload: dict[str, Any]) -> str:
    has_jd = bool(_first_text(payload.get("description"), payload.get("requirements"), payload.get("profile")))
    has_apply = bool(_first_text(payload.get("direct_apply_url"), payload.get("apply_url"), payload.get("application_url")))
    has_posted = bool(_first_text(payload.get("posted_at"), payload.get("published_at")))
    if not has_jd:
        return "discovery_only"
    if has_apply and has_posted:
        return "fully_enriched"
    return "partially_enriched"


def _normalise_manual_payload(spec: ManualSourceSpec, raw: RawJobPayload) -> Job:
    payload = raw.raw_payload
    description = strip_html(_first_text(payload.get("description"), payload.get("requirements"), payload.get("profile"), payload.get("summary")))
    location_text = _first_text(payload.get("location"), payload.get("raw_location")) or "United Kingdom"
    observation = SourceObservation(
        source_name=raw.source_name,
        source_job_id=raw.source_job_id,
        original_url=raw.source_url,
        first_seen_at=raw.first_seen_at or raw.observed_at,
        last_seen_at=raw.last_seen_at or raw.observed_at,
        posted_at=raw.posted_at,
        raw_description=description,
        canonical_application_url=raw.canonical_application_url,
        raw_payload={**payload, "_enrichment_state": enrichment_state(payload)},
        deadline=parse_datetime(payload.get("deadline")),
    )
    return Job(
        id=f"{spec.source_name}:{raw.source_job_id}",
        title=_first_text(payload.get("title"), payload.get("name"), f"{spec.display_name} imported job"),
        company=_first_text(payload.get("company"), payload.get("company_name"), "Unknown company"),
        description=description,
        locations=[normalise_location(location_text)],
        source_observations=[observation],
        salary=_first_text(payload.get("salary")) or None,
        raw_location=location_text,
    )


def _manual_import_item(spec: ManualSourceSpec, record: dict[str, Any], ingestion_method: str, row_number: int) -> ManualImportItem:
    url = _first_text(record.get("job_url"), record.get("url"), record.get("source_url"))
    if not url:
        return ManualImportItem(row_number=row_number, url="", status="missing_required_fields", error="missing job URL")
    if not spec.is_source_url(url):
        return ManualImportItem(row_number=row_number, url=url, status="invalid_url", error=f"not a recognised {spec.display_name} job URL")
    normalised = _normalise_manual_record(record, ingestion_method)
    missing = []
    if not _first_text(record.get("title"), record.get("name")):
        missing.append("title")
    if not _first_text(record.get("company"), record.get("company_name")):
        missing.append("company")
    if missing:
        return ManualImportItem(row_number=row_number, url=url, status="missing_required_fields", record=normalised, error=f"missing {', '.join(missing)}")
    return ManualImportItem(row_number=row_number, url=url, status="imported", record=normalised)


def _normalise_manual_record(record: dict[str, Any], ingestion_method: str) -> dict[str, Any]:
    url = _first_text(record.get("job_url"), record.get("url"), record.get("source_url"))
    apply_url = _first_text(record.get("application_url"), record.get("apply_url"), record.get("direct_apply_url"), url)
    return {
        **record,
        "title": _first_text(record.get("title"), record.get("name")),
        "company": _first_text(record.get("company"), record.get("company_name")),
        "location": _first_text(record.get("location"), record.get("raw_location")),
        "description": _first_text(record.get("description"), record.get("job_description"), record.get("summary")),
        "requirements": _first_text(record.get("requirements"), record.get("requirement"), record.get("profile")),
        "posted_at": _first_text(record.get("posted_at"), record.get("published_at")),
        "deadline": _first_text(record.get("deadline"), record.get("application_deadline"), record.get("closing_date")),
        "salary": _first_text(record.get("salary")),
        "direct_apply_url": apply_url,
        "job_url": url,
        "source_job_id": _first_text(record.get("source_job_id"), record.get("job_id"), record.get("reference")),
        "_ingestion_method": ingestion_method,
    }


def _source_job_id(payload: dict[str, Any]) -> str:
    return _first_text(payload.get("source_job_id"), payload.get("job_id"), payload.get("reference"))


def _source_url(payload: dict[str, Any]) -> str:
    return _first_text(payload.get("job_url"), payload.get("url"), payload.get("source_url"))


def _application_url(payload: dict[str, Any]) -> str:
    return _first_text(payload.get("direct_apply_url"), payload.get("apply_url"), payload.get("application_url"))


def _is_relevant(payload: dict[str, Any], query: ConnectorQuery) -> bool:
    if not query.keywords:
        return True
    haystack = f"{payload.get('title', '')} {payload.get('name', '')}\n{payload.get('description', '')}".casefold()
    return any(keyword.casefold() in haystack for keyword in query.keywords)


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
