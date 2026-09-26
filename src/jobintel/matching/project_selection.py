from __future__ import annotations

from dataclasses import dataclass, field

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


ROLE_RELEVANCE_WEIGHT = 3.0


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

    # 1. Relevance is strictly primary: only consider projects with positive evidence score
    relevant_matches = [m for m in initial_matches if m.score > 0]
    if not relevant_matches:
        return [], {}, mode

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

        selected.append(best_candidate)
        remaining.remove(best_candidate)
        explanations[best_candidate.project.id] = best_explanation

        for req_name in best_candidate.matched_requirements:
            covered_requirements.add(req_name.casefold())
        for cap in profile.capabilities.values():
            if any(ev.project_id == best_candidate.project.id for ev in cap.evidence):
                covered_skills.add(cap.name.casefold())

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
