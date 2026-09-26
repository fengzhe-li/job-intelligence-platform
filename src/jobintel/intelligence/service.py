from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jobintel.analysis.jd_requirements import JDRequirements, extract_jd_requirements
from jobintel.config import RankingConfig, default_ranking_config
from jobintel.intelligence.grounding import (
    BULLET_SCHEMA, MATCH_SCHEMA, REQUIREMENT_SCHEMA, RELATIONS, LEVEL, GroundedRequirement,
    concept, evidence_records, select_evidence, validate_bullets, validate_matches, validate_requirements,
)
from jobintel.intelligence.provider import GeminiProvider, IntelligenceConfig, IntelligenceProvider, ProviderFailure
from jobintel.matching.matcher import MatchResult, match_job
from jobintel.models.candidate import CandidateProfile, CapabilityEvidence, Project
from jobintel.models.job import Job

PROMPT_VERSION = "3.5a-1"
SCHEMA_VERSION = "1"
GROUNDING_VERSION = "1"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


@dataclass
class IntelligenceMetadata:
    provider: str = "gemini"
    model: str = ""
    used: str = "deterministic_fallback"
    cache: str = "disabled"
    fallback_reason: str | None = None
    validation_failures: list[str] = field(default_factory=list)
    cache_warning: str | None = None
    prompt_version: str = PROMPT_VERSION
    schema_version: str = SCHEMA_VERSION
    grounding_version: str = GROUNDING_VERSION
    selected_evidence_count: int = 0
    omitted_evidence_count: int = 0


@dataclass
class HybridResult:
    deterministic_jd: JDRequirements
    deterministic_match: MatchResult
    requirements: list[GroundedRequirement] = field(default_factory=list)
    semantic_matches: list[dict] = field(default_factory=list)
    combined: list[dict] = field(default_factory=list)
    disagreements: list[dict] = field(default_factory=list)
    project_relevance: list[dict] = field(default_factory=list)
    deterministic_requirements_not_extracted: list[str] = field(default_factory=list)
    metadata: IntelligenceMetadata = field(default_factory=IntelligenceMetadata)

    def to_dict(self) -> dict:
        return asdict(self)


