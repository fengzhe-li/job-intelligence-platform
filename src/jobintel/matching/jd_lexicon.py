"""ATS / AI CV-reading layer: JD lexical extraction, grounded lexical alignment, skill status, coverage
report and learning backlog (evidence-grounded lexical matching).

Applicant-tracking systems and AI screeners match the employer's own words. This module makes the CV use
the JD's terminology where the candidate's evidence already supports it, and never otherwise:

* `extract_terms` -- important JD terms (hard skills, engineering practices / problem shapes, collaborative
  signals, role-domain terms), weighted by where they appear (basic / required > responsibilities >
  preferred > other), repetition and the title.
* `term_status` -- per term: GROUNDED_EXACT (the term itself is in grounded evidence), GROUNDED_SYNONYM (an
  explicit, narrow equivalence rule matches grounded evidence), USER_APPROVED_REFRESH (a skill the user
  explicitly approved for the Skills section while relearning it) or UNSUPPORTED. Problem-shape claims
  (scalable, microservices, fault-tolerant, distributed, cloud-native ...) have NO synonym rule: only the
  literal evidence grounds them.
* `align_text` -- rewrites an on-page bullet into the JD's wording only through explicit rules whose new
  words are grounded in that bullet's OWN source evidence (or are pure wording equivalents such as "worked
  with" -> "collaborated with"). Nothing is added that the source does not say.
* `refresh_skills` / `implied_skills` -- skills that may join Technical Skills: user-approved refresh
  skills the JD names, and umbrella terms implied by grounded skills (NoSQL by DynamoDB; Git by the
  candidate's own GitHub repositories). A refresh skill never enters a bullet.
* `coverage_report` / `learning_backlog` -- internal review records (never printed on the CV).

No company, project or domain name is consulted.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

GROUNDED_EXACT = "GROUNDED_EXACT"
GROUNDED_SYNONYM = "GROUNDED_SYNONYM"
USER_APPROVED_REFRESH = "USER_APPROVED_REFRESH"
UNSUPPORTED = "UNSUPPORTED"

HARD, PRACTICE, SOFT, DOMAIN = "hard_skill", "engineering_practice", "soft_signal", "domain_term"

# key -> (category, JD pattern, display)
VOCABULARY: dict[str, tuple[str, str, str]] = {
    # hard technical skills
    "java": (HARD, r"\bjava\b(?!script)", "Java"), "python": (HARD, r"\bpython\b", "Python"),
    "c++": (HARD, r"(?<!\w)c\+\+(?!\w)", "C++"), "c#": (HARD, r"(?<!\w)c#(?!\w)", "C#"),
    # "Go" only as a name in a list ("Python, Go, Rust") or as "Golang" -- never the English verb.
    "go": (HARD, r"\bgolang\b|(?-i:\bGo\b)(?=[ \t]*(?:,|/|\)|\bor\b|\band\b|\r?\n|$))", "Go"), "rust": (HARD, r"\brust\b", "Rust"),
    "typescript": (HARD, r"\btypescript\b", "TypeScript"), "javascript": (HARD, r"\bjavascript\b", "JavaScript"),
    "kotlin": (HARD, r"\bkotlin\b", "Kotlin"), "scala": (HARD, r"\bscala\b", "Scala"),
    "aws": (HARD, r"\baws\b|amazon web services", "AWS"), "gcp": (HARD, r"\bgcp\b|google cloud", "GCP"),
    "azure": (HARD, r"\bazure\b", "Azure"), "kubernetes": (HARD, r"\bkubernetes\b|\bk8s\b", "Kubernetes"),
    "terraform": (HARD, r"\bterraform\b", "Terraform"), "docker": (HARD, r"\bdocker\b", "Docker"),
    "sql": (HARD, r"\bsql\b", "SQL"), "nosql": (HARD, r"\bno-?sql\b", "NoSQL"),
    "postgresql": (HARD, r"\bpostgre(?:s|sql)\b", "PostgreSQL"), "react": (HARD, r"\breact\b", "React"),
    "fastapi": (HARD, r"\bfastapi\b", "FastAPI"), "grafana": (HARD, r"\bgrafana\b", "Grafana"),
    "prometheus": (HARD, r"\bprometheus\b", "Prometheus"), "linux": (HARD, r"\blinux\b", "Linux"),
    "git": (HARD, r"\bgit\b(?!hub)", "Git"),
    # engineering practices / problem shapes
    "version control": (PRACTICE, r"version control", "version control"),
    "ci/cd": (PRACTICE, r"\bci/cd\b|continuous (?:integration|delivery|deployment)", "CI/CD"),
    "debugging": (PRACTICE, r"\bdebug\w*", "debugging"),
    "troubleshooting": (PRACTICE, r"\btroubleshoot\w*", "troubleshooting"),
    "monitoring": (PRACTICE, r"\bmonitor\w*", "monitoring"),
    "testing": (PRACTICE, r"\btest(?:ing|s)?\b", "testing"),
    "sdlc": (PRACTICE, r"software development life\s?cycle|\bsdlc\b", "software development lifecycle"),
    "code review": (PRACTICE, r"code reviews?", "code review"),
    "technical documentation": (PRACTICE, r"technical documentation", "technical documentation"),
    "data structures": (PRACTICE, r"data structures?", "data structures"),
    "algorithms": (PRACTICE, r"\balgorithm\w*", "algorithms"),
    "object-oriented design": (PRACTICE, r"object[- ]oriented", "object-oriented design"),
    "design patterns": (PRACTICE, r"design patterns?", "design patterns"),
    "scalable": (PRACTICE, r"\bscalab\w*", "scalable"),
    "resilient": (PRACTICE, r"\bresilien\w*", "resilient"),
    "fault-tolerant": (PRACTICE, r"fault[- ]toleran\w*", "fault-tolerant"),
    "distributed systems": (PRACTICE, r"distributed (?:systems?|computing)", "distributed systems"),
    "cloud-native": (PRACTICE, r"cloud[- ]native", "cloud-native"),
    "microservices": (PRACTICE, r"micro-?services?", "microservices"),
    "operational excellence": (PRACTICE, r"operational excellence", "operational excellence"),
    "on-call": (PRACTICE, r"on[- ]call", "on-call"), "agile": (PRACTICE, r"\bagile\b", "agile"),
    "open source": (PRACTICE, r"open[- ]source", "open source"),
    # soft / collaborative signals
    "collaboration": (SOFT, r"\bcollaborat\w*", "collaboration"),
    "communication": (SOFT, r"\bcommunicat\w*", "communication"),
    "cross-functional": (SOFT, r"cross[- ](?:functional|disciplinary)", "cross-functional"),
    "teamwork": (SOFT, r"\bteam(?:work| environment)\b", "teamwork"),
    "problem-solving": (SOFT, r"problem[- ]solving|solve (?:real |complex )?\w* ?problems", "problem-solving"),
    "analytical": (SOFT, r"\banalytical\b", "analytical"),
    "adaptability": (SOFT, r"\badapt\w*", "adaptability"),
    "ambiguity": (SOFT, r"\bambiguity\b", "ambiguity"),
    "ownership": (SOFT, r"\bownership\b|\bown the\b", "ownership"),
    # role-domain terms
    "genai": (DOMAIN, r"\bgenai\b|generative ai|ai[- ]powered|\bai tools\b|\bllms?\b", "GenAI / AI tools"),
    "cloud platforms": (DOMAIN, r"cloud platforms?", "cloud platforms"),
    "database systems": (DOMAIN, r"database systems?", "database systems"),
    "data pipelines": (DOMAIN, r"data pipelines?", "data pipelines"),
    "retrieval": (DOMAIN, r"\bretrieval\b|\brag\b", "retrieval"),
    "inference": (DOMAIN, r"\binference\b", "inference"),
    "site reliability": (DOMAIN, r"site reliability|\bsre\b", "site reliability"),
    "platform engineering": (DOMAIN, r"platform engineering", "platform engineering"),
}

# Section headings -> weight. Unknown text weighs 1.
_SECTIONS = (
    (re.compile(r"^\s*(?:basic|minimum|required|essential)\s+(?:qualifications?|requirements?|skills?)|^\s*requirements\b|"
                r"^\s*what you(?:'ll)? need|^\s*must have", re.I | re.M), "basic", 3.0),
    (re.compile(r"^\s*(?:key\s+)?(?:job\s+)?responsibilities|^\s*what you(?:'ll| will) do|^\s*the role\b", re.I | re.M),
     "responsibilities", 2.0),
    (re.compile(r"^\s*(?:preferred|desirable|nice to have|bonus)\b.*$", re.I | re.M), "preferred", 1.5),
)
MIN_IMPORTANT_WEIGHT = 1.5


@dataclass
class Term:
    key: str
    display: str
    category: str
    phrase: str                 # the JD's exact wording (first occurrence)
    weight: float
    sections: list = field(default_factory=list)
    count: int = 0


def _sectioned(jd_text: str) -> list[tuple[str, float, str]]:
    """(section, weight, text) blocks in order."""
    marks = []
    for regex, name, weight in _SECTIONS:
        marks += [(m.start(), name, weight) for m in regex.finditer(jd_text)]
    marks.sort()
    blocks, last, current = [], 0, ("other", 1.0)
    for start, name, weight in marks:
        blocks.append((current[0], current[1], jd_text[last:start]))
        last, current = start, (name, weight)
    blocks.append((current[0], current[1], jd_text[last:]))
    return [b for b in blocks if b[2].strip()]


def extract_terms(jd_text: str, job_title: str = "") -> list[Term]:
    """Important JD terms, heaviest first. Weight = best section weight + 0.5 per extra mention (cap +1.5)
    + 1 if the title names it."""
    terms = []
    blocks = _sectioned(jd_text or "")
    for key, (category, pattern, display) in VOCABULARY.items():
        regex = re.compile(pattern, re.I)
        hits = [(name, weight, m.group(0)) for name, weight, text in blocks for m in regex.finditer(text)]
        if not hits:
            continue
        weight = max(w for _, w, _ in hits) + min(0.5 * (len(hits) - 1), 1.5)
        if regex.search(job_title or ""):
            weight += 1.0
        # Quote the wording from the most important section it appears in (not the first marketing mention).
        phrase = max(hits, key=lambda h: h[1])[2]
        terms.append(Term(key, display, category, phrase, round(weight, 2),
                          sorted({n for n, _, _ in hits}), len(hits)))
    return sorted(terms, key=lambda t: (-t.weight, t.key))


def important_terms(terms: list[Term]) -> list[Term]:
    return [t for t in terms if t.weight >= MIN_IMPORTANT_WEIGHT]


# --- evidence ------------------------------------------------------------------------------------------

# Narrow equivalences: JD term -> grounded wording that genuinely means it. Problem-shape claims (scalable,
# resilient, fault-tolerant, distributed, cloud-native, microservices, ...) deliberately have none.
SYNONYMS = {
    "debugging": r"troubleshoot\w*|investigat\w* (?:\w+ ){0,3}(?:software|application|system)s?\b.{0,40}(?:problems?|issues?)",
    "troubleshooting": r"troubleshoot\w*|troubleshot",
    "collaboration": r"\bworked with\b|\bworking with\b|collaborat\w*",
    "communication": r"communicat\w*|clarify (?:\w+ )?requirements",
    "problem-solving": r"\binvestigat\w*|\bdiagnos\w*|\bresolv\w*|\btroubleshoot\w*",
    "analytical": r"\banalys\w*|\binvestigat\w*|\bvalidat\w*",
    "teamwork": r"\bteam\b|colleagues",
    "cross-functional": r"business (?:\w+ )?(?:and|&) (?:\w+ )?(?:technical|reporting) colleagues|technical and business colleagues",
    "ci/cd": r"\bci\b|github actions|continuous integration",
    "testing": r"\btest\w*",
    "monitoring": r"\bmonitor\w*",
    "algorithms": r"\balgorithm\w*",
    "genai": r"genai|\bllm\b|openai|language model|gemini",
    "cloud platforms": r"\baws\b|lambda|dynamodb|iot core",
    "database systems": r"\bsql\b|postgres\w*|dynamodb|sqlite",
    "data pipelines": r"pipeline",
}
# Umbrella skills implied by grounded skills.
IMPLIED_BY = {"nosql": {"dynamodb", "mongodb", "redis", "cassandra", "firestore", "couchdb"},
              "version control": {"git"}, "git": set()}


@dataclass
class EvidenceView:
    """What the candidate's grounded evidence says, for lexical checks."""
    corpus: str                                  # every grounded quote + experience statement, casefolded
    capabilities: dict                           # casefolded name -> evidence tier ("strong"/"partial")
    owns_repositories: bool = False              # the candidate's own GitHub repositories are evidence of Git
    by_entity: dict = field(default_factory=dict)  # project id / experience heading -> casefolded corpus
    sdlc_projects: list = field(default_factory=list)
    implied_capabilities: dict = field(default_factory=dict)


