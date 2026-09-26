# Sources

For per-source automation status, live-testing evidence, and honest coverage classification (`PRODUCTION_READY` / `PARTIAL_COVERAGE` / `MANUAL_FALLBACK_ONLY` / `BROKEN_OR_UNAVAILABLE`) across all 11 sources this project touches, see [docs/SOURCE_COVERAGE.md](SOURCE_COVERAGE.md) -- this document covers *access method and compliance assumption* per source; that one covers *does it actually work, and how much of the source does it actually see*.

## Supported Sources

### Greenhouse

Access method: public Greenhouse Job Board API.

Endpoint shape:

```text
GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
```

Compliance assumption: public job-board GET endpoints are intended for published jobs. Application submission endpoints are not used.

Limitations:

- Requires known company board tokens.
- Posted time may be represented as update time depending on board payload.
- Company name may need board-token mapping for nicer display.

### Lever

Access method: public Lever postings API.

Endpoint shape:

```text
GET https://api.lever.co/v0/postings/{site}?mode=json
```

Compliance assumption: public postings API exposes published job postings. Application submission is not implemented.

Limitations:

- Requires known company site names.
- EU Lever instances may need the `jobs.eu.lever.co` base.

### Adzuna

Access method: official Adzuna REST API.

Endpoint shape:

```text
GET https://api.adzuna.com/v1/api/jobs/gb/search/1
```

Compliance assumption: official API access with registered credentials.

Limitations:

- Requires `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`.
- Redirect URLs may point through Adzuna rather than the direct employer ATS.
- Results depend on Adzuna query behavior and API limits.
- Paginates up to 20 pages / ~1000 results per refresh (Phase 2.6), cross-page deduplicated; stops after 3 consecutive page failures rather than continuing to hammer a rate-limited API. Credentials are not available in this project's development environment, so this was validated with 9 mocked unit tests (`tests/test_adzuna_pagination.py`), not a live multi-page run.

### Welcome to the Jungle

Access method: official WelcomeKit Jobs API when credentials are available, plus a manual CSV/JSON import fallback.

Endpoint shape:

```text
GET https://www.welcomekit.co/api/v1/external/jobs
```

Compliance assumption: automated ingestion uses the documented WelcomeKit API with a configured bearer token. Public WTTJ pages are not scraped.

Limitations:

- Requires `WTTJ_API_KEY` with `jobs_r` or `jobs_rw` scope for automated ingestion.
- Requires verified organization references; the system does not guess references from company names.
- Manual imports can preserve WTTJ URLs and source identity, but richer matching depends on supplied title, company, location, and description fields.

See `docs/WTTJ_INTEGRATION.md` for commands and fallback format.

### Prospects

Access method: public schema.org `JobPosting` JSON-LD structured data, embedded in server-rendered job detail pages -- the same data the site publishes for Google for Jobs indexing, fetched via plain HTTP GET. Not a hidden/private API.

Endpoint shape:

```text
GET https://www.prospects.ac.uk/browse-graduate-jobs/{category-slug}/all-locations   # listing
GET https://www.prospects.ac.uk/graduate-jobs/{job-slug}-{id}                        # detail (redirects; JSON-LD in <head>)
```

Compliance assumption: fetching a public HTML page and reading its own publicly-embedded, search-engine-facing structured data is not scraping in the sense this project avoids elsewhere (no parsing of arbitrary/private markup, no bypassing any access control). Confirmed no CAPTCHA/Cloudflare-challenge on these paths during the Phase 2.5 audit (unlike Gradcracker/Bright Network, which are and are therefore not touched).

Limitations:

- No credentials required, no known-company list required (category-based, not company-scoped).
- Only IT and Engineering-and-manufacturing categories configured by default -- Phase 2.6 re-audited all 29 of Prospects' own sector categories against this project's target role families and confirmed these two are the correct/sufficient ones (no dedicated AI/ML/telecoms/embedded/motorsport category exists on Prospects).
- No pagination mechanism found on listing pages after live re-investigation (`?page=2` returns byte-identical content to page 1, no total-count text, no listings-API reference) -- treated as the full per-category listing, though a client-side-only mechanism a plain HTTP GET can't observe can't be fully ruled out.

See `docs/GRADUATE_SOURCES.md` and `docs/SOURCE_COVERAGE.md` for the full audit and commands.

## Not Implemented

- LinkedIn scraping
- Indeed scraping
- automated application submission

Additional connectors remain future candidates only where access is compliant and technically reliable.

## Source Preference

Ranking uses configurable source weights. Direct employer/ATS sources, Greenhouse, and Lever are preferred by default over aggregator-only observations, while Adzuna remains useful for discovery and market coverage.

Welcome to the Jungle has its own configurable source weight under `welcome_to_the_jungle`; it is not hard-coded as universally superior.

## Raw Snapshot Policy

Raw source payloads are preserved before normalisation. Local development stores JSONL snapshots under `data/local/raw_snapshots`, while the PostgreSQL-ready schema includes `raw_job_snapshots`.

## Target Company Registry

Editable registry: `config/target_companies.json`.

Fields:

- `company_name`
- `industry`
- `priority`
- `greenhouse_board_token`
- `lever_site_token`
- `careers_url`
- `source_connector_type`
- `enabled`
- `notes`

Only enabled entries with a supported compliant connector are used by `ingest-registry`. Careers URLs are tracked for future research, but no fragile scraping is performed.
