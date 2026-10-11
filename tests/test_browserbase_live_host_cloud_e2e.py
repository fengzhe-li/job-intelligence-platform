"""ONE controlled Browserbase cloud run in single-request LIVE-HOST configuration. Opt-in.

Runs only with BROWSERBASE_API_KEY set AND JOBINTEL_BROWSERBASE_LIVE_HOST_E2E=1 (its own flag,
so the other cloud E2E flags never start a second session). Needs a plan with keepAlive.
The target is still the reserved synthetic host jobs.example.test, served from this process:
it is only *declared* non-synthetic so the full live-host gate is exercised (explicit opt-in,
exact host, mandatory handoff, recording and logging off, no screenshot). No real employer or
ATS host is contacted. The Live View URL is never printed or stored; the session is released
explicitly at the end.
"""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("playwright.sync_api")

from jobintel.submission import browserbase_service  # noqa: E402
from jobintel.submission.browserbase_cloud import CloudTransport  # noqa: E402
from test_browserbase_service import approve, build, no_receipt_and_not_applied, status  # noqa: E402

HOST = "jobs.example.test"

pytestmark = [
    pytest.mark.skipif(not (os.environ.get("BROWSERBASE_API_KEY") and os.environ.get("JOBINTEL_BROWSERBASE_LIVE_HOST_E2E") == "1"),
                       reason="live-host E2E is opt-in: needs BROWSERBASE_API_KEY and JOBINTEL_BROWSERBASE_LIVE_HOST_E2E=1"),
    pytest.mark.usefixtures("synthetic_answer_bank"),
]


def test_one_cloud_live_host_run_records_nothing_and_stops_before_submit(tmp_path, monkeypatch):
    from test_browserbase_runner_offline import Server
    monkeypatch.setattr(browserbase_service, "is_synthetic_host", lambda url: False)
    server = Server()
    s = build(tmp_path / "store", CloudTransport(serve=server.handle, screenshot_dir=tmp_path / "shots"))
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="live-e2e")

    out = s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, review_seconds=300,
                        allow_live_host=True, live_host=HOST)
    live_view = out.get("live_view_url") or ""
    during = s.svc.handoff_status(r["id"])
    released = s.svc.release_handoff(r["id"], session_id=out["session_id"], claim_token=c["claim_token"])
    after = s.svc.handoff_status(r["id"])

    persisted = b"".join(p.read_bytes() for p in (tmp_path / "store").rglob("*") if p.is_file())
    events = {e["kind"]: e["data"] for e in s.svc.store.events() if e["entity_id"] == r["id"]}
    shots = tmp_path / "shots"
    summary = {"status": out["status"], "handoff": out.get("handoff"), "session_id": out["session_id"],
               "audit": events["browserbase_live_host_authorized"], "evidence_live": events["browserbase_evidence"]["live"],
               "replay_url": events["browserbase_evidence"]["replay_url"],
               "screenshots_written": shots.exists() and any(shots.iterdir()),
               "live_view_url_present": live_view.startswith("https://"),
               "live_view_url_persisted": bool(live_view) and live_view.encode() in persisted,
               "provider_status_during_handoff": during.get("provider_status"),
               "explicit_release": released.get("released"), "state_after_release": after["handoff_state"],
               "final_submit_clicked": out["final_submit_clicked"], "server_requests": server.requests,
               "external": server.external}
    print("LIVE_HOST_CLOUD_E2E_SUMMARY", json.dumps(summary))

    assert out["status"] == "ready_for_human_review" and out["handoff"] == "active"
    assert summary["audit"]["host"] == HOST and summary["audit"]["session_recording"] is False
    assert summary["audit"]["session_logging"] is False and summary["audit"]["human_handoff_required"] is True
    assert summary["evidence_live"]["recording"] is False and summary["evidence_live"]["blocked_requests"] == []
    assert summary["replay_url"] == "" and summary["screenshots_written"] is False
    assert live_view.startswith("https://") and summary["live_view_url_persisted"] is False
    assert during["provider_status"] == "RUNNING"
    assert released["released"] is True and after["handoff_state"] == "released"
    assert out["final_submit_clicked"] is False
    assert [x for x in server.requests if x[0] != "GET" or x[1].endswith("/submit")] == [] and server.external == []
    assert status(s, r) == "ready_for_human_review"
    no_receipt_and_not_applied(s)
