from __future__ import annotations

import re
from dataclasses import replace, dataclass, field

from jobintel.models.candidate import CandidateProfile, Project
from jobintel.models.job import Job

# CV generation / bullet rewriting is Phase 2. This module only scores and ranks
# existing candidate projects against a specific job's extracted skill requirements --
# a read-only foundation Phase 2's CV generation calls into.


@dataclass(frozen=True)
class ProjectMatch:
    project: Project
    score: float
    matched_requirements: list[str]
    missing_requirements: list[str]
    explanation: str
    # "exact_requirement": grounded evidence for an extracted JD skill requirement.
    # "transferable": selected by the fallback below -- matches NO JD skill
    # requirement; relevant only via a software-engineering dimension the JD
    # text names and the project's own evidence supports.
    selection_basis: str = "exact_requirement"
    transferable_dimensions: tuple[str, ...] = ()


ROLE_RELEVANCE_WEIGHT = 3.0

# --- Transferable-relevance fallback ---
#
# When exact required-skill matching yields too few projects (e.g. the JD's only
# extracted requirement is a language the candidate has no evidence for), the
# selector may add a small number of projects that are relevant through a
# software-engineering dimension the JD explicitly asks about (testing, CI,
# APIs, databases, latency...) AND that the project's own linked capability
# evidence supports. This never touches matched/missing requirements: a
# transferable project matches no JD requirement, and every unsupported
# requirement stays unsupported.
#
# dimension -> (JD-text signal pattern, capability names that evidence it, evidence-text pattern).
# The evidence-text pattern is matched against the project's OWN linked evidence quotes (curated
# from what its repository implements and validates), so a project that demonstrates the
# capability -- e.g. repeated validation trials for "testing" -- counts even when it carries no
# matching tool name. It never creates a skill: it only says the JD-named dimension is evidenced.
TRANSFERABLE_DIMENSIONS: dict[str, tuple[str, frozenset[str], str | None]] = {
    "testing": (
        r"\btests?\b|\btesting\b|\btdd\b|\bbdd\b|test-driven|unit[- ]test",
        frozenset({"pytest", "junit", "jest", "unittest", "selenium", "cypress", "playwright"}),
        r"\b\d+/\d+ (?:final |strict |successful )?(?:trials|validation|runs|migrations)\b|\brepeated evaluation\b"
        r"|\btestbed\b|\btest client\b|\bintegration tests?\b|\bvalidation (?:runs|trials)\b",
    ),
    "ci_cd": (
        r"continuous (?:integration|delivery|deployment)|\bci/cd\b|\bci\b|github actions|jenkins",
        frozenset({"ci/cd", "github actions", "jenkins", "gitlab ci"}),
        None,
    ),
    "automation": (
        r"\bautomat(?:e|ed|es|ing|ion)\b",
        frozenset(),
        r"\bautomat(?:ed|es|ing|ion)\b|\borchestrat(?:e|es|ing|ion)\b|github actions runs",
    ),
    "resilience": (
        r"\bresilien(?:t|ce)\b|\breliab(?:le|ility)\b|fault[- ]toleran|high[- ]availability|\bfailover\b",
        frozenset(),
        r"\bretry\b|\bdeduplicat|\bidempoten|\bcontinuity\b|\bpreserves? an active session\b"
        r"|without disconnecting|\bfailover\b|\bdowntime\b",
    ),
    "containers_linux": (
        r"\bdocker\b|\bcontaineri[sz]|\bcontainers?\b|kubernetes|\blinux\b|\bunix\b",
        frozenset({"docker", "kubernetes", "linux", "bash"}),
        None,
    ),
    "apis_services": (
        r"\bapis?\b|\brest(?:ful)?\b|microservices?|back[- ]?end|web services?",
        frozenset({"rest", "fastapi", "flask", "django", "spring", "express", "grpc", "graphql"}),
        None,
    ),
    "databases": (
        r"\bdatabases?\b|\bsql\b|\bpersistence\b|data model",
        frozenset({"sql", "postgresql", "mysql", "sqlite", "dynamodb", "mongodb", "redis"}),
        None,
    ),
    "distributed_networked": (
        r"\bdistributed\b|\bnetwork(?:ed|ing)?\b|\bmessaging\b|message queue|event[- ]driven|\btcp\b",
        frozenset({"tcp/ip", "mqtt", "kafka", "rabbitmq", "grpc", "websocket"}),
        None,
    ),
    "systems_performance": (
        r"\blatency\b|\bthroughput\b|low[- ]level|systems programming|operating systems?",
        frozenset({"c", "c++", "rust", "linux", "criu"}),
        None,
    ),
}
# Fallback only kicks in below this many exact-match projects ...
MIN_EXACT_MATCH_PROJECTS = 2
# ... and fills to at most this many projects in total (never just to fill space: each needs
# MIN_TRANSFERABLE_SCORE of its own JD-named, evidenced dimensions).
TRANSFERABLE_FALLBACK_TARGET = 4
# A project needs at least one full-strength JD-named dimension to qualify.
MIN_TRANSFERABLE_SCORE = 1.0


