"""ATS / AI CV-reading layer: JD lexical extraction, grounded alignment, refresh skills, coverage, backlog.

Synthetic candidates and JDs throughout (no company or project name in any rule); the final test regenerates a
real public JD fixture against the private local profile when present (skipped otherwise).
"""
from __future__ import annotations

import inspect
import json
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.matching import jd_lexicon as lx
from jobintel.matching.cv_generation import generate_and_save_cv, generate_cv
from jobintel.models.candidate import (CandidateProfile, Capability, CapabilityEvidence, Education, EvidenceSource,
                                       Experience, ExperienceEvidence, Project)
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import EvidenceSourceType, SkillCategory
from jobintel.storage.cv_artifact_store import CVArtifactStore

JD = """Build services in a collaborative team environment.
Key responsibilities
Design and develop scalable solutions using microservices.
Collaborate and communicate effectively with colleagues. Demonstrate operational excellence through monitoring and troubleshooting.
Work in an agile environment practicing CI/CD principles.
Basic Qualifications
- Experience with at least one programming language such as Java, Python or Go
Preferred Qualifications
- Debugging and troubleshooting complex systems
- Database systems (SQL and NoSQL); Version control systems
"""


def _job(description=JD, title="Software Engineer"):
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    return enrich_job(Job(id="job-x", title=title, company="Example Co", description=description,
                          locations=[Location(city="London", country="United Kingdom")],
                          source_observations=[SourceObservation(source_name="company", source_job_id="job-x",
                              original_url="https://example.com/jobs/x", first_seen_at=now, last_seen_at=now,
                              posted_at=now, raw_description=description,
                              canonical_application_url="https://example.com/jobs/x/apply")]), 2026)


def _candidate():
    repo = EvidenceSource(id="capmap:o/r@1", source_type=EvidenceSourceType.GITHUB_REPOSITORY_SYNC, title="o/r",
                          collected_at=date(2026, 10, 1))
    project = Project(id="p-app", name="Booking Service", description="A booking service.", evidence_sources=[repo])
    profile = CandidateProfile(
        id="c", name="Sam Sample", graduation_year=2026, email="sam@example.com", phone="07700 000000",
        github_url="https://github.com/sam-sample",
        education=[Education(institution="Example University", programme="MSc Computing", graduation_year=2026,
                             description="Relevant Modules (MSc): Computer Programming, Mobile Communication Systems")],
        experience=[Experience(organisation="Example Ltd", title="Software Intern", description="",
                               evidence=[ExperienceEvidence(id="e1", text="Worked with technical colleagues to investigate software and system problems, assisting with troubleshooting.", sources=(EvidenceSource(id="cv", source_type=EvidenceSourceType.EXPERIENCE_DESCRIPTION, title="CV", collected_at=date(2026, 10, 1)),), source_locations=("internship",))])],
        projects=[project])
    for name, quote, cat in (("Python", "Built a Python booking API with automated tests and CI checks.", SkillCategory.LANGUAGE),
                             ("DynamoDB", "Persisted bookings in DynamoDB tables.", SkillCategory.DATABASE),
                             ("SQL", "Queried booking history with SQL reports.", SkillCategory.DATA)):
        profile.upsert_capability(Capability(name=name, category=cat, technology=name, evidence=[
            CapabilityEvidence(source=repo, quote=quote, project_id="p-app", confidence=1.0, id=f"p-app:{name}",
                               category="Backend", claim_type="implementation")]))
    return profile


def _approve(root, *skills):
    path = Path(root) / "profile" / "skill_approvals.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "skills": {s: {"status": lx.USER_APPROVED_REFRESH, "approved_at": "2026-10-15",
                                                             "source": "test approval"} for s in skills}}))


# 1 + 2 + 3 -- alignment ---------------------------------------------------------------------------------

def test_exact_jd_keyword_is_kept_when_grounded():
    text, applied = lx.align_text("Collaborated with colleagues on CI checks.", {"collaboration"}, "collaborated")
    assert text == "Collaborated with colleagues on CI checks." and applied == []


