"""Offline Browserbase bundle contract. NOT an executable backend.

Turns an already-approved, sealed ApprovedSubmissionPackage plus its live
approval/request identity into a deterministic bundle that a future, separate
browser runner could consume. This module has no network, environment, secret,
store, answer-bank or browser access (enforced by an AST test); it never chooses
a CV, generates or looks up an answer, or launches anything. "browserbase" is
deliberately absent from STOP_ONLY_EXECUTORS, so the submission service cannot
enqueue or execute it.

Field vocabulary, required answers and the stop mode come from the shared
stop-only contract (jobintel.submission.stop_only), the same rules AirTap uses.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from jobintel.models.submission import ApprovedSubmissionPackage, canonical, instant, utcnow
from jobintel.submission.stop_only import LABELS, MODE, missing_required_answers

EXECUTOR = "browserbase"
SCHEMA = "jobintel.browserbase_bundle.v1"
REQUEST_FILE = "browserbase_request.json"
CV_FILE = "cv.pdf"
MANIFEST_FILE = "manifest.json"


@dataclass(frozen=True)
class BrowserbaseBundleAuthorization:
    """Identity of the live, human-reviewed approval and request this bundle is for.
    Supplied by the caller from the approval ledger; this module never reads it."""
    approval_id: str
    request_id: str
    package_hash: str
    expires_at: str
    human_reviewed: bool
    status: str


def _check(package: ApprovedSubmissionPackage, a: BrowserbaseBundleAuthorization, now) -> bytes:
    """Fail closed unless the package is approved, bound and sealed; returns the sealed PDF bytes."""
    if (not isinstance(package, ApprovedSubmissionPackage) or not isinstance(a, BrowserbaseBundleAuthorization)
            or a.status != "approved" or a.human_reviewed is not True or not a.approval_id or not a.request_id
            or instant(a.expires_at) <= now):
        raise ValueError("A live, service-validated human approval is required")
    if a.package_hash != package.package_hash:
        raise ValueError("Approved package hash mismatch")
    if package.eligibility_state == "hard_ineligible":
        raise ValueError("HARD_INELIGIBLE package cannot be bundled")
    if not package.company.strip() or not package.role.strip():
        raise ValueError("Approved target is required")
    parts = urlsplit(package.application_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("An explicit HTTPS application URL without embedded credentials is required")
    ack = package.supervised_acknowledgement
    if package.gate_status == "block_auto_submission" and ack is None:
        raise ValueError("Blocked package requires a supervised acknowledgement")
    if ack is not None and (
        (ack.application_id, ack.job_id, ack.cv_artifact_id, ack.cv_pdf_sha256)
        != (package.application_id, package.job_id, package.cv_artifact_id, package.cv_pdf_sha256)
        or ack.final_submit_allowed is not False
    ):
        raise ValueError("Supervised acknowledgement does not match this package")
    if any(v.field not in LABELS for v in package.answers):
        raise ValueError("Non-permitted answer in package")
    if len({v.field for v in package.answers}) != len(package.answers):
        raise ValueError("Duplicate answer field in package")
    missing = missing_required_answers(package)
    if missing:
        raise ValueError(f"Approved package is missing required candidate answers: {missing}")
    source = Path(package.cv_path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Sealed PDF unavailable")
    pdf = source.read_bytes()
    if hashlib.sha256(pdf).hexdigest() != package.cv_pdf_sha256:
        raise ValueError("CV hash mismatch")
    return pdf


def build_request(package: ApprovedSubmissionPackage, a: BrowserbaseBundleAuthorization) -> dict:
    """The runner-facing request. Every candidate value is copied verbatim from the
    sealed package; permitted fields the package does not answer are listed as
    unresolved and carry no value. No timestamps, nonces, credentials or session
    configuration."""
    ack = package.supervised_acknowledgement
    answered = {v.field for v in package.answers}
    return {
        "schema": SCHEMA,
        "executor": EXECUTOR,
        "execution_mode": MODE,
        "final_submit_allowed": False,
        "request_id": a.request_id,
        "approval_id": a.approval_id,
        "approval_expires_at": a.expires_at,
        "package_hash": package.package_hash,
        "input_hash": package.input_hash,
        "application_id": package.application_id,
        "job_id": package.job_id,
        "application_url": package.application_url,
        "company": package.company,
        "expected_company_names": list(package.expected_company_names),
        "role": package.role,
        "cv": {"artifact_id": package.cv_artifact_id, "sha256": package.cv_pdf_sha256, "path": CV_FILE,
               "upload_confirmation_required": True, "remote_integrity_verified": False},
        "answers": [{"field": v.field, "value": v.value, "source": v.source, "version": v.version}
                    for v in sorted(package.answers, key=lambda v: v.field)],
        "unresolved_fields": sorted(set(LABELS) - answered),
        "review": {"gate_status": package.gate_status, "gate_reasons": list(package.gate_reasons),
                   "submission_policy": package.submission_policy,
                   "eligibility_state": package.eligibility_state,
                   "authorization": package.execution_mode},
        "supervised_acknowledgement": None if ack is None else {
            "reviewer": ack.reviewer, "reviewed": ack.reviewed, "acknowledged_at": ack.acknowledged_at,
            "reason": ack.reason, "acknowledged_blockers": list(ack.acknowledged_blockers),
            "execution_mode": ack.execution_mode, "final_submit_allowed": False},
    }


def build_bundle_files(package: ApprovedSubmissionPackage, authorization: BrowserbaseBundleAuthorization,
                       *, clock=utcnow) -> dict[str, bytes]:
    """Deterministic bundle contents: same package + approval/request => same bytes."""
    pdf = _check(package, authorization, clock())
    files = {REQUEST_FILE: (canonical(build_request(package, authorization)) + "\n").encode(), CV_FILE: pdf}
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files[MANIFEST_FILE] = (canonical(manifest) + "\n").encode()
    return files


def write_bundle(bundle_root, package: ApprovedSubmissionPackage, authorization: BrowserbaseBundleAuthorization,
                 *, clock=utcnow) -> Path:
    """Write the bundle read-only into a new directory named from the request id hash.
    Never overwrites or regenerates an existing bundle."""
    files = build_bundle_files(package, authorization, clock=clock)
    root = Path(bundle_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("Bundle directory cannot be a symlink")
    directory = root / hashlib.sha256(authorization.request_id.encode()).hexdigest()
    directory.mkdir(mode=0o700)
    for name, data in files.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    return directory
