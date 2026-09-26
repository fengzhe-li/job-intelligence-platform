from __future__ import annotations

import re
from collections import defaultdict

from jobintel.models.job import RoleTrackProfile, RoleTrackScore, SkillRequirement
from jobintel.models.taxonomy import RoleTrack


TRACK_KEYWORDS: dict[RoleTrack, tuple[str, ...]] = {
    RoleTrack.SOFTWARE_ENGINEERING: ("software engineer", "software developer", "application engineer", "android engineer", "mobile engineer", "api", "code", "testing"),
    RoleTrack.BACKEND_ENGINEERING: ("backend", "back-end", "api", "microservice", "postgres", "python", "java"),
    RoleTrack.FRONTEND_ENGINEERING: ("frontend", "front-end", "react", "typescript", "css", "ui"),
    RoleTrack.FULL_STACK_ENGINEERING: ("full-stack", "full stack"),
    RoleTrack.DATA_ENGINEERING: ("data engineer", "etl", "pipeline", "spark", "airflow", "sql", "warehouse"),
    RoleTrack.DATA_PLATFORM: ("data platform", "platform data", "lakehouse", "parquet", "distributed data"),
    RoleTrack.CLOUD_ENGINEERING: ("cloud", "aws", "azure", "gcp", "kubernetes", "terraform"),
    RoleTrack.PLATFORM_ENGINEERING: ("platform engineer", "developer platform", "kubernetes", "ci/cd"),
    RoleTrack.INFRASTRUCTURE_ENGINEERING: ("infrastructure", "linux", "networking", "systems"),
    RoleTrack.DEVOPS_SRE: ("devops", "sre", "reliability", "observability", "on-call", "ci/cd"),
    RoleTrack.AI_ML_ENGINEERING: ("machine learning", "ml engineer", "ml scientist", "llm", "ai engineer", "pytorch", "tensorflow", "keras"),
    RoleTrack.TELECOMMUNICATIONS: ("telecom", "telecommunications", "5g", "radio", "ran"),
    RoleTrack.NETWORK_ENGINEERING: ("network engineer", "routing", "switching", "tcp/ip", "bgp"),
    RoleTrack.NETWORK_SOFTWARE: ("network software", "sdn", "network automation", "packet", "tcp/ip", "python"),
    RoleTrack.INTERNET_ENGINEERING: ("internet", "dns", "cdn", "edge network", "http"),
    RoleTrack.ELECTRONIC_COMMUNICATION: ("electronic", "communication engineering", "signal", "rf"),
    RoleTrack.IOT: ("iot", "sensor", "connected device", "mqtt"),
    RoleTrack.EMBEDDED_SOFTWARE: ("embedded", "firmware", "c++", "rtos", "microcontroller"),
    RoleTrack.EDGE_CONNECTED_SYSTEMS: ("edge", "connected systems", "edge computing", "device cloud"),
    RoleTrack.MOTORSPORT_ENGINEERING: (
        "motorsport",
        "formula 1",
        "formula one",
        "f1",
        "race engineering",
        "race strategy",
        "vehicle dynamics",
        "telemetry",
        "simulation engineer",
        "trackside",
        "indy 500",
        "indianapolis 500",
        "indycar",
        "qualifying simulation",
        "pit strategy",
        "lap time",
    ),
}

SKILL_PATTERNS: tuple[str, ...] = (
    "Python",
    "Java",
    "C",
    "TypeScript",
    "JavaScript",
    "React",
    "Node.js",
    "FastAPI",
    "PostgreSQL",
    "SQL Server",
    "SQL",
    "Spark",
    "PySpark",
    "Airflow",
    "Kafka",
    "AWS",
    "Azure",
    "GCP",
    "DynamoDB",
    "Docker",
    "Kubernetes",
    "Terraform",
    "Linux",
    "Bash",
    "REST",
    "GraphQL",
    "C++",
    "Go",
    "CRIU",
    "TCP/IP",
    "BGP",
    "5G",
    "MQTT",
    "Pandas",
    "TensorFlow",
    "Keras",
    "PyTorch",
    "LLM",
    # Expanded beyond the original ~38-entry list -- still a fixed, deterministic
    # vocabulary (an open-vocabulary technology recognizer isn't feasible without
    # an LLM), but covers substantially more real-world graduate-SWE JD/evidence
    # text. See docs/CV_GENERATION.md "Capability Recognition".
    "Rust",
    "Ruby",
    "PHP",
    "Swift",
    "Kotlin",
    "Scala",
    "C#",
    ".NET",
    "Perl",
    "Elixir",
    "MATLAB",
    "Django",
    "Flask",
    "Spring",
    "Spring Boot",
    "Express",
    "Ruby on Rails",
    "ASP.NET",
    "Vue",
    "Angular",
    "Next.js",
    "Svelte",
    "Redux",
    "MongoDB",
    "Redis",
    "Cassandra",
    "Elasticsearch",
    "MySQL",
    "SQLite",
    "Oracle",
    "Neo4j",
    "ClickHouse",
    "CockroachDB",
    "Helm",
    "Ansible",
    "Jenkins",
    "GitHub Actions",
    "GitLab CI",
    "CircleCI",
    "Prometheus",
    "Grafana",
    "Istio",
    "Nginx",
    "CloudFormation",
    "Pulumi",
    "RabbitMQ",
    "SQS",
    "SNS",
    "gRPC",
    "WebSocket",
    "scikit-learn",
    "NumPy",
    "OpenCV",
    "Hugging Face",
    "JAX",
    "XGBoost",
    "NLP",
    "Computer Vision",
    "MLflow",
    "dbt",
    "Snowflake",
    "BigQuery",
    "Redshift",
    "Hadoop",
    "Hive",
    "pytest",
    "JUnit",
    "Selenium",
    "Cypress",
    "Jest",
    "React Native",
    "Flutter",
    "Git",
    "Jira",
    "CI/CD",
    "Simulink",
    "LabVIEW",
    "CAN bus",
    "Qdrant",
    "Pydantic",
    "Polars",
    "Playwright",
    "Alembic",
)


