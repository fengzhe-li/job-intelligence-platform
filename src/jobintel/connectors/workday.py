from __future__ import annotations

import time
from typing import Any

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, RawJobPayload, ScopeCollector, SourceHealth, SourceSnapshot
from jobintel.connectors.utils import fetch_json, normalise_location, now_utc, parse_datetime, strip_html
from jobintel.models.job import Job, SourceObservation

# Workday-hosted career sites' own frontend JavaScript calls this same "CXS"
# (Candidate Experience) endpoint to render job listings and details on the
# public careers page -- it is not a private/internal API, and no
# authentication is required for a public job board. Confirmed live (Phase
# 2.7) against Darktrace's real Workday tenant: POST .../wday/cxs/{tenant}/
# {site}/jobs returns real, current postings with stable requisition IDs; GET
# .../wday/cxs/{tenant}/{site}/job{externalPath} returns the full JD.
WORKDAY_PAGE_SIZE = 20
# Safety cap, not a Workday-imposed limit -- 25 pages * 20 = up to 500
# postings/company/refresh, consistent with the caps other connectors here use
# (SmartRecruiters: 10 pages; Adzuna: 20 pages).
WORKDAY_MAX_PAGES = 25
WORKDAY_DETAIL_DELAY_SECONDS = 0.15


