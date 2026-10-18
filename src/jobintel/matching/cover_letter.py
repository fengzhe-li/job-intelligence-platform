"""Role-specific cover letters, generated alongside every prepared CV (deterministic draft pipeline).

A letter is composed deterministically from the SAME application CV artifact (its selected projects,
their on-CV technologies, its skills and education), the candidate profile's project descriptions and
experience headings, and the job description's own wording. It covers why this role, why this company,
why me, what I can contribute and what I hope to gain -- as prose, not labelled sections -- and never
repeats the CV's bullet list.

Truth rules (checked before a letter is stored, see `validate`):
* only projects that are on the application CV are named;
* a technology is attributed to past work only if the CV lists it (skills or that project's line);
  user-approved refresh skills (learning backlog) appear only in a sentence that says they are being
  learned, never as historical usage;
* text quoted from the JD is the JD's own wording and is exempt from the technology check.

Output: an editable DOCX (stdlib zip/XML, no extra dependency) and a PDF (reportlab), both A4, 0.5 inch
margins, one page -- verified on the rendered PDF. The text carries no date, so regenerating from the
same inputs is byte-for-byte reproducible and a sealed package hash stays stable.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from jobintel.matching import jd_lexicon

GENERATOR_VERSION = "cover-letter-1"
MARGIN_INCH = 0.5
BODY_PT = 10.5

# --- role family -------------------------------------------------------------------------------------

_FAMILY_BY_ARCHETYPE = {
    "backend": "backend", "platform_systems": "systems", "cloud_devops": "systems",
    "ai_applied": "ai", "forward_deployed_ai": "ai", "fullstack": "fullstack",
}

# What each family's engineering work is about (why-role / contribute / gain wording). Every phrase is a
# habit or interest, never a claim of past usage of a specific technology.
_FAMILY = {
    "backend": {
        "role_noun": "building and running backend services",
        "why_company": "Services that customers rely on every day are where careful backend engineering matters most, and that is where I want to learn.",
        "interest": "designing APIs and data models, validating data at service boundaries, and writing the tests that keep changes safe to ship",
        "contribute": "careful API and database work, explicit validation and error handling, and test coverage that makes a service reliable to change",
        "gain": "experience of running backend services at real production scale",
        "focus_terms": ("rest", "fastapi", "postgresql", "sql", "testing", "ci/cd", "docker", "python", "api"),
    },
    "systems": {
        "role_noun": "building and operating reliable production infrastructure",
        "why_company": "Infrastructure that the rest of an organisation depends on is where reliability and automation matter most, and that is where I want to learn.",
        "interest": "Linux and networking work, automation, and the infrastructure and CI/CD pipelines that other engineers depend on",
        "contribute": "systems-level debugging on Linux, networking fundamentals, and automated, repeatable delivery through CI/CD",
        "gain": "depth in operating distributed infrastructure in production, alongside engineers who have done it for years",
        "focus_terms": ("linux", "docker", "ci/cd", "aws", "python", "kubernetes", "terraform", "testing"),
    },
    "ai": {
        "role_noun": "putting machine learning into real products",
        "why_company": "AI features that reach real users at scale are where evaluation and reliability matter most, and that is where I want to learn.",
        "interest": "building ML systems end to end, from data validation and model evaluation to serving inference behind an API, with results that are reproducible",
        "contribute": "Python engineering around models, careful evaluation and data validation, and the reproducibility and provenance controls that make AI features trustworthy",
        "gain": "experience of taking AI features from prototype to production with real users",
        "focus_terms": ("python", "pytorch", "tensorflow", "llm", "genai", "fastapi", "testing", "docker"),
    },
    "fullstack": {
        "role_noun": "shipping product features across the stack",
        "why_company": "Products that customers use every day are where end-to-end delivery and testing matter most, and that is where I want to learn.",
        "interest": "delivering product features across the stack, from the API and data model to a tested interface",
        "contribute": "backend and frontend integration, pragmatic product delivery, and automated tests on both sides of the API",
        "gain": "experience of shipping product to real users and learning from how they use it",
        "focus_terms": ("react", "typescript", "javascript", "fastapi", "rest", "postgresql", "testing", "python"),
    },
    "general": {
        "role_noun": "solving real engineering problems carefully",
        "why_company": "Software that many people depend on is where careful engineering matters most, and that is where I want to learn.",
        "interest": "solving concrete engineering problems carefully: understanding the requirement, making a sound design choice, and testing it properly",
        "contribute": "a fast learning pace, sound engineering judgement, disciplined testing, and clear collaboration with the people around me",
        "gain": "broad exposure to how a strong engineering organisation designs, reviews and operates software",
        "focus_terms": ("python", "testing", "ci/cd", "sql", "docker", "linux"),
    },
}

_GAIN_SIGNALS = (
    (re.compile(r"\bmentor(?:ing|ship)?\b", re.I), "the mentoring the role offers"),
    (re.compile(r"\brotation", re.I), "the rotations across teams"),
    (re.compile(r"\b(?:structured )?training\b", re.I), "the structured training"),
    (re.compile(r"\bcode review", re.I), "working within a strong code-review culture"),
)


_TITLE_FAMILY = ((re.compile(r"\b(?:infrastructure|platform|sre|site reliability|devops|ubuntu|linux|kernel|systems?|network\w*|cloud)\b", re.I), "systems"),
                 (re.compile(r"\b(?:ai|ml|machine learning|data scien\w*|llm)\b", re.I), "ai"),
                 (re.compile(r"\bfull[- ]?stack\b", re.I), "fullstack"),
                 (re.compile(r"\bback[- ]?end\b", re.I), "backend"))


def role_family(artifact, job_title: str = "") -> str:
    """The JD title's family when it names one (an Ubuntu role is systems work whatever the CV archetype),
    else the CV's writing archetype, else general SWE."""
    for regex, family in _TITLE_FAMILY:
        if regex.search(job_title or ""):
            return family
    archetype = ((getattr(artifact, "writing_quality", None) or {}).get("archetype") or "")
    return _FAMILY_BY_ARCHETYPE.get(archetype, "general")


