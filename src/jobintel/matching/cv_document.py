"""Structured one-page CV document: the stable sections a tailored CV is built from.

A tailored CV keeps the candidate's core CV identity -- header/contact,
professional summary, education, technical skills, key projects, internship
experience -- and tailors *within* those sections (project/bullet selection and
ordering, skill ordering). Sections the Evidence Bank cannot supply are left
absent and reported, never invented.

Summary, education and experience come from the candidate's own authored CV
text (`CandidateProfile.cv_versions`, a CV_TEXT evidence source with provenance)
unless structured `Education`/`Experience` records exist. They are reused
verbatim: only whole lines are kept or dropped, never re-worded or sliced.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jobintel.models.candidate import CandidateProfile, CVVersion
from jobintel.models.taxonomy import SkillCategory

# Layout/format of generated CVs. 1 = the legacy flat layout (header, skills,
# projects only; recorded before this field existed). 2 = the structured
# six-section document. Bump when the rendered CV structure changes.
CV_DOCUMENT_FORMAT = 2

CORE_SECTIONS = ("contact", "summary", "education", "skills", "projects", "experience")

_SECTION_ALIASES: dict[str, set[str]] = {
    "summary": {"professional summary", "summary", "profile", "personal profile", "personal statement", "career summary"},
    "education": {"education", "education & qualifications", "education and qualifications", "academic background"},
    "skills": {"technical skills", "skills", "key skills", "core skills"},
    "projects": {"key projects", "projects", "selected projects", "technical projects", "personal projects"},
    "experience": {
        "internship experience",
        "experience",
        "work experience",
        "professional experience",
        "relevant experience",
        "employment",
        "employment history",
    },
}
_HEADING_TO_SECTION = {alias: section for section, aliases in _SECTION_ALIASES.items() for alias in aliases}

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_DATE = rf"(?:{_MONTH}\s+\d{{4}}|\d{{4}})"
_DATE_RANGE = re.compile(rf"({_DATE})\s*[-–—]+\s*({_DATE}|Present|Current|Now)(\s*\((?:Expected|expected)\))?")
_LIST_MARKER = re.compile(r"^\s*[-•*·▪●]\s*")

# Recruiter-readable skill groups, keyed off the Evidence Bank's own
# SkillCategory -- never a hardcoded skill list.
SKILL_GROUPS: tuple[tuple[str, frozenset[SkillCategory]], ...] = (
    ("Programming", frozenset({SkillCategory.LANGUAGE})),
    ("Backend & Frontend", frozenset({SkillCategory.BACKEND, SkillCategory.FRAMEWORK, SkillCategory.FRONTEND})),
    ("Databases & Data", frozenset({SkillCategory.DATABASE, SkillCategory.DATA})),
    ("Cloud, DevOps & Tooling", frozenset({SkillCategory.CLOUD, SkillCategory.DEVOPS, SkillCategory.TOOLING, SkillCategory.DOMAIN})),
    ("AI & Machine Learning", frozenset({SkillCategory.ML_AI})),
    ("Embedded & Networking", frozenset({SkillCategory.EMBEDDED, SkillCategory.NETWORK})),
)


@dataclass
class DatedEntry:
    """An education or experience entry: heading, date range, and detail lines
    (modules for education, bullets for experience), all verbatim."""

    heading: str
    dates: str = ""
    details: list[str] = field(default_factory=list)


@dataclass
class SkillGroup:
    label: str
    skills: list[str]


@dataclass
class ProjectBlock:
    project_id: str
    name: str
    technologies: list[str]
    bullets: list[Any]  # matching.cv_generation._BulletCandidate (anything with `.text`)
    # "focus" projects best demonstrate the JD's core requirements and may take
    # more page space than "secondary" ones.
    emphasis: str = "secondary"
    # Project-selection score (JD relevance + evidence strength) for this job.
    selection_score: float = 0.0


@dataclass
class CVDocument:
    name: str
    headline: str
    contact_line: str
    summary: str | None
    education: list[DatedEntry]
    skill_groups: list[SkillGroup]
    projects: list[ProjectBlock]
    experience: list[DatedEntry]
    base_cv_source_id: str | None = None

    def present_sections(self) -> list[str]:
        present = {
            "contact": bool(self.contact_line),
            "summary": bool(self.summary),
            "education": bool(self.education),
            "skills": any(group.skills for group in self.skill_groups),
            "projects": any(project.bullets for project in self.projects),
            "experience": bool(self.experience),
        }
        return [section for section in CORE_SECTIONS if present[section]]

    def missing_sections(self) -> list[str]:
        present = set(self.present_sections())
        return [section for section in CORE_SECTIONS if section not in present]

    def skills(self) -> list[str]:
        return [skill for group in self.skill_groups for skill in group.skills]


@dataclass
class AuthoredCV:
    headline: str
    summary: str | None
    education: list[DatedEntry]
    experience: list[DatedEntry]

    def core_section_count(self) -> int:
        return sum(bool(part) for part in (self.summary, self.education, self.experience))


def parse_authored_cv(text: str) -> AuthoredCV:
    """Split candidate-authored CV text into its stable sections, verbatim."""
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    headline = ""
    if lines and "|" in lines[0] and _section_for(lines[0]) is None:
        headline = lines[0].split("|", 1)[1].strip()

    buckets: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        section = _section_for(line)
        if section is not None:
            current = section
            buckets.setdefault(section, [])
            continue
        if current is not None:
            buckets[current].append(line)

    summary_lines = buckets.get("summary", [])
    return AuthoredCV(
        headline=headline,
        summary=" ".join(summary_lines) or None,
        education=_dated_entries(buckets.get("education", [])),
        experience=_dated_entries(buckets.get("experience", [])),
    )


def select_base_cv(candidate: CandidateProfile, jd_terms: set[str]) -> tuple[CVVersion, AuthoredCV] | None:
    """Pick the candidate-authored CV version to take stable sections from: the
    one with the most core sections, then the most JD-term overlap."""
    best: tuple[tuple[int, int], CVVersion, AuthoredCV] | None = None
    for version in candidate.cv_versions:
        parsed = parse_authored_cv(version.text)
        if not parsed.core_section_count():
            continue
        lowered = version.text.casefold()
        key = (parsed.core_section_count(), sum(1 for term in jd_terms if term and term in lowered))
        if best is None or key > best[0]:
            best = (key, version, parsed)
    return (best[1], best[2]) if best else None


def _merge_sparse_groups(groups: list[SkillGroup]) -> list[SkillGroup]:
    """Merge sparse (single-item) skill groups into compatible parent groups
    to eliminate wasted vertical lines while preserving skill order and truthfulness."""
    if len(groups) <= 1:
        return groups

    merged = [SkillGroup(label=g.label, skills=list(g.skills)) for g in groups]

    # Rule 1: Sparse Embedded & Networking (<= 1 skill) merges into Cloud, DevOps & Tooling
    emb = next((g for g in merged if g.label == "Embedded & Networking"), None)
    if emb and len(emb.skills) <= 1:
        cloud = next((g for g in merged if g.label == "Cloud, DevOps & Tooling"), None)
        if cloud:
            cloud.skills.extend(emb.skills)
            cloud.label = "Cloud, Systems & DevOps"
            merged.remove(emb)

    # Rule 2: Sparse Databases & Data (<= 1 skill) merges into Backend & Frontend if > 4 groups
    if len(merged) > 4:
        db = next((g for g in merged if g.label == "Databases & Data"), None)
        if db and len(db.skills) <= 1:
            backend = next((g for g in merged if g.label == "Backend & Frontend"), None)
            if backend:
                backend.skills.extend(db.skills)
                backend.label = "Backend, Data & Frontend"
                merged.remove(db)

    return merged


def group_skills(ordered_skills: list[str], categories: dict[str, SkillCategory]) -> list[SkillGroup]:
    """Group skills into recruiter-readable categories; groups are ordered by
    their most relevant member, skills keep their relevance order.
    Sparse compatible groups are merged to preserve page budget."""
    groups: dict[str, SkillGroup] = {}
    for skill in ordered_skills:
        category = categories.get(skill.casefold(), SkillCategory.TOOLING)
        label = next((label for label, members in SKILL_GROUPS if category in members), "Cloud, DevOps & Tooling")
        groups.setdefault(label, SkillGroup(label=label, skills=[])).skills.append(skill)
    return _merge_sparse_groups(list(groups.values()))


def document_to_text(doc: CVDocument) -> str:
    lines = [f"{doc.name} | {doc.headline}" if doc.headline else doc.name]
    if doc.contact_line:
        lines.append(doc.contact_line)
    if doc.summary:
        lines += ["", "PROFESSIONAL SUMMARY", doc.summary]
    if doc.education:
        lines += ["", "EDUCATION"]
        for entry in doc.education:
            lines.append(_entry_heading(entry))
            lines.extend(entry.details)
    if doc.skill_groups:
        lines += ["", "TECHNICAL SKILLS"]
        lines.extend(f"{group.label}: {', '.join(group.skills)}" for group in doc.skill_groups if group.skills)
    if doc.projects:
        lines += ["", "KEY PROJECTS"]
        for project in doc.projects:
            lines.append(project_heading(project))
            lines.extend(f"  - {bullet.text}" for bullet in project.bullets)
    if doc.experience:
        lines += ["", "INTERNSHIP EXPERIENCE"]
        for entry in doc.experience:
            lines.append(_entry_heading(entry))
            lines.extend(f"  - {detail}" for detail in entry.details)
    return "\n".join(lines)


def document_to_dict(doc: CVDocument) -> dict[str, Any]:
    """Serialisable record of the rendered CV structure, stored on the artifact."""
    return {
        "name": doc.name,
        "headline": doc.headline,
        "contact_line": doc.contact_line,
        "base_cv_source_id": doc.base_cv_source_id,
        "summary": doc.summary,
        "education": [_entry_dict(entry) for entry in doc.education],
        "skill_groups": [{"label": group.label, "skills": list(group.skills)} for group in doc.skill_groups],
        "projects": [
            {
                "project_id": project.project_id,
                "name": project.name,
                "emphasis": project.emphasis,
                "technologies": list(project.technologies),
                "bullets": [bullet.text for bullet in project.bullets],
            }
            for project in doc.projects
        ],
        "experience": [_entry_dict(entry) for entry in doc.experience],
        "sections_present": doc.present_sections(),
        "sections_missing": doc.missing_sections(),
    }


def project_heading(project: ProjectBlock) -> str:
    return f"{project.name} | {', '.join(project.technologies)}" if project.technologies else project.name


def clean_list_marker(text: str) -> str:
    return _LIST_MARKER.sub("", " ".join((text or "").split()))


def _entry_heading(entry: DatedEntry) -> str:
    return f"{entry.heading} — {entry.dates}" if entry.dates else entry.heading


def _entry_dict(entry: DatedEntry) -> dict[str, Any]:
    return {"heading": entry.heading, "dates": entry.dates, "details": list(entry.details)}


def _section_for(line: str) -> str | None:
    return _HEADING_TO_SECTION.get(line.strip().rstrip(":").casefold())


def _dated_entries(lines: list[str]) -> list[DatedEntry]:
    entries: list[DatedEntry] = []
    for line in lines:
        match = _DATE_RANGE.search(line)
        if match:
            heading = line[: match.start()].strip(" \t—–-|,")
            dates = f"{match.group(1)} – {match.group(2)}" + (f" {match.group(3).strip()}" if match.group(3) else "")
            if heading:
                entries.append(DatedEntry(heading=heading, dates=dates))
                continue
        if entries:
            detail = clean_list_marker(line)
            if detail:
                entries[-1].details.append(detail)
    return entries
