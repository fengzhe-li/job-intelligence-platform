"""Single-request live-host supervised mode for Browserbase. Synthetic, offline.

A request queued for one exact real host can run once, under mandatory human handoff,
with Browserbase recording and logging off, every unknown write blocked, any host drift
stopped, and no final-submit path. Every host here is reserved (.test / example.*); a
"real" host is simulated by declaring the reserved host non-synthetic, so nothing leaves
the machine.
"""
from __future__ import annotations

from dataclasses import fields
from datetime import timedelta
import hashlib
import inspect
import json
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import pytest

from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage
from jobintel.submission import browserbase_cloud, browserbase_service
from jobintel.submission.browserbase import BrowserbaseBundleAuthorization, write_bundle
from jobintel.submission.browserbase_cloud import (LIVE_BROWSER_SETTINGS, BrowserbaseCredentials, CloudTransport,
                                                   run_in_cloud, session_request_body)
from jobintel.submission.browserbase_service import (BrowserbaseExecutionConfig, LiveHostAuthorization,
                                                     canonical_host, validate_live_host)
from test_browserbase_service import (CLOUD_URL, LIVE_URL, SENTINEL_KEY, approve, build, kinds,
                                      no_receipt_and_not_applied, ready_result, status)
from test_browserbase_service import FakeTransport as _BaseFakeTransport

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

HOST = "jobs.example.test"
FIXTURES = Path(__file__).parent / "fixtures" / "live_host"
AUTOSAVE = ("POST", f"https://{HOST}/autosave/draft")


