"""Weighted project bullet budget and margin-before-font layout (synthetic; 2026-10-13).

The page visibly prioritises the strongest JD-matched project: primary 4-5 bullets, strong
secondary ~3, supporting ~2. Under page pressure supporting bullets go first, the primary keeps
its strongest 4, the secondary keeps 3 until late, and margins tighten before the font shrinks.
"""
from __future__ import annotations

import tempfile

import pytest

from jobintel.matching import cv_generation as cg
from jobintel.matching.cv_document import ProjectBlock
from jobintel.matching.cv_render import LAYOUT_PRESETS
from test_cv_page_budget import _dense


def _project(pid, score, n=6):
    bullets = [cg._BulletCandidate(pid, [], [f"{pid}:{i}"], f"{pid} bullet {i}", weight=10.0 - i) for i in range(n)]
    return ProjectBlock(pid, pid, [], bullets, selection_score=score)


def test_tiers_follow_strength_not_symmetry():
    projects = cg._assign_emphasis([_project("c", 1.0), _project("a", 3.0), _project("d", 0.5), _project("b", 2.0)])
    assert [(p.project_id, p.emphasis, len(p.bullets)) for p in projects] == [
        ("a", "focus", 4), ("b", "secondary", 3), ("c", "supporting", 2), ("d", "supporting", 2)]
    # The strongest bullets are the ones kept (2026-10-15 editorial policy: primary ceiling 4, not 5).
    assert [b.text for b in projects[0].bullets] == [f"a bullet {i}" for i in range(4)]


def test_ceilings_are_not_quotas():
    projects = cg._assign_emphasis([_project("a", 3.0, n=2), _project("b", 2.0, n=1)])
    assert [len(p.bullets) for p in projects] == [2, 1]


def test_layout_presets_tighten_margins_before_the_font():
    sizes = [p.body_pt for p in LAYOUT_PRESETS]
    assert sizes == sorted(sizes, reverse=True)
    full = [p for p in LAYOUT_PRESETS if p.body_pt == LAYOUT_PRESETS[0].body_pt]
    assert len(full) >= 3  # normal, dense and at least one tighter-margin preset at the full body size
    assert [p.sides_mm for p in full] == sorted((p.sides_mm for p in full), reverse=True)
    for preset in LAYOUT_PRESETS:
        assert 9.0 <= preset.sides_mm <= preset.margin_mm and preset.margin_mm >= 9.0 and preset.body_pt >= 9.5


@pytest.fixture(scope="module")
def dense():
    return _dense()[1]


def test_a_dense_cv_makes_the_primary_project_visibly_dominant(dense):
    projects = dense.document["projects"]
    counts = {p["emphasis"]: [] for p in projects}
    for p in projects:
        counts[p["emphasis"]].append(len(p["bullets"]))
    assert projects[0]["emphasis"] == "focus" and len(counts["focus"]) == 1
    assert counts["focus"][0] >= 4
    assert counts["focus"][0] > max(counts["secondary"]) >= max(counts.get("supporting", [0]))
    assert max(counts["secondary"]) == 3
    assert all(n <= 2 for n in counts.get("supporting", []))
    assert len(set(len(p["bullets"]) for p in projects)) > 1  # never flattened to one count


def test_supporting_bullets_are_trimmed_before_primary_secondary_or_font(dense):
    actions = dense.fit_actions
    def first(fragment):
        return next((i for i, a in enumerate(actions) if fragment in a), len(actions))
    supporting = first("weakest supporting bullet")
    assert supporting < first("weakest focus bullet")
    assert supporting < first("weakest secondary bullet")
    assert supporting < first("preset compact") and supporting < first("preset narrow")
    # Margins tightened at full size were enough: the font was not reduced.
    # (2026-10-15: the editorial layer compresses first, so "dense" can already suffice.)
    assert dense.layout_preset in {"dense", "tight"} and dense.render_body_pt == 10.0
    assert dense.render_side_margin_mm < dense.render_margin_mm
    assert dense.page_count == 1


def test_near_tied_projects_are_ordered_by_distinct_evidence_depth():
    projects = cg._assign_emphasis([_project("shallow", 4.23, n=3), _project("deep", 4.0, n=4), _project("far", 3.0, n=5)])
    assert [(p.project_id, p.emphasis, len(p.bullets)) for p in projects] == [
        ("deep", "focus", 4), ("shallow", "secondary", 3), ("far", "supporting", 2)]


def test_a_clearly_stronger_project_still_leads_even_with_less_evidence():
    projects = cg._assign_emphasis([_project("strong", 5.0, n=2), _project("deep", 3.0, n=5)])
    assert [p.project_id for p in projects] == ["strong", "deep"]
