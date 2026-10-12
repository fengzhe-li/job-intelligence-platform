from __future__ import annotations

import copy
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobintel.analysis.jd_requirements import JDRequirements, extract_jd_requirements
from jobintel.analysis.role_tracks import SKILL_PATTERNS
from jobintel.matching.cv_document import (
    CV_DOCUMENT_FORMAT,
    CVDocument,
    DatedEntry,
    ProjectBlock,
    clean_list_marker,
    document_to_dict,
    document_to_text,
    group_skills,
    select_base_cv,
)
from jobintel.matching.cv_render import LAYOUT_PRESETS, RenderResult, render_cv_document
from jobintel.matching.project_selection import (
    TRANSFERABLE_DIMENSIONS,
    ProjectMatch,
    select_top_projects_hybrid,
    select_top_projects_tailored,
)
from jobintel.analysis.eligibility import IneligibleJobError, evaluate_job_eligibility
from jobintel.matching import impact_writing
from jobintel.matching.claim_grounding import extract_tech_entities, validate_bullet_claim
from jobintel.matching.review_gate import evaluate_review_gate
from jobintel.matching.tailoring_skill import classify_role_archetype
from jobintel.models.candidate import CandidateProfile, CapabilityEvidence
from jobintel.models.cv_artifact import GeneratedCVArtifact, ProjectBullet
from jobintel.models.eligibility import EligibilityState
from jobintel.models.job import Job
from jobintel.models.taxonomy import EvidenceSourceType
from jobintel.storage.cv_artifact_store import CVArtifactStore

# A tailored CV keeps the candidate's full core structure (header, summary,
# education, skills, key projects, internship experience -- see
# matching.cv_document) and must both FIT one A4 page and USE it.
#
# Recruiter-facing text is never character-sliced or ellipsised: every bullet is
# a complete evidence quote (or a complete sentence of one).
#
# Page space follows evidence priority, not visual symmetry. Projects are split
# dynamically (JD relevance + evidence strength) into FOCUS projects (ceiling
# MAX_FOCUS_BULLETS) and SECONDARY projects (ceiling MAX_SECONDARY_BULLETS);
# ceilings are never quotas -- only distinct evidence quotes become bullets,
# and near-duplicate quotes are down-weighted. Internship detail is JD-aware:
# only bullets that touch the JD keep space up front (at least one per role).
#
# Structural floors: every displayed internship keeps >= 1 grounded bullet, and
# every degree keeps its supporting detail line (a long module list may be cut
# to a concise JD-relevant subset of whole module names, never removed).
#
# Overflow is solved in this order:
#   1. drop repetitive project bullets,
#   2. drop internship bullets above the 1-bullet floor (least JD-relevant first),
#   3. cut long module lists to a concise subset (CONCISE_MODULES),
#   4. drop low-value skills (never JD-named ones, never below MIN_SKILLS),
#   5. step to a denser professional layout preset (normal -> dense -> compact),
#   6. drop the weakest secondary-project bullets,
#   7. drop the weakest secondary project (down to MIN_PROJECTS),
#   then, only if still needed: thin focus projects down to FOCUS_BULLET_FLOOR,
#   shorten a bullet to one complete sentence, cut module lists to
#   MIN_MODULES, and finally go below the focus floor.
# Underfill is then solved by restoring supported content while it still fits:
# focus bullets, secondary bullets, a dropped project, full module lists,
# JD-relevant internship bullets, then remaining evidence-backed skills.
# Page count and utilisation are measured on the REAL rendered PDF.
MIN_PROJECTS = 2
MAX_FOCUS_PROJECTS = 2
MAX_FOCUS_BULLETS = 4
MAX_SECONDARY_BULLETS = 3
MAX_BULLETS_PER_PROJECT = MAX_FOCUS_BULLETS
FOCUS_BULLET_FLOOR = 2
# A project is "focus" when its evidence strength is at least this share of
# the strongest selected project's.
FOCUS_STRENGTH_RATIO = 0.75
# Internship bullets per role on the full (pre-fit) document: the most
# JD-relevant, non-overlapping variants from the role's evidence pool. The page
# fitter then keeps 2-4 per role depending on space (1 only under heavy pressure).
MAX_INTERNSHIP_DETAIL = 4
# Under page pressure a role keeps this many substantive bullets until
# lower-value secondary-project detail has been cut.
INTERNSHIP_DETAIL_FLOOR = 2
# Concrete engineering activities that make an internship bullet technically
# substantive even when it shares no literal term with a narrow JD.
_ENGINEERING_ACTIVITY = re.compile(
    r"\b(?:test(?:s|ing)?|debug(?:ging)?|troubleshoot(?:ing)?|monitor(?:ing)?|configur(?:e|ed|ation)|"
    r"automat(?:e|ed|ing|ion)|databases?|data (?:processing|validation|extraction|analysis)|validat(?:e|ed|ing|ion)|"
    r"development|maintenance|deploy(?:ed|ment)?|scripts?|interfaces?|integration)\b"
)
# Internship topics: one counts for a bullet only when the JD names it too, so the
# same evidence pool yields a different selection and order per JD (e.g. Power BI
# dashboards for a BI role, testing/debugging for a software role).
_EXPERIENCE_TOPICS = tuple(re.compile(pattern) for pattern in (
    r"\bpython\b", r"\bpandas\b", r"\bsql\b|\bdatabases?\b", r"\bexcel\b|\bspreadsheets?\b",
    r"\bpower bi\b|\bdashboards?\b", r"\breport(?:s|ing)?\b", r"\bkpis?\b", r"\binventory\b|\bstock\b",
    r"\bturnover\b", r"\btest(?:s|ing|ed)?\b", r"\bdebug(?:ging)?\b|\btroubleshoot(?:ing)?\b",
    r"\bautomat(?:e|ed|es|ing|ion)\b", r"\bvalidat(?:e|ed|ing|ion)\b", r"\bmonitor(?:ing)?\b",
    r"\bnetwork(?:s|ing)?\b", r"\bfacial\b|\bimage\b|\brecognition\b|\bcomputer vision\b|\bmachine learning\b",
    r"\bsoftware\b", r"\bdata (?:quality|cleaning|processing)\b", r"\brequirements?\b",
))
# A reflective summary ("gained practical experience of ...") restates activities
# other bullets show concretely; it ranks below concrete evidence of equal substance.
_GENERIC_EXPERIENCE = re.compile(r"\bgained (?:practical |hands-on )?experience\b|\bexposure to\b")
GENERIC_EXPERIENCE_FACTOR = 0.5
# Beyond the first two bullets of a role, a variant must score at least this
# share of the role's best variant to earn space (2-4 bullets by relevance).
EXTRA_INTERNSHIP_MIN_SHARE = 0.5
# A quote repeating at least this share of a stronger quote's content is not a
# distinct bullet and is never included; smaller overlaps only down-weight it.
REPETITIVE_OVERLAP = 0.35
# Bullets overlapping a stronger bullet of the same project by at least this
# much count as repetitive -- the first content to go under page pressure.
REDUNDANT_OVERLAP = 0.35
CONCISE_MODULES = 3
MIN_MODULES = 2
_LIST_LINE = re.compile(r"^(?P<label>[^:]{1,40}):\s*(?P<items>.+)$")
MIN_SKILLS = 12
MAX_PROJECT_TECHNOLOGIES = 6
# Skills named by the JD always enter the skills section; other role-relevant
# skills fill it up to this size, and the rest are only added if space remains.
MAX_CORE_SKILLS = 18
MAX_TOTAL_SKILLS = 24
MIN_SHORTENED_SENTENCE_CHARS = 40
NON_JD_EVIDENCE_PRIORITY = 0.5
# Below this share of the usable page height the CV is flagged as under-filled.
MIN_PAGE_UTILIZATION = 0.8
REQUIRED_CONTACT_FIELDS = ("email", "phone")
RECOMMENDED_CONTACT_FIELDS = ("location", "linkedin_url", "github_url")
DEFAULT_PDF_DIR = "cv_artifacts/pdfs"
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "approx.", "incl.")


