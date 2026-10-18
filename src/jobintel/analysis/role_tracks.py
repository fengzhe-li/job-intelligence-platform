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


# A heading that opens a list of optional skills ("Bonus:", "Nice to have", "Preferred qualifications").
_OPTIONAL_HEADING = re.compile(
    r"^(?:(?:an? )?(?:added )?bonus(?: points)?|nice[- ]to[- ]haves?|preferred(?: qualifications| skills| experience)?|desirable(?: skills)?"
    r"|good[- ]to[- ]haves?|a plus|plus|ideally|it would be (?:great|nice) if(?: you)?(?: have)?"
    # a short heading led by an optional word: "Desirable Skills, Knowledge, and Experience", "Preferred Qualifications"
    r"|(?:desirable|preferred|bonus|nice[- ]to[- ]have)(?: [a-z]+,?){1,5})\s*:?$", re.IGNORECASE)
_OPTIONAL_INLINE = re.compile(r"^(?:bonus|nice[- ]to[- ]have|preferred|desirable|good[- ]to[- ]have)\s*:\s*\S", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-•*·▪●]|\d+[.)])\s+")


def _optional_spans(description: str) -> list[tuple[int, int]]:
    """Character spans of the description that list OPTIONAL skills: the bullet items under a
    bonus-type heading (until the first non-bullet line) and inline "Bonus: ..." lines."""
    spans, offset, optional_from = [], 0, None
    for line in description.splitlines(keepends=True):
        bare = _BULLET.sub("", line).strip()
        is_bullet = _BULLET.match(line) is not None
        if optional_from is not None and not (is_bullet and not _OPTIONAL_HEADING.match(bare)) and bare:
            spans.append((optional_from, offset))
            optional_from = None
        if _OPTIONAL_HEADING.match(bare):
            optional_from = offset + len(line)
        elif _OPTIONAL_INLINE.match(bare):
            spans.append((offset, offset + len(line)))
        offset += len(line)
    if optional_from is not None:
        spans.append((optional_from, offset))
    return spans


# Section headings that scope REQUIRED skills. When a JD has at least one, a skill is required only if it
# is named inside such a section; mentions elsewhere (role overview, team description, tech stack, values)
# describe the job, they do not require the skill. Without one, every mention stays required (fail closed)
# except inside a DESCRIPTIVE section ("Technologies We Use", "What We Value").
_REQUIRED_HEADING = re.compile(
    r"^(?:what we require|requirements?|(?:minimum|basic|required|key|essential) (?:qualifications|requirements|skills|criteria)"
    r"|qualifications|what you(?:'ll| will)? need(?: to succeed)?|what you need|must[- ]haves?|essential(?: skills| criteria)?"
    r"|what we(?:'re| are) looking for|what we look for|skills (?:and|&) (?:experience|qualifications)|you (?:have|bring|should have)"
    r"|about you|who you are|you should apply if|what you(?:'ll| will)? bring)\s*:?$",
    re.IGNORECASE)
_DESCRIPTIVE_HEADING = re.compile(
    r"^(?:technologies we use|(?:our )?tech(?:nology)? stack(?: (?:and|&) tools)?|(?:the )?tools (?:we use|and technologies)"
    r"|what we value|our values|our stack)\s*:?$", re.IGNORECASE)
# "We don't ask that you have experience in all of this" / "will never expect you to have experience with all
# of them": that sentence and the line right after it (the stack it introduces) describe, they do not require.
_NOT_ALL_REQUIRED = re.compile(r"\b(?:don'?t|do not|never|won'?t|will not)\s+(?:ask|expect|require|need)\b[^.\n]*"
                               r"\b(?:experience|familiar\w*|know\w*)\b[^.\n]*\ball\b", re.IGNORECASE)


def _is_heading(line: str) -> bool:
    bare = line.strip()
    return (0 < len(bare) <= 60 and len(bare.split()) <= 7 and not _BULLET.match(line)
            and not bare.endswith((".", ",", ";")) and bare[0].isupper())


