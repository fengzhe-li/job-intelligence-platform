from __future__ import annotations

import re
from typing import TYPE_CHECKING

from jobintel.analysis.job_quality import extract_seniority
from jobintel.models.eligibility import (
    EligibilityDecision,
    EligibilityState,
    ExtractedRequirement,
    SubmissionPolicy,
    SubmissionPolicyDecision,
)
from jobintel.models.taxonomy import GraduationYearState

if TYPE_CHECKING:
    from jobintel.models.candidate import CandidateProfile
    from jobintel.models.job import Job


class IneligibleJobError(ValueError):
    """Raised when an operation (such as CV generation or package approval)
    is attempted on a job evaluated as HARD_INELIGIBLE."""

    def __init__(self, message: str, decision: EligibilityDecision | None = None) -> None:
        super().__init__(message)
        self.decision = decision


# --- Regex Patterns for Explicit Hard Requirements ---

_SOLE_BRITISH_PATTERNS = (
    re.compile(r"\b(?:sole\s+british\s+nationals?|sole\s+british\s+citizens?|sole\s+british\s+citizenship)\b", re.IGNORECASE),
)

_BRITISH_CITIZENSHIP_PATTERNS = (
    re.compile(r"\b(?:british\s+(?:citizens?|nationals?)\s+only|uk\s+(?:citizens?|nationals?)\s+only)\b", re.IGNORECASE),
    re.compile(r"\b(?:must\s+be\s+a\s+(?:sole\s+)?british\s+(?:national|citizen))\b", re.IGNORECASE),
    re.compile(r"\b(?:must\s+hold\s+(?:sole\s+)?british\s+citizenship)\b", re.IGNORECASE),
    re.compile(r"\b(?:uk\s+citizenship\s+required)\b", re.IGNORECASE),
)

_ACTIVE_CLEARANCE_PATTERNS = (
    re.compile(r"\b(?:must\s+(?:already\s+|currently\s+)?hold\s+(?:an?\s+)?active\s+(?:sc|dv|security)\s+clearance)\b", re.IGNORECASE),
    re.compile(r"\b(?:active\s+(?:sc|dv|security)\s+clearance\s+(?:is\s+)?required)\b", re.IGNORECASE),
    re.compile(r"\b(?:existing\s+(?:sc|dv|security)\s+clearance\s+(?:is\s+)?required)\b", re.IGNORECASE),
    re.compile(r"\b(?:current\s+(?:sc|dv|security)\s+clearance\s+(?:is\s+)?required)\b", re.IGNORECASE),
    re.compile(r"\b(?:must\s+have\s+(?:active|current)\s+(?:sc|dv|security)\s+clearance)\b", re.IGNORECASE),
)

_AMBIGUOUS_CLEARANCE_PATTERNS = (
    re.compile(r"\b(?:must\s+be\s+)?(?:eligible|eligibility)\s+(?:for|to\s+obtain)\s+(?:sc|dv|security\s+clearance|security\s+vetting)\b", re.IGNORECASE),
    re.compile(r"\b(?:ability\s+to\s+obtain|able\s+to\s+obtain|able\s+to\s+undergo|will\s+undergo|undergo)\s+(?:[a-z]+\s+)?(?:sc|dv|security\s+clearance|security\s+vetting)\b", re.IGNORECASE),
    re.compile(r"\b(?:subject\s+to\s+(?:sc|dv|security\s+clearance|security\s+vetting|baseline\s+security))\b", re.IGNORECASE),
    re.compile(r"\b(?:must\s+be\s+eligible\s+(?:for|to\s+obtain)\s+clearance)\b", re.IGNORECASE),
    re.compile(r"\b(?:requires?\s+(?:sc|dv|security)\s+clearance)\b", re.IGNORECASE),
    re.compile(r"\b(?:security\s+clearance\s+(?:is\s+)?required)\b", re.IGNORECASE),
    re.compile(r"\b(?:security\s+clearance\s+vetting|security\s+vetting)\b", re.IGNORECASE),
    re.compile(r"\b(?:sc|dv)\s+clearance\b", re.IGNORECASE),
)

_PROTECTED_CAREER_PATTERNS = (
    re.compile(r"\bgraduate\b", re.IGNORECASE),
    re.compile(r"\bnew\s+grad(?:uate)?\b", re.IGNORECASE),
    re.compile(r"\bearly\s+careers?\b", re.IGNORECASE),
    re.compile(r"\bgraduate\s+(?:programme|program|scheme|engineer|developer|role)\b", re.IGNORECASE),
)