# --- JD wording ------------------------------------------------------------------------------------------

def _sentences(text: str) -> list[str]:
    text = re.sub(r"(?i)<\s*(?:br|/?p|/?li|/?ul|/?ol|/?div|/?h[1-6]|/?tr|/?table)\b[^>]*>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", re.sub(r"&(?:rsquo|lsquo);", "\u2019", text))
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)  # link targets are not prose
    text = re.sub(r"&nbsp;|\u00a0", " ", text)
    out = []
    for line in text.splitlines():
        for part in re.split(r"(?<=[.!?])\s+", line.strip()):
            part = part.strip(" -•*\t")
            if part:
                out.append(part)
    return out


def _quote(sentence: str, max_words: int = 30) -> str:
    """The sentence (without its final full stop), or its first complete clause when it is too long;
    "" when it cannot be shortened at a clause boundary (never cut mid-thought)."""
    text = sentence.strip().rstrip(".")
    if len(text.split()) <= max_words:
        return text
    for sep in (", and ", "; ", ", which ", ", while ", ", but "):
        head, _, tail = text.partition(sep)
        if not tail:
            continue
        if sep == ", and " and not re.match(r"(?:we|our|it|its|they|their|you|your|the|this|that|these|there)\b", tail, re.I):
            continue  # ", and" closing a list ("capital, trading strategies, and risk ..."), not a new clause
        if 8 <= len(head.split()) <= max_words:
            return head
    return ""


_ROLE_VERBS = re.compile(r"^(?:build|design|develop|own|work|contribute|ship|improve|write|create|maintain|operate|"
                         r"collaborate|implement|deliver|solve|help|support|deploy|automate|investigate|engineer|"
                         r"you(?:'ll| will)?)\b", re.I)
_FLUFF = re.compile(r"\b(?:salary|benefits?|equal opportunit\w*|diversity|apply(?:ing)?|your application|application (?:process|form)|"
                    r"applicants?|visa|sponsor\w*|holidays?|pension|bonus\w*|disabilit\w*|reasonable adjustments?|"
                    r"recruit\w*|interviews?|privacy|cookies?|compensation)\b", re.I)


_RESP_HEADING = re.compile(r"^\s*(?:key\s+)?(?:job\s+)?responsibilities|^\s*what you[\u2019']?(?:ll| will)\s+(?:be\s+)?(?:do|doing|work)|"
                           r"^\s*your role|^\s*the role\b|^\s*about the role|^\s*day[- ]to[- ]day|^\s*a day in the life|"
                           r"^\s*what you[\u2019']?ll be working on|^\s*in this role", re.I)
