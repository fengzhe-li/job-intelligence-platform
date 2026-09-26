from __future__ import annotations

import re

from jobintel.models.job import EligibilityEvidence, GraduationYearEvidence, SponsorshipEvidence
from jobintel.models.taxonomy import GraduationYearState, SponsorshipState


def detect_sponsorship(description: str) -> SponsorshipEvidence:
    text = description.casefold()
    explicit_no = (
        "cannot sponsor",
        "unable to sponsor",
        "no visa sponsorship",
        "must have the right to work in the uk without sponsorship",
    )
    explicit_yes = (
        "visa sponsorship is available",
        "we sponsor visas",
        "skilled worker visa sponsorship",
        "sponsorship available",
    )
    likely_yes = ("certificate of sponsorship", "skilled worker", "international graduates welcome")
    likely_no = ("right to work in the uk", "existing right to work")

    for phrase in explicit_no:
        if phrase in text:
            return SponsorshipEvidence(SponsorshipState.EXPLICIT_NO_SPONSOR, _evidence(description, phrase), 1.0)
    for phrase in explicit_yes:
        if phrase in text:
            return SponsorshipEvidence(SponsorshipState.EXPLICIT_SPONSOR, _evidence(description, phrase), 1.0)
    for phrase in likely_yes:
        if phrase in text:
            return SponsorshipEvidence(SponsorshipState.LIKELY_SPONSOR, _evidence(description, phrase), 0.7)
    for phrase in likely_no:
        if phrase in text:
            return SponsorshipEvidence(SponsorshipState.LIKELY_NO_SPONSOR, _evidence(description, phrase), 0.65)
    return SponsorshipEvidence(SponsorshipState.UNKNOWN, "", 0.2)


GRADUATION_YEAR_RANGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(20\d{2})\s*(?:-|–|to)\s*(20\d{2})\s+graduates?", re.IGNORECASE),
    re.compile(r"graduat\w*\s+(?:between|from)\s+(20\d{2})\s+(?:and|to|-|–)\s+(20\d{2})", re.IGNORECASE),
    re.compile(r"(20\d{2})\s*(?:-|–|to)\s*(20\d{2})\s+cohort", re.IGNORECASE),
)

STRICT_GRADUATION_YEAR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(20\d{2})\s+graduates?\s+only", re.IGNORECASE),
    re.compile(r"must\s+graduate\s+in\s+(20\d{2})", re.IGNORECASE),
    re.compile(r"graduating\s+in\s+(20\d{2})\s+only", re.IGNORECASE),
    re.compile(r"class\s+of\s+(20\d{2})\s+only", re.IGNORECASE),
    re.compile(r"only\s+(?:accepting|considering)\s+(20\d{2})\s+graduates?", re.IGNORECASE),
)

# Intake/recruitment-cycle phrasing is about when the *role* starts, not when the
# *candidate* graduated -- e.g. "2027 Graduate Programme" or "2027 intake" says
# nothing about a graduation-year requirement. Deliberately excludes bare "in <year>"
# phrasing to avoid false positives.
INTAKE_YEAR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(20\d{2})\s+intake", re.IGNORECASE),
    re.compile(r"intake\s+(?:of\s+)?(20\d{2})", re.IGNORECASE),
    re.compile(r"(20\d{2})\s+(?:graduate\s+)?(?:programme|program|scheme)\b", re.IGNORECASE),
    re.compile(r"(20\d{2})\s+cohort", re.IGNORECASE),
)


