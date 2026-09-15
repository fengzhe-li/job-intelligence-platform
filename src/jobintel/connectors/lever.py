from __future__ import annotations

from typing import Any

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload, SourceHealth
from jobintel.connectors.utils import build_url, fetch_json, normalise_location, now_utc, parse_datetime, strip_html
from jobintel.models.job import Job, SourceObservation


class LeverConnector(JobSourceConnector):
    source_name = "lever"

    def __init__(self, sites: tuple[str, ...], eu: bool = False) -> None:
        self.sites = sites
        self.base_url = "https://jobs.eu.lever.co" if eu else "https://api.lever.co"

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        observed_at = now_utc()
        jobs: list[RawJobPayload] = []
        for site in self.sites:
            url = build_url(f"{self.base_url}/v0/postings/{site}", {"mode": "json", "limit": query.limit})
            payload = fetch_json(url)
            for item in payload:
                if not isinstance(item, dict):
                    continue
                if not _is_relevant(item, query):
                    continue
                source_id = str(item.get("id") or item.get("hostedUrl") or item.get("applyUrl") or item.get("text") or "unknown")
                source_url = item.get("hostedUrl") or item.get("applyUrl") or f"https://jobs.lever.co/{site}/{source_id}"
                jobs.append(
                    RawJobPayload(
                        source_name=self.source_name,
                        source_job_id=source_id,
                        source_url=source_url,
                        canonical_application_url=item.get("applyUrl") or source_url,
                        raw_payload={**item, "_site": site},
                        observed_at=observed_at,
                        posted_at=parse_datetime(item.get("createdAt")),
                    )
                )
        return jobs[: query.limit]

    def health_check(self) -> SourceHealth:
        if not self.sites:
            return SourceHealth(self.source_name, False, "No Lever sites configured", now_utc())
        return SourceHealth(self.source_name, True, f"{len(self.sites)} site(s) configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        payload = raw.raw_payload
        description = _description(payload)
        categories = payload.get("categories") if isinstance(payload.get("categories"), dict) else {}
        location_text = _first_text(categories.get("location"), payload.get("workplaceType"), "United Kingdom")
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
            id=f"lever:{raw.source_job_id}",
            title=_first_text(payload.get("text"), ""),
            company=_first_text(payload.get("_site"), ""),
            description=description,
            locations=[normalise_location(location_text)],
            source_observations=[observation],
            raw_location=location_text,
        )


def _description(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.extend(_text_fragments(payload.get("descriptionPlain")))
    parts.extend(_text_fragments(payload.get("description")))
    parts.extend(_text_fragments(payload.get("additionalPlain")))
    parts.extend(_text_fragments(payload.get("additional")))
    parts.extend(_text_fragments(payload.get("lists")))
    return strip_html("\n".join(part for part in parts if part))


def _is_relevant(payload: dict[str, Any], query: ConnectorQuery) -> bool:
    if not query.keywords:
        return True
    haystack = f"{payload.get('text', '')}\n{_description(payload)}".casefold()
    return any(keyword.casefold() in haystack for keyword in query.keywords)


def _first_text(*values: Any) -> str:
    for value in values:
        fragments = _text_fragments(value)
        if fragments:
            return fragments[0]
    return ""


def _text_fragments(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        fragments: list[str] = []
        for key in ("text", "content", "description", "descriptionPlain", "additional", "additionalPlain", "value"):
            fragments.extend(_text_fragments(value.get(key)))
        return fragments
    if isinstance(value, list | tuple):
        fragments = []
        for item in value:
            fragments.extend(_text_fragments(item))
        return fragments
    return []
