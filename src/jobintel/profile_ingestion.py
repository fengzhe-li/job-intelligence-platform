from __future__ import annotations

import json
import re
import zipfile
from dataclasses import asdict
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
}


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
    profile = CandidateProfile(id="personal", name=name, graduation_year=graduation_year)
    for index, entry in enumerate(_read_manifest(_manifest_path(root)), start=1):
        source_type = SOURCE_TYPE_BY_LABEL[entry["source_type"]]
        path = Path(entry["path"])
        text = _read_text(path)
        source = EvidenceSource(id=f"personal-source-{index}", source_type=source_type, title=entry["title"], uri=str(path))
        _attach_structural_source(profile, source, text)
        for capability in extract_capabilities(text, source):
            profile.upsert_capability(capability)
    _write_profile(root, profile)
    return profile


def load_candidate_profile(store_root: Path | str) -> CandidateProfile | None:
    path = Path(store_root) / "profile" / "candidate_profile.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    profile = CandidateProfile(id=payload["id"], name=payload["name"], graduation_year=payload["graduation_year"])
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
                project_id=evidence_item.get("project_id"),
                confidence=evidence_item.get("confidence", 1.0),
            )
            for evidence_item in item.get("evidence", [])
        ]
        profile.upsert_capability(
            Capability(
                name=item["name"],
                category=SkillCategory(item["category"]),
                evidence=evidence,
                role_relevance={RoleTrack(key): value for key, value in item.get("role_relevance", {}).items()},
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
            )
        )
    return profile


def extract_capabilities(text: str, source: EvidenceSource) -> list[Capability]:
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
                evidence=[CapabilityEvidence(source=source, quote=quote, confidence=_confidence_for_source(source.source_type))],
                role_relevance=relevance,
            )
        )
    return capabilities


def _attach_structural_source(profile: CandidateProfile, source: EvidenceSource, text: str) -> None:
    if source.source_type in {EvidenceSourceType.README_MARKDOWN, EvidenceSourceType.MANUAL_PROJECT_DESCRIPTION, EvidenceSourceType.GITHUB_README_EXPORT}:
        profile.projects.append(Project(id=source.id, name=source.title, description=text[:1000], evidence_sources=[source]))
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
        "evidence_sources": [_source_to_dict(source) for source in sources.values()],
        "capabilities": [
            {
                "name": capability.name,
                "category": capability.category.value,
                "role_relevance": {track.value: score for track, score in capability.role_relevance.items()},
                "evidence": [
                    {
                        "source_id": evidence.source.id,
                        "quote": evidence.quote,
                        "project_id": evidence.project_id,
                        "confidence": evidence.confidence,
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
    if source_type in {EvidenceSourceType.README_MARKDOWN, EvidenceSourceType.MANUAL_PROJECT_DESCRIPTION, EvidenceSourceType.GITHUB_README_EXPORT}:
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
