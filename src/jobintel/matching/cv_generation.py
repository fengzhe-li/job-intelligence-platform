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
from jobintel.matching.cv_title import CVTitle, cv_title
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
from jobintel.matching import cv_editorial, jd_lexicon
from jobintel.matching.tailoring_skill import classify_role_archetype
from jobintel.models.candidate import CandidateProfile, CapabilityEvidence
from jobintel.models.cv_artifact import GeneratedCVArtifact, ProjectBullet
from jobintel.models.eligibility import EligibilityState
from jobintel.models.job import Job
from jobintel.models.taxonomy import EvidenceSourceType, SkillCategory
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
# Weighted project bullet budget (2026-10-13): the page visibly prioritises the strongest
# JD-matched project instead of spreading bullets evenly. By JD relevance + evidence strength:
#   rank 1  -> "focus" (primary / heavyweight): up to 5 bullets, protected down to 4;
#   rank 2  -> "secondary" (strong secondary):  up to 3 bullets, protected at 3 until late;
#   rank 3+ -> "supporting":                    up to 2 bullets, trimmed first.
# Ceilings are never quotas: only distinct evidence becomes a bullet.
#
# Overflow is solved in this order:
#   1. drop repetitive project bullets,
#   2. cut long module lists to a concise subset (CONCISE_MODULES),
#   3. drop low-value skills (never JD-named ones, never below MIN_SKILLS),
#   4. trim supporting-project bullets (down to 1),
#   5. tighten the layout at the full body size (normal -> dense -> tight -> slim: margins, sides first),
#   6. drop internship bullets above the 2-bullet floor (least JD-relevant first),
#   7. thin the primary project from 5 to 4 bullets,
#   8. drop the weakest supporting project (down to MIN_PROJECTS),
#   9. step to a smaller body size (compact -> narrow),
#   10. thin the strong secondary project from 3 to 2 bullets,
#   then, only if still needed: internship bullets down to 1 per role, drop the secondary
#   project, shorten a bullet to one complete sentence, cut module lists to MIN_MODULES,
#   and finally go below the tier floors.
# Underfill is then solved by restoring supported content while it still fits, tier by
# tier (primary, secondary, supporting bullets), a dropped project, full module lists,
# JD-relevant internship bullets, then remaining evidence-backed skills. A lower tier never
# holds more bullets than a higher one. Page count and utilisation are measured on the
# REAL rendered PDF.
MIN_PROJECTS = 2
MAX_FOCUS_BULLETS = 4  # 2026-10-15 editorial layer: primary 3-4, secondary 2-3, supporting 1-2 (ceilings)
FOCUS_BULLET_FLOOR = 3
MAX_SECONDARY_BULLETS = 3
SECONDARY_BULLET_FLOOR = 2
MAX_SUPPORTING_BULLETS = 2
MAX_BULLETS_PER_PROJECT = MAX_FOCUS_BULLETS
# Candidates per project handed to the editorial pass (cv_editorial), which keeps the distinct, role-relevant ones.
EDITORIAL_CANDIDATES = 12
# Tier order: a lower rank gets page space first and is trimmed last.
TIER_RANK = {"focus": 0, "secondary": 1, "supporting": 2}
TIER_CEILING = {"focus": MAX_FOCUS_BULLETS, "secondary": MAX_SECONDARY_BULLETS, "supporting": MAX_SUPPORTING_BULLETS}
# Projects whose JD-relevance score is at least this share of the best are near-tied: among them
# the one with the most distinct grounded bullets leads (the primary carries the deepest detail).
NEAR_TIE_RATIO = 0.9
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
# Per policy `experience_signals` phrase the role archetype prefers (a selection preference among
# supported statements, below literal JD terms; never a rewording).
ARCHETYPE_SIGNAL_WEIGHT = 0.75
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

    selected_matches, project_explanations = apply_positioning_override(
        job, candidate, selected_matches, project_explanations, store_root)

    from jobintel.matching.project_selection import archetype_category_weights
    category_weights = archetype_category_weights(job)
    theme = cv_editorial.theme_for(job.description, job.title)
    # JD lexical keyword / phrase extraction and the candidate's evidence view (matching.jd_lexicon).
    jd_terms = jd_lexicon.extract_terms(job.description, job.title)
    lexical_view = jd_lexicon.evidence_view(candidate)
    approvals = jd_lexicon.load_approvals(store_root)
    jd_keys = {term.key for term in jd_lexicon.important_terms(jd_terms)}

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
            bullets = _compose_bullets_for_project(match, candidate, jd, impact, category_weights)
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
    # 5a. Final editorial / compression pass (cv_editorial): JD-signal project order, tiers, one bullet per
    # capability slice, inventory removed, projects differentiated -- before page fitting.
    evidence_by_id = _evidence_by_id(candidate)
    jd_named = {name.casefold() for name, priority in ranked_skills if priority >= 2}
    projects, editorial = cv_editorial.edit_projects(projects, theme, evidence_by_id, jd_named)
    # 5a'. JD lexical alignment of the kept project bullets: the employer's own wording where the bullet's own
    # source evidence supports it (never a new claim).
    lexical_actions = _align_project_bullets(projects, jd_keys, lexical_view)
    _mark_cross_project_redundancy(projects)
    if impact is not None:
        _vary_verbs(projects, impact)
    # 5b. Skills: only those the JD names, or that the page's own evidence carries within the role family's
    # skill kinds; at most four groups.
    # Evidence of the SELECTED projects (any of their grounded capabilities), not only the final bullets.
    selected_ids = {project.project_id for project in projects}
    on_page = {capability.name.casefold() for capability in candidate.capabilities.values()
               if capability.evidence_tier != "missing"
               and any(evidence.project_id in selected_ids for evidence in capability.evidence)}
    on_bullets = {name.casefold() for project in projects for bullet in project.bullets for name in bullet.capability_names}
    on_bullets |= {name.casefold() for project in projects for name in project.technologies}
    allowed_skills = [name for name, _ in ranked_skills
                      if cv_editorial.keep_skill(name, categories.get(name.casefold(), "tooling"), theme, jd_named,
                                                 on_page, on_bullets)]
    # Skill status: JD-named umbrella terms implied by grounded skills (NoSQL, Git) and USER_APPROVED_REFRESH
    # skills the JD names join Technical Skills (Skills only -- never a bullet), right after the JD-named ones.
    refresh = jd_lexicon.refresh_skills(jd_terms, approvals, lexical_view)
    # A JD must-have the review gate counts (e.g. "We work mostly in Java, Go ...") is materially valued
    # even below the lexical importance weight: an approved refresh skill for it joins Skills too
    # (user's JD-alignment policy, 2026-10-17). Still Skills-only and learning-backlogged.
    must_have = {name.casefold() for name in jd.must_have_skills}
    refresh += [t for t in jd_terms if t.category == jd_lexicon.HARD and t.key in approvals and t.key in must_have
                and t.key not in lexical_view.capabilities and t not in refresh]
    added =[*jd_lexicon.implied_skills(jd_terms, lexical_view), *(t.display for t in refresh)]
    added = [name for name in added if name.casefold() not in {s.casefold() for s in allowed_skills}]
    for name in added:
        categories.setdefault(name.casefold(), _LEXICAL_SKILL_CATEGORY.get(name.casefold(), SkillCategory.TOOLING))
    lead = [name for name in allowed_skills if name.casefold() in jd_named]
    allowed_skills = [*lead, *added, *(name for name in allowed_skills if name not in lead)]
    core_skills = allowed_skills[:MAX_CORE_SKILLS]
    extra_skills = [name for name in allowed_skills if name not in core_skills]
    protected_skills = jd_named | {name.casefold() for name in added}
    document, title = _assemble_document(candidate, jd, projects, cv_editorial.group_skills(core_skills, categories, theme),
                                         job.description, impact, job_title=job.title, theme=theme,
                                         evidence_by_id=evidence_by_id)
    lexical_actions += _align_experience(document, jd_keys, lexical_view, impact)

    artifact_id = f"cv-{uuid.uuid4().hex[:12]}"
    pdf_path = Path(store_root) / DEFAULT_PDF_DIR / f"{artifact_id}.pdf"

    fitter = _PageFitter(pdf_path, document, jd, job.description, allowed_skills, extra_skills, protected_skills, categories,
                         archetype_signals=_archetype_signals(impact), theme=theme)
    document, render_result = fitter.fit()
    # The summary may cite only evidence that survived page fitting: recompose it from the FINAL page.
    if impact is not None:
        final_summary = _compose_final_summary(document, job, impact, candidate, theme, evidence_by_id)
        if final_summary and final_summary != document.summary:
            document.summary = final_summary
            fitter.doc = document
            render_result = fitter._reduce()
            document = fitter.doc
            fitter.actions.append("editorial: summary recomposed from the final fitted page")
    fitter.actions.extend([*lexical_actions, f"editorial: theme {editorial.theme}; order {', '.join(editorial.order)}",
                           *(f"editorial: removed {item}" for item in editorial.removed),
                           *(f"editorial: compressed {item}" for item in editorial.compressed)])
    cv_text = document_to_text(document)
    summary_reasons = [f"Summary names a project no longer on the page: {name}" for name in
                       {p.name for p in projects} - {p.name for p in document.projects}
                       if document.summary and name in document.summary]

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

    matched_required, missing_required, partial_required = _match_required_skills(candidate, jd, lexical_view)
    # A USER_APPROVED_REFRESH skill on the page is tracked (coverage report + learning backlog), not a gap
    # that forces review on its own.
    refresh_names = {t.display.casefold() for t in refresh}
    missing_required = [name for name in missing_required if name.casefold() not in refresh_names]
    ats_coverage = jd_lexicon.coverage_report(jd_terms, lexical_view, approvals, document)
    backlog = jd_lexicon.learning_backlog([t for t in refresh if t.display in document.skills()], job, approvals)
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
        writing_review_reasons=writing_reasons + summary_reasons,
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
        render_side_margin_mm=render_result.side_margin_mm,
        render_body_pt=render_result.body_pt,
        page_utilization=render_result.page_utilization,
        document=document_to_dict(document),
        fit_actions=[title.action(), *fitter.actions],
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
        ats_coverage=ats_coverage,
        learning_backlog=backlog,
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
    if artifact.learning_backlog:  # a saved CV listing a refresh skill always has its learning backlog
        jd_lexicon.persist_backlog(store_root, artifact.learning_backlog)
    return artifact


