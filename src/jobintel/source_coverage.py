from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jobintel.connectors.utils import parse_datetime
from jobintel.storage.local_store import LocalJobStore

DEFAULT_REGISTRY_PATH = "config/target_companies.json"

# Reuses the exact classification vocabulary mandated for the Phase 2.5 source
# audit (docs/SOURCE_COVERAGE.md) so this module's `automation_status` and that
# document's classification column always mean the same thing.
PRODUCTION_READY = "PRODUCTION_READY"
PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
MANUAL_FALLBACK_ONLY = "MANUAL_FALLBACK_ONLY"
BROKEN_OR_UNAVAILABLE = "BROKEN_OR_UNAVAILABLE"

# Phase 2.6 Section G's four-way answer to "what must I personally still check
# myself, and why":
ALWAYS_MANUAL = "ALWAYS_MANUAL"  # no automation exists for this source at all
PARTIALLY_MANUAL = "PARTIALLY_MANUAL"  # automated, but with a known, structural scope gap
TEMPORARILY_MANUAL = "TEMPORARILY_MANUAL"  # normally automated, but today's refresh actually failed/degraded
OPTIONAL_CROSS_CHECK = "OPTIONAL_CROSS_CHECK"  # automated and healthy right now; checking yourself is a sanity check, not a requirement


@dataclass(frozen=True)
class SourceCoverageProfile:
    """The honest, structured answer to "what does this source actually cover
    automatically, and what do I still need to check myself" -- built so the
    system can answer this directly (`coverage-today`) instead of the user
    having to remember it from documentation. See docs/SOURCE_COVERAGE.md for
    the full narrative audit this is drawn from.
    """

    source_name: str
    display_name: str
    automation_status: str
    coverage_scope: str
    manual_check_required: bool
    manual_check_reason: str
    manual_import_supported: bool
    known_gaps: tuple[str, ...]
    recommended_manual_action: str
    last_successful_automatic_check: datetime | None = None


