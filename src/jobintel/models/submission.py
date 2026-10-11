"""Immutable submission contracts. No executor receives a mutable Application."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed


class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    REVOKED = "revoked"
    EXPIRED = "expired"


class SubmissionStatus(StrEnum):
    REQUESTED = "submission_requested"
    EXECUTING = "executing"
    NEEDS_HUMAN_INPUT = "needs_human_input"
    READY_FOR_HUMAN_REVIEW = "ready_for_human_review"
    PENDING_VERIFICATION = "pending_verification"
    OUTCOME_UNKNOWN = "outcome_unknown"
    FAILED = "failed"
    CANCELLED = "cancelled"
    VERIFIED = "verified"
    SUBMITTED = "submitted"


@dataclass(frozen=True)
class AnswerSnapshot:
    field: str
    value: str
    source: str
    version: str


SUPERVISED_EXECUTION_ONLY = "SUPERVISED_EXECUTION_ONLY"
STANDARD_EXECUTION = "STANDARD"


@dataclass(frozen=True)
class SupervisedReviewAcknowledgement:
    """Explicit, structured human acknowledgement that authorizes ONE supervised
    execution attempt for a package whose CV review status blocks automatic
    submission.

    It does not change the review result, does not assert that any blocker
    (e.g. an unsupported must-have requirement) is satisfied, and never grants
    final-submit permission. It is bound to one application/job/CV/PDF and must
    list exactly the review reasons it acknowledges.
    """

    reviewer: str
    reviewed: bool
    acknowledged_at: str
    reason: str
    acknowledged_blockers: tuple[str, ...]
    application_id: str
    job_id: str
    cv_artifact_id: str
    cv_pdf_sha256: str
    execution_mode: str = SUPERVISED_EXECUTION_ONLY
    final_submit_allowed: bool = False

    def __post_init__(self) -> None:
        if self.reviewed is not True:
            raise ValueError("Supervised acknowledgement requires reviewed=True")
        for name in ("reviewer", "reason", "application_id", "job_id", "cv_artifact_id", "cv_pdf_sha256"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"Supervised acknowledgement requires {name}")
        instant(self.acknowledged_at)
        if not self.acknowledged_blockers or any(not isinstance(b, str) or not b.strip() for b in self.acknowledged_blockers):
            raise ValueError("Supervised acknowledgement must list the review blockers it acknowledges")
        if self.execution_mode != SUPERVISED_EXECUTION_ONLY:
            raise ValueError("Supervised acknowledgement only authorizes SUPERVISED_EXECUTION_ONLY")
        if self.final_submit_allowed is not False:
            raise ValueError("Supervised acknowledgement never grants final-submit permission")

    @classmethod
    def from_dict(cls, value: dict) -> SupervisedReviewAcknowledgement:
        data = dict(value)
        data["acknowledged_blockers"] = tuple(data["acknowledged_blockers"])
        return cls(**data)


@dataclass(frozen=True)
class ApprovedSubmissionPackage:
    application_id: str
    job_id: str
    application_url: str
    cv_artifact_id: str
    cv_pdf_sha256: str
    cv_path: str
    answers: tuple[AnswerSnapshot, ...]
    input_hash: str
    gate_status: str
    gate_reasons: tuple[str, ...]
    schema_version: int = 1
    company: str = ""
    role: str = ""
    submission_policy: str = "require_human_review"
    eligibility_state: str = "eligible"
    # Present only for a supervised exception; gate_status/gate_reasons stay
    # exactly as reviewed (e.g. block_auto_submission) alongside it.
    supervised_acknowledgement: SupervisedReviewAcknowledgement | None = None

    @property
    def execution_mode(self) -> str:
        return SUPERVISED_EXECUTION_ONLY if self.supervised_acknowledgement is not None else STANDARD_EXECUTION

    @property
    def package_hash(self) -> str:
        data = asdict(self)
        if data["supervised_acknowledgement"] is None:
            # Standard packages keep their pre-existing hash identity.
            del data["supervised_acknowledgement"]
        return fingerprint(data)

    @classmethod
    def from_dict(cls, value: dict) -> ApprovedSubmissionPackage:
        data = dict(value)
        data["answers"] = tuple(AnswerSnapshot(**a) for a in data["answers"])
        data["gate_reasons"] = tuple(data["gate_reasons"])
        if data.get("supervised_acknowledgement") is not None:
            data["supervised_acknowledgement"] = SupervisedReviewAcknowledgement.from_dict(data["supervised_acknowledgement"])
        return cls(**data)


@dataclass(frozen=True)
class ReceiptEvidence:
    kind: str
    value: str
    sha256: str = ""
    detail: str = ""  # observed confirmation/portal status, not merely a URL


@dataclass(frozen=True)
class ExecutionResult:
    request_id: str
    package_hash: str
    job_id: str
    cv_artifact_id: str
    application_url: str
    executor: str
    outcome: str  # reported_submitted / needs_human_input / outcome_unknown / failed
    evidence: tuple[ReceiptEvidence, ...] = ()
    unanswered_fields: tuple[str, ...] = ()
    submitted_at: str | None = None

    @classmethod
    def from_dict(cls, value: dict) -> ExecutionResult:
        data = dict(value)
        data["evidence"] = tuple(ReceiptEvidence(**e) for e in data.get("evidence", []))
        data["unanswered_fields"] = tuple(data.get("unanswered_fields", []))
        result = cls(**data)
        if result.outcome not in {"reported_submitted", "needs_human_input", "outcome_unknown", "failed", "ready_for_human_review"}:
            raise ValueError("Invalid execution outcome")
        for name in ("request_id", "package_hash", "job_id", "cv_artifact_id", "application_url", "executor"):
            if not isinstance(getattr(result, name), str) or not getattr(result, name):
                raise ValueError("Invalid execution identity")
        if any(not isinstance(v, str) for v in result.unanswered_fields):
            raise ValueError("Invalid unanswered fields")
        if len(result.evidence) > 20 or any(not all(isinstance(getattr(e, k), str) for k in ("kind", "value", "sha256", "detail")) for e in result.evidence):
            raise ValueError("Malformed receipt evidence")
        if result.submitted_at is not None:
            instant(result.submitted_at)
        return result
