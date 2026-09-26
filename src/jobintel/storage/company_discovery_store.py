from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CompanyObservation:
    """One company seen through a non-registry-scoped source (Prospects,
    Adzuna, or a manual import) that isn't already in the target-company
    registry. Provenance is preserved (which source, which specific job) so
    a human reviewing candidates can judge them, per Phase 2.7 Section G:
    "do not auto-trust arbitrary domains."
    """

    company_name: str
    observed_via_source: str
    example_job_title: str
    example_job_url: str
    example_application_url: str
    suggested_connector_type: str | None
    suggested_connector_token: str | None
    observed_at: str


class CompanyDiscoveryStore:
    """Append-only JSONL log of observed non-registry companies -- same
    provenance-preserving convention as HistoricalJobStore's snapshot log and
    the application status-history log elsewhere in this project.
    """

    def __init__(self, root: Path | str = "data/local") -> None:
        self.root = Path(root)
        self.path = self.root / "company_discovery" / "observed_companies.jsonl"

    def append(self, observation: CompanyObservation) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(observation), ensure_ascii=False) + "\n")

    def read_all(self) -> list[CompanyObservation]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(CompanyObservation(**json.loads(line)))
        return records

    def latest_per_company(self) -> dict[str, CompanyObservation]:
        latest: dict[str, CompanyObservation] = {}
        for observation in self.read_all():
            latest[observation.company_name.casefold()] = observation
        return latest