# manual_import_supported is True for every source as of Phase 2.6: the new
# generic `manual-job-import` command (connectors.manual_source.
# prepare_single_url_import) can import a single job from ANY URL, on top of
# the batch CSV/JSON/URL-list path already wired for the four graduate
# sources + WTTJ -- so there is no source for which a manually-found job is
# structurally unimportable.
_STATIC_PROFILES: tuple[SourceCoverageProfile, ...] = (
    SourceCoverageProfile(
        source_name="greenhouse",
        display_name="Greenhouse",
        automation_status=PRODUCTION_READY,
        coverage_scope="Live public Job Board API, for companies already verified in config/target_companies.json only -- not all Greenhouse-using companies.",
        manual_check_required=True,
        manual_check_reason="No mechanism discovers a company's ATS from nothing -- a company must already be a registry entry with a working careers_url before ats-discover/ats-verify can identify and verify it.",
        manual_import_supported=True,
        known_gaps=("Companies not yet added to, or not yet verified in, the registry are invisible to automated refresh regardless of whether they actually use Greenhouse.",),
        recommended_manual_action="If a target company isn't showing verified jobs, check their careers page yourself, then `ats-add`/`ats-import` it or `manual-job-import` the specific posting.",
    ),
    SourceCoverageProfile(
        source_name="lever",
        display_name="Lever",
        automation_status=PRODUCTION_READY,
        coverage_scope="Live public postings API, registry-configured sites only.",
        manual_check_required=True,
        manual_check_reason="Same registry-seed-list limitation as Greenhouse.",
        manual_import_supported=True,
        known_gaps=("Each site's full listing is requested (no server-side `limit` since Phase 3); a site with more postings than the per-site refresh limit is truncated locally, reported in source health as an incomplete scope, and never used for closure inference.",),
        recommended_manual_action="If a target company isn't showing verified jobs, check their careers page yourself, then `ats-add`/`ats-import` it or `manual-job-import` the specific posting.",
    ),
    SourceCoverageProfile(
        source_name="ashby",
        display_name="Ashby",
        automation_status=PRODUCTION_READY,
        coverage_scope="Live public API, registry-configured boards only.",
        manual_check_required=True,
        manual_check_reason="Same registry-seed-list limitation as Greenhouse.",
        manual_import_supported=True,
        known_gaps=(),
        recommended_manual_action="If a target company isn't showing verified jobs, check their careers page yourself, then `ats-add`/`ats-import` it or `manual-job-import` the specific posting.",
    ),
    SourceCoverageProfile(
        source_name="workable",
        display_name="Workable",
        automation_status=PRODUCTION_READY,
        coverage_scope="Live public API, registry-configured accounts only.",
        manual_check_required=True,
        manual_check_reason="Same registry-seed-list limitation as Greenhouse.",
        manual_import_supported=True,
        known_gaps=(),
        recommended_manual_action="If a target company isn't showing verified jobs, check their careers page yourself, then `ats-add`/`ats-import` it or `manual-job-import` the specific posting.",
    ),
    SourceCoverageProfile(
        source_name="smartrecruiters",
        display_name="SmartRecruiters",
        automation_status=PRODUCTION_READY,
        coverage_scope="Live public API, registry-configured companies only, paginated up to 10 pages/company.",
        manual_check_required=True,
        manual_check_reason="Same registry-seed-list limitation as Greenhouse.",
        manual_import_supported=True,
        known_gaps=("The per-job full-JD detail call silently falls back to list-item-only fields if it fails -- not yet health-tracked at that granularity.",),
        recommended_manual_action="If a target company isn't showing verified jobs, check their careers page yourself, then `ats-add`/`ats-import` it or `manual-job-import` the specific posting.",
    ),
    SourceCoverageProfile(
        source_name="workday",
        display_name="Workday",
        automation_status=PRODUCTION_READY,
        coverage_scope="Live public 'CXS' careers-site API, registry-configured tenants only -- a Workday-hosted careers site's own frontend calls the same endpoint (POST .../wday/cxs/{tenant}/{site}/jobs), no auth required. Live-verified (Phase 2.7) against 3 real tenants (Darktrace, Lloyds Banking Group, London Stock Exchange Group).",
        manual_check_required=True,
        manual_check_reason="Same registry-seed-list limitation as every other ATS connector -- only companies already added and verified are covered.",
        manual_import_supported=True,
        known_gaps=(
            "Workday's own `total` field in the listing response is unreliable past the first page of a session (observed reporting 0 despite real results) -- pagination stops on page-size only, not `total`.",
            "A company's careers page can silently resolve to an ACQUIRER's shared, unscoped Workday tenant after an acquisition (confirmed for Juniper Networks UK -> HPE) -- such matches are deliberately excluded via manual_exclusion rather than auto-enabled, since they'd otherwise ingest an unrelated company's entire global job board.",
        ),
        recommended_manual_action="If a target company isn't showing verified jobs, check their careers page yourself, then `ats-add`/`ats-import` it or `manual-job-import` the specific posting.",
    ),
    SourceCoverageProfile(
        source_name="welcome_to_the_jungle",
        display_name="Welcome to the Jungle",
        automation_status=PRODUCTION_READY,
        coverage_scope="Live WelcomeKit Jobs API, conditional on WTTJ_API_KEY, for configured organisation references only. Gracefully degrades to manual import without a key.",
        manual_check_required=True,
        manual_check_reason="Requires WTTJ_API_KEY and a verified organization_reference per company; without either, no automated coverage exists for that company.",
        manual_import_supported=True,
        known_gaps=(),
        recommended_manual_action="Without WTTJ_API_KEY configured, use `wttj-import`/`wttj-discovery-import` or `manual-job-import` for individual postings.",
    ),
    SourceCoverageProfile(
        source_name="adzuna",
        display_name="Adzuna",
        automation_status=PARTIAL_COVERAGE,
        coverage_scope="Global UK keyword search, not company-scoped -- the only source here that discovers jobs outside the registry. Paginates up to 20 pages / ~1000 results per refresh (Phase 2.6).",
        manual_check_required=True,
        manual_check_reason="Descriptions are often short aggregator snippets, not the original JD; results are capped per refresh by design; true exhaustive UK-market recall for a given query is not confirmed.",
        manual_import_supported=True,
        known_gaps=(
            "Capped at 20 pages / ~1000 results per refresh (a deliberate rate-limit safety cap, not Adzuna's own hard ceiling).",
            "ADZUNA_APP_ID/ADZUNA_APP_KEY are not available in this project's development environment -- Phase 2.6's pagination rewrite was validated with 9 mocked unit tests, not a live multi-page run.",
        ),
        recommended_manual_action="Treat Adzuna results as a lead list, not a complete market scan -- cross-check against the direct company/ATS sources and manual sources for anything Adzuna's snippet descriptions don't fully answer.",
    ),
    SourceCoverageProfile(
        source_name="prospects",
        display_name="Prospects",
        automation_status=PARTIAL_COVERAGE,
        coverage_scope="Live discovery via real schema.org JobPosting JSON-LD, for the information-technology-69 and engineering-and-manufacturing-172 categories (Phase 2.6 re-audit of all 29 Prospects categories confirmed these are the right two for this project's target role families -- no dedicated AI/ML/telecoms/embedded/motorsport category exists there).",
        manual_check_required=True,
        manual_check_reason="Only 2 of Prospects' 29 sector categories are checked; a role miscategorised outside them would be missed automatically.",
        manual_import_supported=True,
        known_gaps=("Phase 2.6 live-tested pagination (`?page=2` returns byte-identical content to page 1, no total-count text, no listings-API reference found) -- treated as the full per-category listing, though a client-side-only mechanism this plain-HTTP approach can't observe can't be fully ruled out.",),
        recommended_manual_action="Periodically browse prospects.ac.uk yourself outside the two configured categories, and use `manual-job-import` for anything relevant found there.",
    ),
    SourceCoverageProfile(
        source_name="trackr",
        display_name="The Trackr",
        automation_status=MANUAL_FALLBACK_ONLY,
        coverage_scope="Manual import only (batch via `graduate-source-import trackr`, or single postings via `manual-job-import`).",
        manual_check_required=True,
        manual_check_reason="A real public API was found (api.the-trackr.com/programmes, disclosed validation schema) but Phase 2.6 confirmed every valid region x industry combination -- and a filterless request -- returns an empty result, and the endpoint enforces a 10-requests/day rate limit (exhausted during this audit's own testing). Not viable for automated polling.",
        manual_import_supported=True,
        known_gaps=("No confirmed way to retrieve real programme data from the public endpoint even before the rate limit was hit; may require authenticated/paid access this project doesn't have.",),
        recommended_manual_action="Browse the-trackr.com yourself for matching graduate schemes/programmes.",
    ),
    SourceCoverageProfile(
        source_name="gradcracker",
        display_name="Gradcracker",
        automation_status=MANUAL_FALLBACK_ONLY,
        coverage_scope="Manual import only.",
        manual_check_required=True,
        manual_check_reason="Site-wide Cloudflare managed-challenge (HTTP 403 'Just a moment...') confirmed on robots.txt, sitemap, and real listing pages -- not bypassed, per this project's compliance stance.",
        manual_import_supported=True,
        known_gaps=(),
        recommended_manual_action="Browse gradcracker.com yourself for matching roles.",
    ),
    SourceCoverageProfile(
        source_name="bright_network",
        display_name="Bright Network",
        automation_status=MANUAL_FALLBACK_ONLY,
        coverage_scope="Manual import only.",
        manual_check_required=True,
        manual_check_reason="Same site-wide Cloudflare managed-challenge as Gradcracker -- not bypassed.",
        manual_import_supported=True,
        known_gaps=(),
        recommended_manual_action="Browse brightnetwork.co.uk yourself for matching roles.",
    ),
)

