"""ONE controlled Browserbase cloud HUMAN-HANDOFF run through the full service. Opt-in.

Runs only with BROWSERBASE_API_KEY set AND JOBINTEL_BROWSERBASE_HANDOFF_E2E=1 (its own flag,
so the other cloud E2E flags never start a second session). Needs a Browserbase plan with
keepAlive. Synthetic data on jobs.example.test only. No human action is required: the test
proves the filled session stays alive with a Live View link, then releases it explicitly.
The Live View URL is never printed or stored.
"""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("playwright.sync_api")

from jobintel.submission.browserbase_cloud import CloudTransport  # noqa: E402
from test_browserbase_service import build, no_receipt_and_not_applied, queued, status  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not (os.environ.get("BROWSERBASE_API_KEY") and os.environ.get("JOBINTEL_BROWSERBASE_HANDOFF_E2E") == "1"),
                       reason="handoff E2E is opt-in: needs BROWSERBASE_API_KEY and JOBINTEL_BROWSERBASE_HANDOFF_E2E=1"),
    pytest.mark.usefixtures("synthetic_answer_bank"),
]


def test_one_cloud_handoff_keeps_filled_session_then_releases_it(tmp_path):
    from test_browserbase_runner_offline import Server
    server = Server()
    s = build(tmp_path / "store", CloudTransport(serve=server.handle, screenshot_dir=tmp_path / "shots"))
    r, c = queued(s)

    out = s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, review_seconds=300)
    live = out.get("live_view_url") or ""
    during = s.svc.handoff_status(r["id"])
    released = s.svc.release_handoff(r["id"], session_id=out["session_id"], claim_token=c["claim_token"])
    after = s.svc.handoff_status(r["id"])

    persisted = b"".join(p.read_bytes() for p in (tmp_path / "store").rglob("*") if p.is_file())
    summary = {"status": out["status"], "handoff": out.get("handoff"), "session_id": out["session_id"],
               "released_at_end_of_run": out["session_released"], "live_view_url_present": live.startswith("https://"),
               "live_view_url_persisted": bool(live) and live.encode() in persisted,
               "provider_status_during_handoff": during.get("provider_status"),
               "explicit_release": released.get("released"), "state_after_release": after["handoff_state"],
               "final_submit_clicked": out["final_submit_clicked"], "server_requests": server.requests,
               "external": server.external}
    print("HANDOFF_CLOUD_E2E_SUMMARY", json.dumps(summary))

    assert out["status"] == "ready_for_human_review" and out["handoff"] == "active"
    assert out["session_released"] is False and live.startswith("https://")
    assert during["handoff_state"] == "active" and during["provider_status"] == "RUNNING"
    assert summary["live_view_url_persisted"] is False
    assert released["released"] is True and after["handoff_state"] == "released"
    assert out["final_submit_clicked"] is False
    assert [x for x in server.requests if x[0] != "GET" or x[1].endswith("/submit")] == [] and server.external == []
    assert status(s, r) == "ready_for_human_review"
    no_receipt_and_not_applied(s)
