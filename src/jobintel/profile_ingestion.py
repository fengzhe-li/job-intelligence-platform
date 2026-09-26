from __future__ import annotations

import json
import re
import zipfile
from dataclasses import asdict
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

from jobintel.analysis.role_tracks import SKILL_PATTERNS, infer_role_track_profile
from jobintel.connectors.utils import strip_html
from jobintel.models.candidate import (
    CVVersion,
    CandidateProfile,
    Capability,
    CapabilityEvidence,
    Education,
    EvidenceSource,
    Experience,
    Project,
)
from jobintel.models.taxonomy import EvidenceSourceType, RoleTrack, SkillCategory


SOURCE_TYPE_BY_LABEL = {item.value: item for item in EvidenceSourceType}

PROJECT_SOURCE_TYPES = {
    EvidenceSourceType.README_MARKDOWN,
    EvidenceSourceType.MANUAL_PROJECT_DESCRIPTION,
    EvidenceSourceType.GITHUB_README_EXPORT,
    EvidenceSourceType.GITHUB_REPOSITORY_SYNC,
}

SKILL_CATEGORIES: dict[str, SkillCategory] = {
    "python": SkillCategory.LANGUAGE,
    "java": SkillCategory.LANGUAGE,
    "c": SkillCategory.LANGUAGE,
    "typescript": SkillCategory.LANGUAGE,
    "javascript": SkillCategory.LANGUAGE,
    "c++": SkillCategory.LANGUAGE,
    "go": SkillCategory.LANGUAGE,
    "react": SkillCategory.FRONTEND,
    "node.js": SkillCategory.BACKEND,
    "fastapi": SkillCategory.BACKEND,
    "postgresql": SkillCategory.DATABASE,
    "sql server": SkillCategory.DATABASE,
    "sql": SkillCategory.DATA,
    "spark": SkillCategory.DATA,
    "pyspark": SkillCategory.DATA,
    "airflow": SkillCategory.DATA,
    "kafka": SkillCategory.DATA,
    "aws": SkillCategory.CLOUD,
    "azure": SkillCategory.CLOUD,
    "gcp": SkillCategory.CLOUD,
    "dynamodb": SkillCategory.DATABASE,
    "docker": SkillCategory.DEVOPS,
    "kubernetes": SkillCategory.CLOUD,
    "terraform": SkillCategory.CLOUD,
    "linux": SkillCategory.DEVOPS,
    "bash": SkillCategory.LANGUAGE,
    "rest": SkillCategory.BACKEND,
    "graphql": SkillCategory.BACKEND,
    "tcp/ip": SkillCategory.NETWORK,
    "bgp": SkillCategory.NETWORK,
    "criu": SkillCategory.TOOLING,
    "5g": SkillCategory.NETWORK,
    "mqtt": SkillCategory.EMBEDDED,
    "pandas": SkillCategory.DATA,
    "tensorflow": SkillCategory.ML_AI,
    "keras": SkillCategory.ML_AI,
    "pytorch": SkillCategory.ML_AI,
    "llm": SkillCategory.ML_AI,
    "rust": SkillCategory.LANGUAGE,
    "ruby": SkillCategory.LANGUAGE,
    "php": SkillCategory.LANGUAGE,
    "swift": SkillCategory.LANGUAGE,
    "kotlin": SkillCategory.LANGUAGE,
    "scala": SkillCategory.LANGUAGE,
    "c#": SkillCategory.LANGUAGE,
    ".net": SkillCategory.BACKEND,
    "perl": SkillCategory.LANGUAGE,
    "elixir": SkillCategory.LANGUAGE,
    "matlab": SkillCategory.TOOLING,
    "django": SkillCategory.BACKEND,
    "flask": SkillCategory.BACKEND,
    "spring": SkillCategory.BACKEND,
    "spring boot": SkillCategory.BACKEND,
    "express": SkillCategory.BACKEND,
    "ruby on rails": SkillCategory.BACKEND,
    "asp.net": SkillCategory.BACKEND,
    "vue": SkillCategory.FRONTEND,
    "angular": SkillCategory.FRONTEND,
    "next.js": SkillCategory.FRONTEND,
    "svelte": SkillCategory.FRONTEND,
    "redux": SkillCategory.FRONTEND,
    "mongodb": SkillCategory.DATABASE,
    "redis": SkillCategory.DATABASE,
    "cassandra": SkillCategory.DATABASE,
    "elasticsearch": SkillCategory.DATABASE,
    "mysql": SkillCategory.DATABASE,
    "sqlite": SkillCategory.DATABASE,
    "oracle": SkillCategory.DATABASE,
    "neo4j": SkillCategory.DATABASE,
    "clickhouse": SkillCategory.DATABASE,
    "cockroachdb": SkillCategory.DATABASE,
    "helm": SkillCategory.DEVOPS,
    "ansible": SkillCategory.DEVOPS,
    "jenkins": SkillCategory.DEVOPS,
    "github actions": SkillCategory.DEVOPS,
    "gitlab ci": SkillCategory.DEVOPS,
    "circleci": SkillCategory.DEVOPS,
    "prometheus": SkillCategory.DEVOPS,
    "grafana": SkillCategory.DEVOPS,
    "istio": SkillCategory.CLOUD,
    "nginx": SkillCategory.DEVOPS,
    "cloudformation": SkillCategory.CLOUD,
    "pulumi": SkillCategory.CLOUD,
    "rabbitmq": SkillCategory.BACKEND,
    "sqs": SkillCategory.CLOUD,
    "sns": SkillCategory.CLOUD,
    "grpc": SkillCategory.BACKEND,
    "websocket": SkillCategory.BACKEND,
    "scikit-learn": SkillCategory.ML_AI,
    "numpy": SkillCategory.DATA,
    "opencv": SkillCategory.ML_AI,
    "hugging face": SkillCategory.ML_AI,
    "jax": SkillCategory.ML_AI,
    "xgboost": SkillCategory.ML_AI,
    "nlp": SkillCategory.ML_AI,
    "computer vision": SkillCategory.ML_AI,
    "mlflow": SkillCategory.ML_AI,
    "dbt": SkillCategory.DATA,
    "snowflake": SkillCategory.DATA,
    "bigquery": SkillCategory.DATA,
    "redshift": SkillCategory.DATA,
    "hadoop": SkillCategory.DATA,
    "hive": SkillCategory.DATA,
    "pytest": SkillCategory.TOOLING,
    "junit": SkillCategory.TOOLING,
    "qdrant": SkillCategory.DATABASE,
    "pydantic": SkillCategory.BACKEND,
    "polars": SkillCategory.DATA,
    "playwright": SkillCategory.TOOLING,
    "alembic": SkillCategory.DATABASE,
    "arm": SkillCategory.EMBEDDED,
    "stm32": SkillCategory.EMBEDDED,
    "i2c": SkillCategory.EMBEDDED,
    "spi": SkillCategory.EMBEDDED,
    "selenium": SkillCategory.TOOLING,
    "cypress": SkillCategory.TOOLING,
    "jest": SkillCategory.TOOLING,
    "react native": SkillCategory.FRONTEND,
    "flutter": SkillCategory.FRONTEND,
    "git": SkillCategory.TOOLING,
    "jira": SkillCategory.TOOLING,
    "ci/cd": SkillCategory.DEVOPS,
    "simulink": SkillCategory.TOOLING,
    "labview": SkillCategory.TOOLING,
    "can bus": SkillCategory.EMBEDDED,
}


