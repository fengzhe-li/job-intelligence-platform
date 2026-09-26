from __future__ import annotations

from typing import Any

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload, ScopeCollector, SourceHealth, SourceSnapshot
from jobintel.connectors.utils import build_url, fetch_json, normalise_location, now_utc, parse_datetime, strip_html
from jobintel.models.job import Job, SourceObservation

# SmartRecruiters' postings API is genuinely paginated (limit/offset, with
# `totalFound` in the response) -- previously this connector only ever fetched
# offset=0, silently missing any company posting more than one page of jobs.
MAX_PAGES_PER_COMPANY = 10


class SmartRecruitersConnector(JobSourceConnector):
    source_name = "smartrecruiters"

    def __init__(self, company_identifiers: tuple[str, ...]) -> None:
        self.company_identifiers = company_identifiers

    supports_closure_inference = True

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        return self.fetch_snapshot(query).payloads_or_raise()

    def fetch_snapshot(self, query: ConnectorQuery) -> SourceSnapshot:
        # Scope = company + the country filter sent to the API, so a UK-only
        # listing can only ever close jobs previously seen in a UK-only
        # listing. Observations stored before scopes existed carry no country
        # and are therefore never closed by absence (fail-closed).
        observed_at = now_utc()
        collector = ScopeCollector(query)
        page_size = min(query.limit, 100) or 100
        country = _country_filter(query.location)
        for company_identifier in self.company_identifiers:
            scope = f"{company_identifier}@{country or 'all'}"
            try:
                items, exhausted = _fetch_all_postings(company_identifier, page_size, country)
            except Exception as exc:
                collector.fail(scope, company_identifier, f"{type(exc).__name__}: {exc}")
                continue
            listed: list[RawJobPayload] = []
            filtered_out = 0
            for item in items:
                detail = _detail_payload(company_identifier, item)
                if not _is_relevant(detail, query):
                    filtered_out += 1
                    continue
                source_id = str(detail.get("id") or item.get("id") or item.get("uuid") or item.get("ref") or item.get("name") or "unknown")
                source_url = _source_url(detail, item, company_identifier, source_id)
                listed.append(
                    RawJobPayload(
                        source_name=self.source_name,
                        source_job_id=source_id,
                        source_url=source_url,
                        canonical_application_url=_first_text(detail.get("applyUrl"), detail.get("jobAdUrl"), source_url),
                        raw_payload={**detail, "_company_identifier": company_identifier, "_list_payload": item},
                        observed_at=observed_at,
                        posted_at=parse_datetime(detail.get("releasedDate") or detail.get("createdOn") or detail.get("updatedOn")),
                    )
                )
            collector.add_scope(scope, listed, filtered_out=filtered_out, exhausted=exhausted)
        return collector.snapshot()

    def health_check(self) -> SourceHealth:
        if not self.company_identifiers:
            return SourceHealth(self.source_name, False, "No SmartRecruiters company identifiers configured", now_utc())
        return SourceHealth(self.source_name, True, f"{len(self.company_identifiers)} company identifier(s) configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        payload = raw.raw_payload
        description = _description(payload)
        location_text = _location_text(payload.get("location"))
        company = payload.get("company") if isinstance(payload.get("company"), dict) else {}
        observation = SourceObservation(
            source_name=raw.source_name,
            source_job_id=raw.source_job_id,
            original_url=raw.source_url,
            first_seen_at=raw.first_seen_at or raw.observed_at,
            last_seen_at=raw.last_seen_at or raw.observed_at,
            posted_at=raw.posted_at,
            raw_description=description,
            canonical_application_url=raw.canonical_application_url,
            raw_payload=payload,
        )
        return Job(
            id=f"smartrecruiters:{raw.source_job_id}",
            title=_first_text(payload.get("name"), payload.get("title"), ""),
            company=_first_text(company.get("name"), payload.get("_company_identifier"), ""),
            description=description,
            locations=[normalise_location(location_text)],
            source_observations=[observation],
            raw_location=location_text,
        )


def _jobs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
        return [item for item in payload["content"] if isinstance(item, dict)]
    return []


def _country_filter(location: str) -> str | None:
    return "gb" if "kingdom" in location.casefold() or location.casefold() == "uk" else None


def _fetch_all_postings(company_identifier: str, page_size: int, country: str | None) -> tuple[list[dict[str, Any]], bool]:
    """Returns (postings, exhausted). `exhausted` is False when the page cap
    stopped pagination before the listing's real end -- such a listing is
    not a complete snapshot."""
    items: list[dict[str, Any]] = []
    offset = 0
    for _ in range(MAX_PAGES_PER_COMPANY):
        url = build_url(
            f"https://api.smartrecruiters.com/v1/companies/{company_identifier}/postings",
            {"limit": page_size, "offset": offset, "country": country},
        )
        payload = fetch_json(url)
        if not (isinstance(payload, dict) and isinstance(payload.get("content"), list)):
            raise ValueError("unexpected response shape: no `content` list")
        page_items = _jobs(payload)
        items.extend(page_items)
        total_found = payload.get("totalFound")
        offset += page_size
        if len(page_items) < page_size:
            return items, True
        if isinstance(total_found, int) and offset >= total_found:
            return items, True
    return items, False


def _detail_payload(company_identifier: str, item: dict[str, Any]) -> dict[str, Any]:
    source_id = item.get("id") or item.get("uuid")
    if not source_id:
        return item
    try:
        detail = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{company_identifier}/postings/{source_id}")
    except Exception:
        return item
    if isinstance(detail, dict):
        return {**item, **detail}
    return item


def _source_url(payload: dict[str, Any], item: dict[str, Any], company_identifier: str, source_id: str) -> str:
    public_url = _first_text(payload.get("jobAdUrl"), item.get("jobAdUrl"))
    if public_url:
        return public_url
    ref = payload.get("ref") if isinstance(payload.get("ref"), str) else item.get("ref")
    if ref:
        return ref
    return f"https://jobs.smartrecruiters.com/{company_identifier}/{source_id}"


def _description(payload: dict[str, Any]) -> str:
    job_ad = payload.get("jobAd") if isinstance(payload.get("jobAd"), dict) else {}
    sections = job_ad.get("sections") if isinstance(job_ad.get("sections"), dict) else {}
    parts = [
        payload.get("description"),
        job_ad.get("text"),
        sections.get("companyDescription"),
        sections.get("jobDescription"),
        sections.get("qualifications"),
        sections.get("additionalInformation"),
    ]
    return strip_html("\n".join(str(part) for part in parts if part))


def _location_text(location: Any) -> str:
    if isinstance(location, str):
        return location
    if not isinstance(location, dict):
        return "United Kingdom"
    parts = [location.get("city"), location.get("region"), location.get("country")]
    value = ", ".join(str(part) for part in parts if part) or "United Kingdom"
    return f"{value} Remote" if location.get("remote") is True else value


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
