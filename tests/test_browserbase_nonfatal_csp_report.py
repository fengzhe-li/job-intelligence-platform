"""Exact non-fatal rule for a third-party CSP report: POST csp.withgoogle.com (offline).

A real local_browser run stopped with `unexpected_write_blocked` right after filling Name: the page
sent POST csp.withgoogle.com/csp/frame-ancestors/<id> (resource type "other", a browser-generated
CSP report). With the exact per-run entry ("POST", "csp.withgoogle.com") that report is still
ABORTED -- it never leaves the browser -- and RECORDED, but the run continues. Nothing else is
widened: the same host without the entry, another method, another Google host (or the parent
domain), any unauthorized application-host write and final/submit mutations all stay fatal.

Every host is reserved or intercepted; the browser resolves no names, so nothing leaves the machine.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from jobintel.submission import browserbase_runner
from jobintel.submission.browserbase_runner import validate_nonfatal_blocked_writes
from test_browserbase_graphql_and_identity import APP, HOST, no_secret

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

CSP_HOST = "csp.withgoogle.com"
CSP = ("POST", CSP_HOST)
CSP_PATH = "/csp/frame-ancestors/38fac9d5b82543fc4729580d18ff2d3d"
RUM = ("POST", "browser-intake-datadoghq.com")
OBSERVED_RULES = (RUM, CSP)  # exactly what the operator helper configures for the real run


# --- policy (pure) ---------------------------------------------------------------------------------

def test_the_csp_entry_is_exact_and_nothing_wider_is_accepted():
    assert validate_nonfatal_blocked_writes(APP, OBSERVED_RULES) == frozenset(OBSERVED_RULES)
    for bad in ([("POST", "*.withgoogle.com")], [("POST", ".withgoogle.com")], [("POST", "CSP.withgoogle.com")],
                [("POST", "csp.withgoogle.com:443")], [("POST", "https://csp.withgoogle.com")],
                [("GET", CSP_HOST)], [("post", CSP_HOST)], [CSP, CSP]):
        with pytest.raises(ValueError):
            validate_nonfatal_blocked_writes(APP, bad)


def test_matching_is_exact_method_and_host_no_suffix_or_parent():
    rules = validate_nonfatal_blocked_writes(APP, OBSERVED_RULES)
    assert ("POST", CSP_HOST) in rules
    for other in (("PUT", CSP_HOST), ("POST", "withgoogle.com"), ("POST", "x.csp.withgoogle.com"),
                  ("POST", "www.google.com"), ("POST", "csp.google.com"), ("POST", HOST)):
        assert other not in rules


# --- the real runner, live-host mode, local Chrome --------------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser, live_bundle  # noqa: E402,F401  (fixture)

FIXTURE = Path(__file__).parent / "fixtures" / "graphql" / "csp_report.html"
SET = (f"https://{HOST}/api/non-user-graphql", "ApiSetFormValue")
REPORT_ONLY = f"img-src 'self'; report-uri https://{CSP_HOST}{CSP_PATH}"


class CspServer:
    """The approved host only. In ?mode=report the fixture gets a report-only CSP naming the CSP host."""

    def __init__(self):
        self.requests, self.external, self.bodies = [], [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname != HOST:
            self.external.append((request.method, parts.hostname))  # would have left the machine
            return route.abort()
        self.requests.append((request.method, parts.path))
        if request.method != "GET":
            self.bodies.append(request.post_data)
            return route.fulfill(status=200, content_type="application/json", body='{"data": {}}')
        if not parts.path.endswith("/csp_report.html"):
            return route.fulfill(status=404, body="not found")
        headers = {"content-type": "text/html; charset=utf-8"}
        if "mode=report" in parts.query:  # only this mode makes the browser itself send the report
            headers["content-security-policy-report-only"] = REPORT_ONLY
        return route.fulfill(status=200, body=FIXTURE.read_text(), headers=headers)


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = CspServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def live_run(tmp_path, server_page, mode, **kwargs):
    server, page = server_page
    bundle = live_bundle(tmp_path, "/graphql/csp_report.html", f"?mode={mode}")
    result = browserbase_runner.run(page, bundle, live_host=HOST, graphql_form_writes=[SET], **kwargs)
    # Nothing reached another host, the application server got no write, nothing was submitted.
    assert server.external == [] and server.bodies == [] and result.final_submit_clicked is False
    assert not any(p.endswith("/submit") for _, p in server.requests)
    return server, bundle, result


def assert_fatal_after_name(result, host, path):
    assert result.status == "needs_human_input" and result.stop_reason == "unexpected_write_blocked"
    assert result.nonfatal_blocked_requests == []
    assert (result.blocked_requests[0]["host"], result.blocked_requests[0]["path"]) == (host, path)
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name"]  # stopped right after Name


@pytest.mark.parametrize("mode,resource_type", [("report", "other"), ("fetch", "fetch")])
def test_A_G_configured_csp_post_is_blocked_recorded_and_the_runner_fills_on(tmp_path, server_page, mode,
                                                                           resource_type):
    server, bundle, result = live_run(tmp_path, server_page, mode, nonfatal_blocked_writes=OBSERVED_RULES)
    assert result.status == "ready_for_human_review" and result.blocked_requests == []
    assert result.stop_reason == "final_page_reached_stop_before_submit"
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email"]  # kept going after Name
    assert result.controls_seen == 2
    assert result.nonfatal_blocked_requests == [{"kind": "nonfatal_blocked_write", "method": "POST",
                                                 "host": CSP_HOST, "path": CSP_PATH, "resource_type": resource_type}]
    persisted = json.dumps({"nonfatal": result.nonfatal_blocked_requests,
                            "form": browserbase_runner.form_diagnostics(result)})
    assert no_secret(persisted) and "csp-report" not in persisted


@pytest.mark.parametrize("rules", [(), (RUM,)])
def test_B_the_same_csp_post_without_the_entry_is_fatal(tmp_path, server_page, rules):
    server, bundle, result = live_run(tmp_path, server_page, "report", nonfatal_blocked_writes=rules)
    assert_fatal_after_name(result, CSP_HOST, CSP_PATH)
    assert result.blocked_requests[0]["method"] == "POST"


def test_C_the_same_host_with_another_method_is_fatal(tmp_path, server_page):
    server, bundle, result = live_run(tmp_path, server_page, "put", nonfatal_blocked_writes=OBSERVED_RULES)
    assert_fatal_after_name(result, CSP_HOST, CSP_PATH)
    assert result.blocked_requests[0]["method"] == "PUT"


@pytest.mark.parametrize("mode,host,path", [("google", "www.google.com", "/csp/report"),
                                            ("parent", "withgoogle.com", "/csp/frame-ancestors/x")])
def test_D_another_google_host_is_fatal(tmp_path, server_page, mode, host, path):
    server, bundle, result = live_run(tmp_path, server_page, mode, nonfatal_blocked_writes=OBSERVED_RULES)
    assert_fatal_after_name(result, host, path)


def test_E_an_unauthorized_application_host_write_is_fatal(tmp_path, server_page):
    server, bundle, result = live_run(tmp_path, server_page, "onhost", nonfatal_blocked_writes=OBSERVED_RULES)
    assert_fatal_after_name(result, HOST, "/api/collect")


def test_F_a_final_submit_mutation_is_fatal(tmp_path, server_page):
    server, bundle, result = live_run(tmp_path, server_page, "submit", nonfatal_blocked_writes=OBSERVED_RULES)
    assert_fatal_after_name(result, HOST, "/api/non-user-graphql")
    assert (result.blocked_requests[0]["graphql_operation_name"],
            result.blocked_requests[0]["graphql_operation_type"]) == ("ApiSubmitApplication", "mutation")


# --- the operational helper configures exactly the two rules -------------------------------------------

HELPER = Path(__file__).resolve().parents[1] / "scratchpad" / "final_transficc_run.py"


@pytest.mark.skipif(not HELPER.exists(), reason="local operational helper not present")
def test_helper_configures_exactly_the_two_nonfatal_rules():
    import importlib.util
    spec = importlib.util.spec_from_file_location("final_transficc_run_csp_under_test", HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    assert helper.NONFATAL_BLOCKED_WRITES == OBSERVED_RULES
    assert [op for _, op in helper.FORM_WRITES] == ["ApiSetFormValue"]
    validate_nonfatal_blocked_writes(f"https://{helper.HOST}/employer/x/application", helper.NONFATAL_BLOCKED_WRITES)
