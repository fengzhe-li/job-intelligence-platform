# Architecture

## MVP Shape

The MVP uses a modular monolith. Profile, analysis, matching, ranking, workflow, and future ingestion modules live in one deployable application boundary while keeping package-level seams clear enough to separate later.

Current modules:

- `jobintel.models`: candidate, job, taxonomy models
- `jobintel.analysis`: deterministic enrichment, role-track inference, evidence extraction
- `jobintel.matching`: location scoring and explainable matching/ranking
- `jobintel.fixtures`: local candidate and job fixtures
- `jobintel.connectors`: compliant source connectors for Greenhouse, Lever, and Adzuna
- `jobintel.storage`: lightweight local JSON/JSONL storage for raw snapshots and canonical jobs
- `jobintel.dedup`: layered vacancy deduplication
- `jobintel.pipeline`: ingestion, normalisation, enrichment, deduplication, and ranking orchestration
- `jobintel.cli`: command-line inspection workflow before a dashboard exists
- `jobintel.profile_ingestion`: personal CV/project/README/education/experience profile ingestion
- `jobintel.github_sync`: incremental GitHub repository discovery into the Evidence Bank (see [GITHUB_SYNC.md](GITHUB_SYNC.md))
- `jobintel.connectors.manual_source` / `jobintel.connectors.graduate_sources`: manual/discovery import for UK graduate boards with no public API (see [GRADUATE_SOURCES.md](GRADUATE_SOURCES.md))
- `jobintel.matching.project_selection`: scores candidate projects against a specific job's requirements
- `jobintel.analysis.jd_requirements`: structured JD requirement extraction (must-have/preferred skills, seniority, role family, domain, intake year, etc.)
- `jobintel.matching.cv_generation` / `jobintel.matching.cv_render` / `jobintel.matching.review_gate`: job-specific evidence-grounded CV generation (JD-aware bullet composition), real PDF rendering with a measured page count (`reportlab` -- the project's one third-party runtime dependency), and the human review gate (see [CV_GENERATION.md](CV_GENERATION.md))
- `jobintel.models.cv_artifact` / `jobintel.storage.cv_artifact_store`: immutable, append-only generated-CV persistence
- `jobintel.models.application` / `jobintel.storage.application_store`: application lifecycle tracking with append-only status history (see [APPLICATION_TRACKER.md](APPLICATION_TRACKER.md))
- `jobintel.company_registry`: editable UK target-company registry
- `jobintel.validation`: measured Phase 3 validation reporting

Spark/Airflow remain outside the application runtime and are planned as separate data-pipeline components.

## Deterministic Logic

- dataclass domain models
- role-track keyword inference baseline
- skill extraction baseline
- sponsorship phrase detection
- graduation-year phrase detection
- location scoring modes
- excluded occupation detection
- source quality weighting
- freshness decay
- source payload normalisation
- cross-source deduplication using source IDs, application URLs, company/title/location, and fuzzy title matching

## NLP/LLM-Assisted Later

- richer skill extraction from long descriptions
- semantic role-track inference
- nuanced sponsorship/eligibility interpretation
- project-to-job evidence matching
- bilingual summaries and explanations
- hybrid CV recommendation refinement

## Persistence

The runtime currently uses in-memory dataclasses for lightweight local development. `data_contracts/schemas/postgres.sql` defines the PostgreSQL-ready shape for normalized app persistence.

Local development writes:

- raw source snapshots to `data/local/raw_snapshots/*.jsonl`
- canonical processed jobs to `data/local/processed/canonical_jobs.json`
- personal candidate sources to `data/local/profile/sources.json`
- rebuilt candidate profile to `data/local/profile/candidate_profile.json`

These paths are intentionally simple and can be replaced by PostgreSQL plus object storage later.

Source observations preserve `first_seen_at`, `last_seen_at`, `posted_at`, `active`, and `latest_observed_state`. When an observed job is absent from a later run, it is retained and marked inactive rather than deleted.

## CLI

Use fixtures:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --fixtures
```

Use configured live sources:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main ingest --greenhouse-board example --lever-site example --limit 20
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --limit 20
```

Adzuna requires `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`.

Registry ingest:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main registry-summary
PYTHONPATH=src python3 -m jobintel.cli.main ingest-registry --limit 50
```

Personal profile:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main profile-import ./my-project.md --source-type manual_project_description --title "My Project"
PYTHONPATH=src python3 -m jobintel.cli.main profile-rebuild --name "My Name" --graduation-year 2026
PYTHONPATH=src python3 -m jobintel.cli.main profile-inspect
```

## Daily refresh and dashboard (Phase 3)

`pipeline.daily.run_daily_refresh` (CLI `daily-refresh`, dashboard "Run daily refresh") runs, in order and each stage isolated:

1. Registry direct-ATS connectors via `refresh_sources` (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, and WTTJ when `WTTJ_API_KEY` is set).
2. Adzuna (only meaningful with `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`; otherwise recorded as `not_configured`, never failed) and Prospects (PARTIAL_COVERAGE, never closes jobs).
3. Company discovery from newly observed non-registry jobs. This is offline and append-only; it never edits the registry or its manual exclusions.
4. Optional incremental GitHub evidence sync (`JOBINTEL_GITHUB_USERNAME`, optional `GITHUB_TOKEN`). It runs last so it can't block job discovery. It uses the existing profile's name and graduation year, and is skipped if no profile exists yet.

Trackr, Gradcracker and Bright Network are MANUAL_FALLBACK_ONLY. They are listed in the report and on the watchlist, never attempted.

The report is persisted to `processed/daily_refresh_latest.json`, with an append-only `processed/daily_refresh_history.jsonl`. Counts are per-refresh deltas: new canonical jobs, changed observations, and canonical jobs that went from active to fully closed *during that refresh*. Each source gets one outcome: `succeeded`, `succeeded_zero_results`, `partial`, `failed` or `not_configured`. The Today page renders this persisted report and marks it STALE after 36h.

Source health (`dashboard.operations.build_source_health_view`) derives each source's state only from persisted refresh evidence plus current configuration:
- Families come from the curated profiles, the registry-derived connector families, and any source with a health record.
- States are `ACTIVE_AND_REFRESHED`, `ACTIVE_NOT_REFRESHED` (configured, never run), `NOT_CONFIGURED`, `FAILED`, `PARTIAL_REFRESH`, `STALE` (>36h), `DEGRADED` (suspicious drop / closure withheld) and `MANUAL_ONLY`.
- Nothing is healthy by default.

Closure semantics are in [SOURCE_COVERAGE.md](SOURCE_COVERAGE.md#closure-invariant-phase-3). Application status ownership is in [APPLICATION_TRACKER.md](APPLICATION_TRACKER.md).

Generated CV PDFs are served by `GET /cv/pdf?id=<cv_version_id>`:
- Only ids matching `cv-<12 hex>` that exist in `CVArtifactStore` are accepted.
- The path is rebuilt from the id and confined to `cv_artifacts/pdfs/`. The stored `pdf_path` is never trusted as a location.
- The bytes must match the SHA-256 recorded at generation (409 otherwise).
- Responses are `application/pdf` with `nosniff`.

Submitting via `POST /application/submit` requires an explicit "I reviewed this exact CV" confirmation.