# --- Bullet composition (JD-aware, evidence-grounded, extractive only) ---


def _collect_scored_quotes(match: ProjectMatch, candidate: CandidateProfile, jd: JDRequirements,
                           category_weights: dict | None = None) -> list[_ScoredQuote]:
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
            contribution = (priority * evidence.confidence + _jd_term_overlap(text, jd)
                            + (category_weights or {}).get(evidence.category or "", 0.0))
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
                                 impact: _ImpactContext | None = None, category_weights: dict | None = None) -> list[_BulletCandidate]:
    """One complete evidence quote per bullet -- never sliced, never stitched
    together with another quote. A quote that largely repeats a stronger one is
    down-weighted so it is the first to go (or never gets space). With an impact
    context every candidate then passes Impact & Ownership composition BEFORE the
    per-project ceiling, so the archetype's foregrounded value decides what gets space."""
    bullets: list[_BulletCandidate] = []
    for quote in _collect_scored_quotes(match, candidate, jd, category_weights):
        # Only like is compared with like here: a curated narrative is never discarded as a repeat of a
        # code-derived template statement (or vice versa) -- the editorial pass chooses between them.
        derived = any(":capmap:" in evidence_id for evidence_id in quote.evidence_ids)
        redundancy = max((_content_overlap(quote.text, kept.text) for kept in bullets
                          if any(":capmap:" in e for e in kept.evidence_ids) == derived), default=0.0)
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
    return bullets[:EDITORIAL_CANDIDATES]


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
    """Weighted bullet budget: the strongest project (JD relevance, then evidence strength) is the
    primary "focus" project, the next is the strong "secondary" one, the rest are "supporting".
    Applies each tier's bullet ceiling and orders the page primary first."""
    if not projects:
        return projects
    best = max(project.selection_score for project in projects)

    def order(project):
        near = best > 0 and project.selection_score >= best * NEAR_TIE_RATIO
        depth = min(len(project.bullets), MAX_FOCUS_BULLETS) if near else 0
        return (not near, -depth, *(-value for value in _project_strength(project)))

    ranked = sorted(projects, key=order)
    for rank, project in enumerate(ranked):
        project.emphasis = "focus" if rank == 0 else "secondary" if rank == 1 else "supporting"
        project.bullets = sorted(project.bullets, key=lambda bullet: -bullet.weight)[:TIER_CEILING[project.emphasis]]
    return ranked


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


