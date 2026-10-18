"""Final CV editorial / compression layer (2026-10-15).

Runs after repository capability mapping, JD-specific evidence selection and project ranking, and before
page fitting / rendering. It decides what of the already-grounded evidence a recruiter for THIS role should
see, under the CV precedence

    evidence_truth > jd_relevance > evidence_strength > recruiter_clarity > non_redundancy > one_page_readability

It never adds a claim: every transformation either selects among grounded bullets or removes words from one
(inventory lists, endpoint paths, component counts). The CV is not a repository audit report, so a true
detail may be left out when it adds little for the role.

Everything here is generic. Decisions come from the JD's role family (`theme_for`), the capability slice
each bullet's evidence belongs to (`slice_family`), evidence weight and complementarity -- never from a
project, company or domain name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- role family -----------------------------------------------------------------------------------------

# theme -> JD signal pattern. The job TITLE decides first ("Site Reliability Engineer" is an SRE role even
# when its description talks mostly about cloud platforms); the description breaks ties and decides when
# the title names no family.
THEME_PATTERNS = {
    "web": r"\b(react|typescript|javascript|front[- ]?end|web|ui|ux|component|css|browser|next\.js|vue|angular|full[- ]?stack)\b",
    "sre": r"\b(reliab\w*|sre|incident|on-call|observab\w*|monitoring|failure|resilien\w*|uptime|slos?|toil)\b",
    "platform": r"\b(platform|infrastructure|kubernetes|terraform|cloud|ci/cd|devops|containers?|iac)\b",
    "ai": r"\b(machine learning|ml|llms?|genai|generative|gen ai|ai|agents?|rag|nlp|models?)\b",
    "backend": r"\b(backend|back-end|apis?|databases?|sql|microservices?|services?|postgres\w*)\b",
}
TITLE_WEIGHT = 100  # a family named by the title always wins over description wording


# Using AI tools to write software ("Use AI to accelerate development", "AI-assisted coding") describes HOW the
# engineer works, not an AI/ML role: it must not pull a C++/Python/Linux tools role into the AI theme
# based solely on tooling language. AI as the subject of the work still counts.
_AI_TOOLING = re.compile(
    r"\b(?:use|using|uses|leverag\w*|apply|applying|with|adopt\w*|embrac\w*)\s+(?:of\s+)?(?:modern\s+)?(?:gen)?ai"
    r"(?:[- ]assisted|[- ]powered)?\s+(?:tools?|tooling|coding|assistants?|to\s+(?:accelerate|speed|boost|improve)\b[^.]*?\b(?:development|coding|productivity|work))"
    r"|\bai[- ]assisted\s+(?:development|coding|engineering|programming)\b"
    r"|\b(?:accelerate|speed up)\s+(?:your\s+)?development\s+with\s+ai\b(?:\s+tools?)?", re.I)


def theme_for(jd_text: str, job_title: str = "") -> str:
    jd_text = _AI_TOOLING.sub(" ", jd_text or "")
    scores = {}
    for theme, pattern in THEME_PATTERNS.items():
        regex = re.compile(pattern, re.I)
        scores[theme] = TITLE_WEIGHT * len(regex.findall(job_title or "")) + len(regex.findall(jd_text or ""))
    best = max(scores, key=lambda t: (scores[t], t == "backend"))
    return best if scores[best] > 0 else "backend"


# --- capability slices -----------------------------------------------------------------------------------

# A bullet's slice comes from its evidence: code-derived statements carry "<slice>:<kind>" in their id
# (repo_capability_map), curated items carry a category / claim type. Different evidence describing the same
# capability (a frontend's view inventory and a curated UI description) share one family, so a project
# shows each capability once.
_CAPMAP_FAMILY = {
    ("frontend", "views"): "ui", ("ui", "dashboard"): "ui", ("frontend", "integration"): "ui_integration",
    ("frontend", "tests"): "ui_tests", ("backend", "endpoints"): "api", ("delivery", "containers"): "containers",
    ("delivery", "pipeline"): "ci", ("quality", "suite"): "tests", ("data", "libraries"): "persistence",
    ("cloud", "libraries"): "cloud", ("ml", "libraries"): "ml", ("llm", "libraries"): "llm",
    ("systems", "operations"): "orchestration", ("systems", "orchestration"): "orchestration",
    ("systems", "timing"): "orchestration", ("embedded", "firmware"): "embedded", ("embedded", "peripherals"): "embedded",
}
# Code-derived statement kinds that are inventory facts (which libraries / how many Dockerfiles / how many
# tests): true and useful as corroboration, but weaker CV material than a narrative of the same capability.
AUDIT_KINDS = {"libraries", "containers", "suite", "timing"}
AUDIT_FACTOR = 0.55
# Other code-derived statements are generated from one template per slice ("Built a React and TypeScript (Vite)
# frontend with N views ..."): a curated narrative of the same capability reads better, and the same template
# in a second project reads as repetition.
TEMPLATE_FACTOR = 0.85
REPEATED_TEMPLATE_FACTOR = 0.4
# A template statement of a slice the project also describes in a curated narrative.
NARRATIVE_AVAILABLE_FACTOR = 0.6
# A views statement names the project's own screens (its problem shape), so it is not a look-alike template.
DISTINCTIVE_TEMPLATES = {"frontend:views"}


def _curated_family(category: str, claim_type: str) -> str:
    category, claim_type = (category or "").casefold(), (claim_type or "").casefold()
    if category.startswith("frontend"):
        return "ui"
    if category.startswith("backend"):
        return "api"  # a service / API description and an endpoint inventory are the same capability
    if category.startswith("testing"):
        return "evaluation" if claim_type == "evaluation" else "tests"
    if category == "database/search":
        return "retrieval"
    if category == "data/search":
        return "validation"
    if category.startswith("database"):
        return "persistence"
    if category.startswith("data"):
        return "data_processing" if "processing" in category else "data_pipeline"
    if category.startswith("devops"):
        return "orchestration" if claim_type == "automation" else "ci"
    if category.startswith("cloud"):
        return "cloud"
    if category.startswith("ml") or category.startswith("modeling"):
        return "modeling"
    for prefix in ("systems", "networking", "embedded", "algorithm"):
        if category.startswith(prefix):
            return prefix
    return category or "other"


def template_kind(evidence_id: str) -> str | None:
    """The code-derived statement kind ("frontend:views", ...) or None for curated evidence."""
    if ":capmap:" not in (evidence_id or ""):
        return None
    parts = evidence_id.split(":capmap:", 1)[1].split(":")
    return f"{parts[0]}:{parts[-1]}"


def slice_family(evidence_id: str, evidence=None) -> tuple[str, bool]:
    """(capability family, is_audit_level) for one evidence item."""
    if ":capmap:" in (evidence_id or ""):
        parts = evidence_id.split(":capmap:", 1)[1].split(":")
        key = (parts[0], parts[-1])
        return _CAPMAP_FAMILY.get(key, f"{parts[0]}:{parts[-1]}"), parts[-1] in AUDIT_KINDS
    if not (getattr(evidence, "category", "") or getattr(evidence, "claim_type", "")):
        # No slice knowledge: nothing can call this evidence a repeat of another, so it is its own family.
        return f"uncategorised:{evidence_id}", False
    return _curated_family(getattr(evidence, "category", "") or "", getattr(evidence, "claim_type", "") or ""), False


# Capability families grouped into what a role family values. Weights multiply a bullet's evidence weight.
FAMILY_GROUP = {
    "ui": "frontend", "ui_integration": "frontend", "ui_tests": "frontend_quality",
    "api": "api", "service": "api",
    "persistence": "data", "retrieval": "data", "data_pipeline": "data", "validation": "integrity",
    "tests": "quality", "evaluation": "evaluation",
    "ci": "delivery", "containers": "delivery",
    "systems": "systems", "networking": "systems", "orchestration": "systems",
    "cloud": "cloud",
    "ml": "ml", "llm": "ml", "modeling": "ml", "data_processing": "ml",
    "embedded": "embedded", "algorithm": "embedded",
}
THEME_GROUP_WEIGHTS = {
    "web": {"frontend": 3.0, "frontend_quality": 2.6, "api": 1.6, "data": 1.0, "integrity": 1.2, "quality": 1.0,
            "evaluation": 0.6, "delivery": 0.9, "systems": 0.3, "cloud": 0.6, "ml": 0.4, "embedded": 0.2},
    "sre": {"systems": 3.0, "cloud": 2.6, "integrity": 2.0, "quality": 1.9, "delivery": 1.9, "api": 0.6,
            "data": 1.0, "evaluation": 0.8, "frontend": 0.25, "frontend_quality": 0.4, "ml": 0.3, "embedded": 0.6},
    "platform": {"cloud": 3.0, "systems": 2.7, "delivery": 2.6, "quality": 1.7, "integrity": 1.6, "api": 0.9,
                 "data": 1.0, "evaluation": 0.7, "frontend": 0.3, "frontend_quality": 0.4, "ml": 0.3, "embedded": 0.5},
    "ai": {"ml": 3.0, "evaluation": 2.8, "integrity": 2.4, "data": 2.0, "api": 2.0, "quality": 1.6, "delivery": 1.2,
           "cloud": 1.0, "frontend": 0.6, "frontend_quality": 0.5, "systems": 0.4, "embedded": 0.3},
    "backend": {"api": 2.6, "data": 2.3, "integrity": 1.8, "quality": 1.8, "delivery": 1.5, "cloud": 1.5,
                "frontend": 1.0, "frontend_quality": 0.8, "systems": 1.2, "evaluation": 1.0, "ml": 0.8, "embedded": 0.5},
}


def family_weight(theme: str, family: str) -> float:
    return THEME_GROUP_WEIGHTS.get(theme, THEME_GROUP_WEIGHTS["backend"]).get(FAMILY_GROUP.get(family, ""), 1.0)


# Wording a role family values inside any slice (a health/readiness check is reliability evidence even in an
# application bullet). Matching bullets get SIGNAL_FACTOR.
THEME_SIGNALS = {
    "sre": r"health|readiness|fail[- ](?:closed|fast)|retr(?:y|ies)|dedup\w*|idempot\w*|rollback|recover\w*|downtime|"
           r"failover|guard\w*|continuity|lock/unlock|reliab\w*|error (?:status|responses?)",
    "platform": r"automat\w*|orchestrat\w*|health|readiness|\bci\b|pipeline|container\w*|deploy\w*|fail[- ](?:closed|fast)",
    "web": r"loading and error|error states|interactive|responsive|accessib\w*|typed|review actions|component",
    "ai": r"evaluation|leakage|reproducib\w*|abstention|validat\w*|benchmark|inference",
    "backend": r"persist\w*|transaction\w*|migration|typed|validat\w*|error (?:status|responses?)",
}
SIGNAL_FACTOR = 1.25
# For a reliability role, a bullet stating operational reliability (health/readiness, fail-closed, retries ...)
# is weighted at least as reliability-delivery evidence, whatever slice it describes.
THEME_SIGNAL_GROUP = {"sre": "delivery"}
# The operational wording that earns that weight (narrower than THEME_SIGNALS).
THEME_OPERATIONAL = {"sre": r"health|readiness|retr(?:y|ies)|rollback|failover|downtime|recover\w*"}


# --- bullet text compression ------------------------------------------------------------------------------

# "using button, columns, dataframe, expander and sidebar for ..." -- a list of code identifiers.
# Four or more lowercase identifiers; a short technology list ("using Pandas and NumPy") is never touched.
_IDENTIFIER_LIST = re.compile(r"\s+using [a-z_]+(?:, [a-z_]+){2,},? and [a-z_]+(?=\s+(?:for|to)\b)")
# A parenthetical holding only a shape / dimension ("(128 x 259)") -- specification detail.
_DIMENSION_PARENTHETICAL = re.compile(r"\s*\(\s*\d+(?:\s*[x×]\s*\d+)+\s*\)")
# A parenthetical naming code artefacts ("(1 Dockerfile)", "(real model module)", "(3 schema migrations)").
_ARTEFACT_PARENTHETICAL = re.compile(
    r"\s*\((?=[^()]*\b(?:modules?|Dockerfiles?|schema migrations?|source files?)\b)[^()]*\)")
# A parenthetical listing endpoints / paths / template strings ("(e.g. GET /health, POST /predict)",
# "(/api/rooms, /api/recommendations)", "(/tasks/${id})").
_PATH_PARENTHETICAL = re.compile(
    r"\s*\((?=[^()]*(?:\b(?:GET|POST|PUT|PATCH|DELETE)\s+/|\$\{|(?:^|[\s(,])/[A-Za-z{$]))(?:[^()]|\([^()]*\))*\)")
_COUNT_REWRITES = (
    # "with 12 endpoints and 16 Pydantic request/response models" -> "with typed Pydantic request/response models"
    (re.compile(r"\bwith \d+ endpoints and \d+ Pydantic request/response models\b"), "with typed Pydantic request/response models"),
    (re.compile(r"\bwith \d+ endpoints\b"), "with REST endpoints"),
    # ", tested through 4 HTTP-level test files" -> " and HTTP-level tests"
    (re.compile(r",?\s*tested through \d+ HTTP-level test files?"), " and HTTP-level tests"),
    # "... frontend with 5 views (A, B) and 9 reusable components" -> "... frontend with 5 views (A, B)"
    (re.compile(r"\s+and \d+ reusable components\b"), ""),
)
# "Label: value. Label: value." -- a specification dump (shapes, class orders), not an achievement.
_SPEC_SENTENCE = re.compile(r"^[A-Z][^.:]{2,40}:\s+[^.]+$")
# A leading verbless inventory fragment ("437 backend tests, 27 frontend tests, Docker Compose stack, CI.")
_FRAGMENT = re.compile(r"^(?:[\w/+#.-]+(?:\s[\w/+#.-]+){0,3},\s*){2,}[\w/+#.-]+(?:\s[\w/+#.-]+){0,3}$")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text.strip()) if s.strip()]


def is_audit_detail(text: str) -> bool:
    """Specification dumps ("Mel-spectrogram shape: 128 x 259. Class order: ...") are code-audit detail."""
    sentences = [s.rstrip(".") for s in _sentences(text)]
    return len(sentences) >= 2 and sum(1 for s in sentences if _SPEC_SENTENCE.match(s)) >= 2


def compress_text(text: str) -> str:
    """Remove inventory from one bullet without adding anything: endpoint/path lists, component counts and a
    leading verbless inventory fragment. Returns the text unchanged when nothing applies."""
    out = _IDENTIFIER_LIST.sub("", _ARTEFACT_PARENTHETICAL.sub("", _PATH_PARENTHETICAL.sub("", text)))
    out = _DIMENSION_PARENTHETICAL.sub("", out)
    for pattern, repl in _COUNT_REWRITES:
        out = pattern.sub(repl, out)
    sentences = _sentences(out)
    if len(sentences) >= 2 and _FRAGMENT.match(sentences[0].rstrip(".")):
        out = " ".join(sentences[1:])
    out = re.sub(r"\s{2,}", " ", out).replace(" ,", ",").replace(" .", ".").strip()
    return out


# --- per-project bullet selection, differentiation and ordering --------------------------------------------

# Ceilings, never quotas.
TIER_CEILING = {"focus": 4, "secondary": 3, "supporting": 2}
# A bullet scoring below this share of its project's best editorial score does not earn space.
WEAK_SHARE = 0.18
SUPPORTING_WEAK_SHARE = 0.5
# An inventory statement (libraries / Dockerfiles / test counts) never takes a slot after this many bullets.
AUDIT_MAX_POSITION = 2
# Two bullets of one slice in the primary project must share less than this much content.
SAME_SLICE_OVERLAP = 0.3
# Within a project, a bullet sharing this much content with a kept one repeats it; across projects, this much
# makes two bullets near-identical (the same template sentence in two projects).
REPEAT_OVERLAP = 0.4
CROSS_PROJECT_DUPLICATE = 0.6
# A family already shown by a higher-ranked project is worth this much in the next one: each project should
# prove a different capability where its evidence allows.
DIFFERENTIATION_FACTOR = 0.6
# Projects whose JD signal is within this share of the best are near-tied: the one with more distinct
# role-relevant slices leads.
NEAR_TIE = 0.9


@dataclass
class EditorialBullet:
    bullet: object
    family: str
    score: float
    template: str | None = None
    intrinsic: float = 0.0  # the score before cross-project differentiation (used for the weak-signal cut)


@dataclass
class EditorialDecision:
    """Audit record of the pass, stored in the CV's fit actions."""
    theme: str
    order: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    compressed: list[str] = field(default_factory=list)


