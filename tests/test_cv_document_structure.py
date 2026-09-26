"""Regression: a real tailored CV rendered only contact + skills + projects,
with "…"-truncated project headings/bullets, on a half-empty page -- and still
reported "fits one page: True" / AUTO_PREPARE.

A tailored graduate CV must keep its core structure (summary, education, skills,
key projects, internship experience), fit exactly one A4 page, actually use
that page, and never character-slice recruiter-facing text.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.matching.cv_document import parse_authored_cv
from jobintel.matching.cv_generation import MIN_PAGE_UTILIZATION, generate_and_save_cv, generate_cv
from jobintel.models.candidate import (
    CandidateProfile,
    Capability,
    CapabilityEvidence,
    CVVersion,
    EvidenceSource,
    Project,
)
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import EvidenceSourceType, ReviewGateStatus, RoleTrack, SkillCategory
from jobintel.storage.cv_artifact_store import CVArtifactStore

AUTHORED_CV = """ALEX EXAMPLE | Graduate Software Engineer
07700 000000 | alex@example.com
PROFESSIONAL SUMMARY
Graduate Software Engineer with experience building backend services and cloud applications using Python and SQL. Skilled in REST APIs, databases and automated testing.
EDUCATION
Example University: MSc Computer SystemsSep 2025 - Sep 2026 (Expected)
Relevant Modules (MSc): Distributed Systems, Machine Learning, Cloud Computing, Software Architecture
Sample Institute: BEng Software Engineering (First Class)  — Sep 2021 – Jun 2024
Relevant Modules (BEng): Programming, Data Structures, Databases, Operating Systems
TECHNICAL SKILLS
Programming Languages: Python, SQL
KEY PROJECTS
Old Project
Did an old thing that should not be copied from the authored CV.
INTERNSHIP EXPERIENCE
Software Intern, Example Ltd (UK)Jun 2024 - Aug 2024
-Built internal Python tooling to validate operational data and automate weekly reporting for the operations team.
- Investigated recurring data quality issues and documented fixes with the engineering team.
Data Intern                             — Sample Org (UK) Jul 2023 – Aug 2023
- Wrote SQL queries to reconcile inventory records and prepared validation summaries for analysts.
"""

# Four projects, four long evidence quotes each: more than one page can hold,
# so the fitter must reduce semantically.
PROJECT_EVIDENCE = {
    "proj-api": (
        "Order Service Platform",
        [
            ("Python", "FastAPI backend exposing REST endpoints for order intake, validation and fulfilment tracking with structured error responses and request tracing."),
            ("PostgreSQL", "PostgreSQL persistence on SQLAlchemy v2.0 models with Alembic migrations, e.g. schema changes for orders and audit history tables."),
            ("Docker", "Docker Compose stack with CI running unit, integration and contract tests against a real PostgreSQL instance on every push."),
            ("Redis", "Redis caching layer for catalogue lookups with explicit invalidation on price and stock updates, e.g. after bulk imports."),
        ],
    ),
    "proj-cloud": (
        "Serverless Event Pipeline",
        [
            ("AWS", "Event-driven AWS pipeline where Lambda functions consume SQS messages, deduplicate them and persist results to DynamoDB tables."),
            ("DynamoDB", "DynamoDB conditional writes and timestamp guards preserving state integrity under retries, duplicate delivery and out-of-order events."),
            ("Python", "Python handlers with structured JSON logging, typed configuration and dead-letter handling for poison messages."),
            ("Terraform", "Terraform modules provisioning queues, functions, tables and IAM roles with separate staging and production workspaces."),
        ],
    ),
    "proj-ml": (
        "Document Retrieval Service",
        [
            ("Python", "Retrieval service indexing technical documents with sentence embeddings and returning cited passages for each answer."),
            ("PyTorch", "PyTorch embedding model fine-tuned on labelled query pairs, evaluated with recall@k on a held-out split."),
            ("FastAPI", "FastAPI inference endpoint batching embedding requests and streaming ranked results to a React client."),
            ("Docker", "Containerised deployment with health checks, pinned model artefacts and reproducible evaluation scripts."),
        ],
    ),
    "proj-data": (
        "Batch Reporting Pipeline",
        [
            ("Python", "Python batch jobs extracting operational exports, validating schemas and loading curated tables for weekly reporting across several business teams."),
            ("SQL", "SQL transformations computing reconciled daily aggregates with window functions, idempotent upserts and documented data contracts for downstream analysts."),
            ("Python", "Data quality checks flagging missing partitions, duplicate keys and late-arriving records before any report is published to stakeholders."),
            ("Airflow", "Airflow DAGs scheduling extraction, validation and publication steps with retries, alerting and run-level lineage metadata."),
        ],
    ),
    "proj-embedded": (
        "Sensor Firmware Prototype",
        [
            ("C", "Embedded C firmware for an ARM Cortex-M microcontroller using hardware timers and interrupt-driven peripheral handling."),
            ("C", "I2C and SPI drivers reading accelerometer samples and configuring sensor range registers at start-up."),
            ("C", "Threshold-based event detection algorithm with a post-event stillness check to reduce false positives."),
            ("C", "UART telemetry interface transmitting framed data packets and alert messages to a host logger."),
        ],
    ),
}

# Two more JD-relevant projects: with six selected projects the page must overflow.
EXTRA_PROJECTS = {
    "proj-auth": (
        "Identity and Access Service",
        [
            ("Python", "Python authentication service issuing short-lived tokens, rotating signing keys and enforcing role-based permissions per tenant."),
            ("PostgreSQL", "PostgreSQL schema for users, roles and audit events with row-level security policies and migration tests."),
            ("AWS", "AWS deployment behind an application load balancer with secrets held in a managed parameter store and rotated automatically."),
            ("Docker", "Docker images for the token service with multi-stage builds, non-root users and health checks used by the orchestrator."),
        ],
    ),
    "proj-search": (
        "Product Search Indexer",
        [
            ("Python", "Python indexer consuming catalogue change events and rebuilding search documents incrementally without full reindexing."),
            ("PostgreSQL", "PostgreSQL logical replication feed used as the source of truth for indexing, with checkpointed offsets for restarts."),
            ("AWS", "AWS OpenSearch cluster sizing experiments comparing shard layouts, refresh intervals and query latency percentiles."),
            ("Docker", "Docker Compose environment reproducing the indexer, replication source and search cluster for integration testing."),
        ],
    ),
}

CATEGORIES = {
    "python": SkillCategory.LANGUAGE,
    "c": SkillCategory.LANGUAGE,
    "postgresql": SkillCategory.DATABASE,
    "dynamodb": SkillCategory.DATABASE,
    "redis": SkillCategory.DATABASE,
    "docker": SkillCategory.DEVOPS,
    "terraform": SkillCategory.CLOUD,
    "aws": SkillCategory.CLOUD,
    "pytorch": SkillCategory.ML_AI,
    "sql": SkillCategory.DATA,
    "airflow": SkillCategory.DATA,
    "fastapi": SkillCategory.BACKEND,
}


def _job(description: str = "Required: Python, PostgreSQL, AWS. Preferred: Docker, FastAPI.", title: str = "Graduate Backend Engineer") -> Job:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    job = Job(
        id="job-structure",
        title=title,
        company="Example Co",
        description=description,
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name="company",
                source_job_id="job-structure",
                original_url="https://example.com/jobs/structure",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description=description,
                canonical_application_url="https://example.com/jobs/structure/apply",
            )
        ],
    )
    return enrich_job(job, 2026)


def _candidate(with_authored_cv: bool = True, projects: dict | None = None, authored_cv: str = AUTHORED_CV) -> CandidateProfile:
    profile = CandidateProfile(id="candidate-structure", name="Alex Example", graduation_year=2026)
    profile.email = "alex@example.com"
    profile.phone = "07700 000000"
    profile.github_url = "https://github.com/alex-example"
    if with_authored_cv:
        source = EvidenceSource(id="cv-source-1", source_type=EvidenceSourceType.CV_TEXT, title="Software CV", uri="cv/software.docx")
        profile.cv_versions.append(CVVersion(id="cv-source-1", category="Software/Backend", text=authored_cv, evidence_source=source))
    for project_id, (name, quotes) in (PROJECT_EVIDENCE if projects is None else projects).items():
        source = EvidenceSource(
            id=project_id,
            source_type=EvidenceSourceType.README_MARKDOWN,
            title=name,
            uri=f"https://github.com/alex-example/{project_id}",
            collected_at=date(2026, 1, 1),
        )
        profile.projects.append(Project(id=project_id, name=name, description=f"{name} description", evidence_sources=[source]))
        for index, (skill, quote) in enumerate(quotes):
            profile.upsert_capability(
                Capability(
                    name=skill,
                    category=CATEGORIES.get(skill.casefold(), SkillCategory.TOOLING),
                    evidence=[CapabilityEvidence(source=source, quote=quote, project_id=project_id, confidence=1.0, id=f"{project_id}:{index}")],
                    technology=skill,
                    role_relevance={RoleTrack.BACKEND_ENGINEERING: 0.8} if skill != "C" else {},
                )
            )
    return profile


def _pdf_text(path: str) -> tuple[int, str]:
    reader = PdfReader(path)
    return len(reader.pages), "\n".join(page.extract_text() for page in reader.pages)


def _source_quotes(candidate: CandidateProfile) -> list[str]:
    return [evidence.quote for capability in candidate.capabilities.values() for evidence in capability.evidence]


class CVDocumentStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_graduate_cv_contains_every_core_section_on_exactly_one_pdf_page(self) -> None:
        artifact = generate_cv(_candidate(), _job(), store_root=self.tmp)
        page_count, text = _pdf_text(artifact.pdf_path)

        self.assertEqual(page_count, 1)
        self.assertEqual(artifact.page_count, 1)
        self.assertEqual(artifact.document["sections_missing"], [])
        for heading in ("PROFESSIONAL SUMMARY", "EDUCATION", "TECHNICAL SKILLS", "KEY PROJECTS", "INTERNSHIP EXPERIENCE"):
            self.assertIn(heading, text)
            self.assertIn(heading, artifact.cv_text)
        self.assertIn("alex@example.com", text)
        self.assertIn("Graduate Software Engineer with experience building backend services", artifact.cv_text)
        self.assertIn("Example University: MSc Computer Systems", artifact.cv_text)
        self.assertIn("Sep 2025 – Sep 2026 (Expected)", artifact.cv_text)
        self.assertIn("Relevant Modules (MSc): Distributed Systems", artifact.cv_text)
        self.assertIn("Software Intern, Example Ltd (UK)", artifact.cv_text)
        # Projects come from the Evidence Bank, never copied from the authored CV.
        self.assertNotIn("Old Project", artifact.cv_text)
        self.assertEqual(artifact.document["base_cv_source_id"], "cv-source-1")
        self.assertEqual(artifact.review_status, ReviewGateStatus.AUTO_PREPARE, artifact.review_reasons)

    def test_one_page_cv_actually_uses_the_page(self) -> None:
        artifact = generate_cv(_candidate(), _job(), store_root=self.tmp)

        self.assertGreaterEqual(artifact.page_utilization, MIN_PAGE_UTILIZATION)

    def test_no_truncation_generated_ellipses_in_recruiter_facing_text(self) -> None:
        candidate = _candidate()
        artifact = generate_cv(candidate, _job(), store_root=self.tmp)
        _, pdf_text = _pdf_text(artifact.pdf_path)
        quotes = _source_quotes(candidate)
        project_names = {project.name for project in candidate.projects}

        self.assertNotIn("…", artifact.cv_text)
        self.assertNotIn("…", pdf_text)
        for project in artifact.document["projects"]:
            self.assertIn(project["name"], project_names)
            for technology in project["technologies"]:
                self.assertIn(technology.casefold(), candidate.capabilities)
        for bullet in artifact.bullets:
            # Complete evidence text: the whole quote, or a whole sentence of it.
            source = next((quote for quote in quotes if bullet.text in quote), None)
            self.assertIsNotNone(source, f"bullet not verbatim evidence: {bullet.text!r}")
            self.assertTrue(bullet.text == source or bullet.text.endswith("."), f"bullet cut mid-phrase: {bullet.text!r}")

    def test_legitimate_dots_in_source_text_are_preserved(self) -> None:
        artifact = generate_cv(_candidate(), _job(), store_root=self.tmp)
        texts = [bullet.text for bullet in artifact.bullets]

        self.assertIn(
            "PostgreSQL persistence on SQLAlchemy v2.0 models with Alembic migrations, e.g. schema changes for orders and audit history tables.",
            texts,
        )

    def test_readme_inventory_prefix_is_dropped_as_a_whole_sentence(self) -> None:
        projects = {
            "proj-api": (
                "Order Service Platform",
                [("Python", "- Backend: Python, FastAPI, Pydantic. FastAPI backend providing REST endpoints for orders and fulfilment tracking.")],
            )
        }
        artifact = generate_cv(_candidate(projects=projects), _job(), store_root=self.tmp)

        self.assertEqual([bullet.text for bullet in artifact.bullets], ["FastAPI backend providing REST endpoints for orders and fulfilment tracking."])

    def _dense(self):
        candidate = _candidate(projects={**PROJECT_EVIDENCE, **EXTRA_PROJECTS})
        return candidate, generate_cv(candidate, _job(), store_root=self.tmp, max_projects=6)

    def test_overflow_is_reduced_semantically_not_by_character_slicing(self) -> None:
        candidate, artifact = self._dense()
        quotes = _source_quotes(candidate)

        self.assertTrue(any(action.startswith("overflow:") for action in artifact.fit_actions))
        self.assertLess(len(artifact.bullets), len(quotes))
        for bullet in artifact.bullets:
            self.assertTrue(any(bullet.text in quote for quote in quotes))
            self.assertFalse(bullet.text.endswith(("…", "...")))

    def test_focus_projects_can_retain_more_bullets_than_secondary_projects(self) -> None:
        _, artifact = self._dense()
        projects = artifact.document["projects"]
        focus = [len(project["bullets"]) for project in projects if project["emphasis"] == "focus"]
        secondary = [len(project["bullets"]) for project in projects if project["emphasis"] == "secondary"]

        self.assertEqual(artifact.page_count, 1)
        self.assertTrue(focus and secondary)
        self.assertGreater(max(focus), max(secondary))
        self.assertGreaterEqual(min(focus), 3)
        # Page space follows priority: focus projects are listed first.
        self.assertEqual([project["emphasis"] for project in projects], sorted((project["emphasis"] for project in projects), key=lambda tier: tier != "focus"))

    def test_weakly_related_internship_detail_loses_budget_before_focus_evidence(self) -> None:
        _, artifact = self._dense()
        experience = {entry["heading"]: entry["details"] for entry in artifact.document["experience"]}
        focus_drops = [index for index, action in enumerate(artifact.fit_actions) if "focus bullet" in action or "protected bullet" in action]
        internship_cuts = [index for index, action in enumerate(artifact.fit_actions) if "internship" in action or action.startswith("budget:")]

        # The internship bullet with no JD relevance is gone; its role, company
        # and dates stay. The JD-relevant (Python) internship bullet stays.
        self.assertIn("Software Intern, Example Ltd (UK)", experience)
        self.assertNotIn("Investigated recurring data quality issues and documented fixes with the engineering team.", experience["Software Intern, Example Ltd (UK)"])
        self.assertIn(
            "Built internal Python tooling to validate operational data and automate weekly reporting for the operations team.",
            experience["Software Intern, Example Ltd (UK)"],
        )
        self.assertTrue(internship_cuts)
        self.assertTrue(not focus_drops or min(internship_cuts) < min(focus_drops))

    def test_bullet_maxima_are_ceilings_not_quotas(self) -> None:
        projects = {
            "proj-api": ("Order Service Platform", PROJECT_EVIDENCE["proj-api"][1][:2]),
            "proj-cloud": ("Serverless Event Pipeline", PROJECT_EVIDENCE["proj-cloud"][1][:1]),
        }
        artifact = generate_cv(_candidate(projects=projects), _job(), store_root=self.tmp)
        counts = {project["project_id"]: len(project["bullets"]) for project in artifact.document["projects"]}

        # Plenty of page space, yet no project gets more bullets than it has
        # distinct evidence for.
        self.assertEqual(counts, {"proj-api": 2, "proj-cloud": 1})
        _, dense = self._dense()
        for project in dense.document["projects"]:
            self.assertLessEqual(len(project["bullets"]), 4 if project["emphasis"] == "focus" else 3)

    def test_no_unsupported_or_repetitive_filler_is_added_to_fill_the_page(self) -> None:
        repeated = "FastAPI backend exposing REST endpoints for order intake, validation and fulfilment tracking with structured error responses."
        projects = {
            "proj-api": (
                "Order Service Platform",
                [
                    ("Python", PROJECT_EVIDENCE["proj-api"][1][0][1]),
                    ("FastAPI", repeated),  # near-duplicate of the first quote
                ],
            )
        }
        candidate = _candidate(projects=projects)
        artifact = generate_cv(candidate, _job(), store_root=self.tmp)
        quotes = _source_quotes(candidate)

        self.assertEqual([bullet.text for bullet in artifact.bullets], [PROJECT_EVIDENCE["proj-api"][1][0][1]])
        for bullet in artifact.bullets:
            self.assertIn(bullet.text, quotes)
        # The page stays honestly under-filled and is flagged, not padded.
        self.assertLess(artifact.page_utilization, MIN_PAGE_UTILIZATION)
        self.assertNotEqual(artifact.review_status, ReviewGateStatus.AUTO_PREPARE)

    def test_stronger_jd_relevant_content_survives_before_weaker_content(self) -> None:
        artifact = generate_cv(_candidate(), _job(), store_root=self.tmp)

        # The off-JD embedded project is the weakest and goes before any
        # JD-relevant project; the JD-must-have evidence survives.
        self.assertNotIn("proj-embedded", artifact.selected_project_ids)
        self.assertIn("proj-api", artifact.selected_project_ids)
        self.assertIn("proj-cloud", artifact.selected_project_ids)
        used = set(artifact.evidence_ids_used)
        self.assertIn("proj-api:1", used)  # PostgreSQL (must-have)
        self.assertIn("proj-cloud:0", used)  # AWS (must-have)
        # Education and internship experience are never removed to keep project content.
        self.assertIn("education", artifact.document["sections_present"])
        self.assertIn("experience", artifact.document["sections_present"])

    def test_missing_optional_sections_are_reported_never_invented(self) -> None:
        candidate = _candidate(with_authored_cv=False, projects={"proj-api": PROJECT_EVIDENCE["proj-api"]})
        artifact = generate_cv(candidate, _job(), store_root=self.tmp)
        _, pdf_text = _pdf_text(artifact.pdf_path)

        self.assertEqual(artifact.document["sections_missing"], ["summary", "education", "experience"])
        for heading in ("PROFESSIONAL SUMMARY", "EDUCATION", "INTERNSHIP EXPERIENCE"):
            self.assertNotIn(heading, pdf_text)
        self.assertNotEqual(artifact.review_status, ReviewGateStatus.AUTO_PREPARE)
        self.assertTrue(any("missing core section" in reason for reason in artifact.review_reasons))

    def test_under_filled_page_fails_review_even_though_it_is_one_page(self) -> None:
        candidate = _candidate(with_authored_cv=False, projects={"proj-api": ("Order Service Platform", PROJECT_EVIDENCE["proj-api"][1][:1])})
        artifact = generate_cv(candidate, _job(), store_root=self.tmp)

        self.assertEqual(artifact.page_count, 1)
        self.assertLess(artifact.page_utilization, MIN_PAGE_UTILIZATION)
        self.assertTrue(any("of the page height" in reason for reason in artifact.review_reasons))
        self.assertNotEqual(artifact.review_status, ReviewGateStatus.AUTO_PREPARE)

    def test_regeneration_creates_new_immutable_version_with_structured_document(self) -> None:
        store = CVArtifactStore(self.tmp)
        first = generate_and_save_cv(_candidate(), _job(), store, store_root=self.tmp)
        first_bytes = Path(first.pdf_path).read_bytes()
        second = generate_and_save_cv(_candidate(), _job(), store, store_root=self.tmp)

        self.assertNotEqual(first.id, second.id)
        reloaded = store.get(first.id)
        self.assertEqual(reloaded.document, first.document)
        self.assertEqual(reloaded.page_utilization, first.page_utilization)
        self.assertEqual(reloaded.fit_actions, first.fit_actions)
        self.assertEqual(Path(first.pdf_path).read_bytes(), first_bytes)

    def test_artifacts_persisted_before_structured_documents_still_load(self) -> None:
        store = CVArtifactStore(self.tmp)
        artifact = generate_and_save_cv(_candidate(), _job(), store, store_root=self.tmp)
        payload = json.loads(store.path.read_text(encoding="utf-8").splitlines()[0])
        for key in ("document", "page_utilization", "fit_actions", "document_format"):
            payload.pop(key)
        store.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        legacy = store.get(artifact.id)
        self.assertEqual(legacy.document, {})
        self.assertIsNone(legacy.page_utilization)
        self.assertEqual(legacy.document_format, 1)
        self.assertEqual(artifact.document_format, 2)


class AuthoredCVParsingTests(unittest.TestCase):
    def test_parses_sections_dates_and_bullets_verbatim(self) -> None:
        parsed = parse_authored_cv(AUTHORED_CV)

        self.assertEqual(parsed.headline, "Graduate Software Engineer")
        self.assertTrue(parsed.summary.startswith("Graduate Software Engineer with experience"))
        self.assertEqual([entry.heading for entry in parsed.education], ["Example University: MSc Computer Systems", "Sample Institute: BEng Software Engineering (First Class)"])
        self.assertEqual(parsed.education[0].dates, "Sep 2025 – Sep 2026 (Expected)")
        self.assertEqual(parsed.education[1].details, ["Relevant Modules (BEng): Programming, Data Structures, Databases, Operating Systems"])
        self.assertEqual([entry.heading for entry in parsed.experience], ["Software Intern, Example Ltd (UK)", "Data Intern — Sample Org (UK)"])
        self.assertEqual(parsed.experience[0].dates, "Jun 2024 – Aug 2024")
        self.assertEqual(
            parsed.experience[0].details[0],
            "Built internal Python tooling to validate operational data and automate weekly reporting for the operations team.",
        )

    def test_text_without_known_sections_yields_nothing(self) -> None:
        parsed = parse_authored_cv("Just some notes\nwith no headings")

        self.assertEqual(parsed.core_section_count(), 0)


if __name__ == "__main__":
    unittest.main()
