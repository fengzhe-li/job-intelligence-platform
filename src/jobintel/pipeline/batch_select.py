"""Choosing a batch: a competitiveness-aware candidate pool, a per-company cap with an explicit
same-company priority order (deferred roles are reported, never discarded), and an ATS coverage audit.

Nothing here prepares, applies or writes. `batch.prepare(job_ids=...)` prepares what is chosen.

Ordering asks "where is the candidate most competitive and most realistically employable?": the
existing rank (`rank_jobs`: technical + role + domain + freshness + source) is combined with level fit
(graduate / junior first; senior / staff / principal last), the years of experience a JD demands, and
eligibility / sponsorship certainty (an explicit "no sponsorship" is an important negative; a confirmed
hard conflict is excluded). Keyword overlap alone never decides it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

# Titles that are not the candidate's target occupation even when technical (pure analysis / quant /
# trading / non-engineering).
_NON_TARGET_TITLE = re.compile(
    r"\b(?:data\s+analyst|business\s+analyst|bi\s+analyst|analytics\s+engineer|quant(?:itative)?|trader|trading\s+analyst"
    r"|actuar\w*|product\s+manager|designer|recruit\w*|sales|account\s+executive|marketing|counsel|accountant"
    r"|customer\s+success|support\s+(?:specialist|analyst)|operations\s+(?:manager|analyst))\b", re.I)
_TECHNICAL_TITLE = re.compile(r"\b(?:engineer(?:ing)?|developer|programmer|sre|devops|software|platform|backend"
                              r"|back-end|full[- ]?stack|infrastructure|machine\s+learning|ml|ai)\b", re.I)
# Explicit UK location evidence only: a UK country/nation or UK city, as whole words. "Remote" (or
# "EMEA"/"Europe") on its own never implies the UK; "United Kingdom Remote" / "UK Remote" match on "UK".
_UK_MARKET = re.compile(r"(?<![a-z])(?:united kingdom|uk|great britain|britain|england|scotland|wales|northern ireland"
                        r"|london|manchester|cambridge|bristol|edinburgh|glasgow|leeds|birmingham|oxford|reading"
                        r"|belfast|cardiff|liverpool|sheffield|newcastle|nottingham)(?![a-z])", re.I)
# Same-named places elsewhere: removed before matching ("New South Wales", "Cambridge, MA", "London, Ontario").
_NON_UK_TWINS = re.compile(
    r"new south wales|new england|(?:cambridge|london|reading|birmingham|manchester|oxford|bristol|newcastle"
    r"|cardiff|glasgow)\s*,\s*(?:ma|massachusetts|on|ontario|pa|pennsylvania|al|alabama|nh|new hampshire|ms"
    r"|mississippi|ct|connecticut|tn|ky|oh|va|nj|ny|ca|california|canada|usa|us|united states)(?![a-z])", re.I)


def is_uk_market(raw_location: str | None) -> bool:
    """True only with explicit UK country/region/city evidence in the location text."""
    return bool(_UK_MARKET.search(_NON_UK_TWINS.sub(" ", (raw_location or "").casefold())))
_LEVEL_SCORE = {"intern": 1.0, "graduate": 1.5, "entry": 1.5, "junior": 1.5, "associate": 0.75, "unspecified": 0.0,
                "mid": -1.0, "mid-level": -1.0, "mid-senior": -2.0, "senior": -3.0, "staff": -4.0, "principal": -4.0, "lead": -3.0,
                "manager": -4.0, "director": -5.0, "head": -5.0}

ATS_FAMILIES = (
    ("ashby", re.compile(r"(?:^|\.)ashbyhq\.com$")),
    ("greenhouse", re.compile(r"(?:^|\.)greenhouse\.io$")),
    ("lever", re.compile(r"(?:^|\.)lever\.co$")),
    ("smartrecruiters", re.compile(r"(?:^|\.)smartrecruiters\.com$")),
    ("workable", re.compile(r"(?:^|\.)workable\.com$")),
    ("workday", re.compile(r"(?:^|\.)myworkdayjobs\.com$|(?:^|\.)myworkdaysite\.com$")),
    ("aggregator:prospects", re.compile(r"(?:^|\.)prospects\.ac\.uk$")),
    ("aggregator:adzuna", re.compile(r"(?:^|\.)adzuna\.co\.uk$")),
)
DISCOVERY_CONNECTORS = {"ashby", "greenhouse", "lever", "smartrecruiters", "workable", "workday"}
# Engineering judgement of a reviewed stop-only execution profile's effort, from the hosted form shape.
DIFFICULTY = {
    "ashby": "done (reviewed profile in production)",
    "greenhouse": "medium: one hosted page, standard fields + resume upload; CAPTCHA/anti-bot and per-board custom questions to verify",
    "lever": "medium: one hosted page, resume upload with server-side parse, custom questions; hCaptcha on some boards",
    "workable": "medium: single-page form, resume parse, custom questions",
    "smartrecruiters": "medium-high: multi-step form, profile/consent steps, resume parse",
    "workday": "high: account creation/login, multi-page wizard, per-tenant forms",
}


def ats_family(url: str | None) -> str:
    host = (urlsplit(url or "").hostname or "").casefold()
    for name, pattern in ATS_FAMILIES:
        if pattern.search(host):
            return name
    return f"other:{host}" if host else "unknown"


@dataclass
class Candidate:
    job_id: str
    company: str
    title: str
    location: str
    application_url: str
    ats: str
    ats_supported: bool
    rank_score: float
    level: str
    years_required: float | None
    sponsorship: str
    eligibility: str
    competitiveness: float
    priority: float
    rationale: list[str] = field(default_factory=list)
    restrictions: list[dict] = field(default_factory=list)
    ats_pending_validation: bool = False  # a profile exists but has not had its one validated live run

    def to_dict(self) -> dict:
        return asdict(self)


# Explicit JD wording that rules the candidate out whatever the profile records (selection only; the
# eligibility module stays the authority for packages): nationality / active clearance / unrestricted RTW.
_HARD_CONDITIONS = re.compile(
    r"\bsole\s+british\s+national|\bbritish\s+(?:citizens?|nationals?)\s+only\b|\bmust\s+be\s+a\s+british\s+(?:citizen|national)"
    r"|\b(?:active|current|existing)\s+(?:dv|sc)\s+(?:uk\s+)?(?:security\s+)?clearance\b"
    r"|\bright\s+to\s+work\s+in\s+the\s+uk\s+without\s+restrictions\b|\bunrestricted\s+right\s+to\s+work\b"
    r"|\bpermanent\s+right\s+to\s+work\b|\bwould\s+not\s+require\s+visa\s+sponsorship\b", re.I)


def _explicit_hard_condition(text: str) -> str | None:
    match = _HARD_CONDITIONS.search(text)
    return match.group(0) if match else None


def _competitiveness(level, years, sponsorship, eligibility) -> tuple[float, list[str]]:
    notes, score = [], _LEVEL_SCORE.get(level, 0.0)
    notes.append(f"level {level} {score:+.2f}")
    if years is not None:
        penalty = 0.0 if years <= 1 else -0.5 if years <= 2 else -1.5 if years <= 3 else -3.0
        score += penalty
        notes.append(f"{years:g}+ years required {penalty:+.2f}")
    if sponsorship in {"explicit_no_sponsor"}:
        score -= 1.5
        notes.append("JD explicitly says no sponsorship -1.50")
    elif sponsorship in {"likely_no_sponsor"}:
        score -= 0.75
        notes.append("JD likely no sponsorship -0.75")
    if eligibility in {"needs_eligibility_review", "unknown"}:
        score -= 0.5
        notes.append(f"eligibility {eligibility} -0.50")
    return score, notes


def candidate_pool(root, config_path="config/personal_strategy.json", *, now=None, profiles=None,
                   live_within_days=3) -> list[Candidate]:
    """Every LIVE (an active source observation seen within `live_within_days`), UK-relevant, technical,
    target-occupation job without a submitted/terminal application, an open attempt, or a confirmed hard
    eligibility conflict, scored for realistic competitiveness."""
    from jobintel.analysis.application_restrictions import classify
    from jobintel.analysis.eligibility import evaluate_job_eligibility
    from jobintel.analysis.job_quality import extract_required_experience, extract_seniority
    from jobintel.config import load_ranking_config
    from jobintel.models.eligibility import EligibilityState
    from jobintel.models.taxonomy import APPLICATION_TERMINAL_STATUSES, WorkflowStatus
    from jobintel.pipeline.ingestion import rank_jobs
    from jobintel.profile_ingestion import load_candidate_profile
    from jobintel.storage.application_store import ApplicationStore
    from jobintel.storage.local_store import LocalJobStore
    from jobintel.submission.ats_profiles import UnknownExecutionProfile, profile_for_url

    candidate = load_candidate_profile(root)
    config = load_ranking_config(config_path)
    from datetime import timedelta
    from jobintel.storage.submission_store import SubmissionStore
    from jobintel.submission.apply_one import ACTIVE_STATUSES
    apps = {a.job_id: a for a in ApplicationStore(root).read_applications()}
    busy = {r["application_id"] for r in SubmissionStore(root).requests().values() if r["status"] in ACTIVE_STATUSES}
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=live_within_days)
    jobs = [j for j in LocalJobStore(root).read_jobs()
            if any(o.active and o.last_seen_at and o.last_seen_at >= cutoff for o in j.source_observations)]
    pool = []
    for item in rank_jobs(jobs, candidate=candidate, config=config, now=now or datetime.now(timezone.utc)):
        job = item.job
        if item.match.excluded or job.workflow_status == WorkflowStatus.IGNORE:
            continue
        if not _TECHNICAL_TITLE.search(job.title) or _NON_TARGET_TITLE.search(job.title):
            continue
        if not is_uk_market(job.raw_location):
            continue
        hard = _explicit_hard_condition(job.description or "")
        if hard:
            continue  # an explicit hard condition (sole British national, active clearance, unrestricted RTW)
        app = apps.get(job.id)
        if app is not None and (app.is_submitted or app.status in APPLICATION_TERMINAL_STATUSES or app.id in busy):
            continue
        decision = evaluate_job_eligibility(job, candidate)
        if decision.state == EligibilityState.HARD_INELIGIBLE:
            continue
        seniority = extract_seniority(job.title, job.description or "")
        experience = extract_required_experience(job.title, job.description or "")
        sponsorship = job.sponsorship.state.value if job.sponsorship else "unknown"
        comp, notes = _competitiveness(seniority.level, experience.years_required, sponsorship, decision.state.value)
        url = job.canonical_application_url or ""
        try:
            profile = profile_for_url(url, profiles)
            supported, pending = profile.live_validated, not profile.live_validated
        except UnknownExecutionProfile:
            supported, pending = False, False
        pool.append(Candidate(
            job_id=job.id, company=job.company, title=job.title, location=(job.raw_location or "")[:80],
            application_url=url, ats=ats_family(url), ats_supported=supported,
            rank_score=round(item.match.overall_priority, 3), level=seniority.level,
            years_required=experience.years_required, sponsorship=sponsorship, eligibility=decision.state.value,
            competitiveness=round(comp, 2), priority=round(item.match.overall_priority + comp, 3), rationale=notes,
            restrictions=[r.to_dict() for r in classify(job.description or "", source="job_description",
                                                         job_id=job.id, company=job.company)],
            ats_pending_validation=pending))
    return sorted(pool, key=lambda c: -c.priority)


def limited_companies(pool) -> dict[str, list[dict]]:
    """company (casefolded) -> the company-wide application-limit restrictions observed in its JDs."""
    from jobintel.analysis.application_restrictions import category
    out: dict[str, list[dict]] = {}
    for c in pool:
        for r in c.restrictions:
            if category(r["kind"]) == "COMPANY_WIDE_LIMIT":
                out.setdefault(c.company.casefold(), []).append(r)
    return out


def select(pool, *, size=5, per_company_cap=2, include=(), require_supported=True, min_priority=None):
    """(selected, same_company_order, deferred). `include` job ids are taken first. Within a company,
    roles are ordered by priority (competitiveness-aware); only the top `per_company_cap` are taken and the
    rest are reported as deferred -- never dropped silently. A company whose JD states a company-wide
    application limit gets at most ONE role (its strongest); its other roles are deferred under the key
    "<company> (COMPANY LIMIT APPLIES)" and nothing there runs until the user chooses."""
    limited = limited_companies(pool)
    by_id = {c.job_id: c for c in pool}
    eligible = [c for c in pool if (c.ats_supported or not require_supported)
                and (min_priority is None or c.priority >= min_priority)]
    by_company: dict[str, list] = {}
    for c in sorted(eligible, key=lambda c: -c.priority):
        by_company.setdefault(c.company.casefold(), []).append(c)
    selected = [by_id[j] for j in include if j in by_id]
    counts: dict[str, int] = {}
    for c in selected:
        counts[c.company.casefold()] = counts.get(c.company.casefold(), 0) + 1
    for c in sorted(eligible, key=lambda c: -c.priority):
        if len(selected) >= size:
            break
        key = c.company.casefold()
        if c in selected or counts.get(key, 0) >= (1 if key in limited else per_company_cap):
            continue
        selected.append(c)
        counts[key] = counts.get(key, 0) + 1
    chosen = {c.job_id for c in selected}
    order = {company: roles for company, roles in by_company.items() if len(roles) > 1}
    deferred = {(f"{company} (COMPANY LIMIT APPLIES)" if company in limited else company):
                [c for c in roles if c.job_id not in chosen]
                for company, roles in by_company.items()
                if counts.get(company, 0) >= (1 if company in limited else per_company_cap)
                and any(c.job_id not in chosen for c in roles)}
    return selected, order, deferred


def ats_audit(root, config_path="config/personal_strategy.json", *, pool=None, high_priority=None, profiles=None):
    """Per final-application ATS family: active jobs, suitable high-priority jobs, discovery and execution
    support, expected gain and difficulty."""
    from jobintel.storage.local_store import LocalJobStore
    pool = pool if pool is not None else candidate_pool(root, config_path, profiles=profiles)
    if high_priority is None:
        scores = sorted((c.priority for c in pool), reverse=True)
        high_priority = scores[min(len(scores) - 1, 49)] if scores else 0.0  # top 50 suitable
    active = [j for j in LocalJobStore(root).read_jobs() if any(o.active for o in j.source_observations)]
    rows: dict[str, dict] = {}
    for job in active:
        fam = ats_family(job.canonical_application_url)
        rows.setdefault(fam, {"ats": fam, "active_jobs": 0, "suitable_jobs": 0, "high_priority_jobs": 0})["active_jobs"] += 1
    for c in pool:
        row = rows.setdefault(c.ats, {"ats": c.ats, "active_jobs": 0, "suitable_jobs": 0, "high_priority_jobs": 0})
        row["suitable_jobs"] += 1
        row["high_priority_jobs"] += int(c.priority >= high_priority)
    for fam, row in rows.items():
        base = fam.split(":")[0]
        row["discovery"] = ("structured connector" if base in DISCOVERY_CONNECTORS
                            else "aggregator (redirects to the employer's own ATS)" if base == "aggregator" else "manual/other")
        from jobintel.submission.ats_profiles import PROFILES
        profile = next((p for p in PROFILES.values() if p.name.startswith(base + "_")), None)
        row["execution"] = ("supported (reviewed profile)" if profile and profile.live_validated
                            else "profile implemented, pending one live validation run" if profile else "not supported")
        row["difficulty"] = DIFFICULTY.get(base, "unknown: custom employer site" if base == "other" else "n/a")
    return sorted(rows.values(), key=lambda r: (-r["high_priority_jobs"], -r["suitable_jobs"])), high_priority