def positioning_override(job_id: str, store_root) -> dict | None:
    """A reviewed, per-job editorial positioning rule from config/cv_positioning_overrides.json (repo config
    next to the local store), or None. Keyed by exact job id only; never inferred."""
    import json as _json
    try:
        path = Path(store_root).resolve().parents[1] / "config" / "cv_positioning_overrides.json"
        rules = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError):
        return None
    rule = rules.get(job_id) if isinstance(rules, dict) else None
    return rule if isinstance(rule, dict) else None


def apply_positioning_override(job, candidate, matches, explanations, store_root):
    """Narrow editorial correction (user-reviewed, per job): drop `avoid_projects`, bring in `prefer_projects`
    from the candidate's OWN projects at `prefer_position`, keeping the same number of projects. Bullets are
    still built only from each project's grounded evidence; nothing is invented."""
    rule = positioning_override(job.id, store_root)
    if not rule:
        return matches, explanations
    from dataclasses import replace as _replace
    avoid = set(rule.get("avoid_projects", []))
    kept = [m for m in matches if m.project.id not in avoid]
    by_id = {p.id: p for p in candidate.projects}
    position = int(rule.get("prefer_position", 1))
    reason = rule.get("reason", "reviewed editorial positioning")
    explanations = {k: v for k, v in explanations.items() if k not in avoid}
    for project_id in rule.get("prefer_projects", []):
        if project_id in by_id and all(m.project.id != project_id for m in kept):
            match = ProjectMatch(project=by_id[project_id], score=max((m.score for m in kept), default=1.0),
                                 matched_requirements=[], missing_requirements=[],
                                 explanation=f"Editorial positioning override (reviewed): {reason}",
                                 selection_basis="transferable")
            kept.insert(min(position, len(kept)), match)
            explanations[project_id] = match.explanation
            position += 1
    kept = kept[:max(len(matches), 1)]
    return kept, {k: v for k, v in explanations.items() if any(m.project.id == k for m in kept)}


