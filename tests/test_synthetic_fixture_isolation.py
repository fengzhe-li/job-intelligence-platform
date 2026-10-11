"""Synthetic submission fixtures contain ONLY explicit synthetic Answer Bank records.

Assertions compare field ids, provenance markers and counts -- never print the
real candidate's values.
"""
from __future__ import annotations

import pytest

from jobintel.submission import authoritative_answers, candidate_facts
from jobintel.submission.authoritative_answers import AuthoritativeAnswerBank
from jobintel.submission.handoff import get_authoritative_answer_bank
from synthetic_answer_bank import (REAL_STORE, SYNTHETIC_PROVENANCE, SYNTHETIC_REQUIRED_ANSWERS, isolation_active,
                                   write_synthetic_answer_bank)
from test_submission_service import setup  # noqa: F401  (fixture)

needs_real_facts = pytest.mark.skipif(not candidate_facts.local_facts_available(),
                                      reason="no real candidate facts in this checkout (git-ignored local module)")

# Captured at import, before any fixture patches the seed.
REAL_DEFAULT_RECORDS = authoritative_answers.build_authoritative_answer_records


def _real_default_values() -> set[str]:
    values = {r.value.strip() for r in REAL_DEFAULT_RECORDS() if isinstance(r.value, str)}
    generic = {"none", "true", "false", "united kingdom", "skilled worker sponsorship", "prefer not to say"}
    return {v for v in values if len(v) >= 4 and v.casefold() not in generic}


def test_synthetic_bank_holds_exactly_the_explicit_synthetic_records(setup):
    records = get_authoritative_answer_bank(setup.root).all_records()
    assert {r.field_id for r in records} == set(SYNTHETIC_REQUIRED_ANSWERS)
    non_synthetic = sum(r.provenance != SYNTHETIC_PROVENANCE for r in records)
    assert non_synthetic == 0


def test_synthetic_package_inherits_no_code_default_records(setup):
    answers = setup.svc.preview(setup.app.id).answers
    allowed_bank_sources = {f"authoritative_answer_bank:{fid}" for fid in SYNTHETIC_REQUIRED_ANSWERS}
    allowed_bank_sources.add("authoritative_answer_bank:address.full_address")  # derived from synthetic parts only
    allowed_bank_sources.add("authoritative_answer_bank:address.city+address.country")  # location, same parts
    inherited = sorted(a.field for a in answers
                       if a.source.startswith("authoritative_answer_bank:") and a.source not in allowed_bank_sources)
    assert inherited == []  # field names only, never values
    assert {a.source for a in answers} <= allowed_bank_sources | {"trusted_profile", "answer_bank"}


@needs_real_facts
def test_no_real_default_value_reaches_a_synthetic_package(setup):
    real = _real_default_values()
    assert real, "production defaults should exist (this test compares against them in memory only)"
    overlap = sum(1 for a in setup.svc.preview(setup.app.id).answers if a.value.strip() in real)
    assert overlap == 0


def test_writing_a_synthetic_bank_without_isolation_fails_loudly(tmp_path):
    assert not isolation_active()
    with pytest.raises(RuntimeError, match="requires the synthetic_answer_bank fixture"):
        write_synthetic_answer_bank(tmp_path)


@pytest.mark.skipif(not (REAL_STORE / "profile" / "authoritative_answers.json").exists(),
                    reason="no real store file to fall back to")
def test_fallback_to_the_real_store_is_refused(tmp_path, synthetic_answer_bank):
    with pytest.raises(AssertionError, match="tried to load the real candidate answer bank"):
        get_authoritative_answer_bank(tmp_path)  # no profile file here -> production falls back to data/local


@needs_real_facts
def test_production_bank_still_seeds_its_code_defaults():
    assert not isolation_active()
    assert len(AuthoritativeAnswerBank().all_records()) > len(SYNTHETIC_REQUIRED_ANSWERS)