_NEXT_HEADING = re.compile(r"^\s*(?:what you[\u2019']?(?:ll)? need|requirements|qualifications|about you|who you are|"
                           r"what we(?:[\u2019']re| are) looking for|minimum|preferred|nice to have|benefits|why join)", re.I)


def role_problem_quote(job) -> str:
    """One sentence of the JD's own responsibility wording: what the role actually works on."""
    lines = _sentences(job.description or "")
    start = next((i for i, l in enumerate(lines) if _RESP_HEADING.search(l)), None)
    pools = []
    if start is not None:
        block = []
        for l in lines[start + 1:]:
            if _NEXT_HEADING.search(l):
                break
            block.append(l)
        pools.append(block)
    pools.append(lines)
    def ok(sentence):
        return 6 <= len(sentence.split()) <= 30 and not _FLUFF.search(sentence) \
            and not re.match(r"^you[\u2019']?(?:ll| will) be joining", sentence, re.I)
    clean = lambda x: _quote(x.split(":", 1)[-1].strip() if re.match(r"^[A-Z][\w ]{2,30}:", x) else x)
    if start is not None:
        block = pools[0]
        hit = next((x for x in block if ok(x) and _ROLE_VERBS.search(x)), None) or next((x for x in block if ok(x)), None)
        if hit:
            return clean(hit)
    hit = next((x for x in lines if ok(x) and _ROLE_VERBS.search(x) and not x.casefold().startswith((job.company or "~").casefold())), None)
    return clean(hit) if hit else ""


def company_quote(job, company: str) -> str:
    """The sentence where the employer best describes its own product or mission (not benefits/news)."""
    names = {company.casefold(), (job.company or "").casefold()} - {""}
    best, best_score = "", 0
    for sentence in _sentences(job.description or ""):
        low = sentence.casefold()
        if re.match(r"^(?:ability|experience|as an? |you|your|must|strong|proficien|knowledge|excellent|expected)\b", low):
            continue  # requirement or role lines, not how the employer describes itself
        first_person = re.match(r"^(?:we|our)\b", low) is not None \
            or re.search(r"\b(?:organi[sz]ation|team|group) of engineers\b", low) is not None
        if not (any(n in low for n in names) or first_person) or len(sentence.split()) < 8 or not _quote(sentence) or _FLUFF.search(sentence):
            continue
        score = 1 + 2 * bool(re.search(r"\b(?:mission|builds?|platform|infrastructure|products?|customers?|users?|"
                                       r"millions?|serves?|helps?|powers?|technology)\b", low))
        score -= 3 * bool(re.search(r"\b(?:part of|acquired|merger|announc|award|certified|great place)\b", low))
        score += 2 * bool(re.search(r"\b(?:engineers?|engineering|infrastructure|architect|build|maintain|systems?)\b", low))
        score -= 2 * bool(re.search(r"\bresearch\b", low))
        if score > best_score:
            best, best_score = sentence, score
    return _quote(best) if best else ""


# --- grounded material from the CV / profile -----------------------------------------------------------------

_GERUND = {"build": "building", "design": "designing", "develop": "developing", "own": "owning", "work": "working",
           "contribute": "contributing", "ship": "shipping", "improve": "improving", "write": "writing",
           "create": "creating", "maintain": "maintaining", "operate": "operating", "collaborate": "collaborating",
           "implement": "implementing", "deliver": "delivering", "solve": "solving", "help": "helping",
           "support": "supporting", "deploy": "deploying", "automate": "automating", "investigate": "investigating",
           "engineer": "engineering", "enhance": "enhancing", "see": "seeing", "learn": "learning", "take": "taking",
           "make": "making", "drive": "driving", "lead": "leading", "use": "using", "identify": "identifying",
           "manage": "managing", "test": "testing", "debug": "debugging", "run": "running", "plan": "planning",
           "scale": "scaling", "monitor": "monitoring", "partner": "partnering", "integrate": "integrating",
           "optimise": "optimising", "optimize": "optimizing", "migrate": "migrating", "architect": "architecting"}