STATIC_PROFILES_BY_NAME: dict[str, SourceCoverageProfile] = {profile.source_name: profile for profile in _STATIC_PROFILES}


def active_registry_connector_types(registry_entries: list[dict[str, Any]]) -> set[str]:
    """The set of ATS/platform families actually driving at least one
    enabled, live connector right now -- derived from the registry itself
    (`connector_type` on any enabled entry), not a hardcoded list. A brand
    new connector family (a future ATS this project adds support for) shows
    up here the moment any company entry uses it with `enabled: true`, with
    no edit to this module required for it to be visible.

    Note: Welcome to the Jungle is deliberately NOT derivable this way -- it
    is configured via its own dedicated `welcome_to_the_jungle_organization_
    reference` field (see company_registry.py's `_connector_token`), not the
    generic `connector_type`/`connector_token` pair every other ATS uses, so
    it keeps its always-included static profile below regardless.
    """
    return {
        str(entry["connector_type"])
        for entry in registry_entries
        if entry.get("enabled") and entry.get("connector_type") and not entry.get("manually_excluded")
    }


def _load_registry_entries(registry_path: Path | str | None) -> list[dict[str, Any]]:
    path = Path(registry_path or DEFAULT_REGISTRY_PATH)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return payload if isinstance(payload, list) else []


