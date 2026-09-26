"""Regression tests for task REAL-DOGFOOD-CV-FRESHNESS-AND-LAYOUT-REFINEMENT.

Proves:
1. newly discovered GitHub repository can become eligible for later CV selection without hardcoding;
2. updated repository evidence can affect a later CV;
3. unchanged repositories retain incremental behavior;
4. unavailable/unverified GitHub freshness is represented truthfully;
5. repository recency itself does not outrank stronger JD relevance;
6. sparse compatible skill groups can merge without inventing skills;
7. bounded ~9–10 mm layout remains exactly one A4 page and unclipped;
8. semantically redundant same-project bullets are not used merely to fill a quota.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth

from jobintel.analysis.jd_requirements import extract_jd_requirements
from jobintel.analysis.job_enrichment import enrich_job
from jobintel.github_sync import (
    RepositoryMetadata,
    check_github_freshness,
    extract_github_username,
    resolve_github_username,
    sync_repositories,
)
from jobintel.matching.cv_document import (
    DatedEntry,
    SkillGroup,
    group_skills,
)
from jobintel.matching.cv_generation import (
    _compose_bullets_for_project,
    _content_overlap,
    generate_cv,
)
from jobintel.matching.cv_render import LAYOUT_PRESETS, render_cv_document
from jobintel.matching.project_selection import (
    ProjectMatch,
    score_projects_for_job,
    select_top_projects_hybrid,
)
from jobintel.models.candidate import (
    CandidateProfile,
    Capability,
    CapabilityEvidence,
    EvidenceSource,
    Project,
)
from jobintel.models.job import Job, Location, RoleTrackProfile, RoleTrackScore, SkillRequirement, SourceObservation
from jobintel.models.taxonomy import EvidenceSourceType, RoleTrack, SkillCategory
from jobintel.profile_ingestion import add_profile_source, build_candidate_profile, set_contact_details
from jobintel.storage.repository_store import RepositoryStore


def _make_job(title: str, required_skills: list[str], description: str = "") -> Job:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    desc = description or f"Role: {title}. Required skills: {', '.join(required_skills)}."
    return Job(
        id="job-test-1",
        title=title,
        company="Test Co",
        description=desc,
        locations=[Location(city="London", country="United Kingdom")],
        role_track_profile=RoleTrackProfile([RoleTrackScore(RoleTrack.BACKEND_ENGINEERING, 1.0)]),
        skill_requirements=[SkillRequirement(name=s, required=True) for s in required_skills],
        source_observations=[
            SourceObservation(
                source_name="direct",
                source_job_id="job-test-1",
                original_url="https://example.com/jobs/1",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description=desc,
                canonical_application_url="https://example.com/jobs/1/apply",
            )
        ],
    )


def _text_runs(page) -> list[tuple[float, float, float]]:
    runs = []

    def visit(text, cm, tm, font, size):
        if text.strip() and font:
            x = tm[4] + cm[4]
            runs.append((x, x + stringWidth(text.rstrip("\n"), font["/BaseFont"].lstrip("/"), size * tm[0]), tm[5] + cm[5]))

    page.extract_words = lambda: None
    page.extract_text(visitor_text=visit)
    return runs


class CVFreshnessAndLayoutRefinementTests(unittest.TestCase):
    def test_github_username_derivation_from_profile_url(self) -> None:
        """Derived from candidate profile github_url without requiring manual env var."""
        self.assertEqual(extract_github_username("https://github.com/developer-candidate"), "developer-candidate")
        self.assertEqual(extract_github_username("https://github.com/developer-candidate/"), "developer-candidate")
        self.assertEqual(extract_github_username("github.com/developer-candidate"), "developer-candidate")
        self.assertEqual(extract_github_username("developer-candidate"), "developer-candidate")
        self.assertIsNone(extract_github_username(None))
        self.assertIsNone(extract_github_username(""))

        cand = CandidateProfile(
            id="c1",
            name="Candidate",
            graduation_year=2026,
            github_url="https://github.com/candidate-account",
        )
        resolved = resolve_github_username(candidate=cand)
        self.assertEqual(resolved, "candidate-account")

    def test_1_newly_discovered_github_repo_becomes_eligible_without_hardcoding(self) -> None:
        """Requirement 1: A newly discovered GitHub repository becomes eligible for later CV selection."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Setup initial profile
            readme_init = root / "initial.md"
            readme_init.write_text("# Initial Repo\n\nBuilt an internal tool with Python and Docker.", encoding="utf-8")
            add_profile_source(root, readme_init, EvidenceSourceType.README_MARKDOWN, title="initial-repo")
            profile = build_candidate_profile(root, name="Test Candidate", graduation_year=2026)
            self.assertEqual(len(profile.projects), 1)

            # Discover a brand new repository via sync
            fake_repos = [
                RepositoryMetadata(
                    full_name="mock-user/new-ai-service",
                    default_branch="main",
                    pushed_at="2026-09-25T12:00:00Z",
                    description="AI backend service in Python and TypeScript",
                    html_url="https://github.com/mock-user/new-ai-service",
                )
            ]
            new_readme = "# New AI Service\n\nFastAPI microservice in Python and TypeScript with automated tests."
            with patch("jobintel.github_sync.discover_repositories", return_value=fake_repos), \
                 patch("jobintel.github_sync._fetch_latest_commit_sha", return_value="sha-abc-123"), \
                 patch("jobintel.github_sync._fetch_readme_text", return_value=new_readme), \
                 patch("jobintel.github_sync._fetch_languages", return_value={"Python": 10000, "TypeScript": 5000}):
                sync_res = sync_repositories(root, "mock-user", token=None, name=profile.name, graduation_year=profile.graduation_year)

            self.assertEqual(sync_res.repositories_synced, 1)

            # Rebuild profile and test matching for a TypeScript/FastAPI job
            updated_profile = build_candidate_profile(root, name=profile.name, graduation_year=profile.graduation_year)
            project_names = [p.name for p in updated_profile.projects]
            self.assertIn("mock-user/new-ai-service", project_names)

            job = _make_job("AI Backend Engineer", ["TypeScript", "FastAPI"])
            matches = score_projects_for_job(job, updated_profile)
            self.assertTrue(any(m.project.name == "mock-user/new-ai-service" for m in matches if m.score > 0))

    def test_2_updated_repository_evidence_affects_later_cv(self) -> None:
        """Requirement 2: Updated repository evidence can affect a later CV."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_name = "mock-user/data-service"
            # 1. Initial sync with basic Python evidence
            fake_repos = [
                RepositoryMetadata(
                    full_name=repo_name,
                    default_branch="main",
                    pushed_at="2026-09-20T10:00:00Z",
                    description="Python service",
                    html_url=f"https://github.com/{repo_name}",
                )
            ]
            with patch("jobintel.github_sync.discover_repositories", return_value=fake_repos), \
                 patch("jobintel.github_sync._fetch_latest_commit_sha", return_value="sha-1"), \
                 patch("jobintel.github_sync._fetch_readme_text", return_value="# Data Service\nPython script."), \
                 patch("jobintel.github_sync._fetch_languages", return_value={"Python": 1000}):
                sync_repositories(root, "mock-user", token=None)

            job = _make_job("Database Specialist", ["PostgreSQL"])
            p1 = build_candidate_profile(root, name="Candidate", graduation_year=2026)
            matches_before = score_projects_for_job(job, p1)
            # Before update, no PostgreSQL evidence exists
            self.assertEqual(sum(m.score for m in matches_before if m.project.name == repo_name), 0)

            # 2. Repository is updated with PostgreSQL integration
            fake_repos_updated = [
                RepositoryMetadata(
                    full_name=repo_name,
                    default_branch="main",
                    pushed_at="2026-09-25T10:00:00Z",  # advanced pushed_at
                    description="Python service with PostgreSQL database",
                    html_url=f"https://github.com/{repo_name}",
                )
            ]
            with patch("jobintel.github_sync.discover_repositories", return_value=fake_repos_updated), \
                 patch("jobintel.github_sync._fetch_latest_commit_sha", return_value="sha-2"), \
                 patch("jobintel.github_sync._fetch_readme_text", return_value="# Data Service\nIntegrated PostgreSQL database with real integration tests."), \
                 patch("jobintel.github_sync._fetch_languages", return_value={"Python": 1000, "SQL": 500}):
                sync_res = sync_repositories(root, "mock-user", token=None)

            self.assertEqual(sync_res.repositories_synced, 1)
            p2 = build_candidate_profile(root, name="Candidate", graduation_year=2026)
            matches_after = score_projects_for_job(job, p2)
            # After update, PostgreSQL evidence matches and scores
            self.assertGreater(sum(m.score for m in matches_after if m.project.name == repo_name), 0)

    def test_3_unchanged_repositories_retain_incremental_behavior(self) -> None:
        """Requirement 3: Unchanged repositories avoid unnecessary re-fetching."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_name = "mock-user/static-service"
            pushed_time = "2026-09-20T10:00:00Z"
            fake_repos = [
                RepositoryMetadata(
                    full_name=repo_name,
                    default_branch="main",
                    pushed_at=pushed_time,
                    description="Static service",
                    html_url=f"https://github.com/{repo_name}",
                )
            ]
            with patch("jobintel.github_sync.discover_repositories", return_value=fake_repos), \
                 patch("jobintel.github_sync._fetch_latest_commit_sha", return_value="sha-1"), \
                 patch("jobintel.github_sync._fetch_readme_text", return_value="# Static\nPython."), \
                 patch("jobintel.github_sync._fetch_languages", return_value={"Python": 1000}):
                res1 = sync_repositories(root, "mock-user", token=None)

            self.assertEqual(res1.repositories_synced, 1)
            self.assertEqual(res1.repositories_unchanged, 0)

            # Second sync without timestamp advance
            with patch("jobintel.github_sync.discover_repositories", return_value=fake_repos), \
                 patch("jobintel.github_sync._fetch_latest_commit_sha") as mock_sha, \
                 patch("jobintel.github_sync._fetch_readme_text") as mock_readme:
                res2 = sync_repositories(root, "mock-user", token=None)

            self.assertEqual(res2.repositories_synced, 0)
            self.assertEqual(res2.repositories_unchanged, 1)
            mock_sha.assert_not_called()
            mock_readme.assert_not_called()

    def test_4_unavailable_or_unverified_github_freshness_represented_truthfully(self) -> None:
        """Requirement 4: Unavailable/unverified GitHub freshness is represented truthfully on artifact."""
        cand_no_github = CandidateProfile(id="c1", name="No GitHub", graduation_year=2026)
        with tempfile.TemporaryDirectory() as tmp:
            freshness_unconfigured = check_github_freshness(tmp, cand_no_github)
            self.assertEqual(freshness_unconfigured, "not_configured")

            job = _make_job("Software Engineer", ["Python"])
            # Generate CV without GitHub config
            artifact = generate_cv(cand_no_github, job, store_root=tmp)
            self.assertEqual(artifact.github_freshness_state, "not_configured")

    def test_5_repository_recency_does_not_outrank_stronger_jd_relevance(self) -> None:
        """Requirement 5: Recency itself is not a ranking signal; stronger JD relevance wins."""
        src_old = EvidenceSource("src-old", EvidenceSourceType.GITHUB_REPOSITORY_SYNC, "old-backend", collected_at=date(2025, 1, 1))
        src_new = EvidenceSource("src-new", EvidenceSourceType.GITHUB_REPOSITORY_SYNC, "new-toy", collected_at=date(2026, 9, 25))

        proj_old = Project(id="p-old", name="Enterprise Backend", description="High-scale Python PostgreSQL API")
        proj_new = Project(id="p-new", name="New Toy Script", description="Recent script with no DB")

        cap_python = Capability(name="Python", category=SkillCategory.LANGUAGE)
        cap_python.add_evidence(CapabilityEvidence(src_old, "Enterprise Python backend with clean architecture.", project_id=proj_old.id))
        cap_python.add_evidence(CapabilityEvidence(src_new, "A new Python script created today.", project_id=proj_new.id))

        cap_sql = Capability(name="PostgreSQL", category=SkillCategory.DATABASE)
        cap_sql.add_evidence(CapabilityEvidence(src_old, "PostgreSQL relational schemas with query optimization.", project_id=proj_old.id))

        cand = CandidateProfile(
            id="cand",
            name="Candidate",
            graduation_year=2026,
            projects=[proj_old, proj_new],
            capabilities={"python": cap_python, "postgresql": cap_sql},
        )

        job = _make_job("Backend Engineer", ["Python", "PostgreSQL"])
        matches, _, _ = select_top_projects_hybrid(job, cand)

        self.assertGreater(len(matches), 0)
        self.assertEqual(matches[0].project.id, "p-old", "Older project with strong JD relevance must outrank recent weak project")

    def test_6_sparse_compatible_skill_groups_merge_without_inventing_skills(self) -> None:
        """Requirement 6: Sparse compatible skill groups merge without inventing skills."""
        cats = {
            "python": SkillCategory.LANGUAGE,
            "typescript": SkillCategory.LANGUAGE,
            "fastapi": SkillCategory.BACKEND,
            "react": SkillCategory.FRONTEND,
            "postgresql": SkillCategory.DATABASE,
            "docker": SkillCategory.CLOUD,
            "tcp/ip": SkillCategory.NETWORK,
        }
        ordered = ["Python", "TypeScript", "FastAPI", "React", "PostgreSQL", "Docker", "TCP/IP"]
        groups = group_skills(ordered, cats)

        # Embedded & Networking had only 1 skill (TCP/IP), so it should merge with Cloud, DevOps & Tooling -> Cloud, Systems & DevOps
        labels = [g.label for g in groups]
        self.assertNotIn("Embedded & Networking", labels)
        self.assertIn("Cloud, Systems & DevOps", labels)

        # Check all skills are preserved exactly in order without inventing or dropping
        all_rendered_skills = [s for g in groups for s in g.skills]
        self.assertEqual(all_rendered_skills, ordered)

    def test_7_bounded_narrow_layout_preset_geometry(self) -> None:
        """Requirement 7: The bounded ~9-10mm layout preset remains exactly one A4 page and unclipped."""
        preset = next((p for p in LAYOUT_PRESETS if p.name == "narrow"), None)
        self.assertIsNotNone(preset)
        self.assertEqual(preset.margin_mm, 10.0)
        self.assertGreaterEqual(preset.body_pt, 9.5)

        from jobintel.matching.cv_document import CVDocument, ProjectBlock
        doc = CVDocument(
            name="FENGZHE LI",
            headline="Graduate Software Engineer",
            contact_line="07770 000000 | test@example.com | London",
            summary="Graduate software engineer with experience across Python, TypeScript, Docker, and distributed systems.",
            education=[
                DatedEntry("University of Example: MSc Computer Science", "2025 – 2026", ["Distributed Systems", "Cloud Computing"])
            ],
            skill_groups=[
                SkillGroup("Programming", ["Python", "TypeScript"]),
                SkillGroup("Cloud, Systems & DevOps", ["Docker", "Linux", "TCP/IP"]),
            ],
            projects=[
                ProjectBlock(
                    project_id="p1",
                    name="Financial Knowledge Intelligence Platform",
                    technologies=["Python", "FastAPI", "Docker"],
                    bullets=[
                        type("B", (), {"text": "Engineered high-throughput financial data pipeline serving analytical queries."})(),
                        type("B", (), {"text": "Implemented transactional persistence and deterministic source grounding."})(),
                    ],
                )
            ],
            experience=[
                DatedEntry("Software Intern, Example Corp", "Jun 2024 – Aug 2024", ["Built internal data validation tooling."])
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "narrow_cv.pdf"
            render_res = render_cv_document(pdf_path, doc, preset=preset)

            self.assertEqual(render_res.page_count, 1)
            self.assertEqual(render_res.margin_mm, 10.0)
            self.assertGreaterEqual(render_res.body_pt, 9.5)

            reader = PdfReader(pdf_path)
            runs = _text_runs(reader.pages[0])
            self.assertTrue(runs)
            left_margin_pt = preset.margin_mm * mm
            right_margin_pt = A4[0] - preset.margin_mm * mm

            # All text runs must be within page boundaries
            for x_start, x_end, y in runs:
                self.assertGreaterEqual(x_start, left_margin_pt - 1.0)
                self.assertLessEqual(x_end, right_margin_pt + 1.0)

    def test_8_semantically_redundant_same_project_bullets_not_used_merely_to_fill_quota(self) -> None:
        """Requirement 8: Semantically redundant same-project bullets are not used merely to fill a quota."""
        src = EvidenceSource("s1", EvidenceSourceType.README_MARKDOWN, "Smart Spill")
        proj = Project("proj-spill", "Edge-to-Cloud Smart Spill Detection", "IoT detection")

        # Two quotes sharing the exact clause: "Python Lambda functions that deduplicate and persist events"
        q1 = "An event-driven edge-to-cloud backend: MQTT devices publish through AWS IoT Core, IoT Rules fan out to Python Lambda functions that deduplicate and persist events."
        q2 = "Python Lambda functions that deduplicate and persist events, with transactional retry deduplication and timestamp-guarded updates keeping historical records separate from current sensor state."
        q3 = "102 tests passed locally, including 15 real PostgreSQL integration tests and four DynamoDB-emulator integration tests."

        cap_py = Capability(name="Python", category=SkillCategory.LANGUAGE)
        cap_py.add_evidence(CapabilityEvidence(src, q1, project_id=proj.id))
        cap_py.add_evidence(CapabilityEvidence(src, q2, project_id=proj.id))
        cap_py.add_evidence(CapabilityEvidence(src, q3, project_id=proj.id))

        cand = CandidateProfile(id="cand", name="Candidate", graduation_year=2026, projects=[proj], capabilities={"python": cap_py})
        job = _make_job("IoT Backend Engineer", ["Python"])
        jd = extract_jd_requirements(job)
        match = ProjectMatch(project=proj, score=3.0, matched_requirements=["Python"], missing_requirements=[], explanation="")

        bullets = _compose_bullets_for_project(match, cand, jd)
        bullet_texts = [b.text for b in bullets]

        # The duplicate Lambda deduplication quote (q2) must be excluded
        self.assertIn(q1, bullet_texts)
        self.assertNotIn(q2, bullet_texts, "Overlapping duplicate clause quote must be pruned even if space exists")
        self.assertIn(q3, bullet_texts)
        self.assertEqual(len(bullets), 2, "Only distinct non-redundant bullets are kept; ceilings are not quotas")


if __name__ == "__main__":
    unittest.main()
