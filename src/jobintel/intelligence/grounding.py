"""Fail-closed grounding. Semantic propositions outside the audited catalog need review.

IDs alone are not proof: both capability identity and the actual linked quotation
must support a relation. Free-form model explanations are never trusted claims.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from jobintel.models.candidate import CandidateProfile, Capability, CapabilityEvidence, Project
from jobintel.matching.claim_grounding import validate_bullet_claim

TYPES = ("role_family", "seniority", "early_career", "eligibility", "skill", "language",
         "framework", "cloud_platform", "systems_networking_embedded", "ai_ml",
         "domain", "responsibility", "experience")
MODALITIES = ("required", "preferred", "unclear")
# Directional relationships, not transitive aliases. Broad claims are at most partial.
RELATIONS = {
    "rest api": {"fastapi": "partial", "rest": "strong"},
    "cloud": {"aws": "partial", "azure": "partial", "gcp": "partial"},
    "serverless": {"aws lambda": "partial"},
    "retrieval": {"rag": "partial", "vector search": "partial"},
    "vector search": {"rag": "partial"},
    "session migration": {"criu": "partial"},
    "networking": {"tcp/ip": "partial", "bgp": "partial"},
}
ALIASES = {
    "restful api": "rest api", "rest apis": "rest api", "restful apis": "rest api",
    "cloud computing": "cloud", "cloud platforms": "cloud",
    "retrieval augmented generation": "rag", "retrieval-augmented generation": "rag",
    "amazon web services": "aws", "k8s": "kubernetes",
    "golang": "go", "c sharp": "c#",
}
LEVEL = {"missing": 0, "partial": 1, "strong": 2}


def concept(value: str) -> str:
    value = " ".join(value.casefold().split())
    return ALIASES.get(value, value)


def contains(text: str, phrase: str) -> bool:
    return re.search(r"(?<![\w+#])" + re.escape(phrase) + r"(?![\w+#])", text, re.I) is not None


def modality(text: str) -> str:
    preferred = bool(re.search(r"\b(preferred|desirable|optional|nice to have|bonus|ideally)\b", text, re.I))
    required = bool(re.search(r"\b(required|require|requires|must|essential|mandatory|need)\b", text, re.I))
    return "unclear" if preferred == required else ("preferred" if preferred else "required")


def sentence_context(text: str, start: int, end: int) -> str:
    # Include omitted context to catch clipped 'Python' from 'Python preferred'.
    left = max(text.rfind("\n", 0, start), text.rfind(". ", 0, start), text.rfind(";", 0, start)) + 1
    if text[end - 1] in ".!?;\n":
        return text[left:end]
    stops = [p for token in ("\n", ". ", ";") if (p := text.find(token, end)) >= 0]
    return text[left:min(stops) if stops else len(text)]


@dataclass(frozen=True)
class GroundedRequirement:
    id: str
    normalized: str
    excerpt: str
    start: int
    end: int
    requirement_type: str
    modality: str


def validate_requirements(payload: dict, jd: str) -> tuple[list[GroundedRequirement], list[str]]:
    items = payload.get("requirements")
    if not isinstance(items, list) or len(items) > 80:
        raise ValueError("invalid_schema")
    accepted, failures, seen = [], [], set()
    fields = {"id", "normalized", "excerpt", "start", "end", "requirement_type", "modality"}
    for item in items:
        if not isinstance(item, dict) or set(item) != fields:
            failures.append("requirement_schema"); continue
        rid, label, quote, start, end, kind, importance = (item[k] for k in
            ("id", "normalized", "excerpt", "start", "end", "requirement_type", "modality"))
        if (not all(isinstance(v, str) for v in (rid, label, quote, kind, importance))
                or not re.fullmatch(r"r[0-9]{1,3}", rid) or rid in seen
                or not label.strip() or len(label) > 160 or not quote.strip()
                or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(jd) or jd[start:end] != quote
                or kind not in TYPES or importance not in MODALITIES):
            failures.append("requirement_grounding"); continue
        seen.add(rid)
        # Extractive labels avoid an exact quote being used to launder unrelated meaning.
        if not contains(quote, label):
            failures.append("unsupported_interpretation"); continue
        context = sentence_context(jd, start, end)
        expected = modality(context)
        if importance != expected:
            failures.append("requirement_modality"); continue
        if re.search(r"\b(no|not|without|never)\b", context, re.I):
            failures.append("negated_requirement_review"); continue
        if kind == "eligibility":
            # A year/intake cannot become eligibility. No model-supplied eligible/ineligible flag.
            explicit = re.search(r"\b(must|only|required|require|requires)\b", context, re.I)
            subject = re.search(r"graduat(?:e|es|ing|ion)|degree|right to work|visa|citizen|clearance", quote, re.I)
            graduation = re.search(r"graduat", quote, re.I)
            explicit_graduation = re.search(r"must graduate|graduat(?:e|es|ing)\s+(?:in\s+)?20\d{2}\s+only|20\d{2}\s+graduates?\s+only|class of 20\d{2} only", quote, re.I)
            if not explicit or not subject or expected != "required" or (graduation and not explicit_graduation):
                failures.append("unstated_eligibility"); continue
        accepted.append(GroundedRequirement(**item))
    return accepted, failures


@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    project_id: str | None
    source_id: str
    capability: str
    quote: str
    tier: str


def evidence_records(profile: CandidateProfile) -> list[EvidenceRecord]:
    records, seen, duplicates = [], set(), set()
    projects = {p.id for p in profile.projects}
    for cap in sorted(profile.capabilities.values(), key=lambda c: c.name):
        for ev in cap.evidence:
            if not ev.id or not ev.source.id or not ev.quote.strip():
                continue
            if ev.id in seen:
                duplicates.add(ev.id)
            seen.add(ev.id)
            if ev.project_id is not None and ev.project_id not in projects:
                continue
            # Never borrow other sources/projects to upgrade this specific piece of evidence.
            single_tier = Capability(cap.name, cap.category, evidence=[ev]).evidence_tier
            tier = min((cap.evidence_tier, single_tier), key=LEVEL.get)
            if tier == "missing":
                continue
            records.append(EvidenceRecord(ev.id, ev.project_id, ev.source.id, cap.name, ev.quote, tier))
    return [r for r in records if r.id not in duplicates]


def supported_relation(req: GroundedRequirement, ev: EvidenceRecord) -> tuple[str, str] | None:
    target, source = concept(req.normalized), concept(ev.capability)
    if req.requirement_type not in {"skill", "language", "framework", "cloud_platform", "systems_networking_embedded", "ai_ml"}:
        return None
    # A capability tag with a contradictory/non-supporting quote is insufficient.
    source_words = [ev.capability, source, *(k for k, v in ALIASES.items() if v == source)]
    if not any(contains(ev.quote, term) for term in source_words):
        return None
    if re.search(r"\b(no|not|without|never|planned|todo|tutorial|learning)\b", ev.quote, re.I):
        return None
    # A plain skill citation cannot satisfy stronger tenure/expertise expectations.
    # Those require a future richer claim validator; fail closed for now.
    if re.search(r"\b(expert|expertise|advanced|extensive|proficien\w*|production|commercial|professional|years?|leadership|led|architect)\b", req.excerpt, re.I):
        return None
    if target == source:
        return "direct", ev.tier
    ceiling = RELATIONS.get(target, {}).get(source)
    if ceiling is None:
        return None
    # FastAPI alone is not proof of an implemented API; AWS alone isn't serverless.
    if target == "rest api" and source == "fastapi" and not (
        re.search(r"\b(built|implemented|developed|served)\b", ev.quote, re.I)
        and re.search(r"\b(api|apis|endpoints?)\b", ev.quote, re.I)
    ):
        return None
    if target == "vector search" and not re.search(r"\b(vector|embedding|embeddings)\b", ev.quote, re.I):
        return None
    if target == "session migration" and not re.search(r"\b(session|sessions|migration|migrate)\b", ev.quote, re.I):
        return None
    return f"{source} -> {target}", min((ceiling, ev.tier), key=LEVEL.get)


def select_evidence(requirements: list[GroundedRequirement], records: list[EvidenceRecord], max_count: int, max_chars: int) -> list[EvidenceRecord]:
    selected, size = [], 0
    for record in records:
        if not any(supported_relation(req, record) for req in requirements):
            continue
        length = len(record.quote)
        if len(selected) >= max_count or size + length > max_chars:
            continue
        selected.append(record); size += length
    return selected


def validate_matches(payload: dict, requirements: list[GroundedRequirement], records: list[EvidenceRecord]) -> tuple[list[dict], list[str]]:
    items = payload.get("matches")
    if not isinstance(items, list) or len(items) > 120:
        raise ValueError("invalid_schema")
    reqs, evidence = {r.id: r for r in requirements}, {e.id: e for e in records}
    accepted, failures, seen = [], [], set()
    fields = {"requirement_id", "evidence_id", "project_id", "source_id", "strength", "explanation"}
    for item in items:
        if not isinstance(item, dict) or set(item) != fields or not all(
            isinstance(item.get(k), str) for k in fields - {"project_id"}
        ) or not (item["project_id"] is None or isinstance(item["project_id"], str)):
            failures.append("match_schema"); continue
        req, ev = reqs.get(item["requirement_id"]), evidence.get(item["evidence_id"])
        if req is None or ev is None:
            failures.append("unknown_requirement_or_evidence"); continue
        if item["project_id"] != ev.project_id or item["source_id"] != ev.source_id:
            failures.append("cross_project_or_source"); continue
        relation = supported_relation(req, ev)
        if relation is None:
            failures.append("unsupported_capability"); continue
        reason, ceiling = relation
        strength = item["strength"]
        if strength not in {"partial", "strong"} or LEVEL[strength] > LEVEL[ceiling]:
            failures.append("evidence_strength_inflation"); continue
        explanation = f"{reason}; {strength} support from existing evidence {ev.id}."
        if item["explanation"] != explanation:
            failures.append("unvalidated_explanation"); continue
        pair = (req.id, ev.id)
        if pair in seen:
            continue
        seen.add(pair)
        # Only the audited proposition can be returned; extra claims reject the match.
        accepted.append(dict(item))
    return accepted, failures


def object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
REQUIREMENT_SCHEMA = object_schema({"requirements": {"type": "array", "items": object_schema({
    "id": STRING, "normalized": STRING, "excerpt": STRING,
    "start": {"type": "integer"}, "end": {"type": "integer"},
    "requirement_type": {"type": "string", "enum": list(TYPES)},
    "modality": {"type": "string", "enum": list(MODALITIES)},
})}})
MATCH_SCHEMA = object_schema({"matches": {"type": "array", "items": object_schema({
    "requirement_id": STRING, "evidence_id": STRING, "project_id": {"type": ["string", "null"]},
    "source_id": STRING, "strength": {"type": "string", "enum": ["partial", "strong"]}, "explanation": STRING,
})}})
BULLET_SCHEMA = object_schema({"bullets": {"type": "array", "items": object_schema({
    "project_id": STRING, "evidence_ids": {"type": "array", "items": STRING}, "text": STRING,
})}})


def validate_bullets(
    payload: dict,
    projects: list[Project],
    candidate: CandidateProfile,
) -> tuple[list[dict], list[dict]]:
    """Validate candidate rewritten bullets against claim-level grounding.

    Returns:
        (accepted_bullets, rejected_bullets)
    """
    items = payload.get("bullets")
    if not isinstance(items, list) or len(items) > 30:
        return [], [{"rejection_reasons": ["invalid_bullet_schema"]}]

    proj_map = {p.id: p for p in projects}
    ev_map = {ev.id: ev for cap in candidate.capabilities.values() for ev in cap.evidence}

    accepted: list[dict] = []
    rejected: list[dict] = []

    fields = {"project_id", "evidence_ids", "text"}
    for item in items:
        if not isinstance(item, dict) or set(item) != fields:
            rejected.append({"item": str(item), "rejection_reasons": ["invalid_bullet_item_schema"]})
            continue

        pid = item.get("project_id")
        eids = item.get("evidence_ids")
        text = item.get("text", "")

        if not isinstance(pid, str) or pid not in proj_map:
            rejected.append({**item, "rejection_reasons": [f"unknown_project_id:{pid}"]})
            continue

        if not isinstance(eids, list) or not eids or not all(isinstance(eid, str) for eid in eids):
            rejected.append({**item, "rejection_reasons": ["invalid_or_empty_evidence_ids"]})
            continue

        cited_evs: list[CapabilityEvidence] = []
        unknown_eids: list[str] = []
        for eid in eids:
            ev = ev_map.get(eid)
            if ev is None:
                unknown_eids.append(eid)
            else:
                cited_evs.append(ev)

        if unknown_eids:
            rejected.append({**item, "rejection_reasons": [f"unknown_evidence_ids:{unknown_eids}"]})
            continue

        # Run claim-level grounding validation
        project = proj_map[pid]
        validation_res = validate_bullet_claim(text, project, cited_evs, candidate)

        if validation_res.is_valid:
            accepted.append(dict(item))
        else:
            rejected.append({**item, "rejection_reasons": validation_res.rejection_reasons})

    return accepted, rejected
