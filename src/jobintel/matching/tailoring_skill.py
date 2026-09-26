"""CV Tailoring Skill Policy Engine & Decision Layer (Phase 3.7).

This module implements the decision-policy layer between the Evidence Bank
and CV generation, enforcing the strict lexical precedence order:
  evidence_truth > jd_relevance > evidence_strength > recruiter_clarity > non_redundancy > one_page_readability

It ensures:
- Project purpose taxonomy & role-archetype positioning
- Semantic preservation of metrics (e.g. latency != downtime)
- Time-gated evidence states preventing future evidence leakage
- Equal or unequal bullet budget allocations according to project importance
- Deterministic fallback when policy is disabled or unavailable
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

import yaml

from jobintel.models.candidate import CandidateProfile, CapabilityEvidence, Project
from jobintel.models.job import Job


class ProjectPurpose(str, Enum):
    """Functional engineering purpose for portfolio projects."""
    SYSTEMS_INFRASTRUCTURE = "systems_infrastructure"
    AI_AGENT_ARCHITECTURE = "ai_agent_architecture"
    DATA_INTELLIGENCE = "data_intelligence"
    FULLSTACK_APPLICATION = "fullstack_application"
    IOT_CLOUD_EVENT = "iot_cloud_event"
    EMBEDDED_FIRMWARE = "embedded_firmware"
    SIMULATION_MODELING = "simulation_modeling"
    AI_ML_RESEARCH = "ai_ml_research"


# Canonical mapping of project IDs to primary engineering purpose
PROJECT_PURPOSE_MAP: dict[str, ProjectPurpose] = {
    "personal-source-session-migration": ProjectPurpose.SYSTEMS_INFRASTRUCTURE,
    "personal-source-afra": ProjectPurpose.AI_AGENT_ARCHITECTURE,
    "personal-source-6": ProjectPurpose.DATA_INTELLIGENCE,
    "personal-source-3": ProjectPurpose.FULLSTACK_APPLICATION,
    "personal-source-iot-spill": ProjectPurpose.IOT_CLOUD_EVENT,
    "personal-source-stm32": ProjectPurpose.EMBEDDED_FIRMWARE,
    "personal-source-indy500": ProjectPurpose.SIMULATION_MODELING,
    "personal-source-2": ProjectPurpose.AI_ML_RESEARCH,
}

# Domain overlap groups for portfolio non-redundancy
FINANCIAL_DOMAIN_PROJECTS = {"personal-source-afra", "personal-source-6"}

# Alias dictionary to resolve common historical or narrative project names to canonical IDs
CANONICAL_PROJECT_ALIASES: dict[str, str] = {
    # Session Migration
    "session migration": "personal-source-session-migration",
    "criu migration": "personal-source-session-migration",
    "cross-machine session migration": "personal-source-session-migration",
    "criu-migration": "personal-source-session-migration",
    # AFRA
    "auditable financial research agent": "personal-source-afra",
    "auditable": "personal-source-afra",
    "financial research agent": "personal-source-afra",
    "financial agent": "personal-source-afra",
    "afra": "personal-source-afra",
    # FKIP
    "financial knowledge intelligence platform": "personal-source-6",
    "financial knowledge platform": "personal-source-6",
    "fkip": "personal-source-6",
    "financial retrieval": "personal-source-6",
    # MeetEat / Full-Stack
    "full-stack recommendation platform": "personal-source-3",
    "full-stack application": "personal-source-3",
    "full-stack": "personal-source-3",
    "meeteat": "personal-source-3",
    "meeteat social dining platform": "personal-source-3",
    # IoT Spill
    "edge-to-cloud iot": "personal-source-iot-spill",
    "iot platform": "personal-source-iot-spill",
    "iot event platform": "personal-source-iot-spill",
    "iot engineering platform": "personal-source-iot-spill",
    "edge-to-cloud smart spill detection": "personal-source-iot-spill",
    "smart spill detection": "personal-source-iot-spill",
    "smart spill": "personal-source-iot-spill",
    # STM32
    "stm32": "personal-source-stm32",
    "stm32 embedded fall detection": "personal-source-stm32",
    # Indy 500
    "indy500": "personal-source-indy500",
    "indy 500": "personal-source-indy500",
    "indy 500 decision support": "personal-source-indy500",
    "indy 500 point-in-time decision support": "personal-source-indy500",
    "indy": "personal-source-indy500",
    # DeepBreath
    "deepbreath": "personal-source-2",
    "respiratory ml": "personal-source-2",
    "deepbreath respiratory sound analysis": "personal-source-2",
}


def resolve_project_id(identifier: str) -> str:
    """Resolves a project ID or name/alias to its canonical project ID."""
    clean = identifier.strip().casefold()
    if clean in CANONICAL_PROJECT_ALIASES:
        return CANONICAL_PROJECT_ALIASES[clean]
    for alias, pid in CANONICAL_PROJECT_ALIASES.items():
        if alias in clean or clean in alias:
            return pid
    return identifier


@dataclass(frozen=True)
class TailoringSkillPolicy:
    """In-memory representation of CV_TAILORING_SKILL_V1."""
    schema_version: str
    source_asset: str
    source_hash: str
    transformation_version: str
    precedence: list[str]
    confidence: str
    evidence_policy: dict[str, Any]
    project_selection: dict[str, Any]
    project_positioning: dict[str, Any]
    bullet_selection: dict[str, Any]
    metric_policy: dict[str, Any]
    bullet_style: dict[str, Any]
    skills_policy: dict[str, Any]
    layout_policy: dict[str, Any]
    anti_patterns: list[str]
    human_review: dict[str, Any]

    @classmethod
    def load(cls, path: Path | str = "data/policies/cv_tailoring_skill_v1.yaml") -> TailoringSkillPolicy:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Policy file not found: {p}")
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        skill = raw.get("CV_TAILORING_SKILL_V1", {})
        return cls(
            schema_version=raw.get("schema_version", "1.0"),
            source_asset=raw.get("source_asset", ""),
            source_hash=raw.get("source_hash", ""),
            transformation_version=raw.get("transformation_version", "1.0"),
            precedence=skill.get("precedence", []),
            confidence=skill.get("confidence", "HIGH"),
            evidence_policy=skill.get("evidence_policy", {}),
            project_selection=skill.get("project_selection", {}),
            project_positioning=skill.get("project_positioning", {}),
            bullet_selection=skill.get("bullet_selection", {}),
            metric_policy=skill.get("metric_policy", {}),
            bullet_style=skill.get("bullet_style", {}),
            skills_policy=skill.get("skills_policy", {}),
            layout_policy=skill.get("layout_policy", {}),
            anti_patterns=skill.get("anti_patterns", []),
            human_review=skill.get("human_review", {}),
        )

    def verify_lexical_precedence(self) -> bool:
        """Verifies that the policy strictly adheres to the required lexical order."""
        expected = [
            "evidence_truth",
            "jd_relevance",
            "evidence_strength",
            "recruiter_clarity",
            "non_redundancy",
            "one_page_readability",
        ]
        return self.precedence == expected


def classify_role_archetype(job: Job) -> str:
    """Classifies the target role archetype based on job title, role tracks, description, and company."""
    title_lower = job.title.lower()
    desc_lower = (job.description or "").lower()
    company_lower = (job.company or "").lower()
    primary_track = job.role_track_profile.primary()
    track_name = primary_track.track.value if primary_track else ""

    # Motorsport
    if (
        "motorsport" in track_name
        or any(k in title_lower for k in ["motorsport", "racing", "f1", "powertrain", "simulation", "langham", "red bull"])
        or any(k in company_lower for k in ["mclaren", "red bull", "williams", "mercedes", "ferrari", "aston martin"])
        or any(k in desc_lower[:600] for k in ["motorsport", "racing", "powertrain", "race car", "engine control"])
    ):
        return "motorsport"

    # Platform / Cloud / Systems / HPC
    if (
        any(k in track_name for k in ["infrastructure", "platform", "devops", "cloud", "network"])
        or any(k in title_lower for k in ["hpc", "sre", "platform", "infrastructure", "systems", "linux", "cloud", "endpoint"])
    ):
        return "platform_cloud_systems"

    # AI / Applied AI
    if (
        "ai_ml" in track_name
        or any(k in title_lower for k in ["ai", "machine learning", "ml", "llm", "agent"])
    ):
        return "ai_applied_ai"

    # FinTech / Finance / Tax / Banking
    if (
        any(k in title_lower for k in ["tax", "fintech", "financial", "trading", "quant", "banking"])
        or any(k in company_lower for k in ["barclays", "morgan stanley", "kpmg", "goldman", "jpmorgan"])
        or any(k in desc_lower[:500] for k in ["tax technology", "fintech", "investment bank", "financial services", "corporate tax", "trading platform"])
    ):
        return "fintech"

    # Fullstack
    if (
        "full_stack" in track_name
        or any(k in title_lower for k in ["full-stack", "full stack", "fullstack", "web developer", "frontend"])
    ):
        return "fullstack"

    # Default: Backend / General SWE
    return "backend_general_swe"


def get_purpose_affinity(archetype: str, purpose: ProjectPurpose, job: Job | None = None) -> float:
    """Computes alignment score between target role archetype and project purpose.

    Affinity provides positive guidance for recruiter clarity and domain fit,
    preventing out-of-context projects from crowding out directly relevant engineering.
    """
    job_desc = (job.description if job and job.description else "").lower()
    job_title = (job.title if job else "").lower()

    if archetype == "motorsport":
        # Motorsport roles value simulation, embedded firmware, systems, and IoT/telemetry
        if "powertrain" in job_title or "powertrain" in job_desc:
            # Red Bull Powertrains: embedded firmware and systems take precedence
            if purpose == ProjectPurpose.EMBEDDED_FIRMWARE:
                return 4.5
            if purpose == ProjectPurpose.SYSTEMS_INFRASTRUCTURE:
                return 4.0
            if purpose == ProjectPurpose.SIMULATION_MODELING:
                return 3.0
            if purpose == ProjectPurpose.IOT_CLOUD_EVENT:
                return 2.5
            return 0.0  # AI agent & generic CRUD have 0 affinity for powertrain
        if purpose == ProjectPurpose.SIMULATION_MODELING:
            return 4.5
        if purpose == ProjectPurpose.SYSTEMS_INFRASTRUCTURE:
            return 3.8
        if purpose == ProjectPurpose.EMBEDDED_FIRMWARE:
            return 3.5
        if purpose == ProjectPurpose.IOT_CLOUD_EVENT:
            return 2.8
        return 0.2

    if archetype == "platform_cloud_systems":
        # Systems / HPC / Cloud / SRE value Linux systems, infrastructure, and event/cloud
        if purpose == ProjectPurpose.SYSTEMS_INFRASTRUCTURE:
            return 4.8
        if purpose == ProjectPurpose.IOT_CLOUD_EVENT:
            return 3.5
        if purpose == ProjectPurpose.AI_AGENT_ARCHITECTURE:
            # Repositioned as state machine / workflow orchestrator
            return 2.5
        if purpose == ProjectPurpose.SIMULATION_MODELING:
            return 2.2
        if purpose == ProjectPurpose.DATA_INTELLIGENCE:
            return 1.5
        return 0.5  # Fullstack CRUD downranked

    if archetype == "ai_applied_ai":
        # AI roles prioritize AI agent architecture, dense retrieval, and ML
        if purpose == ProjectPurpose.AI_AGENT_ARCHITECTURE:
            return 4.8
        if purpose == ProjectPurpose.DATA_INTELLIGENCE:
            return 3.8
        if purpose == ProjectPurpose.AI_ML_RESEARCH:
            return 3.0
        if purpose == ProjectPurpose.FULLSTACK_APPLICATION:
            return 1.8
        if purpose == ProjectPurpose.SYSTEMS_INFRASTRUCTURE:
            return 1.5
        return 0.5

    if archetype == "fintech":
        # FinTech / Tax / Banking values AI agents, financial search, fullstack, and systems
        is_tax = "tax" in job_title or "tax" in job_desc
        if is_tax:
            if purpose == ProjectPurpose.FULLSTACK_APPLICATION:
                return 4.8
            if purpose == ProjectPurpose.AI_AGENT_ARCHITECTURE:
                return 4.6
            if purpose == ProjectPurpose.IOT_CLOUD_EVENT:
                return 3.5
            if purpose == ProjectPurpose.SYSTEMS_INFRASTRUCTURE:
                return 3.0
            if purpose == ProjectPurpose.DATA_INTELLIGENCE:
                return 2.5
            return -3.0
        else:
            # General FinTech / Banking (Barclays, Morgan Stanley)
            if purpose == ProjectPurpose.AI_AGENT_ARCHITECTURE:
                return 4.8
            if purpose == ProjectPurpose.SYSTEMS_INFRASTRUCTURE:
                return 4.2
            if purpose == ProjectPurpose.FULLSTACK_APPLICATION:
                return 3.8
            if purpose == ProjectPurpose.IOT_CLOUD_EVENT:
                return 3.5
            if purpose == ProjectPurpose.DATA_INTELLIGENCE:
                return 2.8
            # Simulation (Indy 500) and STM32 have negative affinity for FinTech/Tax
            return -3.0

    if archetype == "fullstack":
        # Fullstack SWE values end-to-end product delivery, fullstack app, data retrieval
        if purpose == ProjectPurpose.FULLSTACK_APPLICATION:
            return 4.8
        if purpose == ProjectPurpose.DATA_INTELLIGENCE:
            return 3.5
        if purpose == ProjectPurpose.AI_AGENT_ARCHITECTURE:
            return 3.2
        if purpose == ProjectPurpose.IOT_CLOUD_EVENT:
            return 2.8
        if purpose == ProjectPurpose.SYSTEMS_INFRASTRUCTURE:
            return 2.0
        return 0.0

    # backend_general_swe (General Software Engineering Graduate)
    # Balanced software portfolio: systems, data intelligence, fullstack, IoT
    if purpose == ProjectPurpose.SYSTEMS_INFRASTRUCTURE:
        return 4.0
    if purpose == ProjectPurpose.AI_AGENT_ARCHITECTURE:
        return 3.8
    if purpose == ProjectPurpose.FULLSTACK_APPLICATION:
        return 3.5
    if purpose == ProjectPurpose.IOT_CLOUD_EVENT:
        return 3.2
    if purpose == ProjectPurpose.DATA_INTELLIGENCE:
        return 2.8
    # Indy 500 & STM32 receive negative affinity in general SWE to prevent "motorsport CV"
    return -2.0


def validate_tailored_claim(
    claim: str,
    project_id: str,
    as_of_phase: str | None = None,
    time_gate: str | None = None,
) -> tuple[bool, str]:
    """Validates that candidate bullet claims do not violate semantic boundaries,
    metric precision, time-gated evidence states, or ownership rules.
    """
    pid = resolve_project_id(project_id)
    text = claim.strip()
    text_lower = text.lower()

    effective_phase = as_of_phase or time_gate

    # 1. Session Migration rules
    if pid == "personal-source-session-migration":
        # Latency is not downtime
        if any(term in text_lower for term in ["downtime", "zero downtime", "zero-downtime", "service outage", "interruption-free"]):
            return False, "forbidden_rewrite: latency measurement cannot be rewritten as downtime"
        # Don't overclaim TCP continuity beyond observed four-tuple
        if any(term in text_lower for term in ["no second syn", "packet-level", "zero packet loss", "seamless packet stream", "packet level"]):
            return False, "forbidden_claim: claim cannot exceed measurement boundary (observed four-tuple continuity != packet-level seamlessness)"
        # Metric precision: docker 8/10, native criu 5/5, telefork strict 5/5
        if "100%" in text_lower and "docker" in text_lower:
            return False, "metric_error: Docker migration achieved 8/10, not 100%"

    # 2. Auditable Financial Research Agent rules
    if pid == "personal-source-afra":
        # Time-gate check
        is_pre_6_5 = effective_phase in ("pre_6.5", "6.0", "historical")
        has_post_benchmark = any(k in text_lower for k in ["126", "82%", "synthetic benchmark", "14-task", "14 task"])
        if is_pre_6_5 and has_post_benchmark:
            return False, "no_future_evidence_leakage: Phase 6.5 benchmark claims are forbidden before live experiment completion"

        # Post-6.5 scope qualifier requirement
        if has_post_benchmark:
            has_scope = any(k in text_lower for k in ["tested synthetic subset", "synthetic subset", "synthetic benchmark", "14-task benchmark", "14-task"])
            if not has_scope:
                return False, "missing_scope_qualifier: benchmark metrics require explicit synthetic evaluation scope"

        # Production generalization rejection
        if any(term in text_lower for term in ["eliminated all hallucinations", "production ready", "production-ready", "zero hallucinations in production"]):
            return False, "forbidden_generalization: synthetic benchmark does not prove universal elimination of hallucinations"

        # Antigravity CLI vs Gemini API title
        if "gemini api" in text_lower:
            return False, "stack_label_error: canonical live execution used Antigravity CLI, not direct Gemini API"

    # 3. IoT Engineering Platform rules
    if pid == "personal-source-iot-spill":
        if any(term in text_lower for term in ["exactly-once", "exactly once", "production-ready", "unlimited throughput"]):
            return False, "forbidden_claim: mechanisms beat service enumeration; exactly-once is unsupported"

    # 4. STM32 Fall Detection rules
    if pid == "personal-source-stm32":
        if "physical" in text_lower and "re-validated" in text_lower:
            return False, "validation_leakage: software-hardened version was not physically re-validated on hardware"

    # 5. Respiratory ML rules
    if pid == "personal-source-2":
        # Flattering metric without grouped patient evaluation caveat
        if ("94%" in text_lower or "~94%" in text_lower) and not any(k in text_lower for k in ["patient-independent", "grouped", "leakage"]):
            return False, "metric_error: ~94% accuracy without grouped patient-independent evaluation is invalid"

    # 6. Motorsport Society ownership rules
    if any(term in text_lower for term in ["designed vehicle", "built vehicle", "core vehicle engineer", "sole engineer"]):
        return False, "inflated_ownership: contribution must not be claimed as complete vehicle design/ownership"

    # 7. Slurm / HPC missing skills
    if any(term in text_lower for term in ["slurm", "parallel filesystem", "hpc cluster administration"]):
        return False, "forbidden_missing_skill: Slurm/HPC cluster administration is not evidenced"

    return True, "valid"


@dataclass(frozen=True)
class ProjectPolicyMatch:
    """Scored and ranked project match evaluated under the CV Tailoring Skill policy."""
    project: Project
    score: float
    purpose: ProjectPurpose
    matched_requirements: list[str]
    missing_requirements: list[str]
    purpose_affinity: float
    redundancy_penalty: float
    recommended_bullets: int
    explanation: str


class PolicyProjectSelector:
    """Selects and allocates project space according to CV_TAILORING_SKILL_V1."""

    def __init__(self, policy: TailoringSkillPolicy | None = None) -> None:
        self.policy = policy or TailoringSkillPolicy.load()

    def select_projects(
        self,
        job: Job,
        profile: CandidateProfile,
        as_of_phase: str | None = None,
        max_projects: int = 4,
    ) -> list[ProjectPolicyMatch]:
        """Selects top 3-4 projects enforcing lexical precedence:
          evidence_truth > jd_relevance > evidence_strength > recruiter_clarity > non_redundancy > one_page_readability
        """
        archetype = classify_role_archetype(job)
        required_names = {item.name.casefold() for item in job.skill_requirements if item.required}
        all_names = {item.name.casefold(): item for item in job.skill_requirements}
        primary_track = job.role_track_profile.primary()

        # Check explicit role exclusions
        excluded_pids: set[str] = set()
        job_desc = (job.description or "").lower()
        job_title = job.title.lower()

        # Powertrains role excludes Auditable Financial Research Agent
        if archetype == "motorsport" and ("powertrain" in job_title or "powertrain" in job_desc or "red bull" in job_title):
            excluded_pids.add("personal-source-afra")

        # Tax / FinTech SWE excludes Indy 500
        if archetype == "fintech" and ("tax" in job_title or "kpmg" in job_title or "tax" in job_desc or "barclays" in job.company.lower()):
            excluded_pids.add("personal-source-indy500")

        # General Graduate SWE excludes motorsport simulation unless requested
        if archetype == "backend_general_swe":
            if not any(k in job_desc for k in ["motorsport", "racing", "simulation", "embedded"]):
                excluded_pids.add("personal-source-indy500")

        candidate_evaluations: list[dict[str, Any]] = []

        for project in profile.projects:
            if project.id in excluded_pids:
                continue

            purpose = PROJECT_PURPOSE_MAP.get(project.id, ProjectPurpose.SYSTEMS_INFRASTRUCTURE)
            affinity = get_purpose_affinity(archetype, purpose, job)

            # Negative affinity indicates project is fundamentally unsuited to the role
            if affinity < -1.0:
                continue

            # Gather verified capability evidence for this project
            project_capabilities = [
                cap
                for cap in profile.capabilities.values()
                if any(ev.project_id == project.id for ev in cap.evidence)
            ]
            project_skill_names = {cap.name.casefold(): cap for cap in project_capabilities}

            # 1. evidence_truth & jd_relevance
            tier_weights = {"strong": 1.0, "partial": 0.5, "missing": 0.0}
            matched_requirements: list[str] = []
            skill_score = 0.0

            for name, req in all_names.items():
                cap = project_skill_names.get(name)
                if cap is None:
                    continue
                w = tier_weights.get(cap.evidence_tier, 0.0)
                if w <= 0.0:
                    continue
                matched_requirements.append(req.name)
                skill_score += w * (1.5 if req.required else 1.0)

            # Role track bonus
            role_track_bonus = 0.0
            if primary_track is not None and matched_requirements:
                role_track_bonus = project.role_relevance.get(primary_track.track, 0.0) * 3.0

            # Project must possess verified capability evidence or role-track relevance matching this job
            # Lexical precedence: evidence_truth is strictly primary; affinity cannot manufacture evidence
            if skill_score <= 0.0 and role_track_bonus <= 0.0:
                continue

            raw_score = skill_score + role_track_bonus + affinity

            candidate_evaluations.append({
                "project": project,
                "purpose": purpose,
                "raw_score": raw_score,
                "matched_requirements": matched_requirements,
                "affinity": affinity,
                "is_financial": project.id in FINANCIAL_DOMAIN_PROJECTS,
            })

        # Greedy selection enforcing purpose & domain non-redundancy
        selected_matches: list[ProjectPolicyMatch] = []
        covered_purposes: set[ProjectPurpose] = set()
        covered_skills: set[str] = set()
        financial_covered = False
        remaining = list(candidate_evaluations)

        while remaining and len(selected_matches) < max_projects:
            best_candidate = None
            best_eval_score = -1.0
            best_discount = 0.0

            for cand in remaining:
                current_score = cand["raw_score"]
                discount = 0.0

                if cand["purpose"] in covered_purposes:
                    discount += 0.3
                    current_score *= 0.7

                if cand["is_financial"] and financial_covered:
                    # In fintech roles, financial domain projects overlap heavily; outside fintech, it is a mild tie-breaker
                    domain_penalty = 0.5 if archetype == "fintech" else 0.15
                    discount += domain_penalty
                    current_score *= (1.0 - domain_penalty)

                if current_score > best_eval_score:
                    best_eval_score = current_score
                    best_candidate = cand
                    best_discount = discount

            if best_candidate is None or best_eval_score <= 0.0:
                break

            remaining.remove(best_candidate)
            proj = best_candidate["project"]
            purpose = best_candidate["purpose"]
            matched_reqs = best_candidate["matched_requirements"]

            covered_purposes.add(purpose)
            if best_candidate["is_financial"]:
                financial_covered = True

            for r in matched_reqs:
                covered_skills.add(r.casefold())

            missing_reqs = sorted(required_names - covered_skills)

            # Bullet budget allocation:
            # Flagship (first 2): 3-4 bullets
            # Supporting: 2-3 bullets
            if len(selected_matches) < 2:
                bullet_budget = 4 if best_eval_score > 3.0 else 3
            else:
                bullet_budget = 3 if best_eval_score > 2.0 else 2

            explanation = (
                f"Selected for {archetype} (purpose: {purpose.value}, affinity: +{best_candidate['affinity']:.1f}, "
                f"matched: {len(matched_reqs)} reqs, bullet budget: {bullet_budget})"
            )

            selected_matches.append(
                ProjectPolicyMatch(
                    project=proj,
                    score=round(best_eval_score, 3),
                    purpose=purpose,
                    matched_requirements=matched_reqs,
                    missing_requirements=missing_reqs,
                    purpose_affinity=best_candidate["affinity"],
                    redundancy_penalty=best_discount,
                    recommended_bullets=bullet_budget,
                    explanation=explanation,
                )
            )

        return selected_matches
