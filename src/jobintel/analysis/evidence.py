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


def detect_graduation_year(description: str, candidate_year: int = 2026) -> GraduationYearEvidence:
    text = description.casefold()
    strict_2027 = (
        "2027 graduates only",
        "must graduate in 2027",
        "graduating in 2027 only",
        "class of 2027 only",
    )
    for phrase in strict_2027:
        if phrase in text:
            return GraduationYearEvidence(GraduationYearState.YEAR_2027_ONLY_STRICT, _evidence(description, phrase), 1.0)

    if re.search(r"\b2027\b", text):
        return GraduationYearEvidence(GraduationYearState.YEAR_2027_MENTIONED, _evidence(description, "2027"), 0.8)
    if re.search(rf"\b{candidate_year}\b", text):
        return GraduationYearEvidence(
            GraduationYearState.YEAR_2026_ACCEPTED,
            _evidence(description, str(candidate_year)),
            1.0,
        )
    for phrase in ("recent graduate", "graduate programme", "final-year", "final year", "new graduate"):
        if phrase in text:
            return GraduationYearEvidence(GraduationYearState.GRADUATE_FRIENDLY, _evidence(description, phrase), 0.9)
    return GraduationYearEvidence(GraduationYearState.NO_YEAR_STATED, "", 0.8)


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

