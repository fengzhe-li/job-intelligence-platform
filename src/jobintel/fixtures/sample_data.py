from __future__ import annotations

from datetime import datetime, timezone

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.models.candidate import (
    CVVersion,
    CandidateProfile,
    Capability,
    CapabilityEvidence,
    Education,
    EvidenceSource,
    Experience,
    Project,
)
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import EvidenceSourceType, RoleTrack, SkillCategory, WorkMode


def sample_candidate() -> CandidateProfile:
    profile = CandidateProfile(id="candidate-personal", name="Personal Candidate", graduation_year=2026)
    project_source = EvidenceSource(
        id="project-financial-knowledge",
        source_type=EvidenceSourceType.MANUAL_PROJECT_DESCRIPTION,
        title="Financial Knowledge Intelligence Platform",
    )
    readme_source = EvidenceSource(
        id="readme-job-platform",
        source_type=EvidenceSourceType.README_MARKDOWN,
        title="Job Intelligence Platform README",
    )
    education_source = EvidenceSource(
        id="education-msc",
        source_type=EvidenceSourceType.EDUCATION_DESCRIPTION,
        title="MSc Electronic and Communication Engineering",
    )

    profile.projects.extend(
        [
            Project(
                id="financial-knowledge",
                name="Financial Knowledge Intelligence Platform",
                description="Python backend with PostgreSQL persistence, Alembic migrations, document ingestion, and retrieval workflows.",
                evidence_sources=[project_source],
                role_relevance={
                    RoleTrack.BACKEND_ENGINEERING: 0.9,
                    RoleTrack.DATA_ENGINEERING: 0.75,
                    RoleTrack.AI_ML_ENGINEERING: 0.55,
                },
            ),
            Project(
                id="job-intelligence",
                name="Job Intelligence Platform",
                description="Multi-source ingestion, canonical job schema, explainable ranking, and future Spark/Airflow pipeline design.",
                evidence_sources=[readme_source],
                role_relevance={
                    RoleTrack.DATA_PLATFORM: 0.9,
                    RoleTrack.DATA_ENGINEERING: 0.85,
                    RoleTrack.SOFTWARE_ENGINEERING: 0.7,
                },
            ),
        ]
    )

    for name, category, source, quote, relevance in [
        (
            "Python",
            SkillCategory.LANGUAGE,
            project_source,
            "Built Python backend services for document ingestion and retrieval workflows.",
            {RoleTrack.BACKEND_ENGINEERING: 0.9, RoleTrack.DATA_ENGINEERING: 0.75},
        ),
        (
            "PostgreSQL",
            SkillCategory.DATABASE,
            project_source,
            "Implemented PostgreSQL persistence with Alembic migrations.",
            {RoleTrack.BACKEND_ENGINEERING: 0.85, RoleTrack.DATA_ENGINEERING: 0.7},
        ),
        (
            "SQL",
            SkillCategory.DATA,
            project_source,
            "Designed relational data models and query workflows.",
            {RoleTrack.DATA_ENGINEERING: 0.85, RoleTrack.DATA_PLATFORM: 0.7},
        ),
        (
            "Spark",
            SkillCategory.DATA,
            readme_source,
            "Designed Spark processing for historical job snapshots and Parquet analytics.",
            {RoleTrack.DATA_ENGINEERING: 0.9, RoleTrack.DATA_PLATFORM: 0.9},
        ),
        (
            "Airflow",
            SkillCategory.DATA,
            readme_source,
            "Planned Airflow orchestration for ingestion, validation, normalization, and ranking jobs.",
            {RoleTrack.DATA_ENGINEERING: 0.85, RoleTrack.PLATFORM_ENGINEERING: 0.55},
        ),
        (
            "React",
            SkillCategory.FRONTEND,
            readme_source,
            "Created frontend prototypes for bilingual job intelligence workflows.",
            {RoleTrack.FRONTEND_ENGINEERING: 0.75, RoleTrack.FULL_STACK_ENGINEERING: 0.65},
        ),
        (
            "AWS",
            SkillCategory.CLOUD,
            readme_source,
            "Documented cloud-ready service and data lake deployment boundaries.",
            {RoleTrack.CLOUD_ENGINEERING: 0.65, RoleTrack.PLATFORM_ENGINEERING: 0.6},
        ),
        (
            "TCP/IP",
            SkillCategory.NETWORK,
            education_source,
            "Studied communication networks, TCP/IP, radio systems, and connected systems.",
            {RoleTrack.NETWORK_ENGINEERING: 0.75, RoleTrack.TELECOMMUNICATIONS: 0.7},
        ),
        (
            "5G",
            SkillCategory.NETWORK,
            education_source,
            "Covered 5G telecommunications and networked communication systems.",
            {RoleTrack.TELECOMMUNICATIONS: 0.8, RoleTrack.NETWORK_ENGINEERING: 0.5},
        ),
        (
            "C++",
            SkillCategory.LANGUAGE,
            education_source,
            "Completed embedded and communication engineering coursework using C++.",
            {RoleTrack.EMBEDDED_SOFTWARE: 0.65, RoleTrack.IOT: 0.55},
        ),
    ]:
        profile.upsert_capability(
            Capability(
                name=name,
                category=category,
                evidence=[CapabilityEvidence(source=source, quote=quote)],
                role_relevance=relevance,
            )
        )

    profile.education.append(
        Education(
            institution="UK University",
            programme="MSc Electronic and Communication Engineering",
            graduation_year=2026,
            description="Electronic communications, networks, IoT, connected systems, and engineering projects.",
            evidence_source=education_source,
        )
    )
    profile.experience.append(
        Experience(
            organisation="Internship Employer",
            title="Software/Data Intern",
            description="Built Python data processing tools and SQL-backed internal applications.",
        )
    )
    cv_source = EvidenceSource(id="cv-versions", source_type=EvidenceSourceType.CV_TEXT, title="CV base templates")
    profile.cv_versions.extend(
        [
            CVVersion(id="cv-backend", category="Software/Backend", text="Python backend APIs and databases.", evidence_source=cv_source),
            CVVersion(id="cv-data", category="Data", text="SQL, Spark, Airflow, data pipelines.", evidence_source=cv_source),
            CVVersion(id="cv-platform", category="Cloud/Platform", text="Cloud, platform, orchestration.", evidence_source=cv_source),
            CVVersion(id="cv-ai", category="AI/ML", text="AI systems and retrieval workflows.", evidence_source=cv_source),
        ]
    )
    return profile