def _snippet(text: str, start: int, end: int) -> str:
    """Extract a sentence-like snippet around a match for provenance."""
    line_start = max(text.rfind("\n", 0, start), text.rfind(".", 0, start)) + 1
    line_end_candidates = [pos for pos in (text.find("\n", end), text.find(".", end)) if pos != -1]
    line_end = min(line_end_candidates) if line_end_candidates else len(text)
    return text[line_start:line_end].strip()


def extract_hard_eligibility_requirements(job: Job) -> list[ExtractedRequirement]:
    """Extract all explicit eligibility requirements from the job posting."""
    text = f"{job.title}\n{job.description}"
    requirements: list[ExtractedRequirement] = []

    # 1. Sole British National
    for pattern in _SOLE_BRITISH_PATTERNS:
        match = pattern.search(text)
        if match:
            requirements.append(
                ExtractedRequirement(
                    category="nationality_restriction",
                    label="sole_british_national",
                    requirement_text=_snippet(text, match.start(), match.end()),
                    is_hard_restriction=True,
                )
            )
            break

    # 2. British citizenship / UK national only (if not already captured as sole British)
    if not any(r.label == "sole_british_national" for r in requirements):
        for pattern in _BRITISH_CITIZENSHIP_PATTERNS:
            match = pattern.search(text)
            if match:
                requirements.append(
                    ExtractedRequirement(
                        category="nationality_restriction",
                        label="british_citizenship_required",
                        requirement_text=_snippet(text, match.start(), match.end()),
                        is_hard_restriction=True,
                    )
                )
                break

    # 3. Active clearance held
    for pattern in _ACTIVE_CLEARANCE_PATTERNS:
        match = pattern.search(text)
        if match:
            requirements.append(
                ExtractedRequirement(
                    category="clearance_held",
                    label="active_clearance_required",
                    requirement_text=_snippet(text, match.start(), match.end()),
                    is_hard_restriction=True,
                )
            )
            break

    # 4. Ambiguous clearance / vetting eligibility (only if active clearance held wasn't already required)
    if not any(r.label == "active_clearance_required" for r in requirements):
        for pattern in _AMBIGUOUS_CLEARANCE_PATTERNS:
            match = pattern.search(text)
            if match:
                requirements.append(
                    ExtractedRequirement(
                        category="clearance_eligibility",
                        label="clearance_eligibility",
                        requirement_text=_snippet(text, match.start(), match.end()),
                        is_hard_restriction=False,
                    )
                )
                break

    # 5. Strict graduation year restriction
    if job.graduation_year and job.graduation_year.state == GraduationYearState.YEAR_2027_ONLY_STRICT:
        requirements.append(
            ExtractedRequirement(
                category="graduation_year",
                label="strict_graduation_year_2027",
                requirement_text=job.graduation_year.evidence_text or "2027 graduates only",
                is_hard_restriction=True,
            )
        )

    return requirements


