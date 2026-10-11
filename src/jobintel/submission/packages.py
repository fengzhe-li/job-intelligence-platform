"""Reconstruct and fingerprint authoritative inputs on every safety boundary."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from jobintel.analysis.eligibility import evaluate_job_eligibility, determine_submission_policy
from jobintel.analysis.job_enrichment import enrich_job
from jobintel.analysis.jd_requirements import extract_jd_requirements
from jobintel.config import load_ranking_config
from jobintel.matching.cv_generation import resolve_cv_pdf
from jobintel.matching.review_gate import evaluate_review_gate
from jobintel.models.eligibility import EligibilityState, SubmissionPolicy
from jobintel.models.submission import ApprovedSubmissionPackage, fingerprint
from jobintel.models.taxonomy import ReviewGateStatus, APPLICATION_TERMINAL_STATUSES
from jobintel.profile_ingestion import load_candidate_profile
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.submission.answers import AnswerBank


def prepare_package(root, application_id, config_path, supervised_acknowledgement=None):
    root = Path(root).resolve()
    app = next((a for a in ApplicationStore(root).read_applications() if a.id == application_id), None)
    if app is None or app.is_submitted or app.status in APPLICATION_TERMINAL_STATUSES:
        raise ValueError("Application must exist and be unsubmitted and nonterminal")
    job = next((j for j in LocalJobStore(root).read_jobs() if j.id == app.job_id), None)
    profile = load_candidate_profile(root)
    if job is None or profile is None or not app.cv_version_id:
        raise ValueError("Stored job, profile and attached CV are required")
    parts = urlsplit(app.application_url or "")
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("An explicit HTTPS application URL without embedded credentials is required")
    cv_store = CVArtifactStore(root)
    artifacts = [a for a in cv_store.read_all() if a.id == app.cv_version_id]
    if len(artifacts) != 1:
        raise ValueError("CV artifact identity is missing or ambiguous")
    artifact = artifacts[0]
    resolved = resolve_cv_pdf(cv_store, artifact.id)
    if resolved is None or not resolved[0].resolve().is_relative_to(root):
        raise ValueError("CV PDF must exist inside this local store")
    sha = hashlib.sha256(resolved[0].read_bytes()).hexdigest()
    if not artifact.pdf_sha256 or sha != artifact.pdf_sha256:
        raise ValueError("CV PDF hash mismatch; regenerate and review")
    if artifact.job_id != app.job_id or artifact.candidate_id != profile.id or artifact.jd_snapshot != job.description:
        raise ValueError("CV does not match the current job/profile/JD")
    config = load_ranking_config(config_path)
    job = enrich_job(job, profile.graduation_year)
    eligibility = evaluate_job_eligibility(job, profile)
    if eligibility.state == EligibilityState.HARD_INELIGIBLE:
        raise ValueError(f"HARD_INELIGIBLE job cannot be prepared or approved: {eligibility.reason}")
    submission_policy = determine_submission_policy(job)

    jd = extract_jd_requirements(job)
    matched, missing, partial = [], [], []
    for name in jd.must_have_skills:
        cap = profile.capabilities.get(name.casefold())
        if cap is None or cap.evidence_tier == "missing":
            missing.append(name)
        else:
            matched.append(name)
            if cap.evidence_tier == "partial":
                partial.append(name)
    gate = evaluate_review_gate(jd, job, matched, missing, partial,
                               [k for k in ("email", "phone") if not getattr(profile, k, None)],
                               page_count=artifact.page_count)
    blocked = ReviewGateStatus.BLOCK_AUTO_SUBMISSION in {artifact.review_status, gate.status}
    if blocked and supervised_acknowledgement is None:
        pending = sorted(set(list(artifact.review_reasons) + list(gate.reasons)))
        raise ValueError(f"BLOCK_AUTO_SUBMISSION cannot be approved or queued; review reasons: {pending}")
    if blocked:
        status = ReviewGateStatus.BLOCK_AUTO_SUBMISSION
    else:
        status = max((artifact.review_status, gate.status), key=lambda s: s != ReviewGateStatus.AUTO_PREPARE)
    extra_reasons = []
    if eligibility.state in {EligibilityState.NEEDS_ELIGIBILITY_REVIEW, EligibilityState.UNKNOWN}:
        if not blocked:
            status = ReviewGateStatus.REVIEW_REQUIRED
        extra_reasons.append(f"Eligibility review required: {eligibility.reason}")
    reasons = tuple(sorted(set(list(artifact.review_reasons) + list(gate.reasons) + extra_reasons)))
    if supervised_acknowledgement is not None:
        _check_supervised_acknowledgement(supervised_acknowledgement, blocked=blocked, eligibility=eligibility,
                                          submission_policy=submission_policy, application_id=app.id,
                                          job_id=app.job_id, cv_artifact_id=artifact.id, cv_pdf_sha256=sha,
                                          reasons=reasons)
    inputs = {"job": asdict(job), "profile": asdict(profile), "artifact": asdict(artifact),
              "ranking_config": asdict(config), "gate": asdict(gate), "eligibility": asdict(eligibility),
              "submission_policy": asdict(submission_policy), "policy_version": "submission-mvp-1",
              "application_state": app.status.value,
              "gate_code_sha256": hashlib.sha256((Path(__file__).parents[1] / "matching/review_gate.py").read_bytes()).hexdigest()}
    # Local hash only, no candidate database or secret is passed to an executor.
    input_hash = fingerprint(json.loads(json.dumps(inputs, default=str)))
    return ApprovedSubmissionPackage(app.id, app.job_id, app.application_url, artifact.id, sha,
                                     str(root / "submissions/packages" / sha / "cv.pdf"), AnswerBank(root).snapshot(profile, job.id),
                                     input_hash, status.value, reasons, company=job.company, role=job.title,
                                     submission_policy=submission_policy.policy.value,
                                     eligibility_state=eligibility.state.value,
                                     supervised_acknowledgement=supervised_acknowledgement)


def _check_supervised_acknowledgement(ack, *, blocked, eligibility, submission_policy, application_id, job_id,
                                      cv_artifact_id, cv_pdf_sha256, reasons):
    """A supervised exception only lifts the automatic-submission block for one
    explicitly reviewed attempt. It never overrides eligibility or protected
    career-stage review, and it never applies to another application/job/CV."""
    from jobintel.models.submission import SupervisedReviewAcknowledgement
    if not isinstance(ack, SupervisedReviewAcknowledgement):
        raise ValueError("Supervised execution requires a structured SupervisedReviewAcknowledgement")
    if not blocked:
        raise ValueError("Supervised acknowledgement only applies to a BLOCK_AUTO_SUBMISSION package")
    if eligibility.state != EligibilityState.ELIGIBLE:
        raise ValueError(f"Supervised execution requires an ELIGIBLE application (state: {eligibility.state.value})")
    if submission_policy.policy != SubmissionPolicy.AUTO_APPLICATION_ALLOWED:
        raise ValueError(f"Supervised exception is not available under this application policy: {submission_policy.reason}")
    bound = (ack.application_id, ack.job_id, ack.cv_artifact_id, ack.cv_pdf_sha256)
    if bound != (application_id, job_id, cv_artifact_id, cv_pdf_sha256):
        raise ValueError("Supervised acknowledgement is bound to a different application/job/CV")
    if set(ack.acknowledged_blockers) != set(reasons):
        raise ValueError("Supervised acknowledgement must acknowledge exactly the current review reasons")


def seal_pdf(root, package):
    """Create an exclusive, content-addressed read-only copy before approval."""
    import os
    from jobintel.storage.locking import sync_directory
    root = Path(root).resolve()
    target = Path(package.cv_path)
    if not target.resolve().is_relative_to(root / "submissions/packages"):
        raise ValueError("Package PDF escaped the local package directory")
    if not target.exists():
        source = resolve_cv_pdf(CVArtifactStore(root), package.cv_artifact_id)
        if source is None:
            raise ValueError("Source CV PDF unavailable")
        data = source[0].read_bytes()
        if hashlib.sha256(data).hexdigest() != package.cv_pdf_sha256:
            raise ValueError("CV changed before approval")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        target.chmod(0o400)
        sync_directory(target.parent)
    verify_sealed_pdf(root, package)


def verify_sealed_pdf(root, package):
    root = Path(root).resolve()
    target = Path(package.cv_path)
    if (target.is_symlink() or not target.resolve().is_relative_to(root / "submissions/packages")
            or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != package.cv_pdf_sha256):
        raise ValueError("Approved PDF snapshot is missing or modified")