@dataclass
class _ScoredQuote:
    text: str
    capability_names: set[str]
    evidence_ids: set[str]
    score: float


@dataclass
class _BulletCandidate:
    project_id: str
    capability_names: list[str]
    evidence_ids: list[str]
    text: str
    weight: float
    status: str = "extractive"
    rejection_reasons: list[str] = field(default_factory=list)
    original_quote: str | None = None
    # Share of this bullet's content a stronger bullet of the same project repeats.
    redundancy: float = 0.0
    # Impact & Ownership audit record (impact_writing.BulletRewrite.to_dict()); {} when unused.
    provenance: dict = field(default_factory=dict)


# Default for `generate_cv(writing_policy=...)`: the shipped CV_IMPACT_OWNERSHIP_V1 policy when
# present. Pass None for purely extractive bullets (the pre-policy behaviour).
DEFAULT_WRITING_POLICY = object()
# Evidence below this confidence (or tagged partial) is "partial": any rewrite of it is reviewed.
PARTIAL_EVIDENCE_CONFIDENCE = 0.8


@dataclass
class _ImpactContext:
    """Per-generation state of the Impact & Ownership layer."""
    policy: impact_writing.ImpactPolicy
    archetype: str
    store_root: Path
    experience: dict[str, tuple[Any, impact_writing.BulletRewrite]] = field(default_factory=dict)
    summary_original: str | None = None
    summary_removed: list[str] = field(default_factory=list)
    summary_generic: list[str] = field(default_factory=list)


def generate_cv(
    candidate: CandidateProfile,
    job: Job,
    jd: JDRequirements | None = None,
    max_projects: int = 4,
    application_id: str | None = None,
    store_root: Path | str = "data/local",
    hybrid_result: Any | None = None,
    intelligence_service: Any | None = None,
    policy: Any | None = None,
    as_of_phase: str | None = None,
    github_freshness_state: str | None = None,
    writing_policy: Any = DEFAULT_WRITING_POLICY,
) -> GeneratedCVArtifact:
    """Generate one job-specific, evidence-grounded CV artifact with a real,
    rendered-and-measured one-page PDF.

    Bullets are either carefully rewritten with claim-level grounding validation,
    or composed from real `CapabilityEvidence.quote` text linked via `project_id`;
    nothing unsupported is ever promoted. With a writing policy (default: the shipped
    CV_IMPACT_OWNERSHIP_V1), each deterministic bullet then passes the Impact & Ownership
    composition and Engineering Value Translation (matching.impact_writing): action-first
    wording where the evidence proves personal execution, every rewrite re-checked by claim
    grounding and refused (verbatim evidence kept) if it adds anything the evidence lacks.
    """
    jd = jd or extract_jd_requirements(job)
    if writing_policy is DEFAULT_WRITING_POLICY:
        writing_policy = impact_writing.load_default_policy()
    impact = None
    if writing_policy is not None:
        impact = _ImpactContext(writing_policy, impact_writing.impact_archetype(job.title, classify_role_archetype(job), writing_policy),
                                Path(store_root))
    # 0. Eligibility Gate: Evaluate eligibility BEFORE any project selection, bullet rewriting, or PDF rendering
    eligibility = evaluate_job_eligibility(job, candidate)
    if eligibility.state == EligibilityState.HARD_INELIGIBLE:
        raise IneligibleJobError(f"Cannot generate CV for HARD_INELIGIBLE job: {eligibility.reason}", decision=eligibility)

    if github_freshness_state is None:
        from jobintel.github_sync import check_github_freshness
        github_freshness_state = check_github_freshness(store_root, candidate)

    # 1. Hybrid / Tailored Policy Project Selection
    if policy is not None:
        selected_matches, project_explanations, project_mode = select_top_projects_tailored(
            job, candidate, policy=policy, as_of_phase=as_of_phase, max_projects=max_projects
        )
    else:
        selected_matches, project_explanations, project_mode = select_top_projects_hybrid(
            job, candidate, hybrid_result=hybrid_result, max_projects=max_projects
        )

    # 2. Gather candidate project evidence
    project_evidence: dict[str, list[CapabilityEvidence]] = {}
    for match in selected_matches:
        evs = [
            ev
            for cap in candidate.capabilities.values()
            for ev in cap.evidence
            if ev.project_id == match.project.id
            and ev.source.source_type != EvidenceSourceType.GITHUB_LANGUAGES_API
        ]
        project_evidence[match.project.id] = evs

    # 3. Gemini Bullet Rewriting (if enabled and hybrid result was assisted)
    accepted_rewrites: list[dict] = []
    rejected_suggestions: list[dict] = []
    if intelligence_service is not None and hybrid_result is not None:
        if getattr(getattr(hybrid_result, "metadata", None), "used", "") == "gemini_assisted":
            try:
                accepted_rewrites, rejected_suggestions, _ = intelligence_service.rewrite_bullets(
                    job, candidate, [m.project for m in selected_matches], project_evidence
                )
            except Exception:
                accepted_rewrites, rejected_suggestions = [], []

    rewrites_by_project: dict[str, list[dict]] = {}
    for b in accepted_rewrites:
        rewrites_by_project.setdefault(b["project_id"], []).append(b)

    # 4. Compose project blocks with per-project / per-bullet fallback
    ranked_skills = _rank_skills(candidate, jd, hybrid_result=hybrid_result)
    skill_rank = {name.casefold(): index for index, (name, _) in enumerate(ranked_skills)}
    projects: list[ProjectBlock] = []
    any_rewritten = False
    any_fallback = False

    for match in selected_matches:
        proj_id = match.project.id
        bullets: list[_BulletCandidate] = []
        if proj_id in rewrites_by_project and rewrites_by_project[proj_id]:
            any_rewritten = True
            for rw in rewrites_by_project[proj_id][:MAX_BULLETS_PER_PROJECT]:
                cited_caps = sorted({
                    cap.name
                    for cap in candidate.capabilities.values()
                    if any(ev.id in rw.get("evidence_ids", []) for ev in cap.evidence)
                })
                bullets.append(
                    _BulletCandidate(
                        project_id=proj_id,
                        capability_names=cited_caps,
                        evidence_ids=rw.get("evidence_ids", []),
                        text=rw.get("text", ""),
                        weight=3.0,
                        status="gemini_rewritten",
                    )
                )
        else:
            bullets = _compose_bullets_for_project(match, candidate, jd, impact)
            if accepted_rewrites:
                any_fallback = True

        if bullets:
            projects.append(
                ProjectBlock(
                    project_id=proj_id,
                    name=match.project.name,
                    technologies=_project_technologies(candidate, proj_id, skill_rank),
                    bullets=bullets,
                    selection_score=match.score,
                )
            )

    if any_rewritten and not any_fallback:
        generation_mode = "gemini_assisted"
    elif any_rewritten and any_fallback:
        generation_mode = "gemini_with_fallback"
    else:
        generation_mode = "deterministic"

    # 5. Assemble the full structured document (stable core sections + tailored content)
    categories = {capability.name.casefold(): capability.category for capability in candidate.capabilities.values()}
    core_skills = [name for index, (name, priority) in enumerate(ranked_skills) if priority >= 1.5 or (priority > 0 and index < MAX_CORE_SKILLS)]
    extra_skills = [name for name, _ in ranked_skills if name not in core_skills]
    protected_skills = {name.casefold() for name, priority in ranked_skills if priority >= 2}
    _mark_cross_project_redundancy(projects)
    projects = _assign_emphasis(projects)
    if impact is not None:
        _vary_verbs(projects, impact)
    document = _assemble_document(candidate, jd, projects, group_skills(core_skills, categories), job.description, impact)

    artifact_id = f"cv-{uuid.uuid4().hex[:12]}"
    pdf_path = Path(store_root) / DEFAULT_PDF_DIR / f"{artifact_id}.pdf"

    fitter = _PageFitter(pdf_path, document, jd, job.description, [name for name, _ in ranked_skills], extra_skills, protected_skills, categories)
    document, render_result = fitter.fit()
    cv_text = document_to_text(document)

    included_bullets = [bullet for project in document.projects for bullet in project.bullets]
    experience_statements = {
        clean_list_marker(statement.text): statement for item in candidate.experience for statement in item.evidence
    }
    if impact is not None:  # an impact-composed internship line keeps its source statement
        experience_statements.update({text: statement for text, (statement, _) in impact.experience.items()})
    # A variant's id plus the master units it restates: the audit trail of every internship line.
    included_experience_ids = {
        evidence_id
        for entry in document.experience
        for detail in entry.details
        if detail in experience_statements
        for evidence_id in (experience_statements[detail].id, *experience_statements[detail].grounded_in)
    }
    evidence_ids_used = sorted({evidence_id for bullet in included_bullets for evidence_id in bullet.evidence_ids} | included_experience_ids)
    selected_project_ids = [project.project_id for project in document.projects]
    included_skills = {name.casefold() for name in document.skills()}
    skills = [name for name, _ in ranked_skills if name.casefold() in included_skills]

    matched_required, missing_required, partial_required = _match_required_skills(candidate, jd)
    missing_required_contact = _missing_contact_fields(candidate)
    writing_quality: dict = {}
    writing_reasons: list[str] = []
    if impact is not None:
        writing_quality, writing_reasons = _writing_quality(candidate, document, impact)
    review = evaluate_review_gate(
        jd,
        job,
        matched_required,
        missing_required,
        partial_required_skills=partial_required,
        missing_required_contact_fields=missing_required_contact,
        page_count=render_result.page_count,
        missing_cv_sections=document.missing_sections(),
        page_utilization=render_result.page_utilization,
        min_page_utilization=MIN_PAGE_UTILIZATION,
        github_freshness_state=github_freshness_state,
        writing_review_reasons=writing_reasons,
    )

    return GeneratedCVArtifact(
        id=artifact_id,
        job_id=job.id,
        candidate_id=candidate.id,
        generated_at=datetime.now(timezone.utc),
        jd_snapshot=job.description,
        selected_project_ids=selected_project_ids,
        evidence_ids_used=evidence_ids_used,
        bullets=[
            ProjectBullet(
                project_id=b.project_id,
                capability_names=b.capability_names,
                evidence_ids=b.evidence_ids,
                text=b.text,
                status=b.status,
                rejection_reasons=b.rejection_reasons,
                original_quote=b.original_quote,
                provenance=b.provenance,
            )
            for b in included_bullets
        ],
        skills_included=skills,
        cv_text=cv_text,
        pdf_path=str(render_result.pdf_path),
        page_count=render_result.page_count,
        fits_one_page=render_result.page_count <= 1,
        render_margin_mm=render_result.margin_mm,
        render_body_pt=render_result.body_pt,
        page_utilization=render_result.page_utilization,
        document=document_to_dict(document),
        fit_actions=fitter.actions,
        document_format=CV_DOCUMENT_FORMAT,
        layout_preset=render_result.layout_preset,
        github_freshness_state=github_freshness_state,
        review_status=review.status,
        review_reasons=review.reasons,
        application_id=application_id,
        pdf_sha256=_file_sha256(render_result.pdf_path),
        generation_mode=generation_mode,
        project_selection_explanations=project_explanations,
        rejected_bullet_suggestions=rejected_suggestions,
        writing_policy=impact.policy.id if impact is not None else "",
        writing_quality=writing_quality,
    )


