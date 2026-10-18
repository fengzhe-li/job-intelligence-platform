"""Source-code corroboration: a README-silent claim is verified only by the project's OWN repository's
import / dependency-manifest lines at a pinned commit; comments, docstrings and docs never count, a
code record of another repository never counts, and a partly-verified README reason is kept whole."""
from __future__ import annotations

from dataclasses import replace
import json
import subprocess

import pytest

from jobintel.matching import impact_writing as iw
from jobintel.models.candidate import Project
from jobintel.profile_ingestion import _write_profile, load_candidate_profile
from jobintel.repo_code_evidence import code_evidence_text, collect, scan_checkout
from test_package_live_gate_reasons import WRITING, _reasons, _store

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")
REPO = "sam-sample/example-agent"
POLICY = iw.load_default_policy()


def _checkout(tmp_path, origin=f"https://github.com/{REPO}.git"):
    repo = tmp_path / "checkout"
    (repo / "backend/src/app").mkdir(parents=True)
    (repo / "frontend/src").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "README.md").write_text("Uses Celery and Pydantic.\n")
    (repo / "docs/notes.py").write_text("import celery\n")  # docs never count
    (repo / "backend/pyproject.toml").write_text(
        'name = "httpx"\ndependencies = []\ndev = ["pytest>=8", "fastapi>=0.115", "httpx>=0.27"]\n')
    (repo / "backend/src/app/api.py").write_text(
        '"""Celery could be added later."""\n# import redis\nfrom pydantic import BaseModel\nimport os, sqlalchemy.orm\n')
    (repo / "frontend/package.json").write_text('{\n  "dependencies": {\n    "react": "^19.0.0"\n  }\n}\n')
    (repo / "frontend/src/main.tsx").write_text("import { useState } from 'react'\n// import x from 'vue'\n")
    for cmd in (["init", "-q"], ["remote", "add", "origin", origin], ["add", "-A"],
                ["-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
    return repo


def test_only_import_and_manifest_lines_count(tmp_path):
    found = scan_checkout(_checkout(tmp_path),
                          ["Pydantic", "FastAPI", "httpx", "React", "SQLAlchemy", "Celery", "Redis", "Vue", "pytest"])
    assert [(e["path"], e["kind"], e["text"]) for e in found["Pydantic"] if e["kind"] != "documentation"] == [
        ("backend/src/app/api.py", "import", "from pydantic import BaseModel")]
    assert {e["kind"] for e in found["FastAPI"]} == {"manifest"} and found["SQLAlchemy"][0]["kind"] == "import"
    assert len(found["httpx"]) == 1 and found["httpx"][0]["line"] == 3  # `name = "httpx"` is metadata
    assert {e["kind"] for e in found["React"]} == {"manifest", "import"}
    assert found["Redis"] == [] and found["Vue"] == []  # comments never count
    # README / docs mentions are recorded as documentation only; the docs/ .py "import" never counts
    assert {e["kind"] for e in found["Celery"]} == {"documentation"}


def test_collect_pins_the_commit_and_refuses_another_repositorys_checkout(tmp_path):
    repo = _checkout(tmp_path)
    record = collect(tmp_path / "store", REPO, ["Pydantic"], checkout=repo)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert record["commit"] == head and record["source_url"] == f"https://github.com/{REPO}"
    assert code_evidence_text(tmp_path / "store", REPO) == "from pydantic import BaseModel"
    assert code_evidence_text(tmp_path / "store", "sam-sample/other") is None
    with pytest.raises(ValueError, match="origin"):
        collect(tmp_path / "store", "sam-sample/other", ["Pydantic"], checkout=repo)


def _corroborate(code):
    bullets = [("p", "Built a FastAPI layer with Pydantic request models.", ["p:1"])]
    claims, _ = iw.corroborate(bullets, POLICY, repo_text={"p": "FastAPI service."},
                               project_evidence_text={"p": "FastAPI Pydantic"}, repo_code={"p": code})
    return {c["claim"]: c["status"] for c in claims}


def test_a_readme_silent_claim_is_verified_by_the_projects_own_code_record():
    assert _corroborate(None)["Pydantic"] == "partial"
    assert _corroborate("from pydantic import BaseModel")["Pydantic"] == "verified"


README_ONLY = ("Claim(s) not found in the project's own cached repository README (same-project indexed evidence only) "
               "-- verify before use: ")


def _package_reasons(tmp_path, reason, repo_name=REPO, terms=("Pydantic",)):
    root = _store(tmp_path / "store", "Requirements:\n- Expertise in Python.\nLOCATION\nRemote.",
                  reasons=(reason, WRITING))
    profile = load_candidate_profile(root)
    _write_profile(root, replace(profile, projects=[Project("p1", "Agent", "An agent.", repository_full_name=repo_name)]))
    collect(root, REPO, list(terms), checkout=_checkout(tmp_path))
    return _reasons(root)


def test_a_readme_only_reason_is_dropped_when_every_claim_is_code_verified(tmp_path):
    reasons = _package_reasons(tmp_path, README_ONLY + "p1:Pydantic")
    assert not any(r.startswith(README_ONLY) for r in reasons) and WRITING in reasons


def test_one_unverified_claim_keeps_the_whole_reason(tmp_path):
    reason = README_ONLY + "p1:Celery, p1:Pydantic"
    assert reason in _package_reasons(tmp_path, reason, terms=("Pydantic", "Celery"))


def test_a_code_record_of_another_repository_never_verifies(tmp_path):
    reason = README_ONLY + "p1:Pydantic"
    assert reason in _package_reasons(tmp_path, reason, repo_name="sam-sample/different-repo")


def test_each_term_is_classified_and_documentation_alone_never_corroborates(tmp_path):
    repo = _checkout(tmp_path)
    (repo / "backend/tests").mkdir()
    (repo / "backend/tests/test_api.py").write_text("import httpx\nimport pytest\n")
    (repo / "docs/architecture.md").write_text("Events flow through Kafka.\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    record = collect(tmp_path / "store", REPO, ["Pydantic", "httpx", "FastAPI", "Celery", "Kafka", "Redis", "pytest"],
                     checkout=repo)
    status = {term: c["status"] for term, c in record["classification"].items()}
    assert status == {"Pydantic": "directly_implemented", "httpx": "tested", "FastAPI": "dependency_declared",
                      "Celery": "documented_only", "Kafka": "documented_only", "Redis": "absent", "pytest": "tested"}
    assert record["classification"]["httpx"]["evidence"] == ["tested", "dependency_declared"]
    text = code_evidence_text(tmp_path / "store", REPO)
    assert "Celery" not in text and "celery" not in text  # documented-only: never corroboration text
    assert _corroborate_claim("Kafka", text) == "partial" and _corroborate_claim("FastAPI", text) == "verified"


def _corroborate_claim(claim, code):
    bullets = [("p", f"Built a service using {claim}.", ["p:1"])]
    claims, _ = iw.corroborate(bullets, POLICY, repo_text={"p": "A service."},
                               project_evidence_text={"p": claim}, repo_code={"p": code})
    return {c["claim"]: c["status"] for c in claims}.get(claim)
