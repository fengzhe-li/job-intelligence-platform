"""Final CV editorial / compression layer (2026-10-15): generic, evidence-driven, no project/company names.

Synthetic projects, roles and evidence ids throughout, including the end-to-end editorial regression.
"""
from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest

from jobintel.matching import cv_editorial as ce
from jobintel.matching import cv_summary
from jobintel.matching.project_selection import generic_only_projects


# --- helpers ---------------------------------------------------------------------------------------------

def _bullet(eid, text, caps=(), weight=2.0):
    return SimpleNamespace(evidence_ids=[eid], text=text, capability_names=list(caps), weight=weight,
                           original_quote=None, provenance={})


def _project(pid, bullets, score=1.0):
    return SimpleNamespace(project_id=pid, name=pid, bullets=list(bullets), emphasis="secondary", selection_score=score,
                           technologies=[])


def _curated(category, claim_type="implementation"):
    return SimpleNamespace(category=category, claim_type=claim_type)


WEB_PROJECT = lambda: _project("p-web", [  # noqa: E731
    _bullet("p-web:capmap:frontend:app:views", "Built a React and TypeScript (Vite) frontend with 4 views (Search, Cart, Orders and Profile).", ["React", "TypeScript"]),
    _bullet("p-web:capmap:frontend:app:integration", "Integrated the UI with the backend through a typed API client (/api/orders, /api/cart), rendering results with loading and error states.", ["TypeScript"]),
    _bullet("p-web:capmap:backend:http_api:endpoints", "Implemented a FastAPI HTTP API with 9 endpoints (e.g. GET /health, POST /orders) and 6 Pydantic request/response models, tested through 3 HTTP-level test files.", ["FastAPI"]),
])
SYSTEMS_PROJECT = lambda: _project("p-sys", [  # noqa: E731
    _bullet("p-sys:networking", "Preserved an established TCP session across hosts with checkpoint/restore and a floating virtual IP (5/5 trials).", ["Linux", "CRIU"]),
    _bullet("p-sys:automation", "Automated failover testbed scripts measuring downtime and traffic lock/unlock across two hosts.", ["Bash"]),
])
EVIDENCE = {"p-sys:networking": _curated("Networking", "networking"), "p-sys:automation": _curated("DevOps", "automation")}


# 1 + 2 -- summary: target identity first, differs by role family, no status boilerplate, no project names

def _summary(theme, headline, projects):
    for p in projects:
        p.emphasis = "focus"
    return cv_summary.compose_summary(jd_text="", job_title=headline, projects=projects, education=[],
                                      headline=headline, evidence_by_id=EVIDENCE, theme=theme)


def test_summary_changes_materially_across_role_families():
    web = _summary("web", "Web Engineer", [WEB_PROJECT()])
    sre = _summary("sre", "Site Reliability Engineer", [SYSTEMS_PROJECT()])
    platform = _summary("platform", "Platform Engineer", [SYSTEMS_PROJECT()])
    ai = _summary("ai", "Software Engineer", [WEB_PROJECT()])
    assert web.startswith("Web Engineer focused on") and sre.startswith("Site Reliability Engineer focused on")
    assert len({web.split(".")[0], sre.split(".")[0], platform.split(".")[0], ai.split(".")[0]}) == 4
    assert "React/TypeScript" in web and "TCP" in sre


def test_summary_never_opens_with_education_status_or_names_projects():
    for theme, headline, project in (("web", "Web Engineer", WEB_PROJECT()), ("sre", "Site Reliability Engineer", SYSTEMS_PROJECT())):
        text = _summary(theme, headline, [project])
        assert not re.match(r"(?i)(msc|beng|bsc|graduate|student)\b", text)
        assert "student" not in text.casefold() and project.name not in text


# 3 + 6 -- one repository yields different bullets per JD family; project order follows the JD's priorities

