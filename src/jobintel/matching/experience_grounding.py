"""Grounding check for internship CV bullet variants against the long-form master.

A variant may select, shorten, reorder and combine facts from the master units it
cites (`ExperienceEvidence.grounded_in`) -- nothing else. Deterministically:

- every content word of the variant appears (same word stem) in the cited units;
- every technology / tool it names appears in the cited units;
- every number appears verbatim in the cited units (no invented metrics);
- no ownership / seniority / performance term unless the cited units use it;
- no forbidden extrapolation of the cited units.

Connective words (articles, prepositions, "using", "including", ...) are the only
words a variant may add.
"""
from __future__ import annotations

import re
from typing import Iterable

from jobintel.analysis.role_tracks import SKILL_PATTERNS
from jobintel.matching.claim_grounding import _KNOWN_TECH, _OWNERSHIP_TERMS, _PERFORMANCE_TERMS

STEM_CHARS = 5
CONNECTIVES = frozenset({
    "a", "an", "and", "or", "the", "of", "to", "in", "on", "for", "with", "by", "from", "across", "into",
    "as", "at", "its", "their", "including", "using", "through", "while", "within", "between", "both",
    "such", "via", "also",
})
_WORD = re.compile(r"[a-z]+")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?%?")
_EXTRA_TOOLS = ("power bi", "excel", "pandas", "sql")


def _stem(word: str) -> str:
    return word[:STEM_CHARS]


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _mentions(term: str, text: str) -> bool:
    return re.search(rf"(?<![\w+#]){re.escape(term.casefold())}(?![\w+#])", text.casefold()) is not None


def variant_grounding_errors(text: str, master_texts: Iterable[str], forbidden: Iterable[str] = ()) -> list[str]:
    """Reasons `text` is not fully grounded in `master_texts`; empty when it is."""
    master = " ".join(master_texts)
    if not master.strip():
        return ["cites no master unit"]
    errors = []
    master_stems = {_stem(word) for word in _words(master)}
    unsupported = sorted({word for word in _words(text) if word not in CONNECTIVES and _stem(word) not in master_stems})
    if unsupported:
        errors.append(f"words not in the cited master units: {', '.join(unsupported)}")
    terms = {*(_KNOWN_TECH), *(skill.casefold() for skill in SKILL_PATTERNS), *_EXTRA_TOOLS}
    for term in sorted(terms):
        if _mentions(term, text) and not _mentions(term, master):
            errors.append(f"technology not in the cited master units: {term}")
    for number in _NUMBER.findall(text):
        if number not in master:
            errors.append(f"number not in the cited master units: {number}")
    for term in sorted({*_OWNERSHIP_TERMS, *_PERFORMANCE_TERMS}):
        if _mentions(term, text) and not _mentions(term, master):
            errors.append(f"ownership/performance claim not in the cited master units: {term}")
    for term in forbidden:
        if term and _mentions(term, text):
            errors.append(f"forbidden extrapolation: {term}")
    return errors
