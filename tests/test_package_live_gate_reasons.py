"""A package carries the LIVE gate's required-skill reasons, not a stale copy on an immutable artifact.

A "Bonus:" skill is preferred, never a required-skill gap (role_tracks.extract_skill_requirements).
An artifact generated under the older extraction keeps its recorded reasons unchanged, but the
package built from it reports the required-skill gap only if the current gate still finds it.
Writing/ownership and corroboration reasons from the artifact are always kept.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from jobintel.models.application import Application
from jobintel.models.candidate import CandidateProfile
from jobintel.models.job import Job
from jobintel.profile_ingestion import _write_profile
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.submission.packages import BlockedPackageError, prepare_package
from synthetic_answer_bank import write_synthetic_answer_bank
from test_cv_artifact_store import _artifact

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")
BONUS_JD = "Requirements:\n- Expertise in Python.\n- Bonus:\n- Experience with Kubernetes.\nLOCATION\nRemote."
REQUIRED_JD = "Requirements:\n- Expertise in Python.\n- Experience with Kubernetes.\nLOCATION\nRemote."
STALE = "No evidence for required skill(s): Kubernetes"
WRITING = "Impact/ownership rewrite(s) need human review: 1 line(s) (value framing)"


def _store(root, description, reasons=(STALE, WRITING), python=False):
    capabilities = {}
    if python:
        from datetime import date
        from jobintel.models.candidate import Capability, CapabilityEvidence, EvidenceSource
        from jobintel.models.taxonomy import EvidenceSourceType, SkillCategory
        source = EvidenceSource(id="src-p1", source_type=EvidenceSourceType.README_MARKDOWN, title="p1",
                                collected_at=date(2026, 1, 1))
        cap = Capability("Python", SkillCategory.LANGUAGE, [])
        cap.add_evidence(CapabilityEvidence(source, "Built a Python service with tests.", "p1", 1.0, "p1:python"))
        capabilities["python"] = cap
    candidate = CandidateProfile("candidate-1", "Alex Example", 2026, email="alex@example.com", phone="07000000000",
                                 capabilities=capabilities)
    _write_profile(root, candidate)
    write_synthetic_answer_bank(root)
    LocalJobStore(root).write_jobs([Job(id="j1", company="ExampleCo", title="Software Engineer", description=description,
                                        locations=[], source_observations=[])])
    pdf = root / "cv_artifacts/pdfs/cv-0123456789ab.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    artifact = replace(_artifact("cv-0123456789ab", "j1"), jd_snapshot=description, pdf_path=str(pdf),
                       pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(), review_reasons=list(reasons))
    CVArtifactStore(root).save(artifact)
    apps = ApplicationStore(root)
    apps.create_application(Application("app:j1", "j1", "ExampleCo", "Software Engineer",
                                         application_url="https://example.com/jobs/j1"))
    apps.attach_cv("app:j1", artifact.id, artifact.selected_project_ids, artifact.evidence_ids_used)
    return root


def _reasons(root):
    """The package's review reasons (also those of a package that is blocked pending acknowledgement)."""
    try:
        return prepare_package(root, "app:j1", "config/personal_strategy.json").gate_reasons
    except BlockedPackageError as exc:
        return exc.reasons


def test_a_bonus_skill_recorded_as_required_on_an_old_artifact_is_not_carried_into_the_package(tmp_path):
    reasons = _reasons(_store(tmp_path, BONUS_JD))
    assert STALE not in reasons and not any("Kubernetes" in reason for reason in reasons)
    assert WRITING in reasons  # other artifact reasons are kept
    stored = CVArtifactStore(tmp_path).read_all()[0]
    assert STALE in stored.review_reasons  # the historical artifact itself is unchanged


def test_a_genuinely_required_skill_gap_is_still_reported(tmp_path):
    reasons = _reasons(_store(tmp_path, REQUIRED_JD))
    assert any(reason.startswith("No evidence for") and "Kubernetes" in reason for reason in reasons)


def test_a_recorded_gap_for_a_skill_the_jd_no_longer_names_is_kept_fail_closed(tmp_path):
    root = _store(tmp_path, "Requirements:\n- Expertise in Python.\nLOCATION\nRemote.")
    assert STALE in _reasons(root)  # Kubernetes is not now extracted as preferred: never silently dropped



ONE_OF_JD = ("Requirements:\n- Proficiency with one or more programming languages, such as Python, Java, C++, "
             "TypeScript/JavaScript, or similar.\nLOCATION\nRemote.")
STALE_JAVA = "No evidence for required skill(s): Java, C"


def test_a_stale_per_alternative_gap_is_dropped_when_another_member_satisfies_the_group(tmp_path):
    reasons = _reasons(_store(tmp_path, ONE_OF_JD, reasons=("No evidence for required skill(s): Java", WRITING),
                              python=True))
    assert not any("Java" in r for r in reasons) and WRITING in reasons


def test_an_unsatisfied_group_keeps_one_group_reason_and_the_stale_reason_fail_closed(tmp_path):
    reasons = _reasons(_store(tmp_path, ONE_OF_JD, reasons=("No evidence for required skill(s): Java", WRITING)))
    assert any("one of Python / Java / C++ / TypeScript / JavaScript" in r for r in reasons)
    assert "No evidence for required skill(s): Java" in reasons
