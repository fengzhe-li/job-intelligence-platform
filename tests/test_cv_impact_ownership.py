"""CV Impact & Ownership Writing (CV_IMPACT_OWNERSHIP_V1) and Engineering Value Translation.

Synthetic evidence only. Each test is tagged with the requirement letter it covers (A-Z).
Evidence truth stays the highest authority: every rewrite is checked against the bullet's own
evidence and refused (verbatim evidence kept) when it adds a metric, number, qualifier loss,
overclaim, ownership escalation or grounding rejection the evidence did not already carry.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml
from pypdf import PdfReader

from jobintel.matching import impact_writing as iw
from jobintel.matching.claim_grounding import validate_bullet_claim
from jobintel.matching.cv_generation import generate_and_save_cv, generate_cv
from jobintel.models.candidate import CandidateProfile, CapabilityEvidence, EvidenceSource, Project
from jobintel.models.taxonomy import EvidenceSourceType, ReviewGateStatus
from jobintel.storage.cv_artifact_store import CVArtifactStore
from test_cv_document_structure import _candidate, _job

POLICY = iw.load_default_policy()
README = EvidenceSource(id="proj", source_type=EvidenceSourceType.README_MARKDOWN, title="Project", uri="https://example.org/r",
                        collected_at=date(2026, 1, 1))


def compose(text, basis="personal_project", archetype="backend", check=None):
    return iw.compose_bullet(text, project_id="proj", evidence_ids=["proj:1"], ownership_basis=basis, archetype=archetype,
                             policy=POLICY, grounding_check=check)


def grounding_for(quote, project_id="proj", description=""):
    project = Project(id=project_id, name="Project", description=description)
    evidence = [CapabilityEvidence(source=README, quote=quote, project_id=project_id, id=f"{project_id}:1")]
    candidate = CandidateProfile(id="c", name="Sam Sample", graduation_year=2026)
    return lambda text: validate_bullet_claim(text, project, evidence, candidate).rejection_reasons


# --- policy contract ----------------------------------------------------------------------------

def test_policy_is_versioned_and_keeps_the_tailoring_precedence():
    tailoring = yaml.safe_load(Path("data/policies/cv_tailoring_skill_v1.yaml").read_text())["CV_TAILORING_SKILL_V1"]
    assert POLICY.precedence == tailoring["precedence"] == [
        "evidence_truth", "jd_relevance", "evidence_strength", "recruiter_clarity", "non_redundancy", "one_page_readability"]
    assert POLICY.id.startswith("cv_impact_ownership_v1.0@") and POLICY.spec["extends"] == "CV_TAILORING_SKILL_V1"
    assert POLICY.source_asset == "data/policies/sources/cv_impact_ownership_v1_source.md"
    assert len(POLICY.source_hash) == 64


def test_policy_source_hash_contract():
    source = Path(POLICY.source_asset)
    if not source.is_file():
        pytest.skip("Private source asset not present in this checkout")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == POLICY.source_hash


def test_a_tampered_source_asset_fails_closed(tmp_path, monkeypatch):
    source = tmp_path / "data/policies/sources/cv_impact_ownership_v1_source.md"
    source.parent.mkdir(parents=True)
    source.write_text("tampered")
    shutil.copy("data/policies/cv_impact_ownership_v1.yaml", tmp_path / "policy.yaml")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(iw.PolicyIntegrityError):
        iw.ImpactPolicy.load(tmp_path / "policy.yaml")


# --- A / B: action-first composition --------------------------------------------------------------

@pytest.mark.parametrize("original,expected", [
    ("An event-driven backend: devices publish through a broker to functions that deduplicate and persist events.",
     "Built an event-driven backend: devices publish through a broker to functions that deduplicate and persist events."),
    ("FastAPI REST API backend serving order endpoints with request validation and structured error responses.",
     "Built a FastAPI REST API backend serving order endpoints with request validation and structured error responses."),
    ("Progressive migration framework: Level 1 application migration, Level 2 container restore (8/10 final trials).",
     "Built a progressive migration framework: Level 1 application migration, Level 2 container restore (8/10 final trials)."),
    ("Strict Pydantic v2 schemas validating request parameters.", "Defined strict Pydantic v2 schemas validating request parameters."),
    ("Embedded C firmware implemented for an STM32 microcontroller using hardware timers.",
     "Implemented embedded C firmware for an STM32 microcontroller using hardware timers."),
    ("C and Python socket utilities manipulating TCP socket states.", "Developed C and Python socket utilities manipulating TCP socket states."),
])
def test_A_noun_fragment_becomes_action_first(original, expected):
    rewrite = compose(original)
    assert rewrite.rewritten_text == expected
    assert rewrite.changed and rewrite.recruiter_clarity == "action" and not rewrite.refused
    assert rewrite.original_text == original


@pytest.mark.parametrize("text", [
    "Set up a CI/CD pipeline that runs the test suite on every push.",   # irregular past verb
    "Automated nightly data validation with Python scripts.",            # already action-first
    "Established TCP + VIP: restore preserves an active session (5/5 final trials).",  # label, no artifact noun
    "Built around AWS IoT Core, Lambda and PostgreSQL, separating state from audit logs.",  # stack inventory
])
def test_A_openings_without_a_safe_action_rewrite_are_left_verbatim(text):
    assert compose(text).rewritten_text == text


def test_A_unmeasured_performance_adjective_is_narrowed_away_never_added():
    rewrite = compose("High-throughput race telemetry data pipeline processing timing intervals with Python and Polars.")
    assert rewrite.rewritten_text == "Built a race telemetry data pipeline processing timing intervals with Python and Polars."
    assert "narrowed:removed_unmeasured_adjective:high-throughput" in rewrite.rewrite_rationale
    measured = compose("High-throughput telemetry pipeline processing 5,000 events/s with Python.")
    assert measured.rewritten_text == "Built a high-throughput telemetry pipeline processing 5,000 events/s with Python."


def test_A_noun_fragments_from_non_project_sources_are_not_given_an_ownership_verb():
    rewrite = compose("Reporting dashboard for weekly operational KPIs.", basis="evidence_source_cv_text")
    assert rewrite.rewritten_text == "Reporting dashboard for weekly operational KPIs."
    assert any("does_not_prove_personal_execution" in reason for reason in rewrite.refused)


def test_B_flagship_first_bullet_states_what_was_built():
    items = [iw.ProjectItem("120 tests passed locally, including 10 integration tests.", ["p:tests"], 1.5),
             iw.ProjectItem("Event ingestion service validating device payloads with strict schemas.", ["p:svc"], 1.2)]
    composed = iw.compose_project(items, project_id="p", ownership_basis="personal_project", archetype="backend", policy=POLICY)
    first = composed[0].rewrite
    assert first.rewritten_text.startswith("Built an event ingestion service")
    assert "ordered_first:states_what_was_built" in first.rewrite_rationale
    assert composed[1].rewrite.rewritten_text == "120 tests passed locally, including 10 integration tests."


def test_B_generated_cv_flagship_projects_open_with_what_was_built():
    artifact = generate_cv(_candidate(), _job(), store_root=_tmp(), github_freshness_state="current")
    focus = [p["project_id"] for p in artifact.document["projects"] if p["emphasis"] == "focus"]
    assert focus and all(artifact.writing_quality["first_bullet_ownership"][pid] for pid in focus)


# --- C - G: metric classification ------------------------------------------------------------------

def test_C_bounded_trials_stay_bounded_trials():
    text = "Restore plus floating VIP preserves an active session (5/5 final trials) without dropping the client socket."
    [metric] = [m for m in iw.classify_metrics(text, POLICY) if m.metric_type != "count"]
    assert (metric.metric_type, metric.value, metric.denominator, metric.context) == ("bounded_trials", "5", "5", "experiment")
    assert "zero downtime" in metric.must_not_become
    stronger = text.replace("preserves an active session (5/5 final trials)", "guarantees zero downtime (5/5 final trials)")
    assert "overclaim:zero-downtime" in iw.rewrite_problems(text, stronger, POLICY)
    assert "overclaim:guaranteed" in iw.rewrite_problems(text, stronger, POLICY)


def test_D_8_of_10_is_never_rewritten_as_100_percent():
    original = "Docker restore succeeded in 8/10 final trials."
    problems = iw.rewrite_problems(original, "Docker restore succeeded in 100% of final trials.", POLICY)
    assert "metric_lost:8/10 final trials" in problems and "percentage_introduced" in problems
    assert "number_introduced" in problems


@pytest.mark.parametrize("stronger,expected", [
    ("102 E2E tests passed locally.", "metric_lost:102 tests"),
    ("102 tests passed locally with 95% coverage.", "overclaim:coverage-percentage"),
    ("102 tests passed locally, giving 100% reliability.", "overclaim:reliability-percentage"),
])
def test_E_test_count_is_not_coverage_e2e_or_reliability(stronger, expected):
    original = "102 tests passed locally."
    [metric] = iw.classify_metrics(original, POLICY)
    assert metric.metric_type == "test_count" and "coverage" in metric.must_not_become
    assert expected in iw.rewrite_problems(original, stronger, POLICY)


def test_F_simulator_trials_are_not_road_trials():
    original = "Controller evaluated across 513 simulator trials."
    [metric] = [m for m in iw.classify_metrics(original, POLICY) if m.metric_type != "count"]
    assert metric.metric_type == "simulator_dataset" and "road trials" in metric.must_not_become
    assert "metric_lost:513 simulator trials" in iw.rewrite_problems(original, "Controller validated across 513 road trials.", POLICY)
    assert "qualifier_lost:simulator" in iw.rewrite_problems(original, "Controller validated across 513 road trials.", POLICY)


def test_G_external_evaluation_cases_are_not_customer_adoption():
    original = "Agent answers scored on 15 external evaluation cases."
    [metric] = [m for m in iw.classify_metrics(original, POLICY) if m.metric_type != "count"]
    assert metric.metric_type == "evaluation_sample" and "customer adoption" in metric.must_not_become
    problems = iw.rewrite_problems(original, "Agent answers scored on 15 external evaluation cases, driving customer adoption.", POLICY)
    assert "overclaim:customer-deployment" in problems


# --- H - J: technology presence is not performance / production / truth -----------------------------

def test_H_async_framework_does_not_imply_low_latency():
    evidence = "FastAPI service with async worker processing and structured error responses."
    check = grounding_for(evidence)
    assert "unsupported_absolute_claim:low-latency" in check("Built a low-latency async FastAPI service with structured error responses.")
    assert iw.review_overclaims("Built a low latency FastAPI service.", evidence, POLICY) == ["overclaim:low-latency"]


def test_I_rag_and_citations_do_not_imply_zero_hallucination():
    evidence = "RAG pipeline returning cited passages for each generated answer."
    check = grounding_for(evidence)
    assert "unsupported_absolute_claim:zero-hallucination" in check("RAG pipeline with zero hallucinations, returning cited passages.")
    assert "overclaim:zero-hallucination" in iw.review_overclaims("Eliminated hallucinations with citations.", evidence, POLICY)


def test_J_docker_and_config_do_not_imply_production_deployment():
    evidence = "Docker Compose stack, typed configuration and CI checks."
    check = grounding_for(evidence)
    reasons = check("Docker Compose stack deployed to production with typed configuration and CI checks.")
    assert "unsupported_absolute_claim:production-deployment" in reasons
    assert "overclaim:production-ready" in iw.review_overclaims("Production-ready Docker stack.", evidence, POLICY)


# --- K - M: ownership truthfulness -------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Assisted with the development and maintenance of internal tools, including configuration and testing.",
    "Participated in image-recognition development tasks, including annotation and testing recognition results.",
    "Worked with colleagues to investigate software and database problems.",
    "Supported internal applications through configuration and troubleshooting.",
])
def test_K_support_wording_is_never_upgraded(text):
    rewrite = compose(text, basis="experience_record")
    assert rewrite.rewritten_text == text
    assert rewrite.ownership_truthfulness == "collaborative_preserved"
    for verb in ("Led", "Spearheaded", "Owned", "Architected", "Orchestrated"):
        assert verb not in rewrite.rewritten_text


def test_K_escalation_verbs_are_rejected_as_rewrites():
    original = "Supported the release of the reporting service."
    assert "ownership_escalation:led" in iw.rewrite_problems(original, "Led the release of the reporting service.", POLICY)
    assert any(p.startswith("ownership_escalation") for p in
               iw.rewrite_problems("Reporting service.", "Spearheaded the reporting service.", POLICY))


def test_L_internship_automation_never_gains_hours_or_percentages():
    text = "Developed Python scripts using Pandas to automate recurring data-processing tasks, reducing repetitive manual work."
    rewrite = compose(text, basis="experience_record")
    assert rewrite.rewritten_text == text
    assert rewrite.engineering_value is not None and not any(ch.isdigit() for ch in rewrite.rewritten_text)
    assert any(v.rule_id == "reduced_manual_processing" and v.level == "L4" for v in iw.translate_value(text, POLICY, "backend"))
    problems = iw.rewrite_problems(text, text[:-1] + ", saving 10 hours per week.", POLICY)
    assert "number_introduced" in problems and "overclaim:time-saved" in problems
    assert "overclaim:x-percent-faster" in iw.rewrite_problems(text, text[:-1] + ", 40% faster.", POLICY)


def test_L_reduced_manual_work_is_only_translated_when_the_evidence_says_so():
    values = iw.translate_value("Developed Python scripts using Pandas for data cleaning and validation.", POLICY, "backend")
    assert "reduced_manual_processing" not in {v.rule_id for v in values}


def test_M_howell_testing_support_becomes_action_first_but_keeps_the_support_qualifier():
    text = "Supported software testing by checking application behaviour, identifying unexpected results and documenting issues."
    rewrite = compose(text, basis="experience_record")
    assert rewrite.rewritten_text == ("Checked application behaviour, identified unexpected results and documented issues, "
                                      "in support of software testing.")
    assert rewrite.review_required  # ownership wording changed: a human confirms it
    for claim in ("Built", "automated", "QA", "Led", "Owned"):
        assert claim not in rewrite.rewritten_text


def test_M_qa_automation_ownership_is_never_invented():
    text = "Supported software testing by checking application behaviour and documenting issues."
    problems = iw.rewrite_problems(text, "Built automated QA checks for application behaviour and issue documentation.", POLICY)
    assert "qualifier_lost:supported" in problems
    unknown = compose("Supported QA by orchestrating regression suites.", basis="experience_record")
    assert unknown.rewritten_text == "Supported QA by orchestrating regression suites."  # unknown activity: no conversion


# --- N: cross-project isolation ------------------------------------------------------------------------

def test_N_cross_project_technology_is_blocked_and_reported():
    agent_check = grounding_for("Research agent API with structured error responses.", project_id="agent")
    assert "unsupported_technology:qdrant" in agent_check("Research agent API with Qdrant retrieval and structured error responses.")
    report = iw.writing_quality_report(
        [("agent", [("Built a research agent API with Qdrant retrieval.", ["agent:1"])])], [], None, POLICY,
        project_allowed_tech={"agent": {"fastapi", "python"}, "platform": {"qdrant"}})
    assert report["cross_project_leakage"] == [{"project_id": "agent", "technology": "qdrant",
                                               "text": "Built a research agent API with Qdrant retrieval."}]
    assert any("not evidenced by the bullet's own project" in r for r in iw.writing_review_reasons(report, []))


def test_N_the_composer_never_adds_a_technology():
    rewrite = compose("Research agent API with structured error responses.", archetype="forward_deployed_ai")
    from jobintel.matching.claim_grounding import extract_tech_entities
    assert extract_tech_entities(rewrite.rewritten_text) <= extract_tech_entities(rewrite.original_text) | {"rest", "api"}


# --- O - R: role-specific value foregrounding ---------------------------------------------------------

API = "Order API backend serving REST endpoints with request validation and structured error responses."
UI = "React frontend with interactive room creation and voting interface."
FIRMWARE = "Embedded C firmware implemented for an STM32 microcontroller reading I2C sensor registers."
MIGRATION = "Checkpoint/restore harness migrating TCP socket state between Linux hosts (5/5 final trials)."


def ranked(archetype, *texts):
    items = [iw.ProjectItem(text, [f"p:{i}"], 1.0) for i, text in enumerate(texts)]
    composed = iw.compose_project(items, project_id="p", ownership_basis="personal_project", archetype=archetype, policy=POLICY)
    return [texts[entry.index] for entry in composed]


def test_O_forward_deployed_role_foregrounds_integration_and_delivery():
    assert iw.impact_archetype("Forward Deployed Engineer - Software Engineer", "backend_general_swe", POLICY) == "forward_deployed_ai"
    assert ranked("forward_deployed_ai", FIRMWARE, API)[0] == API
    rewrite = compose(API, archetype="forward_deployed_ai")
    assert {"api", "failure_handling", "data_contracts"} <= set(rewrite.jd_relevance)
    assert "customer" not in rewrite.rewritten_text.casefold()


def test_P_backend_role_foregrounds_api_and_correctness():
    assert iw.impact_archetype("Graduate Backend Engineer", "backend_general_swe", POLICY) == "backend"
    assert ranked("backend", UI, API)[0] == API


def test_Q_platform_role_foregrounds_system_mechanisms():
    assert iw.impact_archetype("Graduate Platform Engineer", "platform_cloud_systems", POLICY) == "platform_systems"
    assert ranked("platform_systems", UI, MIGRATION)[0] == MIGRATION
    assert "high availability" not in compose(MIGRATION, archetype="platform_systems").rewritten_text.casefold()


def test_R_automation_role_foregrounds_hardware_integration_and_validation():
    assert iw.impact_archetype("Automation Graduate", "motorsport", POLICY) == "automation_embedded"
    assert ranked("automation_embedded", API, FIRMWARE)[0] == FIRMWARE
    cloud = "Python Lambda functions keeping historical records separate from current sensor state."
    assert "hardware_integration" not in {v.rule_id for v in iw.translate_value(cloud, POLICY, "automation_embedded")}
    assert "plc" not in compose(FIRMWARE, archetype="automation_embedded").rewritten_text.casefold()


# --- S: hard overclaim review ----------------------------------------------------------------------------

@pytest.mark.parametrize("claim,flag", [
    ("Zero-downtime migration.", "zero-downtime"), ("zero downtime migration", "zero-downtime"),
    ("Zero Down-Time cutover", "zero-downtime"), ("zero-loss replication", "zero-loss"),
    ("zero packet loss failover", "zero-loss"), ("zero data loss", "zero-loss"), ("zero-regression release", "zero-regression"),
    ("guaranteed consistency", "guaranteed"), ("production-ready API", "production-ready"),
    ("production ready API", "production-ready"), ("production-scale pipeline", "production-scale"),
    ("sub-second responses", "sub-second"), ("subsecond responses", "sub-second"), ("real-time ingestion", "real-time"),
    ("real time ingestion", "real-time"), ("high-throughput queue", "high-throughput"), ("high throughput queue", "high-throughput"),
    ("low-latency service", "low-latency"), ("ultra low latency service", "low-latency"),
    ("high availability cluster", "high-availability"), ("highly available cluster", "high-availability"),
    ("fault-tolerant design", "fault-tolerant"), ("fault tolerant design", "fault-tolerant"),
    ("95%+ coverage", "coverage-percentage"), ("95% test coverage", "coverage-percentage"),
    ("100% reliability", "reliability-percentage"), ("40% faster builds", "x-percent-faster"), ("3x faster builds", "x-percent-faster"),
    ("5 hours/week saved", "time-saved"), ("saved 5 hours per week", "time-saved"),
    ("customer deployment of the agent", "customer-deployment"), ("customer-facing delivery", "customer-deployment"),
    ("trained an LLM on filings", "trained-llm"), ("led a team of four", "team-leadership"), ("owned the platform team", "team-leadership"),
])
def test_S_unsupported_absolute_and_performance_wording_is_flagged(claim, flag):
    assert f"overclaim:{flag}" in iw.review_overclaims(claim, "Python service with tests.", POLICY)


def test_S_measurement_class_terms_need_a_measurement_in_the_evidence():
    assert iw.review_overclaims("high-throughput queue", "A high-throughput queue.", POLICY) == ["overclaim:high-throughput"]
    assert iw.review_overclaims("high-throughput queue", "A high-throughput queue sustaining 12,000 msg/s.", POLICY) == []
    assert iw.review_overclaims("zero-downtime cutover", "Measured zero downtime in 5/5 trials.", POLICY) == []


# --- T - V: writing quality gate ---------------------------------------------------------------------------

def test_T_U_V_quality_metrics():
    lines = [("p", [("Built an order API with request validation.", ["p:1"]),
                    ("Order API backend for intake and fulfilment tracking with structured errors.", ["p:2"]),
                    ("120 tests passed locally, including 10 integration tests.", ["p:3"])]),
             ("q", [("Built a reporting pipeline with request validation and retries.", ["q:1"]),
                    ("Built a scheduler for nightly exports.", ["q:2"]),
                    ("Built a dashboard for operational metrics.", ["q:3"]),
                    ("Built a parser for vendor files.", ["q:4"])])]
    report = iw.writing_quality_report(lines, ["Assisted with testing internal tools."], None, POLICY)
    assert report["lines_total"] == 8
    assert report["action_openings"] == 5 and report["collaborative_openings"] == 1
    assert report["noun_fragment_openings"] == 1 and report["metric_first_openings"] == 1
    assert report["action_opening_ratio"] == round(6 / 8, 3)                        # T
    assert report["noun_fragment_ratio"] == round(1 / 8, 3)                          # U
    assert report["repeated_verbs"] == {"Built": 5}                                  # V: excessive verb
    assert "with request validation" not in report["repeated_phrases"]               # short overlap is fine
    assert report["first_bullet_ownership"] == {"p": True, "q": True}
    assert {m["type"] for m in report["metrics"]} == {"test_count"}
    assert all(m["evidence_ids"] == ["p:3"] for m in report["metrics"])              # metric provenance


def test_V_repeated_phrasing_and_generic_value_language():
    report = iw.writing_quality_report(
        [("p", [("Built a reporting service that ensured quality for every release cycle.", ["p:1"]),
                ("Developed release tooling that ensured quality for every release cycle.", ["p:2"])])],
        [], "Results-driven engineer who improved efficiency.", POLICY)
    assert "quality for every release" in report["repeated_phrases"]
    assert {g["phrase"].casefold() for g in report["generic_value_language"]} >= {"ensured quality", "improved efficiency"}


def test_V_verb_variety_rotates_only_inserted_interchangeable_verbs():
    rewrites = [compose(text) for text in (
        "Order API backend with validation.", "Event pipeline with retries.", "Ingestion service for files.",
        "Reporting platform for KPIs.", "Billing service for invoices.")]
    assert all(r.rewritten_text.startswith("Built ") for r in rewrites)
    changed = iw.vary_repeated_verbs(rewrites, POLICY)
    assert len(changed) == 2 and all(r.rewritten_text.split()[0] in {"Developed", "Implemented"} for r in changed)
    assert all(r.original_text in {c.original_text for c in rewrites} for r in changed)


# --- W: provenance -------------------------------------------------------------------------------------------

def test_W_evidence_ids_and_original_text_survive_every_rewrite():
    candidate = _candidate()
    artifact = generate_cv(candidate, _job(), store_root=_tmp(), github_freshness_state="current")
    quotes = {e.id: e.quote for c in candidate.capabilities.values() for e in c.evidence}
    rewritten = [b for b in artifact.bullets if b.status == "impact_rewritten"]
    assert rewritten
    for bullet in rewritten:
        assert bullet.provenance["evidence_ids"] == sorted(bullet.evidence_ids)
        assert bullet.provenance["source_project_id"] == bullet.project_id
        assert bullet.original_quote == bullet.provenance["original_text"]
        assert any(bullet.original_quote in quotes[eid] for eid in bullet.evidence_ids)
        assert bullet.provenance["rewritten_text"] == bullet.text
        for key in ("action_verb", "engineering_value", "metric_scope", "overclaim_flags", "rewrite_rationale", "review_required"):
            assert key in bullet.provenance
    assert set(artifact.evidence_ids_used) >= {eid for b in artifact.bullets for eid in b.evidence_ids}
    assert artifact.writing_policy == POLICY.id


def test_W_review_gate_sees_rewritten_value_claims():
    extractive = generate_cv(_candidate(), _job(), store_root=_tmp(), github_freshness_state="current", writing_policy=None)
    composed = generate_cv(_candidate(), _job(), store_root=_tmp(), github_freshness_state="current")
    assert extractive.review_status == ReviewGateStatus.AUTO_PREPARE and extractive.writing_quality == {}
    if composed.writing_quality["rewrites"]["review_required"]:
        assert composed.review_status == ReviewGateStatus.REVIEW_REQUIRED
        assert any(r.startswith("Impact/ownership rewrite(s) need human review") for r in composed.review_reasons)


def test_W_summary_narrowing_drops_only_unsupported_scope_items():
    summary = "Graduate engineer. Skilled in REST APIs, distributed systems, databases and ML."
    narrowed, removed, _ = iw.narrow_summary(summary, "FastAPI REST API with PostgreSQL.", POLICY)
    assert narrowed == "Graduate engineer. Skilled in REST APIs, databases and ML." and removed == ["distributed systems"]
    kept, removed, _ = iw.narrow_summary(summary, "Consensus protocol for distributed systems coursework project.", POLICY)
    assert kept == summary and removed == []


# --- X / Y: one page and immutability -------------------------------------------------------------------

def test_X_composed_cv_is_still_one_real_pdf_page():
    artifact = generate_cv(_candidate(), _job(), store_root=_tmp(), github_freshness_state="current")
    assert artifact.page_count == 1 and artifact.fits_one_page
    assert len(PdfReader(artifact.pdf_path).pages) == 1
    assert all(len(b.text) <= POLICY.composition["max_rendered_chars"] or not b.provenance.get("value_rendered")
               for b in artifact.bullets)


def test_Y_historical_artifacts_stay_byte_identical(tmp_path):
    store = CVArtifactStore(tmp_path)
    old = generate_and_save_cv(_candidate(), _job(), store, store_root=tmp_path, github_freshness_state="current", writing_policy=None)
    record_bytes = store.path.read_bytes()
    pdf_bytes = Path(old.pdf_path).read_bytes()
    legacy = json.loads(record_bytes)
    for key in ("writing_policy", "writing_quality"):
        legacy.pop(key)
    for bullet in legacy["bullets"]:
        bullet.pop("provenance")
    legacy_line = (json.dumps(legacy, ensure_ascii=False, sort_keys=True) + "\n").encode()
    store.path.write_bytes(legacy_line)  # a record written before the policy existed

    new = generate_and_save_cv(_candidate(), _job(), store, store_root=tmp_path, github_freshness_state="current")
    assert new.id != old.id and new.writing_policy == POLICY.id
    assert store.path.read_bytes().startswith(legacy_line)            # append-only: old record untouched
    assert Path(old.pdf_path).read_bytes() == pdf_bytes                # old PDF untouched
    reread = store.get(old.id)
    assert reread.writing_policy == "" and reread.writing_quality == {} and all(b.provenance == {} for b in reread.bullets)
    assert hashlib.sha256(Path(reread.pdf_path).read_bytes()).hexdigest() == old.pdf_sha256


def _tmp() -> Path:
    import tempfile
    return Path(tempfile.mkdtemp(prefix="impact-cv-"))


# --- repository corroboration gate (verified / partial / contradicted / unavailable) --------------------

def _gate(text, *, readme, indexed="", forbidden=(), strengthened=True):
    bullets = [("p", text, ["p:1"])]
    claims, per_bullet = iw.corroborate(bullets, POLICY, repo_text={"p": readme}, project_evidence_text={"p": indexed},
                                        project_forbidden={"p": set(forbidden)}, strengthened={text} if strengthened else set())
    report = {"corroboration": claims, "claim_corroboration": per_bullet}
    return per_bullet[0]["status"], {c["claim"]: c["status"] for c in claims}, iw.writing_review_reasons(report, [])


def test_corroboration_verified_passes():
    status, claims, reasons = _gate("Built a FastAPI service with Docker.", readme="FastAPI service, Docker Compose.")
    assert status == "verified" and claims == {"FastAPI": "verified", "Docker": "verified"} and reasons == []


def test_corroboration_readme_silent_but_indexed_evidence_is_partial_not_false():
    status, claims, reasons = _gate("Built a FastAPI service with Pydantic request models.", readme="FastAPI service.",
                                    indexed="FastAPI layer with Pydantic request models.")
    assert status == "partial" and claims["Pydantic"] == "partial"
    assert any("same-project indexed evidence only" in r and "p:Pydantic" in r for r in reasons)


def test_corroboration_recorded_exclusion_or_no_support_is_contradicted():
    status, claims, reasons = _gate("Built a FastAPI service with async worker processing.", readme="FastAPI service.",
                                    indexed="FastAPI service.", forbidden={"async worker"})
    assert status == "contradicted" and claims["async worker"] == "contradicted"
    assert any(r.startswith("Claim(s) contradicted") for r in reasons)
    status, claims, _ = _gate("Built a pipeline with Polars.", readme="A pipeline in Python.", indexed="A pipeline in Python.")
    assert status == "contradicted" and claims["Polars"] == "contradicted"   # supported by nothing in the project


def test_corroboration_unavailable_fails_closed_only_for_strengthened_claims():
    status, claims, reasons = _gate("Built an event-driven backend with MQTT.", readme=None, indexed="MQTT backend")
    assert status == "unavailable" and set(claims.values()) == {"unavailable"}
    assert any(r.startswith("Strengthened claim(s) whose project repository cannot be checked") for r in reasons)
    _, _, verbatim = _gate("Event-driven backend with MQTT.", readme=None, indexed="MQTT backend", strengthened=False)
    assert not any("cannot be checked" in r for r in verbatim)


def test_a_rewrite_that_adds_a_recorded_exclusion_is_rejected():
    project = Project(id="proj", name="Project", description="", forbidden_extrapolations=["async worker"])
    evidence = [CapabilityEvidence(source=README, quote="FastAPI layer with structured errors.", project_id="proj", id="proj:1")]
    check = lambda text: validate_bullet_claim(text, project, evidence, CandidateProfile(id="c", name="S", graduation_year=2026)).rejection_reasons
    problems = iw.rewrite_problems("FastAPI layer with structured errors.", "Built a FastAPI layer with async worker processing.", POLICY, check)
    assert "grounding:forbidden_extrapolation:async worker" in problems


def test_summary_generic_items_are_narrowed_grammatically():
    summary = ("Graduate engineer with experience building backend systems, cloud applications and AI-powered platforms "
               "using Python. Skilled in REST APIs, distributed systems, databases and AI/ML applications.")
    narrowed, removed, _ = iw.narrow_summary(summary, "REST API backend", POLICY)
    assert narrowed == ("Graduate engineer with experience building backend systems and cloud applications using Python. "
                        "Skilled in REST APIs, databases and AI/ML applications.")
    assert removed == ["distributed systems", "AI-powered platforms"]


def test_language_claims_are_decided_by_the_repository_languages_record():
    bullets = [("p", "Developed C and Python socket utilities.", ["p:1"])]
    claims, per_bullet = iw.corroborate(bullets, POLICY, repo_text={"p": "Socket utilities."}, project_evidence_text={"p": ""},
                                        repo_languages={"p": {"python", "shell"}}, strengthened=set())
    statuses = {c["claim"]: c["status"] for c in claims}
    assert statuses["Python"] == "verified" and statuses["C"] == "contradicted"   # repo has no C at all
    assert per_bullet[0]["status"] == "contradicted"


def test_layer_and_validator_openings_become_action_first():
    assert compose("Thin FastAPI REST layer over the orchestrator.").rewritten_text == "Built a thin FastAPI REST layer over the orchestrator."
    assert compose("Deterministic evidence validator that checks quotes.").rewritten_text == (
        "Implemented a deterministic evidence validator that checks quotes.")
