"""Offline tests for the Browserbase cloud transport spike. No internet, no credentials.

The full `run_in_cloud` path (session create -> connect_over_cdp -> unchanged runner ->
screenshot -> release) runs against a LOCAL headless Chrome's CDP endpoint, handed out
by a fake Browserbase API transport. Only Browserbase's own servers are not exercised.
"""
from __future__ import annotations

import ast
from dataclasses import asdict
import inspect
import io
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
from unittest.mock import patch

import pytest

from jobintel.submission import browserbase_cloud, stop_only
from jobintel.submission.browserbase_cloud import (BrowserbaseCredentials, BrowserbaseSpikeError, CloudSession,
                                                   MissingBrowserbaseCredentials, create_session, load_credentials,
                                                   run_in_cloud, session_request_body)
from jobintel.submission.browserbase_runner import to_stop_only_report
from jobintel.submission.executors import STOP_ONLY_EXECUTORS
from test_handoff_fail_closed import _canonical_bank_values
from test_submission_service import approve, setup  # noqa: F401  (fixture)

SENTINEL_KEY = "bb_live_SENTINEL_" + "7f3a" * 8
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
HOST = "jobs.example.test"
FIXTURE = Path(__file__).parent / "fixtures" / "browser_forms" / "cloud"


class FakeApi:
    """Stands in for api.browserbase.com: records calls, hands out a local CDP endpoint."""

    def __init__(self, connect_url):
        self.connect_url, self.calls = connect_url, []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": json.loads(json.dumps(body))})
        if url.endswith("/sessions"):
            return {"id": "local-cdp-session-1", "status": "RUNNING", "connectUrl": self.connect_url}
        return {"id": url.rsplit("/", 1)[-1], "status": "COMPLETED"}


@pytest.fixture
def local_cdp(tmp_path):
    if not CHROME.exists():
        pytest.skip("local Chrome not installed")
    proc = subprocess.Popen([str(CHROME), "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={tmp_path / 'profile'}",
                             "--disable-background-networking", "--no-first-run", "--disable-component-update",
                             "--disable-sync", "--disable-default-apps", "--disable-extensions", "about:blank"],
                            stderr=subprocess.PIPE, text=True)
    url = None
    for _ in range(200):
        line = proc.stderr.readline()
        match = re.search(r"DevTools listening on (ws://\S+)", line)
        if match:
            url = match.group(1)
            break
    if url is None:
        proc.kill()
        pytest.skip("local Chrome did not expose a CDP endpoint")
    yield url
    proc.kill()
    proc.wait(timeout=10)


@pytest.fixture
def creds():
    return BrowserbaseCredentials(SENTINEL_KEY, "proj_SENTINEL")


def _bundle(tmp_path):
    pytest.importorskip("playwright.sync_api")
    from test_browserbase_runner_offline import make_bundle
    return make_bundle(tmp_path, "cloud")


# --- credentials ------------------------------------------------------------------------------

def test_missing_credentials_fail_cleanly_before_any_request():
    with pytest.raises(MissingBrowserbaseCredentials, match="BROWSERBASE_API_KEY is not set") as exc:
        load_credentials({})
    assert "Nothing was sent" in str(exc.value)
    with pytest.raises(MissingBrowserbaseCredentials):
        load_credentials({"BROWSERBASE_API_KEY": "   "})


def test_credentials_and_connect_url_never_appear_in_reprs():
    creds = load_credentials({"BROWSERBASE_API_KEY": SENTINEL_KEY, "BROWSERBASE_PROJECT_ID": "proj_SENTINEL"})
    session = CloudSession("s1", "RUNNING", f"wss://connect.example?signingKey={SENTINEL_KEY}")
    for text in (repr(creds), str(creds), repr(session), str(session)):
        assert SENTINEL_KEY not in text and "proj_SENTINEL" not in text


def test_http_errors_do_not_echo_response_bodies(creds):
    error = urllib.error.HTTPError("https://api.browserbase.com/v1/sessions", 401, "Unauthorized", {},
                                   io.BytesIO(f"bad key {SENTINEL_KEY}".encode()))
    with patch("urllib.request.urlopen", side_effect=error), pytest.raises(BrowserbaseSpikeError) as exc:
        create_session(creds, allowed_domains=[HOST])
    assert str(exc.value) == "Browserbase API returned HTTP 401"


def test_session_request_is_conservative_and_carries_no_candidate_data(creds):
    body = session_request_body(creds, [HOST])
    assert body["keepAlive"] is False and body["timeout"] == 300
    assert body["browserSettings"] == {"solveCaptchas": False, "blockAds": True, "recordSession": True,
                                       "logSession": True, "allowedDomains": [HOST]}
    assert "proxies" not in body and "verified" not in json.dumps(body) and "advancedStealth" not in json.dumps(body)
    assert SENTINEL_KEY not in json.dumps(body)  # the key travels only in the X-BB-API-Key header
    assert body["userMetadata"] == {"purpose": "jobintel-synthetic-stop-only-spike"}


# --- full transport path against a local CDP endpoint -----------------------------------------------

