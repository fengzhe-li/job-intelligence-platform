from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jobintel.analysis.jd_requirements import extract_jd_requirements
from jobintel.analysis.job_enrichment import enrich_job
from jobintel.config import default_ranking_config
from jobintel.intelligence.grounding import GroundedRequirement
from jobintel.matching.claim_grounding import validate_bullet_claim
from jobintel.matching.cv_generation import generate_cv, generate_and_save_cv
from jobintel.matching.project_selection import (
    score_projects_for_job,
    score_projects_for_job_hybrid,
    select_top_projects_hybrid,
)
from jobintel.models.candidate import (
    CandidateProfile,
    Capability,
    CapabilityEvidence,
    Education,
    EvidenceSource,
    Experience,
    Project,
)
from jobintel.models.cv_artifact import GeneratedCVArtifact, ProjectBullet
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import EvidenceSourceType, ReviewGateStatus, SkillCategory
from jobintel.storage.cv_artifact_store import CVArtifactStore


# --- Test Fixtures ---

def _source(sid: str = "s1") -> EvidenceSource:
    return EvidenceSource(id=sid, source_type=EvidenceSourceType.README_MARKDOWN, title="Project Source", collected_at=date(2026, 1, 1))


def _job(description: str = "Python and REST API required.", title: str = "Software Engineer") -> Job:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    raw = Job(
        id="j-test-1",
        title=title,
        company="TechCorp",
        description=description,
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name="adzuna",
                source_job_id="j-test-1",
                original_url="https://example.com/jobs/1",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description=description,
                canonical_application_url="https://example.com/jobs/1/apply",
            )
        ],
    )
    return enrich_job(raw, 2026)


def _candidate() -> CandidateProfile:
    src1 = _source("s1")
    src2 = _source("s2")
    src3 = _source("s3")
    src4 = _source("s4")

    # Project 1: Backend API with FastAPI and Docker
    p1 = Project("p1", "API Platform", "Production backend service in Python.", [src1])
    ev_fastapi = CapabilityEvidence(src1, "Built REST API endpoints using FastAPI and Docker for JSON processing.", "p1", 1.0, "e1")
    ev_docker = CapabilityEvidence(src1, "Containerized backend application with Docker for local testing.", "p1", 1.0, "e2")

    # Project 2: Cloud Platform with AWS
    p2 = Project("p2", "Cloud Infra", "Cloud infrastructure on AWS.", [src2])
    ev_aws = CapabilityEvidence(src2, "Provisioned serverless resources on AWS using CloudFormation.", "p2", 1.0, "e3")

    # Project 3: Redundant Backend (overlaps with p1)
    p3 = Project("p3", "Secondary API", "Secondary backend API service.", [src3])
    ev_fastapi_2 = CapabilityEvidence(src3, "Developed small FastAPI endpoints for web service.", "p3", 0.8, "e4")

    # Project 4: AI Knowledge with RAG
    p4 = Project("p4", "Knowledge Search", "RAG pipeline for documents.", [src4])
    ev_rag = CapabilityEvidence(src4, "Implemented RAG retrieval pipeline with vector search in Python.", "p4", 1.0, "e5")

    capabilities = {
        "fastapi": Capability("FastAPI", SkillCategory.BACKEND, [ev_fastapi, ev_fastapi_2]),
        "docker": Capability("Docker", SkillCategory.DEVOPS, [ev_docker]),
        "aws": Capability("AWS", SkillCategory.CLOUD, [ev_aws]),
        "rag": Capability("RAG", SkillCategory.ML_AI, [ev_rag]),
        "python": Capability("Python", SkillCategory.LANGUAGE, [ev_fastapi, ev_rag]),
    }

    return CandidateProfile(
        id="c1",
        name="Fengzhe Li",
        graduation_year=2026,
        capabilities=capabilities,
        projects=[p1, p2, p3, p4],
        email="test@example.com",
        phone="07123456789",
    )


@dataclass
class FakeMetadata:
    used: str = "gemini_assisted"
    fallback_reason: str | None = None


@dataclass
class FakeHybridResult:
    requirements: list[GroundedRequirement] = field(default_factory=list)
    semantic_matches: list[dict] = field(default_factory=list)
    metadata: FakeMetadata = field(default_factory=FakeMetadata)


# ==============================================================================
# 1. PROJECT SELECTION TESTS
# ==============================================================================

def test_project_selection_nonexistent_project_rejected():
    cand = _candidate()
    job = _job()
    # Hybrid match references a ghost project 'p999'
    fake_hybrid = FakeHybridResult(
        requirements=[GroundedRequirement("r1", "rest api", "REST API required.", 0, 18, "skill", "required")],
        semantic_matches=[
            {"project_id": "p999", "requirement_id": "r1", "evidence_id": "e1", "strength": "strong"}
        ],
    )
    # Must fail closed to deterministic selection
    matches = score_projects_for_job_hybrid(job, cand, fake_hybrid)
    det_matches = score_projects_for_job(job, cand)
    assert [m.project.id for m in matches] == [m.project.id for m in det_matches]