def infer_role_track_profile(title: str, description: str) -> RoleTrackProfile:
    title_text = title.casefold()
    text = f"{title}\n{description}".casefold()
    non_engineering_context = _has_non_engineering_context(title_text)
    raw_scores: dict[RoleTrack, float] = defaultdict(float)
    evidence: dict[RoleTrack, list[str]] = defaultdict(list)

    for track, keywords in TRACK_KEYWORDS.items():
        for keyword in keywords:
            if _contains_keyword(text, keyword):
                if non_engineering_context and track in {
                    RoleTrack.SOFTWARE_ENGINEERING,
                    RoleTrack.BACKEND_ENGINEERING,
                    RoleTrack.DATA_ENGINEERING,
                    RoleTrack.AI_ML_ENGINEERING,
                    RoleTrack.NETWORK_SOFTWARE,
                } and keyword.casefold() in {"python", "sql", "api", "code", "testing"}:
                    continue
                raw_scores[track] += 1.0
                evidence[track].append(keyword)
                if _contains_keyword(title_text, keyword):
                    raw_scores[track] += 1.0
                    evidence[track].append(f"title:{keyword}")

    if raw_scores.get(RoleTrack.BACKEND_ENGINEERING) and raw_scores.get(RoleTrack.FRONTEND_ENGINEERING):
        raw_scores[RoleTrack.FULL_STACK_ENGINEERING] += 1.5
        evidence[RoleTrack.FULL_STACK_ENGINEERING].append("backend + frontend evidence")

    total = sum(raw_scores.values())
    if total == 0:
        return RoleTrackProfile([])

    scores = [
        RoleTrackScore(track=track, score=round(score / total, 3), evidence=evidence[track])
        for track, score in raw_scores.items()
    ]
    return RoleTrackProfile(sorted(scores, key=lambda item: item.score, reverse=True))


def extract_skill_requirements(description: str) -> list[SkillRequirement]:
    requirements: list[SkillRequirement] = []
    for skill in SKILL_PATTERNS:
        pattern = re.compile(rf"(?<!\w){re.escape(skill)}(?!\w)", re.IGNORECASE)
        match = pattern.search(description)
        if match:
            sentence = _sentence_containing(description, match.start())
            required = "nice to have" not in sentence.casefold() and "preferred" not in sentence.casefold()
            requirements.append(SkillRequirement(name=skill, required=required, evidence=sentence.strip()))
    return requirements


def _sentence_containing(text: str, index: int) -> str:
    start = max(text.rfind(".", 0, index), text.rfind("\n", 0, index)) + 1
    end_candidates = [position for position in (text.find(".", index), text.find("\n", index)) if position != -1]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end]


def _has_non_engineering_context(title_text: str) -> bool:
    title_markers = (
        "manager",
        "analyst",
        "fp&a",
        "treasury",
        "pricing",
        "risk assurance",
        "investigations",
        "team leader",
        "operations",
    )
    engineering_markers = ("engineer", "developer", "software", "platform", "sre", "devops")
    return any(marker in title_text for marker in title_markers) and not any(marker in title_text for marker in engineering_markers)


def _contains_keyword(text: str, keyword: str) -> bool:
    lowered = keyword.casefold()
    if len(lowered) <= 3 and lowered.isalnum():
        return re.search(rf"(?<![a-z0-9]){re.escape(lowered)}(?![a-z0-9])", text) is not None
    return lowered in text
