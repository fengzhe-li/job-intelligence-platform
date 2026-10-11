"""Authoritative question resolution engine and work authorization policy.

Candidate facts (status, expiry, right to work, sponsorship need, contact and education values)
come from `candidate_facts` -- the git-ignored local module -- never from literals here. Without
them every candidate-specific rule fails closed to human input.

Question-resolution rules:
1. "Do you currently have the legal right to work in the UK?" -> YES when the candidate holds it
2. "Are you currently permitted to work in the UK?" -> same, where wording does not require unrestricted/permanent status
3. "Will you now or in the future require sponsorship?" -> YES when the candidate may need it
4. "Will you require Skilled Worker sponsorship in the future?" -> same
5. "Do you have permanent/unrestricted right to work in the UK?" -> never mapped from ordinary right-to-work (NO when not held)
6. Never transform an authoritative YES-to-sponsorship fact into NO merely because the employer says sponsorship is unavailable.
7. Efficiency rule: Do not repeatedly ask the user questions whose authoritative answer is already stored.
8. A no-sponsorship JD alone must not interrupt application preparation. Only an unresolved required application field may block autonomous final submission.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import TYPE_CHECKING

from jobintel.models.answer_bank import FreeTextPolicy
from jobintel.submission.candidate_facts import question_facts

if TYPE_CHECKING:
    from jobintel.models.candidate import CandidateProfile
    from jobintel.models.job import Job


@dataclass(frozen=True)
class AuthoritativeWorkAuthFacts:
    """Canonical candidate work authorization facts in the UK. None = unknown (fails closed)."""

    current_uk_visa_status: str | None = None
    uk_visa_expiry: str | None = None
    has_current_legal_right_to_work_uk: bool | None = None
    student_visa_conditions_apply: bool | None = None
    intends_graduate_route: bool | None = None
    may_require_skilled_worker_sponsorship_future: bool | None = None
    has_permanent_or_unrestricted_right_to_work: bool | None = None


# The candidate's literal answers, keyed by Answer Bank field id, and work-authorisation facts.
CANDIDATE_ANSWERS: dict[str, str] = {k: v for k, v in question_facts().items() if k not in {"work_auth", "explanations"}}
AUTHORITATIVE_FACTS = AuthoritativeWorkAuthFacts(**question_facts().get("work_auth", {}))
# The candidate's own wording for a rule's reason/provenance (explanatory text only; never an answer).
CANDIDATE_EXPLANATIONS: dict[str, dict[str, str]] = dict(question_facts().get("explanations", {}))


@dataclass(frozen=True)
class QuestionResolutionResult:
    """Structured resolution for an application form question or field prompt."""

    question_text: str
    resolved: bool
    answer_value: str | None
    field_category: str
    requires_human_input: bool
    reason: str
    provenance: str
    rule_applied: str
    from_cache: bool = False


# --- Regex Patterns for Question Matching ---

# Permanent / Unrestricted right-to-work patterns:
# MUST NOT map from ordinary right-to-work. Candidate does not hold permanent/unrestricted status.
_PERMANENT_OR_UNRESTRICTED_RTW_PATTERNS = (
    re.compile(r"\bpermanent(?:\s+or\s+unrestricted)?\s+right\s+to\s+work\b", re.IGNORECASE),
    re.compile(r"\bunrestricted\s+right\s+to\s+work\b", re.IGNORECASE),
    re.compile(r"\bindefinite\s+leave\s+to\s+remain\b", re.IGNORECASE),
    re.compile(r"\bsettled\s+status\b", re.IGNORECASE),
    re.compile(r"\bwithout\s+(?:any\s+)?restrictions?\b", re.IGNORECASE),
    re.compile(r"\bpermanent\s+residen(?:cy|t)\b", re.IGNORECASE),
)

# Ordinary right-to-work patterns (without permanent/unrestricted restriction)
_ORDINARY_RTW_PATTERNS = (
    re.compile(r"\b(?:do\s+you\s+(?:currently\s+)?have\s+(?:the\s+)?(?:legal\s+)?right\s+to\s+work)\b", re.IGNORECASE),
    re.compile(r"\b(?:are\s+you\s+(?:currently\s+)?permitted\s+to\s+work)\b", re.IGNORECASE),
    re.compile(r"\b(?:are\s+you\s+(?:legally\s+)?(?:authorized|authorised|permitted|eligible)\s+to\s+work)\b", re.IGNORECASE),
    re.compile(r"\b(?:right\s+to\s+work\s+in\s+the\s+uk)\b", re.IGNORECASE),
    re.compile(r"\b(?:right\s+to\s+work\s+in\s+the\s+united\s+kingdom)\b", re.IGNORECASE),
    re.compile(r"\b(?:eligible\s+to\s+work\s+in\s+the\s+uk)\b", re.IGNORECASE),
    re.compile(r"\b(?:authorized\s+to\s+work\s+in\s+the\s+uk)\b", re.IGNORECASE),
    re.compile(r"\b(?:current\s+legal\s+right\s+to\s+work)\b", re.IGNORECASE),
)

# Sponsorship requirement patterns (now or future, skilled worker)
_SPONSORSHIP_REQUIRED_PATTERNS = (
    re.compile(r"\b(?:will\s+you|do\s+you)\s+(?:now\s+or\s+in\s+the\s+future\s+)?(?:require|need)\s+(?:(?:visa|skilled\s+worker|work\s+visa|immigration)\s+)?sponsorship\b", re.IGNORECASE),
    re.compile(r"\b(?:require|need)\s+(?:(?:visa|skilled\s+worker|work\s+visa|immigration)\s+)?sponsorship\b", re.IGNORECASE),
    re.compile(r"\b(?:require|need)\s+sponsorship\s+(?:now\s+or\s+in\s+the\s+future|in\s+the\s+future|to\s+work)\b", re.IGNORECASE),
    re.compile(r"\bskilled\s+worker\s+sponsorship\b", re.IGNORECASE),
)

# Driving licence patterns
_FULL_UK_DRIVING_LICENCE_PATTERNS = (
    re.compile(r"\b(?:full\s+uk\s+driving\s+licen[sc]e|valid\s+uk\s+driving\s+licen[sc]e|hold\s+(?:a\s+)?full\s+uk\s+driving\s+licen[sc]e)\b", re.IGNORECASE),
)
_ANY_DRIVING_LICENCE_PATTERNS = (
    re.compile(r"\b(?:do\s+you\s+(?:have|hold)\s+(?:a\s+)?driving\s+licen[sc]e)\b", re.IGNORECASE),
)

# Security clearance patterns
_HOLD_SECURITY_CLEARANCE_PATTERNS = (
    re.compile(r"\b(?:do\s+you\s+(?:currently\s+)?hold\s+(?:uk\s+|active\s+)?(?:security\s+)?clearance|currently\s+hold\s+security\s+clearance|hold\s+active\s+security\s+clearance)\b", re.IGNORECASE),
)
_CLEARANCE_ELIGIBILITY_PATTERNS = (
    re.compile(r"\b(?:eligible|eligibility)\s+(?:for|to\s+obtain)\s+(?:sc|dv|security\s+clearance|clearance)\b", re.IGNORECASE),
    re.compile(r"\b(?:are\s+you\s+eligible\s+(?:for|to\s+obtain)\s+security\s+clearance)\b", re.IGNORECASE),
)

# Criminal convictions patterns
_COMPLEX_CRIMINAL_LEGAL_PATTERNS = (
    re.compile(r"\b(?:unspent|spent|caution|cautions|reprimand|reprimands|final\s+warning|pending\s+proceedings|pending\s+charges|dbs|enhanced\s+dbs|disclosure\s+and\s+barring)\b", re.IGNORECASE),
)
_SIMPLE_CRIMINAL_CONVICTIONS_PATTERNS = (
    re.compile(r"\b(?:do\s+you\s+have\s+(?:any\s+)?criminal\s+convictions?|have\s+you\s+(?:ever\s+)?been\s+convicted\s+of\s+a\s+criminal\s+offen[sc]e|any\s+criminal\s+convictions?)\b", re.IGNORECASE),
)

# Disability & reasonable adjustments patterns
_DISABILITY_PATTERNS = (
    re.compile(r"\b(?:do\s+you\s+(?:consider\s+yourself\s+to\s+)?have\s+a\s+disability|disability\s+status|have\s+a\s+disability\s+or\s+health\s+condition)\b", re.IGNORECASE),
)
_ADJUSTMENTS_PATTERNS = (
    re.compile(r"\b(?:reasonable\s+adjustments?|adjustments?\s+(?:required|for\s+the\s+interview|during\s+the\s+application))\b", re.IGNORECASE),
)

# Diversity / Demographics
_DIVERSITY_PATTERNS = (
    re.compile(r"\b(?:ethnic(?:ity)?|ethnic\s+origin|racial\s+group|religion|religious\s+belief|sexual\s+orientation|gender\s+identity)\b", re.IGNORECASE),
)

# Gaokao / A-level equivalence patterns
_A_LEVEL_EQUIVALENCE_PATTERNS = (
    re.compile(r"\b(?:a-level(?:s)?\s+(?:grades?|results?|equivalent)|uk\s+a-levels?|equivalent\s+(?:uk\s+)?a-levels?)\b", re.IGNORECASE),
)

# UCL Completion / Award date patterns (unknown)
_UCL_COMPLETION_PATTERNS = (
    re.compile(r"\b(?:expected\s+completion\s+date|formal\s+award\s+date|msc\s+completion\s+date|ucl\s+graduation\s+date)\b", re.IGNORECASE),
)

# Address move-in date patterns (unknown)
_MOVE_IN_DATE_PATTERNS = (
    re.compile(r"\b(?:move\s*in\s*date|date\s+moved\s+(?:in|into)|date\s+of\s+move|when\s+did\s+you\s+move|address\s+start\s+date|lived\s+at\s+this\s+address\s+since)\b", re.IGNORECASE),
)

# Address and contact patterns
_ADDRESS_PATTERNS = (
    re.compile(r"\b(?:current\s+address|home\s+address|full\s+address|residential\s+address|postal\s+address|what\s+is\s+your\s+(?:current\s+)?address)\b", re.IGNORECASE),
)
_POSTCODE_PATTERNS = (
    re.compile(r"\b(?:postcode|postal\s+code|zip(?:\s*code)?)\b", re.IGNORECASE),
)
_NOTICE_PERIOD_PATTERNS = (
    re.compile(r"\b(?:notice\s+period|current\s+notice\s+period|availability\s+to\s+start)\b", re.IGNORECASE),
)
_NATIONALITY_PATTERNS = (
    re.compile(r"\b(?:nationality|country\s+of\s+nationality|citizenship|country\s+of\s+citizenship)\b", re.IGNORECASE),
)
_FIRST_NAME_PATTERNS = (
    re.compile(r"\b(?:what\s+is\s+your\s+)?(?:first\s+name|given\s+name|forename|legal\s+first\s+name)\b", re.IGNORECASE),
)
_LAST_NAME_PATTERNS = (
    re.compile(r"\b(?:what\s+is\s+your\s+)?(?:last\s+name|surname|family\s+name|legal\s+last\s+name|legal\s+surname)\b", re.IGNORECASE),
)
_FULL_NAME_PATTERNS = (
    re.compile(r"\b(?:what\s+is\s+your\s+)?(?:full\s+name|legal\s+name|legal\s+full\s+name)\b", re.IGNORECASE),
)
_PHONE_PATTERNS = (
    re.compile(r"\b(?:what\s+is\s+your\s+)?(?:phone(?:\s*number)?|telephone|mobile(?:\s*number)?)\b", re.IGNORECASE),
)
_EMAIL_PATTERNS = (
    re.compile(r"\b(?:what\s+is\s+your\s+)?(?:email(?:\s*address)?)\b", re.IGNORECASE),
)
_UG_INST_PATTERNS = (
    re.compile(r"\b(?:undergraduate\s+university|undergraduate\s+institution|bachelors?\s+university|bachelors?\s+institution)\b", re.IGNORECASE),
)
_UG_DEG_PATTERNS = (
    re.compile(r"\b(?:undergraduate\s+degree|bachelors?\s+degree|beng\s+degree)\b", re.IGNORECASE),
)
_UG_CLASS_PATTERNS = (
    re.compile(r"\b(?:undergraduate\s+classification|undergraduate\s+grade|degree\s+grade|degree\s+classification)\b", re.IGNORECASE),
)
_PG_INST_PATTERNS = (
    re.compile(r"\b(?:postgraduate\s+university|postgraduate\s+institution|masters?\s+university|masters?\s+institution)\b", re.IGNORECASE),
)
_PG_DEG_PATTERNS = (
    re.compile(r"\b(?:postgraduate\s+degree|masters?\s+degree|msc\s+degree)\b", re.IGNORECASE),
)


# Standard candidate profile fields
_CONTACT_FIELD_MAP = {
    "name": ("name", "full name", "your name"),
    "email": ("email", "email address"),
    "phone": ("phone", "phone number", "telephone", "mobile"),
    "location": ("location", "current location", "city", "address"),
    "linkedin_url": ("linkedin", "linkedin url", "linkedin profile"),
    "github_url": ("github", "github url", "github profile"),
}


def evaluate_free_text_policy(job: Job | str, is_substantive: bool = True) -> FreeTextPolicy:
    """Determine policy for AI-generated free-text drafting:
    - Graduate / New Grad / Early Career + substantive: HUMAN_REVIEW_REQUIRED
    - Ordinary roles + substantive: AUTO_ALLOWED
    - Simple factual free-text: FACTUAL_AUTOFILL
    """
    if not is_substantive:
        return FreeTextPolicy.FACTUAL_AUTOFILL

    text = job if isinstance(job, str) else f"{job.title} {job.description}"
    lower = text.casefold()

    protected_keywords = (
        "graduate", "grad ", "new grad", "early career", "early careers",
        "campus", "intern", "internship", "apprentice", "trainee",
    )
    if any(kw in lower for kw in protected_keywords):
        return FreeTextPolicy.HUMAN_REVIEW_REQUIRED

    return FreeTextPolicy.AUTO_ALLOWED


def resolve_application_question(
    question_text: str,
    *,
    candidate: CandidateProfile | None = None,
    job: Job | None = None,
    stored_answers: dict[str, str] | None = None,
    facts: AuthoritativeWorkAuthFacts = AUTHORITATIVE_FACTS,
    answers: dict[str, str] | None = None,
) -> QuestionResolutionResult:
    """Resolve one question (see `_resolve_application_question`). For the candidate's own
    facts (the defaults) a resolved rule's explanation uses the candidate's own wording."""
    result = _resolve_application_question(question_text, candidate=candidate, job=job,
                                           stored_answers=stored_answers, facts=facts, answers=answers)
    wording = CANDIDATE_EXPLANATIONS.get(result.rule_applied)
    if wording and answers is None and facts is AUTHORITATIVE_FACTS:
        result = replace(result, **{k: wording[k] for k in ("reason", "provenance") if k in wording})
    return result


def _resolve_application_question(
    question_text: str,
    *,
    candidate: CandidateProfile | None = None,
    job: Job | None = None,
    stored_answers: dict[str, str] | None = None,
    facts: AuthoritativeWorkAuthFacts = AUTHORITATIVE_FACTS,
    answers: dict[str, str] | None = None,
) -> QuestionResolutionResult:
    """Resolve an ATS application question or form field label using authoritative facts.

    Enforces:
    - Right to work / permitted to work / sponsorship / permanent status -> from `facts`
      (permanent status is never mapped from ordinary RTW)
    - Driving licence, direct criminal conviction, clearance held, disability, adjustments,
      contact and education fields -> the candidate's literal answer from `answers`
      (default: CANDIDATE_ANSWERS); a missing answer fails closed to human input
    - Complex legal/cautions/DBS questions -> requires_human_input=True
    - Clearance eligibility -> evaluated from JD / human review
    - Demographic questions without configured answer -> "Prefer not to say"
    - Gaokao to A-level conversion -> requires_human_input=True
    - Unknown dates (UCL completion/award, move-in) -> requires_human_input=True
    - Integrity invariant: Never transform authoritative YES to sponsorship into NO merely because employer states no sponsorship
    - Efficiency rule: Stored answers resolved without re-prompting
    """
    clean_text = " ".join(question_text.split()).strip()
    clean_casefold = clean_text.casefold()
    answers = CANDIDATE_ANSWERS if answers is None else answers

    def known(field_id, *, category, reason, rule, provenance=None):
        """The candidate's literal answer for `field_id`, or fail closed when it is not known."""
        value = answers.get(field_id)
        if value is None:
            return _fact_unknown(clean_text, field_id, category)
        return QuestionResolutionResult(
            question_text=clean_text, resolved=True, answer_value=value, field_category=category,
            requires_human_input=False, reason=reason,
            provenance=provenance or f"Authoritative facts: {field_id}", rule_applied=rule,
        )

    # 1. Efficiency rule: Check stored answers first
    if stored_answers:
        if clean_text in stored_answers:
            return QuestionResolutionResult(
                question_text=clean_text,
                resolved=True,
                answer_value=stored_answers[clean_text],
                field_category="stored_answer",
                requires_human_input=False,
                reason="Resolved from previously stored authoritative answer.",
                provenance="stored_answers",
                rule_applied="efficiency_stored_answer_reuse",
                from_cache=True,
            )
        if clean_casefold in stored_answers:
            return QuestionResolutionResult(
                question_text=clean_text,
                resolved=True,
                answer_value=stored_answers[clean_casefold],
                field_category="stored_answer",
                requires_human_input=False,
                reason="Resolved from previously stored authoritative answer (case-insensitive).",
                provenance="stored_answers",
                rule_applied="efficiency_stored_answer_reuse",
                from_cache=True,
            )

    # 2. Permanent / Unrestricted right-to-work check
    # CRITICAL: "Do you have permanent/unrestricted right to work in the UK? -> do not map from ordinary right-to-work"
    is_permanent_query = any(pattern.search(clean_text) for pattern in _PERMANENT_OR_UNRESTRICTED_RTW_PATTERNS)
    if is_permanent_query:
        if facts.has_permanent_or_unrestricted_right_to_work is not False:
            return _fact_unknown(clean_text, "work_auth.permanent_or_unrestricted_right_to_work_answer",
                                 "permanent_right_to_work")
        return QuestionResolutionResult(
            question_text=clean_text,
            resolved=True,
            answer_value="No",
            field_category="permanent_right_to_work",
            requires_human_input=False,
            reason=(
                f"Candidate holds {facts.current_uk_visa_status} (valid until {facts.uk_visa_expiry}), not permanent or "
                "unrestricted status; ordinary right-to-work must not be mapped to permanent status."
            ),
            provenance=f"Authoritative facts: {facts.current_uk_visa_status} (valid until {facts.uk_visa_expiry}); no permanent/unrestricted status",
            rule_applied="permanent_status_do_not_map_from_ordinary_rtw",
        )

    # 3. Ordinary Right to Work in the UK
    # "Do you currently have the legal right to work in the UK?" -> YES
    # "Are you currently permitted to work in the UK?" -> YES (where wording does not require unrestricted/permanent status)
    is_ordinary_rtw = any(pattern.search(clean_text) for pattern in _ORDINARY_RTW_PATTERNS)
    if is_ordinary_rtw:
        if facts.has_current_legal_right_to_work_uk is not True:
            return _fact_unknown(clean_text, "work_auth.current_legal_right_to_work_uk_answer", "right_to_work")
        return QuestionResolutionResult(
            question_text=clean_text,
            resolved=True,
            answer_value="Yes",
            field_category="right_to_work",
            requires_human_input=False,
            reason=(
                f"Candidate currently holds {facts.current_uk_visa_status} (valid until {facts.uk_visa_expiry}) "
                "with legal right to work in the UK, subject to its conditions."
            ),
            provenance=f"Authoritative facts: Status '{facts.current_uk_visa_status}' valid until {facts.uk_visa_expiry}; legal right to work in UK",
            rule_applied="current_legal_right_to_work_uk_yes",
        )

    # 4. Sponsorship Requirements (Now or Future / Skilled Worker)
    # "Will you now or in the future require sponsorship?" -> YES
    # "Will you require Skilled Worker sponsorship in the future?" -> YES
    # INTEGRITY RULE: "Never transform an authoritative YES-to-sponsorship fact into NO merely because the employer says sponsorship is unavailable."
    is_sponsorship_query = any(pattern.search(clean_text) for pattern in _SPONSORSHIP_REQUIRED_PATTERNS)
    if is_sponsorship_query:
        if facts.may_require_skilled_worker_sponsorship_future is not True:
            return _fact_unknown(clean_text, "sponsorship.now_or_future_sponsorship_required_answer", "visa_sponsorship")
        return QuestionResolutionResult(
            question_text=clean_text,
            resolved=True,
            answer_value="Yes",
            field_category="visa_sponsorship",
            requires_human_input=False,
            reason=(
                "Candidate may require Skilled Worker sponsorship for long-term employment. "
                "Authoritative YES-to-sponsorship fact is never transformed into NO, even if the employer states sponsorship is unavailable."
            ),
            provenance="Authoritative facts: Candidate may require Skilled Worker sponsorship for long-term employment",
            rule_applied="require_sponsorship_future_yes_truthful",
        )

    # 5. Driving Licence
    # "Do you hold a full UK driving licence?" -> NO
    is_full_uk_driving = any(pattern.search(clean_text) for pattern in _FULL_UK_DRIVING_LICENCE_PATTERNS)
    if is_full_uk_driving:
        return known("driving.full_uk_driving_licence_answer", category="driving",
                     reason="Resolved full UK driving licence from the Authoritative Answer Bank.",
                     rule="driving_licence_full_uk_no",
                     provenance="Authoritative facts: driving.full_uk_driving_licence_answer, full_uk_driving_licence")
    is_any_driving = any(pattern.search(clean_text) for pattern in _ANY_DRIVING_LICENCE_PATTERNS)
    if is_any_driving:
        return known("driving.driving_licence_answer", category="driving",
                     reason="Resolved driving licence held from the Authoritative Answer Bank.",
                     rule="driving_licence_held_yes",
                     provenance="Authoritative facts: driving_licence, driving_licence_country")

    # 6. Criminal Record: Complex legal questions vs Simple Convictions
    is_complex_criminal = any(pattern.search(clean_text) for pattern in _COMPLEX_CRIMINAL_LEGAL_PATTERNS)
    if is_complex_criminal:
        return QuestionResolutionResult(
            question_text=clean_text,
            resolved=False,
            answer_value=None,
            field_category="criminal_complex_legal",
            requires_human_input=True,
            reason=(
                "Question involves materially different legal definitions (spent/unspent convictions, cautions, "
                "pending proceedings, or DBS checks); must not blindly reuse simple convictions=NO. Requires human review."
            ),
            provenance="prohibited_blind_reuse",
            rule_applied="criminal_complex_legal_human_review",
        )
    is_simple_conviction = any(pattern.search(clean_text) for pattern in _SIMPLE_CRIMINAL_CONVICTIONS_PATTERNS)
    if is_simple_conviction:
        return known("criminal.direct_convictions_answer", category="criminal_record",
                     reason="Resolved direct criminal-convictions answer from the Authoritative Answer Bank.",
                     rule="criminal_convictions_direct_no", provenance="Authoritative facts: criminal_convictions")

    # 7. Security Clearance: Held vs Eligibility
    is_hold_clearance = any(pattern.search(clean_text) for pattern in _HOLD_SECURITY_CLEARANCE_PATTERNS)
    if is_hold_clearance:
        return known("security.current_clearance_answer", category="security_clearance_held",
                     reason="Resolved current UK security clearance from the Authoritative Answer Bank.",
                     rule="security_clearance_held_no", provenance="Authoritative facts: current_uk_security_clearance")
    is_clearance_eligibility = any(pattern.search(clean_text) for pattern in _CLEARANCE_ELIGIBILITY_PATTERNS)
    if is_clearance_eligibility:
        return QuestionResolutionResult(
            question_text=clean_text,
            resolved=False,
            answer_value=None,
            field_category="security_clearance_eligibility",
            requires_human_input=True,
            reason="Security-clearance eligibility cannot be inferred solely from nationality; requires specific JD evaluation and human review.",
            provenance="policy:security_clearance_eligibility",
            rule_applied="security_clearance_eligibility_evaluate_jd",
        )

    # 8. Disability & Reasonable Adjustments
    is_disability = any(pattern.search(clean_text) for pattern in _DISABILITY_PATTERNS)
    if is_disability:
        return known("disability.disabled_answer", category="disability",
                     reason="Resolved the candidate's self-reported disability answer. Broader medical facts are never inferred.",
                     rule="disability_direct_no", provenance="Authoritative facts: disabled")
    is_adjustments = any(pattern.search(clean_text) for pattern in _ADJUSTMENTS_PATTERNS)
    if is_adjustments:
        return known("disability.adjustments_answer", category="adjustments",
                     reason="Resolved the reasonable-adjustments answer from the Authoritative Answer Bank.",
                     rule="reasonable_adjustments_no", provenance="Authoritative facts: reasonable_adjustments_required")

    # 9. Diversity / Equal Opportunities
    is_diversity = any(pattern.search(clean_text) for pattern in _DIVERSITY_PATTERNS)
    if is_diversity:
        return QuestionResolutionResult(
            question_text=clean_text,
            resolved=True,
            answer_value="Prefer not to say",
            field_category="diversity_equality",
            requires_human_input=False,
            reason="Demographic fields without an authoritative configured answer default to 'Prefer not to say'. Never infer demographic answers.",
            provenance="policy:diversity_default_policy",
            rule_applied="diversity_prefer_not_to_say",
        )

    # 10. Secondary Education / A-levels (Gaokao equivalence)
    is_a_level_query = any(pattern.search(clean_text) for pattern in _A_LEVEL_EQUIVALENCE_PATTERNS)
    if is_a_level_query:
        return QuestionResolutionResult(
            question_text=clean_text,
            resolved=False,
            answer_value=None,
            field_category="secondary_education",
            requires_human_input=True,
            reason="A national exam score is never converted to an A-level grade automatically. Requires human input.",
            provenance="Authoritative facts: gaokao (never auto-converted)",
            rule_applied="gaokao_never_auto_convert_to_a_level",
        )

    # 11. Unknown dates: UCL Completion / Formal Award Date
    is_ucl_completion = any(pattern.search(clean_text) for pattern in _UCL_COMPLETION_PATTERNS)
    if is_ucl_completion:
        return QuestionResolutionResult(
            question_text=clean_text,
            resolved=False,
            answer_value=None,
            field_category="education_ucl",
            requires_human_input=True,
            reason="UCL expected completion date and formal award date are UNKNOWN. Never invent them.",
            provenance="Authoritative facts: expected_completion_date=None, formal_award_date=None",
            rule_applied="ucl_completion_date_unknown_fail_closed",
        )

    # 12. Unknown dates: Address move-in date
    is_move_in_date = any(pattern.search(clean_text) for pattern in _MOVE_IN_DATE_PATTERNS)
    if is_move_in_date:
        return QuestionResolutionResult(
            question_text=clean_text,
            resolved=False,
            answer_value=None,
            field_category="address",
            requires_human_input=True,
            reason="Exact move-in date is UNKNOWN. Never invent it.",
            provenance="Authoritative facts: move_in_date=None",
            rule_applied="address_move_in_date_unknown_fail_closed",
        )

    # 13. Address fields
    is_address_query = any(pattern.search(clean_text) for pattern in _ADDRESS_PATTERNS)
    if is_address_query:
        return known("address.full_address", category="address",
                     reason="Resolved canonical current address from Authoritative Answer Bank.",
                     rule="canonical_address_resolution")

    is_postcode_query = any(pattern.search(clean_text) for pattern in _POSTCODE_PATTERNS)
    if is_postcode_query:
        return known("address.postcode", category="address",
                     reason="Resolved canonical postcode from Authoritative Answer Bank.",
                     rule="canonical_postcode_resolution")

    # 14. Preferences: Notice period
    is_notice_query = any(pattern.search(clean_text) for pattern in _NOTICE_PERIOD_PATTERNS)
    if is_notice_query:
        return known("preferences.notice_period_short", category="preferences",
                     reason="Resolved canonical notice period from Authoritative Answer Bank.",
                     rule="canonical_notice_period_resolution",
                     provenance="Authoritative facts: preferences.notice_period")

    # 15. Nationality / Citizenship
    is_nationality_query = any(pattern.search(clean_text) for pattern in _NATIONALITY_PATTERNS)
    if is_nationality_query:
        return known("identity.nationality", category="identity",
                     reason="Resolved canonical nationality from Authoritative Answer Bank.",
                     rule="canonical_nationality_resolution")

    # 16. Identity: Names, Email, Phone
    if any(p.search(clean_text) for p in _FIRST_NAME_PATTERNS):
        return known("identity.legal_first_name", category="identity",
                     reason="Resolved canonical legal first name from Authoritative Answer Bank.", rule="canonical_first_name_resolution")
    if any(p.search(clean_text) for p in _LAST_NAME_PATTERNS):
        return known("identity.legal_last_name", category="identity",
                     reason="Resolved canonical legal last name from Authoritative Answer Bank.", rule="canonical_last_name_resolution")
    if any(p.search(clean_text) for p in _FULL_NAME_PATTERNS):
        return known("identity.legal_full_name", category="identity",
                     reason="Resolved canonical legal full name from Authoritative Answer Bank.", rule="canonical_full_name_resolution")
    if any(p.search(clean_text) for p in _EMAIL_PATTERNS):
        return known("identity.email", category="identity",
                     reason="Resolved canonical email from Authoritative Answer Bank.", rule="canonical_email_resolution")
    if any(p.search(clean_text) for p in _PHONE_PATTERNS):
        return known("identity.phone_uk", category="identity",
                     reason="Resolved canonical UK phone from Authoritative Answer Bank.", rule="canonical_phone_resolution")

    # 17. Education
    if any(p.search(clean_text) for p in _UG_INST_PATTERNS):
        return known("education.huddersfield.institution", category="education",
                     reason="Resolved canonical undergraduate institution from Authoritative Answer Bank.", rule="canonical_education_resolution")
    if any(p.search(clean_text) for p in _UG_DEG_PATTERNS):
        return known("education.huddersfield.degree", category="education",
                     reason="Resolved canonical undergraduate degree from Authoritative Answer Bank.", rule="canonical_education_resolution")
    if any(p.search(clean_text) for p in _UG_CLASS_PATTERNS):
        return known("education.huddersfield.classification", category="education",
                     reason="Resolved canonical undergraduate classification from Authoritative Answer Bank.", rule="canonical_education_resolution")
    if any(p.search(clean_text) for p in _PG_INST_PATTERNS):
        return known("education.ucl.institution", category="education",
                     reason="Resolved canonical postgraduate institution from Authoritative Answer Bank.", rule="canonical_education_resolution")
    if any(p.search(clean_text) for p in _PG_DEG_PATTERNS):
        return known("education.ucl.degree", category="education",
                     reason="Resolved canonical postgraduate degree from Authoritative Answer Bank.", rule="canonical_education_resolution")

    # 18. Standard Profile Contact Fields
    if candidate is not None:
        for field_name, aliases in _CONTACT_FIELD_MAP.items():
            if clean_casefold in aliases:
                val = getattr(candidate, field_name, None)
                if val:
                    return QuestionResolutionResult(
                        question_text=clean_text,
                        resolved=True,
                        answer_value=str(val),
                        field_category="candidate_contact",
                        requires_human_input=False,
                        reason=f"Resolved from candidate profile contact field '{field_name}'.",
                        provenance=f"CandidateProfile.{field_name}",
                        rule_applied="profile_contact_resolution",
                    )

    # 14. Unresolved / Unknown Question
    # Only an unresolved required application field may block autonomous final submission.
    return QuestionResolutionResult(
        question_text=clean_text,
        resolved=False,
        answer_value=None,
        field_category="unknown_or_unresolved",
        requires_human_input=True,
        reason="Unresolved application question; requires human input or explicit reviewed answer.",
        provenance="unmatched_prompt",
        rule_applied="unresolved_field_fail_closed",
    )


