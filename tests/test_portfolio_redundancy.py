"""Portfolio non-redundancy is measured from evidence, never from a business-domain label (2026-10-15).

Synthetic projects only: no real project, company or domain name is referenced by the model or these tests.
"""
from __future__ import annotations

from types import SimpleNamespace

from jobintel.matching.tailoring_skill import (MAX_REDUNDANCY_PENALTY, PolicyProjectSelector, ProjectFootprint,
                                                redundancy)
from jobintel.models.job import Job, RoleTrackProfile, SkillRequirement


def fp(caps, purposes=("fullstack_application",), reqs=(), evidence=()):
    return ProjectFootprint(frozenset(caps), frozenset(purposes), frozenset(reqs), frozenset(evidence))


# Domain is not even an input: footprints carry only capabilities, problem shape, requirements and evidence.
LEDGER_API = fp({"fastapi", "postgresql", "sql", "react"}, reqs={"python", "sql"}, evidence={"repo:a/ledger"})
LEDGER_API_TWIN = fp({"fastapi", "postgresql", "sql", "react"}, reqs={"python", "sql"}, evidence={"repo:a/ledger-v2"})
LEDGER_AGENT = fp({"llm orchestration", "pydantic", "audit trail", "pytest"}, purposes=("ai_agent_architecture",),
                  reqs={"python"}, evidence={"repo:a/agent"})
DINING_API = fp({"fastapi", "postgresql", "sql", "react"}, reqs={"python", "sql"}, evidence={"repo:a/dining"})


def test_same_domain_same_capabilities_is_redundant():
    assert redundancy(LEDGER_API_TWIN, [LEDGER_API]) >= 0.7


def test_same_domain_complementary_capabilities_is_not_redundant():
    assert redundancy(LEDGER_AGENT, [LEDGER_API]) <= 0.15


def test_different_domain_same_capabilities_can_still_be_redundant():
    assert redundancy(DINING_API, [LEDGER_API]) == redundancy(LEDGER_API_TWIN, [LEDGER_API]) >= 0.7


def test_shared_repository_is_evidence_overlap():
    same_repo = fp({"kafka"}, purposes=("iot_cloud_event",), evidence={"repo:a/ledger"})
    assert redundancy(same_repo, [LEDGER_API]) > redundancy(fp({"kafka"}, purposes=("iot_cloud_event",)), [LEDGER_API])


def test_ubiquitous_capabilities_barely_count():
    a, b = fp({"python", "docker"}, purposes=("x",)), fp({"python", "rust"}, purposes=("y",))
    freq = {"python": 10, "docker": 1, "rust": 1}
    assert redundancy(a, [b], freq, 10) < redundancy(a, [b]) / 2


def test_nothing_selected_means_no_redundancy():
    assert redundancy(LEDGER_API, []) == 0.0


# --- end to end through the selector, with synthetic projects ---------------------------------------------------

def _cap(name, project_ids):
    ev = [SimpleNamespace(project_id=p, id=f"{p}:{name}", category=None) for p in project_ids]
    return SimpleNamespace(name=name, evidence_tier="strong", evidence=ev)


def _project(pid):
    return SimpleNamespace(id=pid, name=pid, repository_full_name=f"o/{pid}", role_relevance={})


def _profile(caps):
    projects = sorted({ev.project_id for c in caps for ev in c.evidence})
    return SimpleNamespace(projects=[_project(p) for p in projects],
                           capabilities={c.name: c for c in caps})


def _job(skills):
    return Job(id="j", title="Software Engineer", company="Example", description="software engineering",
               locations=[], source_observations=[],
               skill_requirements=[SkillRequirement(name=s, required=True) for s in skills],
               role_track_profile=RoleTrackProfile([]))


def test_selector_keeps_two_same_domain_projects_with_complementary_capabilities():
    caps = [_cap("Python", ["p-api", "p-agent", "p-twin"]), _cap("FastAPI", ["p-api", "p-twin"]),
            _cap("SQL", ["p-api", "p-twin"]), _cap("PostgreSQL", ["p-api", "p-twin"]),
            _cap("LLM Orchestration", ["p-agent"]), _cap("Pydantic", ["p-agent"])]
    job = _job(["Python", "FastAPI", "SQL", "LLM Orchestration"])
    matches = PolicyProjectSelector().select_projects(job, _profile(caps), max_projects=3)
    picked = [m.project.id for m in matches]
    assert picked[0] in {"p-api", "p-twin"} and picked[1] == "p-agent"  # the twin is the redundant one
    agent, twin = matches[1], matches[2]
    assert twin.redundancy_penalty >= 2 * agent.redundancy_penalty
    assert twin.redundancy_penalty >= MAX_REDUNDANCY_PENALTY * 0.7
