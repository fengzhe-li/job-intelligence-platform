"""`application-attach-cv`: attach an EXISTING registered CV artifact, never generate one."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from unittest.mock import patch

import pytest

from jobintel.cli.main import main
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from test_cv_artifact_store import _artifact
from test_submission_service import approve, setup  # noqa: F401  (pytest fixture)
from test_supervised_execution import _ack, _approve, _store


pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")


def attach(root, application_id, cv_id, capsys):
    argv = ["jobintel", "application-attach-cv", "--application-id", application_id, "--cv-artifact-id", cv_id, "--store", str(root)]
    with patch("sys.argv", argv):
        main()
    return capsys.readouterr().out


def second_cv(s, cv_id="cv-0123456789ac"):
    pdf = s.root / f"cv_artifacts/pdfs/{cv_id}.pdf"
    pdf.write_bytes(b"%PDF-1.4 second reviewed CV " + cv_id.encode())
    artifact = replace(_artifact(cv_id, s.job.id), jd_snapshot=s.job.description, pdf_path=str(pdf),
                       pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
                       review_status=s.artifact.review_status, review_reasons=list(s.artifact.review_reasons))
    CVArtifactStore(s.root).save(artifact)
    return artifact


def test_h_attaches_existing_registered_cv_and_preserves_application(setup, capsys):
    s = setup
    other = second_cv(s)
    before = ApplicationStore(s.root).for_job(s.job.id)
    count = len(CVArtifactStore(s.root).read_all())

    out = attach(s.root, s.app.id, other.id, capsys)

    after = ApplicationStore(s.root).for_job(s.job.id)
    assert after.cv_version_id == other.id
    assert (after.selected_project_ids, after.evidence_ids_used) == (other.selected_project_ids, other.evidence_ids_used)
    assert (after.id, after.job_id, after.application_url, after.company, after.role_title, after.status) == \
           (before.id, before.job_id, before.application_url, before.company, before.role_title, before.status)
    assert len(CVArtifactStore(s.root).read_all()) == count  # nothing generated
    assert f"Attached CV {other.id}" in out


def test_i_unknown_application_fails_closed(setup, capsys):
    s = setup
    with pytest.raises(SystemExit, match="No application with id app:missing"):
        attach(s.root, "app:missing", s.artifact.id, capsys)


@pytest.mark.parametrize("cv_id", ["cv-ffffffffffff", "latest", "../cv-0123456789ab", "/tmp/cv-0123456789ab.pdf"])
def test_j_unknown_or_unregistered_cv_fails_closed(setup, capsys, cv_id):
    s = setup
    before = ApplicationStore(s.root).for_job(s.job.id)
    with pytest.raises(SystemExit, match="No registered CV artifact"):
        attach(s.root, s.app.id, cv_id, capsys)
    assert ApplicationStore(s.root).for_job(s.job.id) == before


def test_j_cv_for_another_job_or_with_tampered_pdf_fails_closed(setup, capsys):
    s = setup
    foreign_pdf = s.root / "cv_artifacts/pdfs/cv-0123456789ad.pdf"
    foreign_pdf.write_bytes(b"%PDF-1.4 other job")
    CVArtifactStore(s.root).save(replace(_artifact("cv-0123456789ad", "other-job"), pdf_path=str(foreign_pdf),
                                         pdf_sha256=hashlib.sha256(foreign_pdf.read_bytes()).hexdigest()))
    with pytest.raises(SystemExit, match="generated for job other-job"):
        attach(s.root, s.app.id, "cv-0123456789ad", capsys)

    tampered = second_cv(s)
    (s.root / f"cv_artifacts/pdfs/{tampered.id}.pdf").write_bytes(b"%PDF-1.4 edited after review")
    with pytest.raises(SystemExit, match="does not match its recorded SHA-256"):
        attach(s.root, s.app.id, tampered.id, capsys)
    assert ApplicationStore(s.root).for_job(s.job.id).cv_version_id == s.artifact.id


def test_k_same_cv_reattach_is_idempotent_and_keeps_approval(setup, capsys):
    s = setup
    approval = approve(s)
    before = ApplicationStore(s.root).for_job(s.job.id)

    out = attach(s.root, s.app.id, s.artifact.id, capsys)

    assert "already has CV" in out and "nothing changed" in out
    assert ApplicationStore(s.root).for_job(s.job.id) == before  # not even updated_at moved
    assert s.svc.store.approvals()[approval["id"]]["status"] == "approved"
    assert s.svc.enqueue(approval["id"])["status"] == "submission_requested"


def test_l_different_cv_revokes_stale_approval_and_ack_binding(tmp_path, capsys):
    s = _store(tmp_path)
    ack = _ack(s)
    _, approval = _approve(s, ack)
    other = second_cv(s)

    attach(s.root, s.app.id, other.id, capsys)

    assert s.svc.store.approvals()[approval["id"]]["status"] == "revoked"
    with pytest.raises(ValueError):
        s.svc.enqueue(approval["id"])
    # The old acknowledgement is bound to the old CV id/hash and cannot authorize the new CV.
    with pytest.raises(ValueError, match="bound to a different application/job/CV"):
        s.svc.preview(s.app.id, ack)


def test_l_cannot_swap_cv_while_a_submission_is_active(setup, capsys):
    s = setup
    approval = approve(s)
    s.svc.enqueue(approval["id"])
    other = second_cv(s)
    with pytest.raises(SystemExit, match="active submission"):
        attach(s.root, s.app.id, other.id, capsys)
    assert ApplicationStore(s.root).for_job(s.job.id).cv_version_id == s.artifact.id