def score_projects_for_job(job: Job, profile: CandidateProfile) -> list[ProjectMatch]:
    """Score each candidate project against one job's requirements.

    A project's relevance comes from two evidence-grounded signals:
    - the evidence-tier of the capabilities whose evidence is linked to that
      project (`CapabilityEvidence.project_id`), matched against
      `job.skill_requirements` -- this never invents a match: a requirement only
      counts as matched if the candidate has real evidence for it, linked to this
      specific project, via `profile_ingestion.extract_capabilities`;
    - the project's own role-track relevance (`Project.role_relevance`, inferred
      from its README/description the same way a job's role track is inferred)
      against the job's primary role track. This matters because pure
      skill-keyword overlap alone can rank a thematically irrelevant project
      above one that's obviously on-topic (e.g. a motorsport-simulation project
      for a motorsport JD) purely because it happens to share one more generic
      skill tag -- the role-track bonus corrects for that without needing any
      keyword match to the JD's literal wording.
    """
    tier_weight = {"strong": 1.0, "partial": 0.5, "missing": 0.0}
    required_names = {item.name.casefold() for item in job.skill_requirements if item.required}
    all_names = {item.name.casefold(): item for item in job.skill_requirements}
    primary_track = job.role_track_profile.primary()

    matches: list[ProjectMatch] = []
    for project in profile.projects:
        project_capabilities = [
            capability
            for capability in profile.capabilities.values()
            if any(evidence.project_id == project.id for evidence in capability.evidence)
        ]
        project_skill_names = {capability.name.casefold(): capability for capability in project_capabilities}

        matched_requirements: list[str] = []
        score = 0.0
        for name, requirement in all_names.items():
            capability = project_skill_names.get(name)
            if capability is None:
                continue
            weight = tier_weight[capability.evidence_tier]
            if weight <= 0:
                continue
            matched_requirements.append(requirement.name)
            score += weight * (1.5 if requirement.required else 1.0)

        role_bonus = 0.0
        if primary_track is not None and matched_requirements:
            role_bonus = project.role_relevance.get(primary_track.track, 0.0) * ROLE_RELEVANCE_WEIGHT
            score += role_bonus

        missing_requirements = sorted(required_names - {name.casefold() for name in matched_requirements})
        explanation_parts = []
        if matched_requirements:
            explanation_parts.append(f"Matches {len(matched_requirements)} requirement(s) via evidence: {', '.join(matched_requirements)}")
        if role_bonus > 0:
            explanation_parts.append(f"Role-track relevance to {primary_track.track.value}: +{role_bonus:.2f}")
        explanation = "; ".join(explanation_parts) or "No linked capability evidence or role-track relevance matches this job"
        matches.append(
            ProjectMatch(
                project=project,
                score=round(score, 3),
                matched_requirements=matched_requirements,
                missing_requirements=missing_requirements,
                explanation=explanation,
            )
        )
    return sorted(matches, key=lambda item: item.score, reverse=True)