CONTACT_FIELDS = ("email", "phone", "location", "linkedin_url", "github_url")


def set_contact_details(
    store_root: Path | str,
    email: str | None = None,
    phone: str | None = None,
    location: str | None = None,
    linkedin_url: str | None = None,
    github_url: str | None = None,
) -> Path:
    """Persist candidate contact/identity fields, never fabricated.

    Stored separately from the manifest-driven evidence sources (these are direct
    candidate-supplied facts, not extracted evidence) so they survive every
    `build_candidate_profile` rebuild. Only fields explicitly passed (non-None)
    are written; anything not supplied stays whatever it already was (or unset).
    """
    root = Path(store_root)
    path = _contact_path(root)
    existing = read_contact_details(root)
    updates = {"email": email, "phone": phone, "location": location, "linkedin_url": linkedin_url, "github_url": github_url}
    for key, value in updates.items():
        if value is not None:
            existing[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_contact_details(store_root: Path | str) -> dict[str, str]:
    path = _contact_path(Path(store_root))
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {key: payload[key] for key in CONTACT_FIELDS if payload.get(key)}


def _contact_path(root: Path) -> Path:
    return root / "profile" / "contact.json"


def add_profile_source(store_root: Path | str, path: Path | str, source_type: EvidenceSourceType, title: str | None = None) -> Path:
    root = Path(store_root)
    manifest = _manifest_path(root)
    entries = _read_manifest(manifest)
    source_path = Path(path).expanduser().resolve()
    entry = {
        "path": str(source_path),
        "source_type": source_type.value,
        "title": title or source_path.stem,
    }
    if entry not in entries:
        entries.append(entry)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def build_candidate_profile(store_root: Path | str, name: str = "Personal Candidate", graduation_year: int = 2026) -> CandidateProfile:
    root = Path(store_root)
    entries = _read_manifest(_manifest_path(root))
    promoted = [entry for entry in entries if _is_promoted_source_entry(entry)]
    if promoted:
        return _refresh_promoted_profile(root, entries, promoted, name, graduation_year)
    profile = CandidateProfile(id="personal", name=name, graduation_year=graduation_year)
    _apply_contact_details(profile, read_contact_details(root))
    repo_title_to_source_id: dict[str, str] = {}
    for index, entry in enumerate(entries, start=1):
        source_type = SOURCE_TYPE_BY_LABEL[entry["source_type"]]
        path = Path(entry["path"])
        text = _read_text(path)
        source = EvidenceSource(id=f"personal-source-{index}", source_type=source_type, title=entry["title"], uri=str(path))
        _attach_structural_source(profile, source, text)
        project_id = source.id if source_type in PROJECT_SOURCE_TYPES else None
        if source_type == EvidenceSourceType.GITHUB_REPOSITORY_SYNC:
            repo_title_to_source_id[entry["title"]] = source.id
        for capability in extract_capabilities(text, source, project_id=project_id):
            profile.upsert_capability(capability)
    for capability in capabilities_from_github_languages(root, repo_title_to_source_id):
        profile.upsert_capability(capability)
    _write_profile(root, profile)
    return profile


def _is_promoted_source_entry(entry: dict[str, str]) -> bool:
    # `promote_staged_evidence` rewrites sources.json as portable EvidenceSource
    # records ({id, source_type, title, uri}); raw `profile-import` entries carry a
    # local `path` instead. Promoted uris are relative (cv/...) or GitHub URLs, so
    # they cannot be re-read as local files.
    return "path" not in entry and "id" in entry


def _refresh_promoted_profile(
    root: Path,
    entries: list[dict[str, str]],
    promoted: list[dict[str, str]],
    name: str,
    graduation_year: int,
) -> CandidateProfile:
    """Rebuild a store whose Evidence Bank was curated and promoted (Phase 3.6).

    The canonical candidate_profile.json is then the curated truth: re-extracting
    from raw files would silently replace curated projects/evidence with keyword
    hits. So the rebuild keeps every capability, evidence item, project and source
    as-is and only re-applies identity and contact fields.
    """
    raw = [entry for entry in entries if entry not in promoted]
    if raw:
        titles = ", ".join(entry.get("title", "?") for entry in raw)
        raise ValueError(
            f"sources.json mixes promoted curated sources with raw imports ({titles}); "
            "curated evidence would be overwritten by a raw rebuild. Stage and promote the new sources via evidence enrichment instead."
        )
    profile = load_candidate_profile(root)
    if profile is None:
        raise ValueError(f"sources.json holds promoted curated sources but {root / 'profile' / 'candidate_profile.json'} is missing.")
    profile.name = name
    profile.graduation_year = graduation_year
    # Fields absent from contact.json keep the value already in the curated profile.
    _apply_contact_details(profile, {**_profile_contact(profile), **read_contact_details(root)})
    _write_profile(root, profile)
    return profile


def _profile_contact(profile: CandidateProfile) -> dict[str, str]:
    values = {key: getattr(profile, key) for key in CONTACT_FIELDS}
    return {key: value for key, value in values.items() if value}


def _apply_contact_details(profile: CandidateProfile, contact: dict[str, str]) -> None:
    profile.email = contact.get("email")
    profile.phone = contact.get("phone")
    profile.location = contact.get("location")
    profile.linkedin_url = contact.get("linkedin_url")
    profile.github_url = contact.get("github_url")


def capabilities_from_github_languages(store_root: Path | str, repo_title_to_source_id: dict[str, str]) -> list[Capability]:
    """Capabilities discovered from GitHub's own per-repo language breakdown.

    This is the "beyond the fixed keyword list" capability-discovery path: GitHub's
    `languages` API is structured, verified evidence (GitHub's own static analysis
    of the repo) that a language was actually used, so a language becomes a
    candidate capability even if its name never appears in `SKILL_PATTERNS` and
    without any source-code change. It never becomes a "verified" capability
    without evidence, though -- each one still carries a `CapabilityEvidence` with
    real provenance (repo, byte-share, confidence derived from that share), the
    same as every other capability in the Evidence Bank. Languages below
    `MIN_LANGUAGE_SHARE` are dropped as noise (e.g. a handful of config-file bytes).
    """
    root = Path(store_root)
    path = _languages_cache_path(root)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    capabilities: list[Capability] = []
    for full_name, languages in payload.items():
        project_id = repo_title_to_source_id.get(full_name)
        total = sum(languages.values()) or 1
        for language, byte_count in languages.items():
            share = byte_count / total
            if share < MIN_LANGUAGE_SHARE:
                continue
            source = EvidenceSource(id=f"github-languages:{full_name}", source_type=EvidenceSourceType.GITHUB_LANGUAGES_API, title=full_name)
            capabilities.append(
                Capability(
                    name=language,
                    category=SKILL_CATEGORIES.get(language.casefold(), SkillCategory.TOOLING),
                    evidence=[
                        CapabilityEvidence(
                            source=source,
                            quote=f"GitHub language breakdown for {full_name}: {language} is {share:.0%} of the repository by bytes.",
                            project_id=project_id,
                            confidence=_confidence_for_language_share(share),
                            id=f"{full_name}:languages:{language.casefold()}",
                        )
                    ],
                    technology=language,
                    last_verified=date.today(),
                )
            )
    return capabilities


MIN_LANGUAGE_SHARE = 0.03


def _confidence_for_language_share(share: float) -> float:
    if share >= 0.3:
        return 1.0
    if share >= 0.1:
        return 0.7
    return 0.4


def _languages_cache_path(root: Path) -> Path:
    return root / "profile" / "github_languages.json"


def load_candidate_profile(store_root: Path | str) -> CandidateProfile | None:
    path = Path(store_root) / "profile" / "candidate_profile.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    profile = CandidateProfile(
        id=payload["id"],
        name=payload["name"],
        graduation_year=payload["graduation_year"],
        email=payload.get("email"),
        phone=payload.get("phone"),
        location=payload.get("location"),
        linkedin_url=payload.get("linkedin_url"),
        github_url=payload.get("github_url"),
    )
    sources: dict[str, EvidenceSource] = {}
    for item in payload.get("evidence_sources", []):
        sources[item["id"]] = EvidenceSource(
            id=item["id"],
            source_type=EvidenceSourceType(item["source_type"]),
            title=item["title"],
            uri=item.get("uri"),
        )
    for item in payload.get("capabilities", []):
        evidence = [
            CapabilityEvidence(
                source=sources[evidence_item["source_id"]],
                quote=evidence_item["quote"],
                project_id=evidence_item.get("project_id") or (sources[evidence_item["source_id"]].id if sources[evidence_item["source_id"]].source_type in PROJECT_SOURCE_TYPES else None),
                confidence=evidence_item.get("confidence", 1.0),
                id=evidence_item.get("id") or f"{evidence_item['source_id']}:{item['name'].casefold()}",
                category=evidence_item.get("category"),
                claim_type=evidence_item.get("claim_type"),
                source_file=evidence_item.get("source_file"),
                source_location=evidence_item.get("source_location"),
                confidence_tier=evidence_item.get("confidence_tier"),
                safe_paraphrase_scope=evidence_item.get("safe_paraphrase_scope"),
                forbidden_extrapolations=tuple(evidence_item.get("forbidden_extrapolations", ())),
                freshness=evidence_item.get("freshness"),
            )
            for evidence_item in item.get("evidence", [])
        ]
        profile.upsert_capability(
            Capability(
                name=item["name"],
                category=SkillCategory(item["category"]),
                evidence=evidence,
                role_relevance={RoleTrack(key): value for key, value in item.get("role_relevance", {}).items()},
                technology=item.get("technology"),
                domain=item.get("domain"),
                last_verified=date.fromisoformat(item["last_verified"]) if item.get("last_verified") else None,
            )
        )
    for item in payload.get("cv_versions", []):
        profile.cv_versions.append(CVVersion(id=item["id"], category=item["category"], text=item["text"], evidence_source=sources[item["source_id"]]))
    for item in payload.get("projects", []):
        profile.projects.append(
            Project(
                id=item["id"],
                name=item["name"],
                description=item["description"],
                evidence_sources=[sources[source_id] for source_id in item.get("evidence_source_ids", []) if source_id in sources],
                repository_full_name=item.get("repository_full_name"),
                role_relevance={RoleTrack(key): value for key, value in item.get("role_relevance", {}).items()},
                aliases=item.get("aliases", []),
                forbidden_extrapolations=item.get("forbidden_extrapolations", []),
                track_mappings=item.get("track_mappings", []),
            )
        )
    return profile