def _as_activity(sentence: str) -> str:
    """A JD responsibility as an activity phrase in the letter's own sentence ("Build and operate the APIs
    ..." -> "building and operating the APIs ..."); "" when it is not phrased as an activity."""
    text = re.sub(r"^(?:as an? [^,]{2,40}, )?you(?:[\u2019']ll| will| would)?\s+", "", (sentence or "").strip(), flags=re.I)
    first = text.split(" ", 1)[0].casefold().strip(",")
    if first in _GERUND:
        text = _GERUND[first] + text[len(first):]
    elif not first.endswith("ing"):
        return ""
    if text.rstrip().endswith(":") or re.search(r"\bto:\s*$", text):
        return ""  # a lead-in to a bullet list, not a complete activity
    text = re.sub(r"\bours\b", "yours", re.sub(r"\bour\b", "your", text, flags=re.I), flags=re.I)
    verbs = "|".join(sorted(_GERUND, key=len, reverse=True))
    text = re.sub(rf"(,\s*(?:and\s+)?|\s+and\s+)({verbs})\b", lambda m: m.group(1) + _GERUND[m.group(2).casefold()], text, flags=re.I)
    return text[0].lower() + text[1:] if text else ""


def _company_clause(sentence: str, company: str) -> str:
    """The employer's self-description rephrased to follow "What draws me to X is ...": first person turned
    to second ("Our teams build ..." -> "that your teams build ..."), "X is a ..." -> "its work as a ...",
    "X provides ..." -> "that it provides ..."; a short quotation only when it cannot be rephrased."""
    if not sentence:
        return ""
    text = re.sub(rf"^at {re.escape(company)},\s*", "", sentence.strip(), flags=re.I)
    if re.match(r"^(?:we|our)\b", text, re.I) or re.search(r"\bwe(?:[\u2019']re| are)?\b", text, re.I):
        swaps = ((r"\bwe[\u2019']re\b", "you\u2019re"), (r"\bwe are\b", "you are"), (r"\bwe\b", "you"),
                 (r"\bour\b", "your"), (r"\bours\b", "yours"), (r"\bus\b", "you"))
        for pat, rep in swaps:
            text = re.sub(pat, rep, text, flags=re.I)
        proper = re.match(r"[A-Z]\w*\s+[A-Z]", text) is not None  # "Trading Infrastructure is ..." keeps its capitals
        return "that " + (text if proper else text[0].lower() + text[1:])
    m = re.match(rf"^(?:(as an? [^,]+),\s*)?{re.escape(company)}\s+(is|provides|builds|helps|powers|operates|serves|offers|develops|makes|enables|connects|lets|allows|runs|delivers)\b\s*(.*)$", text, re.I)
    if m:
        lead, verb, rest = m.group(1), m.group(2).casefold(), m.group(3)
        rest = re.sub(r"\bours\b", "yours", re.sub(r"\bour\b", "its", rest, flags=re.I), flags=re.I)
        if verb == "is":
            return ("that it is " + rest) if re.match(r"\w+ing\b", rest) else "its work as " + rest
        return "that" + (f", {lead[0].lower() + lead[1:]}," if lead else "") + f" it {verb} {rest}"
    return f"how you describe it: \u201c{text}\u201d"


_MARKETING = re.compile(r"^(?:production[- ]ready|state[- ]of[- ]the[- ]art|cutting[- ]edge|world[- ]class)\s+", re.I)


def _project_clause(description: str) -> str:
    """The project's purpose in a short clause, from the profile description (not a CV bullet)."""
    first = re.split(r"(?<=[.;])\s|:\s", (description or "").strip())[0].rstrip(".;:")
    first = _MARKETING.sub("", first)
    first = re.sub(r"\s*\([^)]*\)", "", first)               # drop parentheticals (e.g. maths notation)
    m = re.search(r"\s(?:combining|with|using|leveraging|featuring|built on|powered by)\s|,\s", first)
    if m and len(first[:m.start()].split()) >= 4:
        first = first[:m.start()]
    words = first.split()
    if len(words) > 24:
        cut = " ".join(words[:24])
        for sep in (", with ", " with ", ", combining ", " combining ", ", "):
            if sep in cut:
                cut = cut[: cut.rfind(sep)]
                break
        first = cut
    if first and not re.match(r"^[A-Z]{2,}", first):
        first = first[0].lower() + first[1:]
    return first.strip(" ,")


_COUNTABLE = re.compile(r"\b(?:platform|framework|engine|pipeline|service|application|app|system|tool|agent|prototype|"
                        r"project|dashboard|library|api|model|simulator|website|bot|cli)s?\b", re.I)