def select_top_projects(matches: list[ProjectMatch], max_projects: int = 4) -> list[ProjectMatch]:
    return [match for match in matches if match.score > 0][:max_projects]


def score_transferable_projects(
    job: Job,
    profile: CandidateProfile,
    exclude_ids: set[str] | None = None,
) -> list[ProjectMatch]:
    """Score projects on transferable software-engineering relevance only.

    A dimension counts only if the JD text names it AND the project has its own
    linked (strong/partial) capability evidence for it. Returned matches carry
    no matched requirements -- every required JD skill stays missing."""
    exclude_ids = exclude_ids or set()
    jd_text = f"{job.title}\n{job.description or ''}".casefold()
    jd_dimensions = {
        name: (capabilities, evidence_pattern)
        for name, (pattern, capabilities, evidence_pattern) in TRANSFERABLE_DIMENSIONS.items()
        if re.search(pattern, jd_text)
    }
    if not jd_dimensions:
        return []

    tier_weight = {"strong": 1.0, "partial": 0.5, "missing": 0.0}
    required = sorted(item.name for item in job.skill_requirements if item.required)
    primary_track = job.role_track_profile.primary()

    matches: list[ProjectMatch] = []
    for project in profile.projects:
        if project.id in exclude_ids:
            continue
        project_capabilities = {
            capability.name.casefold(): capability
            for capability in profile.capabilities.values()
            if any(evidence.project_id == project.id for evidence in capability.evidence)
        }
        # The project's own strong/partial evidence quotes, for evidence-text signals.
        quotes = [(evidence.quote or "", capability.evidence_tier)
                  for capability in project_capabilities.values() if tier_weight[capability.evidence_tier] > 0
                  for evidence in capability.evidence if evidence.project_id == project.id]
        score = 0.0
        supported: list[str] = []
        for dimension, (capability_names, evidence_pattern) in jd_dimensions.items():
            evidenced = sorted(
                (project_capabilities[name] for name in capability_names if name in project_capabilities),
                key=lambda capability: -tier_weight[capability.evidence_tier],
            )
            if evidenced and tier_weight[evidenced[0].evidence_tier] > 0:
                score += tier_weight[evidenced[0].evidence_tier]
                supported.append(f"{dimension} ({', '.join(c.name for c in evidenced if tier_weight[c.evidence_tier] > 0)})")
                continue
            hits = [(match.group(0), tier) for quote, tier in quotes if evidence_pattern
                    for match in re.finditer(evidence_pattern, quote, re.IGNORECASE)]
            if hits:
                score += max(tier_weight[tier] for _, tier in hits)
                shown = list(dict.fromkeys(text for text, _ in hits))[:3]
                supported.append(f"{dimension} (evidence: {', '.join(repr(text) for text in shown)})")
        if score < MIN_TRANSFERABLE_SCORE:
            continue
        if primary_track is not None:
            score += project.role_relevance.get(primary_track.track, 0.0) * ROLE_RELEVANCE_WEIGHT
        dimensions = tuple(item.split(" (", 1)[0] for item in supported)
        unsupported = f" -- no grounded evidence for required: {', '.join(required)}" if required else ""
        matches.append(
            ProjectMatch(
                project=project,
                score=round(score, 3),
                matched_requirements=[],
                missing_requirements=[name.casefold() for name in required],
                explanation=f"Transferable relevance (JD-named dimensions with project evidence): {'; '.join(supported)}{unsupported}",
                selection_basis="transferable",
                transferable_dimensions=dimensions,
            )
        )
    # Stable sort: ties keep the profile's project order (deterministic).
    return sorted(matches, key=lambda item: (-item.score, -len(item.transferable_dimensions)))


