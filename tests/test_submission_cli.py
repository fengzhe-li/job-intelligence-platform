from dataclasses import asdict
from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest

from jobintel.cli.main import main
from test_submission_service import setup


def invoke(s, capsys, command, *args):
    with patch("sys.argv", ["jobintel", "submission-" + command, "--store", str(s.root), *args]):
        main()
    return json.loads(capsys.readouterr().out)


def test_cli_full_manual_workflow_has_separate_effects(setup, capsys):
    s = setup
    p = invoke(s, capsys, "preview", "--application-id", s.app.id)
    assert not s.svc.store.approvals()
    a = invoke(s, capsys, "approve", "--application-id", s.app.id, "--package-hash", p["package_hash"], "--reviewer", "human", "--reviewed")
    assert not s.svc.store.requests()
    r = invoke(s, capsys, "enqueue", "--approval-id", a["id"])
    assert r["status"] == "submission_requested"
    c = invoke(s, capsys, "claim", "--request-id", r["id"], "--worker", "human")
    assert "result" not in c
    handoff = invoke(s, capsys, "execute", "--request-id", r["id"], "--claim-token", c["claim_token"])
    assert handoff["executor"] == "manual" and handoff["package"]["cv_artifact_id"] == s.artifact.id
    payload = {"request_id": r["id"], "package_hash": r["package_hash"], "job_id": "j1",
               "cv_artifact_id": s.artifact.id, "application_url": s.app.application_url,
               "executor": "manual", "outcome": "reported_submitted",
               "evidence": [{"kind": "confirmation_text", "value": "Your application has been successfully submitted."}],
               "submitted_at": datetime.now(timezone.utc).isoformat()}
    path = s.root / "result.json"
    path.write_text(json.dumps(payload))
    out = invoke(s, capsys, "complete", "--request-id", r["id"], "--claim-token", c["claim_token"], "--result-file", str(path))
    assert out["status"] == "pending_verification"
    assert not s.apps.for_job("j1").is_submitted and not s.svc.store.receipts()
    receipt = invoke(s, capsys, "verify", "--request-id", r["id"], "--reviewer", "human", "--confirmed")
    assert receipt["submitted"] and not s.apps.for_job("j1").is_submitted
    assert invoke(s, capsys, "reconcile")[0]["status"] == "submitted"
    assert s.apps.for_job("j1").is_submitted
    assert invoke(s, capsys, "list")["requests"][0]["status"] == "submitted"


def test_cli_revoke_and_answer_append(setup, capsys):
    invoke(setup, capsys, "answer-set", "--field", "email", "--value", "new@example.com", "--reviewer", "human", "--provenance", "user confirmed")
    p = invoke(setup, capsys, "preview", "--application-id", setup.app.id)
    a = invoke(setup, capsys, "approve", "--application-id", setup.app.id, "--package-hash", p["package_hash"], "--reviewer", "human", "--reviewed")
    out = invoke(setup, capsys, "revoke", "--approval-id", a["id"], "--reviewer", "human", "--reason", "stop")
    assert out["status"] == "revoked"
    assert [e["kind"] for e in setup.svc.store.events()] == ["answer_recorded", "approved", "revoked"]


@pytest.mark.parametrize("command,args", [
    ("approve", ["--application-id", "app:j1", "--package-hash", "x", "--reviewer", "human"]),
    ("verify", ["--request-id", "r1", "--reviewer", "human"]),
    ("answer-set", ["--field", "visa_status", "--value", "yes", "--reviewer", "human", "--provenance", "guessed"]),
])
def test_cli_required_human_flags_and_sensitive_fields(setup, capsys, command, args):
    with pytest.raises(SystemExit) as error:
        invoke(setup, capsys, command, *args)
    assert error.value.code == 2
    assert not setup.svc.store.receipts()


def test_cli_malformed_completion_has_no_side_effect(setup, capsys):
    path = setup.root / "bad.json"
    path.write_text("not json")
    with pytest.raises(SystemExit):
        invoke(setup, capsys, "complete", "--request-id", "unknown", "--claim-token", "x", "--result-file", str(path))
    assert not setup.svc.store.receipts()
    assert not setup.apps.for_job("j1").is_submitted