def _bullet_family(bullet, evidence_by_id) -> tuple[str, bool]:
    ids = list(getattr(bullet, "evidence_ids", []) or [])
    if not ids:
        return "other", False
    return slice_family(ids[0], evidence_by_id.get(ids[0]))


def _content_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9+#./-]{3,}", text.casefold()))


def _overlap(a: str, b: str) -> float:
    wa, wb = _content_words(a), _content_words(b)
    return len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0


# Each JD-named capability a bullet carries adds this much (capped), so of two bullets of one slice the one
# showing the JD's own technologies wins.
JD_CAPABILITY_BONUS = 0.15
JD_CAPABILITY_BONUS_CAP = 0.45


def _scored(project, theme, evidence_by_id, decision, jd_named=frozenset()) -> list[EditorialBullet]:
    out = []
    for bullet in project.bullets:
        if is_audit_detail(bullet.text):
            decision.removed.append(f"{project.project_id}: audit detail: {bullet.text[:60]}")
            continue
        compressed = compress_text(bullet.text)
        if compressed != bullet.text:
            decision.compressed.append(f"{project.project_id}: {bullet.text[:60]}")
            bullet.original_quote = bullet.original_quote or bullet.text
            if getattr(bullet, "provenance", None):
                bullet.provenance = {**bullet.provenance, "rewritten_text": compressed,
                                     "rewrite_rationale": [*bullet.provenance.get("rewrite_rationale", []),
                                                           "editorial:inventory_removed"]}
            bullet.text = compressed
        family, audit = _bullet_family(bullet, evidence_by_id)
        kind = template_kind((bullet.evidence_ids or [""])[0])
        # The role family's view of the slice dominates; literal JD-term matches (bullet.weight) refine it.
        signalled = bool(re.search(THEME_SIGNALS.get(theme, "$^"), bullet.text, re.I))
        weight = family_weight(theme, family)
        if theme in THEME_SIGNAL_GROUP and re.search(THEME_OPERATIONAL[theme], bullet.text, re.I):
            weight = max(weight, THEME_GROUP_WEIGHTS[theme][THEME_SIGNAL_GROUP[theme]])
        score = weight * (1.0 + min(max(bullet.weight, 0.0), 4.0)) ** 0.5
        score *= AUDIT_FACTOR if audit else TEMPLATE_FACTOR if kind else 1.0
        if signalled:
            score *= SIGNAL_FACTOR
        named = len({n.casefold() for n in bullet.capability_names} & jd_named)
        score *= 1.0 + min(JD_CAPABILITY_BONUS * named, JD_CAPABILITY_BONUS_CAP)
        out.append(EditorialBullet(bullet, family, round(score, 4), kind, round(score, 4)))
    narrated = {item.family for item in out if item.template is None}
    for item in out:
        if item.template and item.family in narrated:
            item.score = item.intrinsic = round(item.score * NARRATIVE_AVAILABLE_FACTOR, 4)
    return sorted(out, key=lambda item: -item.score)


