from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from jobintel.models.cv_artifact import GeneratedCVArtifact, ProjectBullet
from jobintel.models.taxonomy import ReviewGateStatus
from jobintel.storage.cv_artifact_store import CVArtifactStore


def _artifact(artifact_id: str, job_id: str, application_id: str | None = None) -> GeneratedCVArtifact:
    return GeneratedCVArtifact(
        id=artifact_id,
        job_id=job_id,
        candidate_id="candidate-1",
        generated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        jd_snapshot="Required: Python.",
        selected_project_ids=["p1"],
        evidence_ids_used=["p1:python"],
        bullets=[ProjectBullet(project_id="p1", capability_names=["Python"], evidence_ids=["p1:python"], text="Built a Python service.")],
        skills_included=["Python"],
        cv_text="Test Candidate\n\nSKILLS\nPython\n\nPROJECTS\nProject — tagline\n  - Built a Python service.",
        pdf_path="/tmp/cv-1.pdf",
        page_count=1,
        fits_one_page=True,
        render_margin_mm=18.0,
        render_body_pt=10.0,
        review_status=ReviewGateStatus.AUTO_PREPARE,
        review_reasons=[],
        application_id=application_id,
    )


class CVArtifactStoreTests(unittest.TestCase):
    def test_save_and_read_round_trips_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CVArtifactStore(tmp)
            artifact = _artifact("cv-1", "job-1")
            store.save(artifact)

            reloaded = CVArtifactStore(tmp).read_all()

        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].cv_text, artifact.cv_text)
        self.assertEqual(reloaded[0].bullets[0].evidence_ids, ["p1:python"])
        self.assertEqual(reloaded[0].bullets[0].capability_names, ["Python"])
        self.assertEqual(reloaded[0].review_status, ReviewGateStatus.AUTO_PREPARE)
        self.assertEqual(reloaded[0].page_count, 1)

    def test_multiple_generations_for_the_same_job_are_all_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CVArtifactStore(tmp)
            store.save(_artifact("cv-1", "job-1"))
            store.save(_artifact("cv-2", "job-1"))

            for_job = store.for_job("job-1")

        self.assertEqual({artifact.id for artifact in for_job}, {"cv-1", "cv-2"})

    def test_get_by_id_and_for_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CVArtifactStore(tmp)
            store.save(_artifact("cv-1", "job-1", application_id="app-1"))
            store.save(_artifact("cv-2", "job-2"))

            found = store.get("cv-1")
            for_app = store.for_application("app-1")

        self.assertIsNotNone(found)
        self.assertEqual(found.job_id, "job-1")
        self.assertEqual([artifact.id for artifact in for_app], ["cv-1"])

    def test_get_missing_id_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CVArtifactStore(tmp)
            self.assertIsNone(store.get("does-not-exist"))


if __name__ == "__main__":
    unittest.main()