def _with_transferable_fallback(
    job: Job,
    profile: CandidateProfile,
    selected: list[ProjectMatch],
    explanations: dict[str, str],
    max_projects: int,
) -> tuple[list[ProjectMatch], dict[str, str]]:
    """Exact-requirement matches always come first; transferable projects are
    only appended when exact matching produced too few."""
    if len(selected) >= MIN_EXACT_MATCH_PROJECTS:
        return selected, explanations
    room = min(max_projects, TRANSFERABLE_FALLBACK_TARGET) - len(selected)
    if room <= 0:
        return selected, explanations
    fallback = score_transferable_projects(job, profile, exclude_ids={match.project.id for match in selected})[:room]
    explanations = dict(explanations)
    for match in fallback:
        explanations[match.project.id] = match.explanation
    return selected + fallback, explanations


def score_projects_for_job_hybrid(
    job: Job,
    profile: CandidateProfile,
    hybrid_result: Any | None = None,
) -> list[ProjectMatch]:
    """Score candidate projects using both deterministic and validated Phase 3.5A semantic signals.

    Fails closed: if hybrid_result is None, unassisted, or contains any invalid
    citations, falls back safely to deterministic score_projects_for_job.
    """
    if hybrid_result is None or getattr(getattr(hybrid_result, "metadata", None), "used", "") != "gemini_assisted":
        return score_projects_for_job(job, profile)

    # Validate that semantic signals only reference existing projects and existing project evidence
    project_by_id = {p.id: p for p in profile.projects}
    ev_to_project: dict[str, str] = {}
    for cap in profile.capabilities.values():
        for ev in cap.evidence:
            ev_to_project[ev.id] = ev.project_id

    for match in getattr(hybrid_result, "semantic_matches", []):
        pid = match.get("project_id")
        eid = match.get("evidence_id")
        if pid not in project_by_id:
            return score_projects_for_job(job, profile)
        if eid not in ev_to_project or ev_to_project[eid] != pid:
            return score_projects_for_job(job, profile)

    tier_weight = {"strong": 1.0, "partial": 0.5, "missing": 0.0}
    required_names = {item.name.casefold() for item in job.skill_requirements if item.required}
    all_names = {item.name.casefold(): item for item in job.skill_requirements}
    primary_track = job.role_track_profile.primary()
    req_by_id = {r.id: r for r in getattr(hybrid_result, "requirements", [])}

    # Group validated semantic matches by project
    semantic_by_project: dict[str, list[dict]] = {}
    for match in getattr(hybrid_result, "semantic_matches", []):
        pid = match.get("project_id")
        if pid:
            semantic_by_project.setdefault(pid, []).append(match)

    matches: list[ProjectMatch] = []
    for project in profile.projects:
        project_capabilities = [
            capability
            for capability in profile.capabilities.values()
            if any(evidence.project_id == project.id for evidence in capability.evidence)
        ]
        project_skill_names = {capability.name.casefold(): capability for capability in project_capabilities}

        matched_requirements: list[str] = []
        score = 0.0

        # 1. Deterministic matches
        for name, requirement in all_names.items():
            capability = project_skill_names.get(name)
            if capability is None:
                continue
            weight = tier_weight[capability.evidence_tier]
            if weight <= 0:
                continue
            matched_requirements.append(requirement.name)
            score += weight * (1.5 if requirement.required else 1.0)

        # 2. Semantic matches (e.g. FastAPI -> REST API)
        semantic_matched_names: list[str] = []
        for sem in semantic_by_project.get(project.id, []):
            rid = sem.get("requirement_id")
            req = req_by_id.get(rid)
            if not req:
                continue
            label = req.normalized
            if label.casefold() not in {m.casefold() for m in matched_requirements}:
                s_weight = 0.5 if sem.get("strength") == "partial" else 1.0
                is_req = req.modality == "required"
                score += s_weight * (1.2 if is_req else 0.8)
                semantic_matched_names.append(label)

        # 3. Role-track bonus
        role_bonus = 0.0
        if primary_track is not None:
            role_bonus = project.role_relevance.get(primary_track.track, 0.0) * ROLE_RELEVANCE_WEIGHT
            score += role_bonus

        all_matched = sorted(set(matched_requirements + semantic_matched_names))
        missing_requirements = sorted(required_names - {name.casefold() for name in all_matched})

        explanation_parts = []
        if matched_requirements:
            explanation_parts.append(f"Direct requirements: {', '.join(matched_requirements)}")
        if semantic_matched_names:
            explanation_parts.append(f"Semantic support: {', '.join(semantic_matched_names)}")
        if role_bonus > 0:
            explanation_parts.append(f"Role relevance ({primary_track.track.value}): +{role_bonus:.2f}")

        explanation = "; ".join(explanation_parts) or "No direct or semantic matches"
        matches.append(
            ProjectMatch(
                project=project,
                score=round(score, 3),
                matched_requirements=all_matched,
                missing_requirements=missing_requirements,
                explanation=explanation,
            )
        )

    return sorted(matches, key=lambda item: item.score, reverse=True)