def test_same_repository_produces_different_bullets_for_different_role_families():
    mixed = lambda: _project("p-mixed", [*WEB_PROJECT().bullets, *SYSTEMS_PROJECT().bullets])  # noqa: E731
    (web,), _ = ce.edit_projects([mixed()], "web", EVIDENCE)
    (sre,), _ = ce.edit_projects([mixed()], "sre", EVIDENCE)
    assert web.bullets[0].evidence_ids[0].startswith("p-web:capmap:frontend")   # frontend slice leads for web
    assert sre.bullets[0].evidence_ids[0].startswith("p-sys:")                  # systems slice leads for SRE
    assert [b.text for b in web.bullets] != [b.text for b in sre.bullets]


def test_project_order_changes_with_the_role_family():
    web_order, _ = ce.edit_projects([SYSTEMS_PROJECT(), WEB_PROJECT()], "web", EVIDENCE)
    sre_order, _ = ce.edit_projects([WEB_PROJECT(), SYSTEMS_PROJECT()], "sre", EVIDENCE)
    assert [p.project_id for p in web_order][0] == "p-web" and [p.project_id for p in sre_order][0] == "p-sys"
    assert web_order[0].emphasis == "focus"


# 4 + 11 -- redundancy inside a project; differentiation across similar-technology projects

def test_redundant_bullets_of_one_slice_are_removed():
    project = _project("p-api", [
        _bullet("p-api:capmap:backend:http_api:endpoints", "Implemented a FastAPI HTTP API with 5 endpoints.", ["FastAPI"]),
        _bullet("p-api:service", "Built a FastAPI backend providing REST endpoints for bookings and availability search.", ["FastAPI"]),
    ])
    (kept,), decision = ce.edit_projects([project], "backend", {"p-api:service": _curated("Backend", "architecture")})
    assert len(kept.bullets) == 1 and "bookings" in kept.bullets[0].text  # the curated narrative wins
    assert any("repeats the api slice" in r for r in decision.removed)


def test_similar_technology_projects_are_differentiated_by_capability():
    def twin(pid, views):
        return _project(pid, [
            _bullet(f"{pid}:capmap:frontend:app:views", f"Built a React and TypeScript (Vite) frontend with 3 views ({views}).", ["React"]),
            _bullet(f"{pid}:capmap:frontend:app:integration", "Integrated the UI with the backend through a typed API client, rendering results with loading and error states.", ["React"]),
            _bullet(f"{pid}:workflow", f"Built a review workflow with approve and publish actions for {pid} records.", ["FastAPI"]),
        ])
    ranked, _ = ce.edit_projects([twin("p-a", "Search, Cart and Orders"), twin("p-b", "Tasks, Review and Audit")], "web",
                                 {"p-a:workflow": _curated("Backend", "architecture"), "p-b:workflow": _curated("Backend", "architecture")})
    texts = [[b.text for b in p.bullets] for p in ranked]
    integration = [t for bullets in texts for t in bullets if t.startswith("Integrated the UI")]
    assert len(integration) == 1  # the identical template sentence appears once on the page
    assert any("Tasks, Review and Audit" in t for t in texts[1])  # the second project keeps its own views


# 7 -- code-audit detail and inventory never reach the page as-is

def test_code_audit_details_are_not_exposed_as_bullets():
    assert ce.is_audit_detail("Input shape: 128 x 259. Class order: A, B, C. Output layer: six-unit softmax.")
    compressed = ce.compress_text("Implemented a FastAPI HTTP API with 9 endpoints (e.g. GET /health, POST /orders) and "
                                  "6 Pydantic request/response models, tested through 3 HTTP-level test files.")
    assert "/health" not in compressed and "9 endpoints" not in compressed and "HTTP-level tests" in compressed
    assert ce.compress_text("Packaged the system as Docker images (2 Dockerfiles).") == "Packaged the system as Docker images."
    assert ce.compress_text("Built a dashboard using button, columns, dataframe and sidebar for inspection.") == "Built a dashboard for inspection."
    assert ce.compress_text("Analysed data using pandas and numpy for reporting.") == "Analysed data using pandas and numpy for reporting."
    project = _project("p-ml", [_bullet("p-ml:spec", "Input shape: 128 x 259. Class order: A, B, C."),
                                _bullet("p-ml:eval", "Evaluated the model with patient-independent splits.")])
    (kept,), decision = ce.edit_projects([project], "ai", {"p-ml:spec": _curated("ML/AI", "model_training"),
                                                           "p-ml:eval": _curated("Testing", "evaluation")})
    assert [b.text for b in kept.bullets] == ["Evaluated the model with patient-independent splits."]


