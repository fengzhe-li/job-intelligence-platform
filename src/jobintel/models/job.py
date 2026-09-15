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
    def canonical_application_url(self) -> str:
        direct_sources = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "company")
        preferred = [
            item
            for item in self.source_observations
            if item.source_name.casefold() in direct_sources
        ]
        observation = preferred[0] if preferred else self.source_observations[0]
        return observation.canonical_application_url
