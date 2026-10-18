"""Repository capability maps (2026-10-14): read the repository's own files at a pinned commit, never README
text, into several capability slices with evidence grades; statements name only extracted facts; documented-only
technologies never become evidence; maps feed the Evidence Bank, project selection and the summary."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

import pytest

from jobintel.matching.cv_summary import career_stage, compose_summary, theme_for
from jobintel.repo_capability_map import build_map, scan_checkout, statements_for


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    _write(root, "README.md", "# Demo\nUses Kafka for streaming and Kubernetes for deployment.\n")
    _write(root, "web/package.json", json.dumps({"dependencies": {"react": "19", "recharts": "2"},
                                                  "devDependencies": {"vite": "5", "typescript": "5", "vitest": "1"}}))
    _write(root, "web/src/pages/PitWallOutlook.tsx", "export const PitWallOutlook = () => { const [x] = useState(0); return null }\n")
    _write(root, "web/src/pages/ScenarioMode.tsx", "export const ScenarioMode = () => null\n")
    _write(root, "web/src/components/OutlookChart.tsx", "export const OutlookChart = () => null\n")
    _write(root, "web/src/components/OutlookTable.tsx", "export const OutlookTable = () => null\n")
    _write(root, "web/src/components/LoadingError.tsx", "const [loading, setLoading] = useState(true); if (error) {}\n")
    _write(root, "web/src/App.tsx", "const TABS = [{ label: 'Pit-Wall Outlook' }, { label: 'Scenario Mode' }];\n"
                                    "<Route path=\"/outlook\" element={<PitWallOutlook />} />\n")
    _write(root, "web/src/services/api.ts", "export const get = (id) => fetch(`${BASE}/predictions/${id}`);\n"
                                           "export const h = () => fetch('/api/health');\n")
    _write(root, "web/src/App.test.tsx", "it('renders', () => {}); it('switches tab', () => {});\n")
    _write(root, "api/app.py", "from fastapi import FastAPI\nfrom pydantic import BaseModel\napp = FastAPI()\n"
                               "class Req(BaseModel):\n    x: int\n@app.get('/api/health')\ndef h(): ...\n"
                               "@app.post('/api/predictions')\ndef p(): ...\n")
    _write(root, "tests/test_api.py", "from fastapi.testclient import TestClient\ndef test_health():\n    client.get('/api/health')\n")
    _write(root, "dash/app.py", "import streamlit as st\nst.dataframe([])\nst.selectbox('x', [])\n")
    _write(root, "ops/migrate.sh", "criu dump --tcp-established -t $PID\niptables -I INPUT -p tcp -j DROP\n"
                                   "ip addr add $VIP dev eth0\nssh target ./restore.sh\nscp img target:\nT0=$(date +%s%N)\n")
    _write(root, "ops/restore.sh", "criu restore --tcp-established\niptables -D INPUT -p tcp -j DROP\n")
    _write(root, "ops/client.py", "import socket\ns = socket.socket()\n")
    _write(root, ".github/workflows/ci.yml", "name: demo-ci\njobs:\n  test:\n    steps:\n      - run: pytest\n      - run: npm test\n")
    _write(root, "Dockerfile", "FROM python:3.12\n")
    _write(root, "docker-compose.yml", "services:\n  api:\n    build: .\n  web:\n    build: web\n")
    _write(root, "docs/notes.md", "We also use Redis.\n")
    return root


def _slices(repo):
    return {s["id"]: s for s in scan_checkout(repo)["slices"]}


def test_a_repository_has_several_capability_slices_from_its_own_files(repo):
    slices = _slices(repo)
    assert {"frontend:web", "backend:http_api", "ui:streamlit", "systems:linux_ops", "quality:tests",
            "delivery:ci", "delivery:containers"} <= set(slices)
    front = slices["frontend:web"]["facts"]
    assert front["pages"] == ["Pit-Wall Outlook", "Scenario Mode"]  # the UI's own labels
    assert front["chart_libraries"] == ["Recharts"] and front["build"] == "Vite" and front["typescript"]
    assert "/predictions/${id}" in front["api_paths"] and "/api/health" in front["api_paths"]
    assert slices["frontend:web"]["grade"] == "tested" and front["test_cases"] == 2
    assert slices["backend:http_api"]["facts"]["endpoint_count"] == 2
    assert slices["backend:http_api"]["grade"] == "tested"
    ops = slices["systems:linux_ops"]["facts"]
    assert {"CRIU checkpoint (dump)", "CRIU restore", "iptables traffic lock/unlock", "virtual IP claim/release",
            "remote orchestration over SSH", "persistent TCP client/server"} <= set(ops["operations"])
    assert "--tcp-established" in ops["criu_flags"]


def test_readme_and_docs_are_never_implementation_evidence(repo):
    mapped = scan_checkout(repo)
    assert {"Kafka", "Kubernetes"} <= set(mapped["documented_only"])
    every_cap = {c for s in mapped["slices"] for c in s["capability_names"]}
    assert not every_cap & {"Kafka", "Kubernetes", "Redis"}
    statements = " ".join(st["text"] for s in mapped["slices"] for st in statements_for(s))
    assert "Kafka" not in statements and "Kubernetes" not in statements and "Redis" not in statements
    assert mapped["languages"]["Python"] >= 3 and "TypeScript" in mapped["languages"]


def test_statements_name_only_extracted_facts_and_use_action_verbs(repo):
    for s in scan_checkout(repo)["slices"]:
        for st in statements_for(s):
            first = st["text"].split()[0]
            assert first in {"Built", "Integrated", "Tested", "Implemented", "Automated", "Packaged"}, st["text"]
            assert "Frontend" not in st["capability_names"]  # a category, never a skill
            for banned in ("production", "users", "scale", "%"):
                assert banned not in st["text"]


def test_a_map_is_pinned_to_the_exact_repositorys_commit(repo, tmp_path):
    for cmd in (["init", "-q"], ["remote", "add", "origin", "https://github.com/sam-sample/demo.git"], ["add", "-A"],
                ["-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
    record = build_map("sam-sample/demo", checkout=repo)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert record["commit"] == head and record["repository"] == "sam-sample/demo"
    with pytest.raises(ValueError, match="origin"):
        build_map("sam-sample/other", checkout=repo)


def test_only_implemented_slices_become_evidence_bank_items(repo, tmp_path):
    private = pytest.importorskip("jobintel.evidence_enrichment", reason="Requires the private Evidence Bank adapter")
    CuratedProjectDefinition = private.CuratedProjectDefinition
    _capability_map_evidence = private._capability_map_evidence
    record = {"repository": "sam-sample/demo", "commit": "a" * 40, "source_url": "https://github.com/sam-sample/demo",
              "built_at": "2026-10-14T00:00:00+00:00", **scan_checkout(repo)}
    record["slices"].append({"id": "frontend:docs", "title": "x", "kinds": [], "facts": {}, "files": [], "grade": "documented_only",
                             "capability_names": ["Kafka"], "test_files": []})
    p_def = CuratedProjectDefinition("proj-1", "Demo", "d", "sam-sample/demo", ("demo",), ("trading",), (), "u", ())
    items = _capability_map_evidence(p_def, {"sam-sample/demo": record})
    assert items and all(ev.project_id == "proj-1" and ev.source.id == f"capmap:sam-sample/demo@{'a' * 12}"
                         and ev.source_location.startswith(f"@{'a' * 12}") for ev, _ in items)
    assert not any("Kafka" in caps for _, caps in items)
    assert all("trading" in ev.forbidden_extrapolations for ev, _ in items)
    supporting = [ev for ev, _ in items if ev.category in {"Testing", "DevOps"}]
    assert supporting and all(ev.confidence < 0.9 for ev in supporting)  # ranks below implementation slices


# --- summary ------------------------------------------------------------------------------------------

@dataclass
class _B:
    capability_names: list
    text: str = "Built an interactive React and TypeScript interface."
    evidence_ids: list = field(default_factory=lambda: ["p:capmap:frontend:app:views"])


@dataclass
class _P:
    name: str
    emphasis: str
    bullets: list = field(default_factory=list)


EDU = [{"heading": "University College London: MSc Telecommunications", "dates": "Sep 2025 – Sep 2026 (Expected)"}]


def test_the_summary_states_the_true_career_stage_and_the_jds_theme():
    assert career_stage(EDU) == "MSc Telecommunications student at University College London (graduating Sep 2026)"
    assert career_stage([{"heading": "Uni: BEng X", "dates": "Sep 2020 – Jun 2024"}]) == "BEng X graduate (Uni, Jun 2024)"
    assert theme_for("Build React and TypeScript components for our web UI", "Web Engineer") == "web"
    assert theme_for("On-call incident response, reliability and observability", "Site Reliability Engineer") == "sre"
    projects = [_P("Indy", "focus", [_B(["React", "TypeScript"])]), _P("AFRA", "secondary", [_B(["FastAPI", "React"])]),
                _P("Dropped", "supporting", [_B(["Vue"])])]
    text = compose_summary(jd_text="React TypeScript frontend", job_title="Web Engineer", projects=projects,
                           education=EDU, headline="Web Engineer")
    # 2026-10-15: target identity first; education status is not repeated (it is the next section).
    assert text.startswith("Web Engineer focused on") and "student" not in text.casefold()
    assert "React" in text and "TypeScript" in text
    # Safeguards kept: only retained (focus / secondary) projects' evidence and technologies, no project names,
    # no unsupported experience / years claims.
    assert "Vue" not in text and not any(name in text for name in ("Indy", "AFRA", "Dropped"))
    assert "experience as" not in text.casefold() and "years" not in text.casefold()


def test_no_summary_is_composed_without_grounding():
    assert compose_summary(jd_text="x", job_title="y", projects=[], education=EDU) is None
    assert compose_summary(jd_text="x", job_title="y", projects=[_P("A", "focus", [])], education=[]) is None


# --- selection: a project is not one identity ----------------------------------------------------------

def _profile_with(project_id, categories):
    from datetime import date
    from jobintel.models.candidate import Capability, CapabilityEvidence, CandidateProfile, EvidenceSource, Project
    from jobintel.models.taxonomy import EvidenceSourceType, SkillCategory
    src = EvidenceSource(id="capmap:x@1", source_type=EvidenceSourceType.GITHUB_REPOSITORY_SYNC, title="x", collected_at=date(2026, 10, 14))
    profile = CandidateProfile("c", "Sam", 2026, projects=[Project(project_id, "Demo", "A simulation model.")])
    for i, (category, confidence) in enumerate(categories):
        cap = Capability(f"Cap{i}", SkillCategory.TOOLING, [])
        cap.add_evidence(CapabilityEvidence(src, "q", project_id, confidence, f"{project_id}:capmap:s{i}", category=category))
        profile.capabilities[f"cap{i}"] = cap
    return profile


def test_a_frontend_slice_counts_for_a_web_role_whatever_the_curated_identity():
    from jobintel.matching.project_selection import capability_theme_bonus
    from jobintel.matching.tailoring_skill import ProjectPurpose, project_purposes
    profile = _profile_with("personal-source-indy500", [("Frontend", 1.0), ("Testing", 0.55)])
    project = profile.projects[0]
    assert ProjectPurpose.FULLSTACK_APPLICATION in project_purposes(project, profile)
    assert project_purposes(project, profile)[0] == ProjectPurpose.SIMULATION_MODELING  # the curated one stays
    assert capability_theme_bonus(project, profile, {"Frontend": 1.0}) > 0
    assert capability_theme_bonus(project, profile, {"Testing": 1.0}) == 0  # supporting slices never add theme


def test_role_titles_decide_the_archetype_before_description_inference():
    from types import SimpleNamespace
    from jobintel.matching.tailoring_skill import classify_role_archetype
    profile = SimpleNamespace(primary=lambda: SimpleNamespace(track=SimpleNamespace(value="infrastructure")))
    web = SimpleNamespace(title="Web Engineer", description="scalable infrastructure", company="x", role_track_profile=profile)
    ml = SimpleNamespace(title="Software Engineer (ML Projects)", description="cloud-native", company="x", role_track_profile=profile)
    sre = SimpleNamespace(title="Site Reliability Engineer II", description="", company="x",
                          role_track_profile=SimpleNamespace(primary=lambda: None))
    assert classify_role_archetype(web) == "fullstack"
    assert classify_role_archetype(ml) == "ai_applied_ai"
    assert classify_role_archetype(sre) == "platform_cloud_systems"


def test_generated_validation_artifacts_are_recorded_but_never_claimed(repo):
    _write(repo, "experiments/run1/results/summary.json", '{"accuracy": 0.99}')
    _write(repo, "docs/BENCHMARK_REPORT.md", "# Results\nWe achieved 99%.")
    mapped = scan_checkout(repo)
    assert mapped["validation_artifacts"]["result_files"] == 1
    assert "docs/BENCHMARK_REPORT.md" in mapped["validation_artifacts"]["report_documents"]
    statements = " ".join(st["text"] for s in mapped["slices"] for st in statements_for(s))
    assert "0.99" not in statements and "99%" not in statements


def test_a_map_is_stale_when_the_repository_moved_on():
    from jobintel.repo_capability_map import stale_maps
    import jobintel.repo_capability_map as m
    original = m.load_maps
    m.load_maps = lambda root: {"o/a": {"commit": "old"}, "o/b": {"commit": "same"}}
    try:
        stale = stale_maps("x", {"o/a": "new", "o/b": "same", "o/c": "c1"})
    finally:
        m.load_maps = original
    assert [s["repository"] for s in stale] == ["o/a", "o/c"]
