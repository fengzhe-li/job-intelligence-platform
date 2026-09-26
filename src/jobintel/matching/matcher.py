from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import exp, log

from jobintel.analysis.job_quality import (
    assess_uk_market,
    extract_required_experience,
    extract_seniority,
    infer_engineering_domain,
    infer_role_function,
)
from jobintel.analysis.occupation_filter import is_excluded_occupation
from jobintel.config import RankingConfig
from jobintel.matching.location import score_location
from jobintel.models.candidate import CandidateProfile, Capability
from jobintel.models.job import Job
from jobintel.models.taxonomy import RoleTrack


@dataclass(frozen=True)
class MatchComponent:
    name: str
    score: float
    explanation: str


@dataclass
class MatchResult:
    job_id: str
    title: str
    company: str
    overall_priority: float
    components: list[MatchComponent]
    strongest_supporting_evidence: list[str] = field(default_factory=list)
    missing_or_weak_evidence: list[str] = field(default_factory=list)
    primary_role_tracks: list[RoleTrack] = field(default_factory=list)
    secondary_role_tracks: list[RoleTrack] = field(default_factory=list)
    best_existing_cv_category: str | None = None
    hybrid_cv_recommended: bool = False
    explanation: str = ""
    excluded: bool = False


def match_job(candidate: CandidateProfile, job: Job, config: RankingConfig, now: datetime | None = None) -> MatchResult:
    now = now or datetime.now(timezone.utc)
    excluded, excluded_phrase = is_excluded_occupation(job.title, job.description, config)
    if excluded:
        return MatchResult(
            job_id=job.id,
            title=job.title,
            company=job.company,
            overall_priority=-100.0,
            components=[MatchComponent("excluded_occupation", -100.0, f"Excluded occupation: {excluded_phrase}")],
            explanation=f"Excluded because it matched unrelated occupation phrase: {excluded_phrase}",
            excluded=True,
        )

    seniority = extract_seniority(job.title, job.description)
    experience = extract_required_experience(job.title, job.description)
    role_function = infer_role_function(job.title, job.description)
    engineering_domain = infer_engineering_domain(job.title, job.description)
    market = assess_uk_market(job)
    role_score, primary_tracks, secondary_tracks = _role_score(job, config, role_function.function, engineering_domain.score)
    matched_capabilities, missing = _match_capabilities(candidate, job)
    technical_score = _technical_score(job, matched_capabilities, seniority.score, experience.score, role_function.function, engineering_domain.score)
    sponsorship_score = _sponsorship_score(job, config)
    graduation_score = _graduation_score(job, config)
    location_score, location_explanation = score_location(job, config.location_mode, config.london_preference_weight)
    source_score = _source_quality_score(job, config)
    freshness_score = _freshness_score(job, config, now)
    cv_category, hybrid = _recommend_cv(candidate, primary_tracks, secondary_tracks)

    components = [
        MatchComponent("technical_match", technical_score, _technical_explanation(matched_capabilities, technical_score, role_function.function)),
        MatchComponent("role_preference", role_score, _role_explanation(primary_tracks, secondary_tracks)),
        MatchComponent("seniority", seniority.score, seniority.evidence),
        MatchComponent("required_experience", experience.score, _experience_explanation(experience)),
        MatchComponent("role_function", role_function.score, role_function.evidence),
        MatchComponent("engineering_domain", engineering_domain.score, engineering_domain.evidence),
        MatchComponent("sponsorship", sponsorship_score, _sponsorship_explanation(job)),
        MatchComponent("graduation_year", graduation_score, _graduation_explanation(job)),
        MatchComponent("location", location_score, location_explanation),
        MatchComponent("market_fit", market.score, market.evidence),
        MatchComponent("source_quality", source_score, "Source quality based on configurable source weights"),
        MatchComponent("freshness", freshness_score, "Freshness based on configurable half-life decay"),
    ]
    overall = round(
        2.2 * technical_score
        + 1.1 * role_score
        + 1.8 * seniority.score
        + 0.9 * experience.score
        + 1.0 * role_function.score
        + 1.0 * engineering_domain.score
        + 0.9 * sponsorship_score
        + 0.8 * graduation_score
        + 0.7 * location_score
        + 0.8 * market.score
        + 0.4 * source_score
        + 0.5 * freshness_score,
        3,
    )

    evidence = _strongest_evidence(matched_capabilities)
    explanations = [component.explanation for component in components if component.explanation]
    return MatchResult(
        job_id=job.id,
        title=job.title,
        company=job.company,
        overall_priority=overall,
        components=components,
        strongest_supporting_evidence=evidence,
        missing_or_weak_evidence=missing,
        primary_role_tracks=primary_tracks,
        secondary_role_tracks=secondary_tracks,
        best_existing_cv_category=cv_category,
        hybrid_cv_recommended=hybrid,
        explanation="; ".join(explanations),
    )