def _article(phrase: str) -> str:
    head = re.split(r"\s+(?:for|of|that|which|to|with|in|on)\s+", phrase, maxsplit=1)[0]
    if not _COUNTABLE.search(head):
        return phrase  # mass noun phrase ("decision support for ...", "analysis of ..."): no article
    return ("an " if re.match(r"[aeiou]", phrase, re.I) and not re.match(r"(?:uni|use|one)", phrase, re.I) else "a ") + phrase


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


@dataclass
class CoverLetter:
    application_id: str
    job_id: str
    cv_artifact_id: str
    company: str
    role: str
    family: str
    header_name: str
    contact_line: str
    paragraphs: list[str]
    projects: list[str]
    learning_skills: list[str]
    quotes: list[str] = field(default_factory=list)


def compose(candidate, job, artifact, company: str, application_id: str, max_projects: int = 2) -> CoverLetter:
    doc = artifact.document or {}
    family = role_family(artifact, job.title)
    spec = _FAMILY[family]
    role = re.sub(r"\s+", " ", (job.title or "").strip())
    role_short = re.sub(r"\s*[-–—|]\s*(?:20\d\d|London|UK|United Kingdom|Remote|Hybrid|[A-Z][a-z]+, [A-Z].*)\b.*$", "", role).strip() or role
    role_short = re.sub(r"\s*\((?:full[- ]?time|permanent|fixed[- ]term)\)", "", role_short, flags=re.I).strip()
    descriptions = {p.id: p.description for p in getattr(candidate, "projects", [])}
    cv_projects = doc.get("projects", [])[:max_projects]
    skills = {s.casefold() for g in doc.get("skill_groups", []) for s in g.get("skills", [])}
    learning = [b.get("skill") for b in (artifact.learning_backlog or []) if b.get("skill")]
    grounded_skills = skills - {s.casefold() for s in learning}

    activity = _as_activity(role_problem_quote(job))
    company_line = _company_clause(company_quote(job, company), company)
    jd_spans = [x for x in (activity, company_line) if x]

    # Why this role + why this company, in the letter's own words (JD paraphrased, not quoted by template).
    if activity:
        p1 = (f"I am applying for the {role_short} role at {company}. It centres on {activity}, and that is the "
              f"engineering I enjoy most: {spec['interest']}.")
    else:
        p1 = (f"I am applying for the {role_short} role at {company} because it is centred on {spec['role_noun']}, "
              f"which is the engineering I enjoy most: {spec['interest']}.")
    p1 += (f" What draws me to {company} is {company_line}. " if company_line else " ") + spec["why_company"]

    # 3. why me: education, 2 strongest CV projects (described, not bulleted), internships
    edu = doc.get("education", [])
    edu_bits = []
    for entry in edu[:2]:
        heading = entry.get("heading", "")
        m = re.match(r"(.+?):\s*(.+?)(?:\s*\((First Class)\))?$", heading)
        if m:
            uni, degree, honours = m.group(1), m.group(2).replace("&", "and"), m.group(3)
            degree = re.sub(r"^(MSc|BEng|BSc|MEng|MA|BA|MPhil|PhD)\s+", r"\1 in ", degree)
            uni = ("the " + uni) if uni.startswith("University of") else uni
            edu_bits.append((f"a First Class {degree}" if honours else f"an {degree}" if degree[:1] in "AEIOUM" else f"a {degree}") + f" at {uni}")
    p3 = ""
    if edu_bits:
        p3 = "I have " + ("recently completed " + edu_bits[0] + (", after " + edu_bits[1] if len(edu_bits) > 1 else "")) + ". "
    project_bits, used_tech = [], []
    for block in cv_projects:
        clause = _project_clause(descriptions.get(block["project_id"], ""))
        project_bits.append(f"{block['name']} ({_article(clause)})" if clause else block["name"])
        for t in block.get("technologies", []):
            if t.casefold() in spec["focus_terms"] and t not in used_tech and t.casefold() in grounded_skills | {x.casefold() for x in block.get("technologies", [])}:
                used_tech.append(t)
    if project_bits:
        lead = "The projects closest to this role are " if len(project_bits) > 1 else "The project closest to this role is "
        p3 += lead + _join(project_bits) + "."
        lead_desc = re.split(r"(?<=[.])\s+", (descriptions.get(cv_projects[0]["project_id"], "") or "").strip())
        detail = lead_desc[1].strip() if len(lead_desc) > 1 else ""
        on_cv = {t.casefold() for t in cv_projects[0].get("technologies", [])} | grounded_skills
        detail_tech = [d for k, (c, pat, d) in jd_lexicon.VOCABULARY.items() if c == jd_lexicon.HARD and re.search(pat, detail, re.I)]
        if detail and re.match(r"^[A-Z][a-z]+s\b", detail) and len(detail.split()) <= 30 \
                and all(d.casefold() in on_cv for d in detail_tech):
            p3 += f" {cv_projects[0]['name']} {detail[0].lower() + detail[1:]}"
            p3 += "" if p3.endswith(".") else "."
        if len(used_tech) < 3:  # thin project lines: fall back to grounded CV skills in this role family's focus
            used_tech += [n for g in doc.get("skill_groups", []) for n in g.get("skills", [])
                          if n.casefold() in spec["focus_terms"] and n.casefold() in grounded_skills and n not in used_tech]
        if used_tech:
            p3 += f" Across them I worked mainly with {_join(used_tech[:5])}."
    employers = []
    for entry in doc.get("experience", []):
        m = re.match(r"(.+?),\s*(.+?)(?:\s*\(.*\))?$", entry.get("heading", ""))
        if m:
            employers.append(re.sub(r",?\s*(?:Co\.,?\s*)?Ltd\.?$", "", m.group(2)).strip())
    if employers:
        p3 += (f" Through internships at {_join(employers)}, I also learned to investigate software and data problems "
               f"with technical and business colleagues and to work carefully on systems other people rely on.")

    # What I can contribute + what I hope to gain. Strongest evidence only: no learning-gap sentence.
    terms = [t for t in jd_lexicon.important_terms(jd_lexicon.extract_terms(job.description or "", job.title or ""))
             if t.category == jd_lexicon.HARD and t.display.casefold() in grounded_skills]
    already = {t.casefold() for t in used_tech[:5]}
    named = [t.display for t in terms if t.display.casefold() not in already][:4]
    gains = [label for regex, label in _GAIN_SIGNALS if regex.search(job.description or "")][:2]
    p4 = (f"From the start I could contribute {spec['contribute']}"
          + (f", along with hands-on {_join(named)} that the role asks for. " if named else ". ")
          + f"In return, I am looking for {spec['gain']}" + (f", and I would value {_join(gains)}" if gains else "") + ".")
    p6 = "Thank you for considering my application. I would welcome the chance to discuss it."

    return CoverLetter(application_id, job.id, artifact.id, company, role, family,
                       doc.get("name") or candidate.name, doc.get("contact_line", ""),
                       [p for p in (p1, p3, p4, p6) if p], [b["name"] for b in cv_projects], learning, jd_spans)