def project_signal(scored: list[EditorialBullet]) -> float:
    """JD signal of a project: its strongest distinct-family editorial scores (top 3)."""
    best_by_family: dict[str, float] = {}
    for item in scored:
        best_by_family[item.family] = max(best_by_family.get(item.family, 0.0), item.score)
    return round(sum(sorted(best_by_family.values(), reverse=True)[:3]), 4)


def edit_projects(projects: list, theme: str, evidence_by_id: dict,
                  jd_named: set[str] | None = None) -> tuple[list, EditorialDecision]:
    """Order projects by JD signal, assign focus / secondary / supporting, and keep per project only distinct,
    role-relevant, non-inventory bullets within the tier ceiling, differentiated across projects. A bullet that
    reads like another project's is still kept when it is the page's only evidence of a JD-named capability."""
    jd_named = {name.casefold() for name in (jd_named or ())}
    decision = EditorialDecision(theme=theme)
    scored = {p.project_id: _scored(p, theme, evidence_by_id, decision, jd_named) for p in projects}
    signal = {pid: project_signal(items) for pid, items in scored.items()}
    best = max(signal.values(), default=0.0)

    def order(project):
        # Near-tied role signal: the stronger JD requirement match (selection score) leads, then breadth of
        # distinct slices; outside the tie the role signal decides.
        near = best > 0 and signal[project.project_id] >= best * NEAR_TIE
        distinct = len({i.family for i in scored[project.project_id]}) if near else 0
        return (not near, -(project.selection_score if near else 0.0), -distinct, -signal[project.project_id],
                -project.selection_score)

    ranked = [p for p in sorted(projects, key=order) if scored[p.project_id]]
    used_families: set[str] = set()
    used_templates: set[str] = set()
    page_texts: list[str] = []
    page_capabilities: set[str] = set()
    for rank, project in enumerate(ranked):
        project.emphasis = "focus" if rank == 0 else "secondary" if rank == 1 else "supporting"
        items = [EditorialBullet(i.bullet, i.family,
                                 i.score * (DIFFERENTIATION_FACTOR if i.family in used_families else 1.0)
                                 * (REPEATED_TEMPLATE_FACTOR if i.template in used_templates - DISTINCTIVE_TEMPLATES
                                    else 1.0),
                                 i.template, i.intrinsic)
                 for i in scored[project.project_id]]
        items.sort(key=lambda i: -i.score)
        top = max((i.intrinsic for i in items), default=0.0)
        kept: list[EditorialBullet] = []
        for item in items:
            if len(kept) >= TIER_CEILING[project.emphasis]:
                break
            reason = None
            same = [k for k in kept if k.family == item.family]
            # The primary project may show two clearly distinct mechanisms of one slice (never a second
            # template statement); every other project shows each slice once.
            if (len(same) >= (2 if project.emphasis == "focus" else 1) or (same and item.template)
                    or any(_overlap(item.bullet.text, k.bullet.text) >= SAME_SLICE_OVERLAP for k in same)):
                reason = f"repeats the {item.family} slice"
            elif any(_overlap(item.bullet.text, k.bullet.text) >= REPEAT_OVERLAP for k in kept):
                reason = "repeats a kept bullet"
            elif (item.template not in DISTINCTIVE_TEMPLATES
                  and any(_overlap(item.bullet.text, text) >= CROSS_PROJECT_DUPLICATE for text in page_texts)
                  and not ({n.casefold() for n in item.bullet.capability_names} & jd_named) - page_capabilities):
                reason = "near-identical to another project's bullet"
            elif item.template and item.template.split(":")[-1] in AUDIT_KINDS and len(kept) >= AUDIT_MAX_POSITION:
                reason = "inventory statement after the project's stronger evidence"
            elif kept and item.intrinsic < (SUPPORTING_WEAK_SHARE if project.emphasis == "supporting" else WEAK_SHARE) * top:
                reason = "weak signal for this role"
            if reason:
                decision.removed.append(f"{project.project_id}: {reason}: {item.bullet.text[:60]}")
                continue
            kept.append(item)
        for item in items:
            if item not in kept and not any(item.bullet.text[:60] in r for r in decision.removed):
                decision.removed.append(f"{project.project_id}: over the {project.emphasis} ceiling: {item.bullet.text[:60]}")
        project.bullets = [k.bullet for k in kept]
        page_texts.extend(k.bullet.text for k in kept)
        page_capabilities.update(n.casefold() for k in kept for n in k.bullet.capability_names)
        used_families |= {k.family for k in kept}
        used_templates |= {k.template for k in kept if k.template}
        decision.order.append(f"{project.project_id} ({project.emphasis}, signal {signal[project.project_id]:.2f})")
    return ranked, decision