def evaluate_job_eligibility(
    job: Job,
    candidate: CandidateProfile | None,
) -> EligibilityDecision:
    """Evaluate job eligibility against authoritative candidate profile facts.

    Maintains strict provenance and fail-closed safety:
    - HARD_INELIGIBLE: Explicit mandatory requirement contradicts known candidate fact.
    - NEEDS_ELIGIBILITY_REVIEW: Ambiguous requirement cannot be deterministically verified.
    - UNKNOWN: Incomplete candidate profile data prevents evaluation.
    - ELIGIBLE: All explicit hard requirements are satisfied.

    Authoritative Sponsorship Coverage Policy:
    1. JD explicitly offers sponsorship -> APPLY (no review required merely for sponsorship).
    2. JD may offer sponsorship -> APPLY (no review required merely for sponsorship).
    3. JD is silent on sponsorship -> APPLY (no review required merely for silence).
    4. JD explicitly says "no sponsorship" -> DO NOT classify HARD_INELIGIBLE;
       DO NOT discard the job; CV generation and application preparation remain allowed.
    The candidate's own work-authorisation facts come from the profile / Authoritative Answer
    Bank, never from this module.
    """
    requirements = tuple(extract_hard_eligibility_requirements(job))

    if candidate is None:
        if requirements:
            req = requirements[0]
            return EligibilityDecision(
                state=EligibilityState.UNKNOWN,
                reason=f"Candidate profile missing; cannot verify requirement: {req.requirement_text}",
                extracted_requirement=req.label,
                source_text=req.requirement_text,
                candidate_fact="No candidate profile provided",
                decision_rule="Candidate profile required to evaluate eligibility; fail closed",
                requirements=requirements,
            )
        return EligibilityDecision(
            state=EligibilityState.ELIGIBLE,
            reason="No explicit eligibility restrictions detected",
            requirements=(),
        )

    decisions: list[EligibilityDecision] = []

    for req in requirements:
        if req.label == "sole_british_national":
            cand_nat = candidate.citizenship or candidate.nationality
            is_sole = getattr(candidate, "is_sole_british_national", None)
            if cand_nat is None and is_sole is None:
                decisions.append(
                    EligibilityDecision(
                        state=EligibilityState.UNKNOWN,
                        reason="Role requires sole British nationality; candidate nationality unrecorded in profile.",
                        extracted_requirement="Sole British National",
                        source_text=req.requirement_text,
                        candidate_fact="Candidate nationality/citizenship is not specified in profile",
                        decision_rule="Candidate nationality unrecorded; fail closed",
                        requirements=requirements,
                    )
                )
            elif is_sole is False or (cand_nat and cand_nat.casefold() not in {"british", "uk", "united kingdom"}):
                decisions.append(
                    EligibilityDecision(
                        state=EligibilityState.HARD_INELIGIBLE,
                        reason="Role explicitly requires sole British nationality.",
                        extracted_requirement="Sole British National",
                        source_text=req.requirement_text,
                        candidate_fact=f"Candidate nationality is {cand_nat or 'non-British'} (not a sole British national)",
                        decision_rule="Role requires sole British nationality; contradictory to candidate authoritative profile",
                        requirements=requirements,
                    )
                )

        elif req.label == "british_citizenship_required":
            cand_nat = candidate.citizenship or candidate.nationality
            is_british = getattr(candidate, "is_british_national", None)
            if cand_nat is None and is_british is None:
                decisions.append(
                    EligibilityDecision(
                        state=EligibilityState.UNKNOWN,
                        reason="Role requires British citizenship; candidate nationality unrecorded in profile.",
                        extracted_requirement="British Citizenship Required",
                        source_text=req.requirement_text,
                        candidate_fact="Candidate nationality/citizenship is not specified in profile",
                        decision_rule="Candidate nationality unrecorded; fail closed",
                        requirements=requirements,
                    )
                )
            elif is_british is False or (cand_nat and cand_nat.casefold() not in {"british", "uk", "united kingdom"}):
                decisions.append(
                    EligibilityDecision(
                        state=EligibilityState.HARD_INELIGIBLE,
                        reason="Role explicitly requires British citizenship.",
                        extracted_requirement="British Citizenship Required",
                        source_text=req.requirement_text,
                        candidate_fact=f"Candidate nationality is {cand_nat or 'non-British'} (not British)",
                        decision_rule="Role requires British citizenship; contradictory to candidate authoritative profile",
                        requirements=requirements,
                    )
                )

        elif req.label == "active_clearance_required":
            held_clearance = getattr(candidate, "security_clearance", None)
            if not held_clearance:
                decisions.append(
                    EligibilityDecision(
                        state=EligibilityState.HARD_INELIGIBLE,
                        reason="Role explicitly requires candidate to already hold active security clearance.",
                        extracted_requirement="Active Security Clearance Held",
                        source_text=req.requirement_text,
                        candidate_fact="Candidate does not currently hold active security clearance",
                        decision_rule="Role requires active clearance already held; contradictory to candidate authoritative profile",
                        requirements=requirements,
                    )
                )

        elif req.label == "clearance_eligibility":
            clearance_eligible = getattr(candidate, "security_clearance_eligible", None)
            if clearance_eligible is True:
                continue
            elif clearance_eligible is False:
                decisions.append(
                    EligibilityDecision(
                        state=EligibilityState.HARD_INELIGIBLE,
                        reason="Role requires eligibility to obtain security clearance; profile records ineligibility.",
                        extracted_requirement="Clearance Eligibility",
                        source_text=req.requirement_text,
                        candidate_fact="Candidate profile marks clearance eligibility as false",
                        decision_rule="Candidate not eligible for clearance; contradictory to job requirement",
                        requirements=requirements,
                    )
                )
            else:
                # None: unverified / ambiguous -> NEEDS_ELIGIBILITY_REVIEW
                decisions.append(
                    EligibilityDecision(
                        state=EligibilityState.NEEDS_ELIGIBILITY_REVIEW,
                        reason="Role requires eligibility to obtain security clearance; requires manual eligibility review.",
                        extracted_requirement="Clearance Eligibility",
                        source_text=req.requirement_text,
                        candidate_fact="No deterministic authoritative clearance eligibility record in profile",
                        decision_rule="Clearance eligibility cannot be deterministically verified from authoritative profile facts; fail closed",
                        requirements=requirements,
                    )
                )

        elif req.label == "strict_graduation_year_2027":
            if candidate.graduation_year != 2027:
                decisions.append(
                    EligibilityDecision(
                        state=EligibilityState.HARD_INELIGIBLE,
                        reason="Job explicitly restricts eligibility by graduation year and candidate does not match.",
                        extracted_requirement="Strict Graduation Year 2027 Only",
                        source_text=req.requirement_text,
                        candidate_fact=f"Candidate graduation year is {candidate.graduation_year} (role strictly requires 2027)",
                        decision_rule="Strict graduation year restriction does not match candidate graduation year",
                        requirements=requirements,
                    )
                )

    # Escalation priority: HARD_INELIGIBLE > NEEDS_ELIGIBILITY_REVIEW > UNKNOWN > ELIGIBLE
    for state in (EligibilityState.HARD_INELIGIBLE, EligibilityState.NEEDS_ELIGIBILITY_REVIEW, EligibilityState.UNKNOWN):
        match = next((d for d in decisions if d.state == state), None)
        if match:
            return match

    return EligibilityDecision(
        state=EligibilityState.ELIGIBLE,
        reason="All explicit hard requirements satisfied.",
        requirements=requirements,
    )