# Artifact ids are always generated as f"cv-{uuid4().hex[:12]}" above; anything
# else is rejected before touching the filesystem.
_CV_ID_PATTERN = re.compile(r"^cv-[0-9a-f]{12}$")


def resolve_cv_pdf(cv_store: CVArtifactStore, cv_version_id: str) -> tuple[Path, GeneratedCVArtifact] | None:
    """Maps a cv_version_id to its PDF ONLY if it is a known, persisted
    artifact whose PDF lives inside this store's own PDF directory. The path
    is rebuilt from the validated id (never taken from a request or trusted
    from the stored `pdf_path`), then resolved and confined to the PDF
    directory, so no request can reach any other file (path traversal,
    symlinks, absolute paths)."""
    if not isinstance(cv_version_id, str) or not _CV_ID_PATTERN.fullmatch(cv_version_id):
        return None
    artifact = cv_store.get(cv_version_id)
    if artifact is None or not artifact.pdf_path:
        return None
    pdf_dir = (cv_store.root / DEFAULT_PDF_DIR).resolve()
    candidate = (pdf_dir / f"{artifact.id}.pdf").resolve()
    if candidate.parent != pdf_dir or not candidate.is_file():
        return None
    if Path(artifact.pdf_path).name != candidate.name:
        return None
    return candidate, artifact


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def generate_and_save_cv(
    candidate: CandidateProfile,
    job: Job,
    cv_store: CVArtifactStore,
    jd: JDRequirements | None = None,
    max_projects: int = 4,
    application_id: str | None = None,
    store_root: Path | str = "data/local",
    hybrid_result: Any | None = None,
    intelligence_service: Any | None = None,
    policy: Any | None = None,
    as_of_phase: str | None = None,
    github_freshness_state: str | None = None,
    writing_policy: Any = DEFAULT_WRITING_POLICY,
) -> GeneratedCVArtifact:
    """`generate_cv` only builds and returns the artifact (and renders the PDF
    to disk) -- it does NOT persist the artifact record itself, so every
    caller must remember to call `cv_store.save(artifact)` too, or the CV
    silently never appears in the CV workbench/application detail/"what CV
    did I submit" views despite a real PDF existing on disk (a real bug this
    project hit once already, in the dashboard's `/cv/generate` route).
    This wrapper does both steps atomically so future callers can't miss it.
    """
    artifact = generate_cv(
        candidate,
        job,
        jd=jd,
        max_projects=max_projects,
        application_id=application_id,
        store_root=store_root,
        hybrid_result=hybrid_result,
        intelligence_service=intelligence_service,
        policy=policy,
        as_of_phase=as_of_phase,
        github_freshness_state=github_freshness_state,
        writing_policy=writing_policy,
    )
    cv_store.save(artifact)
    return artifact


# --- Bullet composition (JD-aware, evidence-grounded, extractive only) ---


def _collect_scored_quotes(match: ProjectMatch, candidate: CandidateProfile, jd: JDRequirements) -> list[_ScoredQuote]:
    project = match.project
    matched_lower = {name.casefold() for name in match.matched_requirements}
    must_have = {name.casefold() for name in jd.must_have_skills}
    preferred = {name.casefold() for name in jd.preferred_skills}
    quotes_by_text: dict[str, _ScoredQuote] = {}

    for capability in candidate.capabilities.values():
        key_name = capability.name.casefold()
        if key_name in matched_lower:
            priority = 3.0 if key_name in must_have else (2.0 if key_name in preferred else 1.0)
        else:
            # The project's other evidence is still real, grounded material --
            # eligible to fill the page, but always ranked below JD-matched evidence.
            priority = NON_JD_EVIDENCE_PRIORITY
        for evidence in capability.evidence:
            # Never attribute evidence from another project to this one: only
            # this project's own linked evidence is eligible.
            if evidence.project_id != project.id:
                continue
            # GitHub language-breakdown evidence ("Python is 39% of the repo by
            # bytes") is real, structured provenance -- it can verify a skill and
            # feed the skills list/evidence tier -- but it's metadata, not
            # narrative achievement text, so it's excluded from bullet prose to
            # avoid CV bullets that read like "GitHub language breakdown for...".
            if evidence.source.source_type == EvidenceSourceType.GITHUB_LANGUAGES_API:
                continue
            text = _strip_inventory_prefix(clean_list_marker(evidence.quote))
            if not text:
                continue
            key = text.casefold()
            contribution = priority * evidence.confidence + _jd_term_overlap(text, jd)
            if key in quotes_by_text:
                existing = quotes_by_text[key]
                existing.capability_names.add(capability.name)
                existing.evidence_ids.add(evidence.id)
                existing.score = max(existing.score, contribution)
            else:
                quotes_by_text[key] = _ScoredQuote(text=text, capability_names={capability.name}, evidence_ids={evidence.id}, score=contribution)

    return sorted(quotes_by_text.values(), key=lambda quote: quote.score, reverse=True)


