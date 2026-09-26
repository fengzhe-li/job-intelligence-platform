from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jobintel.ats_discovery import add_manual_ats_url, detect_ats_candidates
from jobintel.connectors.utils import now_utc
from jobintel.models.job import Job
from jobintel.storage.company_discovery_store import CompanyDiscoveryStore, CompanyObservation

# Only sources that aren't already company-registry-scoped can reveal a
# genuinely NEW company -- a job from Greenhouse/Lever/etc. is by definition
# already from a registry-configured company, so "new company" doesn't apply
# to them.
NON_REGISTRY_SOURCES = {"adzuna", "prospects", "trackr", "gradcracker", "bright_network", "manual", "welcome_to_the_jungle"}


def known_company_names(registry_entries: list[dict[str, Any]]) -> set[str]:
    return {entry.get("company_name", "").casefold() for entry in registry_entries}


def observe_companies_from_jobs(
    jobs: list[Job],
    registry_entries: list[dict[str, Any]],
    store: CompanyDiscoveryStore,
) -> list[CompanyObservation]:
    """Scans already-ingested jobs for companies not in the registry and not
    already logged, and records each one with provenance. Never adds anything
    to the registry itself -- that's a separate, explicit `promote_candidate`
    step, so nothing enters the monitored registry without a human decision.
    """
    known = known_company_names(registry_entries)
    already_observed = set(store.latest_per_company())
    new_observations: list[CompanyObservation] = []
    for job in jobs:
        company_key = (job.company or "").casefold().strip()
        if not company_key or company_key in known or company_key in already_observed:
            continue
        source_observation = job.source_observations[0] if job.source_observations else None
        if source_observation is None or source_observation.source_name not in NON_REGISTRY_SOURCES:
            continue
        application_url = source_observation.canonical_application_url or ""
        # The application URL itself sometimes IS a direct ATS link (e.g. an
        # aggregator's redirect resolving straight to boards.greenhouse.io) --
        # reuse the exact same pattern-detection used for careers pages.
        suggestions = detect_ats_candidates(application_url, application_url)
        suggestion = suggestions[0] if suggestions else None
        observation = CompanyObservation(
            company_name=job.company,
            observed_via_source=source_observation.source_name,
            example_job_title=job.title,
            example_job_url=source_observation.original_url,
            example_application_url=application_url,
            suggested_connector_type=suggestion.connector_type if suggestion else None,
            suggested_connector_token=suggestion.connector_token if suggestion else None,
            observed_at=now_utc().isoformat(),
        )
        store.append(observation)
        already_observed.add(company_key)
        new_observations.append(observation)
    return new_observations


def promote_candidate(
    observation: CompanyObservation,
    registry_path: Path | str = "config/target_companies.json",
    write: bool = True,
) -> dict[str, Any]:
    """Turns one reviewed observation into a real registry entry, through the
    SAME registry pipeline every other company uses -- not a parallel
    mechanism. If a connector was already suggested (found on the
    application URL itself), this verifies and enables it immediately, same
    as `ats-add`. Otherwise it adds a bare pending entry (careers_url set to
    the best URL observed) for a normal `ats-discover`/`ats-verify` pass, or
    for a human to fix up if that URL isn't actually the careers page.
    """
    if observation.suggested_connector_type and observation.suggested_connector_token:
        ats_url = observation.example_application_url
        return add_manual_ats_url(observation.company_name, ats_url, registry_path, write=write)

    path = Path(registry_path)
    entries = json.loads(path.read_text(encoding="utf-8"))
    if any(entry.get("company_name", "").casefold() == observation.company_name.casefold() for entry in entries):
        raise ValueError(f"{observation.company_name!r} is already in the registry")
    entry = {
        "company_name": observation.company_name,
        "industry": f"discovered via {observation.observed_via_source}",
        "priority": 3,
        "careers_url": observation.example_application_url or observation.example_job_url,
        "source_connector_type": "pending_verification",
        "enabled": False,
        "notes": f"Discovered via {observation.observed_via_source} ({observation.observed_at}); example job: {observation.example_job_title!r}. No ATS pattern found on the application URL -- careers_url needs manual review/correction before ats-discover can find a real link pattern on it.",
        "verification_status": "pending_ats_discovery",
    }
    entries.append(entry)
    if write:
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return entry