def is_protected_career_stage(job: Job) -> tuple[bool, str]:
    """Determine if a job belongs to a protected career stage (Graduate / New Grad / Early Career)."""
    # 1. Existing classifier seniority check
    seniority = extract_seniority(job.title, job.description)
    if seniority.level == "graduate":
        return True, seniority.evidence

    # 2. Check graduation year evidence on job
    if job.graduation_year and job.graduation_year.state in {
        GraduationYearState.GRADUATE_FRIENDLY,
        GraduationYearState.YEAR_2026_ACCEPTED,
        GraduationYearState.YEAR_2027_MENTIONED,
        GraduationYearState.YEAR_2027_ONLY_STRICT,
    }:
        return True, f"Graduation year evidence: {job.graduation_year.evidence_text}"

    # 3. Broad semantic regex matching on title and description
    for pattern in _PROTECTED_CAREER_PATTERNS:
        match = pattern.search(job.title)
        if match:
            return True, f"Title matches protected career pattern '{match.group(0)}'"
        match_jd = pattern.search(job.description[:1000])
        if match_jd:
            return True, f"Job description matches protected career pattern '{match_jd.group(0)}'"

    return False, "Not in a protected career stage"


def determine_submission_policy(
    job: Job,
    unresolved_fields: list[str] | tuple[str, ...] | None = None,
) -> SubmissionPolicyDecision:
    """Determine submission policy for a job:

    - Protected career stages (Graduate / New Grad / Early Career) REQUIRE_HUMAN_REVIEW.
    - Other roles (experienced / ordinary junior) AUTO_APPLICATION_ALLOWED
      when all required fields can be answered truthfully from authoritative facts/rules.
    - Only an unresolved required application field may block autonomous final submission.
    - Current AirTap PoC continues to enforce STOP_BEFORE_FINAL_SUBMIT.
    """
    protected, reason = is_protected_career_stage(job)
    if protected:
        return SubmissionPolicyDecision(
            policy=SubmissionPolicy.REQUIRE_HUMAN_REVIEW,
            reason=f"Protected career stage ({reason}) requires explicit human review before application execution.",
            is_protected_career_stage=True,
            career_stage="graduate_early_career",
        )
    if unresolved_fields:
        return SubmissionPolicyDecision(
            policy=SubmissionPolicy.REQUIRE_HUMAN_REVIEW,
            reason=f"Unresolved required application field(s): {', '.join(unresolved_fields)}; requires human review before submission.",
            is_protected_career_stage=False,
            career_stage="experienced_or_junior",
        )
    return SubmissionPolicyDecision(
        policy=SubmissionPolicy.AUTO_APPLICATION_ALLOWED,
        reason="Role is not in a protected career stage and all required fields can be answered truthfully from authoritative facts/rules; eligible for automated processing subject to safety gates.",
        is_protected_career_stage=False,
        career_stage="experienced_or_junior",
    )
