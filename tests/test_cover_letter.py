import json
import zipfile
from types import SimpleNamespace

from jobintel.matching import cover_letter as cl

JD = (
    "<p>About Acme</p><p>Acme is a payments platform that helps millions of small businesses get paid.</p>"
    "<h3>What You’ll Be Doing</h3><ul><li>Build and operate the APIs that move money between businesses.</li>"
    "<li>Work with product teams on reliable services.</li></ul>"
    "<h3>What you'll need</h3><ul><li>Python and PostgreSQL.</li><li>Interest in Go.</li></ul>"
    "<p>We offer mentoring and a strong code review culture. Apply now; visa sponsorship available.</p>"
)


def _fixture(tmp_path, bullets=None, techs=("Python", "PostgreSQL", "FastAPI")):
    job = SimpleNamespace(id="greenhouse:1", title="Software Engineer, New Grad", company="acme", description=JD,
                          source_observations=[])
    candidate = SimpleNamespace(name="Ada Example", projects=[
        SimpleNamespace(id="p1", description="Ledger service for shared expenses with React dashboard. Validates and reconciles transfers between accounts."),
        SimpleNamespace(id="p2", description="Command-line tool for log analysis.")])
    document = {
        "name": "Ada Example", "contact_line": "ada@example.com | 07000 000000",
        "education": [{"heading": "Example University: MSc Computing", "dates": "2025 – 2026"},
                      {"heading": "Other University: BEng Electronic Engineering (First Class)", "dates": "2020 – 2024"}],
        "experience": [{"heading": "Software Intern, Example Systems Ltd (UK)", "details": []}],
        "skill_groups": [{"name": "Backend", "skills": list(techs) + ["Go"]}],
        "projects": [{"project_id": "p1", "name": "Ledger Service", "technologies": list(techs),
                      "bullets": bullets or ["Implemented idempotent transfer endpoints with PostgreSQL row locking."]},
                     {"project_id": "p2", "name": "Log Tool", "technologies": ["Python"], "bullets": ["Parsed logs."]}],
    }
    artifact = SimpleNamespace(id="cv-test", pdf_sha256="x", document=document, application_id="app:1",
                               writing_quality={"archetype": "backend"},
                               learning_backlog=[{"skill": "Go", "status": "refresh_required"}])
    return candidate, job, artifact


def test_letter_is_grounded_one_page_and_names_role_and_company(tmp_path):
    candidate, job, artifact = _fixture(tmp_path)
    record = cl.generate_for_application(tmp_path, candidate, job, artifact, "Acme", "app:1", check_form=False)
    assert record["status"] == "ready", record.get("problems")
    assert record["page_count"] == 1
    body = "\n".join(record["paragraphs"])
    assert "Acme" in body and "Software Engineer, New Grad" in body
    # the JD's responsibility is paraphrased into the letter's own sentence, not quoted by template
    assert "building and operating the APIs that move money between businesses" in body
    assert "\u201c" not in body
    assert "React" not in body  # the profile description mentions React; it is not on this CV, so it is not claimed
    # a learnable refresh skill stays in Technical Skills / backlog; the letter never volunteers the gap
    assert "Go" not in body.split() and "learning" not in body.casefold()
    assert record["projects"] == ["Ledger Service", "Log Tool"]


def test_outputs_are_a4_half_inch_docx_and_reproducible(tmp_path):
    candidate, job, artifact = _fixture(tmp_path)
    first = cl.generate_for_application(tmp_path, candidate, job, artifact, "Acme", "app:1", check_form=False)
    with zipfile.ZipFile(first["docx_path"]) as zf:
        xml = zf.read("word/document.xml").decode()
    assert 'w:top="720"' in xml and 'w:left="720"' in xml and 'w:w="11906"' in xml
    assert first["pdf_path"].endswith("Ada_Example_Acme_Software_Engineer_New_Grad_Cover_Letter.pdf")
    again = cl.generate_for_application(tmp_path, candidate, job, artifact, "Acme", "app:1", check_form=False)
    assert again["reused"] and again["pdf_sha256"] == first["pdf_sha256"]
    # regenerating from scratch gives identical bytes (no date, fixed DOCX timestamps, invariant PDF)
    (tmp_path / "cover_letters" / "cv-test" / "cover_letter.json").unlink()
    fresh = cl.generate_for_application(tmp_path, candidate, job, artifact, "Acme", "app:1", check_form=False)
    assert (fresh["pdf_sha256"], fresh["docx_sha256"]) == (first["pdf_sha256"], first["docx_sha256"])


