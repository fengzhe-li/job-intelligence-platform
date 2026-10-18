import json
from types import SimpleNamespace

from jobintel.matching.cv_generation import apply_positioning_override
from jobintel.matching.project_selection import ProjectMatch


def _m(pid):
    return ProjectMatch(project=SimpleNamespace(id=pid), score=1.0, matched_requirements=[], missing_requirements=[], explanation="x")


def test_override_swaps_only_listed_projects_for_the_listed_job(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "cv_positioning_overrides.json").write_text(json.dumps(
        {"job:1": {"prefer_projects": ["sys"], "prefer_position": 1, "avoid_projects": ["ai"], "reason": "systems role"}}))
    store = tmp_path / "data" / "local"; store.mkdir(parents=True)
    cand = SimpleNamespace(projects=[SimpleNamespace(id=i) for i in ("be", "ai", "sys", "web")])
    matches = [_m("be"), _m("ai"), _m("web")]
    out, expl = apply_positioning_override(SimpleNamespace(id="job:1"), cand, matches, {"be": "", "ai": "", "web": ""}, store)
    assert [m.project.id for m in out] == ["be", "sys", "web"] and "ai" not in expl and "override" in expl["sys"]
    same, _ = apply_positioning_override(SimpleNamespace(id="job:2"), cand, matches, {}, store)
    assert same == matches  # other jobs untouched


def test_unknown_preferred_project_is_never_invented(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "cv_positioning_overrides.json").write_text(json.dumps({"job:1": {"prefer_projects": ["ghost"]}}))
    store = tmp_path / "data" / "local"; store.mkdir(parents=True)
    out, _ = apply_positioning_override(SimpleNamespace(id="job:1"), SimpleNamespace(projects=[]), [_m("be")], {}, store)
    assert [m.project.id for m in out] == ["be"]
