"""A JD-specific professional summary composed only from the CV's own selected evidence.

History: before 2026-10-14 every CV reused the authored summary; 2026-10-14 summaries were composed per
CV but opened with education status ("Degree student at ...") and listed project names.

Now (2026-10-15, editorial layer) the summary is TARGET IDENTITY FIRST:

* sentence 1 -- what kind of engineer this candidate is for THIS role: the CV heading (the JD's own title,
  truthfully normalised by cv_title) plus a focus drawn from the role family, each focus item kept only
  when the page's evidence supports it;
* sentence 2 -- the real evidence that makes it credible: capability phrases for the capability slices the
  kept project bullets actually show (ranked by the role family's weights), and the technologies those
  bullets carry.

Education is the next section, so status is not repeated (a "Graduate ..." heading stays because the JD
itself names the level). No project names, no employment history, no years of experience, no production /
on-call ownership claims: every phrase is conditional on evidence that is already on the page.
"""
from __future__ import annotations

import re

from jobintel.matching.cv_editorial import family_weight, slice_family, theme_for

# Kept for callers that read theme focus phrases directly.
THEMES = {
    "web": ("interactive web interfaces and frontend/backend integration", "", ("React", "TypeScript", "Vite", "JavaScript", "FastAPI", "REST", "Playwright", "Vitest")),
    "sre": ("Linux systems, service reliability and automation", "", ("Linux", "CRIU", "TCP/IP", "Bash", "AWS", "Docker", "GitHub Actions", "Python")),
    "platform": ("cloud systems, automation and CI/CD", "", ("AWS", "Docker", "GitHub Actions", "CI/CD", "Linux", "Bash", "Python")),
    "ai": ("AI/ML software: models, evaluation and APIs", "", ("Python", "PyTorch", "TensorFlow", "scikit-learn", "Google GenAI", "FastAPI", "Qdrant", "pytest")),
    "backend": ("backend services and data systems", "", ("Python", "FastAPI", "REST", "PostgreSQL", "SQLAlchemy", "Docker", "pytest")),
}

# role family -> focus items: (phrase, families that support it, capabilities that support it)
_FOCUS = {
    "web": (("interactive React/TypeScript applications", {"ui"}, {"react", "typescript"}),
            ("reliable frontend/backend integration", {"ui_integration", "api"}, {"fastapi", "rest"}),
            ("tested UI code", {"ui_tests"}, {"playwright", "vitest", "react testing library"})),
    "sre": (("Linux systems and networking", {"networking", "systems"}, {"linux", "tcp/ip"}),
            ("service continuity|recovery", {"networking", "orchestration", "systems"}, {"criu"}),
            ("cloud reliability", {"cloud"}, {"aws"}),
            ("automation", {"orchestration", "ci"}, {"bash", "github actions"})),
    "platform": (("cloud systems", {"cloud"}, {"aws"}),
                 ("systems automation", {"orchestration", "systems"}, {"bash", "linux"}),
                 ("CI/CD|containerised delivery", {"ci", "containers"}, {"docker", "github actions", "ci/cd"}),
                 ("reliable backend infrastructure", {"api", "service"}, {"fastapi"})),
    "ai": (("AI/ML software", {"ml", "llm", "modeling", "data_processing"}, {"pytorch", "tensorflow", "scikit-learn", "llm"}),
           ("model evaluation", {"evaluation", "validation"}, set()),
           ("Python APIs", {"api", "service"}, {"fastapi"}),
           ("tested, reliable Python systems", {"tests", "ci"}, {"pytest"})),
    "backend": (("Python backend services", {"api", "service"}, {"fastapi"}),
                ("data persistence", {"persistence", "retrieval"}, {"sql", "postgresql"}),
                ("tested delivery", {"tests", "ci"}, {"pytest", "ci/cd"})),
}

