"""local_browser executor: the shared stop-only runner in a LOCAL Chromium (offline, synthetic).

Every test goes through SubmissionService with the real LocalBrowserTransport: a detached
Playwright Chromium with a fresh profile under the test store, reached over CDP exactly like a
Browserbase session. The synthetic site is served in-process; every other host is aborted, so
nothing leaves the machine. An autouse fixture removes BROWSERBASE_API_KEY and makes every
Browserbase entry point raise, so each test also proves (I) the Browserbase path is never
invoked and (J) no Browserbase key is needed.

Most runs are headless (fast, no windows); test A launches a real headed window.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from jobintel.submission import browserbase_cloud, local_browser
from jobintel.submission.local_browser import LocalBrowserTransport

sync_api = pytest.importorskip("playwright.sync_api")

from test_browserbase_graphql_and_identity import HOST, no_secret  # noqa: E402
from test_browserbase_graphql_form_write import AutosaveServer  # noqa: E402
from test_browserbase_live_host import as_real_host  # noqa: E402,F401  (fixture)
from test_browserbase_nonfatal_telemetry import RUM, RUM_HOST, SET, TelemetryServer, rum_diagnostic  # noqa: E402
from test_browserbase_runner_offline import Server  # noqa: E402
from test_browserbase_service import CLOUD_URL, approve, build, kinds, no_receipt_and_not_applied, status  # noqa: E402

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")


def _browser_available() -> bool:
    try:
        LocalBrowserTransport(runtime_dir=Path("/nonexistent")).preflight()
        return True
    except Exception:
        return False


if not _browser_available():
    pytest.skip("no local Chrome/Chromium", allow_module_level=True)

# Test-only: the browser resolves NO names, so nothing it does on its own can reach the network.
OFFLINE = ("--host-resolver-rules=MAP * ~NOTFOUND",)


@pytest.fixture(autouse=True)
def no_browserbase(monkeypatch):
    """(I)+(J): no key in the environment and every Browserbase entry point fails loudly."""
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    monkeypatch.delenv("BROWSERBASE_PROJECT_ID", raising=False)
    calls = []

    def forbidden(name):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"Browserbase path invoked: {name}")
        return fail
    for name in ("create_session", "release_session", "get_session_status", "get_live_view", "run_in_cloud",
                 "_urllib_post", "_urllib_get", "load_credentials"):
        monkeypatch.setattr(browserbase_cloud, name, forbidden(name))
    monkeypatch.setattr(browserbase_cloud.CloudTransport, "run", forbidden("CloudTransport.run"))
    monkeypatch.setattr(browserbase_cloud.CloudTransport, "preflight", forbidden("CloudTransport.preflight"))
    yield calls
    assert calls == []


class Run:
    def __init__(self, s, r, c, out, server, transport):
        self.s, self.r, self.c, self.out, self.server, self.transport = s, r, c, out, server, transport

    def events(self, kind):
        return [e["data"] for e in self.s.svc.store.events() if e["entity_id"] == self.r["id"] and e["kind"] == kind]

    @property
    def live(self):
        return self.events("browserbase_evidence")[0]["live"]

    @property
    def session_id(self):
        return self.events("browserbase_session_started")[0]["session_id"]

    def record(self):
        path = self.transport.runtime_dir / "sessions" / f"{self.session_id}.json"
        return json.loads(path.read_text()) if path.exists() else None


@pytest.fixture
def runs():
    """Every browser a test leaves open is closed afterwards, whatever happened."""
    started = []
    yield started
    for run in started:
        for path in (run.transport.runtime_dir / "sessions").glob("local-*.json"):
            run.transport.release(path.stem)


def execute(tmp_path, runs, server, url, *, live=True, headless=True, **execute_kwargs):
    s = build(tmp_path / "store", url=url)
    transport = LocalBrowserTransport(runtime_dir=s.svc.runtime_root / "local_browser", headless=headless,
                                      serve=server.handle, extra_args=OFFLINE)
    s.svc._local_browser_transport = transport
    r = s.svc.enqueue(approve(s)["id"], executor="local_browser", live_host=HOST if live else None)
    c = s.svc.claim(r["id"], worker="local")
    if live:
        execute_kwargs = {"human_handoff": True, "allow_live_host": True, "live_host": HOST,
                          "live_graphql_form_writes": [SET], **execute_kwargs}
    out = s.svc.execute(r["id"], claim_token=c["claim_token"], **execute_kwargs)
    run = Run(s, r, c, out, server, transport)
    runs.append(run)
    # (G) never a final Submit; nothing left the machine.
    assert out["final_submit_clicked"] is False and server.external == []
    assert "live_view_url" not in out
    no_receipt_and_not_applied(s)
    return run


def window_state(run):
    """Attach a SECOND, independent CDP client to the handed-off window: the page the human sees."""
    profile = Path(run.record()["profile_dir"])
    port, path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
    with sync_api.sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"ws://127.0.0.1:{int(port)}{path}")
        page = browser.contexts[0].pages[0]
        state = {"url": page.url, "values": page.eval_on_selector_all("input", "els => els.map(e => e.value)")}
    return state  # leaving `with` only disconnects this client; the window stays


def assert_released(run):
    record = run.record()
    out = run.s.svc.release_handoff(run.r["id"], session_id=run.session_id, claim_token=run.c["claim_token"])
    assert out["released"] is True and run.record() is None
    if record:
        assert not Path(record["profile_dir"]).exists() and run.transport.session_status(run.session_id) == "COMPLETED"


# --- A/B/C/G: a real headed window opens the synthetic form, fills approved fields, never submits ----

@pytest.mark.skipif(os.environ.get("JOBINTEL_SKIP_HEADED") == "1", reason="headed browser disabled")
def test_A_B_C_headed_local_browser_fills_the_synthetic_form(tmp_path, runs):
    server = Server()
    run = execute(tmp_path, runs, server, CLOUD_URL, live=False, headless=False)
    assert run.out["status"] == "ready_for_human_review" and status(run.s, run.r) == "ready_for_human_review"
    assert ("GET", "/cloud/apply.html") in server.requests  # (B) the synthetic form was opened
    assert server.submit_requests == []  # (G)
    result = run.events("browserbase_result")[0]
    assert result["executor"] == "local_browser" and result["final_submit_clicked"] is False
    assert len(result["fields_filled"]) >= 3 and result["cv_upload_confirmed"] is True  # (C) synthetic mode
    evidence = run.events("browserbase_evidence")[0]
    assert evidence["session_id"].startswith("local-") and evidence["released"] is True  # no handoff asked
    assert evidence["screenshot_sha256"] and evidence["identity_state"] == "verified"
    assert run.record() is None and not any((run.transport.runtime_dir / "profiles").iterdir())  # closed + wiped


# --- D: the scoped ApiSetFormValue permission behaves exactly as with Browserbase ---------------------

def test_D_scoped_form_writes_only_inside_fill_windows(tmp_path, runs, as_real_host):  # noqa: F811
    server = AutosaveServer()
    run = execute(tmp_path, runs, server, f"https://{HOST}/graphql/autosave.html?mode=autosave")
    autosaves = [a for a in run.live["allowed_requests"] if a["kind"] == "graphql_form_write"]
    assert run.live["blocked_requests"] == [] and autosaves
    assert all(a["reason"] == "approved_field_autosave" and a["graphql_operation_name"] == "ApiSetFormValue"
               for a in autosaves)
    assert server.operations() == ["ApiSetFormValue"] * len(autosaves)
    assert_released(run)


def test_D_without_the_permission_the_same_autosave_is_fatal(tmp_path, runs, as_real_host):  # noqa: F811
    server = AutosaveServer()
    run = execute(tmp_path, runs, server, f"https://{HOST}/graphql/autosave.html?mode=autosave",
                  live_graphql_form_writes=[])
    assert run.live["stop_reason"] == "unexpected_write_blocked" and server.bodies == []
    assert_released(run)


# --- E/H: configured Datadog telemetry is blocked + recorded + non-fatal; the window stays open ------

def test_E_H_configured_telemetry_is_nonfatal_and_the_window_is_the_handoff(tmp_path, runs, as_real_host):  # noqa: F811
    server = TelemetryServer()
    run = execute(tmp_path, runs, server, f"https://{HOST}/graphql/telemetry.html?mode=rum&when=fill",
                  live_nonfatal_blocked_writes=[RUM])
    assert server.bodies == [] and RUM_HOST not in json.dumps(server.requests)  # never left the browser
    assert run.live["blocked_requests"] == [] and run.live["nonfatal_blocked_requests"]
    assert all(x == rum_diagnostic() for x in run.live["nonfatal_blocked_requests"])
    assert [f["field"] for f in run.live["form"]["fields_filled"]] == ["first_name", "email"]  # kept going
    assert run.live["form"]["cv_upload_status"] in {"human_required", "not_seen"}  # never selected on a real host

    # (H) the browser the automation used is still open, as the runner left it, for the human.
    assert run.out["handoff"] == "local_browser_active" and run.out["session_released"] is False
    assert run.out["status"] == "needs_human_input" and run.out["review_expires_at"]
    assert run.out["page_guard_released"] is True and run.events("browserbase_evidence")[0]["page_guard_released"] is True
    assert run.transport.session_status(run.session_id) == "RUNNING"
    state = window_state(run)
    assert "/graphql/telemetry.html" in state["url"] and any(state["values"])  # filled values still on the page
    handoff = run.events("browserbase_handoff_started")[0]
    assert handoff["executor"] == "local_browser" and handoff["live_view_persisted"] is False
    status_view = run.s.svc.handoff_status(run.r["id"])
    assert status_view["handoff"] == "local_browser_active" and status_view["provider_status"] == "RUNNING"
    assert "live_view_url" not in status_view

    # Closing is refused while the window is open; an explicit release ends it and wipes the profile.
    with pytest.raises(ValueError, match="release it explicitly"):
        run.s.svc.close_not_submitted(run.r["id"], reviewer="human", confirmed_not_submitted=True, reason="test")
    assert_released(run)
    run.s.svc.close_not_submitted(run.r["id"], reviewer="human", confirmed_not_submitted=True, reason="test")
    assert status(run.s, run.r) == "closed_not_submitted"
    assert no_secret(json.dumps(run.events("browserbase_evidence")))


# --- F: everything else stays fatal --------------------------------------------------------------------

@pytest.mark.parametrize("mode,configured,host,path", [
    ("rum", False, RUM_HOST, "/api/v2/rum"),                       # same host, not configured
    ("other", True, "collector.analytics.test", "/collect"),       # another third-party host
    ("onhost", True, HOST, "/api/collect"),                        # unauthorized application-host write
])
def test_F_unauthorized_writes_stay_fatal(tmp_path, runs, as_real_host, mode, configured, host, path):  # noqa: F811
    server = TelemetryServer()
    run = execute(tmp_path, runs, server, f"https://{HOST}/graphql/telemetry.html?mode={mode}&when=fill",
                  live_nonfatal_blocked_writes=[RUM] if configured else [])
    assert run.live["stop_reason"] == "unexpected_write_blocked" and run.live["nonfatal_blocked_requests"] == []
    assert (run.live["blocked_requests"][0]["host"], run.live["blocked_requests"][0]["path"]) == (host, path)
    imported = run.s.svc.store.requests()[run.r["id"]]["result"]["unanswered_fields"]
    assert imported[0] == "Runner stopped: unexpected_write_blocked"
    assert_released(run)


@pytest.mark.parametrize("mode,name", [("submit", "ApiSubmitApplication"), ("unknown", "ApiDoSomething")])
def test_F_G_final_or_unknown_mutations_stay_fatal(tmp_path, runs, as_real_host, mode, name):  # noqa: F811
    server = TelemetryServer()
    run = execute(tmp_path, runs, server, f"https://{HOST}/graphql/telemetry.html?mode={mode}&when=fill",
                  live_nonfatal_blocked_writes=[RUM])
    assert run.live["stop_reason"] == "unexpected_write_blocked"
    assert (run.live["blocked_requests"][0]["graphql_operation_name"],
            run.live["blocked_requests"][0]["graphql_operation_type"]) == (name, "mutation")
    assert_released(run)


# --- lifecycle / safety --------------------------------------------------------------------------------

def test_launch_failure_is_outcome_unknown_with_a_safe_reason(tmp_path, runs):
    s = build(tmp_path / "store", url=CLOUD_URL)
    s.svc._local_browser_transport = LocalBrowserTransport(runtime_dir=s.svc.runtime_root / "local_browser",
                                                           executable="/usr/bin/false", headless=True)
    r = s.svc.enqueue(approve(s)["id"], executor="local_browser")
    c = s.svc.claim(r["id"], worker="local")
    with pytest.raises(ValueError, match="Executor interrupted") as info:
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert info.value.failure == {"stage": "local_browser_launch", "category": "local_browser_launch_failed",
                                  "exception_type": "LocalBrowserError"}
    assert status(s, r) == "outcome_unknown" and "browserbase_session_started" not in kinds(s, r)
    with pytest.raises(ValueError):  # never retried
        s.svc.execute(r["id"], claim_token=c["claim_token"])


def test_profile_is_fresh_isolated_and_inside_the_ignored_runtime_tree(tmp_path):
    transport = LocalBrowserTransport(runtime_dir=tmp_path / "data/runtime/local_browser", headless=True,
                                      extra_args=OFFLINE)
    session_id, proc, profile, endpoint = transport._launch()
    try:
        assert profile.parent == transport.runtime_dir / "profiles" and oct(profile.stat().st_mode)[-3:] == "700"
        assert all(oct(d.stat().st_mode)[-3:] == "700" for d in (transport.runtime_dir, profile.parent,
                                                                   transport.runtime_dir / "sessions"))
        assert endpoint.startswith("ws://127.0.0.1:")
        command = local_browser._command_line(proc.pid)
        assert f"--user-data-dir={profile}" in command and "--disable-extensions" in command
        assert "Application Support/Google/Chrome" not in command  # never the user's own Chrome profile
        assert transport.session_status(session_id) == "RUNNING"
    finally:
        transport.release(session_id)
    assert not profile.exists() and transport.session_status(session_id) == "COMPLETED"
    with pytest.raises(ValueError):
        transport.session_status("local-../../etc")


def test_store_and_runtime_trees_are_gitignored():
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text().splitlines()
    assert "data/local/" in ignore and "data/runtime/" in ignore


def test_cli_offers_the_local_browser_executor():
    import argparse
    from jobintel.submission.cli import add_submission_commands
    parser = argparse.ArgumentParser()
    add_submission_commands(parser.add_subparsers(dest="command"))
    enqueue = parser._subparsers._group_actions[0].choices["submission-enqueue"]
    choices = next(a.choices for a in enqueue._actions if "--executor" in a.option_strings)
    assert "local_browser" in choices and "browserbase" in choices


# --- human handoff: the guard is released, the HUMAN can submit without a reload ----------------------

HANDOFF_FIXTURE = Path(__file__).parent / "fixtures" / "graphql" / "handoff_submit.html"


class HandoffServer:
    """Serves the SPA-style form. Every request it sees is recorded: after handoff it must see none."""

    def __init__(self):
        self.requests, self.external, self.bodies = [], [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname != HOST:
            self.external.append(request.url)
            return route.abort()
        self.requests.append((request.method, parts.path))
        if request.method != "GET":
            self.bodies.append(request.post_data)
            return route.fulfill(status=200, content_type="application/json", body='{"data": {}}')
        if not parts.path.endswith("/handoff_submit.html"):
            return route.fulfill(status=404, body="not found")
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HANDOFF_FIXTURE.read_text())


def human(run, action):
    """A SECOND, independent CDP client acting on the handed-off window, like the human would."""
    profile = Path(run.record()["profile_dir"])
    port, path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
    with sync_api.sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"ws://127.0.0.1:{int(port)}{path}")
        page = next(pg for ctx in browser.contexts for pg in ctx.pages if "/handoff_submit.html" in pg.url)
        return action(page)


PAGE_STATE = """() => ({
  submitted: document.body.dataset.submitted, forged: document.body.dataset.forged || null,
  blocked: [...(window.__jobintelBlockedSubmits || [])],
  native: [HTMLFormElement.prototype.submit, HTMLFormElement.prototype.requestSubmit, Element.prototype.attachShadow]
    .every((f) => f.toString().includes('[native code]')),
  filled: [...document.querySelectorAll('input')].every((el) => el.value !== ''),
})"""


def test_B_C_D_E_human_can_submit_after_handoff_without_reload(tmp_path, runs, as_real_host):  # noqa: F811
    server = HandoffServer()
    run = execute(tmp_path, runs, server, f"https://{HOST}/graphql/handoff_submit.html")
    # (D) the automation never submitted: nothing was clicked, no write reached the server.
    assert run.out["final_submit_clicked"] is False and server.bodies == []
    assert run.events("browserbase_result")[0]["final_submit_clicked"] is False
    assert [f["field"] for f in run.live["form"]["fields_filled"]] == ["first_name", "email"]
    # (B) at handoff the guard and the interception are gone; the browser is still open (E).
    assert run.out["handoff"] == "local_browser_active" and run.out["page_guard_released"] is True
    assert run.events("browserbase_evidence")[0]["page_guard_released"] is True
    assert run.transport.session_status(run.session_id) == "RUNNING"
    seen_by_automation = list(server.requests)

    before = human(run, lambda page: page.evaluate(PAGE_STATE))
    assert before == {"submitted": "0", "forged": None, "blocked": [], "native": True, "filled": True}

    def click_submit(page):
        page.click("#final")  # a real mouse click on the final Submit control, no reload
        page.wait_for_function("() => document.body.dataset.submitted === '1'", timeout=5000)
        # No route handler is left: the page's own POST goes to the (offline) network and fails
        # promptly instead of being served, aborted or paused by the automation.
        network = page.evaluate("() => fetch('/api/non-user-graphql', {method: 'POST', body: '{}'})"
                                ".then((r) => 'served:' + r.status, () => 'network_error')")
        return page.evaluate(PAGE_STATE), network, page.url
    after, network, url = human(run, click_submit)
    # (C) the human's click ran the page's own submit handler; the guard recorded nothing.
    assert after["submitted"] == "1" and after["blocked"] == [] and after["native"] is True
    assert network == "network_error" and url.endswith("/graphql/handoff_submit.html")
    assert server.requests == seen_by_automation and server.bodies == []  # automation intercepts nothing now
    assert human(run, lambda page: page.evaluate("() => window.__jobintelReleaseGuard('x'.repeat(64))")) is False
    assert_released(run)


def test_A_automation_cannot_submit_and_the_page_cannot_release_the_guard(tmp_path, runs, as_real_host):  # noqa: F811
    server = HandoffServer()
    run = execute(tmp_path, runs, server, f"https://{HOST}/graphql/handoff_submit.html?auto=1")
    # While the runner filled, the page tried to release the guard (forged token) and submit: both refused.
    assert run.live["stop_reason"] == "submission_attempt_blocked" and run.out["final_submit_clicked"] is False
    assert server.bodies == [] and not any(m == "POST" for m, _ in server.requests)
    state = human(run, lambda page: page.evaluate(PAGE_STATE))
    assert state["submitted"] == "0" and state["forged"] == "false" and "form.requestSubmit()" in state["blocked"]
    # Only after the run was over did the runner itself release the guard for the human.
    assert run.out["page_guard_released"] is True and state["native"] is True
    assert_released(run)


def test_F_the_guard_is_only_releasable_with_a_per_run_secret():
    from jobintel.submission import browserbase_runner
    assert "const releaseToken = null;" in browserbase_runner.guard_script(None)  # Browserbase: never releasable
    token = "a" * 64
    assert f'const releaseToken = "{token}";' in browserbase_runner.guard_script(token)
    for bad in ("", "short", "x" * 31, "a" * 40 + '"; alert(1); "', 123):
        with pytest.raises(ValueError):
            browserbase_runner.guard_script(bad)


def test_F_browserbase_transport_never_asks_for_a_guard_release():
    import inspect
    source = Path(browserbase_cloud.__file__).read_text()  # the real module (the autouse fixture stubs its functions)
    assert "def run_in_cloud" in source and "browser_session.run_connected(" in source
    assert "release_guard_on_handoff" not in source  # default False: the Browserbase guard stays as before
    assert "release_guard_on_handoff=True" in inspect.getsource(local_browser.LocalBrowserTransport.run)


# --- the operational helper, end to end on the synthetic site ------------------------------------------

HELPER = Path(__file__).resolve().parents[1] / "scratchpad" / "final_transficc_run.py"


@pytest.mark.skipif(not HELPER.exists(), reason="local operational helper not present")
def test_helper_runs_local_browser_and_prints_only_safe_fields(tmp_path, runs, as_real_host, monkeypatch, capsys):  # noqa: F811
    import importlib.util
    from test_browserbase_graphql_and_identity import ENDPOINT
    spec = importlib.util.spec_from_file_location("final_transficc_run_local_under_test", HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    server = HandoffServer()
    s = build(tmp_path / "store", url=f"https://{HOST}/graphql/handoff_submit.html")
    transport = LocalBrowserTransport(runtime_dir=s.svc.runtime_root / "local_browser", headless=True,
                                      serve=server.handle, extra_args=OFFLINE)
    s.svc._local_browser_transport = transport
    r = s.svc.enqueue(approve(s)["id"], executor="local_browser", live_host=HOST)
    for name, value in (("REQUEST", r["id"]), ("HOST", HOST), ("READS", ((ENDPOINT, "ApiJobPosting"),)),
                        ("FORM_WRITES", ((ENDPOINT, "ApiSetFormValue"),)),
                        ("NONFATAL_BLOCKED_WRITES", (("POST", RUM_HOST),))):
        monkeypatch.setattr(helper, name, value)

    summary = helper.run(s.svc)
    claimed = s.svc.store.requests()[r["id"]]
    run = Run(s, r, {"claim_token": claimed["claim_token"]}, {}, server, transport)
    runs.append(run)
    assert claimed["worker"] == "fengzhe-local-browser"
    assert (summary["handoff"], summary["page_guard_released"], summary["browser_process_state"]) == (
        "local_browser_active", True, "open")
    assert summary["final_submit_clicked"] is False and summary["identity_state"] == "verified"
    assert [f["field"] for f in summary["fields_filled"]] == ["first_name", "email"]
    assert summary["error_category"] is None and summary["blocked_fatal_operations"] == []
    printed = json.dumps(summary) + capsys.readouterr().out
    assert claimed["claim_token"] not in printed and "ws://" not in printed and "devtools" not in printed
    values = human(run, lambda page: page.eval_on_selector_all("input", "els => els.map(e => e.value)"))
    assert values and not any(v and v in printed for v in values)  # no form value printed
    with pytest.raises(SystemExit):  # never a second claim/run of the same request
        helper.run(s.svc)
    assert_released(run)


# --- delayed (JS-rendered) form: the runner waits, fills, and still hands the window over ---------------

def test_I_delayed_render_is_filled_and_the_window_stays_open(tmp_path, runs, as_real_host):  # noqa: F811
    from test_browserbase_render_wait import DelayedServer
    server = DelayedServer()
    run = execute(tmp_path, runs, server, f"https://{HOST}/graphql/delayed.html?mode=rum",
                  live_nonfatal_blocked_writes=[RUM])
    assert run.live["stop_reason"] != "no_safe_navigation" and run.live["blocked_requests"] == []
    assert run.live["form"]["controls_seen_count"] == 2 and run.live["nonfatal_blocked_requests"]
    assert [f["field"] for f in run.live["form"]["fields_filled"]] == ["first_name", "email"]
    assert run.out["handoff"] == "local_browser_active" and run.out["page_guard_released"] is True
    assert run.transport.session_status(run.session_id) == "RUNNING" and server.bodies == []
    assert_released(run)


# --- a browser-generated CSP report right after Name: recorded, aborted, the run fills on ----------------

def test_csp_report_after_name_is_nonfatal_only_with_its_exact_rule(tmp_path, runs, as_real_host):  # noqa: F811
    from test_browserbase_nonfatal_csp_report import CSP_HOST, CSP_PATH, OBSERVED_RULES, CspServer
    url = f"https://{HOST}/graphql/csp_report.html?mode=report"
    run = execute(tmp_path / "configured", runs, CspServer(), url, live_nonfatal_blocked_writes=OBSERVED_RULES)
    assert run.live["blocked_requests"] == [] and run.live["stop_reason"] != "unexpected_write_blocked"
    assert [(r["method"], r["host"], r["path"]) for r in run.live["nonfatal_blocked_requests"]] == [
        ("POST", CSP_HOST, CSP_PATH)]
    assert [f["field"] for f in run.live["form"]["fields_filled"]] == ["first_name", "email"]
    assert run.out["handoff"] == "local_browser_active" and run.out["page_guard_released"] is True
    assert run.server.bodies == []
    assert_released(run)

    plain = execute(tmp_path / "datadog_only", runs, CspServer(), url, live_nonfatal_blocked_writes=[RUM])
    assert plain.live["form"]["stop_reason"] == "unexpected_write_blocked"
    assert [(r["method"], r["host"]) for r in plain.live["blocked_requests"]] == [("POST", CSP_HOST)]
    assert [f["field"] for f in plain.live["form"]["fields_filled"]] == ["first_name"]
    assert_released(plain)


# --- H: the ephemeral runtime never travels with data/local ---------------------------------------------

def _store(tmp_path):
    from jobintel.submission.service import SubmissionService
    root = tmp_path / "data" / "local"
    (root / "submissions").mkdir(parents=True)
    (root / "submissions" / "events.jsonl").write_text("{}\n")
    return SubmissionService(root, tmp_path / "config.json")


def test_H_service_runtime_lives_outside_the_store_and_copies_ignore_live_profiles(tmp_path):
    import shutil
    from jobintel.dedup.repair import backup_store
    svc = _store(tmp_path)
    default = svc._browserbase_runtime("local_browser")[0]
    assert default.runtime_dir == tmp_path / "data" / "runtime" / "local_browser"
    assert not default.runtime_dir.is_relative_to(svc.root)
    assert default.legacy_runtime_dirs == (svc.root / "submissions" / "local_browser",)
    live = LocalBrowserTransport(runtime_dir=default.runtime_dir, headless=True, extra_args=OFFLINE)
    session_id, proc, profile, endpoint = live._launch()
    try:
        assert live.session_status(session_id) == "RUNNING"
        shutil.copytree(svc.root, tmp_path / "plain-copy")  # a live profile can no longer break this
        backup_store(svc.root, tmp_path / "backup")
        assert not any("profiles" in str(p) for p in (tmp_path / "backup").rglob("*"))
    finally:
        live.release(session_id)
    assert not profile.exists()


def test_H_legacy_live_profile_is_skipped_by_copies_and_still_releasable(tmp_path):
    import shutil
    from jobintel.dedup.repair import backup_store
    from jobintel.storage.ephemeral import ignore_ephemeral
    svc = _store(tmp_path)
    legacy = LocalBrowserTransport(runtime_dir=svc.root / "submissions" / "local_browser", headless=True,
                                   extra_args=OFFLINE)  # where earlier versions kept profiles
    session_id, proc, profile, endpoint = legacy._launch()
    try:
        backup_store(svc.root, tmp_path / "backup")
        shutil.copytree(svc.root, tmp_path / "fixture-copy", ignore=ignore_ephemeral)
        for copy in (tmp_path / "backup", tmp_path / "fixture-copy"):
            assert (copy / "submissions" / "events.jsonl").exists()
            assert not (copy / "submissions" / "local_browser" / "profiles").exists()
            assert not (copy / "submissions" / "local_browser" / "sessions").exists()
        # The service's default transport (new runtime dir) still finds and releases it.
        default = svc._browserbase_runtime("local_browser")[0]
        assert default.session_status(session_id) == "RUNNING"
        default.release(session_id)
        assert default.session_status(session_id) == "COMPLETED" and not profile.exists()
    finally:
        legacy.release(session_id)


def test_H_a_record_pointing_outside_its_runtime_dir_is_refused(tmp_path):
    transport = LocalBrowserTransport(runtime_dir=tmp_path / "runtime", headless=True)
    session_id = "local-" + "0" * 32
    (tmp_path / "runtime" / "sessions").mkdir(parents=True)
    (tmp_path / "runtime" / "sessions" / f"{session_id}.json").write_text(json.dumps(
        {"session_id": session_id, "pid": os.getpid(), "profile_dir": str(tmp_path / "elsewhere")}))
    for call in (transport.session_status, transport.release):
        with pytest.raises(ValueError, match="does not match"):
            call(session_id)
