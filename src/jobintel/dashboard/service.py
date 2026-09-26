from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from jobintel.application_lifecycle import overlay_lifecycle, set_job_status
from jobintel.analysis.job_quality import extract_required_experience, extract_seniority
from jobintel.config import RankingConfig, load_ranking_config
from jobintel.dashboard.i18n import labels
from jobintel.matching.matcher import MatchComponent, MatchResult
from jobintel.matching.project_selection import score_projects_for_job, select_top_projects
from jobintel.models.application import Application
from jobintel.models.candidate import CandidateProfile
from jobintel.models.job import Job
from jobintel.models.taxonomy import LocationMode, RoleTrack, SponsorshipFilterMode, SponsorshipState, WorkflowStatus
from jobintel.pipeline.ingestion import RankedJob, rank_jobs
from jobintel.profile_ingestion import load_candidate_profile
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.local_store import LocalJobStore


@dataclass(frozen=True)
class DashboardFilters:
    sponsorship: SponsorshipFilterMode = SponsorshipFilterMode.ALL
    location_mode: LocationMode = LocationMode.LONDON_FIRST_UK_WIDE
    role_track: RoleTrack | None = None
    seniority: str | None = None
    freshness_days: float | None = None
    company: str | None = None
    source: str | None = None
    workflow_status: WorkflowStatus | None = None
    enrichment_state: str | None = None
    search: str | None = None
    freshness_window: str | None = None
    new_today: bool = False
    limit: int = 50
    # priority (default) | deadline | first_seen | last_seen | company
    sort: str = "priority"


SORT_OPTIONS = ("priority", "deadline", "first_seen", "last_seen", "company")


PRESETS = {
    "london_focus": DashboardFilters(location_mode=LocationMode.LONDON_ONLY),
    "uk_wide": DashboardFilters(location_mode=LocationMode.UK_WIDE),
    "sponsor_first": DashboardFilters(sponsorship=SponsorshipFilterMode.SPONSOR_ONLY),
    "all_opportunities": DashboardFilters(),
    "new_today": DashboardFilters(new_today=True),
    "wttj_jobs": DashboardFilters(source="welcome_to_the_jungle"),
    "london_backend": DashboardFilters(location_mode=LocationMode.LONDON_ONLY, search="backend"),
    "graduate_software": DashboardFilters(search="graduate software"),
    "data_engineering": DashboardFilters(role_track=RoleTrack.DATA_ENGINEERING),
    "cloud_platform": DashboardFilters(search="cloud platform"),
    "network_telecom": DashboardFilters(search="network telecom"),
}


def filters_from_params(params: dict[str, str]) -> DashboardFilters:
    preset = params.get("preset")
    base = PRESETS.get(preset, DashboardFilters())
    sponsorship = _enum_value(SponsorshipFilterMode, params.get("sponsorship"), base.sponsorship)
    location_mode = _enum_value(LocationMode, params.get("location_mode"), base.location_mode)
    role_track = _optional_enum(RoleTrack, params.get("role_track"))
    workflow_status = _optional_enum(WorkflowStatus, params.get("workflow_status"))
    freshness_days = _float_or_none(params.get("freshness_days"))
    limit = int(params.get("limit") or base.limit)
    return replace(
        base,
        sponsorship=sponsorship,
        location_mode=location_mode,
        role_track=role_track,
        seniority=params.get("seniority") or base.seniority,
        freshness_days=freshness_days if freshness_days is not None else base.freshness_days,
        company=params.get("company") or base.company,
        source=params.get("source") or base.source,
        workflow_status=workflow_status or base.workflow_status,
        enrichment_state=params.get("enrichment_state") or base.enrichment_state,
        search=params.get("search") or base.search,
        freshness_window=params.get("freshness_window") or base.freshness_window,
        new_today=_bool_param(params.get("new_today"), base.new_today),
        limit=limit,
        sort=params.get("sort") if params.get("sort") in SORT_OPTIONS else base.sort,
    )


