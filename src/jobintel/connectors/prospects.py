from __future__ import annotations

import html
import re
import time
from typing import Any

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, PartialFetchError, RawJobPayload, SourceHealth
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

# Prospects (prospects.ac.uk) publishes real schema.org JobPosting JSON-LD on its
# job detail pages -- structured data the site itself publishes for search-engine
# indexing (Google for Jobs), not a private/hidden API. Confirmed live during the
# Phase 2.5 source audit: plain HTTP GET, no CAPTCHA/Cloudflare-challenge
# encountered on listing or detail pages (unlike Gradcracker/Bright Network, which
# are Cloudflare-managed-challenge-protected site-wide and are NOT scraped here).
# See docs/SOURCE_COVERAGE.md for the full audit and classification.
BASE_URL = "https://www.prospects.ac.uk"

# Category slugs cover this project's target technical role families. Prospects'
# own category taxonomy doesn't split further than this -- graduate/early-career
# classification (title/description keyword matching) happens downstream in
# analysis.job_quality/role_tracks, same as every other source, and is what
# actually filters for relevance, not the category choice here.
#
# Phase 2.6 re-audit of ALL 29 of Prospects' own sector categories (live-fetched
# https://www.prospects.ac.uk/browse-graduate-jobs, the full category index) to
# check whether any category besides these two holds jobs matching this
# project's ~20 target role tracks (models.taxonomy.RoleTrack -- software,
# data, cloud/platform, devops, AI/ML, telecoms, network, embedded, IoT,
# motorsport, etc). Prospects has no dedicated category for AI/ML, telecoms,
# embedded, or motorsport specifically -- roles in those areas are tagged
# under IT or Engineering-and-manufacturing on Prospects' own site, which are
# both already configured here. Spot-checked three plausible adjacent
# categories live (information-research-and-analysis-166,
# science-and-pharmaceuticals-183, business-consulting-and-management-559):
# all three were dominated by unrelated finance/food-science/general-business
# graduate schemes with no genuine target-role-family content found. No
# category was added as a result -- this is an evidence-based conclusion, not
# an unexamined default.
DEFAULT_CATEGORY_SLUGS: tuple[str, ...] = (
    "information-technology-69",
    "engineering-and-manufacturing-172",
)

JOB_LINK_PATTERN = re.compile(r'href="(/graduate-jobs/[a-zA-Z0-9-]+-(\d+))"')
REQUEST_DELAY_SECONDS = 0.2


class ProspectsConnector(JobSourceConnector):
    """Live automated discovery for Prospects graduate jobs.

    Listing pages (`/browse-graduate-jobs/<category>/all-locations`) are plain
    server-rendered HTML with real job links. Phase 2.6 re-investigated
    pagination live: requesting `?page=2` on a listing page returns byte-for-
    byte the same job links as page 1 (confirmed on information-technology-69,
    42 links both times), there is no "N of M results" text, no `rel="next"`
    link, and no reference to an underlying listings JSON API anywhere in the
    page or its linked scripts. The evidence now points to these being the
    full, un-paginated listing for each category (not a truncated page 1 of an
    unknown total) -- a materially stronger claim than Phase 2.5's "pagination
    completeness is unconfirmed", though still not a guarantee Prospects could
    never render more via a mechanism this plain-HTTP-GET approach can't see
    (e.g. a client-side-only fetch that never appears in the initial HTML).
    Detail pages redirect to an employer-scoped URL and embed a full schema.org
    JobPosting JSON-LD block (title, company, description, datePosted,
    validThrough/deadline).

    Jobs cross-listed under more than one configured category are deduplicated
    by numeric job id before detail pages are fetched (see `fetch_jobs`), so
    each job is fetched and emitted exactly once regardless of how many
    configured categories link to it.
    """

    source_name = "prospects"
    # PARTIAL_COVERAGE: only the configured categories are fetched, the
    # listing's completeness is unproven, and daily refresh keyword-filters
    # it -- so a Prospects job missing from a later fetch is NEVER evidence
    # the vacancy closed. This connector deliberately does not override
    # `fetch_snapshot`, so it reports no complete scope and cannot close
    # anything (see connectors.base.SourceSnapshot).
    supports_closure_inference = False

    def __init__(self, category_slugs: tuple[str, ...] = DEFAULT_CATEGORY_SLUGS) -> None:
        self.category_slugs = category_slugs

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        observed_at = now_utc()
        failures: dict[str, str] = {}
        job_paths: dict[str, str] = {}  # canonical numeric id -> path
        for slug in self.category_slugs:
            try:
                listing_html = fetch_text(f"{BASE_URL}/browse-graduate-jobs/{slug}/all-locations")
            except Exception as exc:
                failures[f"listing:{slug}"] = f"{type(exc).__name__}: {exc}"
                continue
            for path, job_id in JOB_LINK_PATTERN.findall(listing_html):
                job_paths[job_id] = path

        jobs: list[RawJobPayload] = []
        for job_id, path in job_paths.items():
            if len(jobs) >= query.limit:
                break
            try:
                time.sleep(REQUEST_DELAY_SECONDS)
                detail_html = fetch_text(f"{BASE_URL}{path}")
            except Exception as exc:
                failures[f"detail:{job_id}"] = f"{type(exc).__name__}: {exc}"
                continue
            posting = extract_json_ld(detail_html, "JobPosting")
            if posting is None:
                failures[f"detail:{job_id}"] = "no JobPosting JSON-LD found on detail page (layout may have changed)"
                continue
            if not _is_relevant(posting, query):
                continue
            jobs.append(
                RawJobPayload(
                    source_name=self.source_name,
                    source_job_id=job_id,
                    source_url=f"{BASE_URL}{path}",
                    canonical_application_url=_first_text(posting.get("url"), f"{BASE_URL}{path}"),
                    raw_payload={**posting, "_detail_path": path},
                    observed_at=observed_at,
                    posted_at=parse_datetime(posting.get("datePosted")),
                )
            )

        if failures:
            raise PartialFetchError(jobs, failures)
        return jobs

    def health_check(self) -> SourceHealth:
        return SourceHealth(self.source_name, True, f"{len(self.category_slugs)} category slug(s) configured (live discovery, no credentials required)", now_utc())

    def normalise(self, raw: RawJobPayload) -> Job:
        posting = raw.raw_payload
        description = strip_html(_first_text(posting.get("description")))
        location_text = json_ld_location_text(posting.get("jobLocation")) or "United Kingdom"
        observation = SourceObservation(
            source_name=raw.source_name,
            source_job_id=raw.source_job_id,
            original_url=raw.source_url,
            first_seen_at=raw.first_seen_at or raw.observed_at,
            last_seen_at=raw.last_seen_at or raw.observed_at,
            posted_at=raw.posted_at,
            raw_description=description,
            canonical_application_url=raw.canonical_application_url,
            raw_payload=posting,
            deadline=parse_datetime(posting.get("validThrough")),
        )
        return Job(
            id=f"prospects:{raw.source_job_id}",
            title=html.unescape(_first_text(posting.get("title"))).strip(),
            company=html.unescape(json_ld_organisation_name(posting.get("hiringOrganization"))).strip(),
            description=description,
            locations=[normalise_location(location_text)],
            source_observations=[observation],
            raw_location=location_text,
        )


def _is_relevant(posting: dict[str, Any], query: ConnectorQuery) -> bool:
    if not query.keywords:
        return True
    haystack = f"{posting.get('title', '')}\n{strip_html(_first_text(posting.get('description')))}".casefold()
    return any(keyword.casefold() in haystack for keyword in query.keywords)


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