def _match_required_skills(candidate: CandidateProfile, jd: JDRequirements,
                           lexical_view=None) -> tuple[list[str], list[str], list[str]]:
    """Review-gate view of the JD's required skills: individually mandatory ones count on their own; an
    alternatives group ("one or more of", "such as") counts once (analysis.jd_requirements). A skill implied
    by grounded evidence (jd_lexicon GROUNDED_SYNONYM: Git by the candidate's own repositories) is matched."""
    from jobintel.analysis.jd_requirements import resolve_required_skills

    def tier(name):
        capability = candidate.capabilities.get(name.casefold())
        if capability is not None and capability.evidence_tier != "missing":
            return capability.evidence_tier
        if lexical_view is not None and jd_lexicon.implied_grounding(name, lexical_view):
            return "implied"
        return None

    return resolve_required_skills(jd, tier)


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
    return [pattern for pattern, *_ in TRANSFERABLE_DIMENSIONS.values() if re.search(pattern, (jd_text or "").casefold())]


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


def _archetype_preference(text: str, archetype_signals: list[re.Pattern]) -> float:
    """Ordering bonus for the wording this role archetype prefers (e.g. collaboration /
    investigation for a forward-deployed role, SQL / validation for backend). It only reorders
    supported statements of real relevance: never makes an irrelevant one relevant and never
    moves the relevance cut-offs."""
    lowered = text.casefold()
    return ARCHETYPE_SIGNAL_WEIGHT * sum(1 for pattern in archetype_signals if pattern.search(lowered))


def _archetype_signals(impact: _ImpactContext | None) -> list[re.Pattern]:
    """The impact archetype's preferred internship wording (policy `experience_signals`)."""
    if impact is None:
        return []
    spec = impact.policy.spec["archetypes"].get(impact.archetype, {})
    return [re.compile(pattern) for pattern in spec.get("experience_signals", ())]


def _select_experience_statements(statements, jd: JDRequirements | None, jd_text: str,
                                  archetype_signals: list[re.Pattern] = ()) -> list:
    """Per JD: the role's evidence pool ranked by relevance (ties keep the
    authored order), skipping any variant that restates a master unit an
    already-chosen bullet covers, up to MAX_INTERNSHIP_DETAIL."""
    if jd is None:
        return list(statements)[:MAX_INTERNSHIP_DETAIL]
    dimensions, topics = _jd_dimensions(jd_text), _jd_topics(jd_text)
    scores = {statement.id: _experience_relevance(statement.text, jd, dimensions, topics) for statement in statements}
    order = {statement.id: scores[statement.id] + (_archetype_preference(statement.text, archetype_signals)
                                                   if scores[statement.id] > 0 else 0.0) for statement in statements}
    ranked = sorted(statements, key=lambda statement: -order[statement.id])
    best = max(scores.values(), default=0.0)
    chosen, covered = [], set()
    for statement in ranked:
        units = set(statement.grounded_in) or {statement.id}
        if units & covered:
            continue
        # The cut-off is relative to the best plain relevance, so a preference can lift a relevant
        # statement over it but never raises the bar for the others.
        if len(chosen) >= 2 and order[statement.id] < EXTRA_INTERNSHIP_MIN_SHARE * best:
            break
        chosen.append(statement)
        covered |= units
        if len(chosen) == MAX_INTERNSHIP_DETAIL:
            break
    return chosen