def detect_graduation_year(description: str, candidate_graduation_year: int) -> GraduationYearEvidence:
    """Classify a job's graduation-year eligibility text against the candidate.

    Only ONE comparison drives the returned `state` (and therefore ranking weight):
    `candidate_graduation_year` (the candidate's actual degree completion year)
    against the JD's own explicit graduation-year eligibility text -- a strict
    single-year restriction ("2026 graduates only", "class of 2026") or an explicit
    range ("2025-2027 graduates").

    A job's intake/start year (e.g. "2027 Graduate Programme", "2027 intake") is a
    SEPARATE concept -- when the role starts, not a graduation-year requirement on
    the candidate -- and is extracted into `intake_year` for display/context only.
    It deliberately never drives `state`: there is no single "target" intake year to
    filter against, and graduate opportunities from any intake year are relevant if
    the candidate is otherwise eligible (do not conflate "different intake year"
    with "ineligible").

    Permissive by default, to avoid silent false negatives: a job is only classified
    as excluding the candidate when the JD gives explicit, unambiguous evidence of
    that (a strict single-year restriction for a *different* year, or a range that
    excludes the candidate's year). Anything else -- no year mentioned, an intake
    year with no separate graduation-year restriction, a generic "graduate
    programme" phrase -- falls through to `NO_YEAR_STATED` or `GRADUATE_FRIENDLY`,
    both of which are neutral-to-positive and never filter the job out.
    """
    intake_year = _detect_intake_year(description)

    range_match = _first_match(GRADUATION_YEAR_RANGE_PATTERNS, description)
    if range_match:
        match, low, high = range_match[0], int(range_match[1]), int(range_match[2])
        evidence_text = _evidence(description, match.group(0))
        if low <= candidate_graduation_year <= high:
            return GraduationYearEvidence(GraduationYearState.YEAR_2026_ACCEPTED, evidence_text, 0.95, intake_year)
        return GraduationYearEvidence(GraduationYearState.OTHER_YEAR_RESTRICTION, evidence_text, 0.9, intake_year)

    strict_match = _first_match(STRICT_GRADUATION_YEAR_PATTERNS, description)
    if strict_match:
        match, year = strict_match[0], int(strict_match[1])
        evidence_text = _evidence(description, match.group(0))
        if year == candidate_graduation_year:
            return GraduationYearEvidence(GraduationYearState.YEAR_2026_ACCEPTED, evidence_text, 1.0, intake_year)
        return GraduationYearEvidence(GraduationYearState.YEAR_2027_ONLY_STRICT, evidence_text, 1.0, intake_year)

    text = description.casefold()
    if re.search(rf"\b{candidate_graduation_year}\b", text):
        return GraduationYearEvidence(
            GraduationYearState.YEAR_2026_ACCEPTED,
            _evidence(description, str(candidate_graduation_year)),
            0.7,
            intake_year,
        )

    for phrase in ("recent graduate", "graduate programme", "final-year", "final year", "new graduate"):
        if phrase in text:
            return GraduationYearEvidence(GraduationYearState.GRADUATE_FRIENDLY, _evidence(description, phrase), 0.9, intake_year)

    return GraduationYearEvidence(GraduationYearState.NO_YEAR_STATED, "", 0.8, intake_year)


def _detect_intake_year(description: str) -> int | None:
    match = _first_match(INTAKE_YEAR_PATTERNS, description)
    return int(match[1]) if match else None


def _first_match(patterns: tuple[re.Pattern[str], ...], text: str) -> tuple | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return (match, *match.groups())
    return None


def detect_eligibility(description: str) -> list[EligibilityEvidence]:
    findings: list[EligibilityEvidence] = []
    for phrase in ("right to work", "security clearance", "uk driving licence", "degree in computer science"):
        if phrase in description.casefold():
            findings.append(EligibilityEvidence(label=phrase, evidence_text=_evidence(description, phrase), confidence=0.8))
    return findings


def _evidence(description: str, phrase: str) -> str:
    match = description.casefold().find(phrase.casefold())
    if match == -1:
        return ""
    start = max(description.rfind(".", 0, match), description.rfind("\n", 0, match)) + 1
    end_candidates = [position for position in (description.find(".", match), description.find("\n", match)) if position != -1]
    end = min(end_candidates) if end_candidates else len(description)
    return description[start:end].strip()

