"""JD-driven CV identity (synthetic values only, offline).

* The CV heading is the target job's own title, normalised only where truth needs it: no invented
  umbrella title, no seniority / status / clearance the candidate does not hold.
* Delivery / collaboration wording is preferred only where the role archetype asks for it, chosen
  from supported statements (selection, never rewording); unsupported customer / client /
  stakeholder / production / commercial claims are still rejected.
"""
from __future__ import annotations

import re
import tempfile

import pytest

from jobintel.analysis.jd_requirements import extract_jd_requirements
from jobintel.matching import impact_writing as iw
from jobintel.matching.cv_generation import _archetype_signals, _select_experience_statements, generate_cv
from jobintel.matching.cv_title import cv_title, normalise_job_title
from jobintel.models.candidate import EvidenceSource, ExperienceEvidence
from jobintel.models.taxonomy import EvidenceSourceType
from test_cv_generation import CVGenerationTestCase, _job

POLICY = iw.load_default_policy()
AUTHORED = "Software Engineer"  # the candidate's own authored headline (fallback only)
UMBRELLA = "Backend, Cloud & AI Systems"


# --- 1. dynamic, JD-matched title --------------------------------------------------------------------

@pytest.mark.parametrize("job_title,expected", [
    ("Graduate Software Engineer", "Graduate Software Engineer"),                         # 1
    ("Backend Software Engineer", "Backend Software Engineer"),                           # 2
    ("Forward Deployed Engineer - Software Engineer", "Forward Deployed Engineer"),       # 3
    ("Forward Deployed Engineer - Software Engineer - United Kingdom", "Forward Deployed Engineer"),
    ("Graduate Cloud Software Engineer", "Graduate Cloud Software Engineer"),             # 4
    ("Senior Backend Engineer", "Backend Engineer"),                                      # 5
    ("Staff Software Engineer", "Software Engineer"),                                     # 6
    ("Principal ML Engineer", "Machine Learning Engineer"),                               # 7
    ("Lead Platform Engineer", "Platform Engineer"),
    ("Software Engineer - AI/ML", "Software Engineer - AI/ML"),
    ("Junior Software Engineer", "Junior Software Engineer"),
    ("Graduate Platform Engineer", "Graduate Platform Engineer"),
    ("Sr. Data Engineer", "Data Engineer"),
    ("Mid-level Software Engineer (m/f/d)", "Software Engineer"),
    ("Software Engineer II, Backend | Payments", "Software Engineer - Backend"),
    ("Graduate Software Engineer (2027 start)", "Graduate Software Engineer"),
    ("Security Cleared Software Engineer", "Software Engineer"),
    ("Software Engineering Intern", "Software Engineering Intern"),
])
def test_the_title_is_the_jobs_own_truthfully_normalised(job_title, expected):
    title = cv_title(job_title, AUTHORED)
    assert (title.title, title.source) == (expected, "jd_title")
    assert UMBRELLA not in title.title
    assert not re.search(r"\b(?:senior|staff|principal|lead|chartered|cleared|ii)\b", title.title, re.IGNORECASE)
    assert cv_title(job_title, AUTHORED) == title  # deterministic


@pytest.mark.parametrize("job_title", ["Engineering Manager", "Head of Engineering", "Director of Platform",
                                       "Chartered Engineer", "Security Cleared Engineer", "Tech Lead", ""])
def test_a_title_that_cannot_be_presented_truthfully_falls_back_to_the_authored_headline(job_title):
    title = cv_title(job_title, AUTHORED)
    assert (title.title, title.source) == (AUTHORED, "authored_fallback") and title.reason
    assert normalise_job_title(job_title)[0] is None


def test_early_career_markers_are_never_stripped():
    for marker in ("Graduate", "Junior", "Entry-level", "Associate", "Trainee", "Apprentice"):
        assert cv_title(f"{marker} Software Engineer").title == f"{marker} Software Engineer"


class TestGeneratedHeading(CVGenerationTestCase):
    def _heading(self, title):
        candidate = self._candidate_with_projects([("p1", "API Platform", ["Python", "PostgreSQL"])])
        artifact = self._generate(candidate, _job("Required: Python, PostgreSQL.", title=title),
                                  github_freshness_state="current")
        return artifact

    def test_the_generated_cv_heading_follows_each_job(self):
        backend = self._heading("Backend Software Engineer")
        fde = self._heading("Forward Deployed Engineer - Software Engineer - United Kingdom")
        senior = self._heading("Senior Backend Engineer")
        assert backend.cv_text.splitlines()[0] == "Test Candidate | Backend Software Engineer"
        assert fde.cv_text.splitlines()[0] == "Test Candidate | Forward Deployed Engineer"
        assert senior.cv_text.splitlines()[0] == "Test Candidate | Backend Engineer"
        assert backend.document["headline"] == "Backend Software Engineer"
        assert backend.fit_actions[0].startswith("title: 'Backend Software Engineer' from job title")
        assert "dropped: Senior" in senior.fit_actions[0]
        for artifact in (backend, fde, senior):
            assert UMBRELLA not in artifact.cv_text and "Senior" not in artifact.cv_text.splitlines()[0]