def _match_capabilities(candidate: CandidateProfile, job: Job) -> tuple[list[Capability], list[str]]:
    matched: list[Capability] = []
    missing: list[str] = []
    for requirement in job.skill_requirements:
        capability = candidate.capabilities.get(requirement.name.casefold())
        if capability:
            matched.append(capability)
        elif requirement.required:
            missing.append(requirement.name)
    return matched, missing


def _technical_score(
    job: Job,
    matched: list[Capability],
    seniority_score: float = 0.0,
    experience_score: float = 0.0,
    role_function: str = "unknown",
    engineering_domain_score: float = 0.0,
) -> float:
    required_count = max(len([item for item in job.skill_requirements if item.required]), 1)
    weighted_matches = sum(capability.confidence for capability in matched)
    denominator = max(required_count, len(job.skill_requirements), 3)
    score = min(weighted_matches / denominator, 1.0)
    if len(matched) <= 1:
        score = min(score, 0.45)
    elif len(matched) == 2:
        score = min(score, 0.7)
    elif len(matched) == 3:
        score = min(score, 0.85)
    if role_function in {"business_market", "management", "operations_investigations", "fp&a_finance_analytics", "admin_recruiting", "legal_compliance"}:
        score = min(score, 0.35)
    elif role_function == "data_analytics_bi":
        score = min(score, 0.65)
    if seniority_score <= -1.0:
        score = min(score, 0.45)
    elif seniority_score <= -0.8:
        score = min(score, 0.6)
    if experience_score <= -0.9:
        score = min(score, 0.5)
    elif experience_score < 0:
        score = min(score, 0.75)
    if engineering_domain_score <= -0.8:
        score = min(score, 0.3)
    elif engineering_domain_score < 0:
        score = min(score, 0.45)
    return round(score, 3)


def _role_score(job: Job, config: RankingConfig, role_function: str = "unknown", engineering_domain_score: float = 0.0) -> tuple[float, list[RoleTrack], list[RoleTrack]]:
    if role_function in {"business_market", "management", "operations_investigations", "fp&a_finance_analytics", "admin_recruiting", "legal_compliance"}:
        return -0.8, [], []
    if role_function == "data_analytics_bi":
        return -0.25, [], []
    primary = job.role_track_profile.primary()
    secondary = job.role_track_profile.secondary()
    tracks = ([primary] if primary else []) + secondary
    if not tracks:
        return 0.0, [], []
    weighted = sum(item.score * config.role_preferences.get(item.track, 0.5) for item in tracks)
    total = sum(item.score for item in tracks)
    score = round(weighted / total, 3)
    if engineering_domain_score <= -0.8:
        score = min(score, -0.35)
    elif engineering_domain_score < 0:
        score = min(score, 0.15)
    return score, ([primary.track] if primary else []), [item.track for item in secondary]


def _sponsorship_score(job: Job, config: RankingConfig) -> float:
    if job.sponsorship is None:
        return 0.0
    return config.sponsorship_weights[job.sponsorship.state]


def _graduation_score(job: Job, config: RankingConfig) -> float:
    if job.graduation_year is None:
        return 0.0
    return config.graduation_year_weights[job.graduation_year.state]


def _source_quality_score(job: Job, config: RankingConfig) -> float:
    weights = [config.source_weights.get(item.source_name.casefold(), 0.4) for item in job.source_observations]
    return round(max(weights, default=0.0), 3)