def _jd_terms(jd: JDRequirements) -> set[str]:
    return {name.casefold() for name in (*jd.must_have_skills, *jd.preferred_skills, *jd.keywords) if name}


def _jd_term_overlap(text: str, jd: JDRequirements) -> float:
    # Whole-term matches only: "sql" must not match inside another word, and a
    # one-letter skill such as "C" must not match every sentence.
    lowered = text.casefold()
    return sum(0.1 for term in _jd_terms(jd) if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered))


def _compose_bullets_for_project(match: ProjectMatch, candidate: CandidateProfile, jd: JDRequirements,
                                 impact: _ImpactContext | None = None) -> list[_BulletCandidate]:
    """One complete evidence quote per bullet -- never sliced, never stitched
    together with another quote. A quote that largely repeats a stronger one is
    down-weighted so it is the first to go (or never gets space). With an impact
    context every candidate then passes Impact & Ownership composition BEFORE the
    per-project ceiling, so the archetype's foregrounded value decides what gets space."""
    bullets: list[_BulletCandidate] = []
    for quote in _collect_scored_quotes(match, candidate, jd):
        redundancy = max((_content_overlap(quote.text, kept.text) for kept in bullets), default=0.0)
        if redundancy >= REPETITIVE_OVERLAP:
            continue
        bullets.append(
            _BulletCandidate(
                project_id=match.project.id,
                capability_names=sorted(quote.capability_names),
                evidence_ids=sorted(quote.evidence_ids),
                text=quote.text,
                weight=quote.score * (1.0 - redundancy),
                redundancy=redundancy,
            )
        )
    if impact is not None:
        bullets = _apply_impact(bullets, match.project, candidate, impact)
    bullets.sort(key=lambda bullet: -bullet.weight)
    return bullets[:MAX_BULLETS_PER_PROJECT]


def _vary_verbs(projects: list[ProjectBlock], impact: _ImpactContext) -> None:
    """Verb variety across the whole document, in page-priority order, BEFORE page fitting (so any
    length change is measured on the real PDF)."""
    ordered = [bullet for project in projects for bullet in project.bullets if bullet.provenance]
    rewrites = [_rewrite_from(bullet.provenance) for bullet in ordered]
    changed = {id(rewrite) for rewrite in impact_writing.vary_repeated_verbs(rewrites, impact.policy)}
    for bullet, rewrite in zip(ordered, rewrites):
        if id(rewrite) in changed:
            bullet.text = rewrite.rewritten_text
            bullet.provenance = rewrite.to_dict()


def _evidence_by_id(candidate: CandidateProfile) -> dict[str, CapabilityEvidence]:
    return {evidence.id: evidence for capability in candidate.capabilities.values() for evidence in capability.evidence if evidence.id}


def _ownership_basis(evidence: list[CapabilityEvidence], policy: impact_writing.ImpactPolicy) -> str:
    """personal_project only when EVERY cited evidence item is the candidate's own project write-up."""
    allowed = set(policy.ownership["personal_execution_source_types"])
    types = {str(item.source.source_type) for item in evidence}
    if evidence and types <= allowed:
        return "personal_project"
    return "evidence_source_" + "+".join(sorted(types)) if types else "unknown"


def _is_partial(evidence: list[CapabilityEvidence]) -> bool:
    return any(item.confidence < PARTIAL_EVIDENCE_CONFIDENCE or (item.confidence_tier or "").casefold() in {"partial", "low"}
               for item in evidence)


def _apply_impact(bullets: list[_BulletCandidate], project, candidate: CandidateProfile, impact: _ImpactContext) -> list[_BulletCandidate]:
    """Impact & Ownership composition + Engineering Value Translation for one project's
    candidates. Each rewrite is re-checked by claim grounding against the bullet's OWN cited
    evidence; a refused rewrite keeps the verbatim quote. Evidence ids never change."""
    by_id = _evidence_by_id(candidate)
    cited = {id(bullet): [by_id[e] for e in bullet.evidence_ids if e in by_id] for bullet in bullets}
    bases = {_ownership_basis(cited[id(bullet)], impact.policy) for bullet in bullets}
    result = []
    # Composition runs per ownership basis so a value clause / first-bullet choice never mixes bases.
    for basis in sorted(bases):
        group = [bullet for bullet in bullets if _ownership_basis(cited[id(bullet)], impact.policy) == basis]
        items = [impact_writing.ProjectItem(
            bullet.text, bullet.evidence_ids, bullet.weight, _is_partial(cited[id(bullet)]),
            lambda text, evidence=cited[id(bullet)]: validate_bullet_claim(text, project, evidence, candidate).rejection_reasons)
            for bullet in group]
        for entry in impact_writing.compose_project(items, project_id=project.id, ownership_basis=basis,
                                                    archetype=impact.archetype, policy=impact.policy):
            bullet, rewrite = group[entry.index], entry.rewrite
            result.append(_BulletCandidate(
                bullet.project_id, bullet.capability_names, bullet.evidence_ids, rewrite.rewritten_text, entry.weight,
                status="impact_rewritten" if rewrite.changed else bullet.status,
                rejection_reasons=bullet.rejection_reasons,
                original_quote=bullet.text if rewrite.changed else bullet.original_quote,
                redundancy=bullet.redundancy, provenance=rewrite.to_dict()))
    return result


_CONTENT_WORD = re.compile(r"[a-z0-9][a-z0-9+#./-]{3,}")


def _content_overlap(text: str, other: str) -> float:
    """Share of the shorter text's content words that the other text repeats.
    Also flags substantial clause repetition (>= 4 consecutive content words)."""
    words_seq = _CONTENT_WORD.findall(text.casefold())
    other_words_seq = _CONTENT_WORD.findall(other.casefold())
    words, other_words = set(words_seq), set(other_words_seq)
    if not words or not other_words:
        return 0.0
    token_overlap = len(words & other_words) / min(len(words), len(other_words))
    if len(words_seq) >= 4 and len(other_words_seq) >= 4:
        ngrams_other = {tuple(other_words_seq[i:i + 4]) for i in range(len(other_words_seq) - 3)}
        if any(tuple(words_seq[i:i + 4]) in ngrams_other for i in range(len(words_seq) - 3)):
            token_overlap = max(token_overlap, 0.40)
    return token_overlap


def _mark_cross_project_redundancy(projects: list[ProjectBlock]) -> None:
    """A bullet that largely repeats a stronger bullet of ANOTHER selected
    project is just as repetitive on the page as a within-project repeat, so it
    is the first to go under page pressure. Weights are left unchanged."""
    for project in projects:
        for bullet in project.bullets:
            stronger = [
                other.text
                for other_project in projects
                if other_project is not project
                for other in other_project.bullets
                if other.weight > bullet.weight
            ]
            bullet.redundancy = max([bullet.redundancy, *(_content_overlap(bullet.text, text) for text in stronger)])


def _project_strength(project: ProjectBlock) -> tuple[float, float]:
    """JD relevance + evidence strength: the project-selection score for this
    job first, then the strength of the project's own strongest bullets."""
    return (project.selection_score, sum(sorted((bullet.weight for bullet in project.bullets), reverse=True)[:3]))


