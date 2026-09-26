"""One-time, idempotent repair of canonical jobs merged under the old dedup rules.

Before Phase 3 the title/company/location heuristics ignored seniority and
never checked whether two observations were different postings from the SAME
source, so e.g. TransFICC's junior, mid and senior roles collapsed into one
canonical job and the junior one became invisible. The current rules
(`dedup.v1`) no longer do that, but a merged record keeps re-matching itself
by source id, so persisted history never heals on its own.

For every canonical job with more than one observation this rebuilds one job
per persisted observation -- re-normalised from that observation's own raw
payload by its own connector, with the ORIGINAL SourceObservation kept
verbatim (ids, URLs, first/last seen, active state, deadline, raw data) -- and
re-runs the CURRENT dedup over just those. Only when they resolve into more
than one vacancy is the job split. Legitimate duplicates (the same vacancy seen
on several sources) resolve to one group and are left exactly as persisted.

A merged job is left UNCHANGED and reported for manual review when splitting
it could misattribute something: it has a workflow status, application or CV
artifact attached; an observation's source has no known normaliser; the
original id can't be assigned to exactly one group; a new id would collide
with an existing job; or a split-off vacancy would match a DIFFERENT existing
canonical job (re-homing it is a separate decision).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.connectors.adzuna import AdzunaConnector
from jobintel.connectors.ashby import AshbyConnector
from jobintel.connectors.base import RawJobPayload
from jobintel.connectors.graduate_sources import GRADUATE_SOURCE_SPECS
from jobintel.connectors.greenhouse import GreenhouseConnector
from jobintel.connectors.lever import LeverConnector
from jobintel.connectors.manual_source import ManualSourceConnector
from jobintel.connectors.prospects import ProspectsConnector
from jobintel.connectors.smartrecruiters import SmartRecruitersConnector
from jobintel.connectors.welcome_to_the_jungle import WelcomeToTheJungleConnector
from jobintel.connectors.workable import WorkableConnector
from jobintel.connectors.workday import WorkdayConnector
from jobintel.dedup.v1 import deduplicate_jobs, find_match
from jobintel.models.job import Job, SourceObservation

REPAIR_LOG_NAME = "dedup_repair_log.jsonl"
_MANUAL_METHODS = {"manual_import", "discovery_import", "single_url_import"}

Normaliser = Callable[[RawJobPayload], Job]


def default_normalisers() -> dict[str, Normaliser]:
    live: dict[str, Normaliser] = {
        "greenhouse": GreenhouseConnector(()).normalise,
        "lever": LeverConnector(()).normalise,
        "ashby": AshbyConnector(()).normalise,
        "workable": WorkableConnector(()).normalise,
        "smartrecruiters": SmartRecruitersConnector(()).normalise,
        "workday": WorkdayConnector(()).normalise,
        "adzuna": AdzunaConnector(None, None).normalise,
        "welcome_to_the_jungle": WelcomeToTheJungleConnector((), None).normalise,
    }
    manual = {name: ManualSourceConnector(spec, ()).normalise for name, spec in GRADUATE_SOURCE_SPECS.items()}
    prospects_live = ProspectsConnector().normalise

    def prospects(raw: RawJobPayload) -> Job:
        # Live discovery and manual imports share source_name "prospects" but
        # have different payload shapes.
        if raw.raw_payload.get("_ingestion_method") in _MANUAL_METHODS:
            return manual["prospects"](raw)
        return prospects_live(raw)

    return {**manual, **live, "prospects": prospects}


@dataclass(frozen=True)
class RepairCase:
    original_id: str
    action: str  # "split" | "ambiguous"
    reason: str
    # One entry per reconstructed vacancy: (canonical id, title, [source:source_job_id, ...])
    groups: list[tuple[str, str, list[str]]] = field(default_factory=list)


@dataclass(frozen=True)
class RepairReport:
    inspected: int
    multi_observation: int
    jobs_before: int
    jobs_after: int
    cases: list[RepairCase]
    dry_run: bool
    backup_path: str | None = None

    @property
    def splits(self) -> list[RepairCase]:
        return [case for case in self.cases if case.action == "split"]

    @property
    def ambiguous(self) -> list[RepairCase]:
        return [case for case in self.cases if case.action == "ambiguous"]


def plan_repair(
    jobs: list[Job],
    referenced_job_ids: set[str],
    normalisers: dict[str, Normaliser] | None = None,
    graduation_year: int = 2026,
) -> tuple[list[Job], list[RepairCase]]:
    """Pure: returns (the repaired job list, one case per split/ambiguous job).
    Jobs that need no change are returned as the very same objects."""
    normalisers = normalisers if normalisers is not None else default_normalisers()
    # Pass 1: reconstruct every multi-observation job under the current rules.
    reconstructed: dict[int, list[Job] | str] = {}
    for index, job in enumerate(jobs):
        if len(job.source_observations) < 2:
            continue
        missing = sorted({o.source_name for o in job.source_observations if o.source_name not in normalisers})
        if missing:
            reconstructed[index] = f"no normaliser for source(s) {missing}; cannot reconstruct safely"
            continue
        reconstructed[index] = deduplicate_jobs([_rebuild_piece(job, observation, normalisers, graduation_year) for observation in job.source_observations])
    # The vacancies every OTHER job stands for once repaired -- a split-off
    # piece is checked against these, not against still-broken merged records.
    pool = {index: (value if isinstance(value, list) else [jobs[index]]) for index, value in reconstructed.items()}

    # Pass 2: decide.
    existing_ids = {job.id for job in jobs}
    result: list[Job] = []
    cases: list[RepairCase] = []
    for index, job in enumerate(jobs):
        value = reconstructed.get(index)
        if value is None:
            result.append(job)
            continue
        if isinstance(value, str):
            if _has_same_source_distinct_ids(job):
                cases.append(RepairCase(job.id, "ambiguous", value))
            result.append(job)
            continue
        groups = value
        if len(groups) == 1:
            result.append(job)  # legitimate merge under current rules: untouched
            continue
        described = [(group.id, group.title, [f"{o.source_name}:{o.source_job_id}" for o in group.source_observations]) for group in groups]
        others = [candidate for other_index, other in enumerate(jobs) if other_index != index for candidate in pool.get(other_index, [other])]
        reason = _ambiguity(job, groups, referenced_job_ids, existing_ids, others)
        if reason:
            cases.append(RepairCase(job.id, "ambiguous", reason, described))
            result.append(job)
            continue
        repaired = [replace(group, id=job.id, workflow_status=job.workflow_status) if _owns_id(group, job.id) else group for group in groups]
        existing_ids.update(group.id for group in repaired)
        cases.append(RepairCase(job.id, "split", f"observations resolve into {len(groups)} distinct vacancies under current dedup rules", [(group.id, group.title, obs) for group, (_, _, obs) in zip(repaired, described)]))
        result.extend(repaired)
    return result, cases


def repair_historical_merges(
    store_root: Path | str,
    backups_dir: Path | str,
    dry_run: bool = False,
    graduation_year: int = 2026,
    normalisers: dict[str, Normaliser] | None = None,
) -> RepairReport:
    """Backs the whole store up (timestamped, never deleted) before writing,
    applies `plan_repair`, persists exactly the repaired list, and appends
    every split/ambiguous case to processed/dedup_repair_log.jsonl."""
    from jobintel.storage.application_store import ApplicationStore
    from jobintel.storage.cv_artifact_store import CVArtifactStore
    from jobintel.storage.local_store import LocalJobStore

    store = LocalJobStore(store_root)
    jobs = store.read_jobs()
    references = set(store.read_workflow_statuses())
    references |= {application.job_id for application in ApplicationStore(store.root).read_applications()}
    references |= {artifact.job_id for artifact in CVArtifactStore(store.root).read_all()}
    repaired, cases = plan_repair(jobs, references, normalisers, graduation_year)

    backup_path: str | None = None
    if not dry_run and any(case.action == "split" for case in cases):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = Path(backups_dir) / f"local-before-dedup-repair-{stamp}"
        shutil.copytree(store.root, target)
        backup_path = str(target)
        store.replace_jobs(repaired)
    if not dry_run and cases:
        log = store.processed_dir / REPAIR_LOG_NAME
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            for case in cases:
                handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "backup": backup_path, **asdict(case)}, ensure_ascii=False, sort_keys=True) + "\n")
    return RepairReport(
        inspected=len(jobs),
        multi_observation=sum(1 for job in jobs if len(job.source_observations) > 1),
        jobs_before=len(jobs),
        jobs_after=len(repaired),
        cases=cases,
        dry_run=dry_run,
        backup_path=backup_path,
    )


def _rebuild_piece(job: Job, observation: SourceObservation, normalisers: dict[str, Normaliser], graduation_year: int) -> Job:
    raw = RawJobPayload(
        source_name=observation.source_name,
        source_job_id=observation.source_job_id,
        source_url=observation.original_url,
        canonical_application_url=observation.canonical_application_url,
        raw_payload=observation.raw_payload,
        observed_at=observation.last_seen_at,
        posted_at=observation.posted_at,
        first_seen_at=observation.first_seen_at,
        last_seen_at=observation.last_seen_at,
    )
    piece = normalisers[observation.source_name](raw)
    # The persisted observation is the provenance record: keep it verbatim.
    piece = replace(piece, source_observations=[observation], workflow_status=job.workflow_status)
    return enrich_job(piece, graduation_year)


def _owns_id(group: Job, job_id: str) -> bool:
    return any(f"{o.source_name}:{o.source_job_id}" == job_id for o in group.source_observations)


def _ambiguity(job: Job, groups: list[Job], referenced: set[str], existing_ids: set[str], others: list[Job]) -> str | None:
    if job.id in referenced:
        return "has workflow/application/CV references -- cannot tell which reconstructed vacancy they belong to"
    owners = [group for group in groups if _owns_id(group, job.id)]
    if len(owners) != 1:
        return f"original id {job.id} does not identify exactly one reconstructed vacancy"
    new_ids = [group.id for group in groups if group is not owners[0]]
    if len(set(new_ids)) != len(new_ids) or any(new_id in existing_ids or new_id == job.id for new_id in new_ids):
        return f"reconstructed id collides with an existing canonical job: {new_ids}"
    for group in groups:
        if group is owners[0]:
            continue
        match = find_match(others, group)
        if match is not None:
            return f"split-off vacancy {group.id} would match existing canonical job {match.id}; re-homing needs review"
    return None


def _has_same_source_distinct_ids(job: Job) -> bool:
    seen: dict[str, str] = {}
    for observation in job.source_observations:
        if observation.source_name in seen and seen[observation.source_name] != observation.source_job_id:
            return True
        seen[observation.source_name] = observation.source_job_id
    return False
