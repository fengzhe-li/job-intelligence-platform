# Job Intelligence Platform

Multi-source job aggregation, layered deduplication, and explainable candidate/job ranking — built entirely on the Python standard library.

## Overview

Job Intelligence Platform ingests job postings from seven different sources (six ATS providers plus Adzuna), merges duplicate postings of the same vacancy across sources, and ranks them against a candidate's evidence-backed capability profile using a transparent, component-scored matching engine. It is served through both a CLI and a lightweight bilingual (English/Chinese) web dashboard.

It began as a personal tool for a single job search and is published here as a backend/data-engineering portfolio piece. The example configuration (target companies, ranking weights) in this repository is illustrative, not a claim about any specific job search outcome — no real candidate data, CVs, or contact details are included.

## Problem / Motivation

Job boards and company ATS pages are fragmented: the same role is often posted on a company's own Greenhouse/Lever page *and* on aggregators, with slightly different text each time. A simple keyword search across sources produces duplicates and buries relevant roles under irrelevant ones. This project treats that as an integration and ranking problem: pull from multiple compliant public APIs, merge what's actually the same job, and score what's left against concrete evidence of a candidate's skills — with every score traceable to a reason, not a black box.

## Architecture

![System architecture](docs/images/system-architecture.svg)

Four stages: **connectors** (one per source, behind a shared `JobSourceConnector` interface) → **pipeline** (normalise, deduplicate, enrich, match) → **storage** (local JSON/JSONL today; a PostgreSQL-ready schema contract for later) → **serving** (CLI and dashboard, both reading the same store).

## Core Workflow

```text
Configured sources (registry or CLI flags)
  -> connector.fetch_jobs()            # compliant public API per source
  -> normalise to canonical Job model
  -> deduplicate_jobs()                # layered ID/URL/company+title/fuzzy match
  -> enrich_job()                      # sponsorship, graduation-year, role-track, seniority
  -> match_job(candidate, job, config) # component-scored, explainable ranking
  -> LocalJobStore.write_jobs()        # merges with existing state, preserves history
```

Refreshing a source doesn't delete jobs that disappear — they're marked `inactive` and kept, so history (`first_seen_at`, `last_seen_at`, `latest_observed_state`) is never silently lost.

## Key Capabilities

