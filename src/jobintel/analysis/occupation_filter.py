from __future__ import annotations

from jobintel.config import RankingConfig


ADJACENT_ENGINEERING_EXCLUSIONS = {
    "civil engineer",
    "mechanical engineer",
    "chemical engineer",
    "power systems engineer",
    "high voltage",
}


def is_excluded_occupation(title: str, description: str, config: RankingConfig) -> tuple[bool, str | None]:
    title_text = title.casefold()
    text = f"{title}\n{description}".casefold()
    for phrase in config.excluded_occupations:
        if phrase.casefold() in text:
            if phrase.casefold() in ADJACENT_ENGINEERING_EXCLUSIONS and "engineer" in title_text:
                continue
            return True, phrase
    return False, None
