"""Sanitized reporting of Browserbase infrastructure failures (offline).

A real operational attempt failed in the session-creation call and the cause was lost: the
service journaled only `executor_interrupted`. The attempt is still outcome_unknown and never
retried, but a closed-set reason (stage, category, HTTP status, exception class name) is now
journaled and attached to the raised exception. Never raw exception text, URLs, headers,
request/response bodies, cookies, the API key or the connectUrl.

The real urllib transport functions are exercised with a patched `urlopen`; nothing leaves the
machine (the CDP case points at a closed local port).
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import urllib.error

import pytest

from jobintel.submission import browserbase_cloud
from jobintel.submission.browserbase_cloud import BrowserbaseSpikeError, CloudTransport, safe_failure
from jobintel.submission.service import ExecutorInterrupted
from test_browserbase_service import SENTINEL_KEY, build, kinds, no_receipt_and_not_applied, queued, status

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

SECRET = "SECRET-" + "7a" * 12
CONNECT_URL = f"ws://127.0.0.1:9/devtools/browser/{SECRET}?signingKey={SECRET}"  # closed port: refused
FORBIDDEN = (SENTINEL_KEY, SECRET, "X-BB-API-Key", "Authorization", "Bearer", "connectUrl", "Set-Cookie",
             "signingKey", "debuggerFullscreenUrl")


class Urlopen:
    """Stands in for urllib.request.urlopen: records the call, raises the configured failure."""

    def __init__(self, failure):
        self.failure, self.calls = failure, 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        raise self.failure


def http_error(code):
    body = io.BytesIO(json.dumps({"error": f"bad key {SENTINEL_KEY}", "connectUrl": CONNECT_URL}).encode())
    headers = {"Set-Cookie": f"session={SECRET}", "Authorization": f"Bearer {SECRET}"}
    return urllib.error.HTTPError(f"https://api.browserbase.com/v1/sessions?token={SECRET}", code, f"denied {SECRET}",
                                  headers, body)


def store_text(s):
    return b"".join(p.read_bytes() for p in s.root.rglob("*") if p.is_file()).decode("utf-8", "replace")


def assert_clean(*texts):
    for text in texts:
        assert not any(f in text for f in FORBIDDEN), text


def failed_run(tmp_path, monkeypatch, *, failure=None, post=None):
    opener = Urlopen(failure)
    monkeypatch.setattr(browserbase_cloud.urllib.request, "urlopen", opener)
    transport = CloudTransport(environ={"BROWSERBASE_API_KEY": SENTINEL_KEY},
                               **({"post": post} if post is not None else {}))
    s = build(tmp_path, transport)
    r, c = queued(s)
    with pytest.raises(ExecutorInterrupted, match="Executor interrupted; investigate this attempt, do not retry") as info:
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    journaled = s.svc.store.requests()[r["id"]]
    assert journaled["status"] == "outcome_unknown" and journaled["reason"] == "executor_interrupted"
    assert journaled["failure"] == info.value.failure  # the raised reason is exactly the journaled one
    assert_clean(store_text(s), str(info.value), repr(info.value), json.dumps(info.value.failure))
    no_receipt_and_not_applied(s)
    return s, r, c, info.value.failure, opener


# --- A/B: HTTP errors from the real session-create call ----------------------------------------------

@pytest.mark.parametrize("code", [401, 429])
def test_AB_session_create_http_error_is_classified(tmp_path, monkeypatch, code):
    s, r, c, failure, opener = failed_run(tmp_path, monkeypatch, failure=http_error(code))
    assert failure == {"stage": "browserbase_session_create", "category": "browserbase_http_error",
                       "http_status": code, "exception_type": "HTTPError"}
    assert opener.calls == 1 and "browserbase_session_started" not in kinds(s, r)


# --- C: unreachable ------------------------------------------------------------------------------------

@pytest.mark.parametrize("error,exc_type", [
    (urllib.error.URLError(ConnectionRefusedError(f"refused {SECRET}")), "ConnectionRefusedError"),
    (TimeoutError(f"timed out {SECRET}"), "TimeoutError"),
])
def test_C_unreachable_is_classified(tmp_path, monkeypatch, error, exc_type):
    s, r, c, failure, opener = failed_run(tmp_path, monkeypatch, failure=error)
    assert failure == {"stage": "browserbase_session_create", "category": "browserbase_unreachable",
                       "exception_type": exc_type}


# --- D: malformed session response ---------------------------------------------------------------------

@pytest.mark.parametrize("response", [
    {"id": "sess-1", "status": "RUNNING"},                                   # no connectUrl
    {"status": "RUNNING", "connectUrl": CONNECT_URL},                         # no id
    {"id": "sess-1", "connectUrl": f"https://example.invalid/{SECRET}"},      # not a ws URL
])
def test_D_malformed_session_response_is_classified(tmp_path, monkeypatch, response):
    s, r, c, failure, opener = failed_run(tmp_path, monkeypatch, post=lambda url, headers, body, timeout: response)
    assert failure == {"stage": "browserbase_session_create", "category": "browserbase_malformed_session_response",
                       "exception_type": "BrowserbaseSpikeError"}
    assert "browserbase_session_started" not in kinds(s, r)


def test_unexpected_session_create_exception_keeps_only_its_class(tmp_path, monkeypatch):
    def post(url, headers, body, timeout):
        raise KeyError(f"{SENTINEL_KEY} {CONNECT_URL}")
    s, r, c, failure, opener = failed_run(tmp_path, monkeypatch, post=post)
    assert failure == {"stage": "browserbase_session_create", "category": "browserbase_session_create_failed",
                       "exception_type": "KeyError"}


# --- E: CDP connection failure after the session exists -------------------------------------------------

def test_E_cdp_connect_failure_is_classified_and_session_released(tmp_path, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from test_browserbase_cloud_offline import FakeApi
    api = FakeApi(CONNECT_URL)
    s, r, c, failure, opener = failed_run(tmp_path, monkeypatch, post=api)
    assert failure["stage"] == "browserbase_cdp_connect" and failure["category"] == "browserbase_cdp_connect_failed"
    assert set(failure) <= {"stage", "category", "exception_type"} and "http_status" not in failure
    assert "browserbase_session_started" in kinds(s, r) and api.calls[-1]["body"]["status"] == "REQUEST_RELEASE"


# --- pure classifier -----------------------------------------------------------------------------------

def test_safe_failure_keeps_only_closed_set_values():
    assert safe_failure(RuntimeError(f"boom {SECRET}"), "browserbase_run") == {
        "stage": "browserbase_run", "category": "executor_internal_error", "exception_type": "RuntimeError"}
    forged = BrowserbaseSpikeError(f"x {SECRET}", category=f"evil {SECRET}", http_status=True,
                                   stage=f"stage {SECRET}", exception_type=f"Bad Type {SECRET}")
    assert safe_failure(forged, "bundle_prepare") == {"stage": "bundle_prepare", "category": "executor_internal_error",
                                                      "exception_type": "BrowserbaseSpikeError"}
    assert "http_status" not in safe_failure(BrowserbaseSpikeError("x", http_status=99999), "browserbase_run")
    assert safe_failure(BrowserbaseSpikeError("x", category="browserbase_runner_incomplete", stage="browserbase_run"),
                        "bundle_prepare")["category"] == "browserbase_runner_incomplete"


def test_failure_before_the_transport_is_reported_at_bundle_prepare(tmp_path, monkeypatch):
    from jobintel.submission import browserbase_service
    def broken(self, package, *, idempotency_key):
        raise OSError(f"disk full at /secret/{SECRET}")
    monkeypatch.setattr(browserbase_service.BrowserbaseServiceExecutor, "prepare", broken)
    s, r, c, failure, opener = failed_run(tmp_path, monkeypatch, failure=AssertionError("must not be called"))
    assert failure == {"stage": "bundle_prepare", "category": "executor_internal_error", "exception_type": "OSError"}
    assert opener.calls == 0


# --- G: still never retried --------------------------------------------------------------------------

def test_G_interrupted_attempt_is_not_retried(tmp_path, monkeypatch):
    s, r, c, failure, opener = failed_run(tmp_path, monkeypatch, failure=http_error(429))
    with pytest.raises(ValueError):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    with pytest.raises(ValueError):
        s.svc.claim(r["id"], worker="runner")
    assert opener.calls == 1 and status(s, r) == "outcome_unknown"
    assert [k for k in kinds(s, r) if k == "outcome_unknown"] == ["outcome_unknown"]


# --- operational helper: the sanitized summary carries the safe reason ------------------------------------

HELPER = Path(__file__).resolve().parents[1] / "scratchpad" / "final_transficc_run.py"


@pytest.mark.skipif(not HELPER.exists(), reason="local operational helper not present")
def test_F_helper_reports_the_safe_reason_and_no_secret(tmp_path, monkeypatch, capsys):
    from test_browserbase_graphql_and_identity import ENDPOINT, HOST
    from test_browserbase_live_host import as_real_host  # noqa: F401
    from jobintel.submission import browserbase_service
    from test_browserbase_service import approve
    monkeypatch.setattr(browserbase_service, "is_synthetic_host", lambda url: False)
    spec = importlib.util.spec_from_file_location("final_transficc_run_under_test", HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    opener = Urlopen(http_error(429))
    monkeypatch.setattr(browserbase_cloud.urllib.request, "urlopen", opener)
    s = build(tmp_path, CloudTransport(environ={"BROWSERBASE_API_KEY": SENTINEL_KEY}),
              url=f"https://{HOST}/graphql/telemetry.html")
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=HOST)
    monkeypatch.setattr(helper, "REQUEST", r["id"])
    monkeypatch.setattr(helper, "EXPECTED_EXECUTOR", "browserbase")
    monkeypatch.setattr(helper, "HOST", HOST)
    monkeypatch.setattr(helper, "READS", ((ENDPOINT, "ApiJobPosting"),))
    monkeypatch.setattr(helper, "FORM_WRITES", ((ENDPOINT, "ApiSetFormValue"),))

    summary = helper.run(s.svc)
    assert (summary["status"], summary["error"], summary["error_category"], summary["error_stage"],
            summary["http_status"]) == ("outcome_unknown", "executor_interrupted", "browserbase_http_error",
                                        "browserbase_session_create", 429)
    assert summary["final_submit_clicked"] is None and summary["handoff"] is None
    printed = json.dumps(summary)
    assert_clean(printed, capsys.readouterr().out, store_text(s))
    assert "claim_token" not in printed and "Executor interrupted" not in printed
    assert opener.calls == 1 and status(s, r) == "outcome_unknown"