# --- relevant modules --------------------------------------------------------------------------------------

_MODULE_TOPICS = {
    "software": r"programm|software|computing|algorithm",
    "internet": r"internet|\bweb\b",
    "services": r"services|distributed|cloud",
    "networks": r"network",
    "systems": r"operating system|distributed system|linux",
    "ml": r"machine learning|artificial intelligence|\bdata\b|deep learning",
    "embedded": r"embedded|microcontroller|digital system",
    "telecom": r"mobile|communication",
}
THEME_MODULE_WEIGHTS = {
    "web": {"software": 3, "internet": 2, "services": 2},
    "sre": {"networks": 3, "services": 3, "systems": 2, "software": 2, "internet": 1},
    "platform": {"services": 3, "internet": 2.5, "networks": 2.5, "systems": 2, "software": 2},
    "ai": {"ml": 3, "software": 2, "internet": 1},
    "backend": {"software": 3, "services": 2, "internet": 1.5, "ml": 1},
}
MAX_MODULES = 3
MODULE_SHARE = 0.4
_MODULE_LINE = re.compile(r"^(?P<label>Relevant Modules[^:]*):\s*(?P<items>.+)$", re.I)


def module_score(module: str, theme: str, jd_text: str) -> float:
    lowered, jd = module.casefold(), (jd_text or "").casefold()
    weights = THEME_MODULE_WEIGHTS.get(theme, THEME_MODULE_WEIGHTS["backend"])
    score = sum(w for topic, w in weights.items() if re.search(_MODULE_TOPICS[topic], lowered))
    # The JD naming the module itself ("machine learning", "embedded systems") makes it relevant; one shared
    # word ("communication" skills, continuous "learning") does not.
    if re.search(rf"\b{re.escape(lowered)}\b", jd):
        score += 2
    return score


