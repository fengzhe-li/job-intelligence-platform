"""Transferable-relevance fallback in project selection.

When the JD's explicit skill requirements have no grounded candidate evidence,
the selector may still pick projects whose OWN evidence supports a
software-engineering dimension the JD text names (testing, CI, ...). The
unsupported requirement must stay unsupported everywhere.
"""
from __future__ import annotations

import re
import tempfile
from datetime import date, datetime, timezone

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.matching.cv_generation import generate_cv
from jobintel.matching.project_selection import score_projects_for_job, select_top_projects_hybrid
from jobintel.models.candidate import CandidateProfile, Capability, CapabilityEvidence, EvidenceSource, Project
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import EvidenceSourceType, ReviewGateStatus, SkillCategory

TRANSFERABLE_JD = (
    "Required: {skill}. You will write lots of tests and we practice continuous integration: "
    "every commit is verified by our CI pipeline."
)


def _job(description: str, title: str = "Junior Software Engineer") -> Job:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    raw = Job(
        id="j-transfer-1",
        title=title,
        company="ExampleCo",
        description=description,
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name="ashby",
                source_job_id="j-transfer-1",
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


def _candidate(projects: dict[str, list[tuple[str, SkillCategory, str]]]) -> CandidateProfile:
    """projects: project_id -> [(capability name, category, evidence quote)]."""
    capabilities: dict[str, Capability] = {}
    project_list: list[Project] = []
    for project_id, items in projects.items():
        source = EvidenceSource(id=f"src-{project_id}", source_type=EvidenceSourceType.README_MARKDOWN, title=project_id, collected_at=date(2026, 1, 1))
        project_list.append(Project(project_id, f"Project {project_id}", f"Description of {project_id}.", [source]))
        for index, (name, category, quote) in enumerate(items):
            evidence = CapabilityEvidence(source, quote, project_id, 1.0, f"{project_id}:{index}")
            capabilities.setdefault(name.casefold(), Capability(name, category, [])).add_evidence(evidence)
    return CandidateProfile(
        id="c1",
        name="Test Candidate",
        graduation_year=2026,
        capabilities=capabilities,
        projects=project_list,
        email="test@example.com",
        phone="07123456789",
    )


TESTED_SERVICE = [
    ("pytest", SkillCategory.TOOLING, "Wrote a pytest suite covering the order service's API endpoints."),
    ("CI/CD", SkillCategory.DEVOPS, "Set up a CI/CD pipeline that runs the test suite on every push."),
    ("JavaScript", SkillCategory.LANGUAGE, "Built the JavaScript dashboard that consumes the order service."),
]
ML_ONLY = [("Keras", SkillCategory.ML_AI, "Trained a Keras classifier for respiratory sound recordings.")]
EMBEDDED_ONLY = [("I2C", SkillCategory.EMBEDDED, "Read accelerometer samples over the I2C bus on a microcontroller.")]
PYTHON_EXACT = [("Python", SkillCategory.LANGUAGE, "Implemented the ingestion service in Python.")]


class TestTransferableFallback:
    # --- A: unsupported required skill, grounded transferable projects exist ---

    def test_a_selects_grounded_transferable_project_and_keeps_skill_unsupported(self) -> None:
        job = _job(TRANSFERABLE_JD.format(skill="Rust"))
        assert [(r.name, r.required) for r in job.skill_requirements] == [("Rust", True)]
        candidate = _candidate({"svc": TESTED_SERVICE, "ml": ML_ONLY})

        # Exact matching alone finds nothing ...
        assert all(match.score == 0 for match in score_projects_for_job(job, candidate))

        selected, explanations, _ = select_top_projects_hybrid(job, candidate)

        assert [match.project.id for match in selected] == ["svc"]
        match = selected[0]
        assert match.selection_basis == "transferable"
        assert set(match.transferable_dimensions) == {"testing", "ci_cd"}
        # ... and the fallback never converts the unsupported skill into a match.
        assert match.matched_requirements == []
        assert match.missing_requirements == ["rust"]
        assert "no grounded evidence for required: Rust" in explanations["svc"]

    # --- B: no genuinely relevant grounded projects ---

    def test_b_no_relevant_grounded_projects_selects_nothing(self) -> None:
        job = _job(TRANSFERABLE_JD.format(skill="Rust"))
        candidate = _candidate({"ml": ML_ONLY, "embedded": EMBEDDED_ONLY})

        selected, explanations, _ = select_top_projects_hybrid(job, candidate)

        assert selected == []
        assert explanations == {}

    def test_b_jd_naming_no_transferable_dimension_does_not_fill_with_projects(self) -> None:
        # Real, well-evidenced projects exist, but this JD gives nothing they are
        # relevant to: they must not be added just to populate the CV.
        job = _job("Required: Rust.")
        candidate = _candidate({"svc": TESTED_SERVICE})

        selected, _, _ = select_top_projects_hybrid(job, candidate)

        assert selected == []

    # --- C: exact-skill matching wins ---

    def test_c_exact_evidence_wins_and_fallback_does_not_run_when_enough_exact(self) -> None:
        job = _job(TRANSFERABLE_JD.format(skill="Python"))
        candidate = _candidate({"svc": TESTED_SERVICE, "py1": PYTHON_EXACT, "py2": PYTHON_EXACT})

        selected, _, _ = select_top_projects_hybrid(job, candidate)

        assert {match.project.id for match in selected} == {"py1", "py2"}
        assert all(match.selection_basis == "exact_requirement" for match in selected)

    def test_c_exact_match_ranks_before_transferable_projects(self) -> None:
        job = _job(TRANSFERABLE_JD.format(skill="Python"))
        # "svc" has more transferable dimensions than "py1" has exact matches,
        # but exact grounded evidence for the JD's requirement still comes first.
        candidate = _candidate({"svc": TESTED_SERVICE, "py1": PYTHON_EXACT})

        selected, _, _ = select_top_projects_hybrid(job, candidate)

        assert [match.project.id for match in selected] == ["py1", "svc"]
        assert selected[0].selection_basis == "exact_requirement"
        assert selected[0].matched_requirements == ["Python"]
        assert selected[1].selection_basis == "transferable"
        assert selected[1].matched_requirements == []

    def test_c_fallback_respects_max_projects(self) -> None:
        job = _job(TRANSFERABLE_JD.format(skill="Python"))
        candidate = _candidate({"svc": TESTED_SERVICE, "py1": PYTHON_EXACT})

        selected, _, _ = select_top_projects_hybrid(job, candidate, max_projects=1)

        assert [match.project.id for match in selected] == ["py1"]

    # --- D: no unsupported skill enters CV skills / bullets ---

    def test_d_generated_cv_never_introduces_the_unsupported_skill(self) -> None:
        # Java is required; the candidate only has JavaScript -- which must not
        # be read as Java anywhere.
        job = _job(TRANSFERABLE_JD.format(skill="Java"))
        candidate = _candidate({"svc": TESTED_SERVICE, "ml": ML_ONLY})
        java = re.compile(r"\bjava\b", re.IGNORECASE)

        with tempfile.TemporaryDirectory() as tmp:
            artifact = generate_cv(candidate, job, store_root=tmp, github_freshness_state="current")

            assert artifact.selected_project_ids == ["svc"]
            assert not [name for name in artifact.skills_included if java.search(name)]
            assert not [bullet.text for bullet in artifact.bullets if java.search(bullet.text)]
            assert not java.search(artifact.cv_text)
            # Every bullet is a verbatim quote of this project's own evidence, or the Impact &
            # Ownership composition of one (its verbatim source kept in the provenance).
            quotes = {quote for _, _, quote in TESTED_SERVICE}
            assert artifact.bullets and all((bullet.provenance.get("original_text") or bullet.text) in quotes
                                            for bullet in artifact.bullets)
            # The unsupported must-have is still reported, and still blocks auto-submission.
            assert any("Java" in reason and "must-have" in reason for reason in artifact.review_reasons)
            assert artifact.review_status == ReviewGateStatus.BLOCK_AUTO_SUBMISSION
            assert not any("missing core section(s): projects" in reason for reason in artifact.review_reasons)



# --- E: capability shown by implementation/validation evidence, not by a tool name (2026-10-13) ---

RESILIENCE_JD = (
    "Required: Java. Build resilient platforms, write lots of tests (TDD), automate as much as possible; "
    "we care about latency."
)
VALIDATED_SYSTEMS = [
    ("Linux", SkillCategory.DEVOPS, "Progressive migration framework: native process migration (5/5 final trials)."),
    ("Bash", SkillCategory.LANGUAGE, "Automated testbed scripts orchestrating state capture and downtime measurement."),
    ("TCP/IP", SkillCategory.NETWORK, "Floating VIP preserves an active session without disconnecting the client."),
]


class TestEvidenceGroundedDimensions:
    def test_e_repeated_validation_and_orchestration_evidence_count_without_a_test_framework(self) -> None:
        job = _job(RESILIENCE_JD)
        candidate = _candidate({"svc": TESTED_SERVICE, "systems": VALIDATED_SYSTEMS})
        selected, explanations, _ = select_top_projects_hybrid(job, candidate)
        systems = next(match for match in selected if match.project.id == "systems")
        assert {"testing", "automation", "resilience", "systems_performance"} <= set(systems.transferable_dimensions)
        assert "'5/5 final trials'" in explanations["systems"]
        # Grounded relevance only -- no skill or requirement is created.
        assert systems.matched_requirements == [] and systems.missing_requirements == ["java"]
        assert "no grounded evidence for required: Java" in explanations["systems"]

    def test_e_a_dimension_the_jd_does_not_name_never_counts(self) -> None:
        job = _job("Required: Java. You will write lots of tests.")
        candidate = _candidate({"systems": VALIDATED_SYSTEMS})
        selected, _, _ = select_top_projects_hybrid(job, candidate)
        assert [set(m.transferable_dimensions) for m in selected] == [{"testing"}]

    def test_e_a_fourth_qualifying_project_may_be_selected_but_never_filler(self) -> None:
        job = _job(RESILIENCE_JD)
        candidate = _candidate({"a": TESTED_SERVICE, "b": TESTED_SERVICE, "c": TESTED_SERVICE,
                                "systems": VALIDATED_SYSTEMS, "ml": ML_ONLY})
        selected, _, _ = select_top_projects_hybrid(job, candidate, max_projects=4)
        assert len(selected) == 4 and "ml" not in {m.project.id for m in selected}
