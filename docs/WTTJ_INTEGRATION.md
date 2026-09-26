# Welcome to the Jungle Integration

## Supported Access Method

Welcome to the Jungle is represented as a first-class source family:

`welcome_to_the_jungle`

The stable automated path is the official WelcomeKit Jobs API:

`GET https://www.welcomekit.co/api/v1/external/jobs`

This requires a WelcomeKit API key with `jobs_r` or `jobs_rw` scope. The connector expects one or more organization references and reads the API key from `WTTJ_API_KEY` by default.

Example:

```bash
export WTTJ_API_KEY="..."
PYTHONPATH=src python3 -m jobintel.cli.main ingest \
  --wttj-org-ref example_org \
  --limit 100
```

## Automatic Refresh

WTTJ can participate in the same scheduled refresh workflow as the other live connectors when official WelcomeKit credentials and organization references are configured in the registry.

Registry-backed WTTJ entries use:

- `connector_type = welcome_to_the_jungle`
- `connector_token`, `organization_reference`, or `welcome_to_the_jungle_organization_reference`
- `enabled = true`
- `verification_status = verified` once a credentialed refresh succeeds

The refresh command is suitable for cron or launchd:

```bash
export WTTJ_API_KEY="..."
PYTHONPATH=src python3 -m jobintel.cli.main refresh-sources --limit 100
```

Refresh behavior:

- Fetches current published jobs from the official WelcomeKit Jobs API.
- Adds newly observed jobs as `NEW`.
- Marks same-content jobs as `UNCHANGED`.
- Marks meaningful title/location/JD/apply URL changes as `CHANGED`.
- Marks jobs missing from a successful refresh of an organization as `DISAPPEARED` only when that organization's listing was fully observed (pagination exhausted, no keyword filter, not truncated). Manual WTTJ imports are never closed by an API refresh.
- Marks previously disappeared jobs as `REAPPEARED` when they return.
- Preserves `first_seen_at`, updates `last_seen_at`, and retains source observations.

No-credential behavior:

- If `WTTJ_API_KEY` is absent, WTTJ is marked `auth_required`.
- The refresh command continues with Greenhouse, Lever, Ashby, Workable, and SmartRecruiters.
- Existing WTTJ jobs are not marked disappeared merely because credentials are missing.
- Manual URL/CSV/JSON/discovery import remains the fallback.

WTTJ source health is shown in the dashboard as:

- `Live API`
- `Auth required`
- `Manual only`
- `Error`

The dashboard also shows last synced time, active WTTJ jobs, and the last error where present.

Recommended schedule: once or twice per day is sufficient for personal job-search use unless your WelcomeKit API agreement permits and justifies more frequent polling.

## Manual Fallback

If API credentials are not available, WTTJ jobs can be imported from a CSV, JSON, or newline-delimited URL file. This is intended for manually collected WTTJ job URLs or exports, not for scraping WTTJ pages.

## Bulk Discovery Import

For larger batches collected from WTTJ UK category/search result pages, use a lightweight discovery import. This does not fetch or scrape individual WTTJ job pages. It preserves enough list-result metadata to create source observations that can be ranked conservatively and enriched later.

Discovery CSV columns:

`wttj_url,title,company,location,work_mode,posted_at,employment_type,salary,source_category,discovery_url`

Minimum required fields:

- `wttj_url`
- `title`
- `company`

Example:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main wttj-discovery-import wttj_discovery.csv --limit 1000
```

Discovery JSON can be either a list of job objects or an object containing `jobs` or `records`, using the same field names as the CSV schema.

Discovery rows are recorded with `_ingestion_method = discovery_import` and `_enrichment_state = discovery_only` when no description or requirements are supplied.

Dashboard behavior:

- WTTJ badge remains visible.
- WTTJ count includes discovery-only and enriched jobs.
- Discovery-only jobs show `Needs JD enrichment`.
- The enrichment filter supports `discovery_only`, `partially_enriched`, and `fully_enriched`.

Later enrichment uses the existing rich import command:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main wttj-import wttj_jobs.csv --limit 100
```

If the enriched row has the same WTTJ URL/source job ID, the existing source observation is updated rather than duplicated. `first_seen_at` is preserved and `last_seen_at` advances.

Rich CSV columns may include:

`wttj_url,source_job_id,title,company,location,work_mode,posted_at,description,requirements,direct_apply_url,salary,contract_type,employment_type,remote_policy,notes`