# Evidence category weight per role archetype (tailoring_skill.classify_role_archetype): the JD's theme decides
# which of a project's capability slices gets space (e.g. a web role prefers a project's frontend evidence).
ARCHETYPE_CATEGORY_WEIGHTS = {
    "fullstack": {"Frontend": 1.0, "Backend": 0.4},
    "platform_cloud_systems": {"Systems": 1.0, "Cloud": 0.8, "DevOps": 0.6, "Networking": 0.8, "Testing": 0.3},
    "ai_applied_ai": {"ML/AI": 1.0, "Modeling": 0.6, "ML": 0.8, "Backend": 0.3},
    "backend_general_swe": {"Backend": 1.0, "Database": 0.5, "Testing": 0.2},
    "fintech": {"Backend": 0.6, "ML/AI": 0.4, "Database": 0.4},
    "motorsport": {"Modeling": 1.0, "Embedded": 1.0, "Systems": 0.6},
}


THEME_WEIGHT = 2.5


# The JD's own wording strengthens a category beyond its archetype default (a reliability/networking JD weighs
# systems work more): weight x (1 + 0.25 per distinct signal, capped at x2).
_CATEGORY_SIGNALS = {
    "Systems": r"\b(linux|network\w*|tcp|reliab\w*|incident|recover\w*|failover|on-call|resilien\w*|scripting|kernel)\b",
    "Frontend": r"\b(react|typescript|component|ui|ux|front[- ]?end|browser|css|accessib\w*)\b",
    "ML/AI": r"\b(llms?|genai|generative|machine learning|models?|agents?|rag|evaluation)\b",
    "Cloud": r"\b(aws|gcp|azure|cloud|serverless|lambda)\b",
    "Backend": r"\b(apis?|backend|microservices?|databases?|python)\b",
    "Embedded": r"(?<![\w+#])(?:c/c\+\+|c\+\+|embedded|firmware|microcontrollers?|kernel|low[- ]level|device drivers?)(?![\w+#])",
}
# A category the archetype does not list still counts when the JD itself signals it strongly (2026-10-15: a
# Linux / C/C++ role titled only "Software Engineer" must favour Linux-systems and C evidence): it gets the base
# weight of an archetype's primary category, scaled by its signals like any other.
SIGNALLED_CATEGORY_MIN_SIGNALS = 2
SIGNALLED_CATEGORY_BASE = 1.0
# Only low-level evidence categories are added this way; every other category keeps its archetype weight.
SIGNALLED_CATEGORIES = ("Systems", "Embedded")


def archetype_category_weights(job: Job) -> dict:
    import re
    from jobintel.matching.tailoring_skill import classify_role_archetype
    weights = dict(ARCHETYPE_CATEGORY_WEIGHTS.get(classify_role_archetype(job), {}))
    text = f"{job.title} {job.description or ''}".casefold()
    for category, pattern in _CATEGORY_SIGNALS.items():
        signals = len(set(re.findall(pattern, text)))
        if category in weights:
            weights[category] = round(weights[category] * min(2.0, 1 + 0.25 * signals), 3)
        elif category in SIGNALLED_CATEGORIES and signals >= SIGNALLED_CATEGORY_MIN_SIGNALS:
            weights[category] = round(SIGNALLED_CATEGORY_BASE * min(2.0, 1 + 0.25 * signals), 3)
    return weights


