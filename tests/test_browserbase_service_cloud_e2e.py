"""ONE controlled Browserbase cloud run through the FULL submission-service lifecycle. Opt-in.

Runs only with BROWSERBASE_API_KEY set AND JOBINTEL_BROWSERBASE_E2E=1. Synthetic candidate,
synthetic CV, synthetic form served from this process on jobs.example.test (no public
page, no employer/ATS). approve -> enqueue(browserbase) -> claim -> execute -> strict
import. Exactly one session, never retried; no receipt; final submit never clicked.
"""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("playwright.sync_api")

from jobintel.submission.browserbase_cloud import CloudTransport  # noqa: E402
from test_browserbase_service import build, kinds, no_receipt_and_not_applied, queued, status  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not (os.environ.get("BROWSERBASE_API_KEY") and os.environ.get("JOBINTEL_BROWSERBASE_E2E") == "1"),
                       reason="cloud E2E is opt-in: needs BROWSERBASE_API_KEY and JOBINTEL_BROWSERBASE_E2E=1"),
    pytest.mark.usefixtures("synthetic_answer_bank"),
]


def test_one_full_service_cloud_run_stops_before_submit(tmp_path):
    from test_browserbase_runner_offline import Server
    server = Server()
    s = build(tmp_path / "store", CloudTransport(serve=server.handle, screenshot_dir=tmp_path / "shots"))
    r, c = queued(s)

    out = s.svc.execute(r["id"], claim_token=c["claim_token"])

    report = next(e["data"] for e in s.svc.store.events() if e["kind"] == "browserbase_result")
    summary = {**out, "ledger": kinds(s, r), "fields": [f["page_label"] for f in report["fields_filled"]],
               "cv_upload_confirmed": report["cv_upload_confirmed"], "server_requests": server.requests,
               "external": server.external}
    print("SERVICE_CLOUD_E2E_SUMMARY", json.dumps(summary))

    assert out["session_released"] is True
    assert out["final_submit_clicked"] is False and report["final_submit_clicked"] is False
    assert [x for x in server.requests if x[0] != "GET" or x[1].endswith("/submit")] == []
    assert server.external == []
    assert out["status"] == status(s, r) == "ready_for_human_review"
    no_receipt_and_not_applied(s)