# 8 -- relevant modules follow the role family; incidental word overlap does not count

MODULES = "Relevant Modules (MSc): Machine Learning, Internet of Things, Network and Services Management, Telecommunications Networks, Mobile Communication Systems"


def test_education_modules_change_with_jd_relevance():
    web, sre, ai = (ce.select_modules(MODULES, t, "") for t in ("web", "sre", "ai"))
    assert "Machine Learning" not in web and "Network and Services Management" in web
    assert "Telecommunications Networks" in sre and "Machine Learning" not in sre
    assert ai.endswith(": Machine Learning")
    # "communication skills" / "continuous learning" in a JD do not make the modules relevant
    assert ce.select_modules(MODULES, "web", "Strong communication skills and continuous learning.") == web
    assert ce.select_modules("Relevant Modules (BEng): Communication Systems", "web", "") is None


# 5 + 9 -- skills: low-relevance true skills omitted; at most four groups

def test_low_relevance_skills_are_omitted_even_when_true():
    assert not ce.keep_skill("CRIU", "tooling", "web", set(), {"criu"})
    assert ce.keep_skill("CRIU", "tooling", "sre", set(), {"criu"})
    assert ce.keep_skill("CRIU", "tooling", "web", {"criu"}, set())  # the JD naming it always counts
    assert not ce.keep_skill("Matplotlib", "tooling", "sre", set(), {"matplotlib"}, on_bullets=set())


def test_skills_use_at_most_four_groups():
    skills = ["Python", "React", "FastAPI", "PostgreSQL", "Pandas", "AWS", "Docker", "pytest", "Playwright", "CRIU",
              "TensorFlow", "STM32", "Power BI"]
    categories = {s.casefold(): c for s, c in zip(skills, ["language", "frontend", "backend", "database", "data", "cloud",
                                                           "devops", "tooling", "tooling", "tooling", "ml_ai", "embedded", "tooling"])}
    for theme in ce.THEME_SKILL_GROUPS:
        groups = ce.group_skills(skills, categories, theme)
        assert len(groups) <= ce.MAX_SKILL_GROUPS
        flat = [s for g in groups for s in g.skills]
        assert len(flat) == len(set(flat))  # never repeated across groups


# 10 -- generic Python/Docker/REST overlap alone does not force selection

def test_generic_overlap_alone_is_demoted():
    def match(pid, reqs):
        return SimpleNamespace(project=SimpleNamespace(id=pid), matched_requirements=reqs, score=1.0)

    caps = {"p-specific": "Systems", "p-generic-1": None, "p-generic-2": None}
    profile = SimpleNamespace(capabilities={
        "x": SimpleNamespace(evidence=[SimpleNamespace(project_id=pid, id=f"{pid}:capmap:systems:x:operations", category=cat,
                                                       confidence=1.0) for pid, cat in caps.items() if cat])})
    matches = [match("p-generic-1", ["Python", "Docker", "REST"]), match("p-generic-2", ["Python", "Docker", "REST"]),
               match("p-specific", ["Python"]), match("p-generic-3", ["Python", "Docker"])]
    demoted = generic_only_projects(matches, profile, {"Systems": 1.0})
    assert demoted == {"p-generic-1", "p-generic-2", "p-generic-3"}


def test_role_specific_evidence_outranks_generic_overlap_in_selection():
    from jobintel.matching import project_selection as ps
    assert ps.GENERIC_ONLY_FACTOR < 1.0
    source = inspect.getsource(ps.select_top_projects_hybrid)
    assert "generic_only_projects" in source and "GENERIC_ONLY_FACTOR" in source


