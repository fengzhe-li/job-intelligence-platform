from __future__ import annotations

from datetime import datetime
from typing import Any

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload, SourceHealth
from jobintel.connectors.utils import fetch_json, normalise_location, now_utc, parse_datetime, strip_html
from jobintel.models.job import Job, SourceObservation


class GreenhouseConnector(JobSourceConnector):
    source_name = "greenhouse"

    def __init__(self, board_tokens: tuple[str, ...]) -> None:
        self.board_tokens = board_tokens

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        observed_at = now_utc()
        jobs: list[RawJobPayload] = []
        for token in self.board_tokens:
            url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
            payload = fetch_json(url)
            for item in payload.get("jobs", []):
                if not _is_relevant(item, query):
                    continue
                source_url = item.get("absolute_url") or f"https://boards.greenhouse.io/{token}/jobs/{item.get('id')}"
                jobs.append(
                    RawJobPayload(
                        source_name=self.source_name,
                        source_job_id=str(item["id"]),
                        source_url=source_url,
                        canonical_application_url=source_url,
                        raw_payload={**item, "_board_token": token},
                        observed_at=observed_at,
                        posted_at=parse_datetime(item.get("updated_at")),
                    )
                )
        return jobs[: query.limit]

    def health_check(self) -> SourceHealth:
        if not self.board_tokens:
            return SourceHealth(self.source_name, False, "No Greenhouse board tokens configured", now_utc())
        return SourceHealth(self.source_name, True, f"{len(self.board_tokens)} board token(s) configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        payload = raw.raw_payload
        description = strip_html(payload.get("content"))
        offices = payload.get("offices") or []
        locations = [normalise_location(office.get("location") or office.get("name")) for office in offices]
        if not locations:
            locations = [normalise_location(payload.get("location", {}).get("name") if isinstance(payload.get("location"), dict) else None)]
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
            id=f"greenhouse:{raw.source_job_id}",
            title=payload.get("title", ""),
            company=payload.get("_company") or payload.get("_board_token", ""),
            description=description,
            locations=locations,
            source_observations=[observation],
            raw_location=", ".join(location.raw or "" for location in locations if location.raw) or None,
        )


def _is_relevant(payload: dict[str, Any], query: ConnectorQuery) -> bool:
    if not query.keywords:
        return True
    haystack = f"{payload.get('title', '')}\n{strip_html(payload.get('content'))}".casefold()
    return any(keyword.casefold() in haystack for keyword in query.keywords)