# --- validation ------------------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.casefold())


def validate(letter: CoverLetter, artifact, page_count: int | None = None) -> list[str]:
    problems = []
    if page_count is not None and page_count != 1:
        problems.append(f"letter renders to {page_count} pages (max 1)")
    paras = [_norm(p) for p in letter.paragraphs]
    for i in range(len(paras)):
        for j in range(i + 1, len(paras)):
            if paras[i] == paras[j] or difflib.SequenceMatcher(None, paras[i], paras[j]).ratio() > 0.85:
                problems.append(f"paragraphs {i + 1} and {j + 1} duplicate each other")
    body = "\n".join(letter.paragraphs)
    if re.search(r"https?://|www\.", body):
        problems.append("letter contains a raw URL")
    if letter.company.casefold() not in body.casefold():
        problems.append(f"company name '{letter.company}' missing")
    if not letter.role or letter.role.split(" ")[0].casefold() not in body.casefold():
        problems.append(f"role '{letter.role}' missing")
    doc = artifact.document or {}
    cv_names = [p["name"] for p in doc.get("projects", [])]
    for name in letter.projects:
        if name not in cv_names:
            problems.append(f"project '{name}' is not on the application CV")
    # technology claims outside JD quotes and outside the learning sentence must be on the CV
    claim_text = body
    for quote in letter.quotes:
        claim_text = claim_text.replace(quote, " ").replace(quote[0].lower() + quote[1:], " ")
    claim_text = re.sub(r"Your stack also includes[^.]*\.", " ", claim_text)
    on_cv = {s.casefold() for g in doc.get("skill_groups", []) for s in g.get("skills", [])}
    on_cv |= {t.casefold() for p in doc.get("projects", []) for t in p.get("technologies", [])}
    learning = {s.casefold() for s in letter.learning_skills}
    for key, (category, pattern, display) in jd_lexicon.VOCABULARY.items():
        if category != jd_lexicon.HARD:
            continue
        if re.search(pattern, claim_text, re.I) and display.casefold() not in on_cv - learning and key not in on_cv - learning:
            problems.append(f"technology '{display}' claimed but not grounded on the CV")
    bullets = [_norm(b) for p in doc.get("projects", []) for b in p.get("bullets", [])]
    for sentence in re.split(r"(?<=[.!?])\s+", body):
        s = _norm(sentence)
        if len(s) > 40 and any(difflib.SequenceMatcher(None, s, b).ratio() > 0.8 for b in bullets):
            problems.append("a sentence repeats a CV bullet: " + sentence[:60])
    return problems


