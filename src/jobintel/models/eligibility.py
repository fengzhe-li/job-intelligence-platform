from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EligibilityState(StrEnum):
    ELIGIBLE = "eligible"
    HARD_INELIGIBLE = "hard_ineligible"
    NEEDS_ELIGIBILITY_REVIEW = "needs_eligibility_review"
    UNKNOWN = "unknown"


class SubmissionPolicy(StrEnum):
    REQUIRE_HUMAN_REVIEW = "require_human_review"
    AUTO_APPLICATION_ALLOWED = "auto_application_allowed"


@dataclass(frozen=True)
class ExtractedRequirement:
    category: str  # "nationality_restriction", "clearance_held", "clearance_eligibility", "graduation_year", etc.
    label: str
    requirement_text: str
    is_hard_restriction: bool = True


@dataclass(frozen=True)
class EligibilityDecision:
    state: EligibilityState
    reason: str
    extracted_requirement: str = ""
    source_text: str = ""
    candidate_fact: str = ""
    decision_rule: str = ""
    requirements: tuple[ExtractedRequirement, ...] = ()

    @property
    def is_eligible(self) -> bool:
        return self.state == EligibilityState.ELIGIBLE

    @property
    def is_hard_ineligible(self) -> bool:
        return self.state == EligibilityState.HARD_INELIGIBLE

    @property
    def needs_review(self) -> bool:
        return self.state in {EligibilityState.NEEDS_ELIGIBILITY_REVIEW, EligibilityState.UNKNOWN}


@dataclass(frozen=True)
class SubmissionPolicyDecision:
    policy: SubmissionPolicy
    reason: str
    is_protected_career_stage: bool = False
    career_stage: str = "unspecified"
