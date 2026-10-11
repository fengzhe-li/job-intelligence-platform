"""Fixes from the first real supervised run (synthetic, offline).

1. Identity NOT OBSERVED is not identity MISMATCH: a run that stops before the start page's
   identity is checked (e.g. a blocked background write during page load) stays a safe,
   reviewable needs_human_input stop instead of becoming wrong-job / failed. A genuine
   mismatch still fails closed exactly as before.
2. GraphQL POSTs are authorized only per exact read operation (query + operationName) on the
   exact host; mutations, unknown, unnamed, batched or malformed operations stay blocked and
   only method/host/path/resource type/operation name/operation type are ever kept.

Every host is reserved; the browser resolves no names, so nothing leaves the machine.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from jobintel.submission import graphql_policy, stop_only
from jobintel.submission.browserbase_runner import identity_state, validate_write_entries
from test_stop_only_contract import PACKAGE, report

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

HOST = "jobs.example.test"
APP = f"https://{HOST}/graphql/apply.html"
ENDPOINT = f"https://{HOST}/api/non-user-graphql"
READ = (ENDPOINT, "ApiJobPosting")
SECRETS = ("SECRET-VARIABLE-SENTINEL", "SECRET-TOKEN-SENTINEL", "SECRET-BODY-SENTINEL")
VARS = {"postingId": "p-1", "email": "SECRET-VARIABLE-SENTINEL"}


def body(name="ApiJobPosting", query="query ApiJobPosting($postingId: String!) { jobPosting(id: $postingId) { title } }",
         **extra):
    value = {"query": query, "variables": VARS, **extra}
    if name is not None:
        value["operationName"] = name
    return json.dumps(value)


def allowed(url=ENDPOINT, text=..., reads=frozenset({READ}), method="POST", resource="fetch"):
    text = body() if text is ... else text
    return graphql_policy.is_allowed_read(method, resource, url, graphql_policy.inspect(url, text), reads)


def no_secret(blob):
    text = json.dumps(blob, default=str) if not isinstance(blob, (str, bytes)) else blob
    text = text.decode(errors="ignore") if isinstance(text, bytes) else text
    return not any(s in text for s in SECRETS)


# --- GraphQL policy (pure) -------------------------------------------------------------------------

def test_exact_allowlisted_query_operation_is_allowed():
    assert allowed()
    assert graphql_policy.inspect(ENDPOINT, body()) == {"graphql_operation_name": "ApiJobPosting",
                                                       "graphql_operation_type": "query", "single_operation": True}


def test_query_not_allowlisted_is_blocked():
    assert not allowed(reads=frozenset())


def test_different_query_operation_is_blocked():
    assert not allowed(text=body("ApiOtherQuery", "query ApiOtherQuery { other { id } }"))


def test_mutation_on_the_same_endpoint_is_blocked():
    text = body("ApiSetDraft", "mutation ApiSetDraft($email: String) { setDraft(email: $email) { ok } }")
    assert graphql_policy.inspect(ENDPOINT, text)["graphql_operation_type"] == "mutation"
    assert not allowed(text=text, reads=frozenset({READ, (ENDPOINT, "ApiSetDraft")}))


def test_mutation_reusing_an_allowlisted_operation_name_is_blocked():
    text = body("ApiJobPosting", "mutation ApiJobPosting { setDraft { ok } }")
    assert graphql_policy.inspect(ENDPOINT, text)["graphql_operation_type"] == "mutation"
    assert not allowed(text=text)


@pytest.mark.parametrize("text", [
    body(name=None),                                                              # missing operationName
    body(name=""),                                                                # empty operationName
    body(name="Api Job"),                                                         # not a plain name
    "{not json SECRET-BODY-SENTINEL",                                             # malformed JSON
    json.dumps([json.loads(body())]),                                             # batch
    json.dumps({"operationName": "ApiJobPosting", "variables": VARS,
                "extensions": {"persistedQuery": {"sha256Hash": "ab" * 32}}}),     # persisted, no document
    body(query="query ApiJobPosting { a } mutation ApiSetDraft { b }"),          # mixed document
    body(query="query ApiJobPosting { a } query ApiJobPosting { b }"),           # ambiguous name
    body(query="subscription ApiJobPosting { a }"),                               # subscription
    body(query="{ jobPosting { title } }"),                                       # anonymous shorthand
    body(query='query ApiJobPosting { a(x: "unterminated) }'),                   # malformed GraphQL
    body(query="type Query { a: String }"),                                       # schema definition
    "x" * (graphql_policy.MAX_BODY_CHARS + 1),                                    # oversized
    None,                                                                         # no inspectable body
])
def test_unknown_or_unsafe_graphql_is_blocked(text):
    assert not allowed(text=text)


def test_strings_comments_fragments_and_variable_defaults_do_not_confuse_the_parser():
    query = ('# mutation Evil { x }\nquery ApiJobPosting($f: Filter = {kind: "mutation { x }"}) @cached '
             '{ jobPosting(id: "}") { ...F title(s: """ mutation Y { z } """) } }\nfragment F on Job { id }')
    assert graphql_policy.operations(query) == [("query", "ApiJobPosting")]
    assert allowed(text=body(query=query))


def test_url_level_operation_must_agree_with_the_body():
    assert allowed(url=ENDPOINT + "?op=ApiJobPosting", reads=frozenset({READ}))
    assert not allowed(url=ENDPOINT + "?op=ApiSubmitApplication", reads=frozenset({READ}))


@pytest.mark.parametrize("method,resource,url", [
    ("PUT", "fetch", ENDPOINT), ("POST", "document", ENDPOINT), ("POST", "fetch", f"https://other.example.test/api/non-user-graphql"),
    ("POST", "fetch", f"https://{HOST}/api/other-graphql"),
])
def test_method_resource_endpoint_must_match_exactly(method, resource, url):
    assert not allowed(url=url, method=method, resource=resource)


@pytest.mark.parametrize("entries", [
    [(f"https://other.example.test/api/non-user-graphql", "ApiJobPosting")],
    [(f"https://login.{HOST}/api/non-user-graphql", "ApiJobPosting")],
    [(f"http://{HOST}/api/non-user-graphql", "ApiJobPosting")],
    [(ENDPOINT + "?op=ApiJobPosting", "ApiJobPosting")],
    [(f"https://{HOST}/api/*", "ApiJobPosting")],
    [(ENDPOINT, "*")], [(ENDPOINT, "Api Job")], [(ENDPOINT, "")],
    [(ENDPOINT, "ApiSubmitSingleApplicationForm")], [(ENDPOINT, "applyToJob")], [(ENDPOINT, "FinalizeApplication")],
    [(ENDPOINT, "ApiCompleteForm")], [(ENDPOINT, "ConfirmCandidate")], [(ENDPOINT, "UpdateDraft")],
    [(ENDPOINT, "UploadResume")], [(ENDPOINT, "SaveDraft")],
    [READ, READ],
    [(ENDPOINT, f"ApiRead{i}") for i in range(graphql_policy.MAX_READS + 1)],
])
def test_read_allowlist_rejects_anything_but_exact_safe_reads(entries):
    with pytest.raises(ValueError):
        graphql_policy.validate_reads(APP, entries)


SAFE_READ_NAMES = ["ApiAutocompleteGeoLocation", "ApplicationStatusQuery", "GetSavedJobs", "ApiJobPosting",
                   "autocompleteLocation", "API_AUTOCOMPLETE_GEO", "APIAutocompleteGeoLocation"]
DANGEROUS_NAMES = [
    "SubmitApplication", "ApplyToJob", "CompleteApplication", "FinalSubmit", "ConfirmApplication",
    "SendApplication", "CreateApplication", "UpdateApplication", "DeleteApplication", "UploadApplication",
    "SaveApplication", "RemoveApplication", "UpsertApplication", "MutationSubmitApplication",
    # snake_case / UPPER_SNAKE
    "submit_application", "apply_to_job", "complete_application", "final_submit", "confirm_application",
    "SUBMIT_APPLICATION", "CREATE_APPLICATION", "update_application",
    # mixed / irregular case and words spanning boundaries
    "submitApplication", "SUBMITApplication", "sUbMiTApplication", "Sub_MitApplication", "applyTOJob",
    "UpDateApplication", "complete", "COMPLETE",
    # stems embedded in other words are not exempt unless explicitly benign
    "ResubmitApplication", "AutosaveDraft", "FinalizeApplication", "ApplicationSubmission", "SentApplication",
    "GetCompleteProfileData",  # adjectival "Complete" cannot be lexically distinguished: refused
    "AutocompleteSubmit", "AutocompleteAndSave",  # benign word does not mask a dangerous neighbour
]


def test_name_words_split_is_identifier_aware():
    assert graphql_policy.name_words("ApiAutocompleteGeoLocation") == ["api", "autocomplete", "geo", "location"]
    assert graphql_policy.name_words("APIAutocomplete_v2") == ["api", "autocomplete", "v", "2"]
    assert graphql_policy.name_words("submit_application") == ["submit", "application"]
    assert graphql_policy.name_words("MutationSubmitApplication") == ["mutation", "submit", "application"]


@pytest.mark.parametrize("name", SAFE_READ_NAMES)
def test_safe_read_names_are_not_flagged(name):
    assert not graphql_policy.is_dangerous_name(name)
    assert graphql_policy.validate_reads(APP, [(ENDPOINT, name)]) == frozenset({(ENDPOINT, name)})


@pytest.mark.parametrize("name", DANGEROUS_NAMES)
def test_dangerous_action_names_stay_blocked(name):
    assert graphql_policy.is_dangerous_name(name)
    with pytest.raises(ValueError, match="non-final operation"):
        graphql_policy.validate_reads(APP, [(ENDPOINT, name)])
    # even if one were somehow in the read set, the runtime check still refuses it
    text = body(name, f"query {name} {{ a }}")
    assert not allowed(text=text, reads=frozenset({(ENDPOINT, name)}))


def test_autocomplete_read_is_allowed_only_as_an_exact_allowlisted_query():
    name = "ApiAutocompleteGeoLocation"
    reads = graphql_policy.validate_reads(APP, [(ENDPOINT, name)])
    assert allowed(text=body(name, f"query {name}($q: String!) {{ geo(q: $q) {{ id }} }}"), reads=reads)
    assert not allowed(text=body(name, f"mutation {name} {{ a }}"), reads=reads)          # mutation
    assert not allowed(text=body(name, f"query {name} {{ a }}"), reads=frozenset({READ}))  # not allowlisted
    assert not allowed(url=ENDPOINT + "2", text=body(name, f"query {name} {{ a }}"), reads=reads)  # other endpoint
    assert not allowed(text=body(None, f"query {name} {{ a }}"), reads=reads)              # anonymous


def test_graphql_endpoint_cannot_be_url_allowlisted_as_a_write():
    with pytest.raises(ValueError, match="GraphQL endpoints cannot be URL-allowlisted"):
        validate_write_entries(APP, [("POST", ENDPOINT)])
    validate_write_entries(APP, [("POST", f"https://{HOST}/autosave/draft")])  # ordinary exact writes unchanged


def test_inspection_never_returns_variables_or_query_text():
    for text in (body(), body("ApiSetDraft", "mutation ApiSetDraft { a }"), "{bad SECRET-BODY-SENTINEL"):
        info = graphql_policy.inspect(ENDPOINT, text)
        assert set(info) == {"graphql_operation_name", "graphql_operation_type", "single_operation"}
        assert no_secret(info) and "jobPosting" not in json.dumps(info)


# --- identity semantics in the shared importer ----------------------------------------------------

def imported(identity, **changes):
    return stop_only.import_result(report("browserbase", **changes), package=PACKAGE, request_id="r",
                                   approval_id="a", executor="browserbase", display_name="Browserbase",
                                   identity_state=identity)


PRE_IDENTITY = dict(status="needs_human_input", observed_company="unknown", observed_role="unknown",
                    observed_url="unknown", fields_filled=[], cv_upload_confirmed=False)


def test_pre_identity_stop_stays_needs_human_input():
    result = imported("not_observed", **PRE_IDENTITY)
    assert result.outcome == "needs_human_input"
    assert result.unanswered_fields[0].startswith("identity_not_observed")
    # The same report judged by placeholders (old behaviour) was the real run's failure.
    assert imported(None, **PRE_IDENTITY).outcome == "failed"


@pytest.mark.parametrize("changes", [
    dict(fields_filled=[{"field": "email", "value": "x", "page_label": "Email"}]),
    dict(cv_upload_confirmed=True),
    dict(status="ready_for_human_review"),
])
def test_not_observed_cannot_claim_page_actions_or_a_reviewable_final_page(changes):
    with pytest.raises(ValueError):
        imported("not_observed", **{**PRE_IDENTITY, **changes})


def test_not_observed_never_upgrades_a_stop():
    assert imported("not_observed", **{**PRE_IDENTITY, "status": "aborted_wrong_job"}).outcome == "failed"  # host drift
    assert imported("not_observed", **{**PRE_IDENTITY, "status": "outcome_unknown"}).outcome == "outcome_unknown"
    duplicate = imported("not_observed", **{**PRE_IDENTITY, "duplicate_detected": True})
    assert duplicate.outcome == "needs_human_input" and "duplicate_detected" in duplicate.unanswered_fields[-1]


def test_genuine_mismatch_still_fails_closed():
    assert imported("mismatch").outcome == "failed"  # self-reported mismatch, even with matching strings
    assert imported("verified", observed_company="OtherCo").outcome == "failed"  # verified but values disagree
    assert imported("verified", observed_url=PACKAGE.application_url + "?x=1").outcome == "failed"
    assert imported(None, observed_role="Other Role").outcome == "failed"  # legacy path unchanged
    assert imported("verified").outcome == "ready_for_human_review"
    with pytest.raises(ValueError, match="Unknown identity state"):
        imported("unknown")


def test_airtap_import_and_schema_are_unchanged():
    from jobintel.submission import airtap
    assert "identity_state" not in airtap.result_schema()["properties"]
    with pytest.raises(ValueError):  # the report schema itself gained no field
        stop_only.import_result(report("airtap", identity_state="not_observed"), package=PACKAGE, request_id="r",
                                approval_id="a", executor="airtap", display_name="AirTap")
    assert airtap.import_result(report("airtap", **PRE_IDENTITY), package=PACKAGE, request_id="r",
                                approval_id="a").outcome == "failed"


def test_identity_state_comes_from_the_runner_start_page():
    from jobintel.submission.browserbase_runner import RunResult
    assert identity_state(RunResult()) == "not_observed"
    assert identity_state(RunResult(start_identity={"state": "matched"})) == "verified"
    assert identity_state(RunResult(start_identity={"state": "aborted_wrong_job"})) == "mismatch"


# --- the real runner, live-host mode, local Chrome --------------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser, live_bundle  # noqa: E402,F401  (fixture)
from jobintel.submission.browserbase_runner import run  # noqa: E402
from pathlib import Path  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "graphql" / "apply.html"


class GraphQLServer:
    def __init__(self):
        self.requests, self.external, self.bodies = [], [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname != HOST:
            self.external.append(request.url)
            return route.abort()
        self.requests.append((request.method, parts.path))
        if request.method == "POST" and parts.path == "/api/non-user-graphql":
            self.bodies.append(request.post_data)
            return route.fulfill(status=200, content_type="application/json", body='{"data": {}}')
        if request.method != "GET" or not parts.path.endswith("/apply.html"):
            return route.fulfill(status=404, body="not found")
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text())


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = GraphQLServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def live_run(tmp_path, server_page, gql, **kwargs):
    server, page = server_page
    result = run(page, live_bundle(tmp_path, "/graphql/apply.html", f"?gql={gql}"), live_host=HOST, **kwargs)
    assert result.final_submit_clicked is False and server.external == []
    assert not any(p.endswith("/submit") for _, p in server.requests)
    return server, result


def test_real_sequence_blocked_load_time_graphql_stops_before_identity(tmp_path, server_page):
    server, result = live_run(tmp_path, server_page, "read")
    assert result.status == "needs_human_input" and result.stop_reason == "unexpected_write_blocked"
    assert identity_state(result) == "not_observed" and result.fields_filled == [] and result.cv_upload == {}
    assert result.blocked_requests == [{"kind": "write", "method": "POST", "host": HOST, "path": "/api/non-user-graphql",
                                        "resource_type": "fetch", "graphql_operation_name": "ApiJobPosting",
                                        "graphql_operation_type": "query"}]
    assert server.bodies == []  # aborted: nothing reached the server
    assert no_secret(result.__dict__)


def test_allowlisted_read_passes_and_the_form_is_filled(tmp_path, server_page):
    server, result = live_run(tmp_path, server_page, "read", graphql_reads=[READ])
    assert result.status == "ready_for_human_review" and identity_state(result) == "verified"
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email"]
    assert result.blocked_requests == [] and len(server.bodies) == 1
    assert result.allowed_writes == [{"kind": "graphql_read", "method": "POST", "host": HOST,
                                      "path": "/api/non-user-graphql", "resource_type": "fetch",
                                      "graphql_operation_name": "ApiJobPosting", "graphql_operation_type": "query"}]
    assert no_secret(result.__dict__)


@pytest.mark.parametrize("gql,name,kind", [
    ("other_read", "ApiOtherQuery", "query"),
    ("mutation", "ApiSetDraft", "mutation"),
    ("mutation_same_name", "ApiJobPosting", "mutation"),
    ("no_name", None, "unknown"),
    ("mixed", "ApiJobPosting", "query"),  # named op is a query but the document also holds a mutation
    ("malformed", None, "unknown"),
    ("batch", None, "unknown"),
])
def test_everything_else_stays_blocked_even_with_a_read_allowlisted(tmp_path, server_page, gql, name, kind):
    server, result = live_run(tmp_path, server_page, gql, graphql_reads=[READ])
    assert result.status == "needs_human_input" and result.stop_reason == "unexpected_write_blocked"
    assert result.fields_filled == [] and server.bodies == []
    [blocked] = result.blocked_requests
    assert (blocked["graphql_operation_name"], blocked["graphql_operation_type"]) == (name, kind)
    assert set(blocked) == {"kind", "method", "host", "path", "resource_type", "graphql_operation_name",
                            "graphql_operation_type"}
    assert no_secret(result.__dict__)


def test_synthetic_mode_graphql_is_never_allowed_by_url(tmp_path, server_page):
    server, page = server_page
    bundle = live_bundle(tmp_path, "/graphql/apply.html", "?gql=read")
    with pytest.raises(ValueError, match="GraphQL endpoints cannot be URL-allowlisted"):
        run(page, bundle, write_allowlist=[("POST", ENDPOINT)])
    result = run(page, bundle)
    assert result.status == "needs_human_input" and server.bodies == []
    assert result.blocked_requests[0]["graphql_operation_type"] == "query" and no_secret(result.__dict__)


# --- the full service: real sequence keeps a reviewable handoff ------------------------------------

from test_browserbase_cloud_offline import FakeApi, local_cdp  # noqa: E402,F401  (fixture)
from test_browserbase_live_host import as_real_host  # noqa: E402,F401  (fixture)
from test_browserbase_service import LIVE_URL, SENTINEL_KEY, approve, build, no_receipt_and_not_applied  # noqa: E402


def service_run(tmp_path, local_cdp, gql, **execute_kwargs):  # noqa: F811
    from jobintel.submission.browserbase_cloud import CloudTransport
    api, server = FakeApi(local_cdp), GraphQLServer()

    def get(url, headers, timeout):
        return {"debuggerFullscreenUrl": LIVE_URL, "pages": []} if "/debug" in url else {"status": "RUNNING"}

    s = build(tmp_path, url=f"https://{HOST}/graphql/apply.html?gql={gql}")
    s.svc._browserbase_transport = CloudTransport(serve=server.handle, screenshot_dir=tmp_path / "shots", post=api,
                                                  get=get, environ={"BROWSERBASE_API_KEY": SENTINEL_KEY})
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="runner")
    out = s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, allow_live_host=True,
                        live_host=HOST, **execute_kwargs)
    ev = {e["kind"]: e["data"] for e in s.svc.store.events() if e["entity_id"] == r["id"]}
    store = b"".join(p.read_bytes() for p in s.root.rglob("*") if p.is_file())
    return s, r, out, ev, store, server, api


def test_real_sequence_through_the_service_keeps_a_reviewable_handoff(tmp_path, local_cdp, as_real_host):  # noqa: F811
    s, r, out, ev, store, server, api = service_run(tmp_path, local_cdp, "read")
    assert out["status"] == "needs_human_input" and out["handoff"] == "active" and out["session_released"] is False
    assert out["final_submit_clicked"] is False and "browserbase_session_released" not in ev
    assert ev["browserbase_evidence"]["identity_state"] == "not_observed"
    assert ev["browserbase_evidence"]["live"]["stop_reason"] == "unexpected_write_blocked"
    assert ev["browserbase_evidence"]["live"]["blocked_requests"][0]["graphql_operation_name"] == "ApiJobPosting"
    unanswered = ev["needs_human_input"]["result"]["unanswered_fields"]
    assert unanswered[0] == "Runner stopped: unexpected_write_blocked"  # the real reason stays visible
    assert unanswered[1].startswith("identity_not_observed") and "cv_upload_unconfirmed" not in unanswered
    assert ev["browserbase_handoff_started"]["live_host"] == HOST
    assert server.bodies == [] and no_secret(store) and LIVE_URL.encode() not in store
    bundle = json.loads(next((s.root / "submissions/browserbase").rglob("browserbase_request.json")).read_text())
    assert bundle["final_submit_allowed"] is False and ev["browserbase_live_host_authorized"]["final_submit_allowed"] is False
    no_receipt_and_not_applied(s)


def test_allowlisted_read_through_the_service_is_audited(tmp_path, local_cdp, as_real_host):  # noqa: F811
    s, r, out, ev, store, server, api = service_run(tmp_path, local_cdp, "read", live_graphql_reads=[READ])
    assert ev["browserbase_live_host_authorized"]["graphql_read_allowlist_count"] == 1
    assert ev["browserbase_evidence"]["identity_state"] == "verified"
    assert [a["graphql_operation_name"] for a in ev["browserbase_evidence"]["live"]["allowed_requests"]] == ["ApiJobPosting"]
    assert out["status"] == "needs_human_input"  # no CV control on this synthetic page
    assert len(server.bodies) == 1 and no_secret(store)
    no_receipt_and_not_applied(s)


def test_invalid_graphql_reads_are_refused_before_any_session(tmp_path, as_real_host):  # noqa: F811
    from test_browserbase_live_host import FakeTransport
    from test_browserbase_service import kinds, ready_result
    s = build(tmp_path, FakeTransport(ready_result), url=f"https://{HOST}/graphql/apply.html")
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="runner")
    for bad in ([(ENDPOINT, "ApiSubmitApplication")], [("https://other.example.test/api/non-user-graphql", "ApiJobPosting")]):
        with pytest.raises(ValueError):
            s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, allow_live_host=True,
                          live_host=HOST, live_graphql_reads=bad)
    assert s.transport.calls == 0 and "handoff_started" not in kinds(s, r)


def test_cli_exposes_graphql_reads_but_no_submit_or_mutation_option():
    import argparse
    from jobintel.submission.cli import add_submission_commands
    parser = argparse.ArgumentParser()
    add_submission_commands(parser.add_subparsers(dest="command"))
    execute = parser._subparsers._group_actions[0].choices["submission-execute"]
    options = {o for a in execute._actions for o in a.option_strings}
    assert "--live-graphql-read" in options
    assert not any(w in o for o in options for w in ("final", "force", "submit", "mutation", "auto"))