# --- 2. JD-driven delivery / operational evidence -------------------------------------------------

SOURCE = EvidenceSource(id="cv-1", source_type=EvidenceSourceType.CV_TEXT, title="Authored CV")
COLLAB = ("Worked with operations and reporting colleagues to investigate inventory-data discrepancies and "
          "support recurring reporting workflows.")
SQL = "Used SQL and Python to query, clean and validate database records, checking record consistency."
AUTOMATE = "Automated recurring Python data processing to make routine data preparation more repeatable."
DASHBOARD = "Maintained internal dashboards visualising inventory movements."
STATEMENTS = tuple(ExperienceEvidence(id=f"exp:{i}", text=text, sources=(SOURCE,), source_locations=("bullet",)) for i, text in
                   enumerate((SQL, COLLAB, AUTOMATE, DASHBOARD)))
# The same JD body for both roles, so only the role archetype differs.
JD_BODY = ("Python and SQL. You will investigate data issues, support reporting workflows, validate records and "
           "debug integrations.")


def _chosen(title):
    job = _job(JD_BODY, title=title)
    archetype = iw.impact_archetype(job.title, "backend_general_swe", POLICY)
    impact = type("Impact", (), {"policy": POLICY, "archetype": archetype})()
    return archetype, [s.text for s in _select_experience_statements(
        STATEMENTS, extract_jd_requirements(job), JD_BODY, _archetype_signals(impact))]


def test_8_a_backend_jd_prefers_data_correctness_and_never_gains_customer_language():
    archetype, chosen = _chosen("Backend Software Engineer")
    assert archetype == "backend"
    assert chosen[0] == SQL and COLLAB not in chosen and AUTOMATE in chosen
    assert not re.search(r"customer|client|stakeholder", " ".join(chosen), re.IGNORECASE)


def test_9_an_fde_jd_prefers_supported_collaboration_and_investigation_evidence():
    archetype, chosen = _chosen("Forward Deployed Engineer - Software Engineer")
    assert archetype == "forward_deployed_ai"
    # Same JD body as the backend case: only the role archetype differs.
    assert COLLAB in chosen and (AUTOMATE not in chosen or chosen.index(COLLAB) < chosen.index(AUTOMATE))
    assert set(chosen) <= {s.text for s in STATEMENTS}  # chosen verbatim, nothing reworded or added


def test_the_archetype_preference_never_makes_an_irrelevant_statement_relevant():
    from jobintel.matching.cv_generation import _archetype_preference, _experience_relevance
    job = _job("Required: Java.", title="Forward Deployed Engineer")
    jd = extract_jd_requirements(job)
    filler = ExperienceEvidence(id="exp:f", text="Collaborated with colleagues on shared goals.", sources=(SOURCE,),
                                source_locations=("bullet",))
    impact = type("Impact", (), {"policy": POLICY, "archetype": "forward_deployed_ai"})()
    signals = _archetype_signals(impact)
    assert _experience_relevance(filler.text, jd, [], []) == 0 and _archetype_preference(filler.text, signals) > 0
    chosen = _select_experience_statements((filler, *STATEMENTS), jd, "Required: Java.", signals)
    assert chosen[0].text != filler.text  # its preference bonus is not applied without real relevance


@pytest.mark.parametrize("archetype,title", [("backend", "Backend Software Engineer"),
                                             ("forward_deployed_ai", "Forward Deployed Engineer"),
                                             ("platform_systems", "Graduate Platform Engineer"),
                                             ("cloud_devops", "Graduate Cloud Software Engineer"),
                                             ("ai_applied", "Software Engineer - AI/ML")])
def test_each_role_family_has_its_own_archetype(archetype, title):
    assert iw.impact_archetype(title, "backend_general_swe", POLICY) == archetype
    spec = POLICY.spec["archetypes"][archetype]
    assert spec["foreground"] and spec["experience_signals"]
    collaboration = re.compile(r"collaborat|colleagues|work\(\?:ed")
    if archetype != "forward_deployed_ai":  # collaboration wording is preferred only where the role asks for it
        assert not any(collaboration.search(p) for p in spec["experience_signals"])