def _fact_unknown(clean_text: str, field_id: str, category: str) -> QuestionResolutionResult:
    """A candidate-specific rule matched but the candidate's fact is not known: fail closed."""
    return QuestionResolutionResult(
        question_text=clean_text,
        resolved=False,
        answer_value=None,
        field_category=category,
        requires_human_input=True,
        reason=f"No authoritative candidate fact for {field_id}; requires human input. Never invented.",
        provenance=f"missing_fact:{field_id}",
        rule_applied="candidate_fact_unknown_fail_closed",
    )


def resolve_application_questions(
    questions: list[str],
    *,
    candidate: CandidateProfile | None = None,
    job: Job | None = None,
    stored_answers: dict[str, str] | None = None,
    facts: AuthoritativeWorkAuthFacts = AUTHORITATIVE_FACTS,
    answers: dict[str, str] | None = None,
) -> tuple[dict[str, QuestionResolutionResult], list[str]]:
    """Resolve a batch of form questions.

    Returns:
    - resolutions: dict mapping question text to QuestionResolutionResult
    - unresolved_questions: list of question texts that could not be resolved
    """
    resolutions: dict[str, QuestionResolutionResult] = {}
    unresolved: list[str] = []

    for q in questions:
        res = resolve_application_question(q, candidate=candidate, job=job, stored_answers=stored_answers,
                                           facts=facts, answers=answers)
        resolutions[q] = res
        if not res.resolved or res.requires_human_input:
            unresolved.append(q)

    return resolutions, unresolved
