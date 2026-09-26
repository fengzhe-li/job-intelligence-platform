from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Phase 2.7's five-state company coverage model. Deliberately distinct from
# ats_discovery.py's `verification_status` enum: `verification_status`
# describes what a single discovery/verify cycle found, while this describes
# a considered, investigation-backed judgement of what the user can actually
# rely on -- e.g. a company that hit an HTTPError once is not automatically
# MANUAL_REQUIRED, and a company with a verified connector is not
# AUTO_VERIFIED unless it was actually live-tested end-to-end.
AUTO_VERIFIED = "AUTO_VERIFIED"
PARTIAL_AUTOMATION = "PARTIAL_AUTOMATION"
MANUAL_REQUIRED = "MANUAL_REQUIRED"
TEMPORARILY_FAILED = "TEMPORARILY_FAILED"
UNKNOWN = "UNKNOWN"

# Companies individually investigated (Phase 2.7 Section A/C/D/E) and found to
# have a CONFIRMED, named bot-protection product blocking them consistently
# (not just a bare HTTP error code) -- evidence stronger than a single failed
# fetch, so these are classified MANUAL_REQUIRED rather than
# TEMPORARILY_FAILED even though the registry's own verification_status is
# currently `pending_network_validation` (which on its own would only mean
# "failed this cycle").
_CONFIRMED_BLOCKED_COMPANIES: dict[str, str] = {
    "Bloomberg": "Confirmed bot-detection challenge page ('Bloomberg - Are you a robot?') on direct fetch.",
    "Confluent": "Confirmed 'Vercel Security Checkpoint' bot-protection page on direct fetch.",
    "Gradcracker": "Confirmed site-wide Cloudflare managed challenge (robots.txt, sitemap, listing pages all return it).",
    "Bright Network": "Confirmed site-wide Cloudflare managed challenge (robots.txt, listing pages all return it).",
}

# Companies where THIS SESSION directly observed the exact same fetch attempt
# succeed on one run and fail (HTTPError) on another, consecutive run --
# direct, first-hand evidence the failure is transient rather than a
# permanent/structural block, distinct from a company only ever observed to
# fail once.
_OBSERVED_FLAKY_COMPANIES: set[str] = {"Revolut", "Wayve", "Tesco Technology", "Akamai", "NatWest Group", "Dyson", "Arista Networks UK"}


@dataclass(frozen=True)
class CompanyCoverageResult:
    company_name: str
    state: str
    reason: str
    provider: str | None
    careers_url: str | None
    jobs_available: int | None
    verification_status: str
    manual_check_required: bool
    # Explicit, machine-checkable distinction from every other reason a
    # company can require manual checking (temporary fetch failure,
    # unsupported ATS, no current jobs, parser failure) -- those are all
    # findings ABOUT the automated path; this is a human DECISION that
    # overrides it regardless of what the automated path finds.
    manually_excluded: bool = False
    exclusion_reason: str | None = None
    excluded_at: str | None = None


