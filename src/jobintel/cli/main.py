from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import datetime, timezone

from jobintel.company_registry import connectors_from_registry, load_company_registry, registry_summary
from jobintel.ats_discovery import (
    add_manual_ats_url,
    ats_summary,
    check_ats_url,
    discover_registry,
    import_manual_ats_urls,
    unresolved_entries,
    verify_registry,
)
from jobintel.config import default_ranking_config, load_ranking_config
from jobintel.dashboard.server import run_dashboard
from jobintel.connectors.adzuna import AdzunaConnector
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
from jobintel.fixtures.sample_data import sample_jobs
from jobintel.models.taxonomy import LocationMode, RoleTrack, SponsorshipFilterMode, SponsorshipState
from jobintel.profile_ingestion import add_profile_source, build_candidate_profile, load_candidate_profile
from jobintel.pipeline.ingestion import DEFAULT_TECH_KEYWORDS, ingest_from_connectors, rank_jobs
from jobintel.pipeline.refresh import refresh_sources
from jobintel.storage.local_store import LocalJobStore
from jobintel.models.taxonomy import EvidenceSourceType
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

    registry_cmd = subparsers.add_parser("registry-summary", help="Inspect target-company registry coverage")
    registry_cmd.add_argument("--registry", default="config/target_companies.json")

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

    import_profile = subparsers.add_parser("profile-import", help="Register a candidate evidence source file")
    import_profile.add_argument("path")
    import_profile.add_argument("--source-type", required=True, choices=[item.value for item in EvidenceSourceType])
    import_profile.add_argument("--title")
    import_profile.add_argument("--store", default="data/local")

    rebuild_profile = subparsers.add_parser("profile-rebuild", help="Rebuild unified candidate profile from imported sources")
    rebuild_profile.add_argument("--name", default="Personal Candidate")
    rebuild_profile.add_argument("--graduation-year", type=int, default=2026)
    rebuild_profile.add_argument("--store", default="data/local")

    inspect_profile = subparsers.add_parser("profile-inspect", help="Inspect extracted capabilities and evidence")
    inspect_profile.add_argument("--store", default="data/local")
    inspect_profile.add_argument("--role-track", choices=[track.value for track in RoleTrack])
    inspect_profile.add_argument("--limit", type=int, default=50)

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
    elif args.command == "wttj-import":
        _wttj_import(args)
    elif args.command == "wttj-discovery-import":
        _wttj_discovery_import(args)
    elif args.command == "profile-import":
        _profile_import(args)
    elif args.command == "profile-rebuild":
        _profile_rebuild(args)
    elif args.command == "profile-inspect":
        _profile_inspect(args)
    elif args.command == "list-ranked":
        _list_ranked(args)
    elif args.command == "new-jobs":
        _new_jobs(args)
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
    if not connectors:
        raise SystemExit("Configure at least one connector with --greenhouse-board, --lever-site, --ashby-board, --workable-account, --smartrecruiters-company, --wttj-org-ref, or --adzuna")

    query = ConnectorQuery(keywords=DEFAULT_TECH_KEYWORDS, location=args.where, limit=args.limit)
    jobs = ingest_from_connectors(connectors, query=query, store=LocalJobStore(args.store), config=load_ranking_config(args.config))
    print(f"Ingested {len(jobs)} canonical job(s) into {args.store}")


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
    jobs = ingest_from_connectors(connectors, query=query, store=LocalJobStore(args.store), config=load_ranking_config(args.config))
    print(f"Ingested {len(jobs)} canonical job(s) from registry into {args.store}")


def _registry_summary(args: argparse.Namespace) -> None:
    summary = registry_summary(load_company_registry(args.registry))
    for key, value in summary.items():
        print(f"{key}: {value}")


def _refresh_sources(args: argparse.Namespace) -> None:
    results = refresh_sources(
        registry_path=args.registry,
        store=LocalJobStore(args.store),
        config=load_ranking_config(args.config),
        limit=args.limit,
        where=args.where,
    )
    for result in results:
        print(f"{result.source_name}: {result.status}")
        print(f"       jobs_seen: {result.jobs_seen} | jobs_active: {result.jobs_active} | canonical_written: {result.canonical_jobs_written}")
        if result.error:
            print(f"       error: {result.error}")


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


def _wttj_import(args: argparse.Namespace) -> None:
    prepared = prepare_wttj_manual_import(args.path)
    _run_wttj_prepared_import(args, prepared)


def _wttj_discovery_import(args: argparse.Namespace) -> None:
    prepared = prepare_wttj_discovery_import(args.path)
    _run_wttj_prepared_import(args, prepared)


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
        mark_missing_inactive=False,
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
    profile = build_candidate_profile(args.store, args.name, args.graduation_year)
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