# JD wording that makes a theme-gated role (e.g. a current web / e-commerce internship) relevant even on a CV
# whose overall theme is systems, platform or ML.
_GATED_ROLE_WORK = re.compile(r"\b(?:web(?:site|sites)?|front[- ]?end|full[- ]?stack|e-?commerce|shopify|product engineer\w*)\b", re.I)


def _experience_entries(candidate: CandidateProfile, jd: JDRequirements | None = None, jd_text: str = "",
                        impact: _ImpactContext | None = None) -> list[DatedEntry]:
    entries = []
    theme = cv_editorial.theme_for(jd_text) if jd_text else None
    for item in candidate.experience:
        # A role with cv_themes (curated policy) is shown only for those CV themes or when the JD names its
        # kind of work; it is never forced onto an unrelated CV.
        if getattr(item, "cv_themes", ()) and theme is not None and theme not in item.cv_themes \
                and not _GATED_ROLE_WORK.search(jd_text):
            continue
        dates = ""
        if item.start_date:
            dates = f"{item.start_date:%b %Y} – {item.end_date:%b %Y}" if item.end_date else f"{item.start_date:%b %Y} – Present"
        if item.evidence:
            selected = _select_experience_statements(item.evidence, jd, jd_text, _archetype_signals(impact))
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


_LEXICAL_SKILL_CATEGORY = {
    "nosql": SkillCategory.DATABASE, "git": SkillCategory.DEVOPS, "object-oriented design": SkillCategory.LANGUAGE, "java": SkillCategory.LANGUAGE,
    "c++": SkillCategory.LANGUAGE, "c#": SkillCategory.LANGUAGE, "go": SkillCategory.LANGUAGE,
    "rust": SkillCategory.LANGUAGE, "kotlin": SkillCategory.LANGUAGE, "scala": SkillCategory.LANGUAGE,
    "typescript": SkillCategory.LANGUAGE, "kubernetes": SkillCategory.DEVOPS, "terraform": SkillCategory.DEVOPS,
    "grafana": SkillCategory.DEVOPS, "prometheus": SkillCategory.DEVOPS, "gcp": SkillCategory.CLOUD,
    "azure": SkillCategory.CLOUD,
}


def _align_project_bullets(projects: list[ProjectBlock], jd_keys: set[str], view) -> list[str]:
    actions = []
    for project in projects:
        source = view.by_entity.get(project.project_id, "")
        for bullet in project.bullets:
            text, applied = jd_lexicon.align_text(bullet.text, jd_keys, source)
            if applied:
                bullet.original_quote = bullet.original_quote or bullet.text
                if bullet.provenance:
                    bullet.provenance = {**bullet.provenance, "rewritten_text": text, "rewrite_rationale": [
                        *bullet.provenance.get("rewrite_rationale", []), *(f"lexical_alignment:{t}" for t in applied)]}
                bullet.text = text
                actions.append(f"lexical: aligned {project.project_id} bullet to JD term(s) {', '.join(applied)}")
    return actions


def _align_experience(document: CVDocument, jd_keys: set[str], view, impact: _ImpactContext | None) -> list[str]:
    """Internship lines in the JD's wording, only through jd_lexicon's rules grounded in that role's own
    evidence; the line keeps its source statement (audit trail) under the new text."""
    actions = []
    for entry in document.experience:
        source = view.by_entity.get(entry.heading, "")
        for index, detail in enumerate(entry.details):
            text, applied = jd_lexicon.align_text(detail, jd_keys, source)
            if not applied:
                continue
            entry.details[index] = text
            if impact is not None and detail in impact.experience:
                impact.experience[text] = impact.experience[detail]
            actions.append(f"lexical: aligned internship line ({entry.heading}) to JD term(s) {', '.join(applied)}")
    return actions


