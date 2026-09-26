from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
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


class PartialFetchError(Exception):
    """Raised when a multi-identifier connector (e.g. several Greenhouse board
    tokens sharing one connector instance) succeeds for some identifiers and
    fails for others.

    Without this, a single broken company's board/account/site would raise an
    unstructured exception, silently losing that refresh cycle's results for
    every OTHER, healthy identifier too -- not just the broken one. The refresh
    pipeline (pipeline.refresh) still writes the partial_payloads that
    succeeded; a failed identifier's scope is never in `SourceSnapshot.
    complete_scopes`, so its jobs can't be closed this cycle (see
    docs/SOURCE_COVERAGE.md, "Closure invariant").
    """

    def __init__(self, partial_payloads: list["RawJobPayload"], failures: dict[str, str]) -> None:
        self.partial_payloads = partial_payloads
        self.failures = failures
        super().__init__(f"{len(failures)} identifier(s) failed: {failures}; {len(partial_payloads)} job(s) recovered from the rest")


@dataclass(frozen=True)
class ConnectorQuery:
    keywords: tuple[str, ...] = ()
    location: str = "United Kingdom"
    # Multi-identifier ATS connectors apply this cap PER identifier (board/
    # site/tenant), never across all of them combined -- a combined cap made
    # whichever boards came last in the registry silently invisible.
    limit: int = 50
    since: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# Key written into every RawJobPayload.raw_payload (and therefore persisted on
# its SourceObservation) naming the closure scope the job was observed in --
# e.g. one Greenhouse board token. See SourceSnapshot.
CLOSURE_SCOPE_KEY = "_closure_scope"


@dataclass(frozen=True)
class SourceSnapshot:
    """One fetch's payloads plus EXPLICIT evidence of what it covered completely.

    THE closure invariant (docs/SOURCE_COVERAGE.md): absence may only imply
    closure when the current observation is known to be a complete snapshot of
    the relevant source scope. `complete_scopes` is that knowledge -- the
    scopes (a board token, Lever site, Workday tenant/site, WTTJ organization
    ...) for which `payloads` holds the scope's ENTIRE current listing: fetch
    succeeded, pagination was exhausted, nothing was truncated by a limit or
    page cap, nothing was dropped by a keyword filter, and no per-job detail
    fetch failed. It is empty by default, so any connector that doesn't
    positively prove completeness (search aggregators, category-limited
    discovery, manual imports) can never cause a closure -- fail-closed.
    """

    payloads: list[RawJobPayload]
    complete_scopes: frozenset[str] = frozenset()
    # Scope/identifier -> human-readable reason it is NOT complete this cycle
    # (truncated, filtered, page cap, ...). Surfaced in source health.
    incomplete_scopes: dict[str, str] = field(default_factory=dict)
    # Identifier -> error, for fetches that actually failed.
    failures: dict[str, str] = field(default_factory=dict)

    def payloads_or_raise(self) -> list[RawJobPayload]:
        """Adapter for the plain `fetch_jobs` contract: any failure is raised
        as PartialFetchError, exactly as before snapshots existed."""
        if self.failures:
            raise PartialFetchError(self.payloads, self.failures)
        return self.payloads


class ScopeCollector:
    """Accumulates one multi-identifier fetch into a SourceSnapshot, applying
    the per-identifier limit and recording exactly why a scope is incomplete.
    Shared by every direct-ATS connector so the completeness rules can't drift
    between them."""

    def __init__(self, query: ConnectorQuery) -> None:
        self.query = query
        self.payloads: list[RawJobPayload] = []
        self.complete: set[str] = set()
        self.incomplete: dict[str, str] = {}
        self.failures: dict[str, str] = {}

    def fail(self, scope: str, identifier: str, message: str) -> None:
        self.failures[identifier] = message
        self.mark_incomplete(scope, f"fetch failed: {message}")

    def mark_incomplete(self, scope: str, reason: str) -> None:
        self.complete.discard(scope)
        self.incomplete.setdefault(scope, reason)

    def add_scope(self, scope: str, listed: list[RawJobPayload], filtered_out: int = 0, exhausted: bool = True) -> None:
        """`listed`: this scope's relevant payloads, in source order.
        `filtered_out`: listed jobs dropped by the query's keyword filter.
        `exhausted`: False if pagination stopped at a safety cap rather than
        at the real end of the listing."""
        tagged = [_with_scope(payload, scope) for payload in listed]
        kept = tagged[: self.query.limit]
        self.payloads.extend(kept)
        if len(kept) < len(tagged):
            self.mark_incomplete(scope, f"truncated at limit {self.query.limit} of {len(tagged)} listed jobs")
        if filtered_out:
            self.mark_incomplete(scope, f"keyword-filtered query dropped {filtered_out} listed job(s)")
        if not exhausted:
            self.mark_incomplete(scope, "pagination stopped at a safety page cap before the end of the listing")
        if scope not in self.incomplete:
            self.complete.add(scope)

    def snapshot(self) -> SourceSnapshot:
        return SourceSnapshot(self.payloads, frozenset(self.complete - set(self.incomplete)), dict(self.incomplete), dict(self.failures))


def _with_scope(payload: RawJobPayload, scope: str) -> RawJobPayload:
    return replace(payload, raw_payload={**payload.raw_payload, CLOSURE_SCOPE_KEY: scope})


class JobSourceConnector(ABC):
    source_name: str
    # Informational: False marks sources that by nature can NEVER supply a
    # complete snapshot (capped/ranked search aggregators). Closure itself is
    # governed only by `fetch_snapshot().complete_scopes`, which is empty for
    # every connector that doesn't override `fetch_snapshot`.
    supports_closure_inference: bool = False

    def fetch_snapshot(self, query: ConnectorQuery) -> SourceSnapshot:
        """Fetch plus completeness evidence. The default wraps `fetch_jobs`
        and claims NO complete scope, so absence from it never closes
        anything. A PartialFetchError is folded into `failures`; any other
        exception propagates to the caller as a total failure."""
        try:
            return SourceSnapshot(self.fetch_jobs(query))
        except PartialFetchError as exc:
            return SourceSnapshot(exc.partial_payloads, failures=exc.failures)

    def closure_scope(self, raw_payload: dict[str, Any]) -> str | None:
        """Which scope a stored observation belongs to. Observations with no
        known scope are never closed by absence. Connectors override this only
        to recognise observations stored before `_closure_scope` existed."""
        scope = raw_payload.get(CLOSURE_SCOPE_KEY) if isinstance(raw_payload, dict) else None
        return str(scope) if scope else None

    @abstractmethod
    def fetch_jobs(self, query: ConnectorQuery) -> list[RawJobPayload]:
        """Fetch current jobs from a compliant source endpoint.

        A connector configured with multiple company/board identifiers should
        isolate failures per identifier (one broken board must not silently drop
        every other configured company's results): catch per-identifier
        exceptions, keep going, and raise `PartialFetchError` at the end if any
        failed, carrying both the results that DID succeed and which identifiers
        failed -- a partially-successful refresh must still surface exactly what
        failed, per the project's "never silently represent a failure as 0 new
        jobs" rule.
        """

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