def generic_profile_for_connector_type(connector_type: str) -> SourceCoverageProfile:
    """Honest fallback for a connector family that's real and live (it drove
    at least one enabled, registry-verified company) but has no curated
    coverage write-up yet -- surfaced rather than silently omitted, per the
    Phase 3 requirement that a new connector family must appear automatically
    without a dashboard/source-list code edit.
    """
    display_name = connector_type.replace("_", " ").title()
    return SourceCoverageProfile(
        source_name=connector_type,
        display_name=display_name,
        automation_status=PRODUCTION_READY,
        coverage_scope=f"Live connector for registry-configured companies using {display_name} (detected dynamically from the registry). No curated coverage write-up exists yet for this connector family.",
        manual_check_required=True,
        manual_check_reason="Same registry-seed-list limitation as every other ATS connector, plus no curated known_gaps have been recorded for this newly-detected family yet.",
        manual_import_supported=True,
        known_gaps=("No curated per-family coverage notes recorded yet -- this profile was generated automatically because a live, enabled connector of this type exists in the registry.",),
        recommended_manual_action="If a target company isn't showing verified jobs, check their careers page yourself, then `ats-add`/`ats-import` it or `manual-job-import` the specific posting.",
    )


def all_profiles(registry_path: Path | str | None = None, registry_entries: list[dict[str, Any]] | None = None) -> list[SourceCoverageProfile]:
    """Every curated static profile, PLUS a generic profile for any
    registry-active connector family that isn't already named -- the merged,
    dynamic source list Section B requires. Curated profiles always take
    precedence over the generic fallback when both exist for the same name.
    """
    entries = registry_entries if registry_entries is not None else _load_registry_entries(registry_path)
    active_types = active_registry_connector_types(entries)
    profiles = list(_STATIC_PROFILES)
    named = {profile.source_name for profile in profiles}
    for connector_type in sorted(active_types - named):
        profiles.append(generic_profile_for_connector_type(connector_type))
    return profiles


def coverage_profiles(
    store: LocalJobStore | None = None,
    registry_path: Path | str | None = None,
    registry_entries: list[dict[str, Any]] | None = None,
) -> list[SourceCoverageProfile]:
    """Static (curated + dynamically-discovered) per-source profiles merged
    with today's dynamic health data (`last_successful_automatic_check`) --
    the combination is what makes this answerable as of *right now*, not a
    stale snapshot from when this module was written.
    """
    store = store or LocalJobStore()
    health = store.read_source_health()
    profiles = []
    for profile in all_profiles(registry_path=registry_path, registry_entries=registry_entries):
        record = health.get(profile.source_name)
        last_synced = parse_datetime(record.get("last_synced")) if isinstance(record, dict) else None
        profiles.append(
            SourceCoverageProfile(
                source_name=profile.source_name,
                display_name=profile.display_name,
                automation_status=profile.automation_status,
                coverage_scope=profile.coverage_scope,
                manual_check_required=profile.manual_check_required,
                manual_check_reason=profile.manual_check_reason,
                manual_import_supported=profile.manual_import_supported,
                known_gaps=profile.known_gaps,
                recommended_manual_action=profile.recommended_manual_action,
                last_successful_automatic_check=last_synced,
            )
        )
    return profiles


