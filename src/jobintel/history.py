from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.analysis.job_quality import extract_seniority
from jobintel.models.job import Job, SourceObservation
from jobintel.models.taxonomy import WorkflowStatus
from jobintel.storage.local_store import LocalJobStore, job_to_dict


OBSERVATION_SCHEMA = {
    "canonical_job_id": "Stable canonical job id in the local store.",
    "source": "Source connector family.",
    "source_job_id": "Stable source job identifier.",
    "company": "Normalized company name.",
    "title": "Normalized job title.",
    "normalized_location": "Semicolon-delimited normalized location string.",
    "raw_location": "Raw location text retained from the source.",
    "work_mode": "Comma-delimited work mode values.",
    "posted_at": "Source posted timestamp when available.",
    "first_seen_at": "First time this canonical/source observation was seen.",
    "last_seen_at": "Last time this canonical/source observation was seen.",
    "observation_timestamp": "Timestamp of this historical snapshot observation.",
    "ingestion_run_id": "Idempotency key for the snapshot/ingestion run.",
    "source_url": "Source job URL.",
    "direct_apply_url": "Canonical direct application URL.",
    "active_state": "active or inactive.",
    "change_status": "NEW, CHANGED, UNCHANGED, DISAPPEARED, or REAPPEARED.",
    "sponsorship_classification": "Extracted sponsorship state.",
    "graduation_year_classification": "Extracted graduation-year state.",
    "seniority": "Extracted seniority label.",
    "role_tracks": "Comma-delimited primary role-track labels.",
    "field_hash": "Hash of meaningful normalized fields for change detection.",
}


@dataclass(frozen=True)
class SnapshotResult:
    run_id: str
    observations_written: int
    analytics_written: list[str]
    counts: dict[str, int]
    data_quality: dict[str, Any]
    idempotent: bool = False


