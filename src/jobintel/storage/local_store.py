from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
        self.daily_report_path = self.processed_dir / "daily_refresh_latest.json"
        self.daily_report_history_path = self.processed_dir / "daily_refresh_history.jsonl"

    def write_raw_payloads(self, payloads: list[RawJobPayload]) -> Path:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.raw_dir / f"raw_jobs_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for payload in payloads:
                handle.write(json.dumps(_jsonable(asdict(payload)), ensure_ascii=False, sort_keys=True) + "\n")
        return path

    def write_jobs(self, jobs: list[Job]) -> Path:
        """Merge `jobs` into the store. NEVER infers closure: a job missing
        from `jobs` is left exactly as it was. Closure is only ever applied by
        `write_snapshot`, with explicit completeness evidence."""
        existing = self.read_jobs()
        # (Unchanged pre-Phase-3 behaviour: a first write into an empty store
        # persists `jobs` as given; later writes dedup against the store.)
        return self._persist(deduplicate_jobs(existing + jobs) if existing else jobs)

    def write_snapshot(
        self,
        jobs: list[Job],
        source_name: str,
        complete_scopes: frozenset[str] | set[str],
        scope_of: Callable[[dict[str, Any]], str | None],
    ) -> list[tuple[str, str]]:
        """Merge one source's refresh and close ONLY what it proves closed.

        An existing, still-active observation from `source_name` is marked
        DISAPPEARED only if it is absent from `jobs` AND `scope_of(its
        raw_payload)` is one of `complete_scopes` -- i.e. this refresh fetched
        that scope's entire current listing. Observations of any other source,
        with an unknown scope, or in a scope not proven complete this cycle are
        untouched. Returns the (source_name, source_job_id) keys that
        transitioned active -> DISAPPEARED in THIS write (the per-refresh
        delta, not a cumulative count)."""
        incoming_keys = {
            (observation.source_name, observation.source_job_id)
            for job in jobs
            for observation in job.source_observations
        }
        closed: list[tuple[str, str]] = []
        existing = self.read_jobs()
        if not existing:
            self._persist(jobs)
            return []
        prior: list[Job] = []
        for job in existing:
            observations = []
            for observation in job.source_observations:
                key = (observation.source_name, observation.source_job_id)
                if (
                    complete_scopes
                    and observation.active
                    and observation.source_name == source_name
                    and key not in incoming_keys
                    and scope_of(observation.raw_payload) in complete_scopes
                ):
                    observation = replace(observation, active=False, latest_observed_state="DISAPPEARED")
                    closed.append(key)
                observations.append(observation)
            prior.append(replace(job, source_observations=observations))
        self._persist(deduplicate_jobs(prior + jobs))
        return closed

    def replace_jobs(self, jobs: list[Job]) -> Path:
        """Persist EXACTLY `jobs` -- no merge, no dedup, no closure. Only for
        audited, backed-up data migrations (dedup.repair)."""
        return self._persist(jobs)

    def _persist(self, jobs: list[Job]) -> Path:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        path = self.processed_dir / "canonical_jobs.json"
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump([job_to_dict(job) for job in jobs], handle, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(path)
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

    def write_daily_report(self, payload: dict[str, Any]) -> Path:
        """Latest daily-refresh report (overwritten) + append-only history,
        so what each refresh actually did stays auditable."""
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        data = _jsonable(payload)
        tmp = self.daily_report_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.daily_report_path)
        with self.daily_report_history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
        return self.daily_report_path

    def read_daily_report(self) -> dict[str, Any] | None:
        if not self.daily_report_path.exists():
            return None
        payload = json.loads(self.daily_report_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None

    def read_daily_report_history(self, limit: int = 30) -> list[dict[str, Any]]:
        if not self.daily_report_history_path.exists():
            return []
        lines = [line for line in self.daily_report_history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [json.loads(line) for line in lines[-limit:]]

    def read_saved_searches(self) -> list[dict[str, Any]]:
        if not self.saved_searches_path.exists():
            return []
        payload = json.loads(self.saved_searches_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []

    def write_saved_searches(self, payload: list[dict[str, Any]]) -> Path:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.saved_searches_path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self.saved_searches_path


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
            deadline=_datetime(item["deadline"]) if item.get("deadline") else None,
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
