from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from jobintel.models.cv_artifact import GeneratedCVArtifact, ProjectBullet
from jobintel.models.taxonomy import ReviewGateStatus


class CVArtifactStore:
    """Append-only persistence for generated CVs.

    Every `generate_cv` call produces a new, immutable artifact -- never
    overwritten -- so this store can answer "what exact CV did I submit to this
    company?" even after the Evidence Bank or ranking config later changes.
    Mirrors the append-only convention already used by `HistoricalJobStore`
    (job_observations.jsonl) and `ApplicationStore` (status_history.jsonl).
    """

    def __init__(self, root: Path | str = "data/local") -> None:
        self.root = Path(root)
        self.path = self.root / "cv_artifacts" / "artifacts.jsonl"

    def save(self, artifact: GeneratedCVArtifact) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_dict(artifact), ensure_ascii=False, sort_keys=True) + "\n")

    def read_all(self) -> list[GeneratedCVArtifact]:
        if not self.path.exists():
            return []
        artifacts = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    artifacts.append(_from_dict(json.loads(line)))
        return artifacts

    def get(self, artifact_id: str) -> GeneratedCVArtifact | None:
        for artifact in self.read_all():
            if artifact.id == artifact_id:
                return artifact
        return None

    def for_job(self, job_id: str) -> list[GeneratedCVArtifact]:
        return [artifact for artifact in self.read_all() if artifact.job_id == job_id]

    def for_application(self, application_id: str) -> list[GeneratedCVArtifact]:
        return [artifact for artifact in self.read_all() if artifact.application_id == application_id]


def _to_dict(artifact: GeneratedCVArtifact) -> dict[str, Any]:
    payload = asdict(artifact)
    payload["generated_at"] = artifact.generated_at.isoformat()
    payload["review_status"] = artifact.review_status.value
    return payload


def _from_dict(payload: dict[str, Any]) -> GeneratedCVArtifact:
    return GeneratedCVArtifact(
        id=payload["id"],
        job_id=payload["job_id"],
        candidate_id=payload["candidate_id"],
        generated_at=datetime.fromisoformat(payload["generated_at"]),
        jd_snapshot=payload["jd_snapshot"],
        selected_project_ids=payload["selected_project_ids"],
        evidence_ids_used=payload["evidence_ids_used"],
        bullets=[ProjectBullet(**bullet) for bullet in payload.get("bullets", [])],
        skills_included=payload["skills_included"],
        cv_text=payload["cv_text"],
        pdf_path=payload.get("pdf_path"),
        page_count=payload["page_count"],
        fits_one_page=payload["fits_one_page"],
        render_margin_mm=payload["render_margin_mm"],
        render_body_pt=payload["render_body_pt"],
        review_status=ReviewGateStatus(payload["review_status"]),
        review_reasons=payload.get("review_reasons", []),
        application_id=payload.get("application_id"),
        pdf_sha256=payload.get("pdf_sha256"),
        generation_mode=payload.get("generation_mode", "deterministic"),
        project_selection_explanations=payload.get("project_selection_explanations", {}),
        rejected_bullet_suggestions=payload.get("rejected_bullet_suggestions", []),
        page_utilization=payload.get("page_utilization"),
        document=payload.get("document", {}),
        fit_actions=payload.get("fit_actions", []),
        # Records written before the field existed: a structured `document`
        # means the six-section generator (format 2), otherwise legacy (1).
        document_format=payload.get("document_format", 2 if payload.get("document") else 1),
        layout_preset=payload.get("layout_preset", ""),
        github_freshness_state=payload.get("github_freshness_state", "not_configured"),
    )