class IntelligenceService:
    """Explicit on-demand overlay: cannot mutate discovery, ranking, projects or CVs.

    Default disk cache belongs under the caller's local store; no implicit global
    state and no candidate contact details are sent to the provider.
    """
    def __init__(self, config: IntelligenceConfig | None = None,
                 provider: IntelligenceProvider | None = None, cache_dir: Path | None = None):
        self.config = config or IntelligenceConfig.from_env()
        self.provider = provider or GeminiProvider(self.config)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._memory: dict[str, dict] = {}

    def analyze(self, job: Job, profile: CandidateProfile, ranking_config: RankingConfig | None = None) -> HybridResult:
        result = HybridResult(extract_jd_requirements(job), match_job(profile, job, ranking_config or default_ranking_config()))
        cfg = self.config
        # Invalid configuration is not echoed (it may accidentally contain a secret).
        if not cfg.valid():
            result.metadata.fallback_reason = "invalid_configuration"
            return result
        meta = result.metadata = IntelligenceMetadata(provider=cfg.provider, model=cfg.model)
        if not os.getenv("GEMINI_API_KEY", "").strip():
            meta.fallback_reason = "not_configured"
            return result
        if not job.description.strip() or len(job.description) > cfg.max_jd_chars:
            meta.fallback_reason = "jd_empty_or_input_limit"
            return result
        # Full profile fingerprint is LOCAL ONLY. Even unrelated profile changes safely
        # invalidate results; only selected quotations are sent to Gemini.
        key = digest({"jd": job.description, "profile_version": digest(asdict(profile)),
                      "config": asdict(cfg), "prompt": PROMPT_VERSION, "schema": SCHEMA_VERSION,
                      "grounding": GROUNDING_VERSION})
        meta.cache = "miss"
        cached = self._read_cache(key, meta)
        try:
            raw_jd = cached["jd"] if cached else self.provider.generate("extract", {"jd_text": job.description}, REQUIREMENT_SCHEMA)
            requirements, failures = validate_requirements(raw_jd, job.description)
            if failures:
                return self._fallback(result, "grounding_failed", failures)
            if not requirements:
                return self._fallback(result, "no_grounded_requirements")
            records = evidence_records(profile)
            selected = select_evidence(requirements, records, cfg.max_evidence, cfg.max_evidence_chars)
            meta.selected_evidence_count = len(selected)
            meta.omitted_evidence_count = len(records) - len(selected)
            if cached:
                raw_matches = cached["semantic"]
            elif requirements and selected:
                raw_matches = self.provider.generate("match", {
                    "requirements": [asdict(r) for r in requirements],
                    "evidence": [asdict(e) for e in selected], "allowed_relations": RELATIONS,
                    "explanation_format": "{relation}; {strength} support from existing evidence {evidence_id}.",
                    "relation_format": "Use direct for equivalent concepts; otherwise lowercase canonical source -> target, e.g. fastapi -> rest api.",
                }, MATCH_SCHEMA)
            else:
                raw_matches = {"matches": []}
            matches, failures = validate_matches(raw_matches, requirements, selected)
            if failures:
                return self._fallback(result, "grounding_failed", failures)
        except ProviderFailure as exc:
            allowed = {"provider_error", "provider_timeout", "malformed_response", "quota_exhausted", "authentication_failed", "not_configured", "invalid_configuration"}
            reason = str(exc) if str(exc) in allowed else "provider_error"
            return self._fallback(result, reason)
        except TimeoutError:
            return self._fallback(result, "provider_timeout")
        except (ValueError, TypeError, KeyError, AttributeError):
            return self._fallback(result, "malformed_response")
        except Exception:
            # Never retain exception messages, model response bodies, or unvalidated text.
            return self._fallback(result, "provider_or_schema_error")
        meta.used = "gemini_assisted"
        result.requirements, result.semantic_matches = requirements, matches
        self._combine(result, profile)
        safe = {"jd": {"requirements": [asdict(r) for r in requirements]}, "semantic": {"matches": matches}}
        if not cached:
            self._memory[key] = safe
            self._write_cache(key, safe, meta)
        return result

    def rewrite_bullets(
        self,
        job: Job,
        profile: CandidateProfile,
        projects: list[Project],
        project_evidence: dict[str, list[CapabilityEvidence]],
    ) -> tuple[list[dict], list[dict], str]:
        """Request and validate claim-grounded bullet rewrites for selected projects.

        Returns:
            (accepted_bullets, rejected_bullets, mode)
        """
        cfg = self.config
        if not cfg.valid() or not os.getenv("GEMINI_API_KEY", "").strip():
            return [], [], "deterministic_fallback"

        evidence_fingerprint = {
            pid: sorted(f"{e.id}:{digest(e.quote)}" for e in evs)
            for pid, evs in project_evidence.items()
        }
        key = digest({
            "task": "rewrite_bullets",
            "jd": job.description,
            "projects": sorted(p.id for p in projects),
            "evidence": evidence_fingerprint,
            "prompt": PROMPT_VERSION,
            "schema": SCHEMA_VERSION,
            "grounding": GROUNDING_VERSION,
        })

        meta = IntelligenceMetadata(provider=cfg.provider, model=cfg.model)
        cached = self._read_cache(key, meta)
        if cached and "bullets" in cached:
            accepted, rejected = validate_bullets(cached, projects, profile)
            if accepted:
                return accepted, rejected, "gemini_assisted"

        # Prepare inputs strictly with project evidence
        inputs = {
            "job_title": job.title,
            "job_description_snippet": job.description[:2000],
            "projects": [
                {
                    "project_id": p.id,
                    "project_name": p.name,
                    "evidence": [
                        {"evidence_id": e.id, "quote": e.quote}
                        for e in project_evidence.get(p.id, [])
                    ],
                }
                for p in projects
            ],
        }

        try:
            raw = self.provider.generate("rewrite_bullets", inputs, BULLET_SCHEMA)
            accepted, rejected = validate_bullets(raw, projects, profile)
            if accepted:
                safe = {"bullets": accepted, "rejected": rejected}
                self._memory[key] = safe
                self._write_cache(key, safe, meta)
                return accepted, rejected, "gemini_assisted"
            return [], rejected, "deterministic_fallback"
        except Exception:
            return [], [], "deterministic_fallback"

    @staticmethod
    def _fallback(result: HybridResult, reason: str, failures: list[str] | None = None) -> HybridResult:
        result.metadata.fallback_reason = reason
        result.metadata.validation_failures = failures or []
        return result

    @staticmethod
    def _combine(result: HybridResult, profile: CandidateProfile) -> None:
        projects: dict[str, dict] = {}
        deterministic_names = set(result.deterministic_jd.must_have_skills + result.deterministic_jd.preferred_skills)
        extracted = {concept(r.normalized) for r in result.requirements}
        result.deterministic_requirements_not_extracted = sorted(name for name in deterministic_names if concept(name) not in extracted)
        for req in result.requirements:
            cap = profile.capabilities.get(req.normalized.casefold())
            literal_tier = cap.evidence_tier if cap and req.normalized in deterministic_names else "missing"
            deterministic_modality = ("required" if req.normalized in result.deterministic_jd.must_have_skills
                                      else "preferred" if req.normalized in result.deterministic_jd.preferred_skills else "not_extracted")
            supported = [m for m in result.semantic_matches if m["requirement_id"] == req.id]
            semantic_tier = max((m["strength"] for m in supported), key=LEVEL.get, default="missing")
            row = {"requirement_id": req.id, "deterministic_evidence": literal_tier,
                   "semantic_evidence": semantic_tier,
                   "combined_evidence": max((literal_tier, semantic_tier), key=LEVEL.get),
                   "deterministic_modality": deterministic_modality, "semantic_modality": req.modality,
                   "review_required": literal_tier != semantic_tier or req.modality != deterministic_modality,
                   "advisory_only": True}
            result.combined.append(row)
            if row["review_required"]:
                result.disagreements.append(dict(row))
            for match in supported:
                pid = match["project_id"]
                if pid is None:
                    continue
                signal = projects.setdefault(pid, {"project_id": pid, "requirement_ids": [], "evidence_ids": [], "advisory_only": True})
                signal["requirement_ids"].append(req.id)
                signal["evidence_ids"].append(match["evidence_id"])
        for signal in projects.values():
            signal["requirement_ids"] = sorted(set(signal["requirement_ids"]))
            signal["evidence_ids"] = sorted(set(signal["evidence_ids"]))
        result.project_relevance = sorted(projects.values(), key=lambda p: p["project_id"])

    def _read_cache(self, key: str, meta: IntelligenceMetadata) -> dict | None:
        if key in self._memory:
            meta.cache = "hit"
            return self._memory[key]
        if self.cache_dir is None:
            return None
        try:
            path = self.cache_dir / f"{key}.json"
            if not path.exists():
                return None
            if path.stat().st_size > 1_000_000:
                raise ValueError()
            payload = json.loads(path.read_text())
            if payload["key"] != key or not isinstance(payload["result"], dict):
                raise ValueError()
            meta.cache = "hit"
            return payload["result"]
        except Exception:
            # Corrupt or inaccessible cache is a miss; grounding is rechecked on hits.
            meta.cache_warning = "cache_read_failed"
            return None

    def _write_cache(self, key: str, result: dict, meta: IntelligenceMetadata) -> None:
        if self.cache_dir is None:
            return
        path = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.cache_dir, suffix=".tmp", delete=False) as handle:
                path = Path(handle.name)
                json.dump({"key": key, "result": result}, handle, ensure_ascii=False)
            path.replace(self.cache_dir / f"{key}.json")
        except Exception:
            meta.cache_warning = "cache_write_failed"
        finally:
            if path is not None and path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
