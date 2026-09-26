from __future__ import annotations

import urllib.parse
from typing import Any

from jobintel.connectors.base import ConnectorQuery, JobSourceConnector, PartialFetchError, RawJobPayload, SourceHealth
from jobintel.connectors.utils import build_url, fetch_json, normalise_location, now_utc, parse_datetime, salary_text, strip_html
from jobintel.models.job import Job, SourceObservation

# Adzuna's documented practical cap on results_per_page. Requesting more is not
# reliably honoured by the API, so page size is capped here regardless of
# query.limit -- more results are obtained by fetching more pages, not a bigger
# page.
ADZUNA_RESULTS_PER_PAGE = 50

# Safety cap on TOTAL page requests per refresh, across every keyword search,
# independent of query.limit, so a very large limit or a long keyword list
# can't turn one refresh into an unbounded number of requests against a
# rate-limited external API.
ADZUNA_MAX_PAGES = 20

# The /gb/ endpoint is already UK-scoped, and Adzuna does not recognise
# "United Kingdom" as a `where` value -- live-tested 2026-09-22, sending it
# returned count=0 for a query that otherwise returned 1,247 results. Any of
# these means "no narrower location", so `where` is omitted entirely.
_UK_WIDE_LOCATIONS = {"", "united kingdom", "uk", "gb", "great britain"}

# Adzuna's redirect_url embeds the caller's app_id as a `utm_source` tracking
# parameter (confirmed live). It must never reach persisted payloads, the
# dashboard, or logs, so it is stripped before a payload leaves the connector.
_CREDENTIAL_QUERY_PARAMS = {"utm_source", "app_id", "app_key"}

# Graduate/early-career searches for the daily refresh. Adzuna's `what` ANDs
# every word, so each phrase is its own search. This keeps the ranked results
# on entry-level roles; generic phrases such as "python" or "software
# engineer" return mostly senior postings at the top of Adzuna's relevance
# order. Kept to 12 so every search gets page 1 within ADZUNA_MAX_PAGES, with
# budget left over for deeper pages on the broad ones.
ADZUNA_GRADUATE_KEYWORDS = (
    "graduate software engineer",
    "junior software engineer",
    "entry level software engineer",
    "graduate backend",
    "graduate cloud engineer",
    "graduate devops",
    "graduate machine learning",
    "graduate data engineer",
    "graduate embedded software",
    "graduate network engineer",
    "telecommunications software engineer",
    "motorsport software",
)

# After this many consecutive page failures, stop paginating rather than keep
# hammering an API that looks persistently rate-limited/blocked this cycle --
# report what was recovered as partial instead.
ADZUNA_MAX_CONSECUTIVE_PAGE_FAILURES = 3


