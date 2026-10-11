"""Genuinely synthetic Authoritative Answer Bank for submission tests.

Production `AuthoritativeAnswerBank` always seeds itself from the real candidate's
code-default records (`build_authoritative_answer_records`) and a JSON file can
only override records, never remove them; `get_authoritative_answer_bank` also
falls back to the repository's real `data/local` store. The `synthetic_answer_bank`
fixture therefore (1) replaces the code-default seed with nothing and (2) refuses
any load from the real store, so a synthetic bank contains exactly the explicit
records written by `write_synthetic_answer_bank`. Production behaviour is untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobintel.models.answer_bank import AnswerRecord, AnswerType, ResolutionMode
from jobintel.submission import authoritative_answers

SYNTHETIC_PROVENANCE = "synthetic-test-fixture"
SYNTHETIC_VERIFIED_AT = "2026-01-01"
REAL_STORE = (Path(__file__).resolve().parents[1] / "data" / "local").resolve()

SYNTHETIC_REQUIRED_ANSWERS = {
    "identity.legal_first_name": "Alex",
    "identity.legal_last_name": "Example",
    "identity.legal_full_name": "Alex Example",
    "identity.email": "alex@example.com",
    "identity.phone_uk": "07000000000",
    "address.address_line_1": "1 Example Street",
    "address.city": "Exampletown",
    "address.postcode": "EX1 1AA",
    "address.country": "United Kingdom",
    "work_auth.current_legal_right_to_work_uk_answer": "Yes",
    "work_auth.permanent_or_unrestricted_right_to_work_answer": "Yes",
    "sponsorship.now_or_future_sponsorship_required_answer": "No",
}


def _no_code_defaults() -> list[AnswerRecord]:
    return []


def synthetic_records(values=SYNTHETIC_REQUIRED_ANSWERS, human_required=()) -> list[AnswerRecord]:
    """Explicit records only: deterministic synthetic values, plus explicitly
    unanswerable (HUMAN_REQUIRED, no value) records for `human_required` ids."""
    records = [AnswerRecord(field_id=fid, value=value, answer_type=AnswerType.AUTHORITATIVE_FACT,
                            provenance=SYNTHETIC_PROVENANCE, verified_at=SYNTHETIC_VERIFIED_AT)
               for fid, value in values.items()]
    records += [AnswerRecord(field_id=fid, value=None, answer_type=AnswerType.AUTHORITATIVE_FACT,
                             provenance=SYNTHETIC_PROVENANCE, verified_at=SYNTHETIC_VERIFIED_AT,
                             resolution_mode=ResolutionMode.HUMAN_REQUIRED)
                for fid in human_required if fid not in values]
    return records


def isolation_active() -> bool:
    return authoritative_answers.build_authoritative_answer_records is _no_code_defaults


def write_synthetic_answer_bank(root, values=SYNTHETIC_REQUIRED_ANSWERS, human_required=()):
    if not isolation_active():
        raise RuntimeError("write_synthetic_answer_bank requires the synthetic_answer_bank fixture; "
                           "otherwise the real candidate's code-default records are merged in")
    data = {r.field_id: r.to_dict() for r in synthetic_records(values, human_required)}
    path = Path(root) / "profile" / "authoritative_answers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


@pytest.fixture
def synthetic_answer_bank(monkeypatch):
    monkeypatch.setattr(authoritative_answers, "build_authoritative_answer_records", _no_code_defaults)
    original_load = authoritative_answers.AuthoritativeAnswerBank.load_from_file

    def guarded_load(self, source_path=None):
        path = Path(source_path) if source_path else self.root / "profile" / "authoritative_answers.json"
        if path.resolve().is_relative_to(REAL_STORE):
            raise AssertionError("synthetic submission test tried to load the real candidate answer bank")
        return original_load(self, source_path)

    monkeypatch.setattr(authoritative_answers.AuthoritativeAnswerBank, "load_from_file", guarded_load)
    yield