def _assign_emphasis(projects: list[ProjectBlock]) -> list[ProjectBlock]:
    """Split selected projects into focus/secondary, apply the per-tier bullet
    ceilings, and order focus projects first so page space follows evidence
    priority."""
    if not projects:
        return projects
    ranked = sorted(projects, key=lambda project: tuple(-value for value in _project_strength(project)))
    best = _project_strength(ranked[0])[0]
    focus_ids = {project.project_id for project in ranked[:MAX_FOCUS_PROJECTS] if project.selection_score >= best * FOCUS_STRENGTH_RATIO}
    for project in projects:
        project.emphasis = "focus" if project.project_id in focus_ids else "secondary"
        ceiling = MAX_FOCUS_BULLETS if project.emphasis == "focus" else MAX_SECONDARY_BULLETS
        project.bullets = sorted(project.bullets, key=lambda bullet: -bullet.weight)[:ceiling]
    return [project for project in projects if project.emphasis == "focus"] + [project for project in projects if project.emphasis == "secondary"]


def _project_technologies(candidate: CandidateProfile, project_id: str, skill_rank: dict[str, int]) -> list[str]:
    """Evidence-backed technologies of one project, most JD-relevant first."""
    names = {
        capability.name
        for capability in candidate.capabilities.values()
        if capability.evidence_tier != "missing" and any(evidence.project_id == project_id for evidence in capability.evidence)
    }
    ordered = sorted(names, key=lambda name: (skill_rank.get(name.casefold(), len(skill_rank)), name.casefold()))
    return ordered[:MAX_PROJECT_TECHNOLOGIES]


_INVENTORY_SENTENCE = re.compile(r"^[^:.]{1,30}:\s*(?:[^,.:]{1,30},\s*)+[^,.:]{1,30}\.$")


def _strip_inventory_prefix(text: str) -> str:
    """Drop a leading README-style inventory fragment ("Backend: Python,
    FastAPI, Pydantic.") when a complete sentence follows it. Only whole
    sentences are removed; the kept text is still verbatim evidence."""
    sentences = _sentences(text)
    if len(sentences) >= 2 and _INVENTORY_SENTENCE.match(sentences[0]):
        return " ".join(sentences[1:])
    return text


def _sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for part in _SENTENCE_BREAK.split(text):
        if sentences and sentences[-1].casefold().endswith(_ABBREVIATIONS):
            sentences[-1] = f"{sentences[-1]} {part}"
        else:
            sentences.append(part)
    return [sentence.strip() for sentence in sentences if sentence.strip()]


# --- Skills, education, experience, contact (stable base identity) ---


def _rank_skills(
    candidate: CandidateProfile,
    jd: JDRequirements,
    hybrid_result: Any | None = None,
) -> list[tuple[str, float]]:
    """Evidence-backed skills, most JD-relevant first, with their priority
    (>= 2: named by the JD; > 0: JD/role relevant; 0: supported but off-JD)."""
    must_have = {name.casefold() for name in jd.must_have_skills}
    preferred = {name.casefold() for name in jd.preferred_skills}
    semantic_required: set[str] = set()
    semantic_preferred: set[str] = set()

    if hybrid_result is not None and getattr(getattr(hybrid_result, "metadata", None), "used", "") == "gemini_assisted":
        req_map = {r.id: r for r in getattr(hybrid_result, "requirements", [])}
        for match in getattr(hybrid_result, "semantic_matches", []):
            rid = match.get("requirement_id")
            req = req_map.get(rid)
            eid = match.get("evidence_id")
            if req and eid:
                for cap in candidate.capabilities.values():
                    if any(ev.id == eid for ev in cap.evidence):
                        if req.modality == "required":
                            semantic_required.add(cap.name.casefold())
                        else:
                            semantic_preferred.add(cap.name.casefold())

    scored = []
    for capability in candidate.capabilities.values():
        if capability.evidence_tier == "missing":
            continue
        key = capability.name.casefold()
        if key in must_have:
            priority = 4
        elif key in semantic_required:
            priority = 3
        elif key in preferred:
            priority = 2
        elif key in semantic_preferred:
            priority = 1.5
        elif jd.primary_role_track and capability.role_relevance.get(jd.primary_role_track, 0.0) > 0:
            priority = 1
        else:
            priority = 0
        tier_score = 1.0 if capability.evidence_tier == "strong" else 0.5
        relevance = capability.role_relevance.get(jd.primary_role_track, 0.0) if jd.primary_role_track else 0.0
        scored.append((priority, relevance, tier_score, len(capability.evidence), capability.name))
    scored.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3], item[4].casefold()))
    return [(name, priority) for priority, _, _, _, name in scored]


def _match_required_skills(candidate: CandidateProfile, jd: JDRequirements) -> tuple[list[str], list[str], list[str]]:
    matched: list[str] = []
    missing: list[str] = []
    partial: list[str] = []
    for name in jd.must_have_skills:
        capability = candidate.capabilities.get(name.casefold())
        if capability is None or capability.evidence_tier == "missing":
            missing.append(name)
            continue
        matched.append(name)
        if capability.evidence_tier == "partial":
            partial.append(name)
    return matched, missing, partial


def _missing_contact_fields(candidate: CandidateProfile) -> list[str]:
    return [field for field in REQUIRED_CONTACT_FIELDS if not getattr(candidate, field)]


def _contact_line(candidate: CandidateProfile) -> str:
    parts = [candidate.email, candidate.phone, candidate.location, candidate.linkedin_url, candidate.github_url]
    return " | ".join(part for part in parts if part)


def _education_entries(candidate: CandidateProfile) -> list[DatedEntry]:
    return [
        DatedEntry(
            heading=f"{item.programme}, {item.institution} ({item.graduation_year})",
            details=[clean_list_marker(item.description)] if item.description.strip() else [],
        )
        for item in candidate.education
    ]


def _jd_dimensions(jd_text: str) -> list[str]:
    return [pattern for pattern, _ in TRANSFERABLE_DIMENSIONS.values() if re.search(pattern, (jd_text or "").casefold())]


def _experience_relevance(text: str, jd: JDRequirements, jd_dimensions: list[str], jd_topics: list[re.Pattern] = ()) -> float:
    """Internship-bullet value: literal JD terms first, then technical
    substance -- named technologies, concrete engineering activities and
    software-engineering dimensions the JD names. Shared filler words
    ("with", "teams", "improve") count for nothing."""
    lowered = text.casefold()
    technologies = {skill for skill in SKILL_PATTERNS if re.search(rf"(?<!\w){re.escape(skill)}(?!\w)", text)}
    activities = {match.group(0).casefold()[:5] for match in _ENGINEERING_ACTIVITY.finditer(lowered)}
    dimensions = sum(1 for pattern in jd_dimensions if re.search(pattern, lowered))
    topics = sum(1 for pattern in jd_topics if pattern.search(lowered))
    score = (10 * _jd_term_overlap(text, jd) + 0.5 * len(technologies) + 0.25 * len(activities) + 0.5 * dimensions
             + 1.0 * topics)
    return score * GENERIC_EXPERIENCE_FACTOR if _GENERIC_EXPERIENCE.search(lowered) else score


def _jd_topics(jd_text: str) -> list[re.Pattern]:
    lowered = (jd_text or "").casefold()
    return [pattern for pattern in _EXPERIENCE_TOPICS if pattern.search(lowered)]


