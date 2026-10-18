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
from jobintel.matching.cv_review_export import company_display_name
from jobintel.matching.review_gate import evaluate_review_gate
from jobintel.models.eligibility import EligibilityState, SubmissionPolicy
from jobintel.models.submission import ApprovedSubmissionPackage, fingerprint
from jobintel.models.taxonomy import ReviewGateStatus, APPLICATION_TERMINAL_STATUSES
from jobintel.profile_ingestion import load_candidate_profile
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.submission.answers import AnswerBank


class BlockedPackageError(ValueError):
    """BLOCK_AUTO_SUBMISSION without a supervised acknowledgement. `reasons` are the review
    reasons a human would have to acknowledge (the message is unchanged)."""

    def __init__(self, reasons):
        self.reasons = tuple(reasons)
        super().__init__(f"BLOCK_AUTO_SUBMISSION cannot be approved or queued; review reasons: {list(self.reasons)}")


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
    # The same required-skill view as the CV's own gate (an implied, grounded skill such as Git by the
    # candidate's own repositories is matched there too), so the two gates never disagree on it.
    from jobintel.matching import jd_lexicon
    from jobintel.matching.cv_generation import _match_required_skills
    matched, missing, partial = _match_required_skills(profile, jd, jd_lexicon.evidence_view(profile))
    # A user-approved refresh skill listed on this CV is tracked in its learning backlog, not an evidence
    # gap -- the same rule as the CV's own gate, so the two gates never disagree on it.
    on_page_refresh = {str(item.get("skill", "")).casefold() for item in (artifact.learning_backlog or [])}
    missing = [name for name in missing if name.casefold() not in on_page_refresh]
    gate =evaluate_review_gate(jd, job, matched, missing, partial,
                               [k for k in ("email", "phone") if not getattr(profile, k, None)],
                               page_count=artifact.page_count)
    # An artifact stored as BLOCK keeps blocking unless EVERY block-causing reason it recorded is superseded by
    # the live analysis (e.g. a stack-description language list the JD itself says can be learned); it then
    # counts as REVIEW_REQUIRED (still human-reviewed, never AUTO_PREPARE).
    artifact_status = artifact.review_status
    # Fail closed: only when the artifact RECORDED a block-causing reason and none survives the live analysis.
    if artifact_status == ReviewGateStatus.BLOCK_AUTO_SUBMISSION \
            and any(reason.startswith(_BLOCKING_REASONS) for reason in artifact.review_reasons) and not any(
            reason.startswith(_BLOCKING_REASONS)
            for reason in _artifact_reasons(artifact, job, jd, missing, _code_verifier(root, profile))):
        artifact_status = ReviewGateStatus.REVIEW_REQUIRED
    blocked = ReviewGateStatus.BLOCK_AUTO_SUBMISSION in {artifact_status, gate.status}
    if blocked and supervised_acknowledgement is None:
        pending = sorted(set(list(_artifact_reasons(artifact, job, jd, missing, _code_verifier(root, profile))) + list(gate.reasons)))
        raise BlockedPackageError(pending)
    if blocked:
        status = ReviewGateStatus.BLOCK_AUTO_SUBMISSION
    else:
        status = max((artifact_status, gate.status), key=lambda s: s != ReviewGateStatus.AUTO_PREPARE)
    extra_reasons = []
    if eligibility.state in {EligibilityState.NEEDS_ELIGIBILITY_REVIEW, EligibilityState.UNKNOWN}:
        if not blocked:
            status = ReviewGateStatus.REVIEW_REQUIRED
        extra_reasons.append(f"Eligibility review required: {eligibility.reason}")
    reasons = tuple(sorted(set(list(_artifact_reasons(artifact, job, jd, missing, _code_verifier(root, profile))) + list(gate.reasons) + extra_reasons)))
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
                                     supervised_acknowledgement=supervised_acknowledgement,
                                     expected_company_names=expected_company_names(job.company, config_path),
                                     **_cover_letter_hashes(root, artifact.id))


def _cover_letter_hashes(root, cv_artifact_id) -> dict:
    """The stored, validated cover letter for this CV artifact (hashes re-verified), else nothing."""
    from jobintel.matching.cover_letter import load_for_artifact
    record = load_for_artifact(root, cv_artifact_id)
    if not record or record.get("status") != "ready":
        return {}
    return {"cover_letter_pdf_sha256": record["pdf_sha256"], "cover_letter_docx_sha256": record["docx_sha256"]}


def seal_cover_letter(root, package):
    """Read-only copies of the package's cover letter (PDF + DOCX) next to its sealed CV, hash-checked."""
    import os
    if not package.cover_letter_pdf_sha256:
        return []
    from jobintel.matching.cover_letter import load_for_artifact
    record = load_for_artifact(root, package.cv_artifact_id) or {}
    out = []
    for kind in ("pdf", "docx"):
        want = getattr(package, f"cover_letter_{kind}_sha256")
        if record.get(f"{kind}_sha256") != want:
            raise ValueError("Cover letter changed after the package was prepared")
        target = Path(package.cv_path).parent / f"cover_letter.{kind}"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            data = Path(record[f"{kind}_path"]).read_bytes()
            with target.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            target.chmod(0o400)
        if hashlib.sha256(target.read_bytes()).hexdigest() != want:
            raise ValueError("Sealed cover letter is missing or modified")
        out.append(str(target))
    return out