def test_project_selection_nonexistent_evidence_rejected():
    cand = _candidate()
    job = _job()
    # References nonexistent evidence 'e999'
    fake_hybrid = FakeHybridResult(
        requirements=[GroundedRequirement("r1", "rest api", "REST API required.", 0, 18, "skill", "required")],
        semantic_matches=[
            {"project_id": "p1", "requirement_id": "r1", "evidence_id": "e999", "strength": "strong"}
        ],
    )
    matches = score_projects_for_job_hybrid(job, cand, fake_hybrid)
    det_matches = score_projects_for_job(job, cand)
    assert [m.project.id for m in matches] == [m.project.id for m in det_matches]


def test_project_selection_cross_project_evidence_rejected():
    cand = _candidate()
    job = _job()
    # Project p1 claims evidence e3 (which actually belongs to p2)
    fake_hybrid = FakeHybridResult(
        requirements=[GroundedRequirement("r1", "rest api", "REST API required.", 0, 18, "skill", "required")],
        semantic_matches=[
            {"project_id": "p1", "requirement_id": "r1", "evidence_id": "e3", "strength": "strong"}
        ],
    )
    matches = score_projects_for_job_hybrid(job, cand, fake_hybrid)
    det_matches = score_projects_for_job(job, cand)
    assert [m.project.id for m in matches] == [m.project.id for m in det_matches]


def test_project_selection_semantic_relevance_improves_selection():
    cand = _candidate()
    # Job requires REST API (which candidate has via FastAPI in p1)
    job = _job("REST API required.")
    fake_hybrid = FakeHybridResult(
        requirements=[GroundedRequirement("r1", "rest api", "REST API required.", 0, 18, "skill", "required")],
        semantic_matches=[
            {"project_id": "p1", "requirement_id": "r1", "evidence_id": "e1", "strength": "partial"}
        ],
    )
    matches = score_projects_for_job_hybrid(job, cand, fake_hybrid)
    p1_match = next(m for m in matches if m.project.id == "p1")
    assert "rest api" in [r.casefold() for r in p1_match.matched_requirements]
    assert p1_match.score > 0


def test_project_selection_redundant_weaker_project_loses_to_complementary():
    cand = _candidate()
    # Job asks for Python and AWS
    job = _job("Python and AWS required.")
    # p1 has Python + FastAPI + Docker
    # p2 has AWS (complementary cloud skills)
    # p3 has FastAPI (not in JD)
    # p4 has Python (already covered by p1)
    selected, explanations, mode = select_top_projects_hybrid(job, cand, max_projects=2)
    selected_ids = [m.project.id for m in selected]
    # p1 (Python) and p2 (AWS) should be selected
    assert "p1" in selected_ids
    assert "p2" in selected_ids
    assert "p3" not in selected_ids


def test_project_selection_deterministic_fallback_when_unassisted():
    cand = _candidate()
    job = _job()
    fake_hybrid = FakeHybridResult(metadata=FakeMetadata(used="deterministic_fallback"))
    selected, explanations, mode = select_top_projects_hybrid(job, cand, fake_hybrid)
    assert mode == "deterministic"


# ==============================================================================
# 2. CLAIM-LEVEL BULLET GROUNDING TESTS
# ==============================================================================

def test_bullet_claim_invented_metric_rejected():
    cand = _candidate()
    proj = cand.projects[0]
    ev = cand.capabilities["fastapi"].evidence[0]
    # Invented "40% latency reduction" and "10,000 users"
    res = validate_bullet_claim("Built FastAPI endpoints with 40% latency reduction for 10k users.", proj, [ev], cand)
    assert not res.is_valid
    assert any("unsupported_metric_claim" in r for r in res.rejection_reasons)


def test_bullet_claim_invented_technology_rejected():
    cand = _candidate()
    proj = cand.projects[0]
    ev = cand.capabilities["fastapi"].evidence[0]
    # Docker -> Kubernetes
    res_k8s = validate_bullet_claim("Built FastAPI endpoints deployed on Kubernetes clusters.", proj, [ev], cand)
    assert not res_k8s.is_valid
    assert any("docker_cannot_become_kubernetes" in r or "unsupported_technology:kubernetes" in r for r in res_k8s.rejection_reasons)

    # AWS -> Azure
    proj2 = cand.projects[1]
    ev_aws = cand.capabilities["aws"].evidence[0]
    res_azure = validate_bullet_claim("Provisioned serverless resources on Azure.", proj2, [ev_aws], cand)
    assert not res_azure.is_valid
    assert any("aws_cannot_become_azure" in r or "unsupported_technology:azure" in r for r in res_azure.rejection_reasons)

    # Python -> Java/C++
    res_java = validate_bullet_claim("Developed microservices using Java and Spring Boot.", proj, [ev], cand)
    assert not res_java.is_valid
    assert any("python_cannot_become_java" in r or "unsupported_technology:java" in r for r in res_java.rejection_reasons)