# Recruiter-facing relevance check (2026-10-15). A requirement is "ubiquitous" when at least this share of the
# candidate projects match it (Python, REST, Docker, SQL, testing ...): matching it says nothing about whether
# the project's problem shape fits the role.
UBIQUITOUS_REQUIREMENT_SHARE = 0.5
# A project matching only ubiquitous requirements keeps its slot only with at least this share of the best
# role-specific (theme) evidence among the candidates.
GENERIC_ONLY_MIN_THEME_SHARE = 0.5
MIN_SELECTED_PROJECTS = 2
GENERIC_ONLY_FACTOR = 0.5


def generic_only_projects(matches: list, profile: CandidateProfile, theme_weights: dict) -> set[str]:
    """Projects that would occupy a CV slot only through generic shared technologies: every JD requirement
    they match is ubiquitous among the candidates AND their role-specific capability evidence is weak relative
    to the strongest candidate. They are demoted (GENERIC_ONLY_FACTOR), not deleted: such a project yields its
    slot to any candidate with role-specific evidence and fills one only when nothing stronger exists.
    No project, company or domain name is consulted."""
    if len(matches) <= MIN_SELECTED_PROJECTS:
        return set()
    counts: dict[str, int] = {}
    for m in matches:
        for req in {r.casefold() for r in m.matched_requirements}:
            counts[req] = counts.get(req, 0) + 1
    threshold = max(2, UBIQUITOUS_REQUIREMENT_SHARE * len(matches))
    theme = {m.project.id: capability_theme_bonus(m.project, profile, theme_weights) for m in matches}
    best_theme = max(theme.values(), default=0.0)

    def generic_only(m) -> bool:
        return all(counts.get(r.casefold(), 0) >= threshold for r in m.matched_requirements)

    return {m.project.id for m in matches
            if generic_only(m) and theme[m.project.id] < GENERIC_ONLY_MIN_THEME_SHARE * best_theme}


def capability_theme_bonus(project, profile: CandidateProfile, weights: dict) -> float:
    """JD-theme fit from the project's IMPLEMENTED capability slices (code-derived evidence categories):
    each matching category counts once, so a project's frontend counts for a web role even when its
    curated identity is a model."""
    categories = {ev.category for cap in profile.capabilities.values() for ev in cap.evidence
                  if ev.project_id == project.id and ":capmap:" in ev.id and ev.confidence >= 0.9}
    return round(sum(weights.get(c, 0.0) for c in categories) * THEME_WEIGHT, 3)


