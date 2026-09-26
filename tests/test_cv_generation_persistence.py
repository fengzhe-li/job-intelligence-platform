from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jobintel.fixtures.sample_data import sample_candidate
from jobintel.matching.cv_generation import generate_and_save_cv
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.storage.cv_artifact_store import CVArtifactStore

# Regression test for a real bug caught by live end-to-end dashboard testing:
# `generate_cv()` only builds and returns the artifact (and renders the PDF to
# disk) -- it never calls `CVArtifactStore.save()` itself. The dashboard's
# `/cv/generate` route originally called `generate_cv()` directly and never
# persisted the result: a real PDF was written to disk, but the artifact
# record was invisible everywhere else (CV workbench, application detail,
# "what CV did I submit"). Fixed by introducing `generate_and_save_cv`, which
# both the CLI and the dashboard now use so this can't be missed again.


def _job() -> Job:
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return Job(
        id="job-1",
        title="Graduate Software Engineer",
        company="Example Co",
        description="Build backend services using Python, AWS, and Docker.",
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name="company",
                source_job_id="job-1",
                original_url="https://example.com/jobs/1",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description="Build backend services using Python, AWS, and Docker.",
                canonical_application_url="https://example.com/jobs/1/apply",
            )
        ],
    )


class GenerateAndSaveCvPersistsTheArtifactTests(unittest.TestCase):
    def test_the_generated_artifact_is_immediately_retrievable_from_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cv_store = CVArtifactStore(tmp)
            artifact = generate_and_save_cv(sample_candidate(), _job(), cv_store, store_root=tmp)

            retrieved = cv_store.get(artifact.id)

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, artifact.id)
        self.assertEqual(retrieved.job_id, "job-1")

    def test_for_job_finds_it_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cv_store = CVArtifactStore(tmp)
            generate_and_save_cv(sample_candidate(), _job(), cv_store, store_root=tmp)

            for_job = cv_store.for_job("job-1")

        self.assertEqual(len(for_job), 1)

    def test_a_real_pdf_file_actually_exists_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cv_store = CVArtifactStore(tmp)
            artifact = generate_and_save_cv(sample_candidate(), _job(), cv_store, store_root=tmp)

            self.assertIsNotNone(artifact.pdf_path)
            self.assertTrue(Path(artifact.pdf_path).exists())

    def test_two_generations_for_the_same_job_both_remain_retrievable_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cv_store = CVArtifactStore(tmp)
            first = generate_and_save_cv(sample_candidate(), _job(), cv_store, store_root=tmp)
            second = generate_and_save_cv(sample_candidate(), _job(), cv_store, store_root=tmp)

            for_job = cv_store.for_job("job-1")

            self.assertEqual({item.id for item in for_job}, {first.id, second.id})
            self.assertIsNotNone(cv_store.get(first.id))
            self.assertIsNotNone(cv_store.get(second.id))


if __name__ == "__main__":
    unittest.main()
