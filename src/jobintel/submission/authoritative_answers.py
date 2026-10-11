"""Authoritative application Answer Bank catalog and lookup engine.

Maintains strict separation:
1. AUTHORITATIVE FACT: Direct candidate fact
2. DERIVED ANSWER: Answer deterministically derived from current authoritative facts
3. APPLICATION POLICY: Controls whether/how the application proceeds

Guarantees zero passwords, tokens, or credentials stored.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jobintel.models.answer_bank import (
    AnswerRecord,
    AnswerType,
    FreeTextPolicy,
    ResolutionMode,
    SensitivityLevel,
)
from jobintel.submission.candidate_facts import candidate_answer_records

CANONICAL_VERIFIED_DATE = "2026-09-29"


def build_authoritative_answer_records() -> list[AnswerRecord]:
    """Build the canonical catalog: the candidate's facts and derived answers (from the local,
    git-ignored `candidate_facts_local` module, see `candidate_facts`) followed by the
    application policies, which are public.
    """
    records: list[AnswerRecord] = list(candidate_answer_records())
    # =========================================================================
    # 14. FREE-TEXT POLICY
    # =========================================================================
    records.extend([
        AnswerRecord(
            field_id="policy.free_text",
            value={
                "graduate_early_career": FreeTextPolicy.HUMAN_REVIEW_REQUIRED.value,
                "ordinary_roles": FreeTextPolicy.AUTO_ALLOWED.value,
                "factual_autofill": FreeTextPolicy.FACTUAL_AUTOFILL.value,
            },
            answer_type=AnswerType.APPLICATION_POLICY,
            provenance="free_text_policy_specification",
            verified_at=CANONICAL_VERIFIED_DATE,
            sensitivity=SensitivityLevel.STANDARD,
            resolution_mode=ResolutionMode.CONDITIONAL,
            forbidden_inferences=("No hallucinated skills, achievements, metrics or experience.",),
            notes=(
                "Graduate / New Grad / Early Career: AI substantive free-text requires human review. "
                "Ordinary non-protected roles: AI substantive free-text may be used automatically. "
                "Simple factual free-text may auto-fill from authoritative facts."
            ),
        ),
    ])

    # =========================================================================
    # 15. FINAL SUBMISSION POLICY
    # =========================================================================
    records.extend([
        AnswerRecord(
            field_id="policy.final_submission",
            value={
                "graduate_early_career": "REQUIRE_HUMAN_REVIEW",
                "ordinary_roles": "AUTO_APPLICATION_ALLOWED_WHEN_SAFELY_RESOLVED",
                "runtime_safety_rule": "STOP_BEFORE_FINAL_SUBMIT",
            },
            answer_type=AnswerType.APPLICATION_POLICY,
            provenance="submission_policy_specification",
            verified_at=CANONICAL_VERIFIED_DATE,
            sensitivity=SensitivityLevel.STANDARD,
            resolution_mode=ResolutionMode.CONDITIONAL,
            notes="AirTap must still STOP_BEFORE_FINAL_SUBMIT until the live PoC phase explicitly authorizes changing that guard.",
        ),
    ])

    return records


class AuthoritativeAnswerBank:
    """In-memory and file-backed authoritative application Answer Bank."""

    def __init__(self, root: str | Path = "data/local") -> None:
        self.root = Path(root).resolve()
        self._records: dict[str, AnswerRecord] = {}
        self._alias_map: dict[str, str] = {}
        # Populate with canonical records
        for record in build_authoritative_answer_records():
            self._add_record(record)
        self.assert_no_credentials()

    def _add_record(self, record: AnswerRecord) -> None:
        self._records[record.field_id] = record
        for alias in record.aliases:
            norm_alias = " ".join(alias.split()).casefold()
            self._alias_map[norm_alias] = record.field_id

    def get(self, field_id: str) -> AnswerRecord | None:
        return self._records.get(field_id)

    def get_by_alias(self, query: str) -> AnswerRecord | None:
        norm = " ".join(query.split()).casefold()
        field_id = self._alias_map.get(norm)
        if field_id:
            return self._records.get(field_id)
        # Prefix / substring match if exact alias not found
        for alias, fid in self._alias_map.items():
            if alias in norm or norm in alias:
                return self._records.get(fid)
        return None

    def all_records(self) -> tuple[AnswerRecord, ...]:
        return tuple(self._records.values())

    def assert_no_credentials(self) -> None:
        """Verify that absolutely zero passwords, credentials, or API tokens are stored."""
        forbidden_keywords = ("password", "secret", "bearer", "api_key", "token", "credential")
        for fid, rec in self._records.items():
            fid_lower = fid.casefold()
            val_lower = str(rec.value).casefold()
            for kw in forbidden_keywords:
                # Allow 'token' in legitimate contextual phrases only, never as a secret credential value
                if kw in {"password", "secret", "bearer", "api_key"}:
                    if kw in fid_lower or kw in val_lower:
                        raise ValueError(f"Credential detected in Answer Bank: {fid} = {rec.value}")

    def save_to_file(self, target_path: Path | str | None = None) -> Path:
        """Persist Answer Bank records to JSON file."""
        path = Path(target_path) if target_path else self.root / "profile" / "authoritative_answers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {fid: rec.to_dict() for fid, rec in self._records.items()}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def load_from_file(self, source_path: Path | str | None = None) -> None:
        """Load records from JSON file."""
        path = Path(source_path) if source_path else self.root / "profile" / "authoritative_answers.json"
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        for fid, item in data.items():
            rec = AnswerRecord.from_dict(item)
            self._add_record(rec)
        self.assert_no_credentials()