def test_bullet_claim_rag_cannot_become_ml_training():
    cand = _candidate()
    proj = cand.projects[3]  # p4: RAG
    ev = cand.capabilities["rag"].evidence[0]
    # Converting RAG into training custom models
    res = validate_bullet_claim("Trained and fine-tuned custom deep learning models for document retrieval.", proj, [ev], cand)
    assert not res.is_valid
    assert any("retrieval_cannot_become_model_training" in r for r in res.rejection_reasons)


def test_bullet_claim_inflated_ownership_rejected():
    cand = _candidate()
    proj = cand.projects[0]
    ev = cand.capabilities["fastapi"].evidence[0]
    res = validate_bullet_claim("Led the engineering team as principal architect for FastAPI endpoints.", proj, [ev], cand)
    assert not res.is_valid
    assert any("inflated_ownership_claim:led" in r for r in res.rejection_reasons)


def test_bullet_claim_cross_project_evidence_rejected():
    cand = _candidate()
    proj = cand.projects[0]  # p1
    ev_p2 = cand.capabilities["aws"].evidence[0]  # belongs to p2
    res = validate_bullet_claim("Built cloud services on AWS.", proj, [ev_p2], cand)
    assert not res.is_valid
    assert any("cross_project_evidence_citation" in r for r in res.rejection_reasons)


def test_bullet_claim_valid_paraphrase_accepted():
    cand = _candidate()
    proj = cand.projects[0]
    ev = cand.capabilities["fastapi"].evidence[0]
    # Truthful concise phrasing preserving exact facts
    res = validate_bullet_claim("Developed REST API endpoints with FastAPI and Docker for JSON request processing.", proj, [ev], cand)
    assert res.is_valid
    assert len(res.rejection_reasons) == 0


# ==============================================================================
# 3. CV PIPELINE & INVARIANT TESTS
# ==============================================================================

def test_cv_generation_deterministic_fallback_when_gemini_fails(tmp_path):
    cand = _candidate()
    job = _job()
    mock_service = MagicMock()
    mock_service.rewrite_bullets.side_effect = TimeoutError("Simulated Gemini timeout")

    fake_hybrid = FakeHybridResult(metadata=FakeMetadata(used="gemini_assisted"))
    # CV generation should not crash; it safely falls back to deterministic extractive bullets
    artifact = generate_cv(cand, job, store_root=tmp_path, hybrid_result=fake_hybrid, intelligence_service=mock_service)
    assert artifact.generation_mode == "deterministic"
    assert len(artifact.bullets) > 0
    assert all(b.status == "extractive" for b in artifact.bullets)
    assert artifact.fits_one_page


def test_cv_generation_rejected_bullet_falls_back_to_extractive(tmp_path):
    cand = _candidate()
    job = _job()
    mock_service = MagicMock()
    # Mock service returns a rejected bullet suggestion (e.g. invented metric)
    mock_service.rewrite_bullets.return_value = (
        [],  # accepted
        [{"project_id": "p1", "text": "Invented 50% speedup", "rejection_reasons": ["unsupported_metric_claim:50%"]}],
        "gemini_assisted"
    )

    fake_hybrid = FakeHybridResult(metadata=FakeMetadata(used="gemini_assisted"))
    artifact = generate_cv(cand, job, store_root=tmp_path, hybrid_result=fake_hybrid, intelligence_service=mock_service)
    # The artifact records the rejected suggestion in provenance and falls back to extractive bullets
    assert len(artifact.rejected_bullet_suggestions) == 1
    assert artifact.rejected_bullet_suggestions[0]["text"] == "Invented 50% speedup"
    assert all(b.status == "extractive" for b in artifact.bullets)


def test_cv_generation_accepted_rewrite_used_with_provenance(tmp_path):
    cand = _candidate()
    job = _job()
    mock_service = MagicMock()
    mock_service.rewrite_bullets.return_value = (
        [{"project_id": "p1", "evidence_ids": ["e1"], "text": "Developed REST API endpoints with FastAPI and Docker for JSON processing."}],
        [],
        "gemini_assisted"
    )

    fake_hybrid = FakeHybridResult(metadata=FakeMetadata(used="gemini_assisted"))
    artifact = generate_cv(cand, job, store_root=tmp_path, hybrid_result=fake_hybrid, intelligence_service=mock_service)
    assert artifact.generation_mode in ("gemini_assisted", "gemini_with_fallback")
    rewritten_bullets = [b for b in artifact.bullets if b.status == "gemini_rewritten"]
    assert len(rewritten_bullets) >= 1
    assert "FastAPI and Docker" in rewritten_bullets[0].text