class FakeTransport(_BaseFakeTransport):
    """The shared fake provider, also recording what each run was asked to do in live-host mode."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_kwargs = []

    def run(self, bundle, *, write_allowlist, on_session, keep_alive=False, review_seconds=0, live_host=None,
            graphql_reads=(), graphql_form_writes=(), nonfatal_blocked_writes=()):
        self.run_kwargs.append({"write_allowlist": write_allowlist, "keep_alive": keep_alive,
                                "review_seconds": review_seconds, "live_host": live_host,
                                "graphql_reads": graphql_reads, "graphql_form_writes": graphql_form_writes,
                                "nonfatal_blocked_writes": nonfatal_blocked_writes})
        return super().run(bundle, write_allowlist=write_allowlist, on_session=on_session, keep_alive=keep_alive,
                           review_seconds=review_seconds)


@pytest.fixture
def as_real_host(monkeypatch):
    """Treat the reserved host as a real employer host: the synthetic-only default must refuse it."""
    monkeypatch.setattr(browserbase_service, "is_synthetic_host", lambda url: False)


def events(s, r, kind):
    return [e["data"] for e in s.svc.store.events() if e["entity_id"] == r["id"] and e["kind"] == kind]


def store_bytes(s):
    return b"".join(p.read_bytes() for p in s.root.rglob("*") if p.is_file())


def live_queued(s, host=HOST):
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=host)
    return r, s.svc.claim(r["id"], worker="runner")


def live_execute(s, r, c, **kwargs):
    args = dict(claim_token=c["claim_token"], human_handoff=True, allow_live_host=True, live_host=HOST)
    args.update(kwargs)
    return s.svc.execute(r["id"], **args)


def nothing_started(s, r):
    assert s.transport is None or s.transport.calls == 0
    assert "handoff_started" not in kinds(s, r) and "browserbase_live_host_authorized" not in kinds(s, r)
    assert status(s, r) == "executing"


# --- 1. explicit opt-in, per request ------------------------------------------------------------------

def test_real_host_is_refused_by_default(tmp_path, as_real_host):
    s = build(tmp_path, FakeTransport(ready_result))
    with pytest.raises(ValueError, match="live execution is not enabled"):
        s.svc.enqueue(approve(s)["id"], executor="browserbase")
    assert s.svc.store.requests() == {}


def test_live_flag_missing_is_refused_before_any_session(tmp_path, as_real_host):
    s = build(tmp_path, FakeTransport(ready_result))
    r, c = live_queued(s)
    assert s.svc.store.requests()[r["id"]]["live_host"] == HOST
    for kwargs in ({"allow_live_host": False}, {"live_host": None}, {"allow_live_host": False, "live_host": None}):
        with pytest.raises(ValueError, match="explicit live-host opt-in"):
            live_execute(s, r, c, **kwargs)
    nothing_started(s, r)


def test_human_handoff_missing_is_refused_before_any_session(tmp_path, as_real_host):
    s = build(tmp_path, FakeTransport(ready_result))
    r, c = live_queued(s)
    with pytest.raises(ValueError, match="requires human handoff"):
        live_execute(s, r, c, human_handoff=False)
    nothing_started(s, r)


def test_live_flags_on_a_synthetic_request_or_other_executor_are_refused(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result))
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase")  # not queued for a live host
    c = s.svc.claim(r["id"], worker="runner")
    with pytest.raises(ValueError, match="does not match the host this request was queued for"):
        live_execute(s, r, c)
    nothing_started(s, r)
    s2 = build(tmp_path / "b", FakeTransport(ready_result))
    with pytest.raises(ValueError, match="only available for the browserbase executor"):
        s2.svc.enqueue(approve(s2)["id"], executor="airtap", live_host=HOST)


# --- 2. exact host binding --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["JOBS.EXAMPLE.TEST", "jobs.example.test.", "example.test", "login.jobs.example.test",
                                 "*.example.test", "jobs.example.test:443", "https://jobs.example.test", "", " jobs.example.test"])
def test_host_mismatch_is_refused_at_enqueue(tmp_path, as_real_host, bad):
    s = build(tmp_path, FakeTransport(ready_result))
    with pytest.raises(ValueError):
        s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=bad)
    assert s.svc.store.requests() == {}


def test_host_mismatch_is_refused_at_execute_and_live_host_cannot_change(tmp_path, as_real_host):
    s = build(tmp_path, FakeTransport(ready_result))
    approval = approve(s)
    r = s.svc.enqueue(approval["id"], executor="browserbase", live_host=HOST)
    with pytest.raises(ValueError, match="cannot change its live host"):
        s.svc.enqueue(approval["id"], executor="browserbase", live_host="other.example.test")
    with pytest.raises(ValueError, match="cannot change its live host"):
        s.svc.enqueue(approval["id"], executor="browserbase")
    c = s.svc.claim(r["id"], worker="runner")
    with pytest.raises(ValueError, match="does not match the host this request was queued for"):
        live_execute(s, r, c, live_host="login.jobs.example.test")
    nothing_started(s, r)


@pytest.mark.parametrize("url", ["http://jobs.example.test/a", "https://jobs.example.test:8443/a",
                                 "https://user:pw@jobs.example.test/a", "https://192.0.2.10/a", "https://[2001:db8::1]/a"])
def test_canonical_host_rejects_non_plain_https_hosts(url):
    with pytest.raises(ValueError):
        canonical_host(url)


def test_exact_host_match_runs_once_under_handoff_with_privacy_mode(tmp_path, as_real_host):
    s = build(tmp_path, FakeTransport(ready_result))
    r, c = live_queued(s)
    out = live_execute(s, r, c, review_seconds=900)
    assert s.transport.run_kwargs == [{"write_allowlist": (), "keep_alive": True, "review_seconds": 900,
                                       "live_host": HOST, "graphql_reads": (), "graphql_form_writes": (),
                                       "nonfatal_blocked_writes": ()}]
    assert out["status"] == "ready_for_human_review" and out["handoff"] == "active"
    assert out["final_submit_clicked"] is False and out["live_view_url"] == LIVE_URL
    assert kinds(s, r)[:4] == ["enqueued", "claimed", "browserbase_live_host_authorized", "handoff_started"]
    [handoff] = events(s, r, "browserbase_handoff_started")
    assert handoff["live_host"] == HOST and handoff["live_view_persisted"] is False
    no_receipt_and_not_applied(s)


# --- 3. audit trail and runtime-only values ---------------------------------------------------------

def test_audit_event_records_the_authorization_and_nothing_secret(tmp_path, as_real_host):
    s = build(tmp_path, FakeTransport(ready_result))
    r, c = live_queued(s)
    live_execute(s, r, c, review_seconds=600, live_write_allowlist=[AUTOSAVE])
    [audit] = events(s, r, "browserbase_live_host_authorized")
    approval = s.svc.store.approvals()[r["approval_id"]]
    assert audit == {"request_id": r["id"], "application_id": "app:j1", "approval_id": approval["id"],
                     "package_hash": r["package_hash"], "host": HOST, "expires_at": audit["expires_at"],
                     "human_handoff_required": True, "review_seconds": 600, "session_recording": False,
                     "session_logging": False, "screenshots": "not_captured", "write_allowlist_count": 1,
                     "graphql_read_allowlist_count": 0, "graphql_form_write_allowlist_count": 0,
                     "nonfatal_blocked_write_count": 0,
                     "final_submit_allowed": False, "automatic_retry": False}
    # Expires with this request's review window, never later than the approval.
    expires = audit["expires_at"]
    assert expires <= (s.now[0] + timedelta(seconds=300 + 600)).isoformat() and expires <= approval["expires_at"]
    blob = store_bytes(s)
    assert LIVE_URL.encode() not in blob and b"SECRET-TOKEN" not in blob and SENTINEL_KEY.encode() not in blob


def test_live_view_url_is_never_persisted_in_live_mode(tmp_path, as_real_host):
    s = build(tmp_path, FakeTransport(ready_result))
    r, c = live_queued(s)
    out = live_execute(s, r, c)
    assert out["live_view_url"] == LIVE_URL
    view = s.svc.handoff_status(r["id"])
    assert view["live_view_url"] == LIVE_URL
    assert LIVE_URL.encode() not in store_bytes(s)
    s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])
    assert s.transport.release_calls == ["fake-session-1"] and status(s, r) == "ready_for_human_review"
    no_receipt_and_not_applied(s)


def test_credential_absence_fails_before_anything_is_journaled(tmp_path, as_real_host):
    s = build(tmp_path)
    s.svc._browserbase_transport = CloudTransport(environ={}, post=lambda *a: pytest.fail("API must not be called"))
    r, c = live_queued(s)
    with pytest.raises(ValueError, match="BROWSERBASE_API_KEY is not set"):
        live_execute(s, r, c)
    nothing_started(s, r)
    assert not (s.root / "submissions/browserbase").exists()


# --- 4. per-request write allowlist -----------------------------------------------------------------

@pytest.mark.parametrize("entries", [
    [("POST", f"https://{HOST}/autosave/*")],
    [("POST", "https://other.example.test/autosave/draft")],
    [("POST", f"https://login.{HOST}/autosave/draft")],
    [("POST", f"https://{HOST}/application/submit")],
    [("POST", f"https://{HOST}/draft?action=apply")],
    [("DELETE", f"https://{HOST}/autosave/draft")],
    [("GET", f"https://{HOST}/autosave/draft")],
    [("POST", f"http://{HOST}/autosave/draft")],
    [AUTOSAVE, AUTOSAVE],
    [("POST", f"https://{HOST}/autosave/{i}") for i in range(6)],
])
def test_invalid_live_write_allowlists_are_refused_before_any_session(tmp_path, as_real_host, entries):
    s = build(tmp_path, FakeTransport(ready_result))
    r, c = live_queued(s)
    with pytest.raises(ValueError):
        live_execute(s, r, c, live_write_allowlist=entries)
    nothing_started(s, r)


def test_live_run_never_reuses_a_service_wide_write_allowlist(tmp_path, as_real_host):
    s = build(tmp_path, FakeTransport(ready_result))
    s.svc._browserbase_config = BrowserbaseExecutionConfig(write_allowlist=(AUTOSAVE,))
    r, c = live_queued(s)
    with pytest.raises(ValueError, match="only its own per-request write allowlist"):
        live_execute(s, r, c)
    nothing_started(s, r)


# --- 5. no retry ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("stop", [
    dict(status="needs_human_input", stop_reason="unexpected_write_blocked", cv_upload={}),
    dict(status="aborted_wrong_job", stop_reason="host_drift_blocked"),
])
def test_no_retry_after_a_live_stop(tmp_path, as_real_host, stop):
    s = build(tmp_path, FakeTransport(lambda b: ready_result(b, **stop)))
    approval = approve(s)
    r = s.svc.enqueue(approval["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="runner")
    live_execute(s, r, c)
    with pytest.raises(ValueError, match="cannot be started or repeated"):
        live_execute(s, r, c)
    with pytest.raises(ValueError):
        s.svc.claim(r["id"], worker="runner-2")
    try:  # a stopped request is either returned as-is or refused; never a new attempt
        assert s.svc.enqueue(approval["id"], executor="browserbase", live_host=HOST)["id"] == r["id"]
    except ValueError as exc:
        assert "cannot be retried automatically" in str(exc)
    assert s.transport.calls == 1 and len(events(s, r, "browserbase_live_host_authorized")) == 1
    no_receipt_and_not_applied(s)


def test_interrupted_live_run_is_outcome_unknown_and_not_retried(tmp_path, as_real_host):
    s = build(tmp_path, FakeTransport(ready_result, raise_after_session=RuntimeError("provider failure")))
    approval = approve(s)
    r = s.svc.enqueue(approval["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="runner")
    with pytest.raises(ValueError, match="Executor interrupted"):
        live_execute(s, r, c)
    assert status(s, r) == "outcome_unknown"
    with pytest.raises(ValueError, match="cannot be retried automatically"):
        s.svc.enqueue(approval["id"], executor="browserbase", live_host=HOST)
    assert s.transport.calls == 1


# --- 6. no global permission / no final submit ---------------------------------------------------------

def test_no_global_live_host_permission_exists(tmp_path, as_real_host):
    assert [f.name for f in fields(BrowserbaseExecutionConfig)] == ["write_allowlist"]
    assert not any("final" in f.name or "submit" in f.name for f in fields(LiveHostAuthorization))
    # A live-host request for one application grants nothing to another request on the same host.
    s = build(tmp_path, FakeTransport(ready_result))
    r, c = live_queued(s)
    live_execute(s, r, c)
    s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])
    other = build(tmp_path / "other", FakeTransport(ready_result))
    with pytest.raises(ValueError, match="live execution is not enabled"):
        other.svc.enqueue(approve(other)["id"], executor="browserbase")
    with pytest.raises(ValueError, match="live execution is not enabled"):
        browserbase_service.check_target(CLOUD_URL, BrowserbaseExecutionConfig())


def test_no_final_submit_option_anywhere_on_the_live_path():
    import argparse
    from jobintel.submission.cli import add_submission_commands
    from jobintel.submission.service import SubmissionService
    for fn in (SubmissionService.execute, SubmissionService.enqueue, run_in_cloud, session_request_body,
               browserbase_service.BrowserbaseServiceExecutor.__init__):
        assert not any("final" in p or "submit" in p for p in inspect.signature(fn).parameters)
    parser = argparse.ArgumentParser()
    add_submission_commands(parser.add_subparsers(dest="command"))
    execute = parser._subparsers._group_actions[0].choices["submission-execute"]
    options = {o for a in execute._actions for o in a.option_strings}
    assert {"--allow-live-host", "--live-host", "--live-write-allow", "--human-handoff"} <= options
    assert not any(word in o for o in options for word in ("final", "force", "auto-submit", "--submit"))


def test_reported_final_click_is_rejected_in_live_mode(tmp_path, as_real_host):
    s = build(tmp_path, FakeTransport(lambda b: ready_result(b, final_submit_clicked=True)))
    r, c = live_queued(s)
    with pytest.raises(ValueError, match="Invalid Browserbase report"):
        live_execute(s, r, c)
    assert status(s, r) == "outcome_unknown" and s.transport.release_calls == ["fake-session-1"]
    no_receipt_and_not_applied(s)


# --- 7. recording / logging settings ------------------------------------------------------------------

def test_live_session_body_turns_recording_and_logging_off():
    creds = BrowserbaseCredentials(SENTINEL_KEY)
    body = session_request_body(creds, [HOST], keep_alive=True, timeout=900, live=True)
    assert body["browserSettings"] == {"solveCaptchas": False, "blockAds": True, "recordSession": False,
                                       "logSession": False, "allowedDomains": [HOST]}
    assert body["keepAlive"] is True and body["userMetadata"] == {"purpose": browserbase_cloud.LIVE_PURPOSE}
    assert LIVE_BROWSER_SETTINGS["recordSession"] is False and LIVE_BROWSER_SETTINGS["logSession"] is False
    synthetic = session_request_body(creds, [HOST])  # synthetic defaults unchanged
    assert synthetic["browserSettings"]["recordSession"] is True
    for bad in (dict(keep_alive=False), dict(domains=[HOST, "cdn.example.test"]), dict(domains=["*.example.test"])):
        with pytest.raises(ValueError):
            session_request_body(creds, bad.get("domains", [HOST]), keep_alive=bad.get("keep_alive", True),
                                 timeout=900, live=True)


def test_live_session_fails_closed_if_settings_could_record(monkeypatch):
    monkeypatch.setitem(browserbase_cloud.LIVE_BROWSER_SETTINGS, "recordSession", True)
    with pytest.raises(ValueError, match="could record or log"):
        session_request_body(BrowserbaseCredentials(SENTINEL_KEY), [HOST], keep_alive=True, timeout=900, live=True)


def test_run_in_cloud_refuses_live_without_handoff_or_with_other_domains_before_any_api_call(tmp_path):
    never = lambda *a: pytest.fail("API must not be called")  # noqa: E731
    creds = BrowserbaseCredentials(SENTINEL_KEY)
    with pytest.raises(ValueError):
        run_in_cloud(object(), creds=creds, serve=None, allowed_domains=[HOST], screenshot_dir=tmp_path,
                     playwright_factory=object, post=never, live_host=HOST)
    with pytest.raises(ValueError):
        run_in_cloud(object(), creds=creds, serve=None, allowed_domains=["example.test"], screenshot_dir=tmp_path,
                     playwright_factory=object, post=never, keep_alive=True, review_seconds=600, live_host=HOST)


# --- 8. the real runner in live mode (local Chrome, synthetic pages) ----------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_runner_offline import CHROME_ARGS, SYNTHETIC_ANSWERS  # noqa: E402
from jobintel.submission.browserbase_runner import load_bundle, run  # noqa: E402


@pytest.fixture
def browser():
    """Local headless Chrome whose resolver answers NOTFOUND for every name: a redirect the
    browser follows on its own (not routable) can never reach the network."""
    with sync_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch(channel="chrome", headless=True,
                                         args=[*CHROME_ARGS, "--host-resolver-rules=MAP * ~NOTFOUND"])
        except Exception as exc:  # pragma: no cover - environment without local Chrome
            pytest.skip(f"local Chrome unavailable: {exc}")
        yield instance
        instance.close()


class LiveServer:
    """Serves the fixture on the one authorized host; anything else is recorded and aborted."""

    def __init__(self):
        self.requests, self.external = [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname != HOST:
            self.external.append(request.url)
            return route.abort()
        self.requests.append((request.method, parts.path))
        if parts.path == "/bounce/apply.html":
            return route.fulfill(status=302, headers={"location": "https://sso.identity.test/start?token=SECRET-NAV-TOKEN"})
        if request.method == "POST" and parts.path == "/autosave/draft":
            return route.fulfill(status=204, body="")
        if request.method != "GET" or not parts.path.endswith("/apply.html"):
            return route.fulfill(status=404, body="not found")
        return route.fulfill(status=200, content_type="text/html; charset=utf-8",
                             body=(FIXTURES / "apply.html").read_text())


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = LiveServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def live_bundle(tmp_path, path="/live/apply.html", query=""):
    sealed = tmp_path / "sealed" / "cv.pdf"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(b"%PDF-1.4 synthetic live-host CV")
    package = ApprovedSubmissionPackage(
        "app:live-1", "job:live-1", f"https://{HOST}{path}{query}", "cv-0123456789ab",
        hashlib.sha256(sealed.read_bytes()).hexdigest(), str(sealed),
        tuple(AnswerSnapshot(f, v, "synthetic", "v1") for f, v in sorted(SYNTHETIC_ANSWERS.items())), "input-hash",
        "auto_prepare", (), company="ExampleCo", role="Graduate Engineer")
    from datetime import datetime, timezone
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    auth = BrowserbaseBundleAuthorization("approval:live", "request:live" + path + query, package.package_hash,
                                          (now + timedelta(days=1)).isoformat(), True, "approved")
    return load_bundle(write_bundle(tmp_path / "bundles", package, auth, clock=lambda: now))


def live_run(tmp_path, server_page, query="", path="/live/apply.html", **kwargs):
    server, page = server_page
    result = run(page, live_bundle(tmp_path, path, query), live_host=HOST, **kwargs)
    assert result.final_submit_clicked is False
    assert server.external == []  # nothing ever reached another host
    assert not any(p.endswith("/submit") for _, p in server.requests)
    return server, page, result


def test_runner_refuses_a_live_host_that_is_not_the_bundle_host(tmp_path, server_page):
    _, page = server_page
    bundle = live_bundle(tmp_path)
    for host in ("login.jobs.example.test", "example.test", "JOBS.EXAMPLE.TEST"):
        with pytest.raises(ValueError, match="does not match"):
            run(page, bundle, live_host=host)
    assert page.url == "about:blank"


def test_exact_host_page_is_filled_and_stops_before_submit(tmp_path, server_page):
    server, _, result = live_run(tmp_path, server_page)
    assert result.status == "ready_for_human_review" and result.final_submit_seen is True
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email"]
    assert result.live_host == HOST and result.blocked_requests == []
    assert [m for m, _ in server.requests if m != "GET"] == []


def test_http_redirect_to_another_host_fills_nothing(tmp_path, server_page):
    # Playwright does not route the redirected request, so this is caught by the host re-check.
    _, page, result = live_run(tmp_path, server_page, path="/bounce/apply.html")
    assert result.status == "aborted_wrong_job" and result.stop_reason == "host_drift"
    assert result.fields_filled == [] and result.controls_seen == 0
    assert urlsplit(page.url).hostname != HOST
    assert "SECRET-NAV-TOKEN" not in json.dumps(result.__dict__, default=str)


@pytest.mark.parametrize("mode,host,path", [("offhost", "sso.identity.test", "/login"),
                                            ("subdomain", f"login.{HOST}", "/apply")])
def test_navigation_off_the_exact_host_mid_form_stops(tmp_path, server_page, mode, host, path):
    _, page, result = live_run(tmp_path, server_page, query=f"?mode={mode}")
    assert result.status == "aborted_wrong_job" and result.stop_reason == "host_drift_blocked"
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name"]  # stopped at the next step
    assert result.blocked_requests == [{"kind": "navigation", "method": "GET", "host": host, "path": path,
                                        "resource_type": "document"}]
    assert urlsplit(page.url).hostname != host  # aborted: the other host was never loaded
    assert "SECRET-NAV-TOKEN" not in json.dumps(result.__dict__, default=str)


def test_unknown_post_is_blocked_with_minimal_diagnostics_and_stops_immediately(tmp_path, server_page):
    server, _, result = live_run(tmp_path, server_page, query="?mode=collect")
    assert result.status == "needs_human_input" and result.stop_reason == "unexpected_write_blocked"
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name"]
    assert result.blocked_requests == [{"kind": "write", "method": "POST", "host": HOST, "path": "/collect",
                                        "resource_type": "fetch"}]
    assert ("POST", "/collect") not in server.requests
    assert SYNTHETIC_ANSWERS["legal_first_name"] not in json.dumps(result.blocked_requests)


def test_unlisted_autosave_is_blocked(tmp_path, server_page):
    server, _, result = live_run(tmp_path, server_page, query="?mode=autosave")
    assert result.stop_reason == "unexpected_write_blocked" and ("POST", "/autosave/draft") not in server.requests


def test_explicitly_allowed_benign_autosave_passes(tmp_path, server_page):
    server, _, result = live_run(tmp_path, server_page, query="?mode=autosave", write_allowlist=[AUTOSAVE])
    assert result.status == "ready_for_human_review" and result.blocked_requests == []
    assert {(w["method"], w["url"]) for w in result.allowed_writes} == {AUTOSAVE}
    assert {m for m, p in server.requests if m != "GET"} == {"POST"}
    assert {p for m, p in server.requests if m == "POST"} == {"/autosave/draft"}


def test_attempted_final_submit_is_blocked(tmp_path, server_page):
    server, _, result = live_run(tmp_path, server_page, query="?mode=autosubmit")
    assert result.status == "needs_human_input" and result.stop_reason == "submission_attempt_blocked"
    assert "form.requestSubmit()" in result.blocked_submissions
    assert [m for m, _ in server.requests if m != "GET"] == []


def test_urls_after_the_verified_start_page_drop_query_and_fragment(tmp_path, server_page):
    _, _, result = live_run(tmp_path, server_page, query="?ref=SECRET-QUERY")
    assert result.status == "ready_for_human_review"
    assert result.start_identity["url"] == f"https://{HOST}/live/apply.html?ref=SECRET-QUERY"  # == approved URL
    assert result.current_url == f"https://{HOST}/live/apply.html"
    assert "SECRET-QUERY" not in json.dumps(result.evidence)


# --- 9. live cloud transport against a local CDP endpoint (fake Browserbase API) ----------------------

from test_browserbase_cloud_offline import FakeApi, local_cdp  # noqa: E402,F401  (fixture)


def test_live_cloud_transport_records_nothing_and_keeps_minimal_evidence(tmp_path, local_cdp, as_real_host):  # noqa: F811
    from test_browserbase_runner_offline import Server
    api, server = FakeApi(local_cdp), Server()

    def get(url, headers, timeout):
        if "/debug" in url:
            return {"debuggerFullscreenUrl": LIVE_URL, "pages": [{"url": CLOUD_URL}]}
        return {"status": "RUNNING"}

    s = build(tmp_path)
    shots = tmp_path / "shots"
    s.svc._browserbase_transport = CloudTransport(serve=server.handle, screenshot_dir=shots, post=api, get=get,
                                                  environ={"BROWSERBASE_API_KEY": SENTINEL_KEY})
    r, c = live_queued(s)
    out = live_execute(s, r, c, review_seconds=600)
    # Live host: the CV is left for the human, so the run is reviewable but never "ready" on its own.
    assert out["status"] == "needs_human_input" and out["handoff"] == "active"
    assert out["final_submit_clicked"] is False and out["live_view_url"] == LIVE_URL
    [nhi] = events(s, r, "needs_human_input")
    assert nhi["result"]["unanswered_fields"] == ["CV/resume upload requires human action during handoff."]
    [create] = [call for call in api.calls if call["url"].endswith("/sessions")]
    assert create["body"]["browserSettings"]["recordSession"] is False
    assert create["body"]["browserSettings"]["logSession"] is False
    assert create["body"]["browserSettings"]["allowedDomains"] == [HOST] and create["body"]["keepAlive"] is True
    assert not any(call["url"].endswith("/local-cdp-session-1") for call in api.calls)  # kept for review, not released
    [evidence] = events(s, r, "browserbase_evidence")
    assert evidence["replay_url"] == "" and evidence["screenshot_path"] == "" and evidence["screenshot_sha256"] == ""
    form = evidence["live"].pop("form")
    assert (form["cv_control_detected"], form["cv_upload_status"], form["cv_upload_attempted"]) == (
        True, "human_required", False)
    assert evidence["live"] == {"host": HOST, "recording": False, "browser_logging": False,
                                "screenshot": "not_captured", "stop_reason": "final_page_reached_stop_before_submit",
                                "blocked_requests": [], "allowed_requests": [], "nonfatal_blocked_requests": []}
    assert form["controls_seen_count"] == len(form["controls_detected"]) > 0 and form["fields_filled"]
    assert all(set(f) == {"field", "page_label"} for f in form["fields_filled"])
    assert evidence["identity_state"] == "verified"
    assert not shots.exists() or not any(shots.iterdir())
    assert [x for x in server.requests if x[0] != "GET" or x[1].endswith("/submit")] == [] and server.external == []
    blob = store_bytes(s)
    assert SENTINEL_KEY.encode() not in blob and LIVE_URL.encode() not in blob
    no_receipt_and_not_applied(s)


# --- 10. CLI --------------------------------------------------------------------------------------------

def test_cli_live_mode_requires_every_explicit_flag(tmp_path, capsys, as_real_host, monkeypatch):
    from jobintel.cli.main import main
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    s = build(tmp_path)
    approval = approve(s)

    def cli(*args):
        with patch("sys.argv", ["jobintel", *args, "--store", str(s.root)]):
            main()
        return json.loads(capsys.readouterr().out)

    def cli_error(*args):
        with patch("sys.argv", ["jobintel", *args, "--store", str(s.root)]), pytest.raises(SystemExit):
            main()
        return capsys.readouterr().err

    assert "live execution is not enabled" in cli_error("submission-enqueue", "--approval-id", approval["id"],
                                                        "--executor", "browserbase")
    assert "exactly equal" in cli_error("submission-enqueue", "--approval-id", approval["id"], "--executor",
                                        "browserbase", "--live-host", f"login.{HOST}")
    r = cli("submission-enqueue", "--approval-id", approval["id"], "--executor", "browserbase", "--live-host", HOST)
    assert r["live_host"] == HOST
    c = cli("submission-claim", "--request-id", r["id"], "--worker", "cli")
    base = ["submission-execute", "--request-id", r["id"], "--claim-token", c["claim_token"]]
    assert "explicit live-host opt-in" in cli_error(*base, "--human-handoff")
    assert "explicit live-host opt-in" in cli_error(*base, "--human-handoff", "--live-host", HOST)
    assert "requires human handoff" in cli_error(*base, "--allow-live-host", "--live-host", HOST)
    assert "BROWSERBASE_API_KEY is not set" in cli_error(*base, "--human-handoff", "--allow-live-host",
                                                         "--live-host", HOST, "--live-write-allow", *AUTOSAVE)
    assert "handoff_started" not in kinds(s, r) and "browserbase_live_host_authorized" not in kinds(s, r)


def test_validate_live_host_is_exact():
    assert validate_live_host(HOST, f"https://{HOST}/a?b=c") == HOST
    with pytest.raises(ValueError, match="wildcards"):
        validate_live_host("*", f"https://{HOST}/a")
