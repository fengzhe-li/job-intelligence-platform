"""Where the real candidate's facts come from.

The candidate's own facts (identity, contact, address, immigration status, education,
employment, preferences and the other self-declared answers) are personal data. They live in
the git-ignored module `jobintel.submission.candidate_facts_local`, next to the git-ignored
local store (`data/local/profile/authoritative_answers.json`, which still overrides records),
and are never published.

The public repository ships without that module. The Answer Bank then contains only the
application policies, and every candidate-specific question resolves to "requires human
input" instead of an answer -- the same fail-closed path as any unknown fact. Tests use
synthetic candidates (tests/synthetic_answer_bank.py) and never this module.
"""
from __future__ import annotations

import importlib
from typing import Any

from jobintel.models.answer_bank import AnswerRecord

LOCAL_MODULE = "jobintel.submission.candidate_facts_local"

try:
    _local = importlib.import_module(LOCAL_MODULE)
except ModuleNotFoundError as error:  # the public repository: no candidate defaults
    if error.name != LOCAL_MODULE:  # a broken local module must fail loudly, not silently vanish
        raise
    _local = None


def local_facts_available() -> bool:
    return _local is not None


def candidate_answer_records() -> list[AnswerRecord]:
    """The candidate's Answer Bank records, or none when the local module is absent."""
    return list(_local.build_candidate_answer_records()) if _local is not None else []


def question_facts() -> dict[str, Any]:
    """Literal values used by the rule-based question resolver, keyed by Answer Bank field id."""
    return dict(getattr(_local, "QUESTION_FACTS", {})) if _local is not None else {}