class WorkdayConnector(JobSourceConnector):
    source_name = "workday"

    def __init__(self, tenant_site_tokens: tuple[str, ...]) -> None:
        # Each token is "{tenant}.{wdN}/{site}", e.g. "darktrace.wd3/DarktaceExternal"
        # -- both the tenant AND its wdN shard number AND its site slug are
        # required to build a working URL; a bare tenant name is not enough.
        self.tenant_site_tokens = tenant_site_tokens

    supports_closure_inference = True

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        return self.fetch_snapshot(query).payloads_or_raise()

    def fetch_snapshot(self, query: ConnectorQuery) -> SourceSnapshot:
        # One tenant/site token = one closure scope. It is complete only if
        # listing pagination reached the real end AND every listed posting's
        # detail was fetched and kept -- stopping at the per-token limit, a
        # failed detail fetch, or an unidentifiable posting all make it
        # incomplete, since the missing postings may still be open.
        observed_at = now_utc()
        collector = ScopeCollector(query)
        for token in self.tenant_site_tokens:
            try:
                host, tenant, site = parse_workday_token(token)
            except ValueError as exc:
                collector.fail(token, token, f"InvalidToken: {exc}")
                continue
            try:
                postings, exhausted = _fetch_all_postings(host, tenant, site)
            except Exception as exc:
                collector.fail(token, token, f"{type(exc).__name__}: {exc}")
                continue
            listed: list[RawJobPayload] = []
            filtered_out = 0
            for index, posting in enumerate(postings):
                if len(listed) >= query.limit:
                    collector.mark_incomplete(token, f"stopped at limit {query.limit}; {len(postings) - index} listed posting(s) not fetched")
                    break
                external_path = posting.get("externalPath")
                if not external_path:
                    collector.mark_incomplete(token, "a listed posting had no externalPath and could not be fetched")
                    continue
                try:
                    time.sleep(WORKDAY_DETAIL_DELAY_SECONDS)
                    detail = _fetch_detail(host, tenant, site, external_path)
                except Exception as exc:
                    collector.fail(token, f"{token}:{external_path}", f"{type(exc).__name__}: {exc}")
                    continue
                info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
                if not isinstance(info, dict):
                    collector.fail(token, f"{token}:{external_path}", "missing jobPostingInfo in detail response (unexpected shape)")
                    continue
                if not _is_relevant(info, query):
                    filtered_out += 1
                    continue
                source_id = _first_text(info.get("jobReqId")) or f"{tenant}-{site}-{external_path}"
                source_url = _first_text(info.get("externalUrl")) or f"https://{host}/{site}{external_path}"
                listed.append(
                    RawJobPayload(
                        source_name=self.source_name,
                        source_job_id=f"{tenant}:{site}:{source_id}",
                        source_url=source_url,
                        canonical_application_url=source_url,
                        raw_payload={**info, "_tenant": tenant, "_site": site, "_host": host},
                        observed_at=observed_at,
                        posted_at=parse_datetime(info.get("startDate")),
                    )
                )
            collector.add_scope(token, listed, filtered_out=filtered_out, exhausted=exhausted)
        return collector.snapshot()

    def closure_scope(self, raw_payload: dict[str, Any]) -> str | None:
        scope = super().closure_scope(raw_payload)
        if scope:
            return scope
        host, site = raw_payload.get("_host"), raw_payload.get("_site")
        if isinstance(host, str) and host.endswith(".myworkdayjobs.com") and isinstance(site, str) and site:
            return f"{host.removesuffix('.myworkdayjobs.com')}/{site}"
        return None

    def health_check(self) -> SourceHealth:
        if not self.tenant_site_tokens:
            return SourceHealth(self.source_name, False, "No Workday tenant/site tokens configured", now_utc())
        return SourceHealth(self.source_name, True, f"{len(self.tenant_site_tokens)} Workday tenant/site pair(s) configured", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        info = raw.raw_payload
        description = strip_html(_first_text(info.get("jobDescription")))
        location_text = _location_text(info)
        observation = SourceObservation(
            source_name=raw.source_name,
            source_job_id=raw.source_job_id,
            original_url=raw.source_url,
            first_seen_at=raw.first_seen_at or raw.observed_at,
            last_seen_at=raw.last_seen_at or raw.observed_at,
            posted_at=raw.posted_at,
            raw_description=description,
            canonical_application_url=raw.canonical_application_url,
            raw_payload=info,
        )
        return Job(
            id=f"workday:{raw.source_job_id}",
            title=_first_text(info.get("title")),
            company=_first_text(info.get("_tenant")),
            description=description,
            locations=[normalise_location(location_text)],
            source_observations=[observation],
            raw_location=location_text,
        )


def parse_workday_token(token: str) -> tuple[str, str, str]:
    """"{tenant}.{wdN}/{site}" -> (host, tenant, site). Shared with
    ats_discovery.py so discovery and the live connector agree on the exact
    same token format."""
    if "/" not in token:
        raise ValueError(f"expected '<tenant>.<wdN>/<site>', got {token!r}")
    host_part, site = token.split("/", 1)
    if "." not in host_part or not site:
        raise ValueError(f"expected '<tenant>.<wdN>/<site>', got {token!r}")
    tenant = host_part.split(".", 1)[0]
    if not tenant:
        raise ValueError(f"expected '<tenant>.<wdN>/<site>', got {token!r}")
    return f"{host_part}.myworkdayjobs.com", tenant, site


def _fetch_all_postings(host: str, tenant: str, site: str) -> tuple[list[dict[str, Any]], bool]:
    """Returns (postings, exhausted); exhausted=False means WORKDAY_MAX_PAGES
    stopped pagination while full pages were still coming back."""
    postings: list[dict[str, Any]] = []
    offset = 0
    for _ in range(WORKDAY_MAX_PAGES):
        payload = fetch_json(
            f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
            data={"appliedFacets": {}, "limit": WORKDAY_PAGE_SIZE, "offset": offset, "searchText": ""},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("jobPostings", []), list):
            raise ValueError("unexpected response shape: `jobPostings` is not a list")
        page = payload.get("jobPostings") or []
        if not page:
            return postings, True
        postings.extend(page)
        # NOTE (Phase 2.7, live-confirmed against Darktrace): Workday's own
        # `total` field was observed to report 0 on a second consecutive
        # request even though the request still returned 20 real postings --
        # unreliable past the first page. Stopping is based on page size only,
        # never on `total`.
        offset += WORKDAY_PAGE_SIZE
        if len(page) < WORKDAY_PAGE_SIZE:
            return postings, True
    return postings, False


def _fetch_detail(host: str, tenant: str, site: str, external_path: str) -> dict[str, Any]:
    # `externalPath` from the listing response already starts with "/job/..."
    # -- do not prepend another "/job" segment here (that produced a
    # "/job/job/..." URL and a 406 from every detail fetch, caught live
    # against Darktrace's real tenant during Phase 2.7).
    return fetch_json(f"https://{host}/wday/cxs/{tenant}/{site}{external_path}")


def _location_text(info: dict[str, Any]) -> str:
    location = _first_text(info.get("location"))
    if location:
        return location
    req_location = info.get("jobRequisitionLocation")
    if isinstance(req_location, dict):
        descriptor = _first_text(req_location.get("descriptor"))
        country = req_location.get("country")
        country_name = _first_text(country.get("descriptor")) if isinstance(country, dict) else ""
        parts = [part for part in (descriptor, country_name) if part]
        if parts:
            return ", ".join(parts)
    return "United Kingdom"


def _is_relevant(info: dict[str, Any], query: ConnectorQuery) -> bool:
    if not query.keywords:
        return True
    haystack = f"{info.get('title', '')}\n{strip_html(_first_text(info.get('jobDescription')))}".casefold()
    return any(keyword.casefold() in haystack for keyword in query.keywords)


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