def build_dashboard_model(
    store: LocalJobStore | None = None,
    config: RankingConfig | None = None,
    filters: DashboardFilters | None = None,
    now: datetime | None = None,
    lang: str = "en",
) -> dict[str, Any]:
    store = store or LocalJobStore()
    config = config or load_ranking_config()
    filters = filters or DashboardFilters()
    now = now or datetime.now(timezone.utc)
    candidate = load_candidate_profile(store.root)
    ranked = rank_jobs(read_jobs_with_lifecycle(store), candidate=candidate, config=replace(config, location_mode=filters.location_mode), now=now)
    filtered = _sorted(
        [item for item in ranked if _passes_dashboard_filters(item, filters, now)], filters.sort
    )
    applications = _applications_by_job(store)
    cards = [job_card(item, now, candidate=candidate, application=applications.get(item.job.id)) for item in filtered[: filters.limit]]
    refresh_summary = _refresh_summary_model(store, ranked, now)
    return {
        "labels": labels(lang),
        "lang": lang,
        "filters": filters,
        "jobs": cards,
        "counts": {
            "total_ranked": len(ranked),
            "filtered": len(filtered),
            "new_today": len([item for item in ranked if is_new_today(item.job, now)]),
            "high_priority": len([item for item in ranked if item.match.overall_priority >= 4.0]),
            "recently_discovered": len([item for item in ranked if _first_seen_age_days(item.job, now) <= 7]),
            "wttj_jobs": len([item for item in ranked if _has_source(item.job, "welcome_to_the_jungle")]),
            "active_jobs": len([item for item in ranked if _is_active(item.job)]),
            "sponsor_positive": len([item for item in ranked if _sponsor_positive(item.job)]),
            "london_jobs": len([item for item in ranked if any(location.is_london for location in item.job.locations)]),
            "graduate_junior_associate": len([item for item in ranked if extract_seniority(item.job.title, item.job.description).level in {"graduate", "junior", "entry-level", "associate"}]),
        },
        "options": {
            "role_tracks": [item.value for item in RoleTrack],
            "workflow_statuses": [item.value for item in WorkflowStatus],
            "sources": _source_options(ranked),
            "companies": sorted({item.job.company for item in ranked if item.job.company}),
            "seniorities": sorted({extract_seniority(item.job.title, item.job.description).level for item in ranked}),
            "enrichment_states": ["discovery_only", "partially_enriched", "fully_enriched"],
            "freshness_windows": ["", "new_since_last_refresh", "new_today", "seen_24h", "seen_7d"],
            "sorts": list(SORT_OPTIONS),
        },
        "source_health": _source_health_model(store, ranked),
        "refresh_summary": refresh_summary,
        "saved_searches": saved_searches(store),
    }


def build_job_detail_model(job_id: str, store: LocalJobStore | None = None, config: RankingConfig | None = None, now: datetime | None = None, lang: str = "en") -> dict[str, Any]:
    store = store or LocalJobStore()
    config = config or load_ranking_config()
    now = now or datetime.now(timezone.utc)
    candidate = load_candidate_profile(store.root)
    ranked = rank_jobs(read_jobs_with_lifecycle(store), candidate=candidate, config=config, now=now)
    item = next((ranked_item for ranked_item in ranked if ranked_item.job.id == job_id), None)
    if item is None:
        raise KeyError(job_id)
    card = job_card(item, now, candidate=candidate, application=_applications_by_job(store).get(job_id))
    seniority = extract_seniority(item.job.title, item.job.description)
    experience = extract_required_experience(item.job.title, item.job.description)
    return {
        "labels": labels(lang),
        "lang": lang,
        "job": card,
        "original_jd": item.job.description,
        "normalized": {
            "id": item.job.id,
            "title": item.job.title,
            "company": item.job.company,
            "locations": card["location"],
            "work_mode": card["work_mode"],
            "source": card["source"],
            "posted_at": card["posted_at"],
            "first_seen_at": card["first_seen_at"],
            "workflow_status": item.job.workflow_status.value,
        },
        "source_observations": [
            {
                "source": observation.source_name,
                "label": source_display_label(observation.source_name),
                "source_job_id": observation.source_job_id,
                "original_url": observation.original_url,
                "application_url": observation.canonical_application_url,
                "first_seen_at": observation.first_seen_at.isoformat(),
                "last_seen_at": observation.last_seen_at.isoformat(),
                "active": observation.active,
                "latest_observed_state": observation.latest_observed_state,
                "ingestion_method": str(observation.raw_payload.get("_ingestion_method", "")),
                "enrichment_state": _observation_enrichment_state(observation.raw_payload),
            }
            for observation in item.job.source_observations
        ],
        "sponsorship_evidence": item.job.sponsorship.evidence_text if item.job.sponsorship else "",
        "graduation_evidence": item.job.graduation_year.evidence_text if item.job.graduation_year else "",
        "seniority_evidence": f"{seniority.evidence}; {experience.evidence}",
        "role_track_explanation": _component(item.match, "role_preference").explanation,
        "candidate_match_evidence": item.match.strongest_supporting_evidence,
        "project_emphasis": _project_emphasis(item.match),
    }