def evidence_view(candidate) -> EvidenceView:
    from jobintel.models.taxonomy import EvidenceSourceType
    by_entity: dict[str, list[str]] = {}
    capabilities, owns_repos = {}, False
    for cap in candidate.capabilities.values():
        if cap.evidence_tier in {"strong", "partial"}:
            capabilities[cap.name.casefold()] = cap.evidence_tier
        for ev in cap.evidence:
            if ev.project_id:
                by_entity.setdefault(ev.project_id, []).append(ev.quote or "")
            if ev.source.source_type == EvidenceSourceType.GITHUB_REPOSITORY_SYNC:
                owns_repos = True
    for item in candidate.experience:
        heading = f"{item.title}, {item.organisation}"
        by_entity.setdefault(heading, []).extend(s.text for s in item.evidence)
        by_entity[heading].append(item.description or "")
    # Only project- or role-scoped evidence counts: an authored CV's unscoped self-listed skills
    # ("Software Engineering: Object-Oriented Programming, Data Structures & Algorithms ...") ground nothing here.
    joined = {k: " ".join(v).casefold() for k, v in by_entity.items()}
    implied = {"git": "the candidate's own GitHub repositories (implied, not a literal Git statement)"} \
        if owns_repos and "git" not in capabilities else {}
    sdlc = [k for k, text in joined.items() if _covers_lifecycle(text)]
    return EvidenceView(" ".join(joined.values()), capabilities, owns_repos, joined, sdlc, implied_capabilities=implied)