def _select_experience_statements(statements, jd: JDRequirements | None, jd_text: str) -> list:
    """Per JD: the role's evidence pool ranked by relevance (ties keep the
    authored order), skipping any variant that restates a master unit an
    already-chosen bullet covers, up to MAX_INTERNSHIP_DETAIL."""
    if jd is None:
        return list(statements)[:MAX_INTERNSHIP_DETAIL]
    dimensions, topics = _jd_dimensions(jd_text), _jd_topics(jd_text)
    scores = {statement.id: _experience_relevance(statement.text, jd, dimensions, topics) for statement in statements}
    ranked = sorted(statements, key=lambda statement: -scores[statement.id])
    best = scores[ranked[0].id] if ranked else 0.0
    chosen, covered = [], set()
    for statement in ranked:
        units = set(statement.grounded_in) or {statement.id}
        if units & covered:
            continue
        if len(chosen) >= 2 and scores[statement.id] < EXTRA_INTERNSHIP_MIN_SHARE * best:
            break
        chosen.append(statement)
        covered |= units
        if len(chosen) == MAX_INTERNSHIP_DETAIL:
            break
    return chosen


def _experience_entries(candidate: CandidateProfile, jd: JDRequirements | None = None, jd_text: str = "",
                        impact: _ImpactContext | None = None) -> list[DatedEntry]:
    entries = []
    for item in candidate.experience:
        dates = ""
        if item.start_date:
            dates = f"{item.start_date:%b %Y} – {item.end_date:%b %Y}" if item.end_date else f"{item.start_date:%b %Y} – Present"
        if item.evidence:
            selected = _select_experience_statements(item.evidence, jd, jd_text)
            details = [_impact_experience_line(statement, item, impact) for statement in selected]
        else:
            details = [clean_list_marker(item.description)] if item.description.strip() else []
        entries.append(DatedEntry(heading=f"{item.title}, {item.organisation}", dates=dates, details=details))
    return entries


def _impact_experience_line(statement, experience, impact: _ImpactContext | None) -> str:
    """An internship statement, through the ownership-preserving composition when enabled: only a
    "Supported X by <own activities>" line may become action-first (support qualifier kept);
    Assisted / Participated / Worked-with wording is never upgraded."""
    text = clean_list_marker(statement.text)
    if impact is None:
        return text
    rewrite = impact_writing.compose_bullet(
        text, project_id=experience.id or experience.organisation, evidence_ids=[statement.id, *statement.grounded_in],
        ownership_basis="experience_record", archetype=impact.archetype, policy=impact.policy)
    impact.experience[rewrite.rewritten_text] = (statement, rewrite)
    return rewrite.rewritten_text


def _assemble_document(candidate: CandidateProfile, jd: JDRequirements, projects: list[ProjectBlock], skill_groups, jd_text: str = "",
                       impact: _ImpactContext | None = None) -> CVDocument:
    """Stable core sections come from structured Education/Experience records
    when present, otherwise verbatim from the candidate's own authored CV text.
    A section neither can supply is left empty (and reported), never invented."""
    base = select_base_cv(candidate, _jd_terms(jd))
    authored = base[1] if base else None
    education = _education_entries(candidate) or (copy.deepcopy(authored.education) if authored else [])
    experience = _experience_entries(candidate, jd, jd_text, impact) or (copy.deepcopy(authored.experience) if authored else [])
    summary = authored.summary if authored else None
    if impact is not None and summary:
        impact.summary_original = summary
        summary, impact.summary_removed, impact.summary_generic = impact_writing.narrow_summary(
            summary, _grounded_corpus(candidate), impact.policy)
    return CVDocument(
        name=candidate.name,
        headline=authored.headline if authored else "",
        contact_line=_contact_line(candidate),
        summary=summary,
        education=education,
        skill_groups=skill_groups,
        projects=projects,
        experience=experience,
        base_cv_source_id=base[0].evidence_source.id if base else None,
    )


# --- Real one-page fitting: semantic reduction on overflow, restoration on underfill ---


def _bullet_key(bullet: _BulletCandidate) -> tuple:
    return (bullet.project_id, tuple(bullet.evidence_ids), bullet.status)


