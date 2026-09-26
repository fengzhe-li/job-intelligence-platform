from __future__ import annotations

import re
from dataclasses import dataclass

from jobintel.models.job import Job


@dataclass(frozen=True)
class SenioritySignal:
    level: str
    score: float
    evidence: str


@dataclass(frozen=True)
class ExperienceSignal:
    years_required: float | None
    score: float
    evidence: str
    management_required: bool = False


@dataclass(frozen=True)
class RoleFunctionSignal:
    function: str
    score: float
    evidence: str


@dataclass(frozen=True)
class MarketSignal:
    market: str
    score: float
    evidence: str


@dataclass(frozen=True)
class EngineeringDomainSignal:
    domain: str
    score: float
    evidence: str


SENIORITY_PATTERNS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("intern", 1.0, ("intern", "internship")),
    (
        "graduate",
        1.0,
        (
            "graduate",
            "new grad",
            "early careers",
            "early career",
            "graduate scheme",
            "graduate programme",
            "graduate program",
            "new grad programme",
            "new grad program",
            "early careers programme",
            "early careers program",
        ),
    ),
    ("junior", 0.9, ("junior", "entry level", "entry-level")),
    ("associate", 0.65, ("associate",)),
    ("principal", -1.4, ("principal",)),
    ("staff", -1.4, ("staff",)),
    ("director", -1.6, ("director",)),
    ("head", -1.6, ("head of",)),
    ("manager", -1.6, ("manager", "team lead", "team leader", "management experience")),
    ("lead", -1.3, ("lead",)),
    ("senior", -1.15, ("senior", "sr.")),
    ("mid-level", -0.25, ("mid-level", "mid level")),
)

JD_SENIORITY_PATTERNS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("intern", 1.0, ("internship",)),
    (
        "graduate",
        1.0,
        (
            "graduate programme",
            "graduate program",
            "graduate scheme",
            "graduate role",
            "new grad",
            "new grad programme",
            "new grad program",
            "early careers",
            "early career",
            "early careers programme",
            "early careers program",
            "2027 intake",
            "2027 graduate",
            "0-2 years experience",
            "0-2 years of experience",
        ),
    ),
    ("junior", 0.9, ("entry-level role", "entry level role", "junior role")),
    ("associate", 0.65, ("associate-level role", "associate level role")),
    ("principal", -1.4, ("principal-level role", "principal level role")),
    ("staff", -1.4, ("staff-level role", "staff level role")),
    ("director", -1.6, ("director-level role", "director level role")),
    ("head", -1.6, ("head of role",)),
    ("manager", -1.6, ("management experience", "people management", "line management", "managed a team", "lead a team", "leading a team")),
    ("lead", -1.3, ("lead-level role", "lead level role")),
    ("senior", -1.15, ("senior-level role", "senior level role")),
    ("mid-level", -0.25, ("mid-level role", "mid level role")),
)

ROLE_FUNCTION_PATTERNS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("admin_recruiting", -1.2, ("talent partner", "recruiter", "executive assistant", "office administrator", "people partner")),
    ("legal_compliance", -1.2, ("legal counsel", "lawyer", "compliance manager", "risk assurance manager")),
    ("business_market", -1.2, ("market specialist", "business analyst", "commercial analyst", "commercial manager", "strategy analyst", "strategy manager", "business development", "sales engineer", "sales specialist", "account executive", "product manager")),
    ("operations_investigations", -1.1, ("investigations", "fraud investigator", "fraud team leader", "customer operations", "operations manager", "operations analyst")),
    ("fp&a_finance_analytics", -1.0, ("fp&a", "financial planning", "treasury", "pricing analyst", "credit risk manager", "finance analytics")),
    ("data_analytics_bi", -0.35, ("data analyst", "analytics manager", "analytics engineer", "business intelligence", "bi analyst", "reporting analyst", "analyst")),
    ("management", -1.0, ("engineering manager", "manager", "team leader", "head of", "director")),
    ("ai_ml_engineering", 0.55, ("machine learning engineer", "ml engineer", "ai engineer", "applied scientist", "research engineer")),
    ("data_engineering", 0.65, ("data engineer", "analytics engineer", "data platform", "etl", "data pipeline")),
    ("network_software", 0.65, ("network software", "network automation", "tcp/ip", "bgp", "cdn", "edge network")),
    ("software_engineering", 0.65, ("software engineer", "backend engineer", "frontend engineer", "full-stack engineer", "full stack engineer", "platform engineer", "android engineer", "mobile engineer")),
)

