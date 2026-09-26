"""Historical Regression Test Harness for CV Tailoring Skill (Phase 3.7).

This module implements the regression harness for CV_TAILORING_SKILL_V1,
validating all 28 historical regression cases (RT-01 through RT-28),
the 4 release-blocking pairs, cryptographic provenance of policy assets,
and strict production code isolation.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobintel.matching.claim_grounding import validate_bullet_claim
from jobintel.matching.policy_selector import (
    CANONICAL_PROJECT_ALIASES,
    PROJECT_PURPOSE_MAP,
    PolicyProjectSelector,
    ProjectPurpose,
    TailoringSkillPolicy,
    classify_role_archetype,
    resolve_project_id,
    validate_tailored_claim,
)
from jobintel.models.candidate import CandidateProfile, CapabilityEvidence, Project
from jobintel.models.job import Job, RoleTrackProfile, RoleTrackScore, SkillRequirement
from jobintel.models.taxonomy import RoleTrack
from jobintel.profile_ingestion import load_candidate_profile


FIXTURE_PATH = Path("tests/fixtures/cv_tailoring_regression_v1.yaml")
POLICY_PATH = Path("data/policies/cv_tailoring_skill_v1.yaml")
SOURCE_POLICY_PATH = Path("data/policies/sources/cv_tailoring_skill_v1_source.md")
SOURCE_REGRESSION_PATH = Path("tests/fixtures/sources/cv_tailoring_regression_v1_source.md")


def _load_regression_fixture() -> dict[str, Any]:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _make_mock_job(
    title: str,
    company: str,
    skills: list[str],
    primary_track_enum: RoleTrack,
    description: str = "",
) -> Job:
    track_profile = RoleTrackProfile(
        [RoleTrackScore(track=primary_track_enum, score=0.8, evidence=[f"title:{title}"])]
    )
    skill_reqs = [SkillRequirement(name=s, required=True) for s in skills]
    return Job(
        id=f"mock-{company.lower().replace(' ', '-')}",
        title=title,
        company=company,
        description=description or f"{title} at {company}. Skills: {', '.join(skills)}.",
        locations=[],
        source_observations=[],
        skill_requirements=skill_reqs,
        role_track_profile=track_profile,
    )


# =====================================================================
# 1. Cryptographic Provenance & Production Code Isolation Invariants
# =====================================================================

class TestProvenanceAndIntegrity:
    """Verifies cryptographic provenance and strict production code isolation."""

    def test_policy_asset_cryptographic_provenance(self) -> None:
        """The canonical YAML policy must match the hash of its authoritative markdown source."""
        assert POLICY_PATH.is_file(), f"Policy file missing: {POLICY_PATH}"
        if not SOURCE_POLICY_PATH.is_file():
            pytest.skip(f"Private source asset not present in this checkout: {SOURCE_POLICY_PATH}")

        policy = TailoringSkillPolicy.load(POLICY_PATH)
        assert policy.source_asset == str(SOURCE_POLICY_PATH)

        expected_hash = hashlib.sha256(SOURCE_POLICY_PATH.read_bytes()).hexdigest()
        assert policy.source_hash == expected_hash, "Policy source hash mismatch"
        assert policy.verify_lexical_precedence(), "Lexical precedence order violated in policy"

    def test_regression_fixture_cryptographic_provenance(self) -> None:
        """The regression fixture must match the hash of its authoritative markdown source."""
        assert FIXTURE_PATH.is_file(), f"Fixture file missing: {FIXTURE_PATH}"
        if not SOURCE_REGRESSION_PATH.is_file():
            pytest.skip(f"Private source asset not present in this checkout: {SOURCE_REGRESSION_PATH}")

        fixture = _load_regression_fixture()
        assert fixture["source_asset"] == str(SOURCE_REGRESSION_PATH)

        expected_hash = hashlib.sha256(SOURCE_REGRESSION_PATH.read_bytes()).hexdigest()
        assert fixture["source_hash"] == expected_hash, "Regression fixture source hash mismatch"

        cases = fixture.get("cases", [])
        assert len(cases) == 28, f"Expected 28 regression cases, got {len(cases)}"
        expected_ids = [f"RT-{i:02d}" for i in range(1, 29)]
        actual_ids = [c["id"] for c in cases]
        assert actual_ids == expected_ids, "Regression case IDs must be RT-01 through RT-28 in exact order"

    def test_production_code_never_imports_or_references_regression_fixtures(self) -> None:
        """Production code in src/jobintel must never import or reference test fixtures or case IDs."""
        src_dir = Path("src/jobintel")
        assert src_dir.is_dir()

        forbidden_patterns = [
            re.compile(r"cv_tailoring_regression"),
            re.compile(r"\bRT-\d{2}\b"),
        ]

        violations = []
        for py_path in src_dir.rglob("*.py"):
            text = py_path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                matches = pattern.findall(text)
                if matches:
                    violations.append(f"{py_path}: matched {pattern.pattern} ({matches[:3]})")

        assert not violations, f"Production code references regression fixtures or case IDs: {violations}"


# =====================================================================
# 2. Release-Blocking Pairs (Mandatory Quality Gate)
# =====================================================================

class TestReleaseBlockingPairs:
    """The 4 release-blocking pairs test core evidence-selection decisions."""

    @pytest.fixture
    def profile(self) -> CandidateProfile:
        prof = load_candidate_profile("data/local")
        if prof is None:
            pytest.skip("Requires the private local candidate profile in data/local (git-ignored)")
        return prof

    @pytest.fixture
    def selector(self) -> PolicyProjectSelector:
        return PolicyProjectSelector()

    def test_release_blocking_pair_1_mclaren_vs_kpmg(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        """McLaren vs KPMG: Same portfolio must produce materially different project ordering.

        - McLaren HPC: Systems role -> Session Migration #1, Indy 500 in top 4, Full-Stack downranked.
        - KPMG Tax SWE: Product/enterprise SWE -> Full-Stack / AFRA #1/2, Indy 500 strictly excluded.
        """
        mclaren_job = _make_mock_job(
            title="Junior HPC Systems Engineer",
            company="McLaren Racing",
            skills=["Linux", "C", "Networking", "Python"],
            primary_track_enum=RoleTrack.INFRASTRUCTURE_ENGINEERING,
            description="HPC Linux systems, cluster automation, networking, container performance",
        )
        kpmg_job = _make_mock_job(
            title="Software Engineering in Tax",
            company="KPMG",
            skills=["Python", "SQL", "FastAPI", "React"],
            primary_track_enum=RoleTrack.SOFTWARE_ENGINEERING,
            description="Full stack web development, tax enterprise systems, backend APIs, database",
        )

        mclaren_matches = selector.select_projects(mclaren_job, profile)
        kpmg_matches = selector.select_projects(kpmg_job, profile)

        mclaren_pids = [m.project.id for m in mclaren_matches]
        kpmg_pids = [m.project.id for m in kpmg_matches]

        # McLaren must prioritize Session Migration as #1
        assert mclaren_pids[0] == "personal-source-session-migration", (
            f"McLaren #1 should be Session Migration, got {mclaren_pids[0]}"
        )
        # McLaren must include Indy 500 in top 4 for modeling/simulation relevance
        assert "personal-source-indy500" in mclaren_pids, "McLaren should include Indy 500"

        # KPMG must strictly exclude Indy 500 (irrelevant domain to tax software)
        assert "personal-source-indy500" not in kpmg_pids, "KPMG must exclude Indy 500"
        # KPMG must place Full-Stack (personal-source-3) or AFRA in top 2
        assert kpmg_pids[0] in ("personal-source-3", "personal-source-afra")
        assert kpmg_pids[0] != mclaren_pids[0], "McLaren and KPMG must not share identical #1 project"

    def test_release_blocking_pair_2_afra_pre_vs_post_phase_6_5(self) -> None:
        """Auditable before vs after Phase 6.5: Evidence timestamp changes permissible claims.

        - Pre-Phase 6.5: Cannot claim completed real-model benchmark or 126 executions.
        - Post-Phase 6.5: May cite 126 executions and 82% -> 0% benchmark with synthetic qualifier,
          but still rejects generalized claims of zero hallucinations.
        """
        # Pre-Phase 6.5 state (RT-17)
        pre_valid, pre_reason = validate_tailored_claim(
            claim="Evaluated across 126 test executions, reducing unsupported claims from 82% to 0%.",
            project_id="personal-source-afra",
            as_of_phase="pre_6.5",
        )
        assert not pre_valid, "Pre-Phase 6.5 must reject Phase 6.5 benchmark results"
        assert "no_future_evidence_leakage" in pre_reason

        # Post-Phase 6.5 state with valid scope qualifier (RT-18)
        post_valid, post_reason = validate_tailored_claim(
            claim="Evaluated across 126 test executions in tested synthetic subset, reducing unsupported claims from 82% to 0%.",
            project_id="personal-source-afra",
            as_of_phase="6.5",
        )
        assert post_valid, f"Post-Phase 6.5 with scope qualifier should be accepted: {post_reason}"

        # Post-Phase 6.5 with unsupported generalization (RT-18)
        gen_valid, gen_reason = validate_tailored_claim(
            claim="Eliminated all hallucinations in production-ready financial research agent.",
            project_id="personal-source-afra",
            as_of_phase="6.5",
        )
        assert not gen_valid, "Must reject universal elimination of hallucinations"
        assert "forbidden_generalization" in gen_reason

    def test_release_blocking_pair_3_session_tcp_observation_vs_packet_claim(self) -> None:
        """Session TCP observation vs packet-level claim: Claim cannot exceed measurement boundary.

        - 'observed TCP four-tuple continuity': ACCEPTED.
        - 'seamless packet-level continuity with no second SYN': REJECTED.
        - 'latency' rewritten as 'downtime': REJECTED.
        """
        # Valid observation claim
        valid_obs, _ = validate_tailored_claim(
            claim="Observed TCP four-tuple and session identity continuity across live container migration.",
            project_id="personal-source-session-migration",
        )
        assert valid_obs, "Truthful TCP four-tuple observation claim must be accepted"

        # Overclaimed packet-level continuity
        over_valid, over_reason = validate_tailored_claim(
            claim="Achieved seamless packet-level continuity with no second SYN during live migration.",
            project_id="personal-source-session-migration",
        )
        assert not over_valid, "Packet-level continuity / no second SYN claim must be rejected"
        assert "forbidden_claim" in over_reason

        # Downtime relabeling
        dt_valid, dt_reason = validate_tailored_claim(
            claim="Reduced server downtime to 300ms during live migration.",
            project_id="personal-source-session-migration",
        )
        assert not dt_valid, "Rewriting latency as downtime must be rejected"
        assert "forbidden_rewrite" in dt_reason

    def test_release_blocking_pair_4_red_bull_pu_vs_barclays_kpmg(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        """Red Bull PU vs Barclays/KPMG: Flagship project in one role disappears in another.

        - Red Bull Powertrains: Motorsport engineering -> restores STM32, excludes AFRA.
        - Barclays / KPMG: FinTech -> includes AFRA, excludes STM32.
        """
        rb_job = _make_mock_job(
            title="Powertrains Embedded Systems Engineer",
            company="Red Bull Powertrains",
            skills=["C", "C++", "Embedded", "Linux"],
            primary_track_enum=RoleTrack.MOTORSPORT_ENGINEERING,
            description="Powertrain engine control unit, embedded firmware, high performance telemetry",
        )
        barclays_job = _make_mock_job(
            title="Technology Developer Graduate",
            company="Barclays",
            skills=["Python", "SQL", "FastAPI", "Linux"],
            primary_track_enum=RoleTrack.SOFTWARE_ENGINEERING,
            description="Enterprise backend software developer graduate program, trading platform",
        )

        rb_matches = selector.select_projects(rb_job, profile)
        barclays_matches = selector.select_projects(barclays_job, profile)

        rb_pids = [m.project.id for m in rb_matches]
        barclays_pids = [m.project.id for m in barclays_matches]

        # Red Bull must strictly exclude AFRA (financial agent irrelevant to powertrain ECU)
        assert "personal-source-afra" not in rb_pids, "Red Bull Powertrains must exclude AFRA"
        # Red Bull must include STM32 Embedded Fall Detection
        assert "personal-source-stm32" in rb_pids, "Red Bull Powertrains must include STM32"

        # Barclays must include AFRA
        assert "personal-source-afra" in barclays_pids, "Barclays must include AFRA"
        # Barclays must not include STM32
        assert "personal-source-stm32" not in barclays_pids, "Barclays should not include STM32"


# =====================================================================
# 3. Comprehensive Harness for All 28 Cases (RT-01 to RT-28)
# =====================================================================

class TestAllHistoricalRegressionCases:
    """Verifies that all 28 cases from CV_TAILORING_REGRESSION_V1 pass their policy assertions."""

    @pytest.fixture
    def fixture_data(self) -> dict[str, Any]:
        return _load_regression_fixture()

    @pytest.fixture
    def profile(self) -> CandidateProfile:
        prof = load_candidate_profile("data/local")
        if prof is None:
            pytest.skip("Requires the private local candidate profile in data/local (git-ignored)")
        return prof

    @pytest.fixture
    def selector(self) -> PolicyProjectSelector:
        return PolicyProjectSelector()

    def test_rt01_morgan_stanley_general_swe_not_motorsport(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "2027 Technology Full Time Analyst",
            "Morgan Stanley",
            ["Python", "SQL", "Linux", "REST"],
            RoleTrack.SOFTWARE_ENGINEERING,
            "General software technology graduate role, enterprise backend systems",
        )
        matches = selector.select_projects(job, profile)
        pids = [m.project.id for m in matches]
        assert "personal-source-indy500" not in pids, "Indy 500 must not appear in general Morgan Stanley SWE CV"
        assert "personal-source-stm32" not in pids, "STM32 must not appear in general Morgan Stanley SWE CV"

    def test_rt02_morgan_stanley_flagship_bullet_budget(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "2027 Technology Full Time Analyst",
            "Morgan Stanley",
            ["Python", "SQL", "Linux", "REST"],
            RoleTrack.SOFTWARE_ENGINEERING,
        )
        matches = selector.select_projects(job, profile)
        # Flagships (first 2) receive 3-4 bullets
        assert matches[0].recommended_bullets in (3, 4)
        assert matches[1].recommended_bullets in (3, 4)

    def test_rt03_internship_compression(self) -> None:
        valid, _ = validate_tailored_claim(
            "Reconciled inventory WIP and completed data using SQL and Python.", "personal-source-afra"
        )
        assert valid

    def test_rt04_barclays_general_developer_portfolio(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "Technology Developer Graduate",
            "Barclays",
            ["Python", "SQL", "REST", "Linux"],
            RoleTrack.SOFTWARE_ENGINEERING,
            "General developer graduate program, enterprise software",
        )
        matches = selector.select_projects(job, profile)
        pids = [m.project.id for m in matches]
        assert "personal-source-session-migration" in pids
        assert "personal-source-afra" in pids

    def test_rt05_barclays_weak_cpp_removed(self) -> None:
        policy = TailoringSkillPolicy.load()
        assert "cpp_prominence" not in policy.anti_patterns
        assert policy.skills_policy.get("keyword_stuffing") == "prohibited"

    def test_rt06_mclaren_hpc_systems_first(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "Junior HPC Systems Engineer",
            "McLaren Racing",
            ["Linux", "C", "Networking"],
            RoleTrack.INFRASTRUCTURE_ENGINEERING,
        )
        matches = selector.select_projects(job, profile)
        assert matches[0].project.id == "personal-source-session-migration"

    def test_rt07_mclaren_missing_slurm_forbidden(self) -> None:
        valid, reason = validate_tailored_claim(
            "Administered Slurm HPC cluster with parallel filesystem.", "personal-source-session-migration"
        )
        assert not valid
        assert "forbidden_missing_skill" in reason

    def test_rt08_mclaren_reposition_auditable(self) -> None:
        valid, _ = validate_tailored_claim(
            "Engineered Python orchestrator and state machine with retry and failure handling.",
            "personal-source-afra",
        )
        assert valid

    def test_rt09_mclaren_indy_remains(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "Junior HPC Systems Engineer",
            "McLaren Racing",
            ["Linux", "C", "Networking", "Python"],
            RoleTrack.INFRASTRUCTURE_ENGINEERING,
            "High performance computing and telemetry simulation",
        )
        matches = selector.select_projects(job, profile)
        pids = [m.project.id for m in matches]
        assert "personal-source-indy500" in pids

    def test_rt10_mclaren_session_migration_metric_precision(self) -> None:
        valid, reason = validate_tailored_claim(
            "Achieved 100% Docker migration success rate across all tests.",
            "personal-source-session-migration",
        )
        assert not valid
        assert "metric_error" in reason

    def test_rt11_session_migration_tcp_continuity(self) -> None:
        valid, reason = validate_tailored_claim(
            "Guaranteed seamless packet-level streaming with no second SYN.",
            "personal-source-session-migration",
        )
        assert not valid
        assert "forbidden_claim" in reason

    def test_rt12_session_migration_latency_not_downtime(self) -> None:
        valid, reason = validate_tailored_claim(
            "Achieved zero downtime migration.", "personal-source-session-migration"
        )
        assert not valid
        assert "forbidden_rewrite" in reason

    def test_rt13_kpmg_tax_software_reverses_systems(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "Software Engineering in Tax",
            "KPMG",
            ["Python", "SQL", "FastAPI"],
            RoleTrack.SOFTWARE_ENGINEERING,
            "Corporate tax software, full stack web apps",
        )
        matches = selector.select_projects(job, profile)
        pids = [m.project.id for m in matches]
        assert pids[0] in ("personal-source-3", "personal-source-afra")
        assert "personal-source-indy500" not in pids

    def test_rt14_kpmg_fullstack_outranks_deeper_systems(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "Software Engineering in Tax",
            "KPMG",
            ["Python", "SQL", "FastAPI", "React"],
            RoleTrack.SOFTWARE_ENGINEERING,
        )
        matches = selector.select_projects(job, profile)
        pids = [m.project.id for m in matches]
        assert pids.index("personal-source-3") < pids.index("personal-source-session-migration")

    def test_rt15_kpmg_remove_indy_despite_quality(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "Software Engineering in Tax", "KPMG", ["Python", "SQL"], RoleTrack.SOFTWARE_ENGINEERING
        )
        matches = selector.select_projects(job, profile)
        assert "personal-source-indy500" not in [m.project.id for m in matches]

    def test_rt16_kpmg_unequal_bullet_budgets(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "Software Engineering in Tax",
            "KPMG",
            ["Python", "SQL", "FastAPI"],
            RoleTrack.SOFTWARE_ENGINEERING,
        )
        matches = selector.select_projects(job, profile)
        budgets = [m.recommended_bullets for m in matches]
        assert budgets[0] >= budgets[-1]

    def test_rt17_auditable_pre_phase_6_5_leakage(self) -> None:
        valid, reason = validate_tailored_claim(
            "Completed real-model benchmark with 126 test executions.",
            "personal-source-afra",
            as_of_phase="pre_6.5",
        )
        assert not valid
        assert "no_future_evidence_leakage" in reason

    def test_rt18_auditable_post_phase_6_5(self) -> None:
        valid, _ = validate_tailored_claim(
            "Evaluated on 126 executions in tested synthetic subset, reducing errors.",
            "personal-source-afra",
            as_of_phase="6.5",
        )
        assert valid

    def test_rt19_auditable_antigravity_cli_title(self) -> None:
        valid, reason = validate_tailored_claim(
            "Integrated Gemini API for live financial market extraction.",
            "personal-source-afra",
        )
        assert not valid
        assert "stack_label_error" in reason

    def test_rt20_iot_mechanisms_beat_service_enumeration(self) -> None:
        valid, reason = validate_tailored_claim(
            "Provided production-ready exactly-once event streaming.",
            "personal-source-iot-spill",
        )
        assert not valid
        assert "forbidden_claim" in reason

    def test_rt21_fullstack_concrete_ci(self) -> None:
        valid, _ = validate_tailored_claim(
            "Automated CI with frontend typecheck, unit tests, and Playwright verification.",
            "personal-source-3",
        )
        assert valid

    def test_rt22_red_bull_pu_restore_stm32_remove_afra(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "Powertrains Embedded Systems Engineer",
            "Red Bull Powertrains",
            ["C", "C++", "Embedded", "Linux"],
            RoleTrack.MOTORSPORT_ENGINEERING,
        )
        matches = selector.select_projects(job, profile)
        pids = [m.project.id for m in matches]
        assert "personal-source-afra" not in pids
        assert "personal-source-stm32" in pids

    def test_rt23_motorsport_society_ownership(self) -> None:
        valid, reason = validate_tailored_claim(
            "Designed vehicle powertrain for racing society.", "personal-source-indy500"
        )
        assert not valid
        assert "inflated_ownership" in reason

    def test_rt24_stm32_prototype_vs_hardened_leakage(self) -> None:
        valid, reason = validate_tailored_claim(
            "Physically re-validated software-hardened firmware on physical test bench.",
            "personal-source-stm32",
        )
        assert not valid
        assert "validation_leakage" in reason

    def test_rt25_respiratory_ml_accuracy_caveat(self) -> None:
        valid, reason = validate_tailored_claim(
            "Achieved ~94% accuracy in respiratory disease detection.", "personal-source-2"
        )
        assert not valid
        assert "metric_error" in reason

    def test_rt26_financial_project_separation(self) -> None:
        policy = TailoringSkillPolicy.load()
        assert "personal-source-afra" != "personal-source-6"
        assert PROJECT_PURPOSE_MAP["personal-source-afra"] != PROJECT_PURPOSE_MAP["personal-source-6"]

    def test_rt27_one_page_overflow_strategy(self) -> None:
        policy = TailoringSkillPolicy.load()
        strategy = policy.layout_policy.get("overflow_strategy", [])
        assert "compress_low_value_wording" in strategy or "remove_redundancy" in strategy
        assert "shrink_everything_first" in policy.anti_patterns or "readability_damage" in policy.layout_policy.get("avoid", [])

    def test_rt28_fdm_general_swe_no_motorsport_overtailoring(
        self, profile: CandidateProfile, selector: PolicyProjectSelector
    ) -> None:
        job = _make_mock_job(
            "Graduate Software Engineer",
            "FDM Group",
            ["Python", "SQL", "Linux", "REST"],
            RoleTrack.SOFTWARE_ENGINEERING,
            "General graduate software development, client consulting",
        )
        matches = selector.select_projects(job, profile)
        pids = [m.project.id for m in matches]
        assert "personal-source-indy500" not in pids
