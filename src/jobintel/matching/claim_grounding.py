"""Claim-level grounding validator for rewritten CV bullets.

Every factual proposition in a rewritten bullet must be grounded in the specific
evidence belonging to that project. Models may improve grammar, concision, and
presentation, but may NEVER invent technologies, metrics, performance claims,
scale, ownership, outcomes, or borrow facts from other projects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from jobintel.analysis.role_tracks import SKILL_PATTERNS
from jobintel.models.candidate import CandidateProfile, CapabilityEvidence, Project
from jobintel.profile_ingestion import SKILL_CATEGORIES


# Technologies and tools tracked for grounding verification
_KNOWN_TECH = {
    # Languages
    "python", "java", "c", "c++", "c#", "go", "golang", "rust", "typescript", "javascript",
    "scala", "kotlin", "ruby", "php", "swift", "r", "julia", "matlab",
    # Frameworks & Libraries
    "react", "vue", "angular", "node", "node.js", "express", "fastapi", "django", "flask",
    "spring", "spring boot", "next.js", "nuxt", "svelte", "pytorch", "tensorflow", "keras",
    "scikit-learn", "pandas", "numpy", "polars",
    # Cloud & DevOps
    "aws", "amazon web services", "azure", "gcp", "google cloud", "docker", "kubernetes", "k8s",
    "terraform", "ansible", "ci/cd", "github actions", "gitlab ci", "jenkins", "linux", "unix",
    "helm", "prometheus", "grafana", "argo",
    # Databases & Storage
    "postgresql", "postgres", "mysql", "sqlite", "mongodb", "redis", "dynamodb", "cassandra",
    "elasticsearch", "opensearch", "kafka", "rabbitmq", "snowflake", "bigquery", "qdrant",
    # Architecture & Concepts
    "rest", "rest api", "graphql", "grpc", "microservices", "serverless", "lambda", "aws lambda",
    "rag", "vector search", "retrieval", "embeddings", "llm", "llms", "pydantic", "alembic", "playwright",
    # Systems / Embedded / Motorsport
    "hpc", "simulink", "can bus", "rtos", "cuda", "opencl", "mpi", "openmp", "criu", "mqtt",
    "arm", "cortex-m", "stm32", "i2c", "spi",
}

# Ownership / seniority claims that require explicit evidence
_OWNERSHIP_TERMS = {
    "architected", "architect", "architecting", "spearheaded", "spearheading",
    "led", "leading", "lead", "directed", "directing", "managed", "managing",
    "supervised", "supervising", "orchestrated", "orchestrating", "founded", "founding",
    "oversaw", "overseeing", "headed", "heading", "chief", "principal", "senior",
    "sole developer", "sole author", "head of",
}

# Performance claims that require explicit evidence
_PERFORMANCE_TERMS = {
    "high-performance", "high performance", "ultra-fast", "ultra fast",
    "ultra low-latency", "ultra-low latency", "low-latency", "low latency",
    "sub-second", "subsecond", "high-throughput", "high throughput",
    "zero-downtime", "zero downtime", "fault-tolerant", "fault tolerant",
    "highly available", "high availability", "high-availability",
    "ultra-reliable", "blazing fast", "ultra-responsive",
    "optimized for speed", "optimized for performance", "optimized performance",
}

# Scale & traffic claims that require explicit evidence
_SCALE_TERMS = {
    "scalable", "scalability", "elastic", "thousands of", "millions of", "billions of",
    "tens of thousands", "hundreds of thousands", "massive scale", "heavy traffic",
    "high traffic", "high-traffic", "high concurrency", "high-concurrency",
    "concurrent users", "concurrent requests", "at scale", "large-scale", "large scale",
    "production scale", "enterprise scale", "thousands", "millions", "billions",
}

# Outcome & business/impact claims that require explicit evidence
_OUTCOME_TERMS = {
    "reduced cost", "reduced costs", "saved $", "saved money",
    "increased revenue", "increased sales", "increased conversion", "boosted conversion",
    "slashed", "doubled", "tripled", "eliminated bottlenecks",
    "improved efficiency by", "reduced latency by", "cost savings",
}

# Architectural & enterprise scope claims that require explicit evidence
_SCOPE_TERMS = {
    "enterprise-grade", "enterprise grade", "production-grade", "production grade",
    "mission-critical", "mission critical", "distributed system", "distributed systems",
    "distributed architecture", "event-driven architecture", "microservices architecture",
    "multi-tenant", "multi-tenancy",
}

# Metric regex: matches percentages, multipliers, scale, latency, etc.
_METRIC_PATTERN = re.compile(
    r"\b(?:\d+(?:\.\d+)?%|\d+(?:\.\d+)?x|\d+(?:,\d+)*(?:\.\d+)?\s*(?:users?|reqs?|rps|ms|s|sec|seconds?|gb|mb|tb|k|m)\b)",
    re.I,
)

# Number regex: any standalone number with 2 or more digits, or with decimal/comma
_STANDALONE_NUMBER = re.compile(r"\b\d{2,}(?:,\d+)*(?:\.\d+)?\b|\b\d+\.\d+\b")

# Model training vs retrieval
_TRAINING_TERMS = re.compile(
    r"\b(trained|training|fine-tun(?:ed|ing)|pre-train(?:ed|ing)|hyperparameter tuning|model weights)\b",
    re.I,
)


@dataclass(frozen=True)
class BulletClaimValidationResult:
    is_valid: bool
    rejection_reasons: list[str]


def extract_tech_entities(text: str) -> set[str]:
    """Identify technology entities mentioned in text."""
    lowered = text.casefold()
    found = set()
    for tech in _KNOWN_TECH:
        pattern = r"(?<![\w+#])" + re.escape(tech) + r"(?![\w+#])"
        if re.search(pattern, lowered):
            found.add(tech)
    return found


def validate_bullet_claim(
    bullet_text: str,
    project: Project,
    cited_evidence: Sequence[CapabilityEvidence],
    candidate: CandidateProfile,
) -> BulletClaimValidationResult:
    """Validate that every factual claim in bullet_text is grounded in cited_evidence for project."""
    reasons: list[str] = []
    text = (bullet_text or "").strip()

    if not text:
        return BulletClaimValidationResult(False, ["empty_bullet_text"])

    if len(text) > 300:
        reasons.append("bullet_exceeds_max_length")

    if not cited_evidence:
        return BulletClaimValidationResult(False, ["missing_cited_evidence"])

    # 1. Project-isolation check: all cited evidence must strictly belong to project
    for ev in cited_evidence:
        if ev.project_id != project.id:
            reasons.append(f"cross_project_evidence_citation:{ev.id}_belongs_to_{ev.project_id}_not_{project.id}")

    # Combine all project evidence text
    evidence_quotes = [ev.quote for ev in cited_evidence if ev.project_id == project.id]
    all_evidence_text = " ".join(evidence_quotes)
    evidence_lower = all_evidence_text.casefold()

    # Capabilities linked to this project
    project_capabilities = {
        cap.name.casefold()
        for cap in candidate.capabilities.values()
        if any(ev.project_id == project.id for ev in cap.evidence)
    }

    # 2. Technology / Entity check
    bullet_tech = extract_tech_entities(text)
    evidence_tech = extract_tech_entities(all_evidence_text)
    project_desc_tech = extract_tech_entities(project.description)
    allowed_tech = evidence_tech | project_desc_tech | project_capabilities

    # Aliases
    aliases = {
        "postgres": "postgresql", "postgresql": "postgres",
        "k8s": "kubernetes", "kubernetes": "k8s",
        "node": "node.js", "node.js": "node",
        "golang": "go", "go": "golang",
        "rest": "rest api", "rest api": "rest",
        "llm": "llms", "llms": "llm",
    }

    for tech in bullet_tech:
        canonical_alias = aliases.get(tech, tech)
        if tech not in allowed_tech and canonical_alias not in allowed_tech:
            # Explicit technology boundaries
            if tech == "kubernetes" and ("docker" in allowed_tech and "kubernetes" not in allowed_tech):
                reasons.append("unsupported_technology_subsumption:docker_cannot_become_kubernetes")
            elif tech in {"azure", "gcp"} and ("aws" in allowed_tech and tech not in allowed_tech):
                reasons.append(f"unsupported_cloud_translation:aws_cannot_become_{tech}")
            elif tech in {"java", "c++", "go", "rust"} and ("python" in allowed_tech and tech not in allowed_tech):
                reasons.append(f"unsupported_language_translation:python_cannot_become_{tech}")
            else:
                reasons.append(f"unsupported_technology:{tech}")

    # 3. Metrics / Numbers / Scale check
    bullet_metrics = _METRIC_PATTERN.findall(text)
    for metric in bullet_metrics:
        m_clean = metric.strip().casefold()
        if m_clean not in evidence_lower:
            # Check if number part alone exists
            reasons.append(f"unsupported_metric_claim:{metric.strip()}")

    # Standalone numbers check (e.g. 50, 100, 1000)
    for num_match in _STANDALONE_NUMBER.finditer(text):
        num_str = num_match.group(0)
        # Allow year (2025, 2026) or version like 3.12 if in text
        if num_str in {"2024", "2025", "2026", "2027"}:
            continue
        if num_str not in all_evidence_text:
            reasons.append(f"unsupported_numeric_claim:{num_str}")

    # 4. Inflated Ownership / Seniority check
    bullet_lower = text.casefold()
    project_desc_lower = (project.description or "").casefold()
    allowed_context_lower = f"{evidence_lower} {project_desc_lower}"

    for term in _OWNERSHIP_TERMS:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, bullet_lower) and not re.search(pattern, allowed_context_lower):
            reasons.append(f"inflated_ownership_claim:{term}")

    # 4b. Unsupported Performance Claims
    for term in _PERFORMANCE_TERMS:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, bullet_lower) and not re.search(pattern, allowed_context_lower):
            reasons.append(f"unsupported_performance_claim:{term}")

    # 4c. Unsupported Scale / Traffic Claims
    for term in _SCALE_TERMS:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, bullet_lower) and not re.search(pattern, allowed_context_lower):
            reasons.append(f"unsupported_scale_claim:{term}")

    # 4d. Unsupported Outcome / Impact Claims
    for term in _OUTCOME_TERMS:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, bullet_lower) and not re.search(pattern, allowed_context_lower):
            reasons.append(f"unsupported_outcome_claim:{term}")

    # 4e. Unsupported Scope / Architecture Claims
    for term in _SCOPE_TERMS:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, bullet_lower) and not re.search(pattern, allowed_context_lower):
            reasons.append(f"unsupported_scope_claim:{term}")

    # 5. Domain / Scope check (e.g. RAG -> model training)
    if _TRAINING_TERMS.search(bullet_lower) and not _TRAINING_TERMS.search(evidence_lower):
        if any(rag_term in evidence_lower for rag_term in ("rag", "retrieval", "vector search", "embedding")):
            reasons.append("unsupported_domain_claim:retrieval_cannot_become_model_training")
        else:
            reasons.append("unsupported_domain_claim:unverified_model_training")

    # 6. Negated / Speculative source evidence check
    if re.search(r"\b(planned|todo|tutorial|learning|not completed)\b", evidence_lower):
        if not re.search(r"\b(planned|learning)\b", bullet_lower):
            reasons.append("unsupported_completion_of_speculative_evidence")

    # 7. Project-level and Evidence-level forbidden extrapolations
    all_forbidden = set(getattr(project, "forbidden_extrapolations", []) or [])
    for ev in cited_evidence:
        if ev.project_id == project.id and getattr(ev, "forbidden_extrapolations", None):
            all_forbidden.update(ev.forbidden_extrapolations)

    for forbidden in all_forbidden:
        if not forbidden:
            continue
        pattern = r"\b" + re.escape(forbidden.casefold()) + r"\b"
        if re.search(pattern, bullet_lower):
            reasons.append(f"forbidden_extrapolation:{forbidden}")

    # 8. CV Tailoring Skill policy validation (Phase 3.7)
    from jobintel.matching.tailoring_skill import validate_tailored_claim
    skill_valid, skill_reason = validate_tailored_claim(text, project.id)
    if not skill_valid:
        reasons.append(skill_reason)

    return BulletClaimValidationResult(is_valid=len(reasons) == 0, rejection_reasons=reasons)