def select_modules(line: str, theme: str, jd_text: str) -> str | None:
    """A module line cut to the JD-relevant modules (at most MAX_MODULES, authored order); None when no module
    is materially relevant -- the degree title then carries the context. Other lines are returned unchanged."""
    match = _MODULE_LINE.match(line.strip())
    if not match:
        return line
    items = [i.strip() for i in match.group("items").rstrip(".").split(",") if i.strip()]
    scores = {i: module_score(i, theme, jd_text) for i in items}
    best = max(scores.values(), default=0)
    if best <= 0:
        return None
    ranked = sorted((i for i in items if scores[i] >= max(MODULE_SHARE * best, 0.1)), key=lambda i: -scores[i])
    keep = set(ranked[:MAX_MODULES])
    return f"{match.group('label')}: {', '.join(i for i in items if i in keep)}"


# --- technical skills: at most four recruiter-readable groups ---------------------------------------------

# Refines the profile's coarse skill categories (a catch-all "tooling" category holds test runners, build
# tools and systems utilities alike). Skill names only -- never projects or employers.
_SKILL_KIND = {
    "pytest": "testing", "playwright": "frontend_testing", "react testing library": "frontend_testing",
    "vitest": "frontend_testing",
    # An engineering practice, not a technology: it earns a Skills slot only when the JD names it.
    "sql": "database", "object-oriented design": "practice", "vite": "frontend", "streamlit": "data", "docker compose": "devops", "sqlalchemy": "database",
    "alembic": "database", "criu": "systems", "ssh": "systems", "linux": "systems", "tcp/ip": "systems",
    "google genai": "ml", "openai api": "ml", "llm": "ml", "librosa": "ml", "scipy": "data", "matplotlib": "data",
    "statsmodels": "data", "numpy": "data", "pandas": "data", "excel": "business", "power bi": "business",
    "uart": "embedded", "embedded": "embedded", "mqtt": "embedded", "i2c": "embedded", "arm": "embedded",
    "stm32": "embedded",
}
_CATEGORY_KIND = {"language": "language", "framework": "backend", "database": "database", "cloud": "cloud",
                  "data": "data", "ml_ai": "ml", "backend": "backend", "frontend": "frontend", "network": "systems",
                  "embedded": "embedded", "devops": "devops", "domain": "business", "tooling": "devops"}