def _compose_final_summary(document: CVDocument, job: Job, impact: _ImpactContext, candidate: CandidateProfile,
                           theme: str, evidence_by_id: dict) -> str | None:
    from jobintel.matching.cv_summary import compose_summary
    summary = compose_summary(jd_text=job.description, job_title=job.title, projects=document.projects,
                              education=document.education, headline=document.headline, evidence_by_id=evidence_by_id,
                              theme=theme)
    if summary:
        summary, _, _ = impact_writing.narrow_summary(summary, _grounded_corpus(candidate), impact.policy)
    return summary


def _compress_education(education: list[DatedEntry], theme: str, jd_text: str) -> list[DatedEntry]:
    """Relevant Modules cut to the JD-relevant few (cv_editorial.select_modules); a line with no relevant
    module is omitted, the degree heading stays."""
    out = []
    for entry in education:
        details = [line for line in (cv_editorial.select_modules(d, theme, jd_text) for d in entry.details) if line]
        out.append(DatedEntry(heading=entry.heading, dates=entry.dates, details=details))
    return out


def _assemble_document(candidate: CandidateProfile, jd: JDRequirements, projects: list[ProjectBlock], skill_groups, jd_text: str = "",
                       impact: _ImpactContext | None = None, *, job_title: str = "", theme: str | None = None,
                       evidence_by_id: dict | None = None) -> tuple[CVDocument, CVTitle]:
    """Stable core sections come from structured Education/Experience records
    when present, otherwise verbatim from the candidate's own authored CV text.
    A section neither can supply is left empty (and reported), never invented.
    The heading is this job's own title, truthfully normalised (cv_title); the authored
    headline only when the job title cannot be presented (e.g. a management title)."""
    base = select_base_cv(candidate, _jd_terms(jd))
    authored = base[1] if base else None
    education = _education_entries(candidate) or (copy.deepcopy(authored.education) if authored else [])
    if theme is not None:
        education = _compress_education(education, theme, jd_text)
    experience = _experience_entries(candidate, jd, jd_text, impact) or (copy.deepcopy(authored.experience) if authored else [])
    summary = authored.summary if authored else None
    grounded = {name.casefold() for name, cap in candidate.capabilities.items() if cap.evidence_tier in {"strong", "partial"}}
    title = cv_title(job_title, authored.headline if authored else "", grounded=grounded)
    if impact is not None:
        # Target identity first, then the page's own evidence (cv_summary); the authored summary only when
        # nothing grounds a composed one.
        from jobintel.matching.cv_summary import compose_summary
        composed = compose_summary(jd_text=jd_text, job_title=job_title, projects=projects, education=education,
                                   headline=title.title, evidence_by_id=evidence_by_id, theme=theme)
        summary = composed or summary
    if impact is not None and summary:
        impact.summary_original = summary
        summary, impact.summary_removed, impact.summary_generic = impact_writing.narrow_summary(
            summary, _grounded_corpus(candidate), impact.policy)
    return CVDocument(
        name=candidate.name,
        headline=title.title,
        contact_line=_contact_line(candidate),
        summary=summary,
        education=education,
        skill_groups=skill_groups,
        projects=projects,
        experience=experience,
        base_cv_source_id=base[0].evidence_source.id if base else None,
    ), title


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
        archetype_signals: list[re.Pattern] = (),
        theme: str | None = None,
    ) -> None:
        self.theme = theme
        self.pdf_path = pdf_path
        self.archetype_signals = list(archetype_signals)
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
        # preset and at each denser professional preset. Choose by, in order:
        # least missing PROJECT evidence, then the larger body font, then least
        # other missing useful content (internship detail), then the less dense
        # preset -- so a smaller font is only used to keep project evidence,
        # never for internship bullets, modules or skills.
        reduced_doc, reduced_layout, reduced_actions = copy.deepcopy(self.doc), self.layout, list(self.actions)
        best = None
        for layout in range(reduced_layout, len(LAYOUT_PRESETS)):
            self.doc, self.layout, self.actions = copy.deepcopy(reduced_doc), layout, list(reduced_actions)
            # Each trial starts with internship detail at its floor, so project evidence reclaims
            # space first; the fill restores JD-relevant internship bullets only into what is left.
            self._floor_internships()
            if layout != reduced_layout:
                preset = LAYOUT_PRESETS[layout]
                self.actions.append(f"layout: preset {preset.name} ({preset.margin_mm:g}mm margins, {preset.sides_mm:g}mm sides, "
                                    f"{preset.body_pt:g}pt body) to keep useful content")
            self._fill()
            project_missing, other_missing = self._missing_useful_weight()
            key = (round(project_missing, 6), -LAYOUT_PRESETS[layout].body_pt, round(other_missing, 6))
            if best is None or key < best[0]:
                best = (key, layout, self.doc, self.actions)
            if project_missing <= 0 and other_missing <= 0:
                break
        _, self.layout, self.doc, self.actions = best
        return self.doc, self.render()

    def _reduce(self) -> RenderResult:
        result = self.render()
        # Space priority (editorial layer): weak modules, low-value skills, then repeated / inventory bullets,
        # then supporting-project detail -- the strongest JD evidence is trimmed last.
        reductions = (
            lambda: self._trim_module_list(CONCISE_MODULES),
            self._drop_low_value_skill,
            self._drop_repetitive_bullet,
            lambda: self._reduce_tier("supporting", 1),
            self._denser_layout,
            self._drop_extra_internship_bullet,
            lambda: self._reduce_tier("focus", FOCUS_BULLET_FLOOR),
            lambda: self._drop_weakest_project("supporting"),
            self._narrow_layout,
            lambda: self._reduce_tier("secondary", SECONDARY_BULLET_FLOOR),
            self._drop_internship_below_floor,
            lambda: self._drop_weakest_project("secondary"),
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

    def _floor_internships(self) -> None:
        for entry in self.doc.experience:
            if len(entry.details) > INTERNSHIP_DETAIL_FLOOR:
                keep = self._ranked_details(entry.details)[:INTERNSHIP_DETAIL_FLOOR]
                entry.details = [detail for detail in entry.details if detail in keep]

    def _missing_useful_weight(self) -> tuple[float, float]:
        """(project, other): weight of useful content not on the page -- focus-project evidence and
        non-repetitive other project evidence (including whole dropped projects); then JD-relevant
        internship detail."""
        present = {project.project_id: {_bullet_key(bullet) for bullet in project.bullets} for project in self.doc.projects}
        project_missing = 0.0
        for project in self.full.projects:
            for bullet in project.bullets:
                useful = project.emphasis == "focus" or bullet.redundancy < REDUNDANT_OVERLAP
                if useful and _bullet_key(bullet) not in present.get(project.project_id, set()):
                    project_missing += max(bullet.weight, 0.1)
        other_missing = 0.0
        for full_entry, entry in zip(self.full.experience, self.doc.experience):
            other_missing += sum(1.0 for detail in full_entry.details if _jd_term_overlap(detail, self.jd) > 0 and detail not in entry.details)
        return project_missing, other_missing

    # -- relevance: JD-named terms first, JD wording only as a tie-breaker --

    def _relevance(self, text: str) -> float:
        shared = self.jd_words & set(_CONTENT_WORD.findall(text.casefold()))
        return _jd_term_overlap(text, self.jd) + 0.01 * len(shared)

    def _experience_relevance(self, text: str) -> float:
        return _experience_relevance(text, self.jd, self.jd_dimensions, self.jd_topics)

    def _experience_order(self, text: str) -> float:
        """Relevance plus the archetype's ordering preference (cut-offs use relevance alone)."""
        relevance = self._experience_relevance(text)
        return relevance + (_archetype_preference(text, self.archetype_signals) if relevance > 0 else 0.0)

    def _ranked_details(self, details: list[str]) -> list[str]:
        # Stable: equal scores keep the authored order.
        return sorted(details, key=lambda detail: -self._experience_order(detail))

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
            (self._experience_order(detail), -index, entry, detail)
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

    def _step_layout(self, limit: int) -> str | None:
        if self.layout >= limit:
            return None
        self.layout += 1
        preset = LAYOUT_PRESETS[self.layout]
        return (f"overflow: layout preset {preset.name} ({preset.margin_mm:g}mm margins, "
                f"{preset.sides_mm:g}mm sides, {preset.body_pt:g}pt body)")

    def _denser_layout(self) -> str | None:
        """Tighter margins at the full body size (up to the last 10pt preset)."""
        full_size = max(index for index, preset in enumerate(LAYOUT_PRESETS) if preset.body_pt >= LAYOUT_PRESETS[0].body_pt)
        return self._step_layout(full_size)

    def _narrow_layout(self) -> str | None:
        return self._step_layout(len(LAYOUT_PRESETS) - 1)

    def _reduce_tier(self, tier: str, floor: int) -> str | None:
        """Drop the weakest bullet of a `tier` project that still has more than `floor` bullets."""
        candidates = [(project, bullet) for project in self.doc.projects
                      if project.emphasis == tier and len(project.bullets) > floor for bullet in project.bullets]
        if not candidates:
            return None
        project, bullet = min(candidates, key=lambda item: item[1].weight)
        project.bullets.remove(bullet)
        return f"overflow: dropped weakest {tier} bullet from {project.project_id}"

    def _drop_weakest_project(self, tier: str) -> str | None:
        projects = [project for project in self.doc.projects if project.emphasis == tier]
        if not projects or len(self.doc.projects) <= MIN_PROJECTS:
            return None
        project = min(projects, key=_project_strength)
        self.doc.projects.remove(project)
        return f"overflow: dropped weakest {tier} project {project.project_id}"

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
                    missing_bullets.append((TIER_RANK.get(project.emphasis, 2), -bullet.weight, bullet))
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
            (-self._experience_order(detail), entry_index, detail)
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
        rank = TIER_RANK.get(project.emphasis, 2)
        higher = [len(item.bullets) for item in doc.projects if TIER_RANK.get(item.emphasis, 2) < rank]
        adding = not any(_bullet_key(item) == _bullet_key(bullet) for item in project.bullets)
        # Page space follows priority: a lower-tier project never grows past the smallest
        # higher-tier project.
        if adding and higher and len(project.bullets) + 1 > min(higher):
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
        doc.skill_groups = (cv_editorial.group_skills(ordered, self.categories, self.theme) if self.theme
                            else group_skills(ordered, self.categories))
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
    forbidden: dict[str, set[str]] = {project.id: set(project.forbidden_extrapolations or ()) for project in candidate.projects}
    for capability in candidate.capabilities.values():
        for evidence in capability.evidence:
            if evidence.project_id:
                project_text[evidence.project_id] = project_text.get(evidence.project_id, "") + " " + evidence.quote
                allowed.setdefault(evidence.project_id, set()).add(capability.name.casefold())
                forbidden.setdefault(evidence.project_id, set()).update(evidence.forbidden_extrapolations or ())
    projects_by_id = {project.id: project for project in candidate.projects}
    for pid, text in project_text.items():
        allowed[pid] |= extract_tech_entities(text)
        if pid in projects_by_id:
            allowed[pid] |= extract_tech_entities(projects_by_id[pid].description or "")
    repo = {project.project_id: impact_writing.repo_readme_text(impact.store_root, getattr(projects_by_id.get(project.project_id), "repository_full_name", None))
            for project in document.projects}
    from jobintel.repo_capability_map import corroboration_text
    from jobintel.repo_code_evidence import code_evidence_text

    from jobintel.repo_capability_map import map_languages

    def _languages(repo):
        # The GitHub languages record can be older than the mapped commit: a language present in the project's own
        # source at that commit is never "contradicted" by a stale record.
        cached, mapped = impact_writing.repo_languages(impact.store_root, repo), map_languages(impact.store_root, repo)
        return None if cached is None and mapped is None else (cached or set()) | (mapped or set())

    def _code(repo):
        texts = [t for t in (code_evidence_text(impact.store_root, repo), corroboration_text(impact.store_root, repo)) if t]
        return "\n".join(texts) or None
    repo_code = {project.project_id: _code(getattr(projects_by_id.get(project.project_id), "repository_full_name", None))
                 for project in document.projects}
    bullets = [b for project in document.projects for b in project.bullets]
    rewrites = [_rewrite_from(b.provenance) for b in bullets if b.provenance]
    experience_lines = [detail for entry in document.experience for detail in entry.details]
    experience_rewrites = [impact.experience[line][1] for line in experience_lines if line in impact.experience]
    report = impact_writing.writing_quality_report(
        [(project.project_id, [(b.text, b.evidence_ids) for b in project.bullets]) for project in document.projects],
        experience_lines, document.summary, impact.policy, project_evidence_text=project_text, project_allowed_tech=allowed,
        repo_text=repo, repo_code=repo_code, rewrites=rewrites + experience_rewrites, project_forbidden=forbidden,
        repo_languages={project.project_id: _languages(getattr(projects_by_id.get(project.project_id), "repository_full_name", None))
            for project in document.projects})
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