def extract_capabilities(text: str, source: EvidenceSource, project_id: str | None = None) -> list[Capability]:
    clean = strip_html(text)
    profile = infer_role_track_profile(source.title, clean)
    relevance = {score.track: score.score for score in profile.scores}
    capabilities: list[Capability] = []
    for skill in SKILL_PATTERNS:
        if skill == "C" and source.source_type not in {EvidenceSourceType.CV_TEXT, EvidenceSourceType.CV_PDF_EXTRACTED_TEXT}:
            continue
        pattern = _skill_pattern(skill)
        if not pattern.search(clean):
            continue
        quote = _quote_for(clean, pattern)
        if _is_negative_capability_evidence(quote):
            continue
        capabilities.append(
            Capability(
                name=skill,
                category=SKILL_CATEGORIES.get(skill.casefold(), SkillCategory.TOOLING),
                evidence=[
                    CapabilityEvidence(
                        source=source,
                        quote=quote,
                        project_id=project_id,
                        confidence=_confidence_for_source(source.source_type),
                        id=f"{source.id}:{skill.casefold()}",
                    )
                ],
                role_relevance=relevance,
                technology=skill,
                last_verified=source.collected_at or date.today(),
            )
        )
    return capabilities


def _attach_structural_source(profile: CandidateProfile, source: EvidenceSource, text: str) -> None:
    if source.source_type in {
        EvidenceSourceType.README_MARKDOWN,
        EvidenceSourceType.MANUAL_PROJECT_DESCRIPTION,
        EvidenceSourceType.GITHUB_README_EXPORT,
        EvidenceSourceType.GITHUB_REPOSITORY_SYNC,
    }:
        repository_full_name = source.title if source.source_type == EvidenceSourceType.GITHUB_REPOSITORY_SYNC else None
        clean_text = strip_html(text)
        clean_description = re.sub(r"^#+\s*", "", clean_text.lstrip())[:1000]
        role_relevance = {score.track: score.score for score in infer_role_track_profile(source.title, clean_text).scores}
        profile.projects.append(
            Project(
                id=source.id,
                name=source.title,
                description=clean_description,
                evidence_sources=[source],
                repository_full_name=repository_full_name,
                role_relevance=role_relevance,
            )
        )
    elif source.source_type in {EvidenceSourceType.CV_TEXT, EvidenceSourceType.CV_PDF_EXTRACTED_TEXT}:
        profile.cv_versions.append(CVVersion(id=source.id, category=_cv_category(source.title), text=text, evidence_source=source))
    elif source.source_type == EvidenceSourceType.EDUCATION_DESCRIPTION:
        profile.education.append(Education(institution=source.title, programme=source.title, graduation_year=profile.graduation_year, description=text, evidence_source=source))
    elif source.source_type == EvidenceSourceType.EXPERIENCE_DESCRIPTION:
        profile.experience.append(Experience(organisation=source.title, title=source.title, description=text, evidence_source=source))


