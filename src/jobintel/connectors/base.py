from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jobintel.models.job import Job


@dataclass(frozen=True)
class RawJobPayload:
    source_name: str
    source_job_id: str
    source_url: str
    canonical_application_url: str
    raw_payload: dict[str, Any]
    observed_at: datetime
    posted_at: datetime | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True)
class SourceHealth:
    source_name: str
    ok: bool
    message: str
    checked_at: datetime


@dataclass(frozen=True)
class ConnectorQuery:
    keywords: tuple[str, ...] = ()
    location: str = "United Kingdom"
    limit: int = 50
    since: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class JobSourceConnector(ABC):
    source_name: str

    @abstractmethod
    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        """Fetch current jobs from a compliant source endpoint."""

    def fetch_changed_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        jobs = self.fetch_jobs(query)
        if query.since is None:
            return jobs
        return [job for job in jobs if (job.posted_at and job.posted_at >= query.since) or job.observed_at >= query.since]

    @abstractmethod
    def health_check(self) -> SourceHealth:
        """Return source availability/configuration status."""

    @abstractmethod
    def normalise(self, raw: RawJobPayload) -> Job:
        """Map source payload into the canonical Job model."""