JD_ROLE_FUNCTION_PATTERNS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("business_market", -1.2, ("market specialist", "commercial role", "sales role", "business development role", "product management", "go-to-market", "market analysis")),
    ("operations_investigations", -1.1, ("fraud investigations", "customer operations role", "operations role")),
    ("fp&a_finance_analytics", -1.0, ("fp&a", "financial planning", "finance analytics", "treasury role")),
    ("data_analytics_bi", -0.35, ("business intelligence", "bi reporting", "reporting analyst")),
    ("management", -1.0, ("people management", "line management", "managed a team")),
    ("ai_ml_engineering", 0.55, ("machine learning engineer", "ml engineer", "ai engineer", "applied scientist", "research engineer")),
    ("data_engineering", 0.65, ("data engineer", "data platform", "etl", "data pipeline")),
    ("network_software", 0.65, ("network software", "network automation", "tcp/ip", "bgp", "cdn", "edge network")),
    ("software_engineering", 0.65, ("software engineer", "backend engineer", "frontend engineer", "full-stack engineer", "full stack engineer", "platform engineer", "android engineer", "mobile engineer")),
)

EXPERIENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?P<min>\d+)\s*\+\s*years?", re.IGNORECASE),
    re.compile(r"(?P<min>\d+)\s*-\s*(?P<max>\d+)\s*years?", re.IGNORECASE),
    re.compile(r"(?P<min>\d+)\s+or\s+more\s+years?", re.IGNORECASE),
    re.compile(r"minimum\s+(?P<min>\d+)\s+years?", re.IGNORECASE),
    re.compile(r"at\s+least\s+(?P<min>\d+)\s+years?", re.IGNORECASE),
)

TITLE_LADDER_PATTERN = re.compile(
    r"\b(?:(?:software|backend|frontend|full[- ]stack|platform|cloud|data|machine learning|ml|network|android|mobile|site reliability|devops|systems)\s+)?"
    r"(?:engineer|developer|scientist)\s+"
    r"(?P<level>i{1,3}|iv|v|[1-9]\d?)\b",
    re.IGNORECASE,
)

IC_LADDER_PATTERN = re.compile(r"\bic\s*-?\s*(?P<level>[1-9]\d?)\b", re.IGNORECASE)

TARGET_ENGINEERING_DOMAIN_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("software_backend_frontend", ("software engineer", "backend engineer", "frontend engineer", "full-stack engineer", "full stack engineer", "mobile engineer", "android engineer")),
    ("data_platform", ("data engineer", "data platform", "data quality engineer", "data infrastructure", "analytics engineer")),
    ("cloud_platform_infrastructure", ("site reliability engineer", "sre", "platform engineer", "cloud engineer", "infrastructure engineer", "security engineer")),
    ("ai_ml", ("machine learning engineer", "ml engineer", "ai engineer", "research engineer")),
    ("telecom_network_internet", ("network engineer", "network software", "telecom", "telecommunications", "bgp", "tcp/ip", "cdn", "edge network")),
    ("electronic_embedded_iot", ("embedded software engineer", "firmware engineer", "iot", "edge device", "electronic communication")),
)

ADJACENT_INDUSTRIAL_DOMAIN_PATTERNS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("mechanical_engineering", -0.75, ("mechanical engineer", "mechanical design", "mechanical systems", "aerospace mechanical")),
    ("civil_structural_engineering", -0.9, ("civil engineer", "structural engineer", "structural design", "building services")),
    ("manufacturing_engineering", -0.75, ("manufacturing engineer", "production engineer", "process engineer", "factory", "manufacturing process")),
    ("power_high_voltage_engineering", -0.85, ("power systems", "high voltage", "hvdc", "substation", "gas turbine", "turbine performance", "electrical design engineer")),
    ("industrial_systems_engineering", -0.65, ("systems engineer", "requirements engineer", "validation engineer", "test engineer", "labview", "simulink")),
)