def test_grounded_synonym_is_rewritten_into_the_jds_terminology():
    keys = {"collaboration", "debugging", "troubleshooting"}
    text, applied = lx.align_text("Worked with technical colleagues to investigate software problems.", keys,
                                  "assisting with troubleshooting")
    assert text == "Collaborated with technical colleagues to debug and troubleshoot software problems."
    assert applied == ["collaboration", "debugging"]


def test_a_debugging_rewrite_needs_troubleshooting_in_the_same_source():
    text, applied = lx.align_text("Worked with colleagues to investigate software problems.", {"debugging"}, "no such word")
    assert "debug" not in text and applied == []


def test_unsupported_jd_keywords_are_never_inserted_into_bullets():
    for key in ("scalable", "microservices", "fault-tolerant", "cloud-native", "distributed systems", "resilient"):
        assert key not in lx.SYNONYMS  # problem-shape claims have no equivalence rule
        assert all(rule.term != key for rule in lx.ALIGNMENT_RULES)


# 4 + 5 + 6 + 7 -- refresh skills, end to end ------------------------------------------------------------

def test_an_approved_refresh_skill_appears_in_skills_only_and_creates_a_backlog():
    with tempfile.TemporaryDirectory() as tmp:
        _approve(tmp, "Java")
        artifact = generate_and_save_cv(_candidate(), _job(), CVArtifactStore(tmp), store_root=tmp,
                                        github_freshness_state="current")
        skills = artifact.cv_text.split("TECHNICAL SKILLS", 1)[1].split("KEY PROJECTS", 1)[0]
        assert re.search(r"\bJava\b", skills)
        rest = artifact.cv_text.replace(skills, "")
        assert not re.search(r"\bJava\b", rest)  # never in a project / internship / summary claim
        row = next(r for r in artifact.ats_coverage if r["term"] == "Java")
        assert row["status"] == lx.USER_APPROVED_REFRESH and row["locations"] == ["technical skills"]
        assert artifact.learning_backlog and artifact.learning_backlog[0]["skill"] == "Java"
        assert "HashMap" in " ".join(artifact.learning_backlog[0]["minimum_interview_ready_target"])
        stored = json.loads((Path(tmp) / "profile" / "learning_backlog.json").read_text())
        assert [i["skill"] for i in stored["items"]] == ["Java"]
        assert not any("Java" in reason for reason in artifact.review_reasons)  # tracked, not a review gap


def test_an_unapproved_unsupported_skill_stays_omitted():
    with tempfile.TemporaryDirectory() as tmp:
        artifact = generate_cv(_candidate(), _job(), store_root=tmp, github_freshness_state="current")
        assert not re.search(r"\bJava\b|\bGo\b", artifact.cv_text)
        assert next(r for r in artifact.ats_coverage if r["term"] == "Java")["status"] == lx.UNSUPPORTED
        assert artifact.learning_backlog == []


# 8 -- soft-skill alignment through grounded experience --------------------------------------------------

def test_collaboration_is_surfaced_through_grounded_internship_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        artifact = generate_cv(_candidate(), _job(), store_root=tmp, github_freshness_state="current")
        assert "Collaborated with technical colleagues to debug and troubleshoot software" in artifact.cv_text
        assert "excellent communicator" not in artifact.cv_text.casefold()  # no self-evaluation is added
        row = next(r for r in artifact.ats_coverage if r["term"] == "collaboration")
        assert row["in_cv"] and any(loc.startswith("internship") for loc in row["locations"])


# 9 -- modules: incidental overlap never includes a module ----------------------------------------------

def test_incidental_word_overlap_does_not_include_a_module():
    from jobintel.matching.cv_editorial import select_modules
    line = "Relevant Modules (MSc): Computer Programming, Mobile Communication Systems"
    assert select_modules(line, "backend", "Collaborate and communicate effectively.") == \
        "Relevant Modules (MSc): Computer Programming"


# 10 -- the coverage report distinguishes the four statuses ----------------------------------------------