class HistoricalJobStore:
    def __init__(self, root: Path | str = "data/local") -> None:
        self.root = Path(root)
        self.snapshot_dir = self.root / "snapshots"
        self.analytics_dir = self.root / "analytics"
        self.observations_path = self.snapshot_dir / "job_observations.jsonl"
        self.runs_path = self.snapshot_dir / "ingestion_runs.jsonl"

    def create_snapshot(
        self,
        jobs: list[Job],
        run_id: str | None = None,
        observed_at: datetime | None = None,
        run_source: str = "snapshot",
        failures: list[dict[str, Any]] | None = None,
        graduation_year: int = 2026,
    ) -> SnapshotResult:
        observed_at = observed_at or datetime.now(timezone.utc)
        run_id = run_id or f"{run_source}_{observed_at.strftime('%Y%m%dT%H%M%S%fZ')}"
        if self._run_exists(run_id):
            observations = self.read_observations()
            return SnapshotResult(
                run_id=run_id,
                observations_written=0,
                analytics_written=[],
                counts=_count_statuses([row for row in observations if row.get("ingestion_run_id") == run_id]),
                data_quality=self.data_quality_summary(),
                idempotent=True,
            )

        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        previous = self.read_observations()
        previous_latest = _latest_by_source_key(previous)
        rows = _current_observation_rows(jobs, observed_at, run_id, previous_latest, graduation_year)
        rows.extend(_disappeared_rows(previous_latest, rows, observed_at, run_id))

        if rows:
            with self.observations_path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

        counts = _count_statuses(rows)
        run_row = {
            "run_id": run_id,
            "started_at": observed_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "source": run_source,
            "company": "multiple",
            "jobs_seen": len([row for row in rows if row["active_state"] == "active"]),
            "jobs_new": counts.get("NEW", 0),
            "jobs_changed": counts.get("CHANGED", 0),
            "jobs_unchanged": counts.get("UNCHANGED", 0),
            "jobs_disappeared": counts.get("DISAPPEARED", 0),
            "jobs_reappeared": counts.get("REAPPEARED", 0),
            "failures": len(failures or []),
            "error_information": failures or [],
        }
        with self.runs_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(run_row, ensure_ascii=False, sort_keys=True) + "\n")

        analytics = self.write_analytics(jobs, graduation_year)
        quality = self.data_quality_summary()
        return SnapshotResult(run_id, len(rows), analytics, counts, quality)

    def read_observations(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.observations_path)

    def read_runs(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.runs_path)

    def write_analytics(self, jobs: list[Job], graduation_year: int = 2026) -> list[str]:
        self.analytics_dir.mkdir(parents=True, exist_ok=True)
        observations = self.read_observations()
        runs = self.read_runs()
        lifecycle = lifecycle_rows(observations)
        source_observations = _latest_source_rows(observations)
        canonical_jobs = [_canonical_job_row(enrich_job(job, graduation_year)) for job in jobs]
        datasets = {
            "job_observations": observations,
            "job_lifecycle": lifecycle,
            "ingestion_runs": runs,
            "source_observations": source_observations,
            "canonical_jobs": canonical_jobs,
        }
        written: list[str] = []
        parquet_written: list[str] = []
        for name, rows in datasets.items():
            jsonl_path = self.analytics_dir / f"{name}.jsonl"
            csv_path = self.analytics_dir / f"{name}.csv"
            _write_jsonl(jsonl_path, rows)
            _write_csv(csv_path, rows)
            written.extend([str(jsonl_path), str(csv_path)])
            parquet_path = _write_parquet_if_available(self.analytics_dir / f"{name}.parquet", rows)
            if parquet_path:
                parquet_written.append(str(parquet_path))
                written.append(str(parquet_path))
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "datasets": {name: len(rows) for name, rows in datasets.items()},
            "parquet_available": bool(parquet_written),
            "parquet_files": parquet_written,
            "schemas": {
                "job_observations": OBSERVATION_SCHEMA,
                "job_lifecycle": {
                    "canonical_job_id": "Canonical job id.",
                    "sources": "Comma-delimited sources that observed the vacancy.",
                    "first_seen_at": "Earliest observation first_seen_at.",
                    "last_seen_at": "Latest observation last_seen_at.",
                    "active_days": "Days between first and last seen.",
                    "currently_active": "Whether latest observation is active.",
                    "ever_disappeared": "Whether the job disappeared in any snapshot.",
                    "reappeared_count": "Number of REAPPEARED observations.",
                    "changed_count": "Number of CHANGED observations.",
                },
            },
        }
        manifest_path = self.analytics_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(str(manifest_path))
        return written

    def data_quality_summary(self) -> dict[str, Any]:
        observations = self.read_observations()
        seen: set[tuple[str, str, str]] = set()
        duplicate_observations = 0
        missing_source_ids = 0
        invalid_timestamps = 0
        broken_source_urls = 0
        missing_required_normalized_fields = 0
        impossible_lifecycle_ordering = 0
        source_key_to_canonical: dict[tuple[str, str], str] = {}
        canonical_collisions = 0
        for row in observations:
            observation_key = (row.get("ingestion_run_id", ""), row.get("source", ""), row.get("source_job_id", ""))
            if observation_key in seen:
                duplicate_observations += 1
            seen.add(observation_key)
            if not row.get("source_job_id"):
                missing_source_ids += 1
            for key in ("posted_at", "first_seen_at", "last_seen_at", "observation_timestamp"):
                if row.get(key) and not _valid_datetime(row[key]):
                    invalid_timestamps += 1
            if row.get("source_url") and not str(row["source_url"]).startswith(("http://", "https://")):
                broken_source_urls += 1
            if not row.get("canonical_job_id") or not row.get("title") or not row.get("company") or not row.get("direct_apply_url"):
                missing_required_normalized_fields += 1
            first_seen = _parse_datetime(row.get("first_seen_at"))
            last_seen = _parse_datetime(row.get("last_seen_at"))
            if first_seen and last_seen and first_seen > last_seen:
                impossible_lifecycle_ordering += 1
            source_key = (row.get("source", ""), row.get("source_job_id", ""))
            existing = source_key_to_canonical.get(source_key)
            if existing and existing != row.get("canonical_job_id"):
                canonical_collisions += 1
            elif row.get("source") and row.get("source_job_id"):
                source_key_to_canonical[source_key] = row.get("canonical_job_id", "")
        return {
            "observations_checked": len(observations),
            "duplicate_observations": duplicate_observations,
            "missing_source_ids": missing_source_ids,
            "invalid_timestamps": invalid_timestamps,
            "canonical_id_collisions": canonical_collisions,
            "broken_source_urls": broken_source_urls,
            "impossible_lifecycle_ordering": impossible_lifecycle_ordering,
            "missing_required_normalized_fields": missing_required_normalized_fields,
            "passed": all(
                value == 0
                for key, value in {
                    "duplicate_observations": duplicate_observations,
                    "missing_source_ids": missing_source_ids,
                    "invalid_timestamps": invalid_timestamps,
                    "canonical_id_collisions": canonical_collisions,
                    "broken_source_urls": broken_source_urls,
                    "impossible_lifecycle_ordering": impossible_lifecycle_ordering,
                    "missing_required_normalized_fields": missing_required_normalized_fields,
                }.items()
            ),
        }

    def _run_exists(self, run_id: str) -> bool:
        return any(row.get("run_id") == run_id for row in self.read_runs())