# capability family -> (page-text condition or None, evidence phrase)
_EVIDENCE = (
    ("networking", r"\btcp\b", "established-TCP session continuity"),
    ("systems", r"\bcriu\b|checkpoint", "checkpoint/restore process migration"),
    ("orchestration", r"\bvip\b|virtual ip", "automated VIP handover with traffic locking"),
    ("orchestration", None, "scripted multi-host automation"),
    ("cloud", r"lambda|serverless", "event-driven serverless AWS pipelines"),
    ("cloud", None, "cloud service integration"),
    ("ci", r"github actions", "GitHub Actions CI"),
    ("containers", r"docker", "containerised services"),
    ("tests", None, "automated test suites"),
    ("evaluation", None, "reproducible model evaluation"),
    ("modeling", None, "ML model pipelines"),
    ("data_processing", None, "ML data processing"),
    ("ml", None, "ML model pipelines"),
    ("llm", None, "LLM provider integration"),
    ("validation", None, "deterministic validation checks"),
    ("retrieval", None, "cited document retrieval"),
    ("data_pipeline", None, "validated data pipelines"),
    ("persistence", None, "SQL persistence"),
    ("ui", r"react", "multi-view React/TypeScript interfaces"),
    ("ui", None, "web interfaces"),
    ("ui_integration", None, "typed API integration with loading and error states"),
    ("ui_tests", None, "automated frontend tests"),
    ("api", r"docker|container", "containerised API services"),
    ("api", None, "typed REST APIs"),
    ("embedded", None, "embedded C firmware"),
)
MAX_FOCUS = 3
MAX_EVIDENCE = 3
MAX_TECH = 5


def career_stage(education) -> str | None:
    """From the first education entry ("<Institution>: <Programme>", dates); None if there is none.
    Kept for callers; the summary itself no longer opens with it."""
    if not education:
        return None
    entry = education[0]
    heading = getattr(entry, "heading", "") or (entry.get("heading", "") if isinstance(entry, dict) else "")
    dates = getattr(entry, "dates", "") or (entry.get("dates", "") if isinstance(entry, dict) else "")
    if ":" not in heading:
        return None
    institution, programme = (part.strip() for part in heading.split(":", 1))
    end = re.findall(r"[A-Z][a-z]{2,8}\s+\d{4}|\d{4}", dates or "")
    when = end[-1] if end else ""
    if re.search(r"expected", dates or "", re.I):
        return f"{programme} student at {institution}" + (f" (graduating {when})" if when else "")
    return f"{programme} graduate ({institution}{', ' + when if when else ''})"


def _join(items) -> str:
    # A "a|b" item is two list items that belong together ("service continuity|recovery").
    items = [part for item in items for part in str(item).split("|")]
    return ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1] if items else ""


def _kept(projects):
    return [p for p in projects if getattr(p, "emphasis", "") in {"focus", "secondary"}] or list(projects)[:2]


def compose_summary(*, jd_text: str, job_title: str, projects, education, headline: str | None = None,
                    evidence_by_id: dict | None = None, theme: str | None = None) -> str | None:
    """None when the page has no project evidence to ground a summary in (the authored one is used)."""
    if not projects:
        return None
    theme = theme or theme_for(jd_text, job_title)
    # Only focus / secondary projects: their evidence is protected through page fitting (generate_cv
    # recomposes the summary from the FINAL fitted page, so it never cites trimmed evidence).
    bullets = [b for p in _kept(projects) for b in getattr(p, "bullets", [])]
    if not bullets:
        return None
    families = {slice_family((b.evidence_ids or [""])[0], (evidence_by_id or {}).get((b.evidence_ids or [""])[0]))[0]
                for b in bullets}
    carried = {n.casefold(): n for b in bullets for n in (getattr(b, "capability_names", []) or [])}
    page_text = " ".join(b.text for b in bullets).casefold()

    focus = [phrase for phrase, fams, caps in _FOCUS.get(theme, _FOCUS["backend"])
             if fams & families or caps & set(carried)][:MAX_FOCUS]
    if not focus:
        focus = [THEMES[theme][0]]
    identity = (headline or "").strip() or "Software Engineer"
    first = f"{identity} focused on {_join(focus)}."

    candidates = []
    for order, (family, condition, phrase) in enumerate(_EVIDENCE):
        if family in families and (condition is None or re.search(condition, page_text)):
            candidates.append((-family_weight(theme, family), order, phrase))
    phrases, families_used = [], set()
    for _, order, phrase in sorted(candidates):
        family = _EVIDENCE[order][0]
        if phrase not in phrases and family not in families_used:  # one phrase per capability slice
            phrases.append(phrase)
            families_used.add(family)
    phrases = phrases[:MAX_EVIDENCE]
    preferred = THEMES[theme][2]
    tech = [carried[t.casefold()] for t in preferred if t.casefold() in carried][:MAX_TECH]
    if not phrases:
        return first
    second = f"Project work includes {_join(phrases)}" + (f", using {_join(tech)}." if tech else ".")
    return f"{first} {second}"
