# Sources

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
