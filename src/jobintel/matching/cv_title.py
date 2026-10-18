"""The CV heading for one application: the target job's own title, normalised only as far as truth needs.

The CV is JD-driven, so the heading follows the vacancy ("Graduate Software Engineer", "Backend
Software Engineer", "Software Engineer - AI/ML") rather than one umbrella identity. Normalisation:

1. Noise is dropped: parentheticals / trailing segments naming a location, work mode, year, intake,
   contract type or gender marker ("(m/f/d)", "- United Kingdom", "(2027 start)").
2. Of the segments after a spaced dash / pipe / comma, only a technical specialisation is kept
   ("- AI/ML", ", Backend"); a team name or a second job family ("- Software Engineer") is dropped.
3. Unsupported seniority is stripped (Senior, Sr, Staff, Principal, Lead, Distinguished, Mid-level,
   level numerals such as "II"), and so is any professional status or clearance the title asserts
   (Chartered, Security Cleared, Certified, ...). "ML Engineer" reads as "Machine Learning Engineer".
4. A management / executive title (Manager, Director, Head of, VP, Chief ...) is never presented, and
   neither is a title that normalises to nothing specific (a bare "Engineer"): the candidate's own
   authored headline is used instead.

Early-career markers that are part of the role (Graduate, Junior, Entry-level, Associate, Intern,
Trainee, Apprentice) are kept. Deterministic; never adds a word the job title does not contain
(except the ML expansion).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_TITLE_LENGTH = 60

_SEGMENT_SPLIT = re.compile(r"\s+[-–—|]\s+|\s*,\s+")
# "Graduate Programme 2027: Software Engineer": a programme / intake / year prefix before ": " is noise.
_NOISE_PREFIX = re.compile(r"^([^:]{1,60}):\s+(?=\S)")
_PARENTHETICAL = re.compile(r"\s*[(\[]([^)\]]*)[)\]]")
# A parenthetical / segment that says where, when or on what terms -- not what the role is.
_NOISE = re.compile(
    r"\d|\b(?:m/f/d|f/m/d|m/w/d|f/m/x|all genders|remote|hybrid|on[- ]?site|in[- ]office|office[- ]based"
    r"|uk|united kingdom|england|scotland|wales|ireland|london|manchester|cambridge|oxford|edinburgh|bristol"
    r"|europe|emea|us|usa|united states|global|anywhere|worldwide|start|intake|programme|program|scheme|cohort"
    r"|contract|fixed[- ]term|permanent|full[- ]time|part[- ]time|temporary|maternity cover|visa sponsorship)\b",
    re.IGNORECASE)
# Words a kept specialisation segment may consist of.
_SPECIALISATION_WORDS = {
    "ai", "ml", "machine", "learning", "llm", "llms", "genai", "generative", "applied", "nlp", "computer",
    "vision", "data", "backend", "back-end", "frontend", "front-end", "full-stack", "fullstack", "full", "stack",
    "platform", "platforms", "infrastructure", "cloud", "devops", "sre", "security", "mobile", "ios", "android",
    "web", "api", "apis", "systems", "distributed", "networking", "embedded", "python", "java", "go", "golang",
    "rust", "c++", "c#", "typescript", "javascript", "kotlin", "scala", "aws", "azure", "gcp", "and", "&", "/",
    "back", "end", "front",
}
_ROLE_NOUN = re.compile(
    r"\b(?:engineer|engineering|developer|scientist|analyst|architect|programmer|intern|internship|graduate"
    r"|specialist|technologist|researcher|administrator|technician|consultant|designer|apprentice|trainee)\b",
    re.IGNORECASE)
# Seniority the candidate does not hold -- stripped wherever it appears as a whole word.
_SENIORITY = re.compile(
    r"\b(?:senior|snr|sr|staff|principal|distinguished|lead|tech lead|team lead|mid[- ]?level|mid[- ]senior"
    r"|intermediate|experienced|expert|level\s+[1-9]|l[3-9])\b\.?|\b(?:ii|iii|iv|v)\b",
    re.IGNORECASE)
# Professional status / clearance a title may assert -- never presented.
_STATUS = re.compile(
    r"\b(?:chartered|incorporated|certified|licensed|accredited|security[- ]cleared|sc[- ]cleared|dv[- ]cleared"
    r"|cleared|professional engineer)\b", re.IGNORECASE)
# Management / executive roles: no truthful normalisation, so the authored headline is used.
_MANAGEMENT = re.compile(r"\b(?:manager|director|head of|head|vp|vice president|chief|cto|cio|ceo"
                         r"|founder)\b", re.IGNORECASE)
# A parenthetical that is itself an early-career marker ("Software Engineer (Intern)") stays in the
# heading: "Intern" / "Internship" after the role, the others before it.
_EARLY_CAREER = re.compile(r"intern|internship|graduate|junior|entry[- ]level|associate|trainee|apprentice"
                           r"|new grad", re.IGNORECASE)
_SUFFIX_MARKER = re.compile(r"intern|internship", re.IGNORECASE)
# Technologies a title may name as its specialisation ("Software Engineer (Java)"): presented only when the
# candidate's own evidence grounds them (a user-approved refresh skill is not grounded experience).
_TECHNOLOGY_WORDS = {"python", "java", "go", "golang", "rust", "c++", "c#", "typescript", "javascript", "kotlin",
                     "scala", "aws", "azure", "gcp", "ios", "android"}
_ML_ENGINEER = re.compile(r"(?<![/\w])ML (Engineer|Developer|Scientist|Researcher)\b")


@dataclass(frozen=True)
class CVTitle:
    title: str          # the heading to print ("" when neither the job nor the authored CV gives one)
    source: str         # "jd_title" | "authored_fallback"
    job_title: str      # the target job's title as stored
    removed: tuple[str, ...] = ()  # what normalisation dropped (seniority, status, noise, segments)
    reason: str = ""    # why the authored headline was used instead, if it was

    def action(self) -> str:
        if self.source == "jd_title":
            dropped = f" (dropped: {', '.join(self.removed)})" if self.removed else ""
            return f"title: {self.title!r} from job title {self.job_title!r}{dropped}"
        return f"title: authored headline {self.title!r} used; job title {self.job_title!r} {self.reason}"


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:[-/&,|:]\s*|(?:and|of)\s+)+|(?:\s*[-/&,|:]|\s+(?:and|of))+$", "", text, flags=re.IGNORECASE)
    return text.strip()


# Nouns that may accompany a specialisation without changing it ("(ML Projects)", "- Data Products").
_SPECIALISATION_COMPANIONS = {"projects", "project", "products", "product", "team", "teams", "applications"}
_CONNECTORS = {"and", "&", "/"}


def _is_specialisation(segment: str) -> bool:
    words = [w for w in re.split(r"\s+|(?<=\w)/(?=\w)", segment.casefold()) if w]
    core = [w for w in words if w not in _SPECIALISATION_COMPANIONS]
    return (bool(words) and any(w not in _CONNECTORS for w in core)
            and all(w in _SPECIALISATION_WORDS for w in core) and not _ROLE_NOUN.search(segment))


def _ungrounded_technology(segment: str, grounded) -> bool:
    if grounded is None:
        return False
    words = {w for w in re.split(r"\s+|(?<=\w)/(?=\w)", segment.casefold()) if w}
    return any(w in _TECHNOLOGY_WORDS and w not in grounded for w in words)


def normalise_job_title(job_title: str | None, grounded=None) -> tuple[str | None, tuple[str, ...], str]:
    """(title, removed, reason): the truthful CV heading for this job title, or None with the reason
    it cannot be presented. `grounded` (casefolded skill names with grounded evidence), when given,
    drops a technology specialisation the candidate's evidence does not ground."""
    raw = re.sub(r"\s+", " ", job_title or "").strip()
    if not raw:
        return None, (), "is empty"
    # "Campus Systems Engineer (Full-Time)": "Campus" is a recruiting-channel label for graduate hiring
    # in graduate hiring, not part of the role name -- present it as the graduate role.
    raw = re.sub(r"^campus\s+(?=\S)", "Graduate ", raw, flags=re.I)
    removed: list[str] = []
    early: list[str] = []

    def strip_parenthetical(match: re.Match) -> str:
        inner = match.group(1).strip()
        if _EARLY_CAREER.fullmatch(inner):
            early.append(inner)  # "(Intern)", "(Graduate)": part of the role, kept in the heading
            return ""
        if _NOISE.search(inner) or not _is_specialisation(inner) or _ungrounded_technology(inner, grounded):
            removed.append(f"({inner})")
            return ""
        return match.group(0)

    prefix = _NOISE_PREFIX.match(raw)
    if prefix and _NOISE.search(prefix.group(1)):
        # The programme wording is noise; an early-career marker in it ("Graduate") is the role's own.
        removed.append(prefix.group(1))
        early.extend(m.group(0) for m in re.finditer(rf"\b(?:{_EARLY_CAREER.pattern})\b", prefix.group(1), re.I))
        raw = raw[prefix.end():]
    text = _PARENTHETICAL.sub(strip_parenthetical, raw)
    segments = [s.strip() for s in _SEGMENT_SPLIT.split(text) if s.strip()]
    core_index = next((i for i, s in enumerate(segments) if _ROLE_NOUN.search(s) and not _NOISE.fullmatch(s)), 0)
    core = segments[core_index] if segments else ""
    kept = []
    for index, segment in enumerate(segments):
        if index == core_index:
            continue
        if index > core_index and not _NOISE.search(segment) and _is_specialisation(segment) \
                and not _ungrounded_technology(segment, grounded):
            kept.append(segment)
        else:
            removed.append(segment)
    if _MANAGEMENT.search(core):
        return None, tuple(removed), "is a management/executive title, not presented"
    for pattern in (_STATUS, _SENIORITY):
        for match in pattern.finditer(core):
            removed.append(match.group(0).strip())
        core = pattern.sub(" ", core)
    core = _tidy(_ML_ENGINEER.sub(r"Machine Learning \1", _tidy(core)))
    for marker in early:
        if not re.search(rf"\b{re.escape(marker)}\b", core, re.IGNORECASE):
            core = f"{core} {marker}" if _SUFFIX_MARKER.fullmatch(marker) else f"{marker} {core}"
    if not _ROLE_NOUN.search(core) or len(core.split()) < 2:
        return None, tuple(removed), "normalises to nothing specific"
    title = " - ".join([core, *kept])
    if len(title) > MAX_TITLE_LENGTH:
        removed.extend(kept)
        title = core
    if len(title) > MAX_TITLE_LENGTH:
        return None, tuple(removed), "is too long to present"
    return title, tuple(removed), ""


def cv_title(job_title: str | None, authored_headline: str | None = "", grounded=None) -> CVTitle:
    """The heading for this application's CV: the normalised job title, else the authored headline."""
    title, removed, reason = normalise_job_title(job_title, grounded)
    if title:
        return CVTitle(title, "jd_title", job_title or "", removed)
    return CVTitle((authored_headline or "").strip(), "authored_fallback", job_title or "", removed, reason)
