from __future__ import annotations

import urllib.parse
from typing import Any

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload, SourceHealth
from jobintel.connectors.utils import fetch_json, normalise_location, now_utc, parse_datetime, strip_html
from jobintel.models.job import Job, SourceObservation


class AshbyConnector(JobSourceConnector):
    source_name = "ashby"

    def __init__(self, board_names: tuple[str, ...]) -> None:
        self.board_names = board_names

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        observed_at = now_utc()
        jobs: list[RawJobPayload] = []
        for board_name in self.board_names:
            encoded = urllib.parse.quote(board_name, safe="")
            url = f"https://api.ashbyhq.com/posting-api/job-board/{encoded}?includeCompensation=true"
            payload = fetch_json(url)
            for item in _jobs(payload):
                if not _is_relevant(item, query):
                    continue
                source_id = str(item.get("id") or item.get("jobId") or item.get("title") or "unknown")
                source_url = _first_text(item.get("jobUrl"), item.get("url"), f"https://jobs.ashbyhq.com/{encoded}/{source_id}")
                apply_url = _first_text(item.get("applicationUrl"), item.get("applyUrl"), source_url)
                jobs.append(
                    RawJobPayload(
                        source_name=self.source_name,
                        source_job_id=source_id,
                        source_url=source_url,
                        canonical_application_url=apply_url,
                        raw_payload={**item, "_board_name": board_name},
                        observed_at=observed_at,
                        posted_at=parse_datetime(item.get("publishedAt") or item.get("publishedDate") or item.get("createdAt") or item.get("updatedAt")),
                    )
                )
        return jobs[: query.limit]

    def health_check(self) -> SourceHealth:
        if not self.board_names:
            return SourceHealth(self.source_name, False, "No Ashby board names configured", now_utc())
        return SourceHealth(self.source_name, True, f"{len(self.board_names)} board name(s) configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        payload = raw.raw_payload
        description = _description(payload)
        locations = [_with_workplace_type(_location_text(payload.get("location")), payload)]
        locations.extend(_location_text(item) for item in payload.get("secondaryLocations") or [])
        normalised_locations = [normalise_location(item) for item in locations if item]
        if not normalised_locations:
            normalised_locations = [normalise_location("United Kingdom")]
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
            id=f"ashby:{raw.source_job_id}",
            title=_first_text(payload.get("title"), ""),
            company=_first_text(payload.get("organizationName"), payload.get("_board_name"), ""),
            description=description,
            locations=normalised_locations,
            source_observations=[observation],
            raw_location=", ".join(location.raw or "" for location in normalised_locations if location.raw) or None,
        )


def _jobs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        return [item for item in payload["jobs"] if isinstance(item, dict)]
    return []


def _description(payload: dict[str, Any]) -> str:
    parts = [
        payload.get("descriptionPlain"),
        payload.get("description"),
        payload.get("descriptionHtml"),
        payload.get("shortDescription"),
        payload.get("summary"),
    ]
    return strip_html("\n".join(str(part) for part in parts if part))


def _location_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _first_text(value.get("location"), value.get("name"), value.get("city"), value.get("country"), "United Kingdom")
    return ""


def _with_workplace_type(location: str, payload: dict[str, Any]) -> str:
    workplace_type = _first_text(payload.get("workplaceType"), "Remote" if payload.get("isRemote") is True else "")
    if not workplace_type:
        return location
    return f"{location} {workplace_type}".strip()


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