def sample_jobs() -> list[Job]:
    specs = [
        (
            "backend-london-sponsor",
            "Backend Engineer",
            "FinTech Direct",
            "London",
            WorkMode.HYBRID,
            "Build backend APIs in Python with PostgreSQL, REST services and Docker. Visa sponsorship is available for strong candidates. No specific graduation year restriction.",
            "greenhouse",
        ),
        (
            "data-engineer-manchester",
            "Data Engineer",
            "Health Data Labs",
            "Manchester",
            WorkMode.HYBRID,
            "Develop ETL pipeline systems using SQL, Spark, PySpark and Airflow. Recent graduate applicants are welcome.",
            "lever",
        ),
        (
            "software-data-platform",
            "Software Engineer - Data Platform",
            "Retail Analytics",
            "London",
            WorkMode.ONSITE,
            "Work on software engineering for a data platform with Python, SQL, Spark, Parquet and Kubernetes. Skilled worker visa sponsorship is available.",
            "company",
        ),
        (
            "fullstack-engineer",
            "Full-stack Engineer",
            "Product Studio",
            "Bristol",
            WorkMode.REMOTE,
            "Build React frontend features and backend APIs with TypeScript, Node.js and PostgreSQL.",
            "adzuna",
        ),
        (
            "frontend-engineer",
            "Frontend Engineer",
            "Design SaaS",
            "London",
            WorkMode.HYBRID,
            "Create React and TypeScript UI components. CSS and accessibility experience preferred. Sponsorship unknown.",
            "lever",
        ),
        (
            "cloud-platform-engineer",
            "Cloud/Platform Engineer",
            "Energy Tech",
            "Edinburgh",
            WorkMode.HYBRID,
            "Develop cloud platform tooling using AWS, Kubernetes, Terraform, Linux and CI/CD. Must have the right to work in the UK without sponsorship.",
            "greenhouse",
        ),
        (
            "network-software-engineer",
            "Network Software Engineer",
            "Internet Infrastructure Co",
            "London",
            WorkMode.ONSITE,
            "Build network automation and packet processing software with Python, Go, TCP/IP and BGP for edge network systems.",
            "company",
        ),
        (
            "telecom-engineer",
            "Telecommunications Engineer",
            "5G Systems",
            "Cambridge",
            WorkMode.HYBRID,
            "Work on telecom, 5G radio systems, communication engineering, signal testing and network analysis.",
            "adzuna",
        ),
        (
            "iot-embedded",
            "IoT/Embedded Software Engineer",
            "Connected Devices Ltd",
            "London",
            WorkMode.HYBRID,
            "Develop embedded firmware in C++ for IoT sensor devices using MQTT and edge computing patterns.",
            "lever",
        ),
        (
            "strict-2027-grad",
            "2027 Software Engineering Graduate Programme",
            "Future Bank",
            "London",
            WorkMode.HYBRID,
            "Python and Java graduate programme. 2027 graduates only. Applications require graduation in 2027.",
            "greenhouse",
        ),
        (
            "sales-engineer-excluded",
            "Graduate Sales Engineer",
            "Commercial Tech",
            "London",
            WorkMode.ONSITE,
            "Sales role focused on lead generation, front desk demos and account development.",
            "adzuna",
        ),
    ]
    return [enrich_job(_job(*spec), 2026) for spec in specs]


def _job(
    job_id: str,
    title: str,
    company: str,
    city: str,
    work_mode: WorkMode,
    description: str,
    source: str,
) -> Job:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    return Job(
        id=job_id,
        title=title,
        company=company,
        description=description,
        locations=[Location(city=city, country="United Kingdom", work_mode=work_mode, raw=f"{city}, UK")],
        source_observations=[
            SourceObservation(
                source_name=source,
                source_job_id=job_id,
                original_url=f"https://jobs.example.com/{job_id}",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description=description,
                canonical_application_url=f"https://apply.example.com/{job_id}",
            )
        ],
    )

