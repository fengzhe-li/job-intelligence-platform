from __future__ import annotations

import re

from jobintel.config import RankingConfig


ADJACENT_ENGINEERING_EXCLUSIONS = {
    "civil engineer",
    "mechanical engineer",
    "chemical engineer",
    "power systems engineer",
    "high voltage",
}


# A title naming a technical role: the job IS that role, whatever teams its description mentions
# ("work with the Sales team", "marketing site", "accounting ledger service").
_TECHNICAL_ROLE = re.compile(r"\b(?:engineer(?:ing)?|developer|programmer|scientist|architect|technologist|sre|devops"
                             r"|software|platform|backend|back-end|frontend|front-end|full[- ]?stack)\b")


def _mentions(phrase: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.casefold())}(?![a-z0-9])", text) is not None


def is_excluded_occupation(title: str, description: str, config: RankingConfig) -> tuple[bool, str | None]:
    """The job's OCCUPATION is excluded: an excluded phrase names the title (whole words), or -- only
    when the title names no technical role -- the description. A technical title is never excluded
    because its JD mentions another team or domain."""
    title_text = title.casefold()
    technical_title = _TECHNICAL_ROLE.search(title_text) is not None
    for phrase in config.excluded_occupations:
        in_title = _mentions(phrase, title_text)
        if not in_title and (technical_title or not _mentions(phrase, (description or "").casefold())):
            continue
        if phrase.casefold() in ADJACENT_ENGINEERING_EXCLUSIONS and "engineer" in title_text and not in_title:
            continue
        return True, phrase
    return False, None