class _PageFitter:
    def __init__(
        self,
        pdf_path: Path,
        document: CVDocument,
        jd: JDRequirements,
        jd_text: str,
        skill_order: list[str],
        extra_skills: list[str],
        protected_skills: set[str],
        categories: dict,
    ) -> None:
        self.pdf_path = pdf_path
        self.full = copy.deepcopy(document)
        self.doc = document
        self.jd = jd
        self.jd_words = set(_CONTENT_WORD.findall((jd_text or "").casefold()))
        self.jd_dimensions = _jd_dimensions(jd_text)
        self.jd_topics = _jd_topics(jd_text)
        self.skill_order = {name.casefold(): index for index, name in enumerate(skill_order)}
        self.extra_skills = extra_skills
        self.protected_skills = protected_skills
        self.categories = categories
        self.layout = 0
        self.actions: list[str] = []

    def render(self, doc: CVDocument | None = None) -> RenderResult:
        return render_cv_document(self.pdf_path, doc or self.doc, LAYOUT_PRESETS[self.layout])

    def fit(self) -> tuple[CVDocument, RenderResult]:
        self._budget_internships()
        result = self._reduce()
        if result.page_count > 1:
            return self.doc, result
        # Geometry before deletion: fill the reduced document at the current
        # preset and at each denser professional preset, and keep the LEAST
        # dense preset that leaves the least useful grounded content off the
        # page -- a denser layout is only used when it keeps real content,
        # never merely to shrink the margins or to fit more skills.
        reduced_doc, reduced_layout, reduced_actions = copy.deepcopy(self.doc), self.layout, list(self.actions)
        best = None
        for layout in range(reduced_layout, len(LAYOUT_PRESETS)):
            self.doc, self.layout, self.actions = copy.deepcopy(reduced_doc), layout, list(reduced_actions)
            if layout != reduced_layout:
                preset = LAYOUT_PRESETS[layout]
                self.actions.append(f"layout: preset {preset.name} ({preset.margin_mm:g}mm margins, {preset.body_pt:g}pt body) to keep useful content")
            self._fill()
            missing = self._missing_useful_weight()
            if best is None or missing < best[0] - 1e-9:
                best = (missing, layout, self.doc, self.actions)
            if missing <= 0:
                break
        _, self.layout, self.doc, self.actions = best
        return self.doc, self.render()

    def _reduce(self) -> RenderResult:
        result = self.render()
        reductions = (
            self._drop_repetitive_bullet,
            self._drop_extra_internship_bullet,
            lambda: self._trim_module_list(CONCISE_MODULES),
            self._drop_low_value_skill,
            self._denser_layout,
            self._reduce_secondary_project,
            self._drop_internship_below_floor,
            self._drop_weakest_secondary_project,
            self._narrow_layout,
            self._reduce_focus_project,
            self._shorten_wording,
            lambda: self._trim_module_list(MIN_MODULES),
            self._reduce_below_focus_floor,
        )
        while result.page_count > 1:
            action = next((action for action in (reduce() for reduce in reductions) if action), None)
            if action is None:
                break
            self.actions.append(action)
            result = self.render()
        return result

    def _fill(self) -> list[str]:
        accepted: list[str] = []
        for addition in self._additions():
            trial = copy.deepcopy(self.doc)
            label = addition(trial)
            if label and self.render(trial).page_count <= 1:
                self.doc = trial
                self.actions.append(label)
                accepted.append(label)
        return accepted

    def _missing_useful_weight(self) -> float:
        """Weight of useful content not on the page: focus-project evidence,
        non-repetitive secondary evidence (including whole dropped projects)
        and JD-relevant internship detail."""
        present = {project.project_id: {_bullet_key(bullet) for bullet in project.bullets} for project in self.doc.projects}
        missing = 0.0
        for project in self.full.projects:
            for bullet in project.bullets:
                useful = project.emphasis == "focus" or bullet.redundancy < REDUNDANT_OVERLAP
                if useful and _bullet_key(bullet) not in present.get(project.project_id, set()):
                    missing += max(bullet.weight, 0.1)
        for full_entry, entry in zip(self.full.experience, self.doc.experience):
            missing += sum(1.0 for detail in full_entry.details if _jd_term_overlap(detail, self.jd) > 0 and detail not in entry.details)
        return missing

    # -- relevance: JD-named terms first, JD wording only as a tie-breaker --

    def _relevance(self, text: str) -> float:
        shared = self.jd_words & set(_CONTENT_WORD.findall(text.casefold()))
        return _jd_term_overlap(text, self.jd) + 0.01 * len(shared)

    def _experience_relevance(self, text: str) -> float:
        return _experience_relevance(text, self.jd, self.jd_dimensions, self.jd_topics)

    def _ranked_details(self, details: list[str]) -> list[str]:
        # Stable: equal scores keep the authored order.
        return sorted(details, key=lambda detail: -self._experience_relevance(detail))

    # -- internship budget: >= 1 bullet per role, more when JD-relevant or technically substantive --

    def _budget_internships(self) -> None:
        for entry in self.doc.experience:
            if len(entry.details) <= 1:
                continue
            relevant = [detail for detail in entry.details if self._experience_relevance(detail) > 0]
            budget = max(1, min(len(relevant), MAX_INTERNSHIP_DETAIL))
            keep = self._ranked_details(entry.details)[:budget]
            if len(keep) < len(entry.details):  # entry.details is already in relevance order
                entry.details = [detail for detail in entry.details if detail in keep]
                self.actions.append(f"budget: {entry.heading} limited to {budget} strongest bullet(s)")

    # -- overflow: reductions, strictly in policy order --

    def _drop_repetitive_bullet(self) -> str | None:
        candidates = [
            (project, bullet)
            for project in self.doc.projects
            if len(project.bullets) > 1
            for bullet in project.bullets
            if bullet.redundancy >= REDUNDANT_OVERLAP
        ]
        if not candidates:
            return None
        project, bullet = min(candidates, key=lambda item: item[1].weight)
        project.bullets.remove(bullet)
        return f"overflow: dropped repetitive bullet from {project.project_id}"

    def _drop_extra_internship_bullet(self, floor: int = INTERNSHIP_DETAIL_FLOOR) -> str | None:
        extras = [
            (self._experience_relevance(detail), -index, entry, detail)
            for index, entry in enumerate(self.doc.experience)
            if len(entry.details) > floor
            for detail in entry.details
        ]
        if not extras:
            return None
        _, _, entry, detail = min(extras, key=lambda item: (item[0], item[1]))
        entry.details.remove(detail)
        return f"overflow: dropped extra internship bullet ({entry.heading})"

    def _drop_internship_below_floor(self) -> str | None:
        return self._drop_extra_internship_bullet(floor=1)

    def _module_items(self, line: str) -> tuple[str, list[str]] | None:
        match = _LIST_LINE.match(line)
        if not match:
            return None
        items = [item.strip() for item in match.group("items").rstrip(".").split(",") if item.strip()]
        return (match.group("label"), items) if len(items) >= 2 else None

    def _trim_module_list(self, floor: int) -> str | None:
        """Cut the longest module list by its least JD-relevant module; whole
        module names only, and the line itself always stays."""
        lists = [
            (len(parsed[1]), entry, index, parsed)
            for entry in self.doc.education
            for index, line in enumerate(entry.details)
            for parsed in [self._module_items(line)]
            if parsed and len(parsed[1]) > floor
        ]
        if not lists:
            return None
        _, entry, index, (label, items) = max(lists, key=lambda item: item[0])
        weakest = min(reversed(items), key=self._relevance)
        items = [item for item in items if item != weakest]
        entry.details[index] = f"{label}: {', '.join(items)}"
        return f"overflow: shortened module list of {entry.heading} to {len(items)} modules"

    def _drop_low_value_skill(self) -> str | None:
        skills = self.doc.skills()
        removable = [skill for skill in skills if skill.casefold() not in self.protected_skills]
        if len(skills) <= MIN_SKILLS or not removable:
            return None
        weakest = max(removable, key=lambda skill: self.skill_order.get(skill.casefold(), 0))
        for group in self.doc.skill_groups:
            if weakest in group.skills:
                group.skills.remove(weakest)
        self.doc.skill_groups = [group for group in self.doc.skill_groups if group.skills]
        return f"overflow: dropped lower-value skill {weakest}"

    def _denser_layout(self) -> str | None:
        if self.layout >= 2:
            return None
        self.layout += 1
        preset = LAYOUT_PRESETS[self.layout]
        return f"overflow: layout preset {preset.name} ({preset.margin_mm:g}mm margins, {preset.body_pt:g}pt body)"

    def _narrow_layout(self) -> str | None:
        if self.layout + 1 >= len(LAYOUT_PRESETS):
            return None
        self.layout += 1
        preset = LAYOUT_PRESETS[self.layout]
        return f"overflow: layout preset {preset.name} ({preset.margin_mm:g}mm margins, {preset.body_pt:g}pt body)"

    def _secondary(self) -> list[ProjectBlock]:
        return [project for project in self.doc.projects if project.emphasis == "secondary"]

    def _reduce_secondary_project(self) -> str | None:
        candidates = [(project, bullet) for project in self._secondary() if len(project.bullets) > 1 for bullet in project.bullets]
        if not candidates:
            return None
        project, bullet = min(candidates, key=lambda item: item[1].weight)
        project.bullets.remove(bullet)
        return f"overflow: dropped weakest secondary bullet from {project.project_id}"

    def _drop_weakest_secondary_project(self) -> str | None:
        secondary = self._secondary()
        if not secondary or len(self.doc.projects) <= MIN_PROJECTS:
            return None
        project = min(secondary, key=_project_strength)
        self.doc.projects.remove(project)
        return f"overflow: dropped weakest secondary project {project.project_id}"

    def _reduce_focus_project(self) -> str | None:
        candidates = [
            (project, bullet)
            for project in self.doc.projects
            if project.emphasis == "focus" and len(project.bullets) > FOCUS_BULLET_FLOOR
            for bullet in project.bullets
        ]
        if not candidates:
            return None
        project, bullet = min(candidates, key=lambda item: item[1].weight)
        project.bullets.remove(bullet)
        return f"overflow: dropped weakest focus bullet from {project.project_id}"

    def _reduce_below_focus_floor(self) -> str | None:
        candidates = [(project, bullet) for project in self.doc.projects if len(project.bullets) > 1 for bullet in project.bullets]
        if not candidates:
            return None
        project, bullet = min(candidates, key=lambda item: item[1].weight)
        project.bullets.remove(bullet)
        return f"overflow: dropped protected bullet from {project.project_id} (last resort)"

    def _shorten_wording(self) -> str | None:
        options = [
            (project, index, bullet, sentence)
            for project in self.doc.projects
            for index, bullet in enumerate(project.bullets)
            for sentence in [self._best_sentence(bullet.text)]
            if sentence is not None
        ]
        if not options:
            return None
        project, index, bullet, sentence = max(options, key=lambda item: len(item[2].text) - len(item[3]))
        project.bullets[index] = _with_text(bullet, sentence)
        return f"overflow: shortened a bullet of {project.project_id} to one complete sentence"

    def _best_sentence(self, text: str) -> str | None:
        sentences = [sentence for sentence in _sentences(text) if len(sentence) >= MIN_SHORTENED_SENTENCE_CHARS]
        if len(_sentences(text)) < 2 or not sentences:
            return None
        return max(sentences, key=lambda sentence: (self._relevance(sentence), len(sentence)))

    # -- underfill: restore supported content, highest priority first, while it fits --

    def _additions(self):
        present_projects = {project.project_id: project for project in self.doc.projects}
        full_projects = {project.project_id: project for project in self.full.projects}

        missing_bullets = []
        for project_id, project in present_projects.items():
            current = {_bullet_key(bullet): bullet for bullet in project.bullets}
            for bullet in full_projects[project_id].bullets:
                existing = current.get(_bullet_key(bullet))
                if existing is None or existing.text != bullet.text:
                    missing_bullets.append((project.emphasis != "focus", -bullet.weight, bullet))
        # Repetitive project bullets are restored only after internship evidence.
        repetitive = [item for item in missing_bullets if item[2].redundancy >= REDUNDANT_OVERLAP]
        for _, _, bullet in sorted((item for item in missing_bullets if item not in repetitive), key=lambda item: item[:2]):
            yield lambda doc, bullet=bullet: self._restore_bullet(doc, bullet)

        for project in sorted(self.full.projects, key=_project_strength, reverse=True):
            if project.project_id not in present_projects:
                yield lambda doc, project=project: self._restore_project(doc, project)

        for entry_index, entry in enumerate(self.full.education):
            for line_index in range(len(entry.details)):
                yield lambda doc, entry_index=entry_index, line_index=line_index: self._restore_module_line(doc, entry_index, line_index)

        # Only JD-relevant or technically substantive internship detail earns
        # space beyond the 1-bullet floor; generic bullets never fill the page.
        experience_details = [
            (-self._experience_relevance(detail), entry_index, detail)
            for entry_index, entry in enumerate(self.full.experience)
            for detail in entry.details
            if self._experience_relevance(detail) > 0
        ]
        for _, entry_index, detail in sorted(experience_details, key=lambda item: item[:2]):
            yield lambda doc, entry_index=entry_index, detail=detail: self._restore_detail(doc.experience, self.full.experience, entry_index, detail, "substantive internship bullet")

        for _, _, bullet in sorted(repetitive, key=lambda item: item[:2]):
            yield lambda doc, bullet=bullet: self._restore_bullet(doc, bullet)

        # Supported skills in relevance order: skills dropped under pressure
        # first, then ones that were never in the core set.
        for skill in sorted(self.skill_order, key=self.skill_order.get):
            yield lambda doc, skill=skill: self._add_skill(doc, self._skill_name(skill))

    def _restore_bullet(self, doc: CVDocument, bullet: _BulletCandidate) -> str | None:
        project = next((item for item in doc.projects if item.project_id == bullet.project_id), None)
        if project is None:
            return None
        focus_counts = [len(item.bullets) for item in doc.projects if item.emphasis == "focus"]
        adding = not any(_bullet_key(item) == _bullet_key(bullet) for item in project.bullets)
        # Page space follows priority: a secondary project never grows past
        # the smallest focus project.
        if project.emphasis == "secondary" and adding and focus_counts and len(project.bullets) + 1 > min(focus_counts):
            return None
        order = [_bullet_key(item) for item in next(p for p in self.full.projects if p.project_id == project.project_id).bullets]
        kept = [item for item in project.bullets if _bullet_key(item) != _bullet_key(bullet)]
        project.bullets = sorted([*kept, bullet], key=lambda item: order.index(_bullet_key(item)) if _bullet_key(item) in order else len(order))
        return f"underfill: restored bullet on {project.project_id}"

    def _restore_project(self, doc: CVDocument, project: ProjectBlock) -> str | None:
        if any(item.project_id == project.project_id for item in doc.projects):
            return None
        restored = copy.deepcopy(project)
        restored.bullets = sorted(restored.bullets, key=lambda item: -item.weight)[:1]
        order = [item.project_id for item in self.full.projects]
        doc.projects = sorted([*doc.projects, restored], key=lambda item: order.index(item.project_id))
        return f"underfill: restored project {project.project_id}"

    def _restore_module_line(self, doc: CVDocument, entry_index: int, line_index: int) -> str | None:
        full_line = self.full.education[entry_index].details[line_index]
        if doc.education[entry_index].details[line_index] == full_line:
            return None
        doc.education[entry_index].details[line_index] = full_line
        return f"underfill: restored full module list of {doc.education[entry_index].heading}"

    @staticmethod
    def _restore_detail(entries: list[DatedEntry], full_entries: list[DatedEntry], entry_index: int, detail: str, label: str) -> str | None:
        if entry_index >= len(entries) or detail in entries[entry_index].details:
            return None
        order = full_entries[entry_index].details
        entries[entry_index].details = sorted([*entries[entry_index].details, detail], key=order.index)
        return f"underfill: restored {label}"

    def _skill_name(self, key: str) -> str:
        return next((name for name in (*self.full.skills(), *self.extra_skills) if name.casefold() == key), key)

    def _add_skill(self, doc: CVDocument, skill: str) -> str | None:
        if skill in doc.skills() or len(doc.skills()) >= MAX_TOTAL_SKILLS:
            return None
        ordered = sorted([*doc.skills(), skill], key=lambda name: self.skill_order.get(name.casefold(), len(self.skill_order)))
        doc.skill_groups = group_skills(ordered, self.categories)
        return f"underfill: added supported skill {skill}"