def _section_spans(description: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(required spans, descriptive spans): from a matching heading to the next heading-like line."""
    required, descriptive, offset, current = [], [], 0, None
    for line in description.splitlines(keepends=True):
        bare = line.strip().rstrip(":").strip().replace("\u2019", "'")  # "What you’ll need" (curly apostrophe)
        known = _REQUIRED_HEADING.match(bare) or _DESCRIPTIVE_HEADING.match(bare) or _OPTIONAL_HEADING.match(bare)
        # An unrecognised line ending in ":" ("We do require:") introduces the list below; it ends nothing.
        if _is_heading(line) and (known or not line.strip().endswith(":")):
            if current is not None:
                current[1].append((current[0], offset))
                current = None
            if _REQUIRED_HEADING.match(bare):
                current = (offset + len(line), required)
            elif _DESCRIPTIVE_HEADING.match(bare):
                current = (offset + len(line), descriptive)
        offset += len(line)
    if current is not None:
        current[1].append((current[0], offset))
    offset, lines = 0, description.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if _NOT_ALL_REQUIRED.search(line):
            following = len(lines[i + 1]) if i + 1 < len(lines) else 0
            descriptive.append((offset, offset + len(line) + following))
        offset += len(line)
    return required, descriptive


# Optional wording at CLAUSE level ("Python is required; Rust is a plus").
_OPTIONAL_CLAUSE = re.compile(r"nice to have|preferred|\ba plus\b|\bbonus\b|desirable|advantageous|\bbeneficial\b"
                             r"|\b(?:an|a big|a real) advantage\b", re.I)


def _skill_pattern(skill: str) -> re.Pattern:
    # "C" is never the C of "C++" / "C#" (and likewise for any skill followed by + or #).
    return re.compile(rf"(?<!\w){re.escape(skill)}(?![\w+#])", re.IGNORECASE)


_CLAUSE_END = re.compile(r"\.(?=\s|$)|[\n;]")  # a "." ends a clause only before whitespace ("e.g", "Node.js" do not)


def _clause_start(text: str, index: int) -> int:
    ends = [m.end() for m in _CLAUSE_END.finditer(text, 0, index)]
    return ends[-1] if ends else 0


def _clause_containing(text: str, index: int) -> str:
    end = _CLAUSE_END.search(text, index)
    return text[_clause_start(text, index):end.start() if end else len(text)]


_HTML_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")
_BLOCK_TAG = re.compile(r"</?(?:h[1-6]|p|div|li|ul|ol|br|tr|section|header|strong|b)\b[^>]*>", re.IGNORECASE)


def requirement_text(description: str) -> str:
    """The description as plain lines for requirement analysis: an HTML JD (e.g. a hosted job board's
    content) gets one line per heading / paragraph / list item, so its section headings are seen; plain
    text is returned unchanged."""
    import html
    text = description or ""
    if not _HTML_TAG.search(text):
        return text
    text = re.sub(r"<li\b[^>]*>", "\n- ", text, flags=re.IGNORECASE)  # list items stay bullets
    text = _BLOCK_TAG.sub("\n", text)
    text = html.unescape(_HTML_TAG.sub("", text))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def extract_skill_requirements(description: str) -> list[SkillRequirement]:
    """One entry per named skill. Required when ANY mention is a requirement: inside an explicit
    requirements section when the JD has one (see _REQUIRED_HEADING), otherwise anywhere outside an
    optional / descriptive section -- and never in an optional clause ("..., a plus")."""
    description = requirement_text(description)
    requirements: list[SkillRequirement] = []
    optional = _optional_spans(description)
    required_spans, descriptive = _section_spans(description)
    inside = lambda spans, i: any(start <= i < end for start, end in spans)
    for skill in SKILL_PATTERNS:
        matches = list(_skill_pattern(skill).finditer(description))
        if not matches:
            continue
        chosen, required = matches[0], False
        for match in matches:
            i = match.start()
            if inside(optional, i) or _OPTIONAL_CLAUSE.search(_clause_containing(description, i)):
                continue
            if (inside(required_spans, i) or not required_spans) and not inside(descriptive, i):
                chosen, required = match, True
                break
        sentence = _sentence_containing(description, chosen.start())
        requirements.append(SkillRequirement(name=skill, required=required, evidence=sentence.strip()))
    return requirements


# --- alternative requirement groups ("one or more of", "such as", "X, Y or Z") ---------------------------
_ONE_OF_CUE = re.compile(r"\b(?:one or more|at least one|one of|any of|any one of|either)\b", re.I)
_EXAMPLES_CUE = re.compile(r"\b(?:such as|e\.g\.?|for example|for instance|including)\b", re.I)
_OR = re.compile(r"\bor\b", re.I)
_AND = re.compile(r"\band\b", re.I)


def extract_requirement_groups(description: str, requirements: list[SkillRequirement]) -> list[dict]:
    """Required skills that the JD accepts as ALTERNATIVES, one group per clause:
    {"kind": "one_of" | "examples" | "alternatives", "members": [...], "wording": clause}.
    A group needs >= 2 required skills in the same clause and an explicit cue: "one or more" /
    "at least one" / "one of" / "either", "such as" / "e.g." (examples of an accepted category), or an
    "or" list. A clause joining skills with both "and" and "or" without such a cue is ambiguous and is
    NOT grouped (each skill stays individually required: fail closed)."""
    description = requirement_text(description)
    by_clause: dict[tuple[int, int], list[str]] = {}
    clauses: dict[tuple[int, int], str] = {}
    for item in requirements:
        if not item.required:
            continue
        # The occurrence that made the skill required (its evidence sentence), not merely its first mention
        # (e.g. "we use Java and Go" in a stack description before the requirements list).
        matches = list(_skill_pattern(item.name).finditer(description))
        match = next((m for m in matches if _sentence_containing(description, m.start()).strip() == item.evidence),
                     matches[0] if matches else None)
        if match is None:
            continue
        start = _clause_start(description, match.start())
        clause = _clause_containing(description, match.start())
        key = (start, start + len(clause))
        by_clause.setdefault(key, []).append((match.start(), item.name))
        clauses[key] = clause.strip()
    groups = []
    for key, found in by_clause.items():
        members = [name for _, name in sorted(found)]  # in the JD's own order
        if len(members) < 2:
            continue
        clause = clauses[key]
        if _ONE_OF_CUE.search(clause):
            kind = "one_of"
        elif _EXAMPLES_CUE.search(clause):
            kind = "examples"
        elif _OR.search(clause) and not _AND.search(clause):
            kind = "alternatives"
        else:
            continue  # "and", or no cue: every skill stays individually mandatory
        groups.append({"kind": kind, "members": members, "wording": clause})
    return groups


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
