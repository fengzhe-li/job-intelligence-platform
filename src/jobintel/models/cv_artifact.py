from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from jobintel.models.taxonomy import ReviewGateStatus


@dataclass(frozen=True)
class ProjectBullet:
    """One rendered bullet. `text` is composed only from verbatim evidence quote
    fragments (reordered/combined/compressed, never freely generated); every
    fragment's source is retained in `evidence_ids` and `capability_names` so the
    bullet stays traceable, even when it combines more than one piece of evidence
    from the SAME project.
    """

    project_id: str
    capability_names: list[str]
    evidence_ids: list[str]
    text: str
    status: str = "extractive"  # "extractive" or "gemini_rewritten"
    rejection_reasons: list[str] = field(default_factory=list)
    original_quote: str | None = None


@dataclass(frozen=True)
class GeneratedCVArtifact:
    """One immutable, persisted record of a job-specific generated CV.

    Every generation produces a new artifact -- never overwritten -- so
    `storage.cv_artifact_store.CVArtifactStore` can answer "what exact CV did I
    submit to this company?" even after the Evidence Bank or ranking config
    changes later. `application_id` links it to `models.application.Application`
    (via `Application.cv_version_id`) once attached to a specific application.

    `fits_one_page` / `page_count` reflect the REAL rendered PDF page count from
    `matching.cv_render` (reportlab text flow), not a line-count heuristic.
    `page_utilization` is the measured share of the page's usable height that
    content occupies, and `document` records the structured sections actually
    rendered (see `matching.cv_document.document_to_dict`). Both default to
    empty for artifacts persisted before they existed.
    """

    id: str
    job_id: str
    candidate_id: str
    generated_at: datetime
    jd_snapshot: str
    selected_project_ids: list[str]
    evidence_ids_used: list[str]
    bullets: list[ProjectBullet]
    skills_included: list[str]
    cv_text: str
    pdf_path: str | None
    page_count: int
    fits_one_page: bool
    render_margin_mm: float
    render_body_pt: float
    review_status: ReviewGateStatus
    review_reasons: list[str] = field(default_factory=list)
    application_id: str | None = None
    # SHA-256 of the rendered PDF bytes at generation time. The dashboard's
    # PDF route refuses to serve a file that no longer matches, so the PDF
    # shown for a (submitted) version is provably the one generated.
    pdf_sha256: str | None = None
    generation_mode: str = "deterministic"
    project_selection_explanations: dict[str, str] = field(default_factory=dict)
    rejected_bullet_suggestions: list[dict] = field(default_factory=list)
    page_utilization: float | None = None
    document: dict = field(default_factory=dict)
    fit_actions: list[str] = field(default_factory=list)
    # matching.cv_document.CV_DOCUMENT_FORMAT of the generator that produced
    # this version; 1 for legacy artifacts recorded before the field existed.
    document_format: int = 1
    # matching.cv_render.LayoutPreset name used for the PDF ("" for artifacts
    # recorded before presets existed; their margin/font fields still apply).
    layout_preset: str = ""
    # Truthful freshness state of GitHub repository evidence at generation time
    # ('current', 'stale', 'not_configured', 'sync_failed', 'unverified')
    github_freshness_state: str = "not_configured"
