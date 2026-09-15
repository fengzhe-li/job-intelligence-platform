from __future__ import annotations

from typing import Any

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload, SourceHealth
from jobintel.connectors.utils import build_url, fetch_json, normalise_location, now_utc, parse_datetime, salary_text, strip_html
from jobintel.models.job import Job, SourceObservation


class AdzunaConnector(JobSourceConnector):
    source_name = "adzuna"

    def __init__(self, app_id: str | None, app_key: str | None) -> None:
        self.app_id = app_id
        self.app_key = app_key

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        if not self.app_id or not self.app_key:
            raise RuntimeError("Adzuna requires ADZUNA_APP_ID and ADZUNA_APP_KEY")
        observed_at = now_utc()
        what = " OR ".join(query.keywords) if query.keywords else "software engineer"
        url = build_url(
            "https://api.adzuna.com/v1/api/jobs/gb/search/1",
            {
                "app_id": self.app_id,
                "app_key": self.app_key,
                "results_per_page": query.limit,
                "what": what,
                "where": query.location,
                "content-type": "application/json",
            },
        )
        payload = fetch_json(url)
        jobs: list[RawJobPayload] = []
        for item in payload.get("results", []):
            source_url = item.get("redirect_url") or f"https://www.adzuna.co.uk/jobs/details/{item.get('id')}"
            jobs.append(
                RawJobPayload(
                    source_name=self.source_name,
                    source_job_id=str(item["id"]),
                    source_url=source_url,
                    canonical_application_url=source_url,
                    raw_payload=item,
                    observed_at=observed_at,
                    posted_at=parse_datetime(item.get("created")),
                )
            )
        return jobs

    def health_check(self) -> SourceHealth:
        if not self.app_id or not self.app_key:
            return SourceHealth(self.source_name, False, "Missing ADZUNA_APP_ID or ADZUNA_APP_KEY", now_utc())
        return SourceHealth(self.source_name, True, "Adzuna credentials configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        payload = raw.raw_payload
        description = strip_html(payload.get("description"))
        location_text = _location_text(payload.get("location"))
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
            id=f"adzuna:{raw.source_job_id}",
            title=payload.get("title", ""),
            company=(payload.get("company") or {}).get("display_name", ""),
            description=description,
            locations=[normalise_location(location_text)],
            source_observations=[observation],
            salary=salary_text(payload.get("salary_min"), payload.get("salary_max"), payload.get("salary_currency")),
            raw_location=location_text,
        )


def _location_text(location: dict[str, Any] | None) -> str:
    if not location:
        return "United Kingdom"
    if location.get("display_name"):
        return location["display_name"]
    area = location.get("area") or []
    return ", ".join(area) if area else "United Kingdom"

