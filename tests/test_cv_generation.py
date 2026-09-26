from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.matching.cv_generation import generate_cv
from jobintel.models.candidate import (
    CandidateProfile,
    Capability,
    CapabilityEvidence,
    Education,
    EvidenceSource,
    Experience,
    Project,
)
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import EvidenceSourceType, ReviewGateStatus, SkillCategory


def _job(description: str, title: str = "Graduate Backend Engineer") -> Job:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    job = Job(
        id="job-1",
        title=title,
        company="Example Co",
        description=description,
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name="company",
                source_job_id="job-1",
                original_url="https://example.com/jobs/1",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description=description,
                canonical_application_url="https://example.com/jobs/1/apply",
            )
        ],
    )
    return enrich_job(job, 2026)


def _project_with_skills(project_id: str, name: str, skills: list[str], description: str = "") -> tuple[Project, list[Capability]]:
    source = EvidenceSource(id=project_id, source_type=EvidenceSourceType.README_MARKDOWN, title=name, collected_at=date(2026, 1, 1))
    project = Project(id=project_id, name=name, description=description or f"{name} project", evidence_sources=[source])
    capabilities = [
        Capability(
            name=skill,
            category=SkillCategory.LANGUAGE,
            evidence=[
                CapabilityEvidence(
                    source=source,
                    quote=f"Built {name} using {skill} to serve production traffic reliably.",
                    project_id=project_id,
                    confidence=1.0,
                    id=f"{project_id}:{skill.casefold()}",
                )
            ],
            technology=skill,
        )
        for skill in skills
    ]
    return project, capabilities


