from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobintel.connectors.base import RawJobPayload
from jobintel.dedup.v1 import deduplicate_jobs
from jobintel.models.job import Job, Location, SourceObservation
from jobintel.models.taxonomy import WorkflowStatus, WorkMode


class LocalJobStore:
    def __init__(self, root: Path | str = "data/local") -> None:
        self.root = Path(root)
        self.raw_dir = self.root / "raw_snapshots"
        self.processed_dir = self.root / "processed"
        self.workflow_path = self.processed_dir / "workflow_status.json"
        self.source_health_path = self.processed_dir / "source_health.json"
        self.refresh_summary_path = self.processed_dir / "refresh_summary.json"
        self.saved_searches_path = self.processed_dir / "saved_searches.json"

    def write_raw_payloads(self, payloads: list[RawJobPayload]) -> Path:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.raw_dir / f"raw_jobs_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for payload in payloads:
                handle.write(json.dumps(_jsonable(asdict(payload)), ensure_ascii=False, sort_keys=True) + "\n")
        return path

    def write_jobs(self, jobs: list[Job], mark_missing_inactive: bool = True, refreshed_sources: set[str] | None = None) -> Path:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        path = self.processed_dir / "canonical_jobs.json"
        jobs = self._merge_with_existing(jobs, mark_missing_inactive=mark_missing_inactive, refreshed_sources=refreshed_sources)
        with path.open("w", encoding="utf-8") as handle:
            json.dump([job_to_dict(job) for job in jobs], handle, ensure_ascii=False, indent=2, sort_keys=True)
        return path

    def read_jobs(self) -> list[Job]:
        path = self.processed_dir / "canonical_jobs.json"
        if not path.exists():
            return []
        statuses = self.read_workflow_statuses()
        jobs = [job_from_dict(item) for item in json.loads(path.read_text(encoding="utf-8"))]
        return [replace(job, workflow_status=WorkflowStatus(statuses.get(job.id, job.workflow_status.value))) for job in jobs]

    def jobs_first_seen_since(self, since: datetime) -> list[Job]:
        jobs = self.read_jobs()
        return [
            job
            for job in jobs
            if min((observation.first_seen_at for observation in job.source_observations), default=datetime.max) >= since
        ]

    def read_workflow_statuses(self) -> dict[str, str]:
        if not self.workflow_path.exists():
            return {}
        payload = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        valid = {item.value for item in WorkflowStatus}
        return {str(job_id): str(status) for job_id, status in payload.items() if str(status) in valid}

    def set_workflow_status(self, job_id: str, status: WorkflowStatus) -> None:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        statuses = self.read_workflow_statuses()
        statuses[job_id] = status.value
        self.workflow_path.write_text(json.dumps(statuses, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def read_source_health(self) -> dict[str, Any]:
        if not self.source_health_path.exists():
            return {}
        payload = json.loads(self.source_health_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    def write_source_health(self, source_name: str, payload: dict[str, Any]) -> Path:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        health = self.read_source_health()
        health[source_name] = _jsonable(payload)
        self.source_health_path.write_text(json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self.source_health_path

    def read_refresh_summary(self) -> dict[str, Any]:
        if not self.refresh_summary_path.exists():
            return {}
        payload = json.loads(self.refresh_summary_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    def write_refresh_summary(self, payload: dict[str, Any]) -> Path:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.refresh_summary_path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self.refresh_summary_path

    def read_saved_searches(self) -> list[dict[str, Any]]:
        if not self.saved_searches_path.exists():
            return []
        payload = json.loads(self.saved_searches_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []

    def write_saved_searches(self, payload: list[dict[str, Any]]) -> Path:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.saved_searches_path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self.saved_searches_path

    def _merge_with_existing(self, incoming: list[Job], mark_missing_inactive: bool = True, refreshed_sources: set[str] | None = None) -> list[Job]:
        existing = self.read_jobs()
        if not existing:
            return incoming
        if not mark_missing_inactive:
            return deduplicate_jobs(existing + incoming)
        incoming_observation_keys = {
            (observation.source_name, observation.source_job_id)
            for job in incoming
            for observation in job.source_observations
        }
        prior = [
            _mark_missing_observations_inactive(job, incoming_observation_keys, refreshed_sources)
            for job in existing
        ]
        return deduplicate_jobs(prior + incoming)


def job_to_dict(job: Job) -> dict[str, Any]:
    return _jsonable(asdict(job))


def job_from_dict(payload: dict[str, Any]) -> Job:
    locations = [
        Location(
            city=item.get("city"),
            country=item["country"],
            region=item.get("region"),
            work_mode=WorkMode(item.get("work_mode", WorkMode.UNKNOWN.value)),
            raw=item.get("raw"),
        )
        for item in payload["locations"]
    ]
    observations = [
        SourceObservation(
            source_name=item["source_name"],
            source_job_id=item["source_job_id"],
            original_url=item["original_url"],
            first_seen_at=_datetime(item["first_seen_at"]),
            last_seen_at=_datetime(item["last_seen_at"]),
            posted_at=_datetime(item["posted_at"]) if item.get("posted_at") else None,
            raw_description=item["raw_description"],
            canonical_application_url=item["canonical_application_url"],
            raw_payload=item.get("raw_payload", {}),
            active=item.get("active", True),
            latest_observed_state=item.get("latest_observed_state", "active"),
        )
        for item in payload["source_observations"]
    ]
    return Job(
        id=payload["id"],
        title=payload["title"],
        company=payload["company"],
        description=payload["description"],
        locations=locations,
        source_observations=observations,
        salary=payload.get("salary"),
        raw_location=payload.get("raw_location"),
        workflow_status=WorkflowStatus(payload.get("workflow_status", WorkflowStatus.NEW.value)),
    )


def _mark_inactive(job: Job) -> Job:
    return replace(
        job,
        source_observations=[
            replace(observation, active=False, latest_observed_state="DISAPPEARED")
            for observation in job.source_observations
        ],
    )


def _mark_missing_observations_inactive(job: Job, incoming_keys: set[tuple[str, str]], refreshed_sources: set[str] | None) -> Job:
    if not refreshed_sources:
        return _mark_inactive(job)
    observations = []
    for observation in job.source_observations:
        key = (observation.source_name, observation.source_job_id)
        if observation.source_name in refreshed_sources and key not in incoming_keys:
            observations.append(replace(observation, active=False, latest_observed_state="DISAPPEARED"))
        else:
            observations.append(observation)
    return replace(job, source_observations=observations)


def _job_key(job: Job) -> str:
    return job.canonical_application_url.casefold().rstrip("/") if job.source_observations else job.id


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
