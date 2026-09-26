"""Policy-aware project selection module (Phase 3.7).

This module bridges the CV Tailoring Skill policy engine with the project
selection workflow, implementing the policy-aware selector that enforces
lexical precedence:
  evidence_truth > jd_relevance > evidence_strength > recruiter_clarity > non_redundancy > one_page_readability
"""
from __future__ import annotations

from typing import Any

from jobintel.matching.project_selection import ProjectMatch
from jobintel.matching.tailoring_skill import (
    CANONICAL_PROJECT_ALIASES,
    PROJECT_PURPOSE_MAP,
    PolicyProjectSelector,
    ProjectPolicyMatch,
    ProjectPurpose,
    TailoringSkillPolicy,
    classify_role_archetype,
    get_purpose_affinity,
    resolve_project_id,
    validate_tailored_claim,
)
from jobintel.models.candidate import CandidateProfile
from jobintel.models.job import Job

__all__ = [
    "CANONICAL_PROJECT_ALIASES",
    "PROJECT_PURPOSE_MAP",
    "PolicyProjectSelector",
    "ProjectPolicyMatch",
    "ProjectPurpose",
    "TailoringSkillPolicy",
    "classify_role_archetype",
    "get_purpose_affinity",
    "resolve_project_id",
    "select_top_projects_tailored",
    "validate_tailored_claim",
]


def select_top_projects_tailored(
    job: Job,
    profile: CandidateProfile,
    policy: TailoringSkillPolicy | None = None,
    as_of_phase: str | None = None,
    max_projects: int = 4,
) -> tuple[list[ProjectMatch], dict[str, str], str]:
    """Select projects using Phase 3.7 CV Tailoring Skill policy engine.

    Enforces lexical precedence:
      evidence_truth > jd_relevance > evidence_strength > recruiter_clarity > non_redundancy > one_page_readability
    """
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