def job_card(item: RankedJob, now: datetime, candidate: CandidateProfile | None = None, application: Application | None = None) -> dict[str, Any]:
    job = item.job
    match = item.match
    last_seen = max((observation.last_seen_at for observation in job.source_observations), default=None)
    deadline = job.earliest_deadline
    graduation = job.graduation_year
    seniority = extract_seniority(job.title, job.description)
    first_seen = min((observation.first_seen_at for observation in job.source_observations), default=None)
    source_names = sorted({observation.source_name for observation in job.source_observations})
    work_modes = sorted({location.work_mode.value for location in job.locations})
    tracks = [track.value for track in match.primary_role_tracks + match.secondary_role_tracks]
    technical = _component(match, "technical_match").score
    enrichment_state = job_enrichment_state(job)
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "location": "; ".join(location.raw or f"{location.city}, {location.country}" for location in job.locations),
        "work_mode": ", ".join(work_modes),
        "source": ", ".join(source_names),
        "source_badges": [{"name": name, "label": source_display_label(name)} for name in source_names],
        "posted_at": job.posted_at.isoformat() if job.posted_at else "unknown",
        "first_seen_at": first_seen.isoformat() if first_seen else "unknown",
        "application_priority": match.overall_priority,
        "technical_fit": technical,
        "seniority": seniority.level,
        "enrichment_state": enrichment_state,
        "enrichment_label": enrichment_label(enrichment_state),
        "seniority_evidence": seniority.evidence,
        "sponsorship_state": job.sponsorship.state.value if job.sponsorship else SponsorshipState.UNKNOWN.value,
        "graduation_year_state": job.graduation_year.state.value if job.graduation_year else "unknown",
        "role_tracks": tracks,
        "recommended_cv": match.best_existing_cv_category or "",
        "hybrid_cv": match.hybrid_cv_recommended,
        "strongest_candidate_evidence": match.strongest_supporting_evidence,
        "main_weaknesses": match.missing_or_weak_evidence,
        "ranking_explanation": match.explanation,
        "application_url": job.canonical_application_url,
        "workflow_status": job.workflow_status.value,
        "last_seen_at": last_seen.isoformat() if last_seen else "unknown",
        # Unknown stays unknown -- never a guessed or default date.
        "deadline": deadline.isoformat() if deadline else None,
        "deadline_conflict": job.deadline_conflict,
        "deadline_observations": {source: value.isoformat() for source, value in job.deadline_observations.items()},
        "graduation_year_evidence": graduation.evidence_text if graduation else "",
        "intake_year": graduation.intake_year if graduation else None,
        "eligibility": [f"{evidence.label}: {evidence.evidence_text}" for evidence in job.eligibility],
        "matched_projects": _matched_projects(job, candidate),
        "application_id": application.id if application else None,
        "application_status": application.status.value if application else None,
        "new_today": is_new_today(job, now),
        "latest_observed_state": latest_observed_state(job),
        "is_active": _is_active(job),
        "english_summary": _english_summary(job, match),
        "chinese_summary": _chinese_summary(job, match, seniority.level),
    }