Minimum required fields for rich CSV/JSON:

- `wttj_url`
- `title`
- `company`

Everything else is optional. Missing optional fields are reported in the import summary but are not fabricated.

Example:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main wttj-import wttj_jobs.csv --limit 100
```

Rich JSON can be either a list of job objects or an object containing `jobs` or `records`:

```json
[
  {
    "wttj_url": "https://www.welcometothejungle.com/en/companies/example/jobs/junior-backend-engineer_london",
    "source_job_id": "WTTJ_EXAMPLE_001",
    "title": "Junior Backend Engineer",
    "company": "Example Company",
    "location": "London, United Kingdom",
    "work_mode": "hybrid",
    "posted_at": "2026-08-30T09:00:00+00:00",
    "description": "Build Python APIs and maintain PostgreSQL-backed services.",
    "requirements": "0-2 years experience. Skilled Worker visa sponsorship available. 2026 graduates welcome.",
    "direct_apply_url": "https://example.com/jobs/junior-backend-engineer/apply",
    "salary": "GBP 40000-50000",
    "contract_type": "permanent",
    "employment_type": "full_time",
    "remote_policy": "hybrid",
    "notes": "Manual note."
  }
]
```

For URL-only fallback:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main wttj-import wttj_urls.txt --limit 100
```

`wttj_urls.txt` should contain one WTTJ job URL per line:

```text
https://www.welcometothejungle.com/en/companies/example/jobs/junior-backend-engineer_london
https://www.welcometothejungle.com/en/companies/example/jobs/data-engineer_remote
```

CSV imports are recorded with `_ingestion_method = manual_csv`. JSON imports are recorded with `_ingestion_method = manual_json`. URL-list imports are recorded with `_ingestion_method = manual_url`.

The importer validates that each URL is a WTTJ job URL and prints a per-row outcome:

- `imported`
- `duplicate`
- `invalid_url`
- `unsupported_page_structure`
- `missing_required_fields`

The command also prints:

```text
Rows supplied:
Imported:
Updated:
Deduplicated:
Failed:
Missing description:
Missing posted date:
Missing direct apply URL:
Canonical WTTJ jobs:
WTTJ observations retained:
```

URL-only imports preserve the original WTTJ URL and derive title, company, and location only from the URL path where that information is present. They do not fabricate missing descriptions, sponsorship evidence, graduation-year evidence, posted dates, or direct employer apply URLs.

Rich CSV/JSON imports feed `description` and `requirements` into the existing enrichment and ranking pipeline, allowing normal derivation of role tracks, seniority, experience requirements, sponsorship state, graduation-year state, candidate evidence, recommended CV, and ranking explanation.

Repeated imports are additive to the local store: unrelated existing jobs are not marked inactive by a small WTTJ file import. Re-importing the same WTTJ source job updates `last_seen_at`, preserves `first_seen_at`, and replaces the current canonical text/apply fields where the new row has changed.

Imported jobs still use:

`source = welcome_to_the_jungle`

The raw row is retained in the source observation with its exact ingestion method.

## Preserved Fields

For WTTJ observations the system preserves:

- source job ID where available
- WTTJ job URL
- direct application URL where available
- company
- title
- normalized and raw location
- posted date where available
- description
- ingestion method
- first seen and last seen timestamps
- raw payload

## Dashboard Visibility

The dashboard exposes WTTJ explicitly:

- source filter option: `welcome_to_the_jungle`, displayed as `Welcome to the Jungle`
- quick preset: `WTTJ Jobs`
- metric: WTTJ job count
- source badge: `Welcome to the Jungle`

WTTJ jobs are not displayed as generic company-site or manual jobs.

## Deduplication

WTTJ observations are deduplicated against employer ATS jobs using the existing canonical dedupe logic. If the same vacancy appears on WTTJ and on Greenhouse, Lever, Ashby, Workable, SmartRecruiters, or a company source, the canonical job keeps all source observations.

The direct employer/ATS application URL is preferred for the canonical apply link when present. The WTTJ observation and badge are retained.

## Known Limitations

- Automated WTTJ ingestion requires official WelcomeKit API credentials.
- Public WTTJ pages are not scraped.
- URL-only manual imports can preserve the URL and source identity, but richer ranking quality depends on manually supplied title, company, location, description, and requirements fields.
- WTTJ organization references must come from verified API/account context; the system does not guess them from company names.
