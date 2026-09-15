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