def extract_seniority(title: str, description: str) -> SenioritySignal:
    title_text = title.casefold()
    text = f"{title}\n{description}".casefold()
    ladder = _title_ladder_signal(title)
    if ladder:
        return ladder
    for level, score, phrases in SENIORITY_PATTERNS:
        for phrase in phrases:
            if _contains_phrase(title_text, phrase):
                return SenioritySignal(level, score, f"{level.replace('-', ' ').title()}-level role from title phrase '{phrase}'")
    for level, score, phrases in JD_SENIORITY_PATTERNS:
        for phrase in phrases:
            if _contains_phrase(text, phrase):
                return SenioritySignal(level, score, f"{level.replace('-', ' ').title()}-level role from JD phrase '{phrase}'")
    return SenioritySignal("unspecified", 0.0, "No explicit seniority signal")


def extract_required_experience(title: str, description: str) -> ExperienceSignal:
    text = f"{title}\n{description}"
    best_years: float | None = None
    best_evidence = ""
    for pattern in EXPERIENCE_PATTERNS:
        for match in pattern.finditer(text):
            if _is_age_range_or_age_reference(text, match.start(), match.end()):
                continue
            years = float(match.group("min"))
            if best_years is None or years > best_years:
                best_years = years
                best_evidence = _sentence_containing(text, match.start()).strip() or match.group(0)

    lowered = text.casefold()
    management_required = any(phrase in lowered for phrase in ("management experience", "people management", "line management", "managed a team", "lead a team"))
    if management_required and (best_years is None or best_years < 5):
        return ExperienceSignal(best_years, -0.9, best_evidence or "Management experience required", management_required=True)
    if best_years is None:
        return ExperienceSignal(None, 0.2, "No explicit years-of-experience requirement")
    if best_years <= 2:
        return ExperienceSignal(best_years, 0.4, best_evidence)
    if best_years < 5:
        return ExperienceSignal(best_years, -0.35, best_evidence)
    return ExperienceSignal(best_years, -0.9, best_evidence)


def infer_role_function(title: str, description: str) -> RoleFunctionSignal:
    title_text = title.casefold()
    text = f"{title}\n{description}".casefold()
    for function, score, phrases in ROLE_FUNCTION_PATTERNS:
        for phrase in phrases:
            if _contains_phrase(title_text, phrase):
                return RoleFunctionSignal(function, score, f"{_label(function)} from title phrase '{phrase}'")
    for function, score, phrases in JD_ROLE_FUNCTION_PATTERNS:
        for phrase in phrases:
            if _contains_phrase(text, phrase):
                return RoleFunctionSignal(function, score, f"{_label(function)} from JD phrase '{phrase}'")
    return RoleFunctionSignal("unknown", 0.0, "No clear role-function signal")


def infer_engineering_domain(title: str, description: str) -> EngineeringDomainSignal:
    title_text = title.casefold()
    text = f"{title}\n{description}".casefold()
    for domain, phrases in TARGET_ENGINEERING_DOMAIN_PATTERNS:
        for phrase in phrases:
            if _contains_phrase(title_text, phrase):
                return EngineeringDomainSignal(domain, 0.25, f"{_label(domain)} target domain from title phrase '{phrase}'")
    for domain, score, phrases in ADJACENT_INDUSTRIAL_DOMAIN_PATTERNS:
        for phrase in phrases:
            if _contains_phrase(title_text, phrase) or _contains_phrase(text, phrase):
                return EngineeringDomainSignal(domain, score, _domain_explanation(domain, phrase))
    for domain, phrases in TARGET_ENGINEERING_DOMAIN_PATTERNS:
        for phrase in phrases:
            if _contains_phrase(text, phrase):
                return EngineeringDomainSignal(domain, 0.25, f"{_label(domain)} target domain from JD phrase '{phrase}'")
    return EngineeringDomainSignal("unknown", 0.0, "No clear engineering-domain signal")