class AdzunaConnector(JobSourceConnector):
    source_name = "adzuna"
    # A capped, relevance-ranked keyword search: a posting missing from today's
    # results may just have dropped out of the top N, so absence never means
    # the vacancy closed.
    supports_closure_inference = False

    def __init__(self, app_id: str | None, app_key: str | None) -> None:
        self.app_id = app_id
        self.app_key = app_key

    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        if not self.app_id or not self.app_key:
            raise RuntimeError("Adzuna requires ADZUNA_APP_ID and ADZUNA_APP_KEY")
        observed_at = now_utc()
        # Adzuna's `what` requires EVERY word to match (a literal " OR " is just
        # another required word -- live-tested: count=0), so each keyword
        # phrase is its own search. `query.limit` caps results per search.
        searches = list(dict.fromkeys(query.keywords)) or ["software engineer"]
        results_per_page = min(max(query.limit, 1), ADZUNA_RESULTS_PER_PAGE)
        pages_per_search = -(-max(query.limit, 1) // results_per_page)
        where = None if (query.location or "").strip().casefold() in _UK_WIDE_LOCATIONS else query.location

        jobs: list[RawJobPayload] = []
        seen_ids: set[str] = set()
        failures: dict[str, str] = {}
        consecutive_failures = 0
        requests_made = 0
        # Per search: results taken so far, and whether it's finished.
        taken = {what: 0 for what in searches}
        finished: set[str] = set()

        # Round-robin by page (page 1 of every search, then page 2, ...), so the
        # request budget covers every search before going deeper on any one.
        for page in range(1, pages_per_search + 1):
            for what in searches:
                if what in finished:
                    continue
                if requests_made >= ADZUNA_MAX_PAGES or consecutive_failures >= ADZUNA_MAX_CONSECUTIVE_PAGE_FAILURES:
                    break
                url = build_url(
                    f"https://api.adzuna.com/v1/api/jobs/gb/search/{page}",
                    {
                        "app_id": self.app_id,
                        "app_key": self.app_key,
                        "results_per_page": results_per_page,
                        "what": what,
                        "where": where,
                        "content-type": "application/json",
                    },
                )
                requests_made += 1
                identifier = f"page_{page}" if len(searches) == 1 else f"{what}: page_{page}"
                try:
                    payload = fetch_json(url)
                except Exception as exc:  # noqa: BLE001 -- any page-fetch failure is isolated below, never lost
                    failures[identifier] = self._redact(f"{type(exc).__name__}: {exc}")
                    consecutive_failures += 1
                    continue
                consecutive_failures = 0
                results = payload.get("results", [])
                if not results:
                    # A genuinely empty page (not a failure) means we've reached
                    # the end of Adzuna's results for this search.
                    finished.add(what)
                    continue
                for item in results:
                    item_id = str(item.get("id"))
                    taken[what] += 1
                    if item_id in seen_ids:
                        # Adzuna's result ordering can shift page-to-page, and
                        # overlapping searches return the same posting -- pass
                        # each posting downstream once.
                        continue
                    seen_ids.add(item_id)
                    item = self._sanitise_item(item)
                    # redirect_url carries an `se=` token that changes between
                    # requests for the same posting (observed live), which made
                    # an unchanged job look CHANGED on every refresh. The public
                    # details page is stable per id and opens the full posting;
                    # the redirect_url stays in the raw payload for audit.
                    source_url = adzuna_job_url(item_id)
                    jobs.append(
                        RawJobPayload(
                            source_name=self.source_name,
                            source_job_id=item_id,
                            source_url=source_url,
                            canonical_application_url=source_url,
                            raw_payload=item,
                            observed_at=observed_at,
                            posted_at=parse_datetime(item.get("created")),
                        )
                    )
                total_count = payload.get("count")
                if taken[what] >= query.limit or (isinstance(total_count, int) and taken[what] >= total_count):
                    finished.add(what)

        if failures:
            raise PartialFetchError(jobs, failures)
        return jobs

    def _redact(self, text: str) -> str:
        for secret in (self.app_id, self.app_key):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text

    def _sanitise_item(self, item: dict[str, Any]) -> dict[str, Any]:
        redirect_url = item.get("redirect_url")
        if not redirect_url:
            return item
        return {**item, "redirect_url": _strip_credential_params(redirect_url)}

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
            salary=_advertised_salary(payload),
            raw_location=location_text,
        )


def adzuna_job_url(job_id: str) -> str:
    return f"https://www.adzuna.co.uk/jobs/details/{job_id}"


def _advertised_salary(payload: dict[str, Any]) -> str | None:
    # Adzuna fills salary_min/max with its OWN estimate when the advertiser gave
    # none, flagged salary_is_predicted="1" (66 of 120 live results on
    # 2026-09-22). An estimate is not the job's salary, so it stays only in the
    # raw payload.
    if str(payload.get("salary_is_predicted", "0")) == "1":
        return None
    return salary_text(payload.get("salary_min"), payload.get("salary_max"), payload.get("salary_currency"))


def _strip_credential_params(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if key.casefold() not in _CREDENTIAL_QUERY_PARAMS]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _location_text(location: dict[str, Any] | None) -> str:
    if not location:
        return "United Kingdom"
    area = location.get("area") or []
    text = location.get("display_name") or ", ".join(area[1:] if area[:1] == ["UK"] else area)
    if not text:
        return "United Kingdom"
    # `area` is Adzuna's structured hierarchy, country first (["UK", "East
    # Midlands", "Northamptonshire", "Towcester", "Silverstone"]). display_name
    # drops the country, and a place like "Silverstone, Towcester" or
    # "Cheltenham, Gloucestershire" is not recognisable as UK from its text
    # alone -- live-validated 2026-09-23: 34 of 134 real UK jobs were being
    # normalised as non-UK. Keep the country Adzuna states explicitly.
    if area[:1] == ["UK"] and not text.rstrip().casefold().endswith(("uk", "united kingdom")):
        text = f"{text}, UK"
    return text