# Required-skill reasons recorded on an (immutable) artifact under an older JD extraction. One is not
# carried into the package only when EVERY skill it names is now explicitly extracted from the same JD
# as preferred/bonus (e.g. a "Bonus:" Kubernetes once read as required). Anything else -- a skill
# still required, or no longer extracted at all -- keeps the reason (fail closed). All other artifact
# reasons (writing/ownership review, README corroboration, ...) are always kept.
_BLOCKING_REASONS = ("No evidence for a majority of must-have requirements:",
                     "Job explicitly restricts eligibility by graduation year",
                     "Generated CV does not fit on one page")
_REQUIRED_SKILL_REASONS = ("No evidence for a majority of must-have requirements:", "No evidence for required skill(s):",
                           "Required skill(s) supported only by partial evidence, not strong:")


_README_ONLY_REASON = "Claim(s) not found in the project's own cached repository README (same-project indexed evidence only) -- verify before use: "


def _code_verifier(root, profile):
    """(project_id, claim) -> True only when the project's OWN repository code record (exact repository
    name, pinned commit, import / dependency-manifest lines; repo_code_evidence) names the claim."""
    import re
    from jobintel.repo_code_evidence import code_evidence_text
    repos = {p.id: getattr(p, "repository_full_name", None) for p in profile.projects}

    def verified(project_id, claim):
        text = code_evidence_text(root, repos.get(project_id)) if project_id in repos else None
        return bool(text and claim and re.search(rf"(?<![\w+#]){re.escape(claim)}(?![\w+#])", text, re.I))
    return verified


def _artifact_reasons(artifact, job, jd=None, live_missing=(), code_verified=None):
    """Also not carried: a stored required-skill reason whose every named skill is a member of a required
    ALTERNATIVES group ("one or more of ...") that the live gate finds satisfied by another member; and a
    stored README-only corroboration reason whose EVERY listed project:claim is now verified by that
    project's own repository code record (any unverified claim keeps the whole reason)."""
    if jd is not None:  # the live JD analysis (incl. explicitly learnable stack skills, analysis.jd_requirements)
        preferred = {name.casefold() for name in jd.preferred_skills}
        required = {name.casefold() for name in jd.must_have_skills}
    else:
        preferred = {item.name.casefold() for item in job.skill_requirements if not item.required}
        required = {item.name.casefold() for item in job.skill_requirements if item.required}
    unsatisfied = {label.casefold() for label in live_missing}
    satisfied_alternatives = set()
    for group in (jd.required_alternatives if jd is not None else []):
        label = ("one of " + " / ".join(group["members"])).casefold()
        if label not in unsatisfied:
            satisfied_alternatives |= {name.casefold() for name in group["members"]}
    kept = []
    for reason in artifact.review_reasons:
        prefix = next((p for p in _REQUIRED_SKILL_REASONS if reason.startswith(p)), None)
        if prefix is not None:
            named = {name.strip().casefold() for name in reason[len(prefix):].split(",") if name.strip()}
            if named and named <= preferred and not named & required:
                continue
            if named and named <= satisfied_alternatives:
                continue
        if code_verified is not None and reason.startswith(_README_ONLY_REASON):
            pairs = [item.strip().split(":", 1) for item in reason[len(_README_ONLY_REASON):].split(",") if item.strip()]
            if pairs and all(len(pair) == 2 and code_verified(pair[0], pair[1].strip()) for pair in pairs):
                continue
        kept.append(reason)
    return kept


def company_aliases(company, aliases_path):
    """Employer display names an exact stored slug is known to appear as on its own pages, from the
    reviewed alias file next to the strategy config ({"elwoodtechnologies": ["Elwood Technologies"]}).
    For employers that are not in the target-company registry. Exact (case-insensitive) slug lookup
    only; a missing or unreadable file adds nothing."""
    try:
        aliases = json.loads(Path(aliases_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    key = " ".join((company or "").split()).casefold()
    for slug, names in aliases.items() if isinstance(aliases, dict) else ():
        if isinstance(slug, str) and slug.strip().casefold() == key and isinstance(names, list):
            return tuple(n for n in names if isinstance(n, str))
    return ()


def expected_company_names(company, config_path):
    """(stored ATS slug, registry display name, reviewed aliases), deduplicated, resolved once here and
    sealed into the package: e.g. ("fanvue.com", "Fanvue"). The registry is the target-company file next
    to the strategy config, the aliases its company_aliases.json; when neither has an entry only the
    stored name is sealed."""
    registry = Path(config_path).with_name("target_companies.json")
    aliases = company_aliases(company, Path(config_path).with_name("company_aliases.json"))
    names = []
    for name in (company, company_display_name(company, registry), *aliases):
        name = " ".join((name or "").split())
        if name and name not in names:
            names.append(name)
    return tuple(names)


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
    seal_cover_letter(root, package)


def verify_sealed_pdf(root, package):
    root = Path(root).resolve()
    target = Path(package.cv_path)
    if (target.is_symlink() or not target.resolve().is_relative_to(root / "submissions/packages")
            or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != package.cv_pdf_sha256):
        raise ValueError("Approved PDF snapshot is missing or modified")
