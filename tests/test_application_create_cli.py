"""CLI `application-create` goes through the canonical application lifecycle.

It must load the persisted Job and call `application_lifecycle.ensure_application`
-- the same path the dashboard uses -- so the application URL, JD snapshot,
source provenance and deadline come from the canonical Job, never from CLI
strings. An unknown job id fails closed.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from jobintel.application_lifecycle import application_id_for, ensure_application
from jobintel.cli.main import main
from jobintel.models.candidate import CandidateProfile
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import ApplicationStatus
from jobintel.profile_ingestion import _write_profile
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.submission.service import SubmissionService
from test_cv_artifact_store import _artifact
from test_submission_service import write_synthetic_answer_bank

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
ASHBY_URL = "https://jobs.ashbyhq.com/exampleco/0000-aaaa/application"
TRACKR_URL = "https://app.the-trackr.com/programme/exampleco-junior-swe"


def _observation(source: str, source_job_id: str, apply_url: str, deadline: datetime | None = None) -> SourceObservation:
    return SourceObservation(
        source_name=source,
        source_job_id=source_job_id,
        original_url=apply_url.removesuffix("/application"),
        first_seen_at=NOW,
        last_seen_at=NOW,
        posted_at=NOW,
        raw_description="Build low-latency backend services.",
        canonical_application_url=apply_url,
        deadline=deadline,
    )


def _job(job_id: str = "ashby:0000-aaaa", observations: list[SourceObservation] | None = None) -> Job:
    return Job(
        id=job_id,
        title="Junior Software Engineer",
        company="ExampleCo",
        description="Junior engineer. Build low-latency backend services in Java and Python.",
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=observations if observations is not None else [_observation("ashby", "0000-aaaa", ASHBY_URL)],
    )


def _create(root: Path, job_id: str, capsys) -> str:
    with patch("sys.argv", ["jobintel", "application-create", "--job-id", job_id, "--store", str(root)]):
        main()
    return capsys.readouterr().out


def test_cli_create_loads_canonical_job_and_copies_its_url(tmp_path, capsys):
    job = _job()
    LocalJobStore(tmp_path).write_jobs([job])

    out = _create(tmp_path, job.id, capsys)

    [application] = ApplicationStore(tmp_path).read_applications()
    assert application.id == application_id_for(job) == "app:ashby:0000-aaaa"
    assert (application.job_id, application.company, application.role_title) == (job.id, job.company, job.title)
    assert application.status == ApplicationStatus.DISCOVERED
    assert application.application_url == job.canonical_application_url == ASHBY_URL
    assert ASHBY_URL in out


def test_direct_ats_url_wins_over_aggregator_observation(tmp_path, capsys):
    # Aggregator observation listed FIRST: the direct ATS apply URL must still win.
    job = _job(observations=[_observation("trackr", "exampleco-junior-swe", TRACKR_URL),
                             _observation("ashby", "0000-aaaa", ASHBY_URL)])
    LocalJobStore(tmp_path).write_jobs([job])

    _create(tmp_path, job.id, capsys)

    application = ApplicationStore(tmp_path).for_job(job.id)
    assert application.application_url == ASHBY_URL == job.canonical_application_url
    assert application.application_url != TRACKR_URL


def test_jd_snapshot_sources_and_deadline_are_carried_through(tmp_path, capsys):
    early, late = NOW + timedelta(days=3), NOW + timedelta(days=10)
    job = _job(observations=[_observation("ashby", "0000-aaaa", ASHBY_URL, deadline=late),
                             _observation("trackr", "exampleco-junior-swe", TRACKR_URL, deadline=early)])
    LocalJobStore(tmp_path).write_jobs([job])

    _create(tmp_path, job.id, capsys)

    application = ApplicationStore(tmp_path).for_job(job.id)
    assert application.jd_snapshot == job.description
    assert application.sources == ["ashby", "trackr"]
    assert application.deadline == job.earliest_deadline == early


def test_cli_and_lifecycle_created_applications_converge(tmp_path, capsys):
    job = _job()
    cli_root, lifecycle_root = tmp_path / "cli", tmp_path / "lifecycle"
    LocalJobStore(cli_root).write_jobs([job])

    _create(cli_root, job.id, capsys)
    cli_app = ApplicationStore(cli_root).for_job(job.id)
    lifecycle_app = ensure_application(job, ApplicationStore(lifecycle_root))

    volatile = {"discovered_at", "updated_at"}
    assert {k: v for k, v in asdict(cli_app).items() if k not in volatile} == \
           {k: v for k, v in asdict(lifecycle_app).items() if k not in volatile}
    # And the dashboard/lifecycle path on the SAME store returns the CLI-created record, not a second one.
    assert ensure_application(job, ApplicationStore(cli_root)) == cli_app
    assert len(ApplicationStore(cli_root).read_applications()) == 1


def test_unknown_job_id_fails_closed_without_creating_an_application(tmp_path, capsys):
    LocalJobStore(tmp_path).write_jobs([_job()])

    with pytest.raises(SystemExit) as exc:
        _create(tmp_path, "ashby:does-not-exist", capsys)

    assert "No job with id ashby:does-not-exist" in str(exc.value)
    assert ApplicationStore(tmp_path).read_applications() == []
    assert not (tmp_path / "applications" / "status_history.jsonl").exists()


def test_cli_no_longer_accepts_free_text_company_or_role(tmp_path, capsys):
    LocalJobStore(tmp_path).write_jobs([_job()])
    argv = ["jobintel", "application-create", "--job-id", "ashby:0000-aaaa", "--company", "Spoofed", "--role-title", "Spoofed", "--store", str(tmp_path)]
    with patch("sys.argv", argv), pytest.raises(SystemExit):
        main()
    assert ApplicationStore(tmp_path).read_applications() == []


def test_rerunning_create_is_idempotent(tmp_path, capsys):
    job = _job()
    LocalJobStore(tmp_path).write_jobs([job])

    _create(tmp_path, job.id, capsys)
    first = ApplicationStore(tmp_path).for_job(job.id)
    out = _create(tmp_path, job.id, capsys)

    store = ApplicationStore(tmp_path)
    assert store.read_applications() == [first]
    assert len(store.status_history_for(first.id)) == 1
    assert "already has application app:ashby:0000-aaaa" in out and "nothing created" in out


def test_cli_created_application_url_reaches_the_submission_package(tmp_path, capsys, synthetic_answer_bank):
    candidate = CandidateProfile("candidate-1", "Alex Example", 2026, email="alex@example.com", phone="07000000000")
    _write_profile(tmp_path, candidate)
    write_synthetic_answer_bank(tmp_path)
    # A JD with no must-have skills, so the package is not review-blocked and
    # the test isolates the URL wiring.
    job = replace(_job(), description="Build backend services in a small team.")
    LocalJobStore(tmp_path).write_jobs([job])
    pdf = tmp_path / "cv_artifacts/pdfs/cv-0123456789ab.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 application-create fixture")
    artifact = replace(_artifact("cv-0123456789ab", job.id), jd_snapshot=job.description,
                       pdf_path=str(pdf), pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest())
    CVArtifactStore(tmp_path).save(artifact)

    _create(tmp_path, job.id, capsys)
    apps = ApplicationStore(tmp_path)
    application = apps.for_job(job.id)
    apps.attach_cv(application.id, artifact.id, artifact.selected_project_ids, artifact.evidence_ids_used)

    package = SubmissionService(tmp_path, clock=lambda: NOW).preview(application.id)
    assert package.application_url == ASHBY_URL == job.canonical_application_url
    assert package.job_id == job.id