def test_coverage_report_distinguishes_grounded_aligned_refresh_and_unsupported():
    with tempfile.TemporaryDirectory() as tmp:
        _approve(tmp, "Java")
        artifact = generate_cv(_candidate(), _job(), store_root=tmp, github_freshness_state="current")
    status = {r["term"]: r["status"] for r in artifact.ats_coverage}
    assert status["Python"] == lx.GROUNDED_EXACT
    assert status["NoSQL"] == lx.GROUNDED_SYNONYM          # implied by grounded DynamoDB
    # The candidate's own repositories IMPLY Git; that is never labelled exact (no literal Git statement).
    assert status["version control"] == lx.GROUNDED_SYNONYM
    assert status["Java"] == lx.USER_APPROVED_REFRESH
    assert status["microservices"] == lx.UNSUPPORTED and status["scalable"] == lx.UNSUPPORTED
    omitted = next(r for r in artifact.ats_coverage if r["term"] == "microservices")
    assert not omitted["in_cv"] and omitted["reason_omitted"] == "insufficient grounded evidence"
    assert "ATS" not in artifact.cv_text and "USER_APPROVED" not in artifact.cv_text  # internal only


def test_terms_are_weighted_by_section():
    terms = {t.key: t for t in lx.extract_terms(JD, "Software Engineer")}
    assert terms["java"].sections == ["basic"] and terms["java"].weight == 3.0
    assert terms["nosql"].sections == ["preferred"] and terms["nosql"].weight == 1.5
    assert terms["collaboration"].phrase.startswith("Collaborate")  # quoted from its most important section
    assert "go" in terms and not re.search(lx.VOCABULARY["go"][1], "we go beyond", re.I)


# 11 -- no company or project name in the generic rules --------------------------------------------------

def test_the_lexical_layer_names_no_company_or_project():
    text = inspect.getsource(lx).casefold()
    for name in ("amazon", "monzo", "gocardless", "starling", "zopa", "afra", "indy", "meeteat", "fkip",
                 "personal-source", "howell", "tianfeng"):
        # "Amazon Web Services" is the AWS product name the vocabulary recognises, not company logic.
        assert not re.search(rf"\b{name}\b(?! web services)", text), name


# 12 -- the existing one-page / editorial rules still hold, on a real public JD fixture -------------------

def test_real_jd_fixture_regression_when_the_local_profile_is_present(tmp_path):
    from jobintel.profile_ingestion import load_candidate_profile
    profile = load_candidate_profile("data/local")
    if profile is None or not (Path("data/local/profile/skill_approvals.json")).exists():
        pytest.skip("Requires the private local profile and approvals (data/local, git-ignored)")
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "skill_approvals.json").write_text(Path("data/local/profile/skill_approvals.json").read_text())
    jd = Path("tests/fixtures/jd_amazon_sde_2026_london.txt").read_text()
    artifact = generate_cv(profile, _job(jd, "Software Development Engineer – 2026"), store_root=tmp_path,
                           github_freshness_state="current")
    text = artifact.cv_text
    assert artifact.page_count == 1 and len(artifact.document["skill_groups"]) <= 4
    skills = text.split("TECHNICAL SKILLS", 1)[1].split("KEY PROJECTS", 1)[0]
    assert re.search(r"\bJava\b", skills) and not re.search(r"\bJava\b", text.replace(skills, ""))
    for term in ("NoSQL", "Git", "Python", "TypeScript", "AWS", "SQL", "CI/CD"):
        assert re.search(rf"(?<!\w){re.escape(term)}(?!\w)", skills), term
    for absent in ("microservices", "fault-tolerant", "scalable", "cloud-native"):
        assert absent not in text.casefold(), absent
    assert "Collaborated with technical colleagues" in text
    assert not text.split("PROFESSIONAL SUMMARY", 1)[1].lstrip().startswith(("MSc", "BEng"))


# --- precision audit (2026-10-15) -------------------------------------------------------------------------