# --- rendering -------------------------------------------------------------------------------------------

def _closing(letter: CoverLetter) -> list[str]:
    return ["Kind regards,", letter.header_name]


def render_pdf(path: Path, letter: CoverLetter) -> int:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer

    margin = MARGIN_INCH * inch
    pages = {"n": 0}

    class _Doc(BaseDocTemplate):
        def afterPage(self):  # noqa: N802 (reportlab hook)
            pages["n"] += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = _Doc(str(path), pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=margin,
               title=f"{letter.header_name} - Cover Letter - {letter.company}", author=letter.header_name,
               creator="jobintel", subject=letter.role, invariant=1)
    # Zero frame padding: Platypus frames pad 6pt by default, which would make the printed margin 0.58in.
    frame = Frame(margin, margin, A4[0] - 2 * margin, A4[1] - 2 * margin, id="body",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="letter", frames=[frame])])
    name = ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=15, leading=18)
    small = ParagraphStyle("small", fontName="Helvetica", fontSize=9.5, leading=12)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=BODY_PT, leading=BODY_PT * 1.38, spaceAfter=7)
    flow = [Paragraph(escape(letter.header_name), name), Paragraph(escape(letter.contact_line), small), Spacer(1, 12),
            Paragraph(escape(f"Re: {letter.role} \u2014 {letter.company}"), ParagraphStyle("re", parent=body, fontName="Helvetica-Bold")),
            Paragraph("Dear Hiring Team,", body)]
    flow += [Paragraph(escape(p), body) for p in letter.paragraphs]
    flow += [Spacer(1, 4)] + [Paragraph(escape(line), ParagraphStyle("c", parent=body, spaceAfter=0)) for line in _closing(letter)]
    doc.build(flow)
    return pages["n"]