# 12 -- no company- or project-specific exception anywhere in the editorial path

def test_the_editorial_path_names_no_company_or_project():
    from jobintel.matching import cv_summary as cs
    text = (inspect.getsource(ce) + inspect.getsource(cs)).casefold()
    for name in ("personal-source", "afra", "indy", "meeteat", "fkip", "deepbreath", "monzo", "gocardless", "zopa",
                 "starling", "barclays", "kpmg"):
        assert not re.search(rf"\b{re.escape(name)}\b", text), name


# --- synthetic end-to-end editorial regression --------------------------------------------------------

def test_sre_editorial_regression_with_synthetic_profile(tmp_path):
    from jobintel.analysis.job_enrichment import enrich_job
    from jobintel.matching.cv_generation import generate_cv
    from test_cv_document_structure import _candidate, _job
    profile = _candidate()
    job = _job()
    job.title = "Site Reliability Engineer"
    job.description = "Requirements: Python, Linux, testing and service reliability."
    sre = generate_cv(profile, enrich_job(job, 2026), store_root=tmp_path, github_freshness_state="fresh")
    assert sre.document["summary"].startswith("Site Reliability Engineer")
    assert len(sre.document["skill_groups"]) <= 4 and sre.page_count == 1
    assert not re.search(r"\bGET /|\(\d+ Dockerfiles?\)|\$\{", sre.cv_text)


# --- final-copy rules (2026-10-15) ------------------------------------------------------------------------

def test_a_specialisation_qualifier_survives_in_the_cv_title_without_seniority():
    from jobintel.matching.cv_title import normalise_job_title
    assert normalise_job_title("Software Engineer (ML Projects)")[0] == "Software Engineer (ML Projects)"
    assert normalise_job_title("Senior Software Engineer (ML Projects)")[0] == "Software Engineer (ML Projects)"
    assert normalise_job_title("Software Engineer (London)")[0] == "Software Engineer"  # noise still dropped
    assert normalise_job_title("Software Engineer (Projects)")[0] == "Software Engineer"  # no specialisation


def test_shape_parentheticals_are_removed():
    assert ce.compress_text("Mel-spectrogram extraction (128 x 259), and CNN inference.") == \
        "Mel-spectrogram extraction, and CNN inference."


def test_summary_lists_read_naturally():
    assert cv_summary._join(["Linux systems and networking", "service continuity|recovery", "cloud reliability"]) == \
        "Linux systems and networking, service continuity, recovery and cloud reliability"
    assert cv_summary._join(["cloud systems", "systems automation", "CI/CD|containerised delivery"]) == \
        "cloud systems, systems automation, CI/CD and containerised delivery"


def test_operational_reliability_wording_wins_a_supporting_slot_for_sre():
    project = _project("p-app", [
        _bullet("p-app:pipeline", "Built a Python evidence pipeline parsing results into tables with source provenance links."),
        _bullet("p-app:app", "Built a FastAPI application packaged with Docker, CI, and health/readiness checks."),
    ])
    evidence = {"p-app:pipeline": _curated("Data", "data_pipeline"), "p-app:app": _curated("Backend", "architecture")}
    (kept,), _ = ce.edit_projects([project], "sre", evidence)
    assert kept.bullets[0].text.endswith("health/readiness checks.")


def test_inventory_statements_never_take_a_third_slot():
    project = _project("p-ml", [
        _bullet("p-ml:eval", "Evaluated the model with patient-independent splits."),
        _bullet("p-ml:svc", "Built an inference service packaging feature extraction and prediction."),
        _bullet("p-ml:capmap:llm:libraries:libraries", "Integrated a model provider client.", weight=4.0),
    ])
    (kept,), decision = ce.edit_projects([project], "ai", {"p-ml:eval": _curated("Testing", "evaluation"),
                                                           "p-ml:svc": _curated("Backend", "deployment")})
    assert "Integrated a model provider client." not in [b.text for b in kept.bullets]
    assert any("inventory statement" in r for r in decision.removed)