def select_top_projects_hybrid(
    job: Job,
    profile: CandidateProfile,
    hybrid_result: Any | None = None,
    max_projects: int = 4,
) -> tuple[list[ProjectMatch], dict[str, str], str]:
    """Select 2 to 4 projects, balancing relevance and complementary diversity.

    Penalizes redundancy when multiple projects only demonstrate identical skills,
    favoring projects with complementary evidence (e.g. backend + cloud/systems).
    """
    initial_matches = score_projects_for_job_hybrid(job, profile, hybrid_result)
    is_assisted = (
        hybrid_result is not None
        and getattr(getattr(hybrid_result, "metadata", None), "used", "") == "gemini_assisted"
    )
    mode = "gemini_assisted" if is_assisted else "deterministic"

    # 1. Relevance is strictly primary: only consider projects with positive evidence score -- a JD skill match,
    # or implemented capability slices in the JD's theme (a Linux-systems project for a platform role).
    theme_weights = archetype_category_weights(job)
    relevant_matches = [m for m in initial_matches if m.score > 0 or capability_theme_bonus(m.project, profile, theme_weights) > 0]
    demoted = generic_only_projects(relevant_matches, profile, theme_weights)
    if not relevant_matches:
        selected, explanations = _with_transferable_fallback(job, profile, [], {}, max_projects)
        return selected, explanations, mode

    required_names = {item.name.casefold() for item in job.skill_requirements if item.required}
    primary_track = job.role_track_profile.primary()

    # Greedy selection where diversity is strictly subordinate to JD relevance.
    # Already-covered requirements receive a redundancy discount (0.4x), rewarding projects
    # that fulfill previously uncovered JD requirements, but never displacing materially stronger evidence.
    selected: list[ProjectMatch] = []
    covered_requirements: set[str] = set()
    covered_skills: set[str] = set()
    remaining = list(relevant_matches)
    explanations: dict[str, str] = {}

    while remaining and len(selected) < max_projects:
        best_candidate = None
        best_incremental_score = -1.0
        best_explanation = ""

        for match in remaining:
            inc_score = 0.0
            new_reqs = []
            overlap_reqs = []

            for req_name in match.matched_requirements:
                req_lower = req_name.casefold()
                is_required = req_lower in required_names
                base_weight = 1.5 if is_required else 1.0

                if req_lower in covered_requirements:
                    inc_score += base_weight * 0.4
                    overlap_reqs.append(req_name)
                else:
                    inc_score += base_weight * 1.0
                    new_reqs.append(req_name)

            if primary_track is not None:
                inc_score += match.project.role_relevance.get(primary_track.track, 0.0) * ROLE_RELEVANCE_WEIGHT
            inc_score += capability_theme_bonus(match.project, profile, theme_weights)
            if match.project.id in demoted:
                inc_score *= GENERIC_ONLY_FACTOR

            inc_score = round(inc_score, 3)

            if inc_score > best_incremental_score:
                best_incremental_score = inc_score
                best_candidate = match
                reason = match.explanation
                if new_reqs and covered_requirements:
                    reason += f"; Uncovered requirements: {', '.join(sorted(new_reqs)[:3])}"
                elif not new_reqs and overlap_reqs:
                    reason += "; Overlaps existing requirements (redundancy discount applied)"
                best_explanation = reason

        if best_candidate is None or best_incremental_score <= 0:
            break

        remaining.remove(best_candidate)
        # The page emphasis (focus / secondary) follows this JD relevance, theme included.
        theme = capability_theme_bonus(best_candidate.project, profile, theme_weights)
        best_candidate = replace(best_candidate, score=round(best_candidate.score + theme, 3))
        selected.append(best_candidate)
        explanations[best_candidate.project.id] = best_explanation

        for req_name in best_candidate.matched_requirements:
            covered_requirements.add(req_name.casefold())
        for cap in profile.capabilities.values():
            if any(ev.project_id == best_candidate.project.id for ev in cap.evidence):
                covered_skills.add(cap.name.casefold())

    selected, explanations = _with_transferable_fallback(job, profile, selected, explanations, max_projects)
    return selected, explanations, mode


def select_top_projects_tailored(
    job: Job,
    profile: CandidateProfile,
    policy: Any | None = None,
    as_of_phase: str | None = None,
    max_projects: int = 4,
) -> tuple[list[ProjectMatch], dict[str, str], str]:
    """Select projects using Phase 3.7 CV Tailoring Skill policy engine.

    Enforces lexical precedence:
      evidence_truth > jd_relevance > evidence_strength > recruiter_clarity > non_redundancy > one_page_readability
    Falls back safely to select_top_projects_hybrid if policy cannot be loaded or executed.
    """
    from jobintel.matching.tailoring_skill import PolicyProjectSelector
    try:
        selector = PolicyProjectSelector(policy)
        policy_matches = selector.select_projects(job, profile, as_of_phase=as_of_phase, max_projects=max_projects)
        project_matches = [
            ProjectMatch(
                project=m.project,
                score=m.score,
                matched_requirements=m.matched_requirements,
                missing_requirements=m.missing_requirements,
                explanation=m.explanation,
            )
            for m in policy_matches
        ]
        explanations = {m.project.id: m.explanation for m in policy_matches}
        return project_matches, explanations, "tailored_policy"
    except Exception:
        return select_top_projects_hybrid(job, profile, max_projects=max_projects)
