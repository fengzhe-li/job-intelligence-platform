"""Explicit per-run non-fatal blocked telemetry, and a stop reason that is never hidden (offline).

A third-party RUM beacon (e.g. POST browser-intake-datadoghq.com) stopped a real run before the
first control scan. An exact (method, host) entry now keeps such a write BLOCKED and RECORDED --
it never leaves the browser -- without stopping the run. Everything else stays fatal: the same
host without the entry, another method or host, any unauthorized application-host write, and
unknown or final GraphQL mutations. A runner-level stop is reported as an explicit escalation so
the importer's generic `cv_upload_unconfirmed` fallback can never stand in for it.

Every host is reserved or intercepted; the browser resolves no names, so nothing leaves the machine.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from jobintel.submission import browserbase_runner
from jobintel.submission.browserbase_runner import validate_nonfatal_blocked_writes
from test_browserbase_graphql_and_identity import APP, ENDPOINT, HOST, no_secret

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

RUM_HOST = "browser-intake-datadoghq.com"
RUM = ("POST", RUM_HOST)
SET = (ENDPOINT, "ApiSetFormValue")


# --- policy (pure) ---------------------------------------------------------------------------------

def test_nonfatal_entries_are_exact_write_methods_on_third_party_hosts():
    assert validate_nonfatal_blocked_writes(APP, [RUM]) == frozenset({RUM})
    for bad in ([("GET", RUM_HOST)], [("post", RUM_HOST)], [("POST", "*.datadoghq.com")], [("POST", "datadoghq")],
                [("POST", "Browser-Intake-Datadoghq.com")], [("POST", "https://browser-intake-datadoghq.com")],
                [("POST", "browser-intake-datadoghq.com:443")], [("POST", "192.0.2.1")], [("POST", "")],
                [("POST", HOST)], [("POST", "api." + HOST)], [("POST", "example.test")], [RUM, RUM],
                [("POST", f"h{i}.analytics.test") for i in range(browserbase_runner.MAX_NONFATAL_BLOCKED_WRITES + 1)]):
        with pytest.raises(ValueError):
            validate_nonfatal_blocked_writes(APP, bad)


def test_stop_escalation_names_the_runner_stop_and_nothing_else():
    stopped = browserbase_runner.RunResult(status="needs_human_input", stop_reason="unexpected_write_blocked")
    assert [e["question"] for e in browserbase_runner._stop_escalation(stopped)] == [
        "Runner stopped: unexpected_write_blocked"]
    for quiet in (browserbase_runner.RunResult(status="ready_for_human_review",
                                               stop_reason="final_page_reached_stop_before_submit"),
                  browserbase_runner.RunResult(status="needs_human_input", stop_reason="questions_escalated")):
        assert browserbase_runner._stop_escalation(quiet) == []


# --- the real runner, live-host mode, local Chrome --------------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser, live_bundle  # noqa: E402,F401  (fixture)

FIXTURE = Path(__file__).parent / "fixtures" / "graphql" / "telemetry.html"


class TelemetryServer:
    def __init__(self):
        self.requests, self.external, self.bodies = [], [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname != HOST:
            self.external.append((request.method, parts.hostname))  # would have left the machine
            return route.abort()
        self.requests.append((request.method, parts.path))
        if request.method == "POST":
            self.bodies.append(request.post_data)
            return route.fulfill(status=200, content_type="application/json", body='{"data": {}}')
        if not parts.path.endswith("/telemetry.html"):
            return route.fulfill(status=404, body="not found")
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text())


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = TelemetryServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def live_run(tmp_path, server_page, mode, when="fill", **kwargs):
    server, page = server_page
    bundle = live_bundle(tmp_path, "/graphql/telemetry.html", f"?mode={mode}&when={when}")
    result = browserbase_runner.run(page, bundle, live_host=HOST, graphql_form_writes=[SET], **kwargs)
    # Nothing ever reached another host, the application server got no write, nothing was submitted.
    assert server.external == [] and server.bodies == [] and result.final_submit_clicked is False
    return server, bundle, result


def rum_diagnostic(path="/api/v2/rum"):
    return {"kind": "nonfatal_blocked_write", "method": "POST", "host": RUM_HOST, "path": path,
            "resource_type": "fetch"}


@pytest.mark.parametrize("when", ["load", "fill"])
def test_A_configured_rum_beacon_is_blocked_recorded_and_not_fatal(tmp_path, server_page, when):
    server, bundle, result = live_run(tmp_path, server_page, "rum", when, nonfatal_blocked_writes=[RUM])
    assert result.status == "ready_for_human_review" and result.blocked_requests == []
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email"]  # the runner kept going
    assert result.controls_seen == 2 and result.nonfatal_blocked_requests
    assert all(r == rum_diagnostic() for r in result.nonfatal_blocked_requests)  # no query, body or headers
    persisted = json.dumps({"nonfatal": result.nonfatal_blocked_requests,
                            "form": browserbase_runner.form_diagnostics(result)})
    assert no_secret(persisted) and "ddsource" not in persisted


@pytest.mark.parametrize("when", ["load", "fill"])
def test_B_the_same_host_without_configuration_stays_fatal(tmp_path, server_page, when):
    server, bundle, result = live_run(tmp_path, server_page, "rum", when)
    assert result.status == "needs_human_input" and result.stop_reason == "unexpected_write_blocked"
    assert result.nonfatal_blocked_requests == [] and result.blocked_requests[0]["host"] == RUM_HOST
    assert len(result.fields_filled) <= 1  # stopped at the next step after the beacon


@pytest.mark.parametrize("mode,host,path", [
    ("other", "collector.analytics.test", "/collect"),   # C: another third-party host
    ("rum_put", RUM_HOST, "/api/v2/rum"),                 # C: configured host, other method
])
def test_C_any_other_third_party_write_stays_fatal(tmp_path, server_page, mode, host, path):
    server, bundle, result = live_run(tmp_path, server_page, mode, nonfatal_blocked_writes=[RUM])
    assert result.stop_reason == "unexpected_write_blocked" and result.nonfatal_blocked_requests == []
    assert (result.blocked_requests[0]["host"], result.blocked_requests[0]["path"]) == (host, path)
    assert len(result.fields_filled) == 1


def test_D_an_unauthorized_application_host_write_stays_fatal(tmp_path, server_page):
    server, bundle, result = live_run(tmp_path, server_page, "onhost", nonfatal_blocked_writes=[RUM])
    assert result.stop_reason == "unexpected_write_blocked" and result.nonfatal_blocked_requests == []
    assert (result.blocked_requests[0]["host"], result.blocked_requests[0]["path"]) == (HOST, "/api/collect")


@pytest.mark.parametrize("mode,name", [("submit", "ApiSubmitApplication"), ("unknown", "ApiDoSomething")])
def test_E_final_or_unknown_mutations_stay_fatal_inside_a_fill(tmp_path, server_page, mode, name):
    server, bundle, result = live_run(tmp_path, server_page, mode, nonfatal_blocked_writes=[RUM])
    assert result.stop_reason == "unexpected_write_blocked" and result.nonfatal_blocked_requests == []
    assert (result.blocked_requests[0]["graphql_operation_name"],
            result.blocked_requests[0]["graphql_operation_type"]) == (name, "mutation")


def test_F_the_stop_reason_is_reported_instead_of_the_cv_fallback(tmp_path, server_page):
    from jobintel.submission import stop_only
    server, bundle, result = live_run(tmp_path, server_page, "rum", "fill")  # unconfigured: fatal after identity
    report = browserbase_runner.to_stop_only_report(result, bundle, task_id="t")
    stop_only.validate_schema(report, stop_only.result_schema("browserbase"))
    assert [q["question"] for q in report["questions_escalated"]] == ["Runner stopped: unexpected_write_blocked"]
    assert browserbase_runner.form_diagnostics(result)["stop_reason"] == "unexpected_write_blocked"


# --- through the service: audit, persisted evidence, imported reason ---------------------------------

from test_browserbase_cloud_offline import FakeApi, local_cdp  # noqa: E402,F401  (fixture)
from test_browserbase_live_host import as_real_host  # noqa: E402,F401  (fixture)
from test_browserbase_service import LIVE_URL, SENTINEL_KEY, approve, build, no_receipt_and_not_applied  # noqa: E402


def service_run(tmp_path, local_cdp, mode, **execute_kwargs):  # noqa: F811
    from jobintel.submission.browserbase_cloud import CloudTransport
    api, server = FakeApi(local_cdp), TelemetryServer()

    def get(url, headers, timeout):
        return {"debuggerFullscreenUrl": LIVE_URL, "pages": []} if "/debug" in url else {"status": "RUNNING"}

    s = build(tmp_path, url=f"https://{HOST}/graphql/telemetry.html?mode={mode}&when=fill")
    s.svc._browserbase_transport = CloudTransport(serve=server.handle, screenshot_dir=tmp_path / "shots", post=api,
                                                  get=get, environ={"BROWSERBASE_API_KEY": SENTINEL_KEY})
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="runner")
    out = s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, allow_live_host=True,
                        live_host=HOST, live_graphql_form_writes=[SET], **execute_kwargs)
    ev = {e["kind"]: e["data"] for e in s.svc.store.events() if e["entity_id"] == r["id"]}
    store = b"".join(p.read_bytes() for p in s.root.rglob("*") if p.is_file())
    assert server.external == [] and server.bodies == [] and out["final_submit_clicked"] is False
    return s, r, out, ev, store


def test_service_audits_and_persists_nonfatal_telemetry(tmp_path, local_cdp, as_real_host):  # noqa: F811
    s, r, out, ev, store = service_run(tmp_path, local_cdp, "rum", live_nonfatal_blocked_writes=[RUM])
    audit = ev["browserbase_live_host_authorized"]
    assert (audit["nonfatal_blocked_write_count"], audit["write_allowlist_count"], audit["final_submit_allowed"]) == (
        1, 0, False)
    live = ev["browserbase_evidence"]["live"]
    assert live["blocked_requests"] == [] and live["nonfatal_blocked_requests"]
    assert all(x == rum_diagnostic() for x in live["nonfatal_blocked_requests"])
    assert [f["field"] for f in live["form"]["fields_filled"]] == ["first_name", "email"]
    assert out["status"] == "needs_human_input" and out["handoff"] == "active"  # no CV control here: still a human
    assert no_secret(store) and b"ddsource" not in store and SENTINEL_KEY.encode() not in store
    no_receipt_and_not_applied(s)


def test_service_imported_result_shows_the_real_stop_reason(tmp_path, local_cdp, as_real_host):  # noqa: F811
    s, r, out, ev, store = service_run(tmp_path, local_cdp, "rum")  # not configured: fatal
    unanswered = ev["needs_human_input"]["result"]["unanswered_fields"]
    assert unanswered[0] == "Runner stopped: unexpected_write_blocked" and "cv_upload_unconfirmed" not in unanswered
    assert ev["browserbase_evidence"]["live"]["form"]["stop_reason"] == "unexpected_write_blocked"
    assert out["status"] == "needs_human_input" and out["handoff"] == "active"


def test_invalid_nonfatal_entries_are_refused_before_any_session(tmp_path, as_real_host):  # noqa: F811
    from test_browserbase_live_host import FakeTransport
    from test_browserbase_service import kinds, ready_result
    s = build(tmp_path, FakeTransport(ready_result), url=f"https://{HOST}/graphql/telemetry.html")
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="runner")
    for bad in ([("POST", HOST)], [("POST", "*.datadoghq.com")], [("GET", RUM_HOST)]):
        with pytest.raises(ValueError):
            s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, allow_live_host=True,
                          live_host=HOST, live_nonfatal_blocked_writes=bad)
    assert s.transport.calls == 0 and "handoff_started" not in kinds(s, r)


def test_cli_exposes_the_exact_nonfatal_option():
    import argparse
    from jobintel.submission.cli import add_submission_commands
    parser = argparse.ArgumentParser()
    add_submission_commands(parser.add_subparsers(dest="command"))
    execute = parser._subparsers._group_actions[0].choices["submission-execute"]
    actions = {o: a for a in execute._actions for o in a.option_strings}
    assert actions["--live-nonfatal-blocked-write"].nargs == 2
    assert not any(w in o for o in actions for w in ("final", "force", "submit", "mutation", "auto"))