def test_a_jd_term_is_never_attributed_to_a_section_without_supporting_text():
    """Coverage locations come only from text in that section: a GenAI term grounded in a project is not
    reported in an internship whose own lines never mention it."""
    from jobintel.matching.cv_document import CVDocument, DatedEntry, ProjectBlock
    from types import SimpleNamespace
    term = lx.Term("genai", "GenAI / AI tools", lx.DOMAIN, "GenAI", 3.0, ["responsibilities"], 1)
    doc = CVDocument(name="x", headline="y", contact_line="", summary="Software engineer.",
                     education=[], skill_groups=[], experience=[DatedEntry(heading="Intern, Example Ltd",
                     details=["Collaborated with colleagues to troubleshoot data issues."])],
                     projects=[ProjectBlock("p", "Agent", [], [SimpleNamespace(text="Integrated a GenAI model provider.")])])
    assert lx._locations(term, doc) == ["project: Agent"]


def test_unscoped_self_listed_skills_ground_nothing():
    """An authored CV's skill list ("Data Structures & Algorithms") is not project / role evidence."""
    profile = _candidate()
    src = EvidenceSource(id="cv-old", source_type=EvidenceSourceType.CV_TEXT, title="old CV", collected_at=date(2025, 1, 1))
    profile.upsert_capability(Capability(name="Linux", category=SkillCategory.DEVOPS, technology="Linux", evidence=[
        CapabilityEvidence(source=src, quote="Software Engineering: Object-Oriented Programming, Data Structures & Algorithms",
                           project_id=None, confidence=1.0, id="cv-old:linux")]))
    view = lx.evidence_view(profile)
    terms = {t.key: t for t in lx.extract_terms("Basic qualifications\n- data structures, object-oriented design", "")}
    assert lx.term_status(terms["data structures"], view, {})[0] == lx.UNSUPPORTED
    assert lx.term_status(terms["object-oriented design"], view, {})[0] == lx.UNSUPPORTED


def test_an_algorithm_rewrite_needs_the_word_in_the_same_source():
    keys = {"algorithms"}
    assert lx.align_text("Built endpoints and deterministic consensus ranking.", keys, "tests for ranking algorithms")[0] == \
        "Built endpoints and a deterministic consensus ranking algorithm."
    assert lx.align_text("Built endpoints and deterministic consensus ranking.", keys, "no such word")[1] == []


def test_object_oriented_design_needs_an_abstraction_and_an_implementation(tmp_path):
    from jobintel.repo_capability_map import _design_slice, _Tree, statements_for
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "base.py").write_text("from abc import ABC\nclass Provider(ABC):\n    pass\n")
    (tmp_path / "pkg" / "impl.py").write_text("class FastProvider(Provider):\n    pass\nclass Room(Base):\n    pass\n")
    [slice_] = _design_slice(_Tree(tmp_path))
    assert slice_["facts"] == {"abstractions": ["Provider"], "implementations": ["FastProvider"]}
    assert "Object-Oriented Design" in slice_["capability_names"]
    assert statements_for(slice_)[0]["text"].startswith("Applied object-oriented design: 1 abstract interface (Provider)")
    (tmp_path / "pkg" / "impl.py").write_text("class Room(Base):\n    pass\n")  # no implementation of the ABC
    assert _design_slice(_Tree(tmp_path)) == []


def test_a_practice_skill_joins_skills_only_when_the_jd_names_it():
    from jobintel.matching.cv_editorial import keep_skill
    assert not keep_skill("Object-Oriented Design", "tooling", "web", set(), {"object-oriented design"})
    assert keep_skill("Object-Oriented Design", "tooling", "web", {"object-oriented design"}, set())


def test_git_implied_by_own_repositories_is_not_a_review_gate_gap():
    """2026-10-16: a JD requiring Git must not report "No evidence for Git" when the candidate's own GitHub
    repositories ground it (GROUNDED_SYNONYM); a skill with no grounding at all is still reported."""
    jd = "Requirements\n- Strong Python and Git experience.\n- Experience with Kafka.\n"
    with tempfile.TemporaryDirectory() as tmp:
        artifact = generate_cv(_candidate(), _job(jd), store_root=tmp, github_freshness_state="current")
    reasons = " ".join(artifact.review_reasons)
    assert "Kafka" in reasons and not re.search(r"No evidence for[^.]*\bGit\b", reasons)