def assess_uk_market(job: Job) -> MarketSignal:
    raw = " ".join(filter(None, [job.raw_location, *(location.raw for location in job.locations)]))
    text = f"{raw}\n{job.description}".casefold()
    has_uk_location = any(location.is_uk for location in job.locations)
    if has_uk_location:
        return MarketSignal("uk", 0.35, "UK target market")
    if any(phrase in text for phrase in ("americas", "united states", "usa", "us only", "canada", "latam")):
        return MarketSignal("non_uk", -1.0, "Outside UK target market: Americas-only or North America signal")
    if any(phrase in text for phrase in ("apac", "singapore", "australia", "japan", "india")):
        return MarketSignal("non_uk", -1.0, "Outside UK target market: APAC-only signal")
    if any(phrase in text for phrase in ("emea", "europe", "ireland", "dublin", "berlin", "paris", "barcelona")):
        return MarketSignal("non_uk", -0.55, "Outside UK target market: non-UK regional signal")
    if "worldwide" in text or "remote" in text:
        return MarketSignal("ambiguous_worldwide", -0.45, "Ambiguous worldwide remote role without UK eligibility evidence")
    return MarketSignal("unknown", -0.35, "No UK target-market evidence")


def seniority_label_counts(results: list[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for title, description in results:
        level = extract_seniority(title, description).level
        counts[level] = counts.get(level, 0) + 1
    return counts


def _contains_phrase(text: str, phrase: str) -> bool:
    if phrase.endswith("."):
        return phrase in text
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.casefold())}(?![a-z0-9])", text) is not None


def _sentence_containing(text: str, index: int) -> str:
    start = max(text.rfind(".", 0, index), text.rfind("\n", 0, index)) + 1
    end_candidates = [position for position in (text.find(".", index), text.find("\n", index)) if position != -1]
    end = min(end_candidates) if end_candidates else min(len(text), index + 160)
    return text[start:end]


def _is_age_range_or_age_reference(text: str, start: int, end: int) -> bool:
    window = text[start : min(len(text), end + 24)].casefold()
    before = text[max(0, start - 4) : start]
    return "year old" in window or "year-old" in window or before.endswith("-")


def _label(function: str) -> str:
    return function.replace("_", " ")


def _domain_explanation(domain: str, phrase: str) -> str:
    if domain == "mechanical_engineering":
        return f"Mechanical engineering role with low software overlap from phrase '{phrase}'"
    if domain == "civil_structural_engineering":
        return f"Civil/structural engineering domain rather than software/data/network from phrase '{phrase}'"
    if domain == "manufacturing_engineering":
        return f"Manufacturing engineering domain rather than software/data/network from phrase '{phrase}'"
    if domain == "power_high_voltage_engineering":
        return f"Power/high-voltage engineering domain rather than software/data/network from phrase '{phrase}'"
    return f"Technically adjacent but weak candidate evidence from phrase '{phrase}'"


def _title_ladder_signal(title: str) -> SenioritySignal | None:
    match = TITLE_LADDER_PATTERN.search(title)
    if not match and re.search(r"\b(?:engineer|developer|scientist)\b", title, flags=re.IGNORECASE):
        match = IC_LADDER_PATTERN.search(title)
    if not match:
        return None
    raw_level = match.group("level")
    numeric_level = _ladder_number(raw_level)
    if numeric_level is None:
        return None
    if numeric_level <= 1:
        return SenioritySignal("junior", 0.75, f"Numeric ladder level {raw_level.upper()} implies junior/entry-ish seniority")
    if numeric_level == 2:
        return SenioritySignal("mid-level", -0.25, f"Numeric ladder level {raw_level.upper()} implies mid-level seniority")
    return SenioritySignal("mid-senior", -0.65, f"Numeric ladder level {raw_level.upper()} implies mid/senior seniority")


def _ladder_number(value: str) -> int | None:
    lowered = value.casefold()
    roman = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5}
    if lowered in roman:
        return roman[lowered]
    try:
        return int(lowered)
    except ValueError:
        return None
