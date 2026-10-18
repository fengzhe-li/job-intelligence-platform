"""Employer application-frequency / reapplication restrictions, classified from EXACT wording.

Five kinds, never inferred from each other:

* ``exact_role``          -- applies only to the same role / job posting ("you have already applied
                             for this position", "you can reapply to this role in 90 days").
* ``company_frequency``   -- a company-wide limit ("one application every 6 months", "if you have
                             applied to any role at <company> in the last 90 days").
* ``simultaneous_active`` -- a limit on concurrent applications ("one role at a time",
                             "a maximum of 3 active applications").
* ``advisory``            -- guidance only ("apply to the roles that best match your skills").
* ``none``                -- no restriction wording.

A previous application to the same company is NEVER by itself a reason to reject another role. An
exact-role restriction binds only that job id; it is never widened into a company-wide one. Ambiguous
wording that names neither "this role" nor "any role" is reported as ``ambiguous`` for a human,
never silently classified. Every finding keeps its exact wording and its source.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re

KINDS = ("exact_role", "company_frequency", "simultaneous_active", "advisory", "none")

_PERIOD = r"(?:\d+|one|two|three|six|twelve|ninety|thirty|sixty)\s*(?:days?|weeks?|months?|years?)"
# A recruiting window named without a number: "per year", "each recruitment cycle", "this hiring season".
_WINDOW = (r"(?:(?:per|each|every|a|this|the same|any one|one)\s+(?:calendar\s+|academic\s+)?"
           r"(?:year|recruit(?:ing|ment)\s+(?:cycle|season|year)|hiring\s+(?:cycle|season)|application\s+(?:cycle|window|period)|cycle|season|intake))")
_THIS_ROLE = r"(?:this|the same|that)\s+(?:role|position|job|opening|vacancy|posting|requisition)"
_ANY_ROLE = r"(?:any|another|other|a different|multiple|more than one)\s+(?:role|position|job|opening|vacanc(?:y|ies)|posting)s?"

_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("simultaneous_active", re.compile(
        rf"\b(?:one|1|a single)\s+(?:role|position|application|job)\s+at\s+a\s+time\b"
        rf"|\b(?:one|1|a single)\s+application\s+per\s+(?:candidate|applicant|person)\s+at\s+a\s+time\b"
        rf"|\bmaximum\s+of\s+(?:\d+|one|two|three)\s+(?:active\s+|open\s+)?applications?\b"
        rf"|\bonly\s+(?:be\s+considered|apply)\s+for\s+one\s+(?:role|position)\s+at\s+a\s+time\b", re.I)),
    ("company_frequency", re.compile(
        rf"\b(?:one|1|a single)\s+application\s+(?:every|per|each|in\s+any)\s+{_PERIOD}"
        rf"|\b(?:one|1|a single)\s+(?:application|role|position)\s+{_WINDOW}"
        rf"|\b(?:only|just)\s+(?:apply|submit\s+an\s+application)\s+(?:to|for)\s+(?:one|1|a single)\s+(?:role|position|job|opportunit(?:y|ies)|programme|program)\b(?![^.]*\bat\s+a\s+time\b)"
        rf"|\b(?:may|can|are\s+able\s+to|will\s+only\s+be\s+able\s+to)\s+only\s+(?:submit|make|have)\s+(?:one|1|a single)\s+application\b(?![^.]*\bat\s+a\s+time\b)"
        rf"|\b(?:may|can)\s+(?:submit|make)\s+only\s+(?:one|1|a single)\s+application\b(?![^.]*\bat\s+a\s+time\b)"
        rf"|\bapplications?\s+(?:are|is)\s+limited\s+to\s+(?:one|1|a single)\b"
        rf"|\bapply\s+(?:to\s+us\s+|to\s+\w+\s+)?(?:only\s+)?once\s+(?:every|per|in)\s+{_PERIOD}"
        rf"|\bapplied\s+(?:to|for)\s+{_ANY_ROLE}\b[^.]*?\b(?:last|past|previous|within)\s+{_PERIOD}"
        rf"|\b(?:within|in)\s+the\s+(?:last|past)\s+{_PERIOD}[^.]*?\b{_ANY_ROLE}\b", re.I)),
    ("exact_role", re.compile(
        rf"\balready\s+applied\s+(?:to|for)\s+{_THIS_ROLE}\b"
        rf"|\b(?:re-?apply|apply\s+again)\s+(?:to|for)\s+{_THIS_ROLE}\b"
        rf"|\b{_THIS_ROLE}\b[^.]*?\b(?:re-?apply|apply\s+again)\b"
        rf"|\balready\s+(?:submitted|have)\s+an\s+application\s+(?:to|for)\s+{_THIS_ROLE}\b", re.I)),
    ("advisory", re.compile(
        r"\b(?:apply|applying)\s+(?:only\s+)?(?:to|for)\s+(?:the\s+)?(?:roles?|positions?)\s+(?:that\s+)?(?:best|most\s+closely)\b"
        r"|\bwe\s+(?:recommend|encourage)\s+(?:you\s+)?(?:to\s+)?(?:apply\s+to|focus\s+on)\s+(?:one|a\s+few|the\s+roles?)\b", re.I)),
)
# Reapplication / waiting-period wording that names neither this role nor any role: a human decides.
_UNSCOPED = re.compile(rf"\b(?:re-?apply|apply\s+again|already\s+applied)\b[^.]*?\b{_PERIOD}|\b{_PERIOD}\b[^.]*?\b(?:re-?apply|apply\s+again)\b", re.I)


# The user-facing categories (2026-10-14): one per classifier kind.
CATEGORIES = {"exact_role": "EXACT_ROLE", "simultaneous_active": "ONE_ACTIVE_APPLICATION",
              "company_frequency": "COMPANY_WIDE_LIMIT", "ambiguous": "AMBIGUOUS", "advisory": "NONE", "none": "NONE"}
# Categories that require an explicit user decision before approval AND a second confirmation before execution.
DECISION_CATEGORIES = frozenset({"ONE_ACTIVE_APPLICATION", "COMPANY_WIDE_LIMIT", "AMBIGUOUS"})


def category(kind: str) -> str:
    return CATEGORIES.get(kind, "AMBIGUOUS")


def window(wording: str) -> str:
    """The stated time window ("6 months", "per recruitment cycle"), or "" when none is stated."""
    match = re.search(rf"{_PERIOD}|{_WINDOW}", wording or "", re.I)
    return match.group(0) if match else ""


@dataclass(frozen=True)
class Restriction:
    kind: str            # one of KINDS, or "ambiguous" (needs a human)
    wording: str         # the employer's exact sentence
    source: str          # e.g. "job_description", "application_form", "page_warning (human-reported)"
    job_id: str          # the posting the wording was observed on
    company: str
    company_wide: bool   # True only for company_frequency / simultaneous_active

    def to_dict(self) -> dict:
        return asdict(self)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def classify(text: str, *, source: str, job_id: str, company: str) -> list[Restriction]:
    """Every restriction sentence in `text`, each classified on its own exact wording."""
    found: list[Restriction] = []
    for sentence in _sentences(text):
        kind = next((name for name, rule in _RULES if rule.search(sentence)), None)
        if kind is None and _UNSCOPED.search(sentence):
            kind = "ambiguous"
        if kind is not None:
            found.append(Restriction(kind, sentence, source, job_id, company,
                                     company_wide=kind in {"company_frequency", "simultaneous_active"}))
    return found


def applies_to(restriction: Restriction, *, job_id: str, company: str) -> bool:
    """Whether a restriction observed on one posting binds `job_id`: an exact-role one only its own job
    id; a company-wide one every role at that company; advisory / ambiguous ones bind nothing."""
    if restriction.kind == "exact_role":
        return restriction.job_id == job_id
    if restriction.company_wide:
        return restriction.company.casefold() == (company or "").casefold()
    return False


def company_history(applications, company: str) -> list[dict]:
    """The candidate's own applications to `company`, for comparing exact job ids before classifying a
    duplicate warning. A previous application here is information, never a rejection."""
    return [{"application_id": a.id, "job_id": a.job_id, "role": a.role_title, "status": a.status.value,
             "submitted": a.is_submitted, "applied_at": a.applied_at.isoformat() if a.applied_at else None}
            for a in applications if (a.company or "").casefold() == (company or "").casefold()]
