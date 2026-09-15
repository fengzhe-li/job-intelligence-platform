from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from jobintel.models.taxonomy import EvidenceSourceType, RoleTrack, SkillCategory


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


@dataclass
class Capability:
    name: str
    category: SkillCategory
    evidence: list[CapabilityEvidence] = field(default_factory=list)
    role_relevance: dict[RoleTrack, float] = field(default_factory=dict)

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

    def upsert_capability(self, capability: Capability) -> None:
        key = capability.name.casefold()
        if key in self.capabilities:
            existing = self.capabilities[key]
            existing.evidence.extend(capability.evidence)
            existing.role_relevance.update(capability.role_relevance)
        else:
            self.capabilities[key] = capability

    def capabilities_for_track(self, track: RoleTrack) -> list[Capability]:
        return sorted(
            self.capabilities.values(),
            key=lambda capability: capability.role_relevance.get(track, 0.0),
            reverse=True,
        )