def implied_grounding(name: str, view: EvidenceView) -> str | None:
    """Why a required skill with no capability of its own is still grounded (GROUNDED_SYNONYM): Git /
    version control by the candidate's own GitHub repositories, an umbrella (NoSQL) by a grounded member.
    None when it is not. Used by the review gate so an implied skill is not reported as a missing one."""
    key = name.casefold()
    if key in view.implied_capabilities or (key == "version control" and view.owns_repositories):
        return "implied by the candidate's own GitHub repositories"
    implied = IMPLIED_BY.get(key, set()) & set(view.capabilities)
    return "implied by grounded " + ", ".join(sorted(implied)) if implied else None


def _covers_lifecycle(text: str) -> bool:
    """One project's own evidence spans design/implementation, testing AND CI/deployment."""
    stages = (r"\b(?:built|implemented|designed|developed|architecture)\b", r"\btest\w*",
              r"\bci\b|github actions|deploy\w*|docker")
    return all(re.search(s, text) for s in stages)


def load_approvals(root) -> dict:
    """User-approved refresh skills (profile/skill_approvals.json), casefolded name -> record."""
    try:
        data = json.loads((Path(root) / "profile" / "skill_approvals.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name.casefold(): {**record, "name": name} for name, record in (data.get("skills") or {}).items()
            if record.get("status") == USER_APPROVED_REFRESH}


def term_status(term: Term, view: EvidenceView, approvals: dict) -> tuple[str, str]:
    """(status, basis) for one JD term."""
    key = term.key
    if key in view.capabilities:
        return GROUNDED_EXACT, "grounded skill evidence"
    if key in view.implied_capabilities or (key == "version control" and view.owns_repositories):
        return GROUNDED_SYNONYM, "implied by the candidate's own GitHub repositories (Git); no literal statement"
    implied = IMPLIED_BY.get(key, set()) & set(view.capabilities)
    if implied:
        return GROUNDED_SYNONYM, "implied by grounded " + ", ".join(sorted(implied))
    if key == "sdlc" and view.sdlc_projects:
        return GROUNDED_SYNONYM, f"{len(view.sdlc_projects)} project(s) evidence design, testing and CI/deployment"
    if re.search(VOCABULARY[key][1], view.corpus, re.I):
        return GROUNDED_EXACT, "term appears in grounded evidence"
    if key in SYNONYMS and re.search(SYNONYMS[key], view.corpus, re.I):
        return GROUNDED_SYNONYM, "equivalent grounded wording"
    if term.category == HARD and key in approvals:
        return USER_APPROVED_REFRESH, approvals[key].get("source", "user approval")
    return UNSUPPORTED, "no grounded evidence" + ("" if term.category != HARD else " and no user approval")


# --- skills ------------------------------------------------------------------------------------------------

def refresh_skills(terms: list[Term], approvals: dict, view: EvidenceView) -> list[Term]:
    """Approved refresh skills the JD names (important terms only) that evidence does not already ground."""
    return [t for t in important_terms(terms) if t.category == HARD and t.key in approvals
            and t.key not in view.capabilities]


def implied_skills(terms: list[Term], view: EvidenceView) -> list[str]:
    """Umbrella skills the JD names that grounded skills imply (shown under their JD name)."""
    out = []
    for t in important_terms(terms):
        if t.key == "nosql" and IMPLIED_BY["nosql"] & set(view.capabilities):
            out.append("NoSQL")
        if t.key in {"git", "version control"} and view.owns_repositories and "Git" not in out:
            out.append("Git")
        if t.key == "object-oriented design" and "object-oriented design" in view.capabilities:
            out.append("Object-Oriented Design")  # the JD's terminology for a code-grounded capability
    return out


# --- lexical alignment of on-page bullets ---------------------------------------------------------------

@dataclass(frozen=True)
class AlignmentRule:
    term: str            # JD term key that must be present in the JD
    pattern: str         # wording in the bullet
    replacement: str
    requires: str = ""   # the bullet's OWN source evidence must contain this (empty: pure wording equivalent)


ALIGNMENT_RULES = (
    AlignmentRule("collaboration", r"^Worked with\b", "Collaborated with"),
    AlignmentRule("collaboration", r"\bworking with (colleagues|technical colleagues|business)", r"collaborating with \1"),
    # "investigate software ... problems" -> the JD's debugging / troubleshooting words, only when the same
    # source evidence itself describes troubleshooting.
    AlignmentRule("algorithms", r"\b(and |for |with )?deterministic ([\w-]+ )?ranking\b(?! algorithm)",
                  r"\1a deterministic \2ranking algorithm", r"algorithm"),
    AlignmentRule("debugging", r"\bto investigate (software|application|system)", r"to debug and troubleshoot \1",
                  r"troubleshoot"),
    AlignmentRule("troubleshooting", r"\bto investigate (software|application|system)", r"to troubleshoot \1",
                  r"troubleshoot"),
)


def align_text(text: str, jd_keys: set[str], source_corpus: str) -> tuple[str, list[str]]:
    """The bullet in the JD's wording where an explicit rule applies; (text, applied term keys)."""
    applied = []
    for rule in ALIGNMENT_RULES:
        if rule.term not in jd_keys or (rule.requires and not re.search(rule.requires, source_corpus or "", re.I)):
            continue
        if rule.term == "troubleshooting" and "debugging" in applied:
            continue
        new = re.sub(rule.pattern, rule.replacement, text, count=1)
        if new != text:
            text, applied = new, [*applied, rule.term]
    return text, applied


# --- coverage report & backlog -----------------------------------------------------------------------------

def _locations(term: Term, document, extra_skill_names=()) -> list[str]:
    regex = re.compile(VOCABULARY[term.key][1], re.I)
    syn = re.compile(SYNONYMS[term.key], re.I) if term.key in SYNONYMS else None
    out = []
    if document.summary and regex.search(document.summary):
        out.append("summary")
    skills = " ".join(document.skills())
    if regex.search(skills) or (term.key == "version control" and re.search(r"\bgit\b", skills, re.I)):
        out.append("technical skills")
    for project in document.projects:
        if any(regex.search(b.text) or (syn and term.category != HARD and syn.search(b.text)) for b in project.bullets):
            out.append(f"project: {project.name}")
    for entry in document.experience:
        if any(regex.search(d) or (syn and term.category != HARD and syn.search(d)) for d in entry.details):
            out.append(f"internship: {entry.heading}")
    for entry in document.education:
        if any(regex.search(d) for d in entry.details):
            out.append(f"education: {entry.heading}")
    return out


def coverage_report(terms: list[Term], view: EvidenceView, approvals: dict, document) -> list[dict]:
    rows = []
    for term in important_terms(terms):
        status, basis = term_status(term, view, approvals)
        where = _locations(term, document)
        rows.append({"term": term.display, "jd_phrase": term.phrase, "category": term.category,
                     "weight": term.weight, "jd_sections": term.sections, "in_cv": bool(where),
                     "locations": where, "status": status, "basis": basis,
                     **({"reason_omitted": "insufficient grounded evidence" if status == UNSUPPORTED
                         else "grounded but not selected for this page"} if not where else {})})
    return rows


LEARNING_TARGETS = {
    "java": ["syntax and core types", "classes and objects", "OOP: encapsulation, inheritance, interfaces",
             "collections: ArrayList, HashMap", "exceptions", "basic data structures",
             "solve simple coding problems in Java"],
    "kubernetes": ["pods, deployments, services", "kubectl basics", "config maps and secrets",
                   "deploy a small containerised service to a local cluster"],
    "terraform": ["providers, resources, state", "plan / apply workflow", "variables and modules",
                  "provision one small cloud resource end to end"],
    "grafana": ["data sources", "dashboards and panels", "alerting basics"],
    "prometheus": ["metrics types", "PromQL basics", "scrape configuration", "alert rules"],
}
_LANGUAGE_TARGETS = ["syntax and core types", "functions and modules", "OOP / type system basics",
                     "standard collections", "error handling", "solve simple coding problems"]
_DEFAULT_TARGETS = ["core concepts", "one small hands-on exercise", "explain usage and trade-offs in an interview"]
_LANGUAGES = {"java", "c++", "c#", "go", "rust", "kotlin", "scala", "typescript", "javascript", "python"}


def learning_backlog(refresh: list[Term], job, approvals: dict, deadline: str | None = None) -> list[dict]:
    items = []
    for term in refresh:
        targets = LEARNING_TARGETS.get(term.key) or (_LANGUAGE_TARGETS if term.key in _LANGUAGES else _DEFAULT_TARGETS)
        items.append({
            "skill": term.display, "job_id": job.id, "company": job.company, "role": job.title,
            "status": "refresh_required", "skill_status": USER_APPROVED_REFRESH,
            "reason": "explicit JD keyword (ATS coverage); user-approved refresh skill",
            "jd_wording": term.phrase, "jd_sections": term.sections,
            "evidence_state": "no grounded project / internship evidence; listed in Technical Skills only",
            "priority": "high" if term.weight >= 3.0 else "medium",
            "deadline": deadline or "before the online assessment / first interview",
            "learning_goal": f"become interview-ready in {term.display} before later recruitment stages",
            "minimum_interview_ready_target": targets,
            "approval": approvals.get(term.key, {}).get("approved_at", ""),
            "created_at": date.today().isoformat(),
        })
    return items


def persist_backlog(root, items: list[dict]) -> Path:
    """Append backlog items to profile/learning_backlog.json (one per skill + job; never duplicated)."""
    path = Path(root) / "profile" / "learning_backlog.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {"version": 1, "items": []}
    seen = {(i["skill"], i["job_id"]) for i in current["items"]}
    for item in items:
        if (item["skill"], item["job_id"]) not in seen:
            current["items"].append(item)
            seen.add((item["skill"], item["job_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return path
