from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from jobintel.models.taxonomy import EvidenceSourceType, RoleTrack, SkillCategory

EvidenceTier = Literal["strong", "partial", "missing"]


@dataclass(frozen=True)
class EvidenceSource:
    id: str
    source_type: EvidenceSourceType
    title: str
    uri: str | None = None
    collected_at: date | None = None


@dataclass(frozen=True)
class CapabilityEvidence:
    source: EvidenceSource
    quote: str
    project_id: str | None = None
    confidence: float = 1.0
    id: str = ""
    # Phase 3.6 structured evidence enrichment fields
    category: str | None = None
    claim_type: str | None = None
    source_file: str | None = None
    source_location: str | None = None
    confidence_tier: str | None = None
    safe_paraphrase_scope: str | None = None
    forbidden_extrapolations: tuple[str, ...] = ()
    freshness: str | None = None


@dataclass
class Capability:
    name: str
    category: SkillCategory
    evidence: list[CapabilityEvidence] = field(default_factory=list)
    role_relevance: dict[RoleTrack, float] = field(default_factory=dict)
    technology: str | None = None
    domain: str | None = None
    last_verified: date | None = None

    def add_evidence(self, evidence: CapabilityEvidence) -> None:
        self.evidence.append(evidence)

    @property
    def confidence(self) -> float:
        if not self.evidence:
            return 0.0
        source_diversity = len({item.source.id for item in self.evidence})
        evidence_strength = min(sum(item.confidence for item in self.evidence) / 3.0, 1.0)
        diversity_bonus = min(source_diversity * 0.08, 0.24)
        return min(evidence_strength + diversity_bonus, 1.0)

    @property
    def evidence_tier(self) -> EvidenceTier:
        # Calibrated against `confidence`'s actual achievable range, not an
        # arbitrary 0-1 split: a single confident (1.0), direct piece of evidence
        # (the common case -- one README/manual/GitHub-sync quote) yields ~0.41
        # under that formula (it's designed to reward multi-source corroboration,
        # not just one very confident mention), so a "strong" threshold above that
        # would make single-source evidence structurally unable to ever be
        # "strong" -- too aggressive for the review gate built on top of this.
        if not self.evidence:
            return "missing"
        if self.confidence >= 0.4:
            return "strong"
        if self.confidence >= 0.15:
            return "partial"
        return "missing"


@dataclass(frozen=True)
class Skill:
    name: str
    category: SkillCategory


@dataclass(frozen=True)
class Technology:
    name: str
    category: SkillCategory
    version: str | None = None


@dataclass
class Project:
    id: str
    name: str
    description: str
    evidence_sources: list[EvidenceSource] = field(default_factory=list)
    role_relevance: dict[RoleTrack, float] = field(default_factory=dict)
    repository_full_name: str | None = None
    aliases: list[str] = field(default_factory=list)
    forbidden_extrapolations: list[str] = field(default_factory=list)
    track_mappings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Education:
    institution: str
    programme: str
    graduation_year: int
    description: str
    evidence_source: EvidenceSource | None = None


@dataclass(frozen=True)
class Experience:
    organisation: str
    title: str
    description: str
    start_date: date | None = None
    end_date: date | None = None
    evidence_source: EvidenceSource | None = None


@dataclass(frozen=True)
class CVVersion:
    id: str
    category: str
    text: str
    evidence_source: EvidenceSource


@dataclass
class CandidateProfile:
    id: str
    name: str
    graduation_year: int
    capabilities: dict[str, Capability] = field(default_factory=dict)
    projects: list[Project] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    experience: list[Experience] = field(default_factory=list)
    cv_versions: list[CVVersion] = field(default_factory=list)
    # Contact/identity fields for a real, submittable CV. Never fabricated --
    # remain None until explicitly set via `profile_ingestion.set_contact_details`
    # (CLI: `profile-set-contact`). Missing required fields (email, phone) are
    # surfaced by the CV review gate rather than silently rendered blank/fake.
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None

    def upsert_capability(self, capability: Capability) -> None:
        key = capability.name.casefold()
        if key in self.capabilities:
            existing = self.capabilities[key]
            existing.evidence.extend(capability.evidence)
            existing.role_relevance.update(capability.role_relevance)
            existing.technology = existing.technology or capability.technology
            existing.domain = existing.domain or capability.domain
            if capability.last_verified and (existing.last_verified is None or capability.last_verified > existing.last_verified):
                existing.last_verified = capability.last_verified
        else:
            self.capabilities[key] = capability

    def capabilities_for_track(self, track: RoleTrack) -> list[Capability]:
        return sorted(
            self.capabilities.values(),
            key=lambda capability: capability.role_relevance.get(track, 0.0),
            reverse=True,
        )

