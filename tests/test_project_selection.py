from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.matching.project_selection import score_projects_for_job, select_top_projects
from jobintel.models.candidate import CandidateProfile, Capability, CapabilityEvidence, EvidenceSource, Project
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import EvidenceSourceType, RoleTrack, SkillCategory


def _job(description: str) -> Job:
    job = Job(
        id="job-1",
        title="Graduate Backend Engineer",
        company="Example Co",
        description=description,
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name="company",
                source_job_id="job-1",
                original_url="https://example.com/jobs/1",
                first_seen_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                last_seen_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                posted_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                raw_description=description,
                canonical_application_url="https://example.com/jobs/1/apply",
            )
        ],
    )
    return enrich_job(job, 2026)


class ProjectSelectionTests(unittest.TestCase):
    def test_strong_evidence_project_outranks_unrelated_project(self) -> None:
        job = _job("Build backend APIs in Python with PostgreSQL, Docker, and AWS. Required: Python, PostgreSQL, Docker.")

        strong_source = EvidenceSource(id="src-strong", source_type=EvidenceSourceType.README_MARKDOWN, title="API project", collected_at=date(2026, 1, 1))
        weak_source = EvidenceSource(id="src-weak", source_type=EvidenceSourceType.README_MARKDOWN, title="Unrelated project", collected_at=date(2026, 1, 1))

        strong_project = Project(id="src-strong", name="API project", description="A backend API project", evidence_sources=[strong_source])
        weak_project = Project(id="src-weak", name="Unrelated project", description="A frontend-only project", evidence_sources=[weak_source])

        profile = CandidateProfile(id="candidate", name="Test Candidate", graduation_year=2026, projects=[strong_project, weak_project])
        for skill in ("Python", "PostgreSQL", "Docker"):
            profile.upsert_capability(
                Capability(
                    name=skill,
                    category=SkillCategory.LANGUAGE,
                    evidence=[CapabilityEvidence(source=strong_source, quote=f"Used {skill} extensively", project_id="src-strong", confidence=1.0)],
                    technology=skill,
                )
            )

        matches = score_projects_for_job(job, profile)
        by_id = {match.project.id: match for match in matches}

        self.assertGreater(by_id["src-strong"].score, by_id["src-weak"].score)
        self.assertIn("Python", by_id["src-strong"].matched_requirements)
        self.assertEqual(by_id["src-weak"].matched_requirements, [])

    def test_select_top_projects_drops_zero_score_projects(self) -> None:
        job = _job("Build backend APIs in Python. Required: Python.")
        unrelated_source = EvidenceSource(id="src-unrelated", source_type=EvidenceSourceType.README_MARKDOWN, title="Unrelated")
        unrelated_project = Project(id="src-unrelated", name="Unrelated", description="No overlap", evidence_sources=[unrelated_source])
        profile = CandidateProfile(id="candidate", name="Test Candidate", graduation_year=2026, projects=[unrelated_project])

        matches = score_projects_for_job(job, profile)
        selected = select_top_projects(matches, max_projects=4)

        self.assertEqual(selected, [])

    def test_no_fabricated_requirement_citations(self) -> None:
        job = _job("Required: Kubernetes experience is essential.")
        source = EvidenceSource(id="src-1", source_type=EvidenceSourceType.README_MARKDOWN, title="Docker project")
        project = Project(id="src-1", name="Docker project", description="Containerised a service with Docker", evidence_sources=[source])
        profile = CandidateProfile(id="candidate", name="Test Candidate", graduation_year=2026, projects=[project])
        profile.upsert_capability(
            Capability(
                name="Docker",
                category=SkillCategory.DEVOPS,
                evidence=[CapabilityEvidence(source=source, quote="Used Docker", project_id="src-1", confidence=1.0)],
                technology="Docker",
            )
        )

        matches = score_projects_for_job(job, profile)
        self.assertNotIn("Kubernetes", matches[0].matched_requirements)


if __name__ == "__main__":
    unittest.main()