- **Multi-source ingestion** from Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Welcome to the Jungle, and Adzuna, each through its official public API — no scraping of application-submission endpoints. See [docs/SOURCES.md](docs/SOURCES.md) for the compliance assumption behind each connector.
- **Company registry with automated ATS discovery** (`config/target_companies.json`): given a careers-page URL, the system probes Greenhouse/Lever/Ashby/Workable/SmartRecruiters endpoints, records which one responds with a valid job feed, and tracks verification status per company.
- **Layered cross-source deduplication** — see [Deduplication](#deduplication) below.
- **Explainable ranking** — see [Explainable Matching](#explainable-matching) below.
- **Evidence-based candidate profiling**: capabilities are extracted from CV text, project descriptions, and README/education text, each capability keeping a citation (source + quote + confidence) back to where it came from.
- **Bilingual dashboard** with saved searches, workflow tracking (new → saved → applied → interview → offer/rejected), source-health monitoring, and freshness-aware "what's new" views.

## Deduplication

![Deduplication flow](docs/images/deduplication-flow.svg)

`jobintel.dedup.v1.deduplicate_jobs` tries four checks in order — exact source ID, canonical application URL, normalised company+title+location, then fuzzy title match (`difflib.SequenceMatcher` ratio ≥ 0.88, same location) — merging into the existing canonical job on the first match and preferring the more direct source (company site/ATS over aggregator) for display fields.

## Explainable Matching

![Explainable ranking](docs/images/explainable-ranking.svg)

`jobintel.matching.matcher.match_job` returns per-component scores (technical fit, role preference, seniority, sponsorship, graduation-year fit, location, source quality, freshness, and more) alongside cited evidence and an assembled explanation sentence — not a single opaque score. See [docs/RANKING.md](docs/RANKING.md) for the full component list and configuration.

## Data / Persistence

Local development writes plain JSON/JSONL under `data/local/`: raw per-refresh snapshots, canonical deduplicated jobs, workflow status, saved searches, and source health. `data_contracts/schemas/postgres.sql` defines a normalized PostgreSQL schema for the same data model, intended for a later persistence layer — **it is not currently wired up**; nothing in this repository talks to a real database.

## Dashboard / CLI Interfaces

The dashboard (`jobintel.dashboard.server`, pure `http.server`, no framework) exposes:

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Ranked, filterable job shortlist |
| `/job?id=...` | GET | Full explainability detail for one job |
| `/refresh` | POST | Trigger a source refresh (lock-guarded against overlap) |
| `/saved-search` | POST | Persist the current filter set under a name |
| `/workflow` | POST | Update a job's application-workflow status |

The CLI (`jobintel.cli.main`) exposes 20 subcommands covering ingestion, the company registry, ATS discovery/verification, profile import/rebuild/inspect, ranked listing, and the dashboard itself — run `PYTHONPATH=src python3 -m jobintel.cli.main --help` for the full list.

## Screenshots

Both screenshots use synthetic fixture data (`jobintel.fixtures.sample_data`) — no real job search results or personal data.

![Dashboard overview](docs/images/dashboard-overview.png)

![Job detail explainability view](docs/images/job-detail-explainability.png)

## Tech Stack

- **Runtime**: Python ≥ 3.12, **zero third-party runtime dependencies** — connectors use `urllib`, the dashboard uses `http.server`, storage uses `json`/`csv`, dedup uses `difflib`.
- **Testing**: `pytest` (dev-only dependency), 143 tests.
- **Data model**: `dataclasses` throughout; a PostgreSQL schema contract for future persistence.

## Engineering Highlights

- A shared `JobSourceConnector` abstract interface lets seven structurally different job-board APIs (JSON REST, paginated, API-key-gated) plug into one pipeline without special-casing each source downstream.
- Deduplication is genuinely layered, not a single heuristic — it's designed around the real-world case where the same vacancy legitimately appears with different text on different platforms.
- The matching engine is explanation-first: every ranked job exposes *why* it ranked where it did, including which specific pieces of candidate evidence matched and what's missing.
- The company registry doubles as living documentation of ATS integration reality: each entry records its actual verification status, HTTP failure kind, and job count as of the last check — not just a static list of URLs.
- Found and fixed during this publication pass: the dashboard's job-detail and list views assumed every ranked job has a full set of match components, which crashes for jobs in excluded occupation categories (the matcher intentionally short-circuits those to a single `excluded_occupation` component). Fixed in `dashboard/service.py` with a regression test (`test_excluded_occupation_job_does_not_crash_dashboard_or_detail_view`).

## Repository Structure

```text
src/jobintel/
  connectors/    One module per source, sharing the JobSourceConnector interface
  dedup/         Layered cross-source deduplication
  analysis/      Deterministic enrichment: role-track, sponsorship, graduation-year, quality
  matching/      Explainable candidate/job matcher
  pipeline/      Ingestion and refresh orchestration
  storage/       Local JSON/JSONL store
  dashboard/     Bilingual dashboard service + stdlib HTTP server
  cli/           20-subcommand CLI entrypoint
  models/        Candidate, job, and taxonomy dataclasses
  profile_ingestion.py   Evidence-based candidate capability extraction
  company_registry.py    Target-company registry + ATS discovery/verification

data_contracts/schemas/  PostgreSQL-ready schema (not yet wired up)
docs/                     Architecture, model, ranking, and source documentation
tests/                    143 tests covering connectors, dedup, matching, dashboard, CLI paths
config/                   Example ranking strategy and target-company registry
```

`data/local/` (raw snapshots, processed jobs, and any real candidate profile) is git-ignored — it is per-user runtime state, not part of the published code.

## Quick Start

No installation step — everything runs via `PYTHONPATH`:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --fixtures
```

Run the dashboard against sample data:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main dashboard --store /tmp/jobintel-demo
```

Ingest from a couple of live public boards:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main ingest --greenhouse-board example --lever-site example --limit 20
```

Import your own candidate evidence and rebuild your profile:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main profile-import ./my-project.md --source-type manual_project_description --title "My Project"
PYTHONPATH=src python3 -m jobintel.cli.main profile-rebuild --name "Your Name" --graduation-year 2026
```

## Testing

```bash
python3 -m pip install -e ".[dev]"   # installs pytest only
PYTHONPATH=src python3 -m pytest tests/ -q
```

143 tests pass, 1 skipped (a live-network source test that only runs with network access). Coverage includes connector normalisation, deduplication edge cases, the matching engine, dashboard model-building and rendering (including the excluded-occupation regression added in this pass), CLI argument wiring, and registry/schema compatibility.

## Limitations

- No persistence layer is actually connected — `data_contracts/schemas/postgres.sql` documents the intended shape but nothing writes to PostgreSQL yet.
- LinkedIn and Indeed are explicitly not implemented (no compliant public API for the intended use case).
- The dashboard is a single-process `http.server` app with no authentication — it's designed for local personal use, not multi-user or public deployment.
- Role-track/skill extraction is deterministic keyword/phrase matching, not NLP or an LLM; `docs/ARCHITECTURE.md` documents this as a possible later direction, not a current feature.
- The "compliant source" claims describe *documented public API access*, not a legal compliance review.

## Future Improvements

- Wire up the PostgreSQL schema as the real persistence layer, replacing local JSON/JSONL.
- Spark/Airflow for batch refresh orchestration, as scoped (but not started) in `docs/ARCHITECTURE.md`.
- Richer, evidence-grounded skill/role-track extraction beyond keyword matching.

## Status

Personal-use MVP. All 143 tests pass locally; no CI-verified live deployment. See [docs/PRD.md](docs/PRD.md) for original scope and [docs/PHASE3_VALIDATION.md](docs/PHASE3_VALIDATION.md) / [docs/LIVE_JOB_VALIDATION.md](docs/LIVE_JOB_VALIDATION.md) for measured validation runs.

## Further Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module boundaries and persistence plan
- [docs/SOURCES.md](docs/SOURCES.md) — per-source access method and compliance assumptions
- [docs/RANKING.md](docs/RANKING.md) — full ranking component list
- [docs/COMPANY_REGISTRY.md](docs/COMPANY_REGISTRY.md) — registry schema and workflow
- [docs/CANDIDATE_MODEL.md](docs/CANDIDATE_MODEL.md) / [docs/JOB_MODEL.md](docs/JOB_MODEL.md) — domain models
- [docs/PROFILE_INGESTION.md](docs/PROFILE_INGESTION.md) — how CV/project evidence becomes capabilities
- [docs/WTTJ_INTEGRATION.md](docs/WTTJ_INTEGRATION.md) — Welcome to the Jungle API + manual import fallback
