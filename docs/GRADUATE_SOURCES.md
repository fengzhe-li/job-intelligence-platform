# UK Graduate Source Expansion

## Supported Sources

Four additional UK graduate-focused job boards are supported, each with its own access reality -- see [docs/SOURCE_COVERAGE.md](SOURCE_COVERAGE.md) for the full audit, live-testing evidence, and per-source classification (`PRODUCTION_READY` / `PARTIAL_COVERAGE` / `MANUAL_FALLBACK_ONLY` / `BROKEN_OR_UNAVAILABLE`):

- `trackr` (The Trackr, `the-trackr.com` / `app.the-trackr.com`) -- manual/discovery import only. **MANUAL_FALLBACK_ONLY** (reclassified in Phase 2.6 -- see below).
- `gradcracker` (Gradcracker) -- manual/discovery import only. **MANUAL_FALLBACK_ONLY**.
- `bright_network` (Bright Network) -- manual/discovery import only. **MANUAL_FALLBACK_ONLY**.
- `prospects` (Prospects) -- **live automated discovery** (`connectors.prospects.ProspectsConnector`), plus manual/discovery import as a fallback. **PARTIAL_COVERAGE** (real and live-verified -- 109 real jobs fetched live in the Phase 2.6 re-audit -- not yet exhaustive of Prospects' full site).

Any of the four (or any other job you find yourself, on any site) can also be imported one at a time via `manual-job-import <url>`, which attempts to auto-fetch the posting's own structured data first -- see [Single-URL Manual Import](#single-url-manual-import) below.

## Access Method

**Prospects** publishes real schema.org `JobPosting` JSON-LD on its job detail pages -- structured data the site itself publishes for search-engine indexing (Google for Jobs), not a hidden or private API, and not scraped in the sense of parsing arbitrary page markup: it's the exact same standards-based block search engines read. `ProspectsConnector` fetches category listing pages (plain server-rendered HTML, no bot-blocking encountered), follows each job link, and parses the JSON-LD block for full JD, company, dates, deadline (`validThrough`), and location. See [CLI](#live-discovery-prospects) below.

**The Trackr** has a real public JSON API (`api.the-trackr.com/programmes`) discovered during the Phase 2.5 audit, with a disclosed parameter schema (`region`, `industry`, `season`, `type`; valid `region` values `UK`/`US`/`EU`/`France`/`Germany`/`Italy`/`Hong Kong`, valid `industry` values `Finance`/`Tech`/`Law`/`Engineering`, both confirmed via the API's own validation errors). Phase 2.6 re-investigated this live and found: every valid `region`×`industry` combination tested, and a filterless request, returns HTTP 200 with an empty `[]` result; and the endpoint's own response headers disclose a **10-requests/day rate limit**, which this audit's own handful of test requests exhausted (`ratelimit: "10-in-1day"; r=0`, `retry-after: 75811` seconds, ~21 hours) on top of a looser 1000/day general limit. No API key or auth token was found exposed anywhere on the site. Conclusion: even setting aside whether a still-untried parameter combination might return real data, a 10-requests/day cap is not viable for a production refresh connector -- this is **not** wired up as a live connector, and won't be without some other form of access this project doesn't have.

**Gradcracker** and **Bright Network** are both Cloudflare-managed-challenge-protected site-wide (confirmed on robots.txt, sitemap, and real listing pages -- HTTP 403 "Just a moment..." pages returned to a plain HTTP client). Per this project's compliance stance, this is **not bypassed** (no CAPTCHA-solving, no anti-bot evasion, no browser-automation workaround). Both remain manual/discovery import only.

The shared manual-import mechanism lives in `jobintel.connectors.manual_source` (`ManualSourceConnector`, `ManualSourceDiscoveryConnector`), parameterized per source by `jobintel.connectors.graduate_sources.GRADUATE_SOURCE_SPECS`. It remains available for all four sources, including Prospects, as a fallback when live discovery misses something or when collecting from a source with no automated path.

## Live Discovery (Prospects)

```bash
PYTHONPATH=src python3 -m jobintel.cli.main ingest --prospects --limit 50
```

Default categories: IT (`information-technology-69`) and Engineering & Manufacturing (`engineering-and-manufacturing-172`). Phase 2.6 re-audited all 29 of Prospects' own sector categories against this project's ~20 target role tracks (software/data/cloud/devops/AI-ML/telecoms/network/embedded/IoT/motorsport, etc.) and confirmed these two are the correct ones -- Prospects has no dedicated category for AI/ML, telecoms, embedded, or motorsport; roles in those areas are tagged IT or Engineering-and-manufacturing on Prospects' own site. Three plausible adjacent categories were spot-checked live and found dominated by unrelated finance/food-science/general-business content, not added. Customize via `ProspectsConnector(category_slugs=(...))` if used programmatically. **Pagination**: Phase 2.6 re-tested live -- requesting `?page=2` on a listing page returns byte-for-byte the same links as page 1, with no "N of M" text and no reference to a listings API anywhere in the page -- treated as the full per-category listing (109 real jobs fetched live across both categories, cross-category duplicates deduplicated), a materially stronger claim than the Phase 2.5 "unconfirmed" wording, though a client-side-only mechanism a plain HTTP GET can't observe can't be fully ruled out.

## Manual Import

```bash
PYTHONPATH=src python3 -m jobintel.cli.main graduate-source-import trackr trackr_jobs.csv --limit 100
```

`source` is one of `trackr`, `gradcracker`, `bright_network`, `prospects`. Input is CSV, JSON, or a newline-delimited URL list, same shape as WTTJ's manual import:

Required fields: `job_url` (must match the source's own domain), `title`, `company`.

Optional fields: `location`, `description`, `requirements`, `posted_at`, `deadline` (preserved with provenance, never invented if absent), `salary`, `application_url`/`apply_url`/`direct_apply_url`, `source_job_id`.

## Discovery Import

For bulk list-page metadata where full JD text isn't yet available:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main graduate-source-discovery-import gradcracker gradcracker_discovery.csv --limit 1000
```

Discovery rows are recorded with `_ingestion_method = discovery_import` and `_enrichment_state = discovery_only` when no description/requirements are supplied. They can be re-imported later with the rich `graduate-source-import` command once full JD text is collected; matching by `job_url` updates the existing observation rather than duplicating it.

## Ranking Neutrality

Per the project's ranking principle, source brand does not get a ranking boost. `config.py`'s `source_weights` gives `trackr`/`gradcracker`/`bright_network`/`prospects` the same weight tier as `adzuna` (0.7) — below a direct company/ATS source (1.0/0.95), but not penalized relative to other aggregators. All valid graduate/early-career jobs enter the same candidate pool regardless of which of these four sources they came from, and regardless of whether they arrived via live discovery or manual import.

## Single-URL Manual Import

For one specific job you found yourself, at any URL (not limited to the four sources above -- a company careers page, a LinkedIn post, anything):

```bash
PYTHONPATH=src python3 -m jobintel.cli.main manual-job-import \
  --url "https://example.com/careers/graduate-software-engineer" \
  --source-name "manual"
```

Attempts to auto-fetch and parse the page's own schema.org `JobPosting` JSON-LD first (the same mechanism Prospects' live discovery uses) -- title, company, location, description, posted date, and deadline are filled in automatically wherever found. Any field auto-fetch can't find falls back to `--title`/`--company`/`--location`/`--description`/`--deadline`/`--posted-at`/`--salary`/`--application-url` if supplied; a field present in neither is left out entirely, never invented. Reports exactly which fields were auto-fetched vs. supplied vs. missing. Runs through the identical canonical pipeline as every other source (`ingest_from_connectors` -- normalise, enrich, dedup, store), not a separate bypass, and deduplicates correctly against an existing automated observation of the same vacancy (see `tests/test_cross_source_manual_dedup.py`).

## Known Limitations

- Only Prospects has live automated discovery today; Trackr/Gradcracker/Bright Network remain manual/discovery import only (see [docs/SOURCE_COVERAGE.md](SOURCE_COVERAGE.md) for exactly why, per source).
- URL-only manual imports (and single-URL auto-fetch, when the target page has no JobPosting JSON-LD) preserve identity but ranking quality depends on manually supplied title/company/location/description.
- Gradcracker, Bright Network, and Trackr are not scraped or hammered past their disclosed limits; nothing in this project attempts to bypass bot protection or exceed a documented rate limit.