def _freshness_score(job: Job, config: RankingConfig, now: datetime) -> float:
    posted_at = job.posted_at
    if posted_at is None:
        return 0.0
    age_days = max((now - posted_at).total_seconds() / 86400.0, 0.0)
    decay = exp(-log(2) * age_days / config.freshness_half_life_days)
    return round(decay, 3)


def _recommend_cv(candidate: CandidateProfile, primary: list[RoleTrack], secondary: list[RoleTrack]) -> tuple[str | None, bool]:
    categories = [cv.category for cv in candidate.cv_versions]
    track_to_category = {
        RoleTrack.BACKEND_ENGINEERING: "Software/Backend",
        RoleTrack.SOFTWARE_ENGINEERING: "Software/Backend",
        RoleTrack.FRONTEND_ENGINEERING: "Frontend",
        RoleTrack.FULL_STACK_ENGINEERING: "Full-stack",
        RoleTrack.DATA_ENGINEERING: "Data",
        RoleTrack.DATA_PLATFORM: "Data",
        RoleTrack.CLOUD_ENGINEERING: "Cloud/Platform",
        RoleTrack.PLATFORM_ENGINEERING: "Cloud/Platform",
        RoleTrack.AI_ML_ENGINEERING: "AI/ML",
    }
    suggested = [track_to_category[track] for track in primary + secondary if track in track_to_category]
    available = [category for category in suggested if category in categories]
    unique_suggestions = set(suggested)
    if len(unique_suggestions) > 1:
        return (available[0] if available else None), True
    return (available[0] if available else (suggested[0] if suggested else None)), False


def _technical_explanation(matched: list[Capability], score: float, role_function: str) -> str:
    if not matched:
        return "No direct skill evidence matched current requirements"
    names = ", ".join(capability.name for capability in matched[:5])
    if score < 0.75 and role_function in {"business_market", "management", "operations_investigations", "fp&a_finance_analytics", "data_analytics_bi", "admin_recruiting", "legal_compliance"}:
        return f"Matched candidate evidence for {names}, capped for {role_function.replace('_', ' ')} role function"
    if score < 0.75:
        return f"Matched candidate evidence for {names}, capped because keyword overlap alone is not enough for a perfect fit"
    return f"Matched candidate evidence for {names}"


def _role_explanation(primary: list[RoleTrack], secondary: list[RoleTrack]) -> str:
    if not primary:
        return "No strong role-track signal detected"
    if secondary:
        return f"Primary {primary[0].value}; cross-track signals: {', '.join(track.value for track in secondary)}"
    return f"Primary {primary[0].value}"


def _sponsorship_explanation(job: Job) -> str:
    if job.sponsorship is None:
        return "No sponsorship evidence extracted"
    if job.sponsorship.evidence_text:
        return f"Sponsorship: {job.sponsorship.state.value} from '{job.sponsorship.evidence_text}'"
    return f"Sponsorship: {job.sponsorship.state.value}"


def _graduation_explanation(job: Job) -> str:
    if job.graduation_year is None:
        return "No graduation-year evidence extracted"
    intake_suffix = f" (informational: detected intake year {job.graduation_year.intake_year}, not used for eligibility)" if job.graduation_year.intake_year else ""
    if job.graduation_year.evidence_text:
        return f"Graduation year: {job.graduation_year.state.value} from '{job.graduation_year.evidence_text}'{intake_suffix}"
    return f"Graduation year: {job.graduation_year.state.value}{intake_suffix}"


def _experience_explanation(experience) -> str:
    if experience.management_required:
        return f"Management experience required: {experience.evidence}"
    if experience.years_required is None:
        return experience.evidence
    years = int(experience.years_required) if experience.years_required.is_integer() else experience.years_required
    return f"{years}+ years required: {experience.evidence}"


def _strongest_evidence(capabilities: list[Capability]) -> list[str]:
    evidence: list[str] = []
    for capability in sorted(capabilities, key=lambda item: item.confidence, reverse=True):
        if capability.evidence:
            item = capability.evidence[0]
            evidence.append(f"{capability.name}: {item.source.title} - {item.quote}")
    return evidence[:5]