def _docx_para(text: str, *, bold=False, size_half_pt=21, after=140) -> str:
    rpr = f'<w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/>{"<w:b/>" if bold else ""}<w:sz w:val="{size_half_pt}"/></w:rPr>'
    return (f'<w:p><w:pPr><w:spacing w:after="{after}" w:line="276" w:lineRule="auto"/></w:pPr>'
            f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')


def write_docx(path: Path, letter: CoverLetter) -> None:
    margin = int(MARGIN_INCH * 1440)
    body = [_docx_para(letter.header_name, bold=True, size_half_pt=30, after=40),
            _docx_para(letter.contact_line, size_half_pt=19, after=240),
            _docx_para(f"Re: {letter.role} \u2014 {letter.company}", bold=True),
            _docx_para("Dear Hiring Team,")]
    body += [_docx_para(p) for p in letter.paragraphs]
    body += [_docx_para(line, after=0) for line in _closing(letter)]
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + "".join(body) +
                f'<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="{margin}" w:right="{margin}" '
                f'w:bottom="{margin}" w:left="{margin}" w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
                '</w:body></w:document>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": document,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))  # fixed timestamp: reproducible bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)


# --- acceptance + storage ----------------------------------------------------------------------------

def accepts_cover_letter(job) -> tuple[bool | None, str]:
    """(accepts, basis). False only when the live Greenhouse form definition clearly has no cover-letter
    field; everything else (Lever, Ashby, own sites, fetch failure) is unknown and gets a letter."""
    from jobintel.connectors.utils import fetch_json
    obs = next((o for o in job.source_observations if o.source_name == "greenhouse"), None)
    board = ((obs.raw_payload or {}).get("_board_token") if obs else None) if obs else None
    if obs is None or not board:
        return None, "form definition not available for this ATS; letter prepared"
    try:
        data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{obs.source_job_id}?questions=true")
    except Exception as exc:  # network: unknown, not a refusal
        return None, f"form definition fetch failed ({type(exc).__name__}); letter prepared"
    labels = [str(q.get("label", "")) for q in (data or {}).get("questions", [])]
    if any("cover letter" in label.casefold() for label in labels):
        return True, "Greenhouse form has a Cover Letter field"
    # An employer page can add a cover-letter upload the ATS definition lacks on some boards, so
    # its absence is not "clearly does not accept": the letter is still prepared, with this basis recorded.
    return None, "Greenhouse form definition lists no cover-letter field (employer page may still accept one); letter prepared"


def _slug(text: str) -> str:
    return "_".join(re.sub(r"[^A-Za-z0-9]+", " ", text or "").split())


def filename_stem(candidate_name: str, company: str, role: str) -> str:
    role = re.sub(r"\s*[-–—|]\s*(?:20\d\d|London|UK|Remote).*$", "", role or "")
    role = re.sub(r"^Graduate Programme 20\d\d:\s*", "Graduate ", role)
    return f"{_slug(candidate_name)}_{_slug(company)}_{_slug(role)}_Cover_Letter"[:120]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def store_dir(root, cv_artifact_id: str) -> Path:
    return Path(root) / "cover_letters" / cv_artifact_id


def load_for_artifact(root, cv_artifact_id: str) -> dict | None:
    """The stored, validated letter record for this CV artifact, with file hashes re-verified."""
    record_path = store_dir(root, cv_artifact_id) / "cover_letter.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if record.get("status") != "ready":
        return record
    for kind in ("pdf", "docx"):
        p = Path(record[f"{kind}_path"])
        if not p.is_file() or _sha(p) != record[f"{kind}_sha256"]:
            return {**record, "status": "invalid", "problems": [f"{kind} file missing or modified"]}
    return record


def generate_for_application(root, candidate, job, artifact, company: str, application_id: str,
                             *, check_form=True) -> dict:
    """Compose, render (PDF + DOCX), validate and store one letter for this application CV. Reuses the
    stored letter when its inputs (CV artifact, JD, generator code) are unchanged."""
    root = Path(root)
    fingerprint = hashlib.sha256(json.dumps({"cv": artifact.id, "cv_sha": artifact.pdf_sha256, "jd": job.description or "",
                                             "title": job.title, "company": company, "code": code_sha(),
                                             "version": GENERATOR_VERSION}, sort_keys=True).encode()).hexdigest()
    existing = load_for_artifact(root, artifact.id)
    if existing and existing.get("fingerprint") == fingerprint and existing.get("status") in {"ready", "not_accepted"}:
        return {**existing, "reused": True}
    accepts, basis = accepts_cover_letter(job) if check_form else (None, "form check skipped")
    out_dir = store_dir(root, artifact.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {"application_id": application_id, "job_id": job.id, "cv_artifact_id": artifact.id, "company": company,
              "role": job.title, "fingerprint": fingerprint, "generator": GENERATOR_VERSION, "accepts": accepts,
              "acceptance_basis": basis}
    if accepts is False:
        record.update(status="not_accepted")
    else:
        for n_projects in (3, 2):  # the 2-3 strongest CV projects; drop to 2 only if 3 would overflow the page
            letter = compose(candidate, job, artifact, company, application_id, max_projects=n_projects)
            stem = filename_stem(letter.header_name, company, job.title)
            pdf, docx = out_dir / f"{stem}.pdf", out_dir / f"{stem}.docx"
            pages = render_pdf(pdf, letter)
            if pages == 1:
                break
        write_docx(docx, letter)
        problems = validate(letter, artifact, page_count=pages)
        record.update(status="ready" if not problems else "invalid", problems=problems, family=letter.family,
                      projects=letter.projects, learning_skills=letter.learning_skills, page_count=pages,
                      pdf_path=str(pdf), docx_path=str(docx), pdf_sha256=_sha(pdf), docx_sha256=_sha(docx),
                      paragraphs=letter.paragraphs)
    (out_dir / "cover_letter.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return {**record, "reused": False}
