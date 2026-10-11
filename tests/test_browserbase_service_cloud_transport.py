"""Browserbase service lifecycle with the REAL CloudTransport class (fake Browserbase API +
local CDP endpoint). Kept in its own module: the transport starts its own Playwright
instance, which cannot share a thread with the module-scoped local `browser` fixture."""
from __future__ import annotations

import json
import logging

import pytest

from jobintel.submission.browserbase_cloud import BrowserbaseSpikeError, CloudTransport
from test_browserbase_cloud_offline import local_cdp  # noqa: F401  (fixture)
from test_browserbase_service import SENTINEL_KEY, build, kinds, no_receipt_and_not_applied, queued, status

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")


def test_cloud_transport_lifecycle_never_persists_credentials(tmp_path, local_cdp, caplog, capsys):
    from test_browserbase_cloud_offline import FakeApi
    from test_browserbase_runner_offline import Server
    caplog.set_level(logging.DEBUG)
    api, server = FakeApi(local_cdp), Server()
    # store under its own directory: the local_cdp fixture uses tmp_path/profile as Chrome's profile
    s = build(tmp_path / "store", CloudTransport(serve=server.handle, environ={"BROWSERBASE_API_KEY": SENTINEL_KEY},
                                                 post=api, screenshot_dir=tmp_path / "shots"))
    r, c = queued(s)
    out = s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert out["status"] == "ready_for_human_review" and out["session_released"] is True
    assert [call["body"].get("status") for call in api.calls] == [None, "REQUEST_RELEASE"]
    blobs = [p.read_bytes() for d in (tmp_path / "store", tmp_path / "shots") for p in d.rglob("*") if p.is_file()]
    assert not any(SENTINEL_KEY.encode() in b or local_cdp.encode() in b for b in blobs)
    text = caplog.text + capsys.readouterr().out + json.dumps(out) + repr(s.svc._browserbase_transport)
    assert SENTINEL_KEY not in text and local_cdp not in text
    no_receipt_and_not_applied(s)


def test_session_creation_failure_is_outcome_unknown(tmp_path):
    def failing_api(url, headers, body, timeout):
        raise BrowserbaseSpikeError("Browserbase API returned HTTP 503")
    s = build(tmp_path, CloudTransport(environ={"BROWSERBASE_API_KEY": SENTINEL_KEY}, post=failing_api))
    r, c = queued(s)
    with pytest.raises(ValueError, match="Executor interrupted"):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert status(s, r) == "outcome_unknown" and "browserbase_session_started" not in kinds(s, r)
    no_receipt_and_not_applied(s)


def test_cdp_failure_is_outcome_unknown_and_session_released(tmp_path):
    from test_browserbase_cloud_offline import FakeApi
    api = FakeApi("ws://127.0.0.1:9/devtools/browser/unreachable")
    s = build(tmp_path, CloudTransport(environ={"BROWSERBASE_API_KEY": SENTINEL_KEY}, post=api))
    r, c = queued(s)
    with pytest.raises(ValueError, match="Executor interrupted"):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert status(s, r) == "outcome_unknown" and "browserbase_session_started" in kinds(s, r)
    assert api.calls[-1]["body"]["status"] == "REQUEST_RELEASE"
    no_receipt_and_not_applied(s)


class FakeApiWithGet:
    """Fake api.browserbase.com with session status and Live View (debug) endpoints."""

    def __init__(self, connect_url):
        self.connect_url, self.calls, self.state = connect_url, [], "RUNNING"
        self.live_url = "https://www.browserbase.example/devtools-fullscreen/inspector.html?token=LIVE-SECRET-" + "e4" * 8

    def post(self, url, headers, body, timeout):
        self.calls.append(("POST", url.split("/v1", 1)[1], json.loads(json.dumps(body))))
        if url.endswith("/sessions"):
            return {"id": "local-cdp-session-9", "status": "RUNNING", "connectUrl": self.connect_url}
        if body.get("status") == "REQUEST_RELEASE":
            self.state = "COMPLETED"
        return {"status": self.state}

    def get(self, url, headers, timeout):
        self.calls.append(("GET", url.split("/v1", 1)[1], None))
        if "/debug" in url:
            return {"debuggerFullscreenUrl": self.live_url, "pages": [{"url": "https://jobs.example.test/cloud/step-2.html"}]}
        return {"status": self.state}


def test_cloud_transport_handoff_keeps_session_and_releases_explicitly(tmp_path, local_cdp, caplog, capsys):
    from test_browserbase_runner_offline import Server
    caplog.set_level(logging.DEBUG)
    api = FakeApiWithGet(local_cdp)
    s = build(tmp_path / "store", CloudTransport(serve=Server().handle, environ={"BROWSERBASE_API_KEY": SENTINEL_KEY},
                                                 post=api.post, get=api.get, screenshot_dir=tmp_path / "shots"))
    r, c = queued(s)
    out = s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, review_seconds=600)

    create = api.calls[0]
    assert create[1] == "/sessions" and create[2]["keepAlive"] is True and create[2]["timeout"] == 300 + 600
    assert not any(call[2] and call[2].get("status") == "REQUEST_RELEASE" for call in api.calls)  # NOT released
    assert out["status"] == status(s, r) == "ready_for_human_review" and out["handoff"] == "active"
    assert out["live_view_url"] == api.live_url and out["final_submit_clicked"] is False
    assert ("GET", "/sessions/local-cdp-session-9/debug?expiresIn=600", None) in api.calls

    persisted = b"".join(p.read_bytes() for d in (tmp_path / "store", tmp_path / "shots") for p in d.rglob("*") if p.is_file())
    for secret in (SENTINEL_KEY, local_cdp, api.live_url, "LIVE-SECRET"):
        assert secret.encode() not in persisted
        assert secret not in caplog.text
    assert SENTINEL_KEY not in capsys.readouterr().out

    # The runner has disconnected; the filled page is still open. Reconnect as a reviewer would:
    # the runner's in-page submit guard is still active in that document.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(local_cdp)
        page = next(pg for ctx in browser.contexts for pg in ctx.pages if pg.url.endswith("/cloud/step-2.html"))
        assert page.eval_on_selector("#ln", "el => el.value") == "Example"  # still filled
        page.evaluate("() => document.querySelector('form').requestSubmit()")
        assert "form.requestSubmit()" in page.evaluate("() => [...window.__jobintelBlockedSubmits]")
        assert page.url.endswith("/cloud/step-2.html")
    released = s.svc.release_handoff(r["id"], session_id="local-cdp-session-9", claim_token=c["claim_token"])
    assert released["released"] is True and released["request_status"] == "ready_for_human_review"
    assert [call for call in api.calls if call[2] and call[2].get("status") == "REQUEST_RELEASE"] == [
        ("POST", "/sessions/local-cdp-session-9", {"status": "REQUEST_RELEASE"})]
    no_receipt_and_not_applied(s)
