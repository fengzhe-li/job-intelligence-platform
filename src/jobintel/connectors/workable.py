from __future__ import annotations

from typing import Any

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload, SourceHealth
from jobintel.connectors.utils import fetch_json, normalise_location, now_utc, parse_datetime, strip_html
from jobintel.models.job import Job, SourceObservation


class WorkableConnector(JobSourceConnector):
    source_name = "workable"

    def __init__(self, account_subdomains: tuple[str, ...]) -> None:
        self.account_subdomains = account_subdomains

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        observed_at = now_utc()
        jobs: list[RawJobPayload] = []
        for account in self.account_subdomains:
            url = f"https://www.workable.com/api/accounts/{account}?details=true"
            payload = fetch_json(url)
            for item in _jobs(payload):
                if not _is_relevant(item, query):
                    continue
                source_id = str(item.get("id") or item.get("shortcode") or item.get("code") or item.get("url") or item.get("title") or "unknown")
                source_url = _first_text(item.get("url"), item.get("shortlink"), item.get("application_url"), f"https://apply.workable.com/{account}/j/{source_id}/")
                jobs.append(
                    RawJobPayload(
                        source_name=self.source_name,
                        source_job_id=source_id,
                        source_url=source_url,
                        canonical_application_url=_first_text(item.get("application_url"), item.get("url"), item.get("shortlink"), source_url),
                        raw_payload={**item, "_account_subdomain": account, "_account_payload": _account_metadata(payload)},
                        observed_at=observed_at,
                        posted_at=parse_datetime(item.get("published_on") or item.get("created_at") or item.get("updated_at")),
                    )
                )
        return jobs[: query.limit]

    def health_check(self) -> SourceHealth:
        if not self.account_subdomains:
            return SourceHealth(self.source_name, False, "No Workable account subdomains configured", now_utc())
        return SourceHealth(self.source_name, True, f"{len(self.account_subdomains)} account subdomain(s) configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        payload = raw.raw_payload
        description = _description(payload)
        location_text = _location_text(payload)
        account_payload = payload.get("_account_payload") if isinstance(payload.get("_account_payload"), dict) else {}
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
            id=f"workable:{raw.source_job_id}",
            title=_first_text(payload.get("title"), payload.get("full_title"), ""),
            company=_first_text(payload.get("company_name"), account_payload.get("name"), payload.get("_account_subdomain"), ""),
            description=description,
            locations=[normalise_location(location_text)],
            source_observations=[observation],
            raw_location=location_text,
        )


def _jobs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("jobs", "results", "positions"):
        if isinstance(payload.get(key), list):
            return [item for item in payload[key] if isinstance(item, dict)]
    return []


def _account_metadata(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {key: payload.get(key) for key in ("name", "subdomain", "website") if payload.get(key)}


def _description(payload: dict[str, Any]) -> str:
    parts = [
        payload.get("description"),
        payload.get("requirements"),
        payload.get("benefits"),
        payload.get("employment_type"),
        payload.get("department"),
    ]
    return strip_html("\n".join(str(part) for part in parts if part))


def _location_text(payload: dict[str, Any]) -> str:
    workplace_type = _first_text(payload.get("workplace_type"), payload.get("workplaceType"))
    if payload.get("location"):
        location = payload["location"]
        if isinstance(location, str):
            return _with_suffix(location, workplace_type)
        if isinstance(location, dict):
            return _with_suffix(_first_text(location.get("display_name"), location.get("full_name"), _join_location_parts(location)), workplace_type)
    locations = payload.get("locations")
    if isinstance(locations, list) and locations:
        first = locations[0]
        if isinstance(first, str):
            return _with_suffix(first, workplace_type)
        if isinstance(first, dict):
            return _with_suffix(_first_text(first.get("display_name"), first.get("full_name"), _join_location_parts(first)), workplace_type)
    return _with_suffix("United Kingdom", workplace_type)


def _join_location_parts(location: dict[str, Any]) -> str:
    parts = [location.get("city"), location.get("region"), location.get("country_name") or location.get("country")]
    return ", ".join(str(part) for part in parts if part)


def _with_suffix(value: str, suffix: str) -> str:
    return f"{value} {suffix}".strip() if suffix else value


def _is_relevant(payload: dict[str, Any], query: ConnectorQuery) -> bool:
    if not query.keywords:
        return True
    haystack = f"{payload.get('title', '')}\n{_description(payload)}".casefold()
    return any(keyword.casefold() in haystack for keyword in query.keywords)


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
