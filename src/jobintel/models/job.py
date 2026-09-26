from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jobintel.models.taxonomy import (
    GraduationYearState,
    RoleTrack,
    SponsorshipState,
    WorkflowStatus,
    WorkMode,
)


# Sources that ARE the employer's own applicant-tracking system (or careers
# site). When one vacancy is observed both here and via an aggregator/job
# board (Adzuna, Prospects, WTTJ, manual imports), the direct observation wins
# canonical display fields and the canonical application URL; every
# observation is still retained as provenance. Welcome to the Jungle is
# deliberately NOT listed: it is an employer-branding job marketplace whose
# apply links frequently hand off to a separate employer ATS.
DIRECT_ATS_SOURCES = frozenset({"company", "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday"})


@dataclass(frozen=True)
class Location:
    city: str | None
    country: str
    region: str | None = None
    work_mode: WorkMode = WorkMode.UNKNOWN
    raw: str | None = None

    @property
    def is_uk(self) -> bool:
        return self.country.casefold() in {"uk", "united kingdom", "gb", "great britain"}

    @property
    def is_london(self) -> bool:
        return (self.city or "").casefold() == "london"


@dataclass(frozen=True)
class SourceObservation:
    source_name: str
    source_job_id: str
    original_url: str
    first_seen_at: datetime
    last_seen_at: datetime
    posted_at: datetime | None
    raw_description: str
    canonical_application_url: str
    raw_payload: dict[str, Any] = field(default_factory=dict)
    active: bool = True
    latest_observed_state: str = "active"
    # Only set when a source explicitly states an application deadline -- never
    # inferred or defaulted. Kept per-observation (not on the canonical Job) so
    # that when the same vacancy is seen via two sources with different stated
    # deadlines, both are preserved rather than one silently overwriting the
    # other -- see Job.deadline_conflict below.
    deadline: datetime | None = None


@dataclass(frozen=True)
class RoleTrackScore:
    track: RoleTrack
    score: float
    evidence: list[str] = field(default_factory=list)


@dataclass
class RoleTrackProfile:
    scores: list[RoleTrackScore] = field(default_factory=list)

    def primary(self) -> RoleTrackScore | None:
        return max(self.scores, key=lambda item: item.score, default=None)

    def secondary(self, threshold: float = 0.15) -> list[RoleTrackScore]:
        primary = self.primary()
        return [
            item
            for item in sorted(self.scores, key=lambda score: score.score, reverse=True)
            if item is not primary and item.score >= threshold
        ]


@dataclass(frozen=True)
class SkillRequirement:
    name: str
    required: bool = True
    evidence: str = ""


@dataclass(frozen=True)
class SponsorshipEvidence:
    state: SponsorshipState
    evidence_text: str
    confidence: float = 1.0


@dataclass(frozen=True)
class GraduationYearEvidence:
    state: GraduationYearState
    evidence_text: str
    confidence: float = 1.0
    # Informational only -- a job's intake/start year (e.g. "2027 Graduate
    # Programme") is NOT a graduation-year eligibility signal and never drives
    # `state` or ranking weight. See analysis.evidence.detect_graduation_year.
    intake_year: int | None = None


@dataclass(frozen=True)
class EligibilityEvidence:
    label: str
    evidence_text: str
    confidence: float = 1.0


@dataclass
class Job:
    id: str
    title: str
    company: str
    description: str
    locations: list[Location]
    source_observations: list[SourceObservation]
    salary: str | None = None
    raw_location: str | None = None
    role_track_profile: RoleTrackProfile = field(default_factory=RoleTrackProfile)
    skill_requirements: list[SkillRequirement] = field(default_factory=list)
    sponsorship: SponsorshipEvidence | None = None
    graduation_year: GraduationYearEvidence | None = None
    eligibility: list[EligibilityEvidence] = field(default_factory=list)
    workflow_status: WorkflowStatus = WorkflowStatus.NEW

    @property
    def posted_at(self) -> datetime | None:
        dates = [item.posted_at for item in self.source_observations if item.posted_at]
        return max(dates, default=None)

    @property
    def deadline_observations(self) -> dict[str, datetime]:
        """Per-source stated deadlines, only for sources that actually gave one."""
        return {item.source_name: item.deadline for item in self.source_observations if item.deadline is not None}

    @property
    def deadline_conflict(self) -> bool:
        """True when two or more sources state a *different* deadline for this job."""
        return len(set(self.deadline_observations.values())) > 1

    @property
    def earliest_deadline(self) -> datetime | None:
        """Soonest stated deadline across sources -- the one that matters for not missing it.

        Does not resolve a conflict silently: `deadline_conflict` still reports
        True and `deadline_observations` still exposes every source's value.
        """
        values = list(self.deadline_observations.values())
        return min(values) if values else None

    @property
    def canonical_application_url(self) -> str:
        preferred = [
            item
            for item in self.source_observations
            if item.source_name.casefold() in DIRECT_ATS_SOURCES
        ]
        observation = preferred[0] if preferred else self.source_observations[0]
        return observation.canonical_application_url