def test_cv_generation_one_page_pdf_invariant_preserved(tmp_path):
    cand = _candidate()
    job = _job("Extensive requirements for senior engineer with 10 years experience.")
    artifact = generate_cv(cand, job, store_root=tmp_path)
    assert artifact.page_count == 1
    assert artifact.fits_one_page
    assert Path(artifact.pdf_path).exists()


def test_cv_store_persistence_immutable_with_provenance(tmp_path):
    cand = _candidate()
    job = _job()
    store = CVArtifactStore(tmp_path)
    artifact = generate_and_save_cv(cand, job, cv_store=store, store_root=tmp_path)
    loaded = store.get(artifact.id)
    assert loaded is not None
    assert loaded.id == artifact.id
    assert loaded.generation_mode == artifact.generation_mode
    assert loaded.pdf_sha256 == artifact.pdf_sha256


# ==============================================================================
# 4. ADDITIONAL CONSTRAINTS TESTS: SEMANTIC ENTAILMENT & SUBORDINATE DIVERSITY
# ==============================================================================

def test_bullet_claim_semantic_factual_entailment_counterexample():
    """Exact user counterexample:
    Evidence: 'Built REST APIs with FastAPI and PostgreSQL'
    Bullet: 'Architected a scalable high-performance backend serving thousands of requests'
    Must be rejected for unsupported ownership, performance, scale claims.
    """
    cand = _candidate()
    proj = cand.projects[0]
    ev = CapabilityEvidence(
        _source("s1"),
        "Built REST APIs with FastAPI and PostgreSQL",
        proj.id,
        1.0,
        "e1",
    )
    bullet = "Architected a scalable high-performance backend serving thousands of requests"
    res = validate_bullet_claim(bullet, proj, [ev], cand)
    assert not res.is_valid
    assert any("architected" in r for r in res.rejection_reasons)
    assert any("scalable" in r for r in res.rejection_reasons)
    assert any("high-performance" in r for r in res.rejection_reasons)
    assert any("thousands" in r for r in res.rejection_reasons)


def test_bullet_claim_unsupported_qualifiers_and_outcomes_rejected():
    """Unsupported enterprise qualifiers and business outcomes must be rejected."""
    cand = _candidate()
    proj = cand.projects[0]
    ev = CapabilityEvidence(
        _source("s1"),
        "Implemented data processing service in Python",
        proj.id,
        1.0,
        "e1",
    )
    # Enterprise scope inflation
    res_scope = validate_bullet_claim("Engineered an enterprise-grade fault-tolerant distributed system in Python.", proj, [ev], cand)
    assert not res_scope.is_valid
    assert any("enterprise-grade" in r or "fault-tolerant" in r or "distributed system" in r for r in res_scope.rejection_reasons)

    # Business outcome inflation
    res_outcome = validate_bullet_claim("Developed Python service, reducing operating costs by 30% and eliminating bottlenecks.", proj, [ev], cand)
    assert not res_outcome.is_valid
    assert any("reduced cost" in r or "eliminated bottlenecks" in r or "30%" in r for r in res_outcome.rejection_reasons)


def test_project_selection_materially_stronger_jd_evidence_never_loses_to_diversity():
    """Constraint 2: Materially stronger JD evidence must never lose to a weaker project merely for variety."""
    cand = _candidate()
    # Job requires Python, FastAPI, and Docker (all required)
    job = _job("Required: Python, FastAPI, Docker. Preferred: AWS.")
    # p1 matches Python, FastAPI, Docker (score = 1.5 * 3 = 4.5)
    # p2 matches AWS (score = 1.0 * 1 = 1.0)
    # p3 matches FastAPI (score = 1.5, but FastAPI already covered by p1)
    # If max_projects is 1, p1 must strictly win
    selected_1, _, _ = select_top_projects_hybrid(job, cand, max_projects=1)
    assert len(selected_1) == 1
    assert selected_1[0].project.id == "p1"

    # Even with redundancy penalty, a materially stronger project (e.g. 4.5 vs 1.0) dominates
    selected_2, _, _ = select_top_projects_hybrid(job, cand, max_projects=2)
    selected_ids = [m.project.id for m in selected_2]
    assert "p1" in selected_ids
    assert "p2" in selected_ids


def test_project_selection_drops_zero_score_projects():
    """Constraint 2: Do not force artificial diversity. Projects with zero JD relevance are not selected."""
    cand = _candidate()
    # Job requires only Rust (candidate has no Rust evidence)
    job = _job("Required: Rust.")
    selected, _, _ = select_top_projects_hybrid(job, cand)
    assert selected == []