def classify_company(entry: dict[str, Any]) -> CompanyCoverageResult:
    name = entry.get("company_name", "")
    status = entry.get("verification_status", "")
    enabled = bool(entry.get("enabled"))
    provider = entry.get("connector_type")
    careers_url = entry.get("careers_url")
    jobs_available = entry.get("jobs_available")

    if entry.get("manually_excluded"):
        # Checked before AND regardless of verification_status/enabled --
        # a manual exclusion overrides everything else, including a
        # perfectly working candidate (e.g. Juniper Networks UK's Workday
        # match). Never AUTO_VERIFIED, never silently reclassified as a
        # different kind of "needs manual checking" (unsupported
        # ATS/temporary failure/etc.) -- the reason text and the dedicated
        # `manually_excluded`/`exclusion_reason`/`excluded_at` fields make
        # this unambiguous to anything reading the result, not just to a
        # human reading `reason` prose.
        reason = entry.get("exclusion_reason") or "Manually excluded (no reason recorded)."
        return CompanyCoverageResult(
            name,
            MANUAL_REQUIRED,
            f"MANUALLY EXCLUDED by human decision: {reason}",
            provider,
            careers_url,
            jobs_available,
            status,
            True,
            manually_excluded=True,
            exclusion_reason=reason,
            excluded_at=entry.get("excluded_at"),
        )

    if status == "verified" and enabled:
        # Every entry reaching this state in this registry was produced by a
        # real live verify_candidate() call against the company's actual
        # public feed (see ats_discovery.verify_candidate) -- jobs_available
        # is the real count observed on that call, not an assumption.
        return CompanyCoverageResult(
            name,
            AUTO_VERIFIED,
            f"Live-verified working {provider} connector with real jobs_available={jobs_available}.",
            provider,
            careers_url,
            jobs_available,
            status,
            False,
        )

    if name in _CONFIRMED_BLOCKED_COMPANIES:
        return CompanyCoverageResult(
            name, MANUAL_REQUIRED, _CONFIRMED_BLOCKED_COMPANIES[name], provider, careers_url, jobs_available, status, True
        )

    if status == "pending_network_validation":
        if name in _OBSERVED_FLAKY_COMPANIES:
            return CompanyCoverageResult(
                name,
                TEMPORARILY_FAILED,
                "This exact fetch was directly observed to succeed on one run and fail on another consecutive run this session -- evidence of a transient/rate-limit-style block, not a permanent one.",
                provider,
                careers_url,
                jobs_available,
                status,
                True,
            )
        return CompanyCoverageResult(
            name,
            TEMPORARILY_FAILED,
            f"Careers page fetch failed this cycle ({entry.get('ats_discovery_error') or entry.get('ats_verification_error') or 'unspecified error'}). Not individually confirmed as a permanent/structural block -- re-check on a future refresh before concluding it's unreachable.",
            provider,
            careers_url,
            jobs_available,
            status,
            True,
        )

    if status == "pending_ats_discovery":
        return CompanyCoverageResult(
            name,
            MANUAL_REQUIRED,
            "Careers page reachable, but no supported ATS pattern (Greenhouse/Lever/Ashby/Workable/SmartRecruiters/Workday) or schema.org JobPosting JSON-LD found on it -- likely a custom, fully client-rendered career search app, or an ATS this project doesn't support. No reasonable, maintainable automated path found within a bounded investigation.",
            provider,
            careers_url,
            jobs_available,
            status,
            True,
        )

    if status == "unsupported_provider":
        return CompanyCoverageResult(
            name,
            MANUAL_REQUIRED,
            entry.get("notes") or "Confirmed to use an ATS provider this project doesn't support.",
            provider,
            careers_url,
            jobs_available,
            status,
            True,
        )

    return CompanyCoverageResult(
        name,
        UNKNOWN,
        f"Registry state ({status!r}) doesn't map to a classified case -- insufficient evidence to classify safely.",
        provider,
        careers_url,
        jobs_available,
        status,
        True,
    )


def classify_registry(entries: list[dict[str, Any]]) -> list[CompanyCoverageResult]:
    return [classify_company(entry) for entry in entries]


def coverage_metrics(results: list[CompanyCoverageResult]) -> dict[str, Any]:
    total = len(results)
    counts = {state: sum(1 for r in results if r.state == state) for state in (AUTO_VERIFIED, PARTIAL_AUTOMATION, MANUAL_REQUIRED, TEMPORARILY_FAILED, UNKNOWN)}
    auto_verified = counts[AUTO_VERIFIED]
    auto_or_partial = counts[AUTO_VERIFIED] + counts[PARTIAL_AUTOMATION]
    return {
        "total_companies": total,
        "counts": counts,
        # These describe coverage of THIS 83-company target registry only --
        # never the UK graduate job market as a whole.
        "automatic_verified_coverage_of_registry": (auto_verified / total) if total else 0.0,
        "automatic_or_partial_coverage_of_registry": (auto_or_partial / total) if total else 0.0,
    }


def manual_watchlist(results: list[CompanyCoverageResult]) -> list[dict[str, Any]]:
    """Machine-readable data for every company still requiring manual
    attention -- Section L: this is meant to be dashboard data, not prose."""
    return [
        {
            "company": r.company_name,
            "careers_url": r.careers_url,
            "reason": r.reason,
            "state": r.state,
            "provider": r.provider,
            "recommended_manual_check_frequency": "weekly" if r.state == TEMPORARILY_FAILED else "monthly",
        }
        for r in results
        if r.manual_check_required
    ]