THEME_SKILL_GROUPS = {
    "web": (("Languages & Frontend", ("language", "frontend")), ("Backend & APIs", ("backend",)),
            ("Data & Databases", ("database", "data")),
            ("Testing, Cloud & DevOps", ("frontend_testing", "testing", "devops", "cloud"))),
    "sre": (("Programming & Scripting", ("language",)), ("Cloud, Systems & Networking", ("cloud", "systems")),
            ("CI/CD, Testing & Tooling", ("devops", "testing")), ("Backend & Data", ("backend", "database", "data"))),
    "platform": (("Programming & Scripting", ("language",)), ("Cloud, Systems & Networking", ("cloud", "systems")),
                 ("CI/CD, Testing & Tooling", ("devops", "testing")), ("Backend & Data", ("backend", "database", "data"))),
    "ai": (("Programming & ML", ("language", "ml")), ("Backend & APIs", ("backend",)),
           ("Data & Retrieval", ("data", "database")), ("Cloud, Testing & DevOps", ("cloud", "devops", "testing"))),
    "backend": (("Programming & Backend", ("language", "backend")), ("Data & Databases", ("database", "data")),
                ("Cloud & DevOps", ("cloud", "devops", "systems")), ("Testing & Frontend", ("testing", "frontend"))),
}
MAX_SKILL_GROUPS = 4
MAX_SKILLS_PER_GROUP = 8
# Kinds a role family leads with: a selected project's evidence of them earns space. Every other kind of
# the family's groups earns space only when the JD names the skill or a project bullet / heading on the
# page carries it.
THEME_PRIMARY_KINDS = {
    "web": {"language", "frontend", "backend", "database", "frontend_testing", "testing", "devops"},
    "sre": {"cloud", "systems", "devops", "testing"},
    "platform": {"cloud", "systems", "devops", "testing"},
    "ai": {"ml", "backend", "testing"},
    "backend": {"backend", "database", "testing", "devops"},
}
# A JD-named skill of a kind the family has no group for joins the group of its nearest kind.
_NEAREST_KIND = {"frontend": "backend", "frontend_testing": "testing", "systems": "devops", "embedded": "language",
                 "business": "data", "ml": "data", "cloud": "devops", "database": "data", "data": "database",
                 "testing": "devops", "backend": "language", "devops": "testing", "language": "backend",
                 "practice": "language"}


