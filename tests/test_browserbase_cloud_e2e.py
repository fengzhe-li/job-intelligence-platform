"""ONE controlled Browserbase cloud session against the synthetic form. Opt-in only.

Runs only when BROWSERBASE_API_KEY is set AND JOBINTEL_BROWSERBASE_E2E=1, so the normal
suite never needs internet or credentials. The synthetic form is served from this
process via route interception (nothing is hosted publicly); the server-side
allowedDomains list restricts the remote browser to the synthetic host. Exactly one
session, never retried; synthetic data only.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict

import pytest

pytest.importorskip("playwright.sync_api")

from jobintel.submission import stop_only  # noqa: E402
from jobintel.submission.browserbase_cloud import load_credentials, run_in_cloud  # noqa: E402
from jobintel.submission.browserbase_runner import to_stop_only_report  # noqa: E402

HOST = "jobs.example.test"

pytestmark = pytest.mark.skipif(
    not (os.environ.get("BROWSERBASE_API_KEY") and os.environ.get("JOBINTEL_BROWSERBASE_E2E") == "1"),
    reason="cloud spike is opt-in: needs BROWSERBASE_API_KEY and JOBINTEL_BROWSERBASE_E2E=1")


def test_one_controlled_cloud_session_stops_before_submit(tmp_path):
    from test_browserbase_runner_offline import Server, make_bundle
    creds = load_credentials()
    bundle, package = make_bundle(tmp_path, "cloud")
    server = Server()
    shots = tmp_path / "shots"
    shots.mkdir()

    result, evidence = run_in_cloud(bundle, creds=creds, serve=server.handle, allowed_domains=[HOST],
                                    screenshot_dir=shots)

    summary = {"session_id": evidence.session_id, "released": evidence.released, "status": result.status,
               "stop_reason": result.stop_reason, "advance_clicks": result.advance_clicks,
               "fields": [f["field"] for f in result.fields_filled], "cv": result.cv_upload.get("dom_matches_sealed_file"),
               "final_submit_seen": result.final_submit_seen, "final_submit_clicked": result.final_submit_clicked,
               "blocked_requests": result.blocked_requests, "blocked_submissions": result.blocked_submissions,
               "server_requests": server.requests, "external": server.external,
               "screenshot_sha256": evidence.screenshot_sha256, "replay_url": evidence.replay_url}
    (tmp_path / "cloud_e2e_summary.json").write_text(json.dumps(summary, indent=2))
    print("CLOUD_E2E_SUMMARY", json.dumps(summary))

    assert evidence.released is True
    assert result.final_submit_clicked is False
    assert [r for r in server.requests if r[0] != "GET" or r[1].endswith("/submit")] == []
    assert server.external == []
    assert result.status == "ready_for_human_review" and result.advance_clicks == ["Next"]
    assert result.cv_upload["sha256_verified_before_selection"] and result.cv_upload["dom_matches_sealed_file"]
    report = to_stop_only_report(result, bundle, task_id=evidence.session_id)
    imported = stop_only.import_result(report, package=package, request_id=bundle.request["request_id"],
                                       approval_id=bundle.request["approval_id"], executor="browserbase",
                                       display_name="Browserbase")
    assert report["cv_upload_confirmed"] is True  # observable form evidence of the exact sealed file
    assert imported.outcome == "ready_for_human_review" and imported.submitted_at is None
    assert creds.api_key not in json.dumps(asdict(evidence)) + json.dumps(report)
