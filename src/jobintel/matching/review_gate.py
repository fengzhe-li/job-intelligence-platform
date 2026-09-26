from __future__ import annotations

from dataclasses import dataclass, field

from jobintel.analysis.jd_requirements import JDRequirements
from jobintel.models.job import Job
from jobintel.models.taxonomy import GraduationYearState, ReviewGateStatus, SponsorshipState

# A high superficial keyword match must not bypass evidence validation: this gate
# looks at *which* required skills are actually evidence-backed (not just present in
# the JD text) and at explicit hard-exclusion signals already extracted elsewhere
# (graduation-year eligibility, sponsorship, missing contact details, actual
# rendered page count), never at the raw match score alone.

_SEVERITY = {
    ReviewGateStatus.AUTO_PREPARE: 0,
    ReviewGateStatus.REVIEW_REQUIRED: 1,
    ReviewGateStatus.BLOCK_AUTO_SUBMISSION: 2,
}


@dataclass(frozen=True)
class ReviewDecision:
    status: ReviewGateStatus
    reasons: list[str] = field(default_factory=list)


def _escalate(current: ReviewGateStatus, candidate: ReviewGateStatus) -> ReviewGateStatus:
    return candidate if _SEVERITY[candidate] > _SEVERITY[current] else current


def evaluate_review_gate(
    jd: JDRequirements,
    job: Job,
    matched_required_skills: list[str],
    missing_required_skills: list[str],
    partial_required_skills: list[str] | None = None,
    missing_required_contact_fields: list[str] | None = None,
    page_count: int | None = None,
    missing_cv_sections: list[str] | None = None,
    page_utilization: float | None = None,
    min_page_utilization: float = 0.0,
    github_freshness_state: str | None = None,
) -> ReviewDecision:
    partial_required_skills = partial_required_skills or []
    missing_required_contact_fields = missing_required_contact_fields or []
    reasons: list[str] = []
    total_required = len(matched_required_skills) + len(missing_required_skills)

    if not missing_required_skills:
        status = ReviewGateStatus.AUTO_PREPARE
    elif total_required and len(missing_required_skills) > total_required / 2:
        status = ReviewGateStatus.BLOCK_AUTO_SUBMISSION
        reasons.append(f"No evidence for a majority of must-have requirements: {', '.join(missing_required_skills)}")
    else:
        status = ReviewGateStatus.REVIEW_REQUIRED
        reasons.append(f"No evidence for required skill(s): {', '.join(missing_required_skills)}")

    if partial_required_skills:
        reasons.append(f"Required skill(s) supported only by partial evidence, not strong: {', '.join(partial_required_skills)}")
        status = _escalate(status, ReviewGateStatus.REVIEW_REQUIRED)

    if job.graduation_year and job.graduation_year.state == GraduationYearState.YEAR_2027_ONLY_STRICT:
        status = ReviewGateStatus.BLOCK_AUTO_SUBMISSION
        reasons.append("Job explicitly restricts eligibility by graduation year and the candidate does not match")

    if job.sponsorship and job.sponsorship.state == SponsorshipState.EXPLICIT_NO_SPONSOR:
        reasons.append("Job explicitly states no visa sponsorship -- confirm eligibility before applying")
        status = _escalate(status, ReviewGateStatus.REVIEW_REQUIRED)

    if jd.required_experience_years and jd.required_experience_years >= 3:
        reasons.append(f"JD states {jd.required_experience_years:g}+ years required -- verify this is genuinely graduate-accessible")
        status = _escalate(status, ReviewGateStatus.REVIEW_REQUIRED)

    if missing_required_contact_fields:
        reasons.append(f"Missing required contact details: {', '.join(missing_required_contact_fields)} -- never fabricated, must be set via profile-set-contact")
        status = _escalate(status, ReviewGateStatus.REVIEW_REQUIRED)

    if page_count is not None and page_count > 1:
        reasons.append(f"Generated CV does not fit on one page even after content trimming and a modest layout adjustment (rendered {page_count} pages)")
        status = _escalate(status, ReviewGateStatus.BLOCK_AUTO_SUBMISSION if page_count >= 3 else ReviewGateStatus.REVIEW_REQUIRED)

    # "Fits one page" is not success on its own: the page must also carry the
    # candidate's core CV sections and actually be used.
    if missing_cv_sections:
        reasons.append(f"Generated CV is missing core section(s): {', '.join(missing_cv_sections)} -- no supporting profile data, never invented")
        status = _escalate(status, ReviewGateStatus.REVIEW_REQUIRED)

    if page_count == 1 and page_utilization is not None and page_utilization < min_page_utilization:
        reasons.append(f"Generated CV uses only {page_utilization:.0%} of the page height -- not enough supported content to fill one page")
        status = _escalate(status, ReviewGateStatus.REVIEW_REQUIRED)

    return ReviewDecision(status=status, reasons=reasons)
