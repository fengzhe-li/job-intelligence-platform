from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone

from jobintel.company_registry import connectors_from_registry, load_company_registry, registry_summary
from jobintel.ats_discovery import (
    add_manual_ats_url,
    ats_summary,
    check_ats_url,
    discover_registry,
    exclude_company,
    import_manual_ats_urls,
    reinclude_company,
    unresolved_entries,
    verify_registry,
)
from jobintel.analysis.job_enrichment import enrich_job
from jobintel.config import default_ranking_config, load_ranking_config
from jobintel.dashboard.server import run_dashboard
from jobintel.connectors.adzuna import ADZUNA_GRADUATE_KEYWORDS, AdzunaConnector
from jobintel.connectors.prospects import ProspectsConnector
from jobintel.connectors.ashby import AshbyConnector
from jobintel.connectors.base import ConnectorQuery
from jobintel.connectors.greenhouse import GreenhouseConnector
from jobintel.connectors.lever import LeverConnector
from jobintel.connectors.smartrecruiters import SmartRecruitersConnector
from jobintel.connectors.welcome_to_the_jungle import (
    WelcomeToTheJungleConnector,
    WelcomeToTheJungleManualConnector,
    prepare_wttj_discovery_import,
    prepare_wttj_manual_import,
)
from jobintel.connectors.workable import WorkableConnector
from jobintel.connectors.graduate_sources import GRADUATE_SOURCE_SPECS
from jobintel.connectors.manual_source import (
    ManualSourceConnector,
    ManualSourceDiscoveryConnector,
    ManualSourceSpec,
    prepare_discovery_import,
    prepare_manual_import,
    prepare_single_url_import,
)
from jobintel.fixtures.sample_data import sample_jobs
from jobintel.models.taxonomy import LocationMode, RoleTrack, SponsorshipFilterMode, SponsorshipState
from jobintel.profile_ingestion import add_profile_source, build_candidate_profile, load_candidate_profile, set_contact_details
from jobintel.github_sync import sync_repositories
from jobintel.pipeline.ingestion import DEFAULT_TECH_KEYWORDS, ingest_from_connectors, rank_jobs
from jobintel.pipeline.refresh import refresh_connector, refresh_sources
from jobintel.pipeline.daily import run_daily_refresh
from jobintel.source_coverage import coverage_today
from jobintel.company_coverage import classify_registry, coverage_metrics, manual_watchlist
from jobintel.company_discovery import observe_companies_from_jobs, promote_candidate
from jobintel.storage.company_discovery_store import CompanyDiscoveryStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.matching.cv_generation import generate_and_save_cv
from jobintel.models.application import Application
from jobintel.models.taxonomy import ApplicationStatus, EvidenceSourceType
from jobintel.validation import build_validation_report, write_validation_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal UK graduate job intelligence CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Fetch jobs from configured compliant sources")
    ingest.add_argument("--greenhouse-board", action="append", default=[], help="Greenhouse board token")
    ingest.add_argument("--lever-site", action="append", default=[], help="Lever site name")
    ingest.add_argument("--ashby-board", action="append", default=[], help="Ashby public job board name")
    ingest.add_argument("--workable-account", action="append", default=[], help="Workable public account subdomain")
    ingest.add_argument("--smartrecruiters-company", action="append", default=[], help="SmartRecruiters company identifier")
    ingest.add_argument("--wttj-org-ref", action="append", default=[], help="Welcome to the Jungle/WelcomeKit organization reference")
    ingest.add_argument("--wttj-api-key-env", default="WTTJ_API_KEY", help="Environment variable containing the WelcomeKit API key")
    ingest.add_argument("--adzuna", action="store_true", help="Use Adzuna credentials from environment")
    ingest.add_argument(
        "--prospects",
        action="store_true",
        help="Live automated discovery from prospects.ac.uk (schema.org JobPosting JSON-LD) -- see docs/SOURCE_COVERAGE.md",
    )
    ingest.add_argument("--limit", type=int, default=50)
    ingest.add_argument("--where", default="United Kingdom")
    ingest.add_argument("--store", default="data/local")
    ingest.add_argument("--config", default="config/personal_strategy.json")

    registry_ingest = subparsers.add_parser("ingest-registry", help="Fetch jobs from enabled target-company registry entries")
    registry_ingest.add_argument("--registry", default="config/target_companies.json")
    registry_ingest.add_argument("--config", default="config/personal_strategy.json")
    registry_ingest.add_argument("--store", default="data/local")
    registry_ingest.add_argument("--limit", type=int, default=50)
    registry_ingest.add_argument("--where", default="United Kingdom")

    refresh_sources_cmd = subparsers.add_parser("refresh-sources", help="Refresh enabled live source connectors safely for scheduled runs")
    refresh_sources_cmd.add_argument("--registry", default="config/target_companies.json")
    refresh_sources_cmd.add_argument("--config", default="config/personal_strategy.json")
    refresh_sources_cmd.add_argument("--store", default="data/local")
    refresh_sources_cmd.add_argument("--limit", type=int, default=100)
    refresh_sources_cmd.add_argument("--where", default="United Kingdom")

    repair_cmd = subparsers.add_parser(
        "repair-historical-dedup",
        help="One-time, idempotent repair of canonical jobs merged under the old dedup rules (backs up the store first)",
    )
    repair_cmd.add_argument("--store", default="data/local")
    repair_cmd.add_argument("--backups-dir", default="data/backups")
    repair_cmd.add_argument("--config", default="config/personal_strategy.json")
    repair_cmd.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing")

    registry_cmd = subparsers.add_parser("registry-summary", help="Inspect target-company registry coverage")
    registry_cmd.add_argument("--registry", default="config/target_companies.json")

    daily_refresh_cmd = subparsers.add_parser(
        "daily-refresh",
        help="One daily command: refresh every automated source safely, then report today's action summary",
    )
    daily_refresh_cmd.add_argument("--registry", default="config/target_companies.json")
    daily_refresh_cmd.add_argument("--config", default="config/personal_strategy.json")
    daily_refresh_cmd.add_argument("--store", default="data/local")
    daily_refresh_cmd.add_argument("--limit", type=int, default=100)
    daily_refresh_cmd.add_argument("--where", default="United Kingdom")

    ats_discover = subparsers.add_parser("ats-discover", help="Discover Greenhouse/Lever links from careers pages")
    ats_discover.add_argument("--registry", default="config/target_companies.json")
    ats_discover.add_argument("--write", action="store_true", help="Persist discovered ATS candidates to the registry")

    ats_verify = subparsers.add_parser("ats-verify", help="Verify discovered Greenhouse/Lever public feeds")
    ats_verify.add_argument("--registry", default="config/target_companies.json")
    ats_verify.add_argument("--write", action="store_true", help="Persist verified enabled connectors to the registry")
    ats_verify.add_argument("--debug", action="store_true", help="Print HTTP verification diagnostics for each ATS candidate")

    ats_check = subparsers.add_parser("ats-check", help="Directly verify one Greenhouse/Lever URL without updating the registry")
    ats_check.add_argument("--url", required=True)

    ats_summary_cmd = subparsers.add_parser("ats-summary", help="Summarise ATS discovery/verification state")
    ats_summary_cmd.add_argument("--registry", default="config/target_companies.json")

    ats_add = subparsers.add_parser("ats-add", help="Manually register and verify a Greenhouse/Lever URL for a company")
    ats_add.add_argument("--company", required=True)
    ats_add.add_argument("--url", required=True)
    ats_add.add_argument("--registry", default="config/target_companies.json")
    ats_add.add_argument("--dry-run", action="store_true", help="Do not persist registry changes")

    ats_import = subparsers.add_parser("ats-import", help="Batch import manual ATS URLs from CSV or JSON")
    ats_import.add_argument("path", help="CSV or JSON containing company_name and ats_url fields")
    ats_import.add_argument("--registry", default="config/target_companies.json")
    ats_import.add_argument("--dry-run", action="store_true", help="Do not persist registry changes")

    ats_unresolved = subparsers.add_parser("ats-unresolved", help="List companies without verified enabled ATS connectors")
    ats_unresolved.add_argument("--registry", default="config/target_companies.json")

    ats_exclude = subparsers.add_parser(
        "ats-exclude",
        help="Manually exclude a company from automatic discovery/verification (persists until explicitly re-included)",
    )
    ats_exclude.add_argument("--company", required=True)
    ats_exclude.add_argument("--reason", required=True, help="Why this company must not be auto-enabled")
    ats_exclude.add_argument("--registry", default="config/target_companies.json")
    ats_exclude.add_argument("--dry-run", action="store_true")

    ats_include = subparsers.add_parser(
        "ats-include",
        help="Explicitly undo a manual exclusion, letting normal discovery/verification pick the company up again",
    )
    ats_include.add_argument("--company", required=True)
    ats_include.add_argument("--registry", default="config/target_companies.json")
    ats_include.add_argument("--dry-run", action="store_true")

    wttj_import = subparsers.add_parser("wttj-import", help="Import Welcome to the Jungle jobs from CSV, JSON, or URL-list files")
    wttj_import.add_argument("path", help="CSV, JSON, or newline-delimited URL file containing WTTJ jobs")
    wttj_import.add_argument("--store", default="data/local")
    wttj_import.add_argument("--config", default="config/personal_strategy.json")
    wttj_import.add_argument("--limit", type=int, default=100)

    wttj_discovery_import = subparsers.add_parser("wttj-discovery-import", help="Bulk import lightweight WTTJ jobs from search/category result exports")
    wttj_discovery_import.add_argument("path", help="CSV or JSON containing lightweight WTTJ discovery rows")
    wttj_discovery_import.add_argument("--store", default="data/local")
    wttj_discovery_import.add_argument("--config", default="config/personal_strategy.json")
    wttj_discovery_import.add_argument("--limit", type=int, default=1000)

    graduate_source_import = subparsers.add_parser(
        "graduate-source-import",
        help="Import jobs from a UK graduate-focused source with no public API (Trackr, Gradcracker, Bright Network, Prospects) from CSV, JSON, or URL-list files",
    )
    graduate_source_import.add_argument("source", choices=sorted(GRADUATE_SOURCE_SPECS))
    graduate_source_import.add_argument("path", help="CSV, JSON, or newline-delimited URL file")
    graduate_source_import.add_argument("--store", default="data/local")
    graduate_source_import.add_argument("--config", default="config/personal_strategy.json")
    graduate_source_import.add_argument("--limit", type=int, default=100)

    graduate_source_discovery_import = subparsers.add_parser(
        "graduate-source-discovery-import",
        help="Bulk import lightweight graduate-source jobs from search/category result exports",
    )
    graduate_source_discovery_import.add_argument("source", choices=sorted(GRADUATE_SOURCE_SPECS))
    graduate_source_discovery_import.add_argument("path", help="CSV or JSON containing lightweight discovery rows")
    graduate_source_discovery_import.add_argument("--store", default="data/local")
    graduate_source_discovery_import.add_argument("--config", default="config/personal_strategy.json")
    graduate_source_discovery_import.add_argument("--limit", type=int, default=1000)

    manual_job_import = subparsers.add_parser(
        "manual-job-import",
        help="Import one specific job you found yourself, by URL, through the same canonical pipeline as every other source",
    )
    manual_job_import.add_argument("--url", required=True, help="The job posting URL you found")
    manual_job_import.add_argument(
        "--source-name",
        default="manual",
        help="Provenance label for this import (e.g. 'manual', 'company-careers-page', 'linkedin-post'). Defaults to 'manual'.",
    )
    manual_job_import.add_argument("--title", help="Overrides/supplies the job title if auto-fetch can't find one")
    manual_job_import.add_argument("--company", help="Overrides/supplies the company name if auto-fetch can't find one")
    manual_job_import.add_argument("--location", help="Overrides/supplies the location if auto-fetch can't find one")
    manual_job_import.add_argument("--description", help="Overrides/supplies the job description if auto-fetch can't find one")
    manual_job_import.add_argument("--deadline", help="Overrides/supplies the application deadline (ISO date/datetime) if auto-fetch can't find one")
    manual_job_import.add_argument("--posted-at", dest="posted_at", help="Overrides/supplies the posted date if auto-fetch can't find one")
    manual_job_import.add_argument("--salary", help="Optional salary text")
    manual_job_import.add_argument("--application-url", dest="application_url", help="Direct application URL, if different from --url")
    manual_job_import.add_argument("--no-auto-fetch", action="store_true", help="Skip attempting to auto-fetch JSON-LD metadata from --url; use only the fields supplied on this command")
    manual_job_import.add_argument("--store", default="data/local")
    manual_job_import.add_argument("--config", default="config/personal_strategy.json")

    github_sync_cmd = subparsers.add_parser("github-sync", help="Incrementally sync public GitHub repositories into the candidate Evidence Bank")
    github_sync_cmd.add_argument("--username", required=True, help="GitHub username to discover repositories for")
    github_sync_cmd.add_argument("--token-env", default="GITHUB_TOKEN", help="Environment variable containing an optional GitHub API token")
    github_sync_cmd.add_argument("--limit", type=int, default=100)
    github_sync_cmd.add_argument("--store", default="data/local")
    github_sync_cmd.add_argument("--name", default="Personal Candidate")
    github_sync_cmd.add_argument("--graduation-year", type=int, default=2026, help="Candidate's actual degree completion year")

    application_create = subparsers.add_parser("application-create", help="Create a new tracked application")
    application_create.add_argument("--job-id", required=True)
    application_create.add_argument("--company", required=True)
    application_create.add_argument("--role-title", required=True)
    application_create.add_argument("--store", default="data/local")

    application_status = subparsers.add_parser("application-status", help="Transition an application's status, recording status history")
    application_status.add_argument("--id", required=True, dest="application_id")
    application_status.add_argument("--status", required=True, choices=[item.value for item in ApplicationStatus])
    application_status.add_argument("--note", default="")
    application_status.add_argument("--submitted-cv", default=None, help="On submission only: the already-attached cv_version_id you actually sent. Omit when you applied without a system CV.")
    application_status.add_argument("--store", default="data/local")

    application_list = subparsers.add_parser("application-list", help="List tracked applications")
    application_list.add_argument("--store", default="data/local")
    application_list.add_argument("--status", choices=[item.value for item in ApplicationStatus])

    application_inspect = subparsers.add_parser("application-inspect", help="Show one application's full status history")
    application_inspect.add_argument("--id", required=True, dest="application_id")
    application_inspect.add_argument("--store", default="data/local")

    cv_generate = subparsers.add_parser("cv-generate", help="Generate a job-specific, evidence-grounded one-page CV")
    cv_generate.add_argument("--job-id", required=True)
    cv_generate.add_argument("--application-id", help="Attach the generated CV to an existing tracked application")
    cv_generate.add_argument("--max-projects", type=int, default=4)
    cv_generate.add_argument("--store", default="data/local")
    cv_generate.add_argument("--config", default="config/personal_strategy.json")
    cv_generate.add_argument("--print-cv", action="store_true", help="Print the generated CV text")

    cv_show = subparsers.add_parser("cv-show", help="Show a previously generated CV artifact by id")
    cv_show.add_argument("--id", required=True, dest="artifact_id")
    cv_show.add_argument("--store", default="data/local")

    import_profile = subparsers.add_parser("profile-import", help="Register a candidate evidence source file")
    import_profile.add_argument("path")
    import_profile.add_argument("--source-type", required=True, choices=[item.value for item in EvidenceSourceType])
    import_profile.add_argument("--title")
    import_profile.add_argument("--store", default="data/local")

    rebuild_profile = subparsers.add_parser("profile-rebuild", help="Rebuild unified candidate profile from imported sources")
    rebuild_profile.add_argument("--name", default=None, help="Candidate name; defaults to the existing profile's name (never silently reset)")
    rebuild_profile.add_argument("--graduation-year", type=int, default=None, help="Candidate's actual degree completion year; defaults to the existing profile's")
    rebuild_profile.add_argument("--store", default="data/local")

    inspect_profile = subparsers.add_parser("profile-inspect", help="Inspect extracted capabilities and evidence")
    inspect_profile.add_argument("--store", default="data/local")
    inspect_profile.add_argument("--role-track", choices=[track.value for track in RoleTrack])
    inspect_profile.add_argument("--limit", type=int, default=50)

    set_contact = subparsers.add_parser("profile-set-contact", help="Set candidate contact/identity details used on generated CVs (never fabricated)")
    set_contact.add_argument("--email")
    set_contact.add_argument("--phone")
    set_contact.add_argument("--location")
    set_contact.add_argument("--linkedin")
    set_contact.add_argument("--github")
    set_contact.add_argument("--store", default="data/local")

    list_cmd = subparsers.add_parser("list-ranked", help="List ranked jobs from local store or fixtures")
    list_cmd.add_argument("--fixtures", action="store_true", help="Use realistic local fixtures")
    list_cmd.add_argument("--store", default="data/local")
    list_cmd.add_argument("--config", default="config/personal_strategy.json")
    list_cmd.add_argument("--role-track", choices=[track.value for track in RoleTrack])
    list_cmd.add_argument("--company")
    list_cmd.add_argument("--source")
    list_cmd.add_argument("--freshness-days", type=float)
    list_cmd.add_argument(
        "--sponsorship",
        choices=[mode.value for mode in SponsorshipFilterMode],
        default=SponsorshipFilterMode.ALL.value,
    )
    list_cmd.add_argument(
        "--location-mode",
        choices=[mode.value for mode in LocationMode],
        default=LocationMode.LONDON_FIRST_UK_WIDE.value,
    )
    list_cmd.add_argument("--limit", type=int, default=20)

    new_jobs = subparsers.add_parser("new-jobs", help="Show jobs first seen since a timestamp")
    new_jobs.add_argument("--since", required=True, help="ISO timestamp, for example 2026-08-20T09:00:00+00:00")
    new_jobs.add_argument("--store", default="data/local")
    new_jobs.add_argument("--config", default="config/personal_strategy.json")
    new_jobs.add_argument("--limit", type=int, default=20)

    validation = subparsers.add_parser("validation-report", help="Write measured Phase 3 validation report")
    validation.add_argument("--store", default="data/local")
    validation.add_argument("--registry", default="config/target_companies.json")
    validation.add_argument("--output", default="docs/PHASE3_VALIDATION.md")

    company_discovery_scan = subparsers.add_parser(
        "company-discovery-scan",
        help="Scan already-ingested jobs for companies not yet in the target-company registry, with provenance",
    )
    company_discovery_scan.add_argument("--store", default="data/local")
    company_discovery_scan.add_argument("--registry", default="config/target_companies.json")

    company_discovery_list = subparsers.add_parser("company-discovery-list", help="List observed non-registry companies awaiting review")
    company_discovery_list.add_argument("--store", default="data/local")

    company_discovery_promote = subparsers.add_parser(
        "company-discovery-promote",
        help="Add one reviewed observed company into the target-company registry (through the normal registry pipeline)",
    )
    company_discovery_promote.add_argument("--company", required=True)
    company_discovery_promote.add_argument("--store", default="data/local")
    company_discovery_promote.add_argument("--registry", default="config/target_companies.json")
    company_discovery_promote.add_argument("--dry-run", action="store_true")

    company_coverage_report = subparsers.add_parser(
        "company-coverage-report",
        help="Classify all target-company registry entries into the five Phase 2.7 coverage states",
    )
    company_coverage_report.add_argument("--registry", default="config/target_companies.json")

    coverage_today_cmd = subparsers.add_parser(
        "coverage-today",
        help="Show exactly what was checked automatically today, what failed/never ran, and what you still need to check yourself",
    )
    coverage_today_cmd.add_argument("--store", default="data/local")
    coverage_today_cmd.add_argument("--registry", default="config/target_companies.json")

    dashboard = subparsers.add_parser("dashboard", help="Run the lightweight personal job-search dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--store", default="data/local")
    dashboard.add_argument("--config", default="config/personal_strategy.json")
    dashboard.add_argument("--registry", default="config/target_companies.json")

    args = parser.parse_args()
    if args.command == "ingest":
        _ingest(args)
    elif args.command == "ingest-registry":
        _ingest_registry(args)
    elif args.command == "registry-summary":
        _registry_summary(args)
    elif args.command == "repair-historical-dedup":
        _repair_historical_dedup(args)
    elif args.command == "daily-refresh":
        _daily_refresh(args)
    elif args.command == "refresh-sources":
        _refresh_sources(args)
    elif args.command == "ats-discover":
        _ats_discover(args)
    elif args.command == "ats-verify":
        _ats_verify(args)
    elif args.command == "ats-check":
        _ats_check(args)
    elif args.command == "ats-summary":
        _ats_summary(args)
    elif args.command == "ats-add":
        _ats_add(args)
    elif args.command == "ats-import":
        _ats_import(args)
    elif args.command == "ats-unresolved":
        _ats_unresolved(args)
    elif args.command == "ats-exclude":
        _ats_exclude(args)
    elif args.command == "ats-include":
        _ats_include(args)
    elif args.command == "wttj-import":
        _wttj_import(args)
    elif args.command == "wttj-discovery-import":
        _wttj_discovery_import(args)
    elif args.command == "graduate-source-import":
        _graduate_source_import(args)
    elif args.command == "graduate-source-discovery-import":
        _graduate_source_discovery_import(args)
    elif args.command == "manual-job-import":
        _manual_job_import(args)
    elif args.command == "github-sync":
        _github_sync(args)
    elif args.command == "application-create":
        _application_create(args)
    elif args.command == "application-status":
        _application_status(args)
    elif args.command == "application-list":
        _application_list(args)
    elif args.command == "application-inspect":
        _application_inspect(args)
    elif args.command == "cv-generate":
        _cv_generate(args)
    elif args.command == "cv-show":
        _cv_show(args)
    elif args.command == "profile-import":
        _profile_import(args)
    elif args.command == "profile-rebuild":
        _profile_rebuild(args)
    elif args.command == "profile-inspect":
        _profile_inspect(args)
    elif args.command == "profile-set-contact":
        _profile_set_contact(args)
    elif args.command == "list-ranked":
        _list_ranked(args)
    elif args.command == "new-jobs":
        _new_jobs(args)
    elif args.command == "company-discovery-scan":
        _company_discovery_scan(args)
    elif args.command == "company-discovery-list":
        _company_discovery_list(args)
    elif args.command == "company-discovery-promote":
        _company_discovery_promote(args)
    elif args.command == "company-coverage-report":
        _company_coverage_report(args)
    elif args.command == "coverage-today":
        _coverage_today(args)
    elif args.command == "validation-report":
        _validation_report(args)
    elif args.command == "dashboard":
        run_dashboard(host=args.host, port=args.port, store_root=args.store, config_path=args.config, registry_path=args.registry)