def manual_check_category(profile: SourceCoverageProfile, health_record: dict[str, Any] | None) -> str:
    """Derives (never hardcodes) which of the four Section G buckets a source
    falls into RIGHT NOW, combining its static profile with today's dynamic
    health record.
    """
    if profile.automation_status == MANUAL_FALLBACK_ONLY:
        return ALWAYS_MANUAL
    status = (health_record or {}).get("status") if isinstance(health_record, dict) else None
    if status in {"error", "auth_required", "partial"}:
        return TEMPORARILY_MANUAL
    if profile.automation_status in {PARTIAL_COVERAGE, BROKEN_OR_UNAVAILABLE}:
        return PARTIALLY_MANUAL
    # PRODUCTION_READY and today's refresh (if any) didn't fail -- still
    # structurally seed-list-scoped (manual_check_required=True), but that's
    # an optional sanity check, not something actively broken or known-partial
    # right now.
    if profile.manual_check_required:
        return OPTIONAL_CROSS_CHECK
    return OPTIONAL_CROSS_CHECK


@dataclass(frozen=True)
class CoverageTodayEntry:
    source_name: str
    display_name: str
    detail: str


@dataclass(frozen=True)
class CoverageTodayReport:
    generated_at: datetime
    automatically_checked_today: tuple[CoverageTodayEntry, ...]
    failed_or_incomplete_today: tuple[CoverageTodayEntry, ...]
    manual_check_still_required: tuple[CoverageTodayEntry, ...]


def coverage_today(
    store: LocalJobStore | None = None,
    now: datetime | None = None,
    registry_path: Path | str | None = None,
    registry_entries: list[dict[str, Any]] | None = None,
) -> CoverageTodayReport:
    """Answers, as of right now: what did automation actually check today, what
    should have run automatically but didn't (or never has), and what is
    structurally never/partially automatic regardless of today. This is the
    direct answer `coverage-today` (CLI) prints -- built so the user never has
    to remember source limitations from documentation to know what to check
    themselves.
    """
    store = store or LocalJobStore()
    now = now or datetime.now(tz=None).astimezone()
    today = now.date()
    health = store.read_source_health()

    checked_today: list[CoverageTodayEntry] = []
    failed_today: list[CoverageTodayEntry] = []
    manual_required: list[CoverageTodayEntry] = []

    for profile in all_profiles(registry_path=registry_path, registry_entries=registry_entries):
        record = health.get(profile.source_name) if isinstance(health.get(profile.source_name), dict) else None
        status = record.get("status") if record else None
        checked_at = parse_datetime(record.get("checked_at")) if record else None

        if profile.automation_status != MANUAL_FALLBACK_ONLY:
            if checked_at is not None and checked_at.date() == today and status == "live_api":
                checked_today.append(
                    CoverageTodayEntry(
                        profile.source_name,
                        profile.display_name,
                        f"jobs_seen={record.get('jobs_seen')} jobs_active={record.get('jobs_active')}",
                    )
                )
            elif checked_at is not None and checked_at.date() == today and status in {"error", "partial", "auth_required"}:
                failed_today.append(
                    CoverageTodayEntry(profile.source_name, profile.display_name, f"status={status}: {record.get('last_error') or record.get('failed_identifiers') or 'see source health'}")
                )
            else:
                failed_today.append(CoverageTodayEntry(profile.source_name, profile.display_name, "not refreshed today (no successful or failed run recorded for today)"))

        category = manual_check_category(profile, record)
        if category != OPTIONAL_CROSS_CHECK:
            manual_required.append(CoverageTodayEntry(profile.source_name, profile.display_name, f"{category}: {profile.manual_check_reason}"))

    return CoverageTodayReport(
        generated_at=now,
        automatically_checked_today=tuple(checked_today),
        failed_or_incomplete_today=tuple(failed_today),
        manual_check_still_required=tuple(manual_required),
    )
