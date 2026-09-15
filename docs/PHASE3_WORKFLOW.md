# Phase 3 Workflow

## Personal Profile Ingestion

Import evidence sources:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main profile-import ./cv-backend.txt --source-type cv_text --title "Backend CV"
PYTHONPATH=src python3 -m jobintel.cli.main profile-import ./project.md --source-type manual_project_description --title "Project Name"
PYTHONPATH=src python3 -m jobintel.cli.main profile-import ./README.md --source-type github_readme_export --title "Repository README"
```

Rebuild and inspect:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main profile-rebuild --name "Personal Candidate" --graduation-year 2026
PYTHONPATH=src python3 -m jobintel.cli.main profile-inspect
PYTHONPATH=src python3 -m jobintel.cli.main profile-inspect --role-track data_engineering
```

CV evidence is included, but project and README evidence remain independent sources of capability.

## Company Registry

Edit `config/target_companies.json` to add or disable target companies. Supported live connectors currently use Greenhouse and Lever tokens. Adzuna can still be run directly with API credentials.

```bash
PYTHONPATH=src python3 -m jobintel.cli.main registry-summary
PYTHONPATH=src python3 -m jobintel.cli.main ingest-registry --limit 50
```

## Daily Shortlist

```bash
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --limit 20
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --role-track data_engineering
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --sponsorship sponsor_only
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --location-mode london_only
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --company Monzo
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --source greenhouse
```

Each result shows title, company, location, work mode, source, posted and first-seen timestamps, role tracks, sponsorship, graduation-year state, technical fit, application priority, CV recommendation, hybrid CV flag, strongest evidence, weaknesses, explanation, and application URL.

## New Jobs

```bash
PYTHONPATH=src python3 -m jobintel.cli.main new-jobs --since 2026-08-20T09:00:00+00:00
```

This uses persisted `first_seen_at` values from the local processed job store.

## Location Modes

- `london_only`: non-London roles receive a strong negative location component.
- `london_first_uk_wide`: London is preferred, but non-London UK roles remain visible.
- `uk_wide`: UK roles are treated broadly without London-first preference.

Remote and hybrid are represented separately from city.

## Validation

```bash
PYTHONPATH=src python3 -m jobintel.cli.main validation-report
```

The report is measured from the local processed store and does not fabricate live-job counts.