def test_validation_rejects_ungrounded_tech_duplicates_wrong_names_and_copied_bullets(tmp_path):
    candidate, job, artifact = _fixture(tmp_path)
    letter = cl.compose(candidate, job, artifact, "Acme", "app:1")
    assert cl.validate(letter, artifact, page_count=1) == []
    letter.paragraphs.append("I built Kubernetes operators in Ledger Service.")
    letter.paragraphs.append(letter.paragraphs[0])
    letter.paragraphs.append("Implemented idempotent transfer endpoints with PostgreSQL row locking.")
    letter.projects.append("Imaginary Project")
    problems = " | ".join(cl.validate(letter, artifact, page_count=2))
    assert "2 pages" in problems and "duplicate" in problems and "Kubernetes" in problems
    assert "Imaginary Project" in problems and "repeats a CV bullet" in problems


def test_stored_letter_is_rejected_after_tampering(tmp_path):
    candidate, job, artifact = _fixture(tmp_path)
    record = cl.generate_for_application(tmp_path, candidate, job, artifact, "Acme", "app:1", check_form=False)
    with open(record["pdf_path"], "ab") as handle:
        handle.write(b"x")
    assert cl.load_for_artifact(tmp_path, "cv-test")["status"] == "invalid"


def test_package_hash_unchanged_without_a_letter():
    from jobintel.models.submission import ApprovedSubmissionPackage
    pkg =ApprovedSubmissionPackage("a", "j", "https://x", "cv", "sha", "/p/cv.pdf", (), "h", "auto_prepare", ())
    with_letter = ApprovedSubmissionPackage("a", "j", "https://x", "cv", "sha", "/p/cv.pdf", (), "h", "auto_prepare", (),
                                            cover_letter_pdf_sha256="1", cover_letter_docx_sha256="2")
    assert "cover_letter_pdf_sha256" not in json.dumps(pkg.package_hash)
    assert pkg.package_hash != with_letter.package_hash
    legacy = ApprovedSubmissionPackage.from_dict({k: v for k, v in json.loads(json.dumps(pkg.__dict__, default=list)).items()
                                                  if not k.startswith("cover_letter")})
    assert legacy.package_hash == pkg.package_hash


def test_company_self_description_is_rephrased_not_quoted():
    assert cl._company_clause("Our teams build and maintain the platforms that support the firm", "MW") == \
        "that your teams build and maintain the platforms that support the firm"
    assert cl._company_clause("Canonical is a leading provider of open source software", "Canonical") == \
        "its work as a leading provider of open source software"
    assert cl._company_clause("As an FCA-regulated firm, Elwood provides investors with trading infrastructure", "Elwood") == \
        "that, as an FCA-regulated firm, it provides investors with trading infrastructure"
    assert cl._as_activity("You\u2019ll collaborate with engineers, solve real problems, and see results") == \
        "collaborating with engineers, solving real problems, and seeing results"


def test_pdf_text_starts_exactly_at_half_inch_margin(tmp_path):
    from pypdf import PdfReader
    candidate, job, artifact = _fixture(tmp_path)
    record = cl.generate_for_application(tmp_path, candidate, job, artifact, "Acme", "app:1", check_form=False)
    xs, ys = [], []

    def visit(text, cm, tm, *_):
        if text.strip():
            xs.append(tm[4] * cm[0] + tm[5] * cm[2] + cm[4])
            ys.append(tm[4] * cm[1] + tm[5] * cm[3] + cm[5])
    PdfReader(record["pdf_path"]).pages[0].extract_text(visitor_text=visit)
    assert abs(min(xs) - 36.0) < 0.5  # 0.5 inch = 36pt, no frame padding
    assert min(ys) >= 36.0 - 3  # nothing below the bottom margin (descenders aside)
