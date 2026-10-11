"""Teardown routing after the runner has produced its result (third real Ashby run).

The runner's page route handler used to be live while the caller disconnected (handoff) or
closed the browser, so in-flight handler calls were cancelled: CancelledError /
TargetClosedError / "Task was destroyed but it is pending". Now, once the result exists the
guard is "finished" (later writes aborted and NOT recorded, no allowlist applies), and the
caller removes it with `detach_guard` (unroute_all(behavior="wait") + one flush round trip)
before the connection goes away. Synthetic, local Chrome, no network.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from jobintel.submission import browserbase_runner  # noqa: E402
from jobintel.submission.browserbase_cloud import BrowserbaseCredentials, run_in_cloud  # noqa: E402
from jobintel.submission.browserbase_runner import identity_state, run  # noqa: E402
from test_browserbase_cloud_offline import CHROME, FakeApi, local_cdp  # noqa: E402,F401  (fixture)
from test_browserbase_live_host import browser, live_bundle  # noqa: E402,F401  (fixture)

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

HOST = "jobs.example.test"
FIXTURE = Path(__file__).parent / "fixtures" / "teardown" / "apply.html"
READ = (f"https://{HOST}/api/non-user-graphql", "ApiJobPosting")
TEARDOWN_NOISE = ("CancelledError", "TargetClosedError", "Task was destroyed but it is pending",
                  "Future exception was never retrieved", "Route is already handled", "Error occurred in event listener")


class BusyServer:
    """Serves the teardown fixture; counts every non-GET request that actually arrives."""

    def __init__(self):
        self.writes, self.external = [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname != HOST:
            self.external.append(request.url)
            return route.abort()
        if request.method == "GET":
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text())
        self.writes.append(parts.path)
        return route.fulfill(status=200, content_type="application/json", body='{"data": {}}')


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = BusyServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


# --- the runner: nothing after the result is attributed to it, nothing new is authorized ---------

def test_late_blocked_writes_are_aborted_and_not_added_to_the_result(tmp_path, server_page):
    server, page = server_page
    result = run(page, live_bundle(tmp_path, "/teardown/apply.html", "?mode=beacon"), live_host=HOST)
    assert result.status == "needs_human_input" and result.stop_reason == "unexpected_write_blocked"
    assert identity_state(result) == "not_observed" and result.fields_filled == []
    recorded = list(result.blocked_requests)
    page.wait_for_timeout(300)  # the page keeps POSTing after the stop
    assert result.blocked_requests == recorded and server.writes == []  # still aborted, never recorded
    # Same order as run_in_cloud: the test-server route (production has none) goes first, then
    # the guard, so no already-aborted route can fall through to another handler.
    page.context.unroute_all(behavior="wait")
    assert browserbase_runner.detach_guard(page) is True
    page.context.route("**/*", server.handle)
    page.wait_for_timeout(300)  # guard removed: only now does traffic reach the (test) server
    assert server.writes and result.blocked_requests == recorded and result.final_submit_clicked is False


def test_late_allowlisted_reads_are_no_longer_authorized_after_the_result(tmp_path, server_page):
    server, page = server_page
    result = run(page, live_bundle(tmp_path, "/teardown/apply.html", "?mode=read"), live_host=HOST,
                 graphql_reads=[READ])
    assert result.status == "ready_for_human_review" and identity_state(result) == "verified"
    assert result.allowed_writes and result.blocked_requests == []
    allowed, reached = list(result.allowed_writes), len(server.writes)
    assert reached == len(allowed)
    page.wait_for_timeout(300)
    assert result.allowed_writes == allowed and len(server.writes) == reached  # aborted, not recorded


def test_detach_guard_reports_a_gone_page():
    class Gone:
        def unroute_all(self, **kwargs):
            raise RuntimeError("Target page, context or browser has been closed")

    assert browserbase_runner.detach_guard(Gone()) is False


# --- the transport: detach happens after the result and before the disconnect / close --------------

@contextmanager
def recording_factory(order):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        yield p
        order.append("disconnect")


@pytest.mark.parametrize("keep_alive", [True, False])
def test_route_handlers_are_removed_before_the_connection_goes_away(tmp_path, local_cdp, keep_alive):  # noqa: F811
    order, behaviors, server = [], [], BusyServer()
    original_detach, original_unroute = browserbase_runner.detach_guard, sync_api.Page.unroute_all

    def spy_detach(page):
        order.append("detach")
        return original_detach(page)

    def spy_unroute(self, *, behavior=None):
        behaviors.append(behavior)
        return original_unroute(self, behavior=behavior)

    bundle = live_bundle(tmp_path, "/teardown/apply.html", "?mode=beacon")
    kwargs = dict(keep_alive=True, review_seconds=600, live_host=HOST) if keep_alive else {}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(browserbase_runner, "detach_guard", spy_detach)
        mp.setattr(sync_api.Page, "unroute_all", spy_unroute)
        result, evidence = run_in_cloud(bundle, creds=BrowserbaseCredentials("k"), serve=server.handle,
                                        allowed_domains=[HOST], screenshot_dir=tmp_path, post=FakeApi(local_cdp),
                                        playwright_factory=lambda: recording_factory(order), **kwargs)
    assert order == ["detach", "disconnect"] and behaviors == ["wait"] and evidence.guard_detached is True
    assert result.final_submit_clicked is False and result.status == "needs_human_input"
    assert evidence.handoff_kept is keep_alive
    assert server.external == []


def test_intentional_stop_result_is_identical_with_and_without_the_teardown_step(tmp_path, local_cdp):  # noqa: F811
    outcomes = []
    for variant in ("with", "without"):
        bundle = live_bundle(tmp_path / variant, "/teardown/apply.html", "?mode=beacon")
        with pytest.MonkeyPatch.context() as mp:
            if variant == "without":
                mp.setattr(browserbase_runner, "detach_guard", lambda page: False)
            result, evidence = run_in_cloud(bundle, creds=BrowserbaseCredentials("k"), serve=BusyServer().handle,
                                            allowed_domains=[HOST], screenshot_dir=tmp_path, post=FakeApi(local_cdp),
                                            keep_alive=True, review_seconds=600, live_host=HOST)
        outcomes.append((result.status, result.stop_reason, identity_state(result), result.fields_filled,
                         result.final_submit_clicked, evidence.handoff_kept, evidence.live["stop_reason"]))
        assert evidence.live["blocked_requests"] == result.blocked_requests  # evidence = the result at stop time
    assert outcomes[0] == outcomes[1] == ("needs_human_input", "unexpected_write_blocked", "not_observed", [], False,
                                          True, "unexpected_write_blocked")


# --- no uncaught teardown errors (separate process: asyncio prints them at interpreter exit) -------

def probe(mode):
    if not CHROME.exists():
        pytest.skip("local Chrome not installed")
    done = subprocess.run([sys.executable, str(Path(__file__).parent / "browserbase_teardown_probe.py"), str(CHROME), mode],
                          capture_output=True, text=True, timeout=120)
    line = next(l for l in done.stdout.splitlines() if l.startswith("PROBE_RESULT "))
    return json.loads(line.removeprefix("PROBE_RESULT ")), done.stderr


def test_teardown_emits_no_cancelled_or_target_closed_errors():
    result, stderr = probe("fix")
    assert result == {"status": "needs_human_input", "stop_reason": "unexpected_write_blocked",
                      "final_submit_clicked": False, "handoff_kept": True, "guard_detached": True}
    assert not [noise for noise in TEARDOWN_NOISE if noise in stderr], stderr[-2000:]


def test_probe_is_sensitive_the_unfixed_teardown_reproduces_the_errors():
    result, stderr = probe("nofix")
    assert result["status"] == "needs_human_input" and result["guard_detached"] is False  # same intentional stop
    assert "CancelledError" in stderr or "TargetClosedError" in stderr