def _grounded_corpus(candidate: CandidateProfile) -> str:
    """All project evidence and internship statements: what a summary claim may rest on."""
    quotes = [evidence.quote for capability in candidate.capabilities.values() for evidence in capability.evidence if evidence.project_id]
    statements = [statement.text for item in candidate.experience for statement in (*item.evidence, *item.master)]
    return " ".join(quotes + statements)


def _writing_quality(candidate: CandidateProfile, document: CVDocument, impact: _ImpactContext) -> tuple[dict, list[str]]:
    """Writing Quality Gate over the FINAL (fitted) document, plus review-gate reasons."""
    project_text: dict[str, str] = {}
    allowed: dict[str, set[str]] = {}
    for capability in candidate.capabilities.values():
        for evidence in capability.evidence:
            if evidence.project_id:
                project_text[evidence.project_id] = project_text.get(evidence.project_id, "") + " " + evidence.quote
                allowed.setdefault(evidence.project_id, set()).add(capability.name.casefold())
    projects_by_id = {project.id: project for project in candidate.projects}
    for pid, text in project_text.items():
        allowed[pid] |= extract_tech_entities(text)
        if pid in projects_by_id:
            allowed[pid] |= extract_tech_entities(projects_by_id[pid].description or "")
    repo = {project.project_id: impact_writing.repo_readme_text(impact.store_root, getattr(projects_by_id.get(project.project_id), "repository_full_name", None))
            for project in document.projects}
    bullets = [b for project in document.projects for b in project.bullets]
    rewrites = [_rewrite_from(b.provenance) for b in bullets if b.provenance]
    experience_lines = [detail for entry in document.experience for detail in entry.details]
    experience_rewrites = [impact.experience[line][1] for line in experience_lines if line in impact.experience]
    report = impact_writing.writing_quality_report(
        [(project.project_id, [(b.text, b.evidence_ids) for b in project.bullets]) for project in document.projects],
        experience_lines, document.summary, impact.policy, project_evidence_text=project_text, project_allowed_tech=allowed,
        repo_text=repo, rewrites=rewrites + experience_rewrites)
    report["archetype"] = impact.archetype
    report["experience_rewrites"] = [r.to_dict() for r in experience_rewrites]
    report["summary"] = {"original": impact.summary_original, "rendered": document.summary,
                         "removed_unsupported_items": impact.summary_removed, "generic_phrases": impact.summary_generic}
    return report, impact_writing.writing_review_reasons(report, rewrites + experience_rewrites)


def _rewrite_from(data: dict) -> impact_writing.BulletRewrite:
    fields = {k: v for k, v in data.items() if k != "changed"}
    return impact_writing.BulletRewrite(**fields)


def _with_text(bullet: _BulletCandidate, text: str) -> _BulletCandidate:
    provenance = dict(bullet.provenance)
    if provenance:
        provenance["rewritten_text"] = text
        provenance["rewrite_rationale"] = [*provenance.get("rewrite_rationale", []), "page_fit:shortened_to_one_complete_sentence"]
    return _BulletCandidate(
        bullet.project_id,
        bullet.capability_names,
        bullet.evidence_ids,
        text,
        bullet.weight,
        status=bullet.status,
        rejection_reasons=bullet.rejection_reasons,
        original_quote=bullet.original_quote or bullet.text,
        redundancy=bullet.redundancy,
        provenance=provenance,
    )