def _matched_projects(job: Job, candidate: CandidateProfile | None, limit: int = 3) -> list[str]:
    """Top evidence-grounded project matches (same scorer the CV engine uses)."""
    if candidate is None or not candidate.projects:
        return []
    matches = select_top_projects(score_projects_for_job(job, candidate), max_projects=limit)
    return [f"{match.project.name} ({', '.join(match.matched_requirements[:3]) or 'role relevance'})" for match in matches]


def _applications_by_job(store: LocalJobStore) -> dict[str, Application]:
    return {application.job_id: application for application in ApplicationStore(store.root).read_applications()}


def _sorted(items: list[RankedJob], sort: str) -> list[RankedJob]:
    far = datetime.max.replace(tzinfo=timezone.utc)
    old = datetime.min.replace(tzinfo=timezone.utc)
    if sort == "deadline":
        # Known deadlines soonest-first; unknown deadlines after, never
        # treated as a date.
        return sorted(items, key=lambda item: (item.job.earliest_deadline is None, item.job.earliest_deadline or far))
    if sort == "first_seen":
        return sorted(items, key=lambda item: min((o.first_seen_at for o in item.job.source_observations), default=old), reverse=True)
    if sort == "last_seen":
        return sorted(items, key=lambda item: max((o.last_seen_at for o in item.job.source_observations), default=old), reverse=True)
    if sort == "company":
        return sorted(items, key=lambda item: (item.job.company.casefold(), -item.match.overall_priority))
    return items  # already priority-ranked


def is_new_today(job: Job, now: datetime) -> bool:
    first_seen = min((observation.first_seen_at for observation in job.source_observations), default=None)
    if first_seen is None:
        return False
    return first_seen.astimezone(now.tzinfo or timezone.utc).date() == now.astimezone(now.tzinfo or timezone.utc).date()


def source_display_label(source_name: str) -> str:
    labels_by_source = {
        "welcome_to_the_jungle": "Welcome to the Jungle",
        "greenhouse": "Greenhouse",
        "lever": "Lever",
        "ashby": "Ashby",
        "workable": "Workable",
        "smartrecruiters": "SmartRecruiters",
        "adzuna": "Adzuna",
        "workday": "Workday",
        "prospects": "Prospects",
        "company": "Company",
    }
    return labels_by_source.get(source_name, source_name)


def job_enrichment_state(job: Job) -> str:
    states = [
        _observation_enrichment_state(observation.raw_payload)
        for observation in job.source_observations
        if observation.source_name == "welcome_to_the_jungle"
    ]
    if not states:
        return "fully_enriched"
    if "fully_enriched" in states:
        return "fully_enriched"
    if "partially_enriched" in states:
        return "partially_enriched"
    return "discovery_only"


def enrichment_label(state: str) -> str:
    return {
        "discovery_only": "Needs JD enrichment",
        "partially_enriched": "Partially enriched",
        "fully_enriched": "Fully enriched",
    }.get(state, state)


def read_jobs_with_lifecycle(store: LocalJobStore) -> list[Job]:
    """Jobs with `workflow_status` projected through the single ownership
    rule (application_lifecycle): a submitted application's status wins."""
    return overlay_lifecycle(store.read_jobs(), ApplicationStore(store.root))


def update_workflow_status(job_id: str, status: str, store: LocalJobStore | None = None) -> None:
    """Routes through application_lifecycle.set_job_status: triage values go
    to the workflow store, post-submission values to ApplicationStore."""
    store = store or LocalJobStore()
    set_job_status(job_id, WorkflowStatus(status), store, ApplicationStore(store.root))


def save_search(name: str, filters: DashboardFilters, store: LocalJobStore | None = None) -> None:
    store = store or LocalJobStore()
    current = [item for item in store.read_saved_searches() if item.get("name") != name]
    current.append({"name": name, "params": filters_to_params(filters)})
    store.write_saved_searches(current)