def skill_kind(name: str, category) -> str:
    return _SKILL_KIND.get(name.casefold(), _CATEGORY_KIND.get(str(category).split(".")[-1].casefold(), "devops"))


def theme_kinds(theme: str) -> set[str]:
    return {k for _, kinds in THEME_SKILL_GROUPS.get(theme, THEME_SKILL_GROUPS["backend"]) for k in kinds}


def keep_skill(name: str, category, theme: str, jd_named: set[str], on_page: set[str],
               on_bullets: set[str] | None = None) -> bool:
    """A true skill earns space when the JD names it; or when it is one of the role family's primary kinds
    and a selected project evidences it (`on_page`); or when it is another kind of the family's groups and
    the page's own bullets / project headings carry it (`on_bullets`). Anything else is omitted, however true."""
    key = name.casefold()
    if key in jd_named:
        return True
    kind = skill_kind(name, category)
    if kind in THEME_PRIMARY_KINDS.get(theme, THEME_PRIMARY_KINDS["backend"]):
        return key in on_page
    return kind in theme_kinds(theme) and key in (on_bullets if on_bullets is not None else on_page)


def group_skills(ordered: list[str], categories: dict, theme: str):
    """At most MAX_SKILL_GROUPS groups for the role family, ordered by their most relevant member; skills keep
    their relevance order and appear once. A JD-named skill of a kind the family has no group for joins the
    last group."""
    from jobintel.matching.cv_document import SkillGroup
    spec = THEME_SKILL_GROUPS.get(theme, THEME_SKILL_GROUPS["backend"])
    groups = {label: SkillGroup(label=label, skills=[]) for label, _ in spec}
    first_seen: dict[str, int] = {}
    for index, skill in enumerate(ordered):
        kind = skill_kind(skill, categories.get(skill.casefold(), "tooling"))
        label = (next((label for label, kinds in spec if kind in kinds), None)
                 or next((label for label, kinds in spec if _NEAREST_KIND.get(kind) in kinds), spec[-1][0]))
        if skill not in groups[label].skills and len(groups[label].skills) < MAX_SKILLS_PER_GROUP:
            groups[label].skills.append(skill)
            first_seen.setdefault(label, index)
    present = [g for g in groups.values() if g.skills]
    present.sort(key=lambda g: first_seen[g.label])
    return present[:MAX_SKILL_GROUPS]
