"""Projects are excluded for a JD from their evidence, never by project or employer name (2026-10-15).

Synthetic projects and roles only; real employer names appear solely to prove they change nothing.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from jobintel.matching import tailoring_skill as ts
from jobintel.matching.tailoring_skill import (PolicyProjectSelector, ProjectPurpose, classify_role_archetype,
                                                evidence_irrelevance)
from jobintel.models.job import Job, RoleTrackProfile, SkillRequirement


def _cand(affinity, reqs):
    return {"affinity": affinity, "matched_requirements": list(reqs)}


POOL = [_cand(3.0, ["Python", "React"]), _cand(2.0, ["Python", "SQL"]), _cand(1.0, ["Python"])]


def test_no_affinity_and_only_ubiquitous_matches_is_irrelevant():
    weak = _cand(0.0, ["Python"])
    assert "only ubiquitous" in evidence_irrelevance(weak, POOL + [weak])


def test_positive_affinity_keeps_a_project_with_ubiquitous_matches():
    assert evidence_irrelevance(_cand(2.5, ["Python"]), POOL) is None


def test_a_distinct_jd_match_keeps_a_project_without_affinity():
    distinct = _cand(0.0, ["Python", "C"])
    assert evidence_irrelevance(distinct, POOL + [distinct]) is None


# --- end to end, synthetic projects --------------------------------------------------------------------------

def _cap(name, project_ids, category=None):
    ev = [SimpleNamespace(project_id=p, id=f"{p}:capmap:{name}" if category else f"{p}:{name}", category=category)
          for p in project_ids]
    return SimpleNamespace(name=name, evidence_tier="strong", evidence=ev)


def _profile(caps):
    pids = sorted({ev.project_id for c in caps for ev in c.evidence})
    return SimpleNamespace(projects=[SimpleNamespace(id=p, name=p, repository_full_name=f"o/{p}", role_relevance={})
                                     for p in pids], capabilities={c.name: c for c in caps})


def _job(title, skills, company="Example Ltd", description=""):
    return Job(id="j", title=title, company=company, description=description or title, locations=[],
               source_observations=[], skill_requirements=[SkillRequirement(name=s, required=True) for s in skills],
               role_track_profile=RoleTrackProfile([]))


@pytest.fixture
def purposes(monkeypatch):
    mapping = {"p-web": ProjectPurpose.FULLSTACK_APPLICATION, "p-api": ProjectPurpose.FULLSTACK_APPLICATION,
               "p-sim": ProjectPurpose.SIMULATION_MODELING, "p-sim-web": ProjectPurpose.SIMULATION_MODELING,
               "p-fw": ProjectPurpose.EMBEDDED_FIRMWARE}
    monkeypatch.setattr(ts, "PROJECT_PURPOSE_MAP", mapping)


def test_a_simulation_only_project_is_dropped_but_one_with_real_web_code_is_kept(purposes):
    caps = [_cap("Python", ["p-web", "p-api", "p-sim", "p-sim-web"]), _cap("React", ["p-web", "p-sim-web"]),
            _cap("Frontend", ["p-sim-web"], category="Frontend"), _cap("SQL", ["p-api"])]
    picked = [m.project.id for m in PolicyProjectSelector().select_projects(
        _job("Tax Software Engineer", ["Python", "React", "SQL"]), _profile(caps))]
    assert "p-sim" not in picked          # its only purpose is unsuited to the role
    assert "p-sim-web" in picked          # same domain, but its evidenced frontend code is relevant


def test_no_affinity_and_ubiquitous_only_drops_a_project_and_a_distinct_match_keeps_it(purposes):
    base = [_cap("Python", ["p-web", "p-api", "p-fw"]), _cap("React", ["p-web"]), _cap("SQL", ["p-api"])]
    job = _job("Full Stack Engineer", ["Python", "React", "SQL", "C"])
    assert "p-fw" not in [m.project.id for m in PolicyProjectSelector().select_projects(job, _profile(base))]
    with_c = base + [_cap("C", ["p-fw"])]
    assert "p-fw" in [m.project.id for m in PolicyProjectSelector().select_projects(job, _profile(with_c))]


@pytest.mark.parametrize("company", ["Barclays", "KPMG", "McLaren Racing", "Red Bull Powertrains", "Morgan Stanley",
                                     "Example Ltd"])
def test_the_employer_name_never_changes_the_role_or_the_selection(purposes, company):
    caps = [_cap("Python", ["p-web", "p-api", "p-fw"]), _cap("React", ["p-web"]), _cap("SQL", ["p-api"]),
            _cap("C", ["p-fw"])]
    job, ref = (_job("Technology Developer Graduate", ["Python", "SQL", "C"], company=c,
                     description="General developer graduate programme") for c in (company, "Example Ltd"))
    assert classify_role_archetype(job) == classify_role_archetype(ref)
    sel = lambda j: [(m.project.id, m.score) for m in PolicyProjectSelector().select_projects(j, _profile(caps))]  # noqa: E731
    assert sel(job) == sel(ref)


def test_the_selection_path_names_no_project_or_employer():
    source = "".join(inspect.getsource(f) for f in (ts.classify_role_archetype, ts.get_purpose_affinity,
                                                     ts.evidence_irrelevance, ts.redundancy,
                                                     ts.PolicyProjectSelector.select_projects)).lower()
    for name in ("personal-source", "afra", "indy", "barclays", "kpmg", "mclaren", "red bull", "morgan stanley",
                 "goldman", "jpmorgan", "williams", "ferrari", "mercedes"):
        assert name not in source, name