def saved_searches(store: LocalJobStore | None = None) -> list[dict[str, Any]]:
    store = store or LocalJobStore()
    stored = store.read_saved_searches()
    by_name = {item.get("name"): item for item in default_saved_searches()}
    for item in stored:
        if item.get("name"):
            by_name[str(item["name"])] = item
    return sorted(by_name.values(), key=lambda item: str(item.get("name", "")))


def default_saved_searches() -> list[dict[str, Any]]:
    return [
        {"name": "All Opportunities", "params": filters_to_params(PRESETS["all_opportunities"])},
        {"name": "Cloud / Platform", "params": filters_to_params(PRESETS["cloud_platform"])},
        {"name": "Data Engineering", "params": filters_to_params(PRESETS["data_engineering"])},
        {"name": "Graduate Software", "params": filters_to_params(PRESETS["graduate_software"])},
        {"name": "London Backend", "params": filters_to_params(PRESETS["london_backend"])},
        {"name": "Network / Telecom", "params": filters_to_params(PRESETS["network_telecom"])},
        {"name": "New Today", "params": filters_to_params(PRESETS["new_today"])},
        {"name": "Sponsor First", "params": filters_to_params(PRESETS["sponsor_first"])},
        {"name": "WTTJ Jobs", "params": filters_to_params(PRESETS["wttj_jobs"])},
    ]


def filters_to_params(filters: DashboardFilters) -> dict[str, str]:
    params: dict[str, str] = {
        "sponsorship": filters.sponsorship.value,
        "location_mode": filters.location_mode.value,
        "limit": str(filters.limit),
    }
    if filters.role_track:
        params["role_track"] = filters.role_track.value
    if filters.seniority:
        params["seniority"] = filters.seniority
    if filters.freshness_days is not None:
        params["freshness_days"] = str(filters.freshness_days)
    if filters.company:
        params["company"] = filters.company
    if filters.source:
        params["source"] = filters.source
    if filters.workflow_status:
        params["workflow_status"] = filters.workflow_status.value
    if filters.enrichment_state:
        params["enrichment_state"] = filters.enrichment_state
    if filters.search:
        params["search"] = filters.search
    if filters.freshness_window:
        params["freshness_window"] = filters.freshness_window
    if filters.new_today:
        params["new_today"] = "1"
    if filters.sort != "priority":
        params["sort"] = filters.sort
    return params


def _passes_dashboard_filters(item: RankedJob, filters: DashboardFilters, now: datetime) -> bool:
    tracks = {track.value for track in item.match.primary_role_tracks + item.match.secondary_role_tracks}
    sponsorship = item.job.sponsorship.state if item.job.sponsorship else SponsorshipState.UNKNOWN
    if filters.sponsorship == SponsorshipFilterMode.SPONSOR_ONLY and sponsorship not in {SponsorshipState.EXPLICIT_SPONSOR, SponsorshipState.LIKELY_SPONSOR}:
        return False
    if filters.sponsorship == SponsorshipFilterMode.NO_SPONSOR_ONLY and sponsorship not in {SponsorshipState.EXPLICIT_NO_SPONSOR, SponsorshipState.LIKELY_NO_SPONSOR}:
        return False
    if filters.role_track and filters.role_track.value not in tracks:
        return False
    if filters.seniority and filters.seniority != extract_seniority(item.job.title, item.job.description).level:
        return False
    if filters.company and filters.company.casefold() not in item.job.company.casefold():
        return False
    if filters.source and filters.source.casefold() not in {observation.source_name.casefold() for observation in item.job.source_observations}:
        return False
    if filters.workflow_status and item.job.workflow_status != filters.workflow_status:
        return False
    if filters.enrichment_state and job_enrichment_state(item.job) != filters.enrichment_state:
        return False
    if filters.search and not _matches_search(item, filters.search):
        return False
    if filters.freshness_window and not _passes_freshness_window(item.job, filters.freshness_window, now):
        return False
    if filters.freshness_days is not None and item.job.posted_at is not None:
        age_days = (now - item.job.posted_at).total_seconds() / 86400.0
        if age_days > filters.freshness_days:
            return False
    if filters.new_today and not is_new_today(item.job, now):
        return False
    return True