def _ingest(args: argparse.Namespace) -> None:
    connectors = []
    if args.greenhouse_board:
        connectors.append(GreenhouseConnector(tuple(args.greenhouse_board)))
    if args.lever_site:
        connectors.append(LeverConnector(tuple(args.lever_site)))
    if args.ashby_board:
        connectors.append(AshbyConnector(tuple(args.ashby_board)))
    if args.workable_account:
        connectors.append(WorkableConnector(tuple(args.workable_account)))
    if args.smartrecruiters_company:
        connectors.append(SmartRecruitersConnector(tuple(args.smartrecruiters_company)))
    if args.wttj_org_ref:
        connectors.append(WelcomeToTheJungleConnector(tuple(args.wttj_org_ref), os.getenv(args.wttj_api_key_env)))
    if args.adzuna:
        connectors.append(AdzunaConnector(os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")))
    if args.prospects:
        connectors.append(ProspectsConnector())
    if not connectors:
        raise SystemExit("Configure at least one connector with --greenhouse-board, --lever-site, --ashby-board, --workable-account, --smartrecruiters-company, --wttj-org-ref, --adzuna, or --prospects")

    query = ConnectorQuery(keywords=DEFAULT_TECH_KEYWORDS, location=args.where, limit=args.limit)
    store = LocalJobStore(args.store)
    config = load_ranking_config(args.config)
    # Every connector run through `ingest` goes through the same
    # fetch/health-tracking lifecycle as a registry refresh (`refresh_sources`
    # -> `refresh_connector`) -- a source's actual execution state and its
    # persisted source_health record can never disagree, and `coverage-today`
    # sees an ad-hoc `ingest --adzuna` run exactly like a scheduled one.
    results = [
        refresh_connector(connector, store, config, replace(query, keywords=ADZUNA_GRADUATE_KEYWORDS) if isinstance(connector, AdzunaConnector) else query)
        for connector in connectors
    ]
    _print_refresh_results(results)
    print(f"Ingested {sum(result.canonical_jobs_written for result in results)} canonical job(s) into {args.store}")


def _list_ranked(args: argparse.Namespace) -> None:
    config = replace(load_ranking_config(args.config), location_mode=LocationMode(args.location_mode))
    jobs = sample_jobs() if args.fixtures else LocalJobStore(args.store).read_jobs()
    candidate = load_candidate_profile(args.store)
    ranked = rank_jobs(jobs, candidate=candidate, config=config, now=datetime.now(timezone.utc))
    ranked = [
        _item
        for _item in ranked
        if _passes_filters(
            _item,
            args.role_track,
            SponsorshipFilterMode(args.sponsorship),
            company=args.company,
            source=args.source,
            freshness_days=args.freshness_days,
        )
    ]
    for item in ranked[: args.limit]:
        _print_ranked(item)


def _ingest_registry(args: argparse.Namespace) -> None:
    companies = load_company_registry(args.registry)
    connectors = connectors_from_registry(companies)
    if not connectors:
        raise SystemExit("No enabled supported ATS companies in registry")
    query = ConnectorQuery(keywords=DEFAULT_TECH_KEYWORDS, location=args.where, limit=args.limit)
    store = LocalJobStore(args.store)
    config = load_ranking_config(args.config)
    # Same health-aware path as `refresh-sources` and `ingest` (Phase 2.7.3) --
    # a manual `ingest-registry` run must be just as visible to
    # `coverage-today` as a scheduled `refresh-sources` run, and one
    # company's failure within a multi-company connector (e.g. one bad
    # Greenhouse board) must not be silently absorbed into "0 new jobs" or a
    # falsely-healthy status for the other companies on that same ATS --
    # `refresh_connector`/`PartialFetchError` already guarantee this, and
    # this path now goes through the exact same code, not a re-implementation.
    results = [
        refresh_connector(connector, store, config, replace(query, keywords=ADZUNA_GRADUATE_KEYWORDS) if isinstance(connector, AdzunaConnector) else query)
        for connector in connectors
    ]
    _print_refresh_results(results)
    print(f"Ingested {sum(result.canonical_jobs_written for result in results)} canonical job(s) from registry into {args.store}")


def _registry_summary(args: argparse.Namespace) -> None:
    summary = registry_summary(load_company_registry(args.registry))
    for key, value in summary.items():
        print(f"{key}: {value}")


def _repair_historical_dedup(args: argparse.Namespace) -> None:
    from jobintel.dedup.repair import repair_historical_merges

    report = repair_historical_merges(args.store, args.backups_dir, dry_run=args.dry_run, graduation_year=load_ranking_config(args.config).candidate_graduation_year)
    print(f"{'DRY RUN -- nothing written. ' if report.dry_run else ''}Backup: {report.backup_path or '(none needed)'}")
    print(f"Canonical jobs inspected: {report.inspected} ({report.multi_observation} with more than one observation)")
    print(f"Canonical jobs before/after: {report.jobs_before} -> {report.jobs_after}")
    print(f"Split: {len(report.splits)}; left for manual review: {len(report.ambiguous)}")
    for case in report.cases:
        print(f"[{case.action}] {case.original_id}: {case.reason}")
        for job_id, title, observations in case.groups:
            print(f"    -> {job_id} | {title} | {', '.join(observations)}")


def _daily_refresh(args: argparse.Namespace) -> None:
    report = run_daily_refresh(
        store=LocalJobStore(args.store),
        config=load_ranking_config(args.config),
        registry_path=args.registry,
        where=args.where,
        limit=args.limit,
    )
    print(f"Daily refresh: {report.started_at.isoformat()} -> {report.finished_at.isoformat()}")
    print()
    _print_refresh_results(report.source_results)
    print()
    print(f"Sources succeeded: {len(report.sources_succeeded)} ({', '.join(report.sources_succeeded) or 'none'})")
    print(f"Sources partial: {len(report.sources_partial)} ({', '.join(report.sources_partial) or 'none'})")
    print(f"Sources failed: {len(report.sources_failed)} ({', '.join(report.sources_failed) or 'none'})")
    print(f"Sources not configured: {len(report.sources_not_configured)} ({', '.join(report.sources_not_configured) or 'none'})")
    print(f"Sources succeeded with zero results: {', '.join(report.sources_zero_results) or 'none'}")
    print(f"Manual-only (never automated): {', '.join(report.manual_only_sources) or 'none'}")
    for stage in report.stages:
        print(f"Stage {stage.name}: {stage.outcome} -- {stage.detail}")
    print()
    print(f"Overall: {report.status}")
    print(f"New canonical jobs this refresh: {report.new_jobs}")
    print(f"Changed observations this refresh: {report.changed_jobs}")
    print(f"Jobs closed during this refresh: {report.closed_jobs} ({report.closed_observations} source observation(s))")
    print(f"High-priority jobs: {report.high_priority_jobs}")
    print(f"Manual checks due: {report.manual_checks_due}")


def _print_refresh_results(results: list) -> None:
    for result in results:
        marker = "OK" if result.status == "live_api" else "FAILED" if result.status in {"error", "auth_required"} else "PARTIAL"
        print(f"{result.source_name}: {marker} ({result.status})")
        print(f"       jobs_seen: {result.jobs_seen} | jobs_active: {result.jobs_active} | canonical_written: {result.canonical_jobs_written}")
        if result.error:
            print(f"       error: {result.error}")
        if result.failed_identifiers:
            print(f"       failed identifiers: {', '.join(result.failed_identifiers)} (coverage incomplete -- their jobs can't be closed this cycle)")
        if getattr(result, "incomplete_scopes", None):
            print(f"       not fully observed (no closure inferred): {'; '.join(f'{scope}: {reason}' for scope, reason in result.incomplete_scopes.items())}")
        if getattr(result, "closed_observations", None):
            print(f"       closed this refresh: {len(result.closed_observations)}")
        if getattr(result, "closure_withheld", None):
            print(f"       {result.closure_withheld}")
        if result.warning:
            print(f"       WARNING: {result.warning}")


def _refresh_sources(args: argparse.Namespace) -> None:
    results = refresh_sources(
        registry_path=args.registry,
        store=LocalJobStore(args.store),
        config=load_ranking_config(args.config),
        limit=args.limit,
        where=args.where,
    )
    _print_refresh_results(results)


def _ats_discover(args: argparse.Namespace) -> None:
    entries = discover_registry(args.registry, write=args.write)
    _print_ats_entries(entries)
    if not args.write:
        print("Dry run only. Re-run with --write to update the registry.")


def _ats_verify(args: argparse.Namespace) -> None:
    entries = verify_registry(args.registry, write=args.write)
    _print_ats_entries(entries)
    if args.debug:
        _print_ats_debug(entries)
    if not args.write:
        print("Dry run only. Re-run with --write to update the registry.")


def _ats_check(args: argparse.Namespace) -> None:
    verification = check_ats_url(args.url)
    candidate = verification.candidate
    print(f"ATS: {candidate.connector_type}")
    print(f"Token: {candidate.connector_token}")
    print(f"Status: {verification.status}")
    print(f"Jobs available: {verification.jobs_available if verification.jobs_available is not None else 'unknown'}")
    print(f"Requested URL: {verification.requested_url}")
    print(f"Failure kind: {verification.failure_kind or ''}")
    print(f"Exception type: {verification.exception_type or ''}")
    print(f"HTTP status: {verification.http_status_code if verification.http_status_code is not None else ''}")
    print(f"Message: {verification.message}")


def _ats_summary(args: argparse.Namespace) -> None:
    with open(args.registry, encoding="utf-8") as handle:
        entries = json.load(handle)
    for key, value in ats_summary(entries).items():
        print(f"{key}: {value}")


def _company_discovery_scan(args: argparse.Namespace) -> None:
    store = LocalJobStore(args.store)
    registry_entries = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    discovery_store = CompanyDiscoveryStore(args.store)
    new_observations = observe_companies_from_jobs(store.read_jobs(), registry_entries, discovery_store)
    print(f"New companies observed this scan: {len(new_observations)}")
    for observation in new_observations:
        suggestion = f" (suggested: {observation.suggested_connector_type}/{observation.suggested_connector_token})" if observation.suggested_connector_type else ""
        print(f"  {observation.company_name} -- via {observation.observed_via_source}{suggestion}")


def _company_discovery_list(args: argparse.Namespace) -> None:
    discovery_store = CompanyDiscoveryStore(args.store)
    latest = discovery_store.latest_per_company()
    print(f"Observed non-registry companies awaiting review: {len(latest)}")
    for observation in latest.values():
        suggestion = f"{observation.suggested_connector_type}/{observation.suggested_connector_token}" if observation.suggested_connector_type else "(none found)"
        print(f"  {observation.company_name} | via {observation.observed_via_source} | suggested ATS: {suggestion} | example: {observation.example_job_title!r}")


def _company_discovery_promote(args: argparse.Namespace) -> None:
    discovery_store = CompanyDiscoveryStore(args.store)
    latest = discovery_store.latest_per_company()
    observation = latest.get(args.company.casefold())
    if observation is None:
        print(f"No observed candidate named {args.company!r} found. Run company-discovery-list to see what's available.")
        return
    result = promote_candidate(observation, args.registry, write=not args.dry_run)
    print(f"Promoted {observation.company_name}: verification_status={result.get('verification_status')}, enabled={result.get('enabled')}")
    if args.dry_run:
        print("Dry run only. Re-run without --dry-run to update the registry.")


def _company_coverage_report(args: argparse.Namespace) -> None:
    entries = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    results = classify_registry(entries)
    metrics = coverage_metrics(results)
    print(f"Total target companies: {metrics['total_companies']}")
    for state, count in metrics["counts"].items():
        print(f"  {state}: {count}")
    print(f"Automatic verified coverage of this {metrics['total_companies']}-company registry: {metrics['automatic_verified_coverage_of_registry']:.1%}")
    print(f"Automatic-or-partial coverage of this {metrics['total_companies']}-company registry: {metrics['automatic_or_partial_coverage_of_registry']:.1%}")
    print("(These percentages describe only the target-company registry, not the UK graduate job market as a whole.)")
    print()
    watchlist = manual_watchlist(results)
    print(f"Manual watchlist ({len(watchlist)} companies):")
    for row in watchlist:
        print(f"  {row['company']} | {row['state']} | check {row['recommended_manual_check_frequency']} | {row['careers_url']}")


def _coverage_today(args: argparse.Namespace) -> None:
    report = coverage_today(LocalJobStore(args.store), registry_path=args.registry)
    print(f"Coverage as of {report.generated_at.isoformat()}")
    print()
    print(f"=== AUTOMATICALLY CHECKED TODAY ({len(report.automatically_checked_today)}) ===")
    if not report.automatically_checked_today:
        print("  (none)")
    for entry in report.automatically_checked_today:
        print(f"  {entry.display_name}: {entry.detail}")
    print()
    print(f"=== FAILED / INCOMPLETE TODAY ({len(report.failed_or_incomplete_today)}) ===")
    if not report.failed_or_incomplete_today:
        print("  (none)")
    for entry in report.failed_or_incomplete_today:
        print(f"  {entry.display_name}: {entry.detail}")
    print()
    print(f"=== MANUAL CHECK STILL REQUIRED ({len(report.manual_check_still_required)}) ===")
    for entry in report.manual_check_still_required:
        print(f"  {entry.display_name}: {entry.detail}")


def _ats_add(args: argparse.Namespace) -> None:
    result = add_manual_ats_url(args.company, args.url, args.registry, write=not args.dry_run)
    _print_ats_entries([result])
    if args.dry_run:
        print("Dry run only. Re-run without --dry-run to update the registry.")


def _ats_import(args: argparse.Namespace) -> None:
    results = import_manual_ats_urls(args.path, args.registry, write=not args.dry_run)
    _print_ats_entries(results)
    if args.dry_run:
        print("Dry run only. Re-run without --dry-run to update the registry.")


def _ats_unresolved(args: argparse.Namespace) -> None:
    with open(args.registry, encoding="utf-8") as handle:
        entries = json.load(handle)
    for entry in unresolved_entries(entries):
        print(f"Company: {entry.get('company_name')}")
        print(f"Careers URL: {entry.get('careers_url') or ''}")
        print(f"Status: {entry.get('verification_status') or 'unknown'}")
        print(f"ATS: {entry.get('connector_type') or entry.get('source_connector_type') or 'unknown'}")
        print(f"Token: {entry.get('connector_token') or entry.get('greenhouse_board_token') or entry.get('lever_site_token') or ''}")


def _ats_exclude(args: argparse.Namespace) -> None:
    result = exclude_company(args.company, args.reason, args.registry, write=not args.dry_run)
    print(f"Excluded {result['company_name']}: reason={result['exclusion_reason']!r} at {result['excluded_at']}")
    if args.dry_run:
        print("Dry run only. Re-run without --dry-run to update the registry.")


def _ats_include(args: argparse.Namespace) -> None:
    result = reinclude_company(args.company, args.registry, write=not args.dry_run)
    print(f"Re-included {result['company_name']}: verification_status={result['verification_status']} (will be re-discovered on the next ats-discover/verify run)")
    if args.dry_run:
        print("Dry run only. Re-run without --dry-run to update the registry.")


def _wttj_import(args: argparse.Namespace) -> None:
    prepared = prepare_wttj_manual_import(args.path)
    _run_wttj_prepared_import(args, prepared)


def _wttj_discovery_import(args: argparse.Namespace) -> None:
    prepared = prepare_wttj_discovery_import(args.path)
    _run_wttj_prepared_import(args, prepared)


def _graduate_source_import(args: argparse.Namespace) -> None:
    spec = GRADUATE_SOURCE_SPECS[args.source]
    prepared = prepare_manual_import(spec, args.path)
    _run_graduate_source_prepared_import(args, spec, prepared, discovery=False)


def _graduate_source_discovery_import(args: argparse.Namespace) -> None:
    spec = GRADUATE_SOURCE_SPECS[args.source]
    prepared = prepare_discovery_import(spec, args.path)
    _run_graduate_source_prepared_import(args, spec, prepared, discovery=True)


def _run_graduate_source_prepared_import(args: argparse.Namespace, spec, prepared, discovery: bool) -> None:
    records = [item.record for item in prepared if item.status == "imported" and item.record is not None]
    store = LocalJobStore(args.store)
    connector = ManualSourceDiscoveryConnector(spec, tuple(records)) if discovery else ManualSourceConnector(spec, tuple(records))
    jobs = ingest_from_connectors(
        [connector],
        query=ConnectorQuery(keywords=(), location="United Kingdom", limit=args.limit),
        store=store,
        config=load_ranking_config(args.config),
    )
    for item in prepared:
        if item.status != "imported":
            print(f"row {item.row_number}: {item.url or '(missing url)'} | {item.status} | {item.error}")
    imported = len(records)
    failed = len(prepared) - imported
    canonical = len([job for job in store.read_jobs() if any(observation.source_name == spec.source_name for observation in job.source_observations)])
    print(f"Rows supplied: {len(prepared)}")
    print(f"Imported: {imported}")
    print(f"Failed: {failed}")
    print(f"Canonical {spec.display_name} jobs in store: {canonical}")
    print(f"Pipeline returned canonical {spec.display_name} batch jobs: {len(jobs)}")


def _manual_job_import(args: argparse.Namespace) -> None:
    overrides = {
        "title": args.title,
        "company": args.company,
        "location": args.location,
        "description": args.description,
        "deadline": args.deadline,
        "posted_at": args.posted_at,
        "salary": args.salary,
        "application_url": args.application_url,
    }
    overrides = {key: value for key, value in overrides.items() if value}
    result = prepare_single_url_import(args.url, overrides, skip_auto_fetch=args.no_auto_fetch)

    print(f"URL: {result.url}")
    print(f"Status: {result.status}")
    if result.auto_fetched_fields:
        print(f"Auto-fetched fields (from the page's own JobPosting structured data): {', '.join(result.auto_fetched_fields)}")
    if result.fetch_error:
        print(f"Auto-fetch note: {result.fetch_error}")
    if result.status == "missing_required_fields":
        print("Not imported -- supply --title and --company (auto-fetch could not find them and they were not given).")
        return

    spec = ManualSourceSpec(source_name=args.source_name, display_name=args.source_name, is_source_url=lambda _url: True)
    store = LocalJobStore(args.store)
    connector = ManualSourceConnector(spec, (result.record,))
    jobs = ingest_from_connectors(
        [connector],
        query=ConnectorQuery(keywords=(), location="United Kingdom", limit=1),
        store=store,
        config=load_ranking_config(args.config),
    )
    print(f"Title: {result.record.get('title')}")
    print(f"Company: {result.record.get('company')}")
    print(f"Imported through the canonical pipeline (normalise -> enrich -> dedup -> store), same as every other source.")
    print(f"Pipeline returned {len(jobs)} job(s) this run.")


def _github_sync(args: argparse.Namespace) -> None:
    result = sync_repositories(
        args.store,
        args.username,
        token=os.getenv(args.token_env),
        limit=args.limit,
        name=args.name,
        graduation_year=args.graduation_year,
    )
    print(f"Repositories discovered: {result.repositories_discovered}")
    print(f"Repositories synced (new/changed): {result.repositories_synced}")
    print(f"Repositories unchanged (skipped): {result.repositories_unchanged}")
    print(f"Capabilities extracted: {result.capabilities_extracted}")
    for name in result.synced_repository_names:
        print(f"  synced: {name}")


def _application_create(args: argparse.Namespace) -> None:
    store = ApplicationStore(args.store)
    application_id = f"{args.company}:{args.role_title}:{args.job_id}".casefold().replace(" ", "-")
    application = Application(id=application_id, job_id=args.job_id, company=args.company, role_title=args.role_title)
    stored = store.create_application(application)
    if stored.id != application_id:
        print(f"Job {args.job_id} already has application {stored.id} ({stored.status.value}) -- one application per vacancy; nothing created")
        return
    print(f"Created application {application_id} with status {application.status.value}")


def _application_status(args: argparse.Namespace) -> None:
    store = ApplicationStore(args.store)
    try:
        was_submitted = next((item.is_submitted for item in store.read_applications() if item.id == args.application_id), False)
        updated = store.update_status(args.application_id, ApplicationStatus(args.status), note=args.note, submitted_cv_version_id=args.submitted_cv)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print(f"Application {args.application_id}: {updated.status.value}")
    if updated.is_submitted and not was_submitted:
        print(f"Submitted with system CV {updated.submitted_cv_version_id}" if updated.submitted_cv_version_id else "Submitted with NO system CV attached (manual application)")


def _application_list(args: argparse.Namespace) -> None:
    store = ApplicationStore(args.store)
    applications = store.read_applications()
    if args.status:
        applications = [application for application in applications if application.status.value == args.status]
    for application in applications:
        print(f"{application.id} | {application.company} | {application.role_title} | {application.status.value}")
    print(f"Total: {len(applications)}")


def _application_inspect(args: argparse.Namespace) -> None:
    store = ApplicationStore(args.store)
    applications = {application.id: application for application in store.read_applications()}
    application = applications.get(args.application_id)
    if application is None:
        raise SystemExit(f"No application with id {args.application_id}")
    print(f"{application.id} | {application.company} | {application.role_title} | current status: {application.status.value}")
    print(f"job_id: {application.job_id}")
    print(f"cv_version_id: {application.cv_version_id or '(none generated yet)'}")
    if application.is_submitted:
        print(f"submitted: yes; system CV submitted: {application.submitted_cv_version_id or 'none -- no system CV attached (manual application)'}")
    else:
        print("submitted: no")
    if application.selected_project_ids:
        print(f"selected_project_ids: {', '.join(application.selected_project_ids)}")
    print(f"notes: {application.notes}")
    print("status history:")
    for event in store.status_history_for(args.application_id):
        print(f"  {event.occurred_at.isoformat()}: {event.from_status.value if event.from_status else '(created)'} -> {event.to_status.value}{f' ({event.note})' if event.note else ''}")


def _cv_generate(args: argparse.Namespace) -> None:
    store = LocalJobStore(args.store)
    jobs = {job.id: job for job in store.read_jobs()}
    job = jobs.get(args.job_id)
    if job is None:
        raise SystemExit(f"No job with id {args.job_id} in {args.store}")
    # LocalJobStore.read_jobs() returns jobs as persisted, without re-running
    # enrichment (skill_requirements/graduation_year/etc. are computed, not stored
    # verbatim) -- re-enrich here the same way rank_jobs() does before matching.
    config = load_ranking_config(args.config)
    job = enrich_job(job, config.candidate_graduation_year)
    candidate = load_candidate_profile(args.store)
    if candidate is None:
        raise SystemExit(f"No candidate profile found in {args.store} -- run profile-rebuild first")

    artifact = generate_and_save_cv(candidate, job, CVArtifactStore(args.store), max_projects=args.max_projects, application_id=args.application_id, store_root=args.store)

    if args.application_id:
        try:
            ApplicationStore(args.store).attach_cv(args.application_id, artifact.id, artifact.selected_project_ids, artifact.evidence_ids_used)
        except ValueError as exc:
            print(f"NOT attached: {exc}")

    print(f"Generated CV artifact: {artifact.id}")
    print(f"Job: {job.title} at {job.company} ({job.id})")
    print(f"Selected projects: {', '.join(artifact.selected_project_ids) or '(none)'}")
    print(f"Skills included: {', '.join(artifact.skills_included) or '(none)'}")
    print(f"Rendered PDF: {artifact.pdf_path}")
    print(f"Page count: {artifact.page_count} (fits one page: {artifact.fits_one_page}, margin: {artifact.render_margin_mm}mm, font: {artifact.render_body_pt}pt)")
    print(f"Review status: {artifact.review_status.value}")
    for reason in artifact.review_reasons:
        print(f"  - {reason}")
    if args.application_id:
        print(f"Attached to application: {args.application_id}")
    if args.print_cv:
        print("\n--- CV TEXT ---")
        print(artifact.cv_text)


def _cv_show(args: argparse.Namespace) -> None:
    artifact = CVArtifactStore(args.store).get(args.artifact_id)
    if artifact is None:
        raise SystemExit(f"No CV artifact with id {args.artifact_id}")
    print(f"{artifact.id} | job {artifact.job_id} | generated {artifact.generated_at.isoformat()} | review: {artifact.review_status.value}")
    print(f"Selected projects: {', '.join(artifact.selected_project_ids) or '(none)'}")
    print(f"Evidence used: {', '.join(artifact.evidence_ids_used) or '(none)'}")
    print(f"Rendered PDF: {artifact.pdf_path} ({artifact.page_count} page(s))")
    print("\n--- CV TEXT ---")
    print(artifact.cv_text)


def _run_wttj_prepared_import(args: argparse.Namespace, prepared) -> None:
    records = [item.record for item in prepared if item.status == "imported" and item.record is not None]
    supplied_urls = [item.url for item in prepared if item.url]
    store = LocalJobStore(args.store)
    before_jobs = store.read_jobs()
    before_canonical_ids = {job.id for job in before_jobs}
    before_observations = _wttj_observations_by_source_id(before_jobs)
    connector = WelcomeToTheJungleManualConnector(tuple(records))
    jobs = ingest_from_connectors(
        [connector],
        query=ConnectorQuery(keywords=(), location="United Kingdom", limit=args.limit),
        store=store,
        config=load_ranking_config(args.config),
    )
    after_jobs = store.read_jobs()
    summary = _wttj_import_summary(prepared, after_jobs, before_canonical_ids, before_observations)
    for item in prepared:
        if item.status != "imported":
            print(f"row {item.row_number}: {item.url or '(missing url)'} | {item.status} | {item.error}")
    for row_number, url, status, detail in summary["row_statuses"]:
        suffix = f" | {detail}" if detail else ""
        print(f"row {row_number}: {url} | {status}{suffix}")
    print(f"Rows supplied: {len(prepared)}")
    print(f"URLs supplied: {len(supplied_urls)}")
    print(f"Imported: {summary['imported']}")
    print(f"Updated: {summary['updated']}")
    print(f"Deduplicated: {summary['deduplicated']}")
    print(f"Failed: {summary['failed']}")
    print(f"Missing description: {summary['missing_description']}")
    print(f"Missing posted date: {summary['missing_posted_date']}")
    print(f"Missing direct apply URL: {summary['missing_direct_apply_url']}")
    print(f"Canonical WTTJ jobs: {summary['canonical_wttj_jobs']}")
    print(f"WTTJ observations retained: {summary['wttj_observations_retained']}")
    print(f"Pipeline returned canonical WTTJ batch jobs: {len(jobs)}")


def _print_ats_entries(entries) -> None:
    for entry in entries:
        if not entry.get("connector_type") and not entry.get("connector_token") and entry.get("verification_status") not in {"verified", "discovered_pending_verification", "invalid_token"}:
            continue
        print(f"Company: {entry.get('company_name')}")
        print(f"ATS: {entry.get('connector_type') or 'unknown'}")
        print(f"Token: {entry.get('connector_token') or ''}")
        print(f"Status: {entry.get('verification_status')}")
        print(f"Jobs available: {entry.get('jobs_available') if entry.get('jobs_available') is not None else 'unknown'}")
        print(f"Evidence URL: {entry.get('verification_url') or ''}")


def _wttj_import_summary(prepared, after_jobs, before_canonical_ids: set[str], before_observations: dict[tuple[str, str], dict] | None = None) -> dict:
    before_observations = before_observations or {}
    retained_by_url = {}
    for job in after_jobs:
        for observation in job.source_observations:
            if observation.source_name != "welcome_to_the_jungle":
                continue
            retained_by_url[observation.original_url] = job
    row_statuses = []
    imported = 0
    updated = 0
    deduplicated = 0
    failed = len([item for item in prepared if item.status != "imported"])
    missing_description = 0
    missing_posted_date = 0
    missing_direct_apply_url = 0
    for item in prepared:
        if item.status != "imported":
            continue
        record = item.record or {}
        source_id = str(record.get("reference") or record.get("source_job_id") or record.get("job_id") or "")
        if not record.get("description") and not record.get("requirements"):
            missing_description += 1
        if not record.get("posted_at") and not record.get("published_at") and not record.get("created_at"):
            missing_posted_date += 1
        imported_payload = record.get("_imported_payload") if isinstance(record.get("_imported_payload"), dict) else record
        if not imported_payload.get("direct_apply_url") and not imported_payload.get("apply_url") and not imported_payload.get("application_url"):
            missing_direct_apply_url += 1
        job = retained_by_url.get(item.url)
        if job is None:
            failed += 1
            row_statuses.append((item.row_number, item.url, "missing_required_fields", "WTTJ observation not retained after import"))
            continue
        source_key = ("welcome_to_the_jungle", source_id)
        if source_key in before_observations:
            updated += 1
            changes = _changed_wttj_fields(before_observations[source_key], record)
            detail = f"changed: {', '.join(changes)}" if changes else "unchanged"
            row_statuses.append((item.row_number, item.url, "updated", detail))
        elif job.id in before_canonical_ids and any(observation.source_name != "welcome_to_the_jungle" for observation in job.source_observations):
            deduplicated += 1
            row_statuses.append((item.row_number, item.url, "duplicate", "merged with existing employer/ATS vacancy"))
        else:
            imported += 1
            row_statuses.append((item.row_number, item.url, "imported", ""))
    canonical_wttj_jobs = len([job for job in after_jobs if any(observation.source_name == "welcome_to_the_jungle" for observation in job.source_observations)])
    wttj_observations = sum(1 for job in after_jobs for observation in job.source_observations if observation.source_name == "welcome_to_the_jungle")
    return {
        "row_statuses": row_statuses,
        "imported": imported,
        "updated": updated,
        "deduplicated": deduplicated,
        "failed": failed,
        "missing_description": missing_description,
        "missing_posted_date": missing_posted_date,
        "missing_direct_apply_url": missing_direct_apply_url,
        "canonical_wttj_jobs": canonical_wttj_jobs,
        "wttj_observations_retained": wttj_observations,
    }


def _wttj_observations_by_source_id(jobs) -> dict[tuple[str, str], dict]:
    observations = {}
    for job in jobs:
        for observation in job.source_observations:
            if observation.source_name == "welcome_to_the_jungle":
                observations[(observation.source_name, observation.source_job_id)] = {
                    "title": job.title,
                    "location": job.raw_location,
                    "description": observation.raw_description,
                    "apply_url": observation.canonical_application_url,
                }
    return observations


def _changed_wttj_fields(before: dict, record: dict) -> list[str]:
    comparisons = {
        "title": str(record.get("name") or record.get("title") or ""),
        "location": str(record.get("location") or record.get("raw_location") or ""),
        "description": "\n".join(str(record.get(key) or "") for key in ("description", "requirements")),
        "apply_url": str(record.get("apply_url") or record.get("application_url") or record.get("direct_apply_url") or ""),
    }
    changes = []
    for key, value in comparisons.items():
        if value and str(before.get(key) or "").strip() != value.strip():
            changes.append(key)
    return changes


def _print_ats_debug(entries) -> None:
    for entry in entries:
        if not entry.get("connector_type") and not entry.get("connector_token"):
            continue
        print(f"Debug: {entry.get('company_name')}")
        print(f"  requested_url: {entry.get('verification_requested_url') or ''}")
        print(f"  failure_kind: {entry.get('verification_failure_kind') or ''}")
        print(f"  exception_type: {entry.get('verification_exception_type') or ''}")
        print(f"  http_status: {entry.get('verification_http_status_code') if entry.get('verification_http_status_code') is not None else ''}")
        print(f"  message: {entry.get('ats_verification_error') or entry.get('verification_message') or ''}")


def _profile_import(args: argparse.Namespace) -> None:
    manifest = add_profile_source(args.store, args.path, EvidenceSourceType(args.source_type), args.title)
    print(f"Registered profile evidence source in {manifest}")


def _profile_rebuild(args: argparse.Namespace) -> None:
    # A rebuild must never silently replace the real name/graduation year on
    # every future CV with a placeholder: default to what the profile has.
    existing = load_candidate_profile(args.store)
    name = args.name or (existing.name if existing else "Personal Candidate")
    graduation_year = args.graduation_year or (existing.graduation_year if existing else 2026)
    profile = build_candidate_profile(args.store, name, graduation_year)
    print(f"Profile name: {profile.name}; graduation year: {profile.graduation_year}")
    print(f"Built profile with {len(profile.capabilities)} capabilities, {len(profile.projects)} projects, {len(profile.cv_versions)} CV versions")


def _profile_inspect(args: argparse.Namespace) -> None:
    profile = load_candidate_profile(args.store)
    if profile is None:
        raise SystemExit("No candidate profile found. Run profile-import and profile-rebuild first.")
    capabilities = list(profile.capabilities.values())
    if args.role_track:
        track = RoleTrack(args.role_track)
        capabilities = [capability for capability in capabilities if capability.role_relevance.get(track, 0) > 0]
        capabilities.sort(key=lambda capability: capability.role_relevance.get(track, 0), reverse=True)
    else:
        capabilities.sort(key=lambda capability: capability.confidence, reverse=True)
    for capability in capabilities[: args.limit]:
        tracks = sorted((track.value, score) for track, score in capability.role_relevance.items() if score > 0)
        print(f"{capability.name} | confidence={capability.confidence:.2f} | category={capability.category.value}")
        print(f"       tracks: {tracks[:5]}")
        for evidence in capability.evidence[:3]:
            print(f"       evidence: {evidence.source.title} - {evidence.quote[:180]}")


def _profile_set_contact(args: argparse.Namespace) -> None:
    path = set_contact_details(
        args.store,
        email=args.email,
        phone=args.phone,
        location=args.location,
        linkedin_url=args.linkedin,
        github_url=args.github,
    )
    print(f"Contact details saved to {path}")
    print(f"Run `PYTHONPATH=src python3 -m jobintel.cli.main profile-rebuild --store {args.store}` to apply them to the candidate profile (your existing name and graduation year are kept).")


def _new_jobs(args: argparse.Namespace) -> None:
    since = datetime.fromisoformat(args.since)
    store = LocalJobStore(args.store)
    config = load_ranking_config(args.config)
    candidate = load_candidate_profile(args.store)
    ranked = rank_jobs(store.jobs_first_seen_since(since), candidate=candidate, config=config, now=datetime.now(timezone.utc))
    for item in ranked[: args.limit]:
        _print_ranked(item)


def _validation_report(args: argparse.Namespace) -> None:
    report = build_validation_report(LocalJobStore(args.store), args.registry)
    path = write_validation_report(report, args.output)
    print(f"Wrote validation report to {path}")


def _passes_filters(
    item,
    role_track: str | None,
    sponsorship_mode: SponsorshipFilterMode,
    company: str | None = None,
    source: str | None = None,
    freshness_days: float | None = None,
) -> bool:
    tracks = {track.value for track in item.match.primary_role_tracks + item.match.secondary_role_tracks}
    if role_track and role_track not in tracks:
        return False
    state = item.job.sponsorship.state if item.job.sponsorship else SponsorshipState.UNKNOWN
    if sponsorship_mode == SponsorshipFilterMode.SPONSOR_ONLY:
        if state not in {SponsorshipState.EXPLICIT_SPONSOR, SponsorshipState.LIKELY_SPONSOR}:
            return False
    elif sponsorship_mode == SponsorshipFilterMode.NO_SPONSOR_ONLY:
        if state not in {SponsorshipState.EXPLICIT_NO_SPONSOR, SponsorshipState.LIKELY_NO_SPONSOR}:
            return False
    if company and company.casefold() not in item.job.company.casefold():
        return False
    if source and source.casefold() not in {observation.source_name.casefold() for observation in item.job.source_observations}:
        return False
    if freshness_days is not None and item.job.posted_at is not None:
        age_days = (datetime.now(timezone.utc) - item.job.posted_at).total_seconds() / 86400.0
        if age_days > freshness_days:
            return False
    return True


def _print_ranked(item) -> None:
    job = item.job
    match = item.match
    source_names = ", ".join(observation.source_name for observation in job.source_observations)
    location = "; ".join(location.raw or f"{location.city}, {location.country}" for location in job.locations)
    work_modes = ", ".join(sorted({location.work_mode.value for location in job.locations}))
    posted = job.posted_at.isoformat() if job.posted_at else "unknown"
    first_seen = min((observation.first_seen_at for observation in job.source_observations), default=None)
    tech = next(component.score for component in match.components if component.name == "technical_match")
    wttj_states = [
        observation.raw_payload.get("_enrichment_state")
        for observation in job.source_observations
        if observation.source_name == "welcome_to_the_jungle"
    ]
    print(f"{match.overall_priority:>6} | {job.title} | {job.company}")
    print(f"       location: {location} | work_mode: {work_modes}")
    print(f"       source: {source_names} | posted: {posted} | first_seen: {first_seen.isoformat() if first_seen else 'unknown'}")
    print(f"       tracks: {[track.value for track in match.primary_role_tracks + match.secondary_role_tracks]}")
    print(f"       sponsorship: {(job.sponsorship.state.value if job.sponsorship else 'unknown')}")
    print(f"       graduation: {(job.graduation_year.state.value if job.graduation_year else 'unknown')}")
    print(f"       technical_fit: {tech} | cv: {match.best_existing_cv_category} | hybrid: {match.hybrid_cv_recommended}")
    if wttj_states:
        print(f"       enrichment: {', '.join(str(state) for state in wttj_states)}{' | Needs JD enrichment' if 'discovery_only' in wttj_states else ''}")
    print(f"       evidence: {match.strongest_supporting_evidence[:2]}")
    print(f"       weaknesses: {match.missing_or_weak_evidence[:5]}")
    print(f"       explanation: {match.explanation[:220]}")
    print(f"       apply: {job.canonical_application_url}")


if __name__ == "__main__":
    main()
