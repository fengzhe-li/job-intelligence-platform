"""Narrow GraphQL form-value mutation permission (per-field autosave), synthetic and offline.

An exact (endpoint, operationName) form-write entry makes ONE named mutation eligible; the
runner's guard lets it through only while an approved-field-fill window is open (fill + blur +
settle of a control whose value comes from the sealed package). Page load, idle time, Next /
Continue, the final page and teardown run with the window closed, so the same mutation stays
blocked there. Only endpoint/operation metadata, the reason and the canonical field key are kept.

Every host is reserved; the browser resolves no names, so nothing leaves the machine.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from jobintel.submission import browserbase_runner, graphql_policy
from test_browserbase_graphql_and_identity import APP, ENDPOINT, HOST, READ, SECRETS, body, no_secret

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

SET = (ENDPOINT, "ApiSetFormValue")
SET_QUERY = "mutation ApiSetFormValue($path: String!, $value: String) { setFormValue(path: $path, value: $value) { ok } }"


def info(name="ApiSetFormValue", query=SET_QUERY, url=ENDPOINT):
    return graphql_policy.inspect(url, body(name, query))


# --- policy (pure) ---------------------------------------------------------------------------------

def test_form_write_entries_are_exact_and_non_final():
    assert graphql_policy.validate_form_writes(APP, [SET]) == frozenset({SET})
    for bad in ([(ENDPOINT, "ApiSubmitApplication")], [(ENDPOINT, "CompleteApplication")], [(ENDPOINT, "FinalSubmit")],
                [(ENDPOINT, "ApiConfirmForm")], [(ENDPOINT, "SendApplication")], [(ENDPOINT, "apply_to_job")],
                [(f"https://other.example.test/api/non-user-graphql", "ApiSetFormValue")],
                [(f"http://{HOST}/api/non-user-graphql", "ApiSetFormValue")], [(ENDPOINT + "?op=x", "ApiSetFormValue")],
                [(ENDPOINT, "*")], [(ENDPOINT, "")], [SET, SET],
                [(ENDPOINT, f"ApiSetValue{i}") for i in range(graphql_policy.MAX_FORM_WRITES + 1)]):
        with pytest.raises(ValueError):
            graphql_policy.validate_form_writes(APP, bad)


def test_form_write_match_needs_exact_named_single_mutation():
    writes = frozenset({SET})
    assert graphql_policy.is_allowed_form_write("POST", "fetch", ENDPOINT, info(), writes)
    assert not graphql_policy.is_allowed_form_write("POST", "fetch", ENDPOINT, info(), frozenset())  # off by default
    assert not graphql_policy.is_allowed_form_write("POST", "fetch", ENDPOINT + "2", info(url=ENDPOINT + "2"), writes)
    assert not graphql_policy.is_allowed_form_write("POST", "document", ENDPOINT, info(), writes)
    assert not graphql_policy.is_allowed_form_write("PUT", "fetch", ENDPOINT, info(), writes)
    assert not graphql_policy.is_allowed_form_write(  # same name as a query: not a mutation
        "POST", "fetch", ENDPOINT, info(query="query ApiSetFormValue { a }"), writes)
    assert not graphql_policy.is_allowed_form_write(  # a different mutation
        "POST", "fetch", ENDPOINT, info("ApiSetDraft", "mutation ApiSetDraft { a }"), writes)
    assert not graphql_policy.is_allowed_form_write(  # two operations in one document
        "POST", "fetch", ENDPOINT, info(query=SET_QUERY + " mutation ApiSubmit { a }"), writes)
    anonymous = graphql_policy.inspect(ENDPOINT, json.dumps({"query": SET_QUERY}))
    malformed = graphql_policy.inspect(ENDPOINT, '{"operationName": "ApiSetFormValue", SECRET')
    assert not graphql_policy.is_allowed_form_write("POST", "fetch", ENDPOINT, anonymous, writes)
    assert not graphql_policy.is_allowed_form_write("POST", "fetch", ENDPOINT, malformed, writes)


def test_read_and_form_write_permissions_never_cross():
    # A read entry never authorizes the mutation; a form-write entry never authorizes a query.
    assert not graphql_policy.is_allowed_read("POST", "fetch", ENDPOINT, info(), frozenset({SET}))
    query = graphql_policy.inspect(ENDPOINT, body())
    assert graphql_policy.is_allowed_read("POST", "fetch", ENDPOINT, query, frozenset({READ}))
    assert not graphql_policy.is_allowed_form_write("POST", "fetch", ENDPOINT, query, frozenset({READ}))


# --- fill-window scoping (unit, recording fake frame) -----------------------------------------------

class RecordingFrame:
    """Records every interaction and the open fill window at that moment."""

    def __init__(self, window):
        self.window, self.calls = window, []

    def _record(self, name):
        self.calls.append((name, self.window["field"]))

    def fill(self, selector, value):
        self._record("fill")

    def eval_on_selector(self, selector, script):
        self._record("eval")
        return {"invalid": False}

    def wait_for_timeout(self, ms):
        self._record("wait")


def control(label, kind="text", required=False):
    return {"label": label, "kind": kind, "required": required, "marker": "c1", "options": []}


def window():
    return {"field": None, "settle_ms": browserbase_runner.FORM_WRITE_SETTLE_MS}


@pytest.fixture
def bundle(tmp_path):
    from test_browserbase_live_host import live_bundle
    return live_bundle(tmp_path)


def test_window_is_open_only_for_the_approved_fill_and_always_closed_after(bundle):
    w = window()
    frame, result = RecordingFrame(w), browserbase_runner.RunResult()
    assert browserbase_runner._process_control(frame, bundle, control("Email"), result, "ctx", w) == "filled"
    assert [c for c, _ in frame.calls[:3]] == ["fill", "eval", "wait"]
    assert {field for name, field in frame.calls if name in {"fill", "wait"}} == {"email"}
    assert w["field"] is None  # closed after the interaction settled
    assert frame.calls[-1] == ("eval", None)  # the validity check after the window runs closed


def test_window_closes_even_when_the_fill_raises(bundle):
    w = window()

    class Exploding(RecordingFrame):
        def fill(self, selector, value):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        browserbase_runner._process_control(Exploding(w), bundle, control("Email"), browserbase_runner.RunResult(),
                                            "ctx", w)
    assert w["field"] is None


@pytest.mark.parametrize("label,kind", [
    ("Favourite colour", "text"),             # unresolved: no approved answer
    ("What is your gender?", "text"),          # sensitive: escalated, never filled
    ("Ethnicity", "select"),                   # sensitive
    ("Upload anything", "file"),               # unknown upload
    ("Rate us", "range"),                      # unsupported control
])
@pytest.mark.parametrize("required", [False, True])
def test_unresolved_sensitive_or_unsupported_controls_never_open_a_window(bundle, label, kind, required):
    w = window()
    frame = RecordingFrame(w)
    state = browserbase_runner._process_control(frame, bundle, control(label, kind, required),
                                                browserbase_runner.RunResult(), "ctx", w)
    assert state in {"skipped", "escalated"} and frame.calls == [] and w["field"] is None


# --- the real runner, live-host mode, local Chrome --------------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser, live_bundle  # noqa: E402,F401  (fixture)
from test_browserbase_runner_offline import SYNTHETIC_ANSWERS  # noqa: E402
from pathlib import Path  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "graphql" / "autosave.html"
VALUES = tuple(SYNTHETIC_ANSWERS.values())


class AutosaveServer:
    def __init__(self):
        self.requests, self.external, self.bodies = [], [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname != HOST:
            self.external.append(request.url)
            return route.abort()
        self.requests.append((request.method, parts.path))
        if request.method == "POST" and parts.path.startswith("/api/"):
            self.bodies.append(json.loads(request.post_data))
            return route.fulfill(status=200, content_type="application/json", body='{"data": {}}')
        if request.method != "GET" or not parts.path.endswith("/autosave.html"):
            return route.fulfill(status=404, body="not found")
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text())

    def operations(self):
        return [b["operationName"] for b in self.bodies]


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = AutosaveServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def live_run(tmp_path, server_page, mode, **kwargs):
    server, page = server_page
    result = browserbase_runner.run(page, live_bundle(tmp_path, "/graphql/autosave.html", f"?mode={mode}"),
                                    live_host=HOST, **kwargs)
    assert result.final_submit_clicked is False and server.external == []
    assert not any(p.endswith("/submit") for _, p in server.requests)
    return server, page, result


def persisted(result):
    """What the cloud transport would persist from this run (live evidence)."""
    return {"blocked_requests": result.blocked_requests, "allowed_requests": result.allowed_writes,
            "form": browserbase_runner.form_diagnostics(result)}


def safe(blob):
    text = json.dumps(blob)
    return no_secret(text) and not any(v in text for v in VALUES) and "setFormValue" not in text


def test_autosave_during_approved_fills_is_allowed_and_audited(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "autosave", graphql_form_writes=[SET])
    assert result.status == "ready_for_human_review" and result.blocked_requests == []
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email"]
    assert result.advance_clicks == ["Next"] and result.final_submit_seen is True
    autosaves = [a for a in result.allowed_writes if a["kind"] == "graphql_form_write"]
    assert autosaves and {a["field"] for a in autosaves} == {"legal_first_name", "email"}
    assert all(a == {"kind": "graphql_form_write", "method": "POST", "host": HOST, "path": "/api/non-user-graphql",
                     "resource_type": "fetch", "graphql_operation_name": "ApiSetFormValue",
                     "graphql_operation_type": "mutation", "reason": "approved_field_autosave", "field": a["field"]}
               for a in autosaves)
    # Ashby-style autosave reached the (synthetic) server: that is the purpose of the permission.
    assert server.operations() == ["ApiSetFormValue"] * len(autosaves)
    assert {b["variables"]["path"] for b in server.bodies} == {"fn", "em"}  # never the skipped field
    assert safe(persisted(result))


def test_autosave_is_blocked_by_default(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "autosave")
    assert result.status == "needs_human_input" and result.stop_reason == "unexpected_write_blocked"
    assert server.bodies == [] and len(result.fields_filled) == 1  # stopped right after the first fill
    assert {(b["graphql_operation_name"], b["graphql_operation_type"]) for b in result.blocked_requests} == {
        ("ApiSetFormValue", "mutation")}
    assert safe(persisted(result))


def test_page_load_autosave_is_blocked(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "load", graphql_form_writes=[SET])
    assert result.stop_reason == "unexpected_write_blocked" and result.fields_filled == []
    assert browserbase_runner.identity_state(result) == "not_observed" and server.bodies == []
    assert result.blocked_requests[0]["graphql_operation_name"] == "ApiSetFormValue"


def test_autosave_right_after_the_window_closes_is_blocked(tmp_path, server_page, monkeypatch):
    # Window settle 50 ms; the page autosaves 250 ms after blur, during the runner's post-control wait.
    monkeypatch.setattr(browserbase_runner, "FORM_WRITE_SETTLE_MS", 50)
    monkeypatch.setattr(browserbase_runner, "SETTLE_MS", 600)
    server, page, result = live_run(tmp_path, server_page, "late", graphql_form_writes=[SET])
    assert result.stop_reason == "unexpected_write_blocked" and server.bodies == []
    assert [a for a in result.allowed_writes if a["kind"] == "graphql_form_write"] == []
    assert result.blocked_requests[0]["graphql_operation_name"] == "ApiSetFormValue"


@pytest.mark.parametrize("mode,name,path", [
    ("other", "ApiSetDraft", "/api/non-user-graphql"),             # different mutation during a fill
    ("submit", "ApiSubmitApplication", "/api/non-user-graphql"),   # submit-like mutation during a fill
    ("endpoint", "ApiSetFormValue", "/api/other-graphql"),         # right name, different endpoint
])
def test_anything_but_the_exact_mutation_is_blocked_even_inside_a_fill(tmp_path, server_page, mode, name, path):
    server, page, result = live_run(tmp_path, server_page, mode, graphql_form_writes=[SET])
    assert result.status == "needs_human_input" and result.stop_reason == "unexpected_write_blocked"
    assert server.bodies == [] and len(result.fields_filled) == 1
    assert {(b["graphql_operation_name"], b["path"]) for b in result.blocked_requests} == {(name, path)}
    assert safe(persisted(result))


def test_autosave_during_next_continue_is_blocked(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "next", graphql_form_writes=[SET])
    assert result.stop_reason == "unexpected_write_blocked" and result.advance_clicks == ["Next"]
    assert len(result.fields_filled) == 2  # fills (and their autosaves) passed before the click
    fill_autosaves = len(server.bodies)
    assert fill_autosaves > 0 and all(a["kind"] == "graphql_form_write" for a in result.allowed_writes)
    [blocked] = result.blocked_requests
    assert blocked["graphql_operation_name"] == "ApiSetFormValue"


def test_final_submit_and_handoff_phases_never_allow_the_mutation(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "autosave", graphql_form_writes=[SET])
    assert result.status == "ready_for_human_review" and result.final_submit_seen is True
    before, recorded = len(server.bodies), (list(result.allowed_writes), list(result.blocked_requests))
    # Final-submit handling: the page tries to autosave on submit; the submit is blocked in-page and
    # the guard (finished) aborts the mutation without recording or authorizing anything.
    page.click("button[type=submit]")
    # Handoff phase (before the caller detaches the guard): page-initiated autosave is aborted too.
    page.evaluate("""() => fetch('/api/non-user-graphql', {method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({operationName: 'ApiSetFormValue', query: 'mutation ApiSetFormValue { a }'})})
        .catch(() => null)""")
    page.wait_for_timeout(300)
    assert len(server.bodies) == before and (result.allowed_writes, result.blocked_requests) == recorded
    assert "submit event" in page.evaluate("() => [...window.__jobintelBlockedSubmits]")


def test_form_diagnostics_are_safe(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "autosave", graphql_form_writes=[SET])
    form = browserbase_runner.form_diagnostics(result)
    assert form == {
        "controls_seen_count": 3,
        "controls_detected": [
            {"label": "first name", "kind": "text", "required": True, "canonical_fields": ["first_name", "legal_first_name"]},
            {"label": "email", "kind": "email", "required": True, "canonical_fields": ["email"]},
            {"label": "favourite colour", "kind": "text", "required": False, "canonical_fields": []}],
        "fields_filled": [{"field": "legal_first_name", "page_label": "first name"},
                          {"field": "email", "page_label": "email"}],
        "fields_skipped": [{"label": "favourite colour", "reason": "unknown_field"}],
        "fields_escalated": [], "cv_control_detected": False, "cv_control": {}, "cv_upload_status": "not_seen",
        "cv_upload_attempted": False, "cv_artifact_id": None, "cv_upload_visible_in_page": None, "cv_upload_steps": {},
        "advance_clicks": 1, "stop_reason": "final_page_reached_stop_before_submit"}
    assert safe(form)


def test_escalated_controls_are_reported_by_label_and_reason_only():
    result = browserbase_runner.RunResult()
    browserbase_runner._escalate(result, "What is your gender?", "sensitive:gender", "page 1: x")
    assert browserbase_runner.form_diagnostics(result)["fields_escalated"] == [
        {"label": "what is your gender?", "reason": "sensitive:gender"}]


# --- through the service: audit, evidence, privacy ------------------------------------------------

from test_browserbase_cloud_offline import FakeApi, local_cdp  # noqa: E402,F401  (fixture)
from test_browserbase_live_host import as_real_host  # noqa: E402,F401  (fixture)
from test_browserbase_service import LIVE_URL, SENTINEL_KEY, approve, build, no_receipt_and_not_applied  # noqa: E402


def service_run(tmp_path, local_cdp, mode, **execute_kwargs):  # noqa: F811
    from jobintel.submission.browserbase_cloud import CloudTransport
    api, server = FakeApi(local_cdp), AutosaveServer()

    def get(url, headers, timeout):
        return {"debuggerFullscreenUrl": LIVE_URL, "pages": []} if "/debug" in url else {"status": "RUNNING"}

    s = build(tmp_path, url=f"https://{HOST}/graphql/autosave.html?mode={mode}")
    s.svc._browserbase_transport = CloudTransport(serve=server.handle, screenshot_dir=tmp_path / "shots", post=api,
                                                  get=get, environ={"BROWSERBASE_API_KEY": SENTINEL_KEY})
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="runner")
    out = s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, allow_live_host=True,
                        live_host=HOST, **execute_kwargs)
    ev = {e["kind"]: e["data"] for e in s.svc.store.events() if e["entity_id"] == r["id"]}
    store = b"".join(p.read_bytes() for p in s.root.rglob("*") if p.is_file())
    return s, r, out, ev, store, server


def answer_values(s):
    bundle = json.loads(next((s.root / "submissions/browserbase").rglob("browserbase_request.json")).read_text())
    return bundle, [a["value"] for a in bundle["answers"] if len(a["value"]) > 3]


def test_service_audits_form_writes_separately_and_persists_only_safe_evidence(tmp_path, local_cdp,
                                                                                as_real_host):  # noqa: F811
    s, r, out, ev, store, server = service_run(tmp_path, local_cdp, "autosave", live_graphql_reads=[READ],
                                               live_graphql_form_writes=[SET])
    audit = ev["browserbase_live_host_authorized"]
    assert (audit["graphql_read_allowlist_count"], audit["graphql_form_write_allowlist_count"],
            audit["write_allowlist_count"], audit["final_submit_allowed"]) == (1, 1, 0, False)
    live = ev["browserbase_evidence"]["live"]
    autosaves = [a for a in live["allowed_requests"] if a["kind"] == "graphql_form_write"]
    assert autosaves and all(a["reason"] == "approved_field_autosave" and a["graphql_operation_type"] == "mutation"
                             for a in autosaves)
    assert live["form"]["controls_seen_count"] == 3
    assert [f["field"] for f in live["form"]["fields_filled"]] == ["first_name", "email"]
    assert out["final_submit_clicked"] is False and len(server.bodies) == len(autosaves)
    bundle, values = answer_values(s)
    assert bundle["final_submit_allowed"] is False
    # Evidence: no candidate values, variables, query text or body; nothing secret anywhere in the store.
    evidence = json.dumps(ev["browserbase_evidence"])
    assert not any(v in evidence for v in values) and "setFormValue" not in evidence and no_secret(evidence)
    assert no_secret(store) and LIVE_URL.encode() not in store and SENTINEL_KEY.encode() not in store
    assert b"setFormValue" not in store and b"SECRET-VARIABLE-SENTINEL" not in store
    no_receipt_and_not_applied(s)


def test_invalid_form_writes_are_refused_before_any_session(tmp_path, as_real_host):  # noqa: F811
    from test_browserbase_live_host import FakeTransport
    from test_browserbase_service import kinds, ready_result
    s = build(tmp_path, FakeTransport(ready_result), url=f"https://{HOST}/graphql/autosave.html")
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="runner")
    for bad in ([(ENDPOINT, "ApiSubmitApplication")], [(ENDPOINT, "FinalSubmit")],
                [("https://other.example.test/api/non-user-graphql", "ApiSetFormValue")]):
        with pytest.raises(ValueError):
            s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, allow_live_host=True,
                          live_host=HOST, live_graphql_form_writes=bad)
    with pytest.raises(ValueError):  # form writes alone switch on live mode, which needs the full opt-in
        s.svc.execute(r["id"], claim_token=c["claim_token"], live_graphql_form_writes=[SET])
    assert s.transport.calls == 0 and "handoff_started" not in kinds(s, r)


def test_ordinary_write_allowlist_still_cannot_authorize_graphql():
    from jobintel.submission.browserbase_runner import validate_write_entries
    with pytest.raises(ValueError, match="GraphQL endpoints cannot be URL-allowlisted"):
        validate_write_entries(APP, [("POST", ENDPOINT)])


def test_cli_exposes_a_separate_form_write_option_and_no_submit_option():
    import argparse
    from jobintel.submission.cli import add_submission_commands
    parser = argparse.ArgumentParser()
    add_submission_commands(parser.add_subparsers(dest="command"))
    execute = parser._subparsers._group_actions[0].choices["submission-execute"]
    actions = {o: a for a in execute._actions for o in a.option_strings}
    assert {"--live-graphql-read", "--live-graphql-form-write"} <= set(actions)
    assert actions["--live-graphql-form-write"].nargs == 2 and actions["--live-graphql-form-write"].dest != "live_graphql_read"
    assert not any(w in o for o in actions for w in ("final", "force", "submit", "mutation", "auto"))
