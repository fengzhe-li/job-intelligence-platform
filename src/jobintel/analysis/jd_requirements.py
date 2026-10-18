from __future__ import annotations

import re

from dataclasses import dataclass, field

from jobintel.analysis.job_quality import (
    extract_required_experience,
    extract_seniority,
    infer_engineering_domain,
    infer_role_function,
)
from jobintel.analysis.role_tracks import extract_requirement_groups
from jobintel.models.job import Job
from jobintel.models.taxonomy import RoleTrack

# Aggregates the JD-analysis extractors that already exist across analysis/*
# (skill requirements, seniority, experience, role function/domain, sponsorship,
# graduation-year/intake, eligibility, role tracks -- all computed during
# `enrich_job`) into one structured object per job, per the JD -> requirement
# extraction step of the personalised application pipeline. Deliberately does not
# duplicate any extraction logic; this is a read-only view over an already-enriched
# Job plus two additional deterministic extractors (seniority/experience/role
# function/domain) that `enrich_job` doesn't persist onto the Job model itself.


@dataclass(frozen=True)
class JDRequirements:
    job_id: str
    must_have_skills: list[str]
    preferred_skills: list[str]
    role_family: str
    engineering_domain: str
    seniority: str
    required_experience_years: float | None
    education_requirements: list[str]
    other_eligibility: list[str]
    location_summary: str
    sponsorship_state: str
    graduation_year_state: str
    # Informational only, never a filtering/ranking input -- see
    # analysis.evidence.detect_graduation_year. A job's intake year differing from
    # any particular year is not evidence of ineligibility.
    detected_intake_year: int | None
    primary_role_track: RoleTrack | None
    keywords: list[str] = field(default_factory=list)
    # Required skills the JD accepts as alternatives (role_tracks.extract_requirement_groups): satisfying
    # ONE member satisfies the group. Members stay in must_have_skills for relevance ranking.
    required_alternatives: list[dict] = field(default_factory=list)


_STACK_DESCRIPTION = re.compile(r"\bwe (?:mostly |primarily |mainly )?(?:work|code|build|write)\s+(?:mostly |primarily |mainly )?(?:in|with)\b|"
                                r"\bour (?:tech(?:nology)? )?stack\b|\bwe (?:mostly |primarily |mainly )?use\b", re.I)
_LEARNABLE_WAIVER = re.compile(r"(?:languages?|technolog\w+|tools?|stack)\s+can be (?:learned|learnt|picked up)|"
                               r"(?:don[\u2019']?t|do not) (?:need|require) (?:prior )?experience (?:in|with) (?:our|these|any specific)|"
                               r"no prior experience (?:in|with) (?:our|these|any specific)", re.I)


def learnable_stack_skills(description: str, requirements) -> set[str]:
    """Required-looking skills that only appear in a description of the employer's own stack ("We work mostly
    in Java, Ruby, Scala and Go") right beside an explicit waiver ("new programming languages can be
    learned"): the JD states they are NOT prerequisites in that section. Narrow on
    purpose -- without the explicit waiver nearby, every skill keeps its extracted status (fail closed)."""
    text = re.sub(r"<[^>]+>", " ", description or "")
    waived = set()
    for item in requirements:
        if not item.required:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if re.search(rf"(?<![\w+#]){re.escape(item.name)}(?![\w+#])", sentence) and _STACK_DESCRIPTION.search(sentence):
                at = text.find(sentence)
                if _LEARNABLE_WAIVER.search(text[max(0, at - 300): at + len(sentence) + 300]):
                    waived.add(item.name)
                    break
    return waived


def resolve_required_skills(jd: JDRequirements, evidence_tier) -> tuple[list[str], list[str], list[str]]:
    """(matched, missing, partial) for the review gate. `evidence_tier(name)` is "strong" / "partial" /
    None. An individually mandatory skill counts on its own; an alternatives group counts ONCE: matched by
    a strong member, partial if only partial members, otherwise one missing entry naming the group."""
    grouped = {name.casefold() for group in jd.required_alternatives for name in group["members"]}
    matched, missing, partial = [], [], []
    for name in jd.must_have_skills:
        if name.casefold() in grouped:
            continue
        tier = evidence_tier(name)
        if tier is None:
            missing.append(name)
        else:
            matched.append(name)
            if tier == "partial":
                partial.append(name)
    for group in jd.required_alternatives:
        tiers = {name: evidence_tier(name) for name in group["members"]}
        strong = [n for n, t in tiers.items() if t == "strong"]
        weak = [n for n, t in tiers.items() if t == "partial"]
        label = "one of " + " / ".join(group["members"])
        if strong:
            matched.append(f"{strong[0]} ({label})")
        elif weak:
            matched.append(f"{weak[0]} ({label})")
            partial.append(label)
        else:
            missing.append(label)
    return matched, missing, partial


def extract_jd_requirements(job: Job) -> JDRequirements:
    seniority = extract_seniority(job.title, job.description)
    experience = extract_required_experience(job.title, job.description)
    role_function = infer_role_function(job.title, job.description)
    engineering_domain = infer_engineering_domain(job.title, job.description)

    waived = learnable_stack_skills(job.description or "", job.skill_requirements)
    must_have = [item.name for item in job.skill_requirements if item.required and item.name not in waived]
    preferred = [item.name for item in job.skill_requirements if not item.required or item.name in waived]

    education_requirements = [item.evidence_text for item in job.eligibility if "degree" in item.label.casefold()]
    other_eligibility = [item.evidence_text for item in job.eligibility if "degree" not in item.label.casefold()]

    location_summary = "; ".join(location.raw or f"{location.city or ''}, {location.country}".strip(", ") for location in job.locations) or job.raw_location or "Unspecified"

    primary = job.role_track_profile.primary()
    keywords = sorted({*must_have, *preferred, *(evidence for score in job.role_track_profile.scores for evidence in score.evidence)})

    return JDRequirements(
        job_id=job.id,
        must_have_skills=must_have,
        preferred_skills=preferred,
        role_family=role_function.function,
        engineering_domain=engineering_domain.domain,
        seniority=seniority.level,
        required_experience_years=experience.years_required,
        education_requirements=education_requirements,
        other_eligibility=other_eligibility,
        location_summary=location_summary,
        sponsorship_state=job.sponsorship.state.value if job.sponsorship else "unknown",
        graduation_year_state=job.graduation_year.state.value if job.graduation_year else "unknown",
        required_alternatives=extract_requirement_groups(job.description or "", job.skill_requirements),
        detected_intake_year=job.graduation_year.intake_year if job.graduation_year else None,
        primary_role_track=primary.track if primary else None,
        keywords=list(keywords),
    )