@pytest.mark.parametrize("claim,flag", [
    ("Delivered a customer-facing integration for enterprise clients.", "customer-facing"),
    ("Owned client delivery for the reporting integration.", "client-ownership"),
    ("Led stakeholder management across operations.", "stakeholder-management"),
    ("Provided consulting on data workflows.", "consulting"),
    ("Deployed the service to production.", "production-deployment"),
    ("Delivered commercial impact through automated reporting.", "commercial-impact"),
    ("Drove business transformation with dashboards.", "business-transformation"),
    ("Achieved user adoption across 300 users.", "user-adoption"),
    ("Delivered customer deployment of the agent.", "customer-deployment"),
])
def test_10_unsupported_delivery_and_commercial_claims_are_rejected(claim, flag):
    assert f"overclaim:{flag}" in iw.review_overclaims(claim, "Built a reporting script in Python.", POLICY)
    assert f"overclaim:{flag}" not in iw.review_overclaims(claim, claim, POLICY)  # only its own evidence supports it


def test_11_supported_operations_and_reporting_collaboration_stays_usable_and_unupgraded():
    statement = ("Collaborated with business operations and reporting colleagues to clarify data requirements, "
                 "investigate unusual records and support recurring reporting.")
    assert iw.review_overclaims(statement, statement, POLICY) == []
    rewrite = iw.compose_bullet(statement, project_id="exp", evidence_ids=["exp:1"], ownership_basis="experience_record",
                                archetype="forward_deployed_ai", policy=POLICY)
    assert rewrite.overclaim_flags == []
    assert rewrite.rewritten_text.startswith("Collaborated with")
    assert not re.search(r"\b(?:led|owned|spearheaded|architected|managed)\b", rewrite.rewritten_text, re.IGNORECASE)


# --- 3. selection stays JD-specific; artifacts stay immutable ----------------------------------------

class TestJDSpecificSelection(CVGenerationTestCase):
    def test_12_project_selection_follows_the_jd_not_a_fixed_set(self):
        candidate = self._candidate_with_projects([
            ("api", "API Service", ["Python", "PostgreSQL", "FastAPI"]),
            ("infra", "Infra Tooling", ["Docker", "AWS", "Terraform"]),
            ("ui", "Web Dashboard", ["React", "TypeScript"]),
        ])
        backend = self._generate(candidate, _job("Required: Python, PostgreSQL, FastAPI.", title="Backend Software Engineer"),
                                 github_freshness_state="current")
        cloud = self._generate(candidate, _job("Required: AWS, Docker, Terraform.", title="Graduate Cloud Software Engineer"),
                               github_freshness_state="current")
        assert backend.selected_project_ids[0] == "api" and cloud.selected_project_ids[0] == "infra"
        assert backend.cv_text.splitlines()[0] != cloud.cv_text.splitlines()[0]

    def test_13_a_new_generation_never_changes_an_existing_artifact(self):
        from jobintel.matching.cv_generation import generate_and_save_cv
        from jobintel.storage.cv_artifact_store import CVArtifactStore
        candidate = self._candidate_with_projects([("api", "API Service", ["Python", "PostgreSQL"])])
        with tempfile.TemporaryDirectory() as tmp:
            store = CVArtifactStore(tmp)
            first = generate_and_save_cv(candidate, _job("Required: Python.", title="Backend Software Engineer"), store,
                                         store_root=tmp, github_freshness_state="current")
            record_before = next(a for a in store.read_all() if a.id == first.id)
            pdf_before = open(first.pdf_path, "rb").read()
            second = generate_and_save_cv(candidate, _job("Required: Python.", title="Forward Deployed Engineer"), store,
                                          store_root=tmp, github_freshness_state="current")
            stored = {a.id: a for a in store.read_all()}
            assert first.id != second.id and {first.id, second.id} <= set(stored)
            assert stored[first.id] == record_before and open(first.pdf_path, "rb").read() == pdf_before
            assert first.cv_text.splitlines()[0].endswith("Backend Software Engineer")
            assert second.cv_text.splitlines()[0].endswith("Forward Deployed Engineer")


@pytest.mark.parametrize("job_title,expected", [
    ("Software Engineer (Intern)", "Software Engineer Intern"),
    ("Software Engineer (Graduate)", "Graduate Software Engineer"),
    ("Backend Engineer (Junior) - Payments", "Junior Backend Engineer"),
    ("Graduate Software Engineer (Graduate)", "Graduate Software Engineer"),
    ("Software Engineer Intern (Summer 2027)", "Software Engineer Intern"),
])
def test_an_early_career_parenthetical_stays_in_the_heading(job_title, expected):
    assert cv_title(job_title, "Authored").title == expected