def snapshot_current_store(store_root: str = "data/local", run_id: str | None = None, graduation_year: int = 2026) -> SnapshotResult:
    store = LocalJobStore(store_root)
    history = HistoricalJobStore(store_root)
    return history.create_snapshot(store.read_jobs(), run_id=run_id, graduation_year=graduation_year)


def lifecycle_rows(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in observations:
        grouped.setdefault(row["canonical_job_id"], []).append(row)
    rows = []
    for canonical_id, group in sorted(grouped.items()):
        first_seen_values = [_parse_datetime(row.get("first_seen_at")) for row in group]
        last_seen_values = [_parse_datetime(row.get("last_seen_at")) for row in group]
        first_seen = min(item for item in first_seen_values if item is not None)
        last_seen = max(item for item in last_seen_values if item is not None)
        latest = max(group, key=lambda row: row.get("observation_timestamp", ""))
        rows.append(
            {
                "canonical_job_id": canonical_id,
                "company": latest.get("company", ""),
                "title": latest.get("title", ""),
                "sources": ", ".join(sorted({row.get("source", "") for row in group if row.get("source")})),
                "source_observation_count": len({(row.get("source"), row.get("source_job_id")) for row in group}),
                "first_seen_at": first_seen.isoformat(),
                "last_seen_at": last_seen.isoformat(),
                "active_days": round((last_seen - first_seen).total_seconds() / 86400, 3),
                "currently_active": latest.get("active_state") == "active",
                "ever_disappeared": any(row.get("change_status") == "DISAPPEARED" for row in group),
                "reappeared_count": len([row for row in group if row.get("change_status") == "REAPPEARED"]),
                "changed_count": len([row for row in group if row.get("change_status") == "CHANGED"]),
            }
        )
    return rows


def _current_observation_rows(
    jobs: list[Job],
    observed_at: datetime,
    run_id: str,
    previous_latest: dict[tuple[str, str], dict[str, Any]],
    graduation_year: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job in jobs:
        enriched = enrich_job(job, graduation_year)
        seniority = extract_seniority(enriched.title, enriched.description)
        role_tracks = ", ".join(score.track.value for score in enriched.role_track_profile.scores)
        sponsorship = enriched.sponsorship.state.value if enriched.sponsorship else "unknown"
        graduation = enriched.graduation_year.state.value if enriched.graduation_year else "unknown"
        normalized_location = "; ".join(location.raw or f"{location.city or ''}, {location.country}".strip(", ") for location in enriched.locations)
        work_mode = ", ".join(sorted({location.work_mode.value for location in enriched.locations}))
        for observation in enriched.source_observations:
            row = {
                "canonical_job_id": enriched.id,
                "source": observation.source_name,
                "source_job_id": observation.source_job_id,
                "company": enriched.company,
                "title": enriched.title,
                "normalized_location": normalized_location,
                "raw_location": enriched.raw_location or "",
                "work_mode": work_mode,
                "posted_at": _dt(observation.posted_at),
                "first_seen_at": _dt(observation.first_seen_at),
                "last_seen_at": _dt(observation.last_seen_at),
                "observation_timestamp": _dt(observed_at),
                "ingestion_run_id": run_id,
                "source_url": observation.original_url,
                "direct_apply_url": observation.canonical_application_url,
                "active_state": "active" if observation.active else "inactive",
                "sponsorship_classification": sponsorship,
                "graduation_year_classification": graduation,
                "seniority": seniority.level,
                "role_tracks": role_tracks,
            }
            row["field_hash"] = _field_hash(row, enriched.description)
            previous = previous_latest.get((row["source"], row["source_job_id"]))
            row["change_status"] = _change_status(row, previous)
            rows.append(row)
    return rows


def _disappeared_rows(
    previous_latest: dict[tuple[str, str], dict[str, Any]],
    current_rows: list[dict[str, Any]],
    observed_at: datetime,
    run_id: str,
) -> list[dict[str, Any]]:
    current_keys = {(row["source"], row["source_job_id"]) for row in current_rows}
    rows = []
    for key, previous in previous_latest.items():
        if key in current_keys or previous.get("active_state") != "active":
            continue
        row = {**previous}
        row["observation_timestamp"] = _dt(observed_at)
        row["ingestion_run_id"] = run_id
        row["active_state"] = "inactive"
        row["change_status"] = "DISAPPEARED"
        rows.append(row)
    return rows


def _change_status(row: dict[str, Any], previous: dict[str, Any] | None) -> str:
    if previous is None:
        return "NEW" if row["active_state"] == "active" else "DISAPPEARED"
    if previous.get("active_state") == "inactive" and row["active_state"] == "active":
        return "REAPPEARED"
    if row["active_state"] == "inactive":
        return "DISAPPEARED" if previous.get("active_state") == "active" else "UNCHANGED"
    if previous.get("field_hash") != row.get("field_hash"):
        return "CHANGED"
    return "UNCHANGED"


def _latest_by_source_key(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("source", ""), row.get("source_job_id", ""))
        if key not in latest or row.get("observation_timestamp", "") >= latest[key].get("observation_timestamp", ""):
            latest[key] = row
    return latest


def _latest_source_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(_latest_by_source_key(rows).values())


def _canonical_job_row(job: Job) -> dict[str, Any]:
    payload = job_to_dict(job)
    return {
        "canonical_job_id": job.id,
        "company": job.company,
        "title": job.title,
        "locations": json.dumps(payload.get("locations", []), ensure_ascii=False),
        "sources": ", ".join(sorted({observation.source_name for observation in job.source_observations})),
        "posted_at": _dt(job.posted_at),
        "workflow_status": job.workflow_status.value if isinstance(job.workflow_status, WorkflowStatus) else str(job.workflow_status),
        "sponsorship_classification": job.sponsorship.state.value if job.sponsorship else "unknown",
        "graduation_year_classification": job.graduation_year.state.value if job.graduation_year else "unknown",
        "role_tracks": ", ".join(score.track.value for score in job.role_track_profile.scores),
        "direct_apply_url": job.canonical_application_url,
    }


def _field_hash(row: dict[str, Any], description: str) -> str:
    meaningful = {
        key: row.get(key)
        for key in (
            "canonical_job_id",
            "source",
            "source_job_id",
            "company",
            "title",
            "normalized_location",
            "raw_location",
            "work_mode",
            "source_url",
            "direct_apply_url",
            "sponsorship_classification",
            "graduation_year_classification",
            "seniority",
            "role_tracks",
        )
    }
    meaningful["description_hash"] = hashlib.sha256(description.encode("utf-8")).hexdigest()
    return hashlib.sha256(json.dumps(meaningful, sort_keys=True).encode("utf-8")).hexdigest()


def _count_statuses(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"NEW": 0, "CHANGED": 0, "UNCHANGED": 0, "DISAPPEARED": 0, "REAPPEARED": 0}
    for row in rows:
        status = row.get("change_status")
        if status in counts:
            counts[status] += 1
    return counts


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _write_parquet_if_available(path: Path, rows: list[dict[str, Any]]) -> Path | None:
    try:
        import pandas as pd  # type: ignore

        pd.DataFrame(rows).to_parquet(path, index=False)
    except Exception:
        return None
    return path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _csv_value(value: Any) -> str:
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else str(value)


def _dt(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _valid_datetime(value: str) -> bool:
    return _parse_datetime(value) is not None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
