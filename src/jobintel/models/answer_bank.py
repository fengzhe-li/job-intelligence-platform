"""Data models for authoritative application profile and Answer Bank.

Strictly separates:
1. AUTHORITATIVE FACT: Direct candidate fact
2. DERIVED ANSWER: Answer deterministically derived from current authoritative facts
3. APPLICATION POLICY: Controls whether/how the application proceeds

Guarantees zero passwords, tokens, or credentials stored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AnswerType(StrEnum):
    """Classification of Answer Bank entries."""

    AUTHORITATIVE_FACT = "authoritative_fact"
    DERIVED_ANSWER = "derived_answer"
    APPLICATION_POLICY = "application_policy"


class SensitivityLevel(StrEnum):
    """Sensitivity rating of the information."""

    PUBLIC = "public"
    STANDARD = "standard"
    SENSITIVE = "sensitive"
    DEMOGRAPHIC = "demographic"
    RESTRICTED = "restricted"


class ResolutionMode(StrEnum):
    """Whether an answer can be used automatically or requires human review."""

    DETERMINISTIC = "deterministic"
    CONDITIONAL = "conditional"
    HUMAN_REQUIRED = "human_required"


class FreeTextPolicy(StrEnum):
    """Policy for substantive or factual free-text generation/filling."""

    HUMAN_REVIEW_REQUIRED = "human_review_required"
    AUTO_ALLOWED = "auto_allowed"
    FACTUAL_AUTOFILL = "factual_autofill"


@dataclass(frozen=True)
class AnswerRecord:
    """Canonical Answer Bank entry representing a fact, derived answer, or policy."""

    field_id: str
    value: Any
    answer_type: AnswerType
    provenance: str
    verified_at: str
    aliases: tuple[str, ...] = ()
    sensitivity: SensitivityLevel = SensitivityLevel.STANDARD
    resolution_mode: ResolutionMode = ResolutionMode.DETERMINISTIC
    derivation_rule: str | None = None
    forbidden_inferences: tuple[str, ...] = ()
    version: str = "1.0"
    notes: str | None = None

    def __post_init__(self) -> None:
        # Guard against storing passwords, tokens, or authentication secrets
        lower_id = self.field_id.casefold()
        val_str = str(self.value).casefold() if self.value is not None else ""
        forbidden_keywords = ("password", "secret_key", "bearer_token", "api_key", "access_token")
        for kw in forbidden_keywords:
            if kw in lower_id:
                raise ValueError(f"Security violation: Passwords/credentials cannot be stored in Answer Bank: {self.field_id}")
            if kw in val_str:
                raise ValueError(f"Security violation: Passwords/credentials cannot be stored in Answer Bank: value contains {kw}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "value": self.value,
            "answer_type": self.answer_type.value,
            "provenance": self.provenance,
            "verified_at": self.verified_at,
            "aliases": list(self.aliases),
            "sensitivity": self.sensitivity.value,
            "resolution_mode": self.resolution_mode.value,
            "derivation_rule": self.derivation_rule,
            "forbidden_inferences": list(self.forbidden_inferences),
            "version": self.version,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnswerRecord:
        return cls(
            field_id=data["field_id"],
            value=data.get("value"),
            answer_type=AnswerType(data["answer_type"]),
            provenance=data.get("provenance", "unknown"),
            verified_at=data.get("verified_at", "2026-09-29"),
            aliases=tuple(data.get("aliases", ())),
            sensitivity=SensitivityLevel(data.get("sensitivity", SensitivityLevel.STANDARD)),
            resolution_mode=ResolutionMode(data.get("resolution_mode", ResolutionMode.DETERMINISTIC)),
            derivation_rule=data.get("derivation_rule"),
            forbidden_inferences=tuple(data.get("forbidden_inferences", ())),
            version=data.get("version", "1.0"),
            notes=data.get("notes"),
        )