def _write_profile(root: Path, profile: CandidateProfile) -> Path:
    path = root / "profile" / "candidate_profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    sources = {}
    for capability in profile.capabilities.values():
        for evidence in capability.evidence:
            sources[evidence.source.id] = evidence.source
    for cv in profile.cv_versions:
        sources[cv.evidence_source.id] = cv.evidence_source
    for project in profile.projects:
        for source in project.evidence_sources:
            sources[source.id] = source
    payload = {
        "id": profile.id,
        "name": profile.name,
        "graduation_year": profile.graduation_year,
        "email": profile.email,
        "phone": profile.phone,
        "location": profile.location,
        "linkedin_url": profile.linkedin_url,
        "github_url": profile.github_url,
        "evidence_sources": [_source_to_dict(source) for source in sources.values()],
        "capabilities": [
            {
                "name": capability.name,
                "category": capability.category.value,
                "role_relevance": {track.value: score for track, score in capability.role_relevance.items()},
                "technology": capability.technology,
                "domain": capability.domain,
                "last_verified": capability.last_verified.isoformat() if capability.last_verified else None,
                "evidence_tier": capability.evidence_tier,
                "evidence": [
                    {
                        "id": evidence.id,
                        "source_id": evidence.source.id,
                        "quote": evidence.quote,
                        "project_id": evidence.project_id,
                        "confidence": evidence.confidence,
                        "category": evidence.category,
                        "claim_type": evidence.claim_type,
                        "source_file": evidence.source_file,
                        "source_location": evidence.source_location,
                        "confidence_tier": evidence.confidence_tier,
                        "safe_paraphrase_scope": evidence.safe_paraphrase_scope,
                        "forbidden_extrapolations": list(evidence.forbidden_extrapolations),
                        "freshness": evidence.freshness,
                    }
                    for evidence in capability.evidence
                ],
            }
            for capability in profile.capabilities.values()
        ],
        "cv_versions": [
            {"id": cv.id, "category": cv.category, "text": cv.text, "source_id": cv.evidence_source.id}
            for cv in profile.cv_versions
        ],
        "projects": [
            {
                "id": project.id,
                "name": project.name,
                "description": project.description,
                "evidence_source_ids": [source.id for source in project.evidence_sources],
                "repository_full_name": project.repository_full_name,
                "role_relevance": {track.value: score for track, score in project.role_relevance.items()},
                "aliases": project.aliases,
                "forbidden_extrapolations": project.forbidden_extrapolations,
                "track_mappings": project.track_mappings,
            }
            for project in profile.projects
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _source_to_dict(source: EvidenceSource) -> dict[str, str | None]:
    return {"id": source.id, "source_type": source.source_type.value, "title": source.title, "uri": source.uri}


def _manifest_path(root: Path) -> Path:
    return root / "profile" / "sources.json"


def _read_manifest(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(path: Path) -> str:
    if path.suffix.casefold() == ".docx":
        return _read_docx_text(path)
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs)


def _skill_pattern(skill: str) -> re.Pattern[str]:
    if skill == "C":
        return re.compile(r"(?<![\w+])C(?![\w+])", flags=re.IGNORECASE)
    return re.compile(rf"(?<!\w){re.escape(skill)}(?!\w)", flags=re.IGNORECASE)


def _quote_for(text: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(text)
    if match is None:
        return text[:180]
    index = match.start()
    start = text.rfind("\n", 0, index) + 1
    end_candidates = [position for position in (text.find("\n", index), text.find(".", index)) if position != -1]
    end = min(end_candidates) if end_candidates else min(index + 180, len(text))
    return text[start:end].strip()


def _is_negative_capability_evidence(quote: str) -> bool:
    lowered = quote.casefold()
    return any(phrase in lowered for phrase in ("unnecessary", "not used", "not required", "not needed", "out of scope"))


def _confidence_for_source(source_type: EvidenceSourceType) -> float:
    if source_type in {
        EvidenceSourceType.README_MARKDOWN,
        EvidenceSourceType.MANUAL_PROJECT_DESCRIPTION,
        EvidenceSourceType.GITHUB_README_EXPORT,
        EvidenceSourceType.GITHUB_REPOSITORY_SYNC,
    }:
        return 1.0
    if source_type == EvidenceSourceType.EXPERIENCE_DESCRIPTION:
        return 0.9
    if source_type == EvidenceSourceType.EDUCATION_DESCRIPTION:
        return 0.75
    return 0.65


def _cv_category(title: str) -> str:
    lowered = title.casefold()
    if "data" in lowered:
        return "Data"
    if "cloud" in lowered or "platform" in lowered:
        return "Cloud/Platform"
    if "ai" in lowered or "ml" in lowered:
        return "AI/ML"
    if "frontend" in lowered:
        return "Frontend"
    return "Software/Backend"