def latest_observed_state(job: Job) -> str:
    priority = ["NEW", "REAPPEARED", "CHANGED", "DISAPPEARED", "UNCHANGED"]
    states = {observation.latest_observed_state for observation in job.source_observations}
    return next((state for state in priority if state in states), "")


def _passes_freshness_window(job: Job, freshness_window: str, now: datetime) -> bool:
    if freshness_window == "new_today":
        return is_new_today(job, now)
    if freshness_window == "seen_24h":
        return _first_seen_at(job) >= now - timedelta(hours=24)
    if freshness_window == "seen_7d":
        return _first_seen_at(job) >= now - timedelta(days=7)
    if freshness_window == "new_since_last_refresh":
        return latest_observed_state(job) in {"NEW", "REAPPEARED"}
    return True


def _matches_search(item: RankedJob, query: str) -> bool:
    tokens = [token for token in query.casefold().split() if token]
    if not tokens:
        return True
    job = item.job
    haystack = " ".join(
        [
            job.title,
            job.company,
            job.raw_location or "",
            " ".join(location.raw or f"{location.city or ''} {location.country}" for location in job.locations),
            " ".join(track.value for track in item.match.primary_role_tracks + item.match.secondary_role_tracks),
            " ".join(requirement.name for requirement in job.skill_requirements),
            " ".join(item.match.strongest_supporting_evidence),
            " ".join(item.match.missing_or_weak_evidence),
            job.description,
        ]
    ).casefold()
    return all(token in haystack for token in tokens)


def _first_seen_at(job: Job) -> datetime:
    return min((observation.first_seen_at for observation in job.source_observations), default=datetime.max.replace(tzinfo=timezone.utc))


def _first_seen_age_days(job: Job, now: datetime) -> float:
    first_seen = min((observation.first_seen_at for observation in job.source_observations), default=None)
    if first_seen is None:
        return 9999.0
    return (now - first_seen).total_seconds() / 86400.0


def _has_source(job: Job, source_name: str) -> bool:
    return source_name.casefold() in {observation.source_name.casefold() for observation in job.source_observations}


def _is_active(job: Job) -> bool:
    return any(observation.active for observation in job.source_observations)


def _sponsor_positive(job: Job) -> bool:
    if not job.sponsorship:
        return False
    return job.sponsorship.state in {SponsorshipState.EXPLICIT_SPONSOR, SponsorshipState.LIKELY_SPONSOR}


def _observation_enrichment_state(payload: dict[str, Any]) -> str:
    return str(payload.get("_enrichment_state") or "fully_enriched")


def _source_options(ranked: list[RankedJob]) -> list[str]:
    sources = {observation.source_name for item in ranked for observation in item.job.source_observations}
    sources.add("welcome_to_the_jungle")
    return sorted(sources)


def _source_health_model(store: LocalJobStore, ranked: list[RankedJob]) -> dict[str, dict[str, Any]]:
    stored = store.read_source_health()
    health: dict[str, dict[str, Any]] = {}
    source_names = set(_source_options(ranked))
    source_names.update(str(source_name) for source_name in stored.keys())
    for source_name in sorted(source_names):
        payload = stored.get(source_name, {}) if isinstance(stored.get(source_name, {}), dict) else {}
        if source_name == "welcome_to_the_jungle" and not payload:
            payload = {"status": "manual_only", "last_synced": None, "jobs_active": 0, "last_error": None}
        health[source_name] = {
            "label": source_display_label(source_name),
            "status": _source_status_label(str(payload.get("status") or "unknown")),
            "last_synced": payload.get("last_synced") or "never",
            "jobs_active": payload.get("jobs_active", 0),
            "jobs_seen": payload.get("jobs_seen", 0),
            "state_counts": payload.get("state_counts", {}),
            "last_error": payload.get("last_error") or "",
            "warning": payload.get("warning") or "",
            "failed_identifiers": payload.get("failed_identifiers") or {},
        }
    return health