def test_run_in_cloud_path_against_local_cdp_preserves_stop_only(tmp_path, local_cdp, creds, caplog, capsys):
    from test_browserbase_runner_offline import Server
    bundle, package = _bundle(tmp_path)
    server, api = Server(), FakeApi(local_cdp)
    caplog.set_level(logging.DEBUG)
    shots = tmp_path / "shots"
    shots.mkdir()

    result, evidence = run_in_cloud(bundle, creds=creds, serve=server.handle, allowed_domains=[HOST],
                                    screenshot_dir=shots, post=api)

    # one session created and released, key only in headers
    assert [c["url"].rsplit("/v1", 1)[1] for c in api.calls] == ["/sessions", "/sessions/local-cdp-session-1"]
    assert api.calls[1]["body"]["status"] == "REQUEST_RELEASE" and evidence.released is True
    assert all(c["headers"]["X-BB-API-Key"] == SENTINEL_KEY for c in api.calls)
    # unchanged runner semantics over the CDP transport
    assert result.status == "ready_for_human_review"
    assert result.advance_clicks == ["Next"] and result.pages_visited == 2
    assert result.start_identity["state"] == "matched"
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email", "legal_last_name", "country",
                                                           "travel"]
    sealed = {(a.field, a.value) for a in bundle.answers}
    assert all((f["field"], f["value"]) in sealed for f in result.fields_filled)
    assert result.cv_upload["sha256_verified_before_selection"] is True
    assert result.cv_upload["dom_matches_sealed_file"] is True
    assert result.final_submit_seen is True and result.final_submit_clicked is False
    assert result.blocked_requests == [] and result.blocked_submissions == []
    assert [r for r in server.requests if r[0] != "GET" or r[1].endswith("/submit")] == []
    assert server.external == []
    # evidence: session id + screenshot, never the key or connect URL
    assert evidence.session_id == "local-cdp-session-1"
    assert Path(evidence.screenshot_path).is_file()
    import hashlib
    assert hashlib.sha256(Path(evidence.screenshot_path).read_bytes()).hexdigest() == evidence.screenshot_sha256
    report = to_stop_only_report(result, bundle, task_id=evidence.session_id)
    blobs = [json.dumps(asdict(evidence)), json.dumps(result.__dict__, default=str), json.dumps(report),
             caplog.text, capsys.readouterr().out]
    assert not any(SENTINEL_KEY in b or local_cdp in b for b in blobs)
    imported = stop_only.import_result(report, package=package, request_id=bundle.request["request_id"],
                                       approval_id=bundle.request["approval_id"], executor="browserbase",
                                       display_name="Browserbase")
    # The native file input shows exactly the sealed CV: observable form evidence -> cv_upload_confirmed.
    assert report["cv_upload_confirmed"] is True and report["final_submit_clicked"] is False
    assert imported.outcome == "ready_for_human_review"  # stop-only: never a receipt
    assert imported.submitted_at is None


def test_session_is_released_even_when_connecting_fails(tmp_path, creds):
    bundle, _ = _bundle(tmp_path)
    api = FakeApi("ws://127.0.0.1:9/devtools/browser/unreachable")
    with pytest.raises(Exception) as exc:
        run_in_cloud(bundle, creds=creds, serve=lambda route, request: route.abort(), allowed_domains=[HOST],
                     screenshot_dir=tmp_path, post=api)
    assert [c["body"].get("status") for c in api.calls][-1] == "REQUEST_RELEASE"
    assert SENTINEL_KEY not in str(exc.value)


# --- purity, literals, registration ---------------------------------------------------------------

def test_cloud_adapter_imports_no_candidate_data_or_agent_dependency():
    tree = ast.parse(inspect.getsource(browserbase_cloud))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert imported <= {"__future__", "annotations", "dataclasses", "dataclass", "field", "asdict", "hashlib", "json",
                        "os", "pathlib", "Path", "urllib.error", "urllib.request", "urllib.parse", "urlsplit",
                        "jobintel.submission", "browser_session", "browserbase_runner", "playwright.sync_api", "sync_playwright"}
    code = subprocess.run([sys.executable, "-c",
                           "import sys, jobintel.submission.browserbase_cloud; "
                           "print(sorted({m.split('.')[0] for m in sys.modules} & {'stagehand', 'openai', 'anthropic', "
                           "'browserbase', 'playwright', 'requests', 'httpx'}))"],
                          capture_output=True, text=True, check=True,
                          env={"PYTHONPATH": str(Path(browserbase_cloud.__file__).resolve().parents[2])})
    assert code.stdout.strip() == "[]"  # Playwright is imported lazily, only when a session actually runs


def test_no_candidate_literals_in_cloud_adapter_or_fixture():
    reference = _canonical_bank_values()
    texts = [inspect.getsource(browserbase_cloud)] + [p.read_text() for p in FIXTURE.glob("*.html")]
    assert sum(1 for v in reference for t in texts if v in t) == 0  # count only


def test_browserbase_is_a_stop_only_executor_and_unregistered_backends_are_refused(setup, capsys):
    assert STOP_ONLY_EXECUTORS == frozenset({"airtap", "browserbase", "local_browser"})
    approval = approve(setup)
    # Browserbase only exists as a stop-only executor of the service; an unregistered backend is refused.
    with pytest.raises(ValueError, match="Unknown executor"):
        setup.svc.enqueue(approval["id"], executor="unregistered_backend")
    assert setup.svc.store.requests() == {}
    from jobintel.cli.main import main
    with patch("sys.argv", ["jobintel", "submission-enqueue", "--approval-id", approval["id"], "--executor",
                            "unregistered_backend", "--store", str(setup.root)]), pytest.raises(SystemExit):
        main()
    assert "invalid choice: 'unregistered_backend'" in capsys.readouterr().err
    assert setup.svc.store.requests() == {}
