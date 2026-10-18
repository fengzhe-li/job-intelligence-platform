"""Convenience review copies of CV artifacts (e.g. ~/Downloads): exact bytes, never authoritative."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from jobintel.matching.cv_review_export import company_display_name, export_cv_for_review, review_filename
from jobintel.storage.application_store import ApplicationStore
from test_application_attach_cv_cli import attach, second_cv
from test_submission_service import setup  # noqa: F401  (pytest fixture)

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")


@pytest.fixture
def dl(tmp_path):
    """A fresh export folder, separate from the synthetic store (which lives in tmp_path)."""
    folder = tmp_path / "downloads"
    folder.mkdir()
    return folder


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.mark.parametrize("args,expected", [
    (("Alex Example",), "Alex_Example_CV.pdf"),
    (("Alex Example", "examplecorp", "Junior Software Engineer"), "Alex_Example_Examplecorp_Junior_Software_Engineer_CV.pdf"),
    (("Alex Example", "ExampleLabs", "Forward Deployed Engineer - Software Engineer - United Kingdom"),
     "Alex_Example_ExampleLabs_Forward_Deployed_Engineer_CV.pdf"),        # qualifier after a spaced dash dropped
    (("Alex Example", "Acme/../Corp", "Engineer (C++) – 2027"), "Alex_Example_Acme_Corp_Engineer_C_CV.pdf"),
    (("Alex Example", "Example Co", "Software Engineer II, Backend | Payments"), "Alex_Example_Example_Co_Software_Engineer_II_Backend_CV.pdf"),
    (("Alex Example", "Example Co", "Back-End Engineer"), "Alex_Example_Example_Co_Back_End_Engineer_CV.pdf"),  # unspaced hyphen kept as a word break
    (("Zoë Ü", "Café Ltd.", "Dév"), "Zoe_U_Cafe_Ltd_Dev_CV.pdf"),
    (("Alex Example", "Example Co", "Engineer.pdf"), "Alex_Example_Example_Co_Engineer_CV.pdf"),  # never .pdf.pdf
    (("Alex Example", "", None), "Alex_Example_CV.pdf"),
])
def test_filenames_are_readable_and_safe(args, expected):
    name = review_filename(*args)
    assert name == expected and Path(name).name == name
    assert name.count(".pdf") == 1 and review_filename(*args) == name  # deterministic


def test_long_titles_are_cut_on_a_word_boundary():
    name = review_filename("Alex Example", "Example Co", "Platform " * 20)
    role = name.removeprefix("Alex_Example_Example_Co_").removesuffix("_CV.pdf")
    assert len(role) <= 60 and set(role.split("_")) == {"Platform"}


def test_company_display_name_comes_from_the_registry(tmp_path):
    registry = tmp_path / "companies.json"
    registry.write_text(json.dumps([{"company_name": "ExampleLabs", "connector_token": "examplelabs",
                                     "ashby_board_name": "examplelabs"}]))
    assert company_display_name("examplelabs", registry) == "ExampleLabs"
    assert company_display_name("ExampleLabs", registry) == "ExampleLabs"
    assert company_display_name("unknown-co", registry) == "unknown-co"  # unchanged, then sanitized as usual
    assert company_display_name("examplelabs", tmp_path / "missing.json") == "examplelabs"
    assert review_filename("Alex Example", company_display_name("examplelabs", registry),
                           "Forward Deployed Engineer - Software Engineer - United Kingdom") == (
        "Alex_Example_ExampleLabs_Forward_Deployed_Engineer_CV.pdf")


def test_copy_is_byte_identical_and_verified(setup, dl):
    s = setup
    before = Path(s.artifact.pdf_path).read_bytes()
    out = export_cv_for_review(s.root, s.artifact.id, "Alex_Example_CV.pdf", export_dir=dl)
    assert out.action == "written" and out.matches
    assert Path(out.export_path) == dl / "Alex_Example_CV.pdf"
    assert Path(out.export_path).read_bytes() == before == Path(s.artifact.pdf_path).read_bytes()
    assert out.source_sha256 == out.export_sha256 == s.artifact.pdf_sha256 == sha(out.export_path)
    entry = json.loads((s.root / "cv_exports/exports.jsonl").read_text().splitlines()[-1])
    assert entry["convenience_copy_only"] is True and entry["artifact_id"] == s.artifact.id


def test_repeating_the_same_export_is_a_no_op(setup, dl):
    s = setup
    first = export_cv_for_review(s.root, s.artifact.id, "A_CV.pdf", export_dir=dl)
    again = export_cv_for_review(s.root, s.artifact.id, "A_CV.pdf", export_dir=dl)
    assert (again.action, again.export_path) == ("unchanged", first.export_path)
    assert sorted(p.name for p in dl.iterdir()) == ["A_CV.pdf"]


def test_own_earlier_export_for_the_same_job_is_replaced(setup, dl):
    s = setup
    export_cv_for_review(s.root, s.artifact.id, "A_X_R_CV.pdf", export_dir=dl, application_id=s.app.id)
    newer = second_cv(s)
    out = export_cv_for_review(s.root, newer.id, "A_X_R_CV.pdf", export_dir=dl, application_id=s.app.id)
    assert out.action == "replaced" and sha(dl / "A_X_R_CV.pdf") == newer.pdf_sha256


@pytest.mark.parametrize("foreign", ["user_file", "edited_export", "other_job"])
def test_a_different_file_is_never_overwritten(setup, dl, foreign):
    s = setup
    target = dl / "A_CV.pdf"
    if foreign == "user_file":
        target.write_bytes(b"my own notes")
    elif foreign == "edited_export":
        export_cv_for_review(s.root, s.artifact.id, "A_CV.pdf", export_dir=dl, application_id=s.app.id)
        target.chmod(0o644)
        target.write_bytes(b"annotated by hand")
    else:
        export_cv_for_review(s.root, s.artifact.id, "A_CV.pdf", export_dir=dl, application_id="app:other")
    kept = target.read_bytes()
    newer = second_cv(s)
    out = export_cv_for_review(s.root, newer.id, "A_CV.pdf", export_dir=dl, application_id=s.app.id)
    assert target.read_bytes() == kept
    assert Path(out.export_path).name == f"A_CV_{newer.id}.pdf" and out.matches and out.action == "written"


def test_symlink_target_is_not_followed(setup, dl):
    s = setup
    victim = dl / "victim.txt"
    victim.write_text("keep")
    (dl / "A_CV.pdf").symlink_to(victim)
    out = export_cv_for_review(s.root, s.artifact.id, "A_CV.pdf", export_dir=dl)
    assert victim.read_text() == "keep" and Path(out.export_path).name == f"A_CV_{s.artifact.id}.pdf"


def test_refuses_when_the_fallback_name_is_also_taken(setup, dl):
    s = setup
    (dl / "A_CV.pdf").write_bytes(b"x")
    (dl / f"A_CV_{s.artifact.id}.pdf").write_bytes(b"y")
    with pytest.raises(ValueError, match="already exists with different content"):
        export_cv_for_review(s.root, s.artifact.id, "A_CV.pdf", export_dir=dl)
    assert (dl / "A_CV.pdf").read_bytes() == b"x"


@pytest.mark.parametrize("bad", ["../A_CV.pdf", "sub/A_CV.pdf", "A_CV.txt", ".A_CV.pdf"])
def test_only_plain_pdf_names(setup, dl, bad):
    with pytest.raises(ValueError, match="plain .pdf"):
        export_cv_for_review(setup.root, setup.artifact.id, bad, export_dir=dl)


def test_tampered_or_unknown_artifacts_are_not_exported(setup, dl):
    s = setup
    with pytest.raises(ValueError, match="No registered CV artifact"):
        export_cv_for_review(s.root, "cv-ffffffffffff", "A_CV.pdf", export_dir=dl)
    pdf = Path(s.artifact.pdf_path)
    pdf.chmod(0o644)
    pdf.write_bytes(pdf.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="does not match its recorded SHA-256"):
        export_cv_for_review(s.root, s.artifact.id, "A_CV.pdf", export_dir=dl)
    assert list(dl.iterdir()) == []


def test_attaching_a_cv_exports_a_job_named_copy_without_changing_anything_else(setup, capsys):
    s = setup
    other = second_cv(s)
    out = attach(s.root, s.app.id, other.id, capsys)
    app = ApplicationStore(s.root).for_job(s.job.id)
    assert app.cv_version_id == other.id  # attachment unaffected
    copies = list(Path(os.environ["JOBINTEL_CV_EXPORT_DIR"]).glob("*_CV.pdf"))
    assert len(copies) == 1 and sha(copies[0]) == other.pdf_sha256
    assert "SHA-256 verified" in out and str(copies[0]) in out


def test_export_failure_never_blocks_the_attachment(setup, capsys, monkeypatch, tmp_path):
    s = setup
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("JOBINTEL_CV_EXPORT_DIR", str(blocker))
    other = second_cv(s)
    out = attach(s.root, s.app.id, other.id, capsys)
    assert ApplicationStore(s.root).for_job(s.job.id).cv_version_id == other.id
    assert "Review copy NOT exported" in out