def _refresh_summary_model(store: LocalJobStore, ranked: list[RankedJob], now: datetime) -> dict[str, Any]:
    summary = store.read_refresh_summary()
    # Prefer the per-refresh deltas (what the last refresh actually changed);
    # fall back to the legacy cumulative counts for summaries written before
    # deltas existed.
    state_counts = summary.get("delta_counts") if isinstance(summary.get("delta_counts"), dict) else summary.get("state_counts") if isinstance(summary.get("state_counts"), dict) else {}
    last_successful = _last_successful_refresh(store.read_source_health())
    return {
        "status": summary.get("status", "never"),
        "started_at": summary.get("started_at", "never"),
        "finished_at": summary.get("finished_at", "never"),
        "last_successful_refresh": last_successful or "never",
        "total_jobs_seen": summary.get("total_jobs_seen", 0),
        "state_counts": {state: int(state_counts.get(state, 0)) for state in ("NEW", "CHANGED", "UNCHANGED", "DISAPPEARED", "REAPPEARED")},
        "active_jobs": summary.get("active_jobs", len([item for item in ranked if _is_active(item.job)])),
        "new_since_last_refresh": len([item for item in ranked if latest_observed_state(item.job) in {"NEW", "REAPPEARED"}]),
        "changed": len([item for item in ranked if latest_observed_state(item.job) == "CHANGED"]),
        "disappeared": len([item for item in ranked if latest_observed_state(item.job) == "DISAPPEARED"]),
        "seen_24h": len([item for item in ranked if _first_seen_at(item.job) >= now - timedelta(hours=24)]),
        "seen_7d": len([item for item in ranked if _first_seen_at(item.job) >= now - timedelta(days=7)]),
        "sources": summary.get("sources", []),
    }


def _last_successful_refresh(source_health: dict[str, Any]) -> str | None:
    synced = [
        payload.get("last_synced")
        for payload in source_health.values()
        if isinstance(payload, dict) and payload.get("status") == "live_api" and payload.get("last_synced")
    ]
    return max(synced, default=None)


def _source_status_label(status: str) -> str:
    return {
        "live_api": "Live API",
        "auth_required": "Auth required",
        "manual_only": "Manual only",
        "error": "Error",
        "partial": "Partial (coverage incomplete)",
    }.get(status, status)


def _component(match: MatchResult, name: str) -> MatchComponent:
    # Excluded-occupation matches short-circuit to a single "excluded_occupation"
    # component (see matcher.match_job), so standard components are absent there.
    return next(
        (component for component in match.components if component.name == name),
        MatchComponent(name, 0.0, "Not scored: occupation excluded from ranking."),
    )


def _project_emphasis(match: MatchResult) -> str:
    tracks = [track.value for track in match.primary_role_tracks + match.secondary_role_tracks]
    track_text = ", ".join(tracks) if tracks else "general technical evidence"
    cv = match.best_existing_cv_category or "strongest existing CV"
    return f"Use {cv}; emphasize {track_text} evidence and address {', '.join(match.missing_or_weak_evidence[:3]) or 'no major extracted gaps'}."


def _english_summary(job: Job, match: MatchResult) -> str:
    location = "; ".join(location.raw or location.country for location in job.locations)
    tracks = ", ".join(track.value for track in match.primary_role_tracks + match.secondary_role_tracks) or "no strong technical track"
    return f"{job.title} at {job.company}; {location}; priority {match.overall_priority:.3f}; tracks: {tracks}."


def _chinese_summary(job: Job, match: MatchResult, seniority: str) -> str:
    tracks = ", ".join(track.value for track in match.primary_role_tracks + match.secondary_role_tracks) or "暂无明确技术方向"
    return f"{job.company} 的 {job.title}；资历：{seniority}；优先级 {match.overall_priority:.3f}；方向：{tracks}。"


def _enum_value(enum_type, value: str | None, default):
    if not value:
        return default
    try:
        return enum_type(value)
    except ValueError:
        return default


def _optional_enum(enum_type, value: str | None):
    if not value:
        return None
    try:
        return enum_type(value)
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _bool_param(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value in {"1", "true", "yes", "on"}