class CVGenerationTestCase(unittest.TestCase):
    def _candidate_with_projects(self, project_specs: list[tuple[str, str, list[str]]], with_contact: bool = True) -> CandidateProfile:
        profile = CandidateProfile(
            id="candidate-1",
            name="Test Candidate",
            graduation_year=2026,
            education=[Education(institution="Example University", programme="MSc Computer Science", graduation_year=2026, description="")],
            experience=[Experience(organisation="Example Ltd", title="Software Engineering Intern", description="Built internal tooling used by three teams daily.")],
        )
        if with_contact:
            profile.email = "test.candidate@example.com"
            profile.phone = "07700 000000"
            profile.location = "London, UK"
            profile.github_url = "https://github.com/test-candidate"
        for project_id, name, skills in project_specs:
            project, capabilities = _project_with_skills(project_id, name, skills)
            profile.projects.append(project)
            for capability in capabilities:
                profile.upsert_capability(capability)
        return profile

    def _generate(self, candidate, job, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            return generate_cv(candidate, job, store_root=tmp, **kwargs)


class CVGenerationTests(CVGenerationTestCase):
    def test_selects_at_most_four_projects_dynamically(self) -> None:
        job = _job("Required: Python, PostgreSQL, Docker, AWS, Kubernetes.")
        candidate = self._candidate_with_projects(
            [
                ("p1", "API Platform", ["Python", "PostgreSQL"]),
                ("p2", "Infra Tooling", ["Docker", "AWS"]),
                ("p3", "K8s Operator", ["Kubernetes"]),
                ("p4", "Data Pipeline", ["Python"]),
                ("p5", "Unrelated Game", ["JavaScript"]),
            ]
        )
        artifact = self._generate(candidate, job)

        self.assertLessEqual(len(artifact.selected_project_ids), 4)
        self.assertNotIn("p5", artifact.selected_project_ids)

    def test_no_hardcoded_project_list_new_project_becomes_eligible(self) -> None:
        # Simulates a repository github_sync discovers *after* this test was written --
        # project selection must be fully evidence-driven, never a fixed id/name list.
        job = _job("Required: GraphQL.")
        candidate = self._candidate_with_projects([("brand-new-repo", "Newly Synced Repo", ["GraphQL"])])
        artifact = self._generate(candidate, job)

        self.assertIn("brand-new-repo", artifact.selected_project_ids)
        self.assertIn("GraphQL", " ".join(bullet.text for bullet in artifact.bullets))

    def test_every_bullet_is_traceable_to_real_evidence(self) -> None:
        job = _job("Required: Python, PostgreSQL.")
        candidate = self._candidate_with_projects([("p1", "API Platform", ["Python", "PostgreSQL"])])
        artifact = self._generate(candidate, job)

        self.assertTrue(artifact.bullets)
        all_evidence_ids = {evidence.id for capability in candidate.capabilities.values() for evidence in capability.evidence}
        for bullet in artifact.bullets:
            self.assertTrue(bullet.evidence_ids)
            for evidence_id in bullet.evidence_ids:
                self.assertIn(evidence_id, all_evidence_ids)
                self.assertIn(evidence_id, artifact.evidence_ids_used)

    def test_bullet_text_is_only_verbatim_evidence_fragments_never_invented(self) -> None:
        job = _job("Required: Python, PostgreSQL.")
        candidate = self._candidate_with_projects([("p1", "API Platform", ["Python", "PostgreSQL"])])
        artifact = self._generate(candidate, job)

        source_quotes = {evidence.quote for capability in candidate.capabilities.values() for evidence in capability.evidence}
        for bullet in artifact.bullets:
            fragments = [fragment.strip() for fragment in bullet.text.split(";")]
            for fragment in fragments:
                # Every fragment must be real evidence text -- never freely
                # generated, and never character-truncated with an ellipsis.
                self.assertNotIn("…", fragment)
                stripped = fragment.strip()
                self.assertTrue(
                    any(quote.startswith(stripped) or stripped in quote for quote in source_quotes),
                    msg=f"Bullet fragment {fragment!r} is not traceable to any real evidence quote",
                )

    def test_never_includes_a_skill_with_no_evidence(self) -> None:
        job = _job("Required: Python, Kubernetes.")
        candidate = self._candidate_with_projects([("p1", "API Platform", ["Python"])])
        artifact = self._generate(candidate, job)

        self.assertIn("Python", artifact.skills_included)
        self.assertNotIn("Kubernetes", artifact.skills_included)
        self.assertNotIn("Kubernetes", artifact.cv_text)

    def test_skills_are_reordered_with_required_jd_skills_first(self) -> None:
        job = _job("Required: Docker.")
        candidate = self._candidate_with_projects([("p1", "Project", ["Python", "Docker"])])
        artifact = self._generate(candidate, job)

        self.assertEqual(artifact.skills_included[0], "Docker")

    def test_education_and_experience_are_preserved_verbatim(self) -> None:
        job = _job("Required: Python.")
        candidate = self._candidate_with_projects([("p1", "Project", ["Python"])])
        artifact = self._generate(candidate, job)

        self.assertIn("MSc Computer Science, Example University (2026)", artifact.cv_text)
        self.assertIn("Software Engineering Intern, Example Ltd", artifact.cv_text)

    def test_output_is_a_real_rendered_pdf_with_measured_page_count(self) -> None:
        job = _job("Required: Python.")
        candidate = self._candidate_with_projects([("p1", "Project", ["Python"])])
        artifact = self._generate(candidate, job)

        self.assertTrue(artifact.pdf_path)
        self.assertGreaterEqual(artifact.page_count, 1)
        self.assertEqual(artifact.fits_one_page, artifact.page_count <= 1)

    def test_review_gate_raises_no_skill_or_contact_concern_when_fully_evidenced(self) -> None:
        # This one-project, one-bullet fixture has no summary and fills a small
        # part of the page, so it is (correctly) held for review on document
        # completeness -- but never for skills or contact details. AUTO_PREPARE
        # for a complete, page-filling CV is covered in test_cv_document_structure.
        job = _job("Required: Python.")
        candidate = self._candidate_with_projects([("p1", "Project", ["Python"])])
        artifact = self._generate(candidate, job)
        self.assertEqual(artifact.review_status, ReviewGateStatus.REVIEW_REQUIRED)
        for reason in artifact.review_reasons:
            self.assertTrue("missing core section" in reason or "of the page height" in reason, reason)

    def test_review_gate_flags_missing_required_evidence(self) -> None:
        job = _job("Required: Python, Kubernetes, Terraform.")
        candidate = self._candidate_with_projects([("p1", "Project", ["Python"])])
        artifact = self._generate(candidate, job)
        self.assertIn(artifact.review_status, {ReviewGateStatus.REVIEW_REQUIRED, ReviewGateStatus.BLOCK_AUTO_SUBMISSION})
        self.assertTrue(artifact.review_reasons)

    def test_artifact_persists_jd_snapshot_and_job_id(self) -> None:
        job = _job("Required: Python.")
        candidate = self._candidate_with_projects([("p1", "Project", ["Python"])])
        artifact = self._generate(candidate, job, application_id="app-1")

        self.assertEqual(artifact.job_id, "job-1")
        self.assertEqual(artifact.jd_snapshot, job.description)
        self.assertEqual(artifact.application_id, "app-1")
        self.assertEqual(artifact.candidate_id, "candidate-1")


class CVGenerationProductionSafetyTests(CVGenerationTestCase):
    """Adversarial/regression tests for Phase 2B production hardening."""

    def test_jd_asks_kubernetes_but_evidence_only_proves_docker(self) -> None:
        job = _job("Required: Docker, Kubernetes.")
        candidate = self._candidate_with_projects([("p1", "Infra Project", ["Docker"])])
        artifact = self._generate(candidate, job)

        self.assertIn("Docker", artifact.skills_included)
        self.assertNotIn("Kubernetes", artifact.skills_included)
        self.assertNotIn("Kubernetes", artifact.cv_text)
        self.assertTrue(any("Kubernetes" in reason for reason in artifact.review_reasons))

    def test_jd_asks_rust_and_candidate_has_no_rust_evidence(self) -> None:
        job = _job("Required: Python, Rust.")
        candidate = self._candidate_with_projects([("p1", "Project", ["Python"])])
        artifact = self._generate(candidate, job)

        self.assertNotIn("Rust", artifact.skills_included)
        self.assertNotIn("Rust", artifact.cv_text)
        self.assertTrue(any("Rust" in reason for reason in artifact.review_reasons))
        self.assertIn(artifact.review_status, {ReviewGateStatus.REVIEW_REQUIRED, ReviewGateStatus.BLOCK_AUTO_SUBMISSION})

    def test_partial_evidence_is_flagged_not_silently_presented_as_strong(self) -> None:
        job = _job("Required: Python.")
        candidate = self._candidate_with_projects([("p1", "Project", [])])
        source = EvidenceSource(id="p1", source_type=EvidenceSourceType.README_MARKDOWN, title="Project")
        # Confidence in the "partial" band (0.3 <= x < 0.7).
        candidate.upsert_capability(
            Capability(
                name="Python",
                category=SkillCategory.LANGUAGE,
                evidence=[CapabilityEvidence(source=source, quote="Touched Python briefly during a workshop.", project_id="p1", confidence=0.4, id="p1:python")],
                technology="Python",
            )
        )
        artifact = self._generate(candidate, job)

        self.assertIn("Python", artifact.skills_included)
        self.assertTrue(any("partial evidence" in reason.casefold() for reason in artifact.review_reasons))
        self.assertNotEqual(artifact.review_status, ReviewGateStatus.AUTO_PREPARE)

    def test_evidence_from_project_a_is_never_attributed_to_project_b(self) -> None:
        job = _job("Required: Python.")
        candidate = self._candidate_with_projects([("project-a", "Project A", ["Python"]), ("project-b", "Project B", [])])
        # Project B has no Python evidence of its own.
        artifact = self._generate(candidate, job)

        for bullet in artifact.bullets:
            if bullet.project_id == "project-b":
                self.fail("Project B produced a bullet despite having no linked evidence for the matched skill")
        selected_bullets_for_a = [b for b in artifact.bullets if b.project_id == "project-a"]
        self.assertTrue(selected_bullets_for_a)
        for bullet in selected_bullets_for_a:
            for evidence_id in bullet.evidence_ids:
                self.assertTrue(evidence_id.startswith("project-a:"))

    def test_no_invented_metrics_every_number_traces_to_source_evidence(self) -> None:
        job = _job("Required: Python.")
        candidate = self._candidate_with_projects([("p1", "Project", [])])
        source = EvidenceSource(id="p1", source_type=EvidenceSourceType.README_MARKDOWN, title="Project")
        candidate.upsert_capability(
            Capability(
                name="Python",
                category=SkillCategory.LANGUAGE,
                evidence=[CapabilityEvidence(source=source, quote="Built a Python service handling 500 requests per second.", project_id="p1", confidence=1.0, id="p1:python")],
                technology="Python",
            )
        )
        artifact = self._generate(candidate, job)

        for bullet in artifact.bullets:
            for token in bullet.text.split():
                if token.strip(".,;…").isdigit():
                    self.assertIn(token.strip(".,;…"), "500", msg=f"Invented-looking number {token!r} not present in source evidence")

    def test_missing_required_contact_fields_triggers_review(self) -> None:
        job = _job("Required: Python.")
        candidate = self._candidate_with_projects([("p1", "Project", ["Python"])], with_contact=False)
        artifact = self._generate(candidate, job)

        self.assertNotEqual(artifact.review_status, ReviewGateStatus.AUTO_PREPARE)
        self.assertTrue(any("contact" in reason.casefold() for reason in artifact.review_reasons))

    def test_contact_fields_are_never_fabricated(self) -> None:
        job = _job("Required: Python.")
        candidate = self._candidate_with_projects([("p1", "Project", ["Python"])], with_contact=False)
        artifact = self._generate(candidate, job)

        self.assertNotIn("@", artifact.cv_text)  # no fabricated email appears

    def test_overflowing_content_is_still_rendered_and_measured_honestly(self) -> None:
        job = _job("Required: " + ", ".join(f"Skill{i}" for i in range(15)) + ".")
        specs = [(f"p{i}", f"Project {i}", [f"Skill{i}"]) for i in range(15)]
        candidate = self._candidate_with_projects(specs)
        for project_id, name, skills in specs:
            for skill in skills:
                capability = candidate.capabilities.get(skill.casefold())
                if capability:
                    capability.evidence[0] = CapabilityEvidence(
                        source=capability.evidence[0].source,
                        quote=(
                            f"Extensive production-grade engineering work on {skill} within {name}, covering system design, "
                            "implementation, rigorous testing, observability, on-call operational ownership, and continuous "
                            "iteration over many months of sustained active development with cross-functional stakeholders."
                        ),
                        project_id=project_id,
                        confidence=1.0,
                        id=capability.evidence[0].id,
                    )
        artifact = self._generate(candidate, job)

        # Real, honest measurement -- never silently claim it fits when it doesn't.
        self.assertEqual(artifact.fits_one_page, artifact.page_count <= 1)
        self.assertLessEqual(len(artifact.selected_project_ids), 4)
        if artifact.page_count > 1:
            self.assertTrue(any("page" in reason.casefold() for reason in artifact.review_reasons))
            self.assertNotEqual(artifact.review_status, ReviewGateStatus.AUTO_PREPARE)

    def test_highly_relevant_evidence_survives_trimming_before_weak_evidence(self) -> None:
        job = _job("Required: Python.")
        candidate = self._candidate_with_projects([("p1", "Project", [])])
        source = EvidenceSource(id="p1", source_type=EvidenceSourceType.README_MARKDOWN, title="Project")
        # Strong, JD-relevant evidence (mentions "Python" directly, high confidence)
        # vs. weak evidence for the same capability (low confidence). The strong one
        # must be the one selected as the project's evidence quote.
        candidate.capabilities["python"] = Capability(
            name="Python",
            category=SkillCategory.LANGUAGE,
            evidence=[
                CapabilityEvidence(source=source, quote="Weak, tangential mention of scripting.", project_id="p1", confidence=0.35, id="p1:python:weak"),
                CapabilityEvidence(source=source, quote="Designed and shipped a Python backend service used by production traffic.", project_id="p1", confidence=1.0, id="p1:python:strong"),
            ],
            technology="Python",
        )
        artifact = self._generate(candidate, job)

        bullet_evidence_ids = {evidence_id for bullet in artifact.bullets for evidence_id in bullet.evidence_ids}
        self.assertIn("p1:python:strong", bullet_evidence_ids)

    def test_future_github_repository_is_selectable_without_hardcoding(self) -> None:
        job = _job("Required: Elixir.")
        candidate = self._candidate_with_projects([("future-repo-xyz", "Repo Synced Tomorrow", ["Elixir"])])
        artifact = self._generate(candidate, job)

        self.assertIn("future-repo-xyz", artifact.selected_project_ids)


if __name__ == "__main__":
    unittest.main()
