"""Live-host CV handling: the runner never selects a file on a real host (synthetic, offline).

Selecting a file makes a hosted form start its own upload flow (an upload-handle mutation, then a
storage host), none of which is authorized. On a live host the CV/resume control is left untouched
and marked human_required, the run continues with the remaining approved controls and normal safe
navigation, and the imported result asks the human to upload the CV during handoff. Synthetic mode
keeps selecting and DOM-verifying the sealed CV as before.

Every host is reserved; the browser resolves no names, so nothing leaves the machine.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from jobintel.submission import browserbase_runner
from test_browserbase_graphql_and_identity import ENDPOINT, HOST, READ, no_secret

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser, live_bundle  # noqa: E402,F401  (fixture)
from test_browserbase_runner_offline import SYNTHETIC_ANSWERS  # noqa: E402

SET = (ENDPOINT, "ApiSetFormValue")
FIXTURE = Path(__file__).parent / "fixtures" / "graphql" / "cv_form.html"
UPLOAD = "ApiCreateFileUploadHandle"
CV_MESSAGE = "CV/resume upload requires human action during handoff."


class CvServer:
    def __init__(self):
        self.requests, self.external, self.bodies = [], [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname != HOST:
            self.external.append(request.url)
            return route.abort()
        self.requests.append((request.method, parts.path))
        if request.method == "POST" and parts.path == "/api/non-user-graphql":
            self.bodies.append(json.loads(request.post_data))
            return route.fulfill(status=200, content_type="application/json", body='{"data": {}}')
        if request.method != "GET" or not parts.path.endswith("/cv_form.html"):
            return route.fulfill(status=404, body="not found")
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text())

    def operations(self):
        return [b["operationName"] for b in self.bodies]


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = CvServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def runner(tmp_path, server_page, mode="", live=True, **kwargs):
    server, page = server_page
    bundle = live_bundle(tmp_path, "/graphql/cv_form.html", f"?mode={mode}" if mode else "")
    result = browserbase_runner.run(page, bundle, live_host=HOST if live else None, graphql_form_writes=[SET],
                                    **kwargs)
    assert result.final_submit_clicked is False and server.external == []
    assert not any(p.endswith("/submit") for _, p in server.requests)
    return server, page, bundle, result


def selected_files(page):
    return page.evaluate("() => document.getElementById('cv').files.length")


@pytest.mark.parametrize("mode", ["", "next"])
def test_live_cv_is_left_for_the_human_and_the_run_continues(tmp_path, server_page, mode):
    server, page, bundle, result = runner(tmp_path, server_page, mode)
    # 1-2. the file input is untouched and no upload flow was started
    assert selected_files(page) == 0 and result.cv_upload == {}
    assert UPLOAD not in server.operations() and result.blocked_requests == []
    # 3. human-required marker with a safe control key
    assert result.cv_upload_status == "human_required"
    assert result.cv_control == {"label": "resume", "kind": "file", "required": True, "canonical_fields": ["cv"]}
    # 4-6. later approved text and select controls were still filled; the required CV did not abort the run
    assert [f["field"] for f in result.fields_filled] == ["legal_full_name", "email", "phone_uk", "country"]
    assert page.evaluate("() => document.getElementById('co').value") == "United Kingdom"
    assert result.questions_escalated == [] and result.status == "ready_for_human_review"
    assert result.stop_reason == "final_page_reached_stop_before_submit" and result.final_submit_seen is True
    assert result.advance_clicks == (["Next"] if mode == "next" else [])
    # approved autosaves only; one per filled field
    assert server.operations() == ["ApiSetFormValue"] * 4


def test_live_report_always_needs_a_human_for_the_cv(tmp_path, server_page):
    server, page, bundle, result = runner(tmp_path, server_page)
    report = browserbase_runner.to_stop_only_report(result, bundle, task_id="t")
    assert report["cv_upload_confirmed"] is False and report["final_submit_clicked"] is False
    assert [q["question"] for q in report["questions_escalated"]] == [CV_MESSAGE]
    from jobintel.submission import stop_only
    stop_only.validate_schema(report, stop_only.result_schema("browserbase"))


def test_live_form_diagnostics_are_safe(tmp_path, server_page):
    server, page, bundle, result = runner(tmp_path, server_page, "next")
    form = browserbase_runner.form_diagnostics(result)
    assert form["cv_control_detected"] is True and form["cv_upload_status"] == "human_required"
    assert form["cv_upload_attempted"] is False and form["advance_clicks"] == 1
    assert form["controls_seen_count"] == 5 and form["fields_skipped"] == [] and form["fields_escalated"] == []
    assert [f["field"] for f in form["fields_filled"]] == ["legal_full_name", "email", "phone_uk", "country"]
    text = json.dumps(form)
    assert not any(v in text for v in SYNTHETIC_ANSWERS.values() if len(v) > 3)
    assert "cv.pdf" not in text and str(bundle.cv_path) not in text and bundle.request["cv"]["sha256"] not in text


def test_offline_mode_still_selects_and_verifies_the_sealed_cv(tmp_path, server_page):
    server, page, bundle, result = runner(tmp_path, server_page, "noupload", live=False)
    assert selected_files(page) == 1 and result.cv_upload_status == "selected"
    assert result.cv_upload["dom_matches_sealed_file"] is True and result.status == "ready_for_human_review"
    report = browserbase_runner.to_stop_only_report(result, bundle, task_id="t")
    assert report["cv_upload_confirmed"] is True and report["questions_escalated"] == []


def test_offline_mode_upload_flow_is_still_blocked_by_the_network_guard(tmp_path, server_page):
    # Unchanged default: a site upload mutation is never allowed, in any mode.
    server, page, bundle, result = runner(tmp_path, server_page, live=False)
    assert UPLOAD not in server.operations()
    assert any(b.get("graphql_operation_name") == UPLOAD for b in result.blocked_requests)
    assert result.status == "needs_human_input"


def test_unknown_file_inputs_keep_their_existing_handling(tmp_path):
    w = {"field": None, "settle_ms": 0}
    result = browserbase_runner.RunResult(live_host=HOST)
    control = {"label": "Portfolio attachment", "kind": "file", "required": False, "marker": "c1", "options": []}

    class NoTouch:
        def __getattr__(self, name):
            raise AssertionError(f"frame.{name} must not be called")

    bundle = live_bundle(tmp_path)
    state = browserbase_runner._process_control(NoTouch(), bundle, control, result, "ctx", w)
    assert state == "skipped" and result.cv_upload_status == "not_seen"
    cv = dict(control, label="Resume")
    assert browserbase_runner._process_control(NoTouch(), bundle, cv, result, "ctx", w) == "human_required"


# --- through the service: handoff, audit and persisted evidence -------------------------------------

from test_browserbase_cloud_offline import FakeApi, local_cdp  # noqa: E402,F401  (fixture)
from test_browserbase_live_host import as_real_host  # noqa: E402,F401  (fixture)
from test_browserbase_service import LIVE_URL, SENTINEL_KEY, approve, build, no_receipt_and_not_applied  # noqa: E402


def test_service_keeps_a_needs_human_handoff_and_persists_nothing_sensitive(tmp_path, local_cdp,
                                                                            as_real_host):  # noqa: F811
    from jobintel.submission.browserbase_cloud import CloudTransport
    api, server = FakeApi(local_cdp), CvServer()

    def get(url, headers, timeout):
        return {"debuggerFullscreenUrl": LIVE_URL, "pages": []} if "/debug" in url else {"status": "RUNNING"}

    s = build(tmp_path, url=f"https://{HOST}/graphql/cv_form.html?mode=next")
    s.svc._browserbase_transport = CloudTransport(serve=server.handle, screenshot_dir=tmp_path / "shots", post=api,
                                                  get=get, environ={"BROWSERBASE_API_KEY": SENTINEL_KEY})
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="runner")
    with pytest.raises(ValueError, match="human handoff"):  # 9. handoff stays mandatory
        s.svc.execute(r["id"], claim_token=c["claim_token"], allow_live_host=True, live_host=HOST,
                      live_graphql_form_writes=[SET])
    out = s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, allow_live_host=True,
                        live_host=HOST, live_graphql_reads=[READ], live_graphql_form_writes=[SET])
    ev = {e["kind"]: e["data"] for e in s.svc.store.events() if e["entity_id"] == r["id"]}
    # 7. reviewable, but only with a human: the CV is the explicit reason
    assert out["status"] == "needs_human_input" and out["handoff"] == "active" and out["final_submit_clicked"] is False
    assert ev["needs_human_input"]["result"]["unanswered_fields"] == [CV_MESSAGE]
    assert ev["browserbase_handoff_started"]["live_host"] == HOST
    # 8-9. no final submit anywhere; handoff required in the audit
    audit = ev["browserbase_live_host_authorized"]
    assert audit["final_submit_allowed"] is False and audit["human_handoff_required"] is True
    bundle = json.loads(next((s.root / "submissions/browserbase").rglob("browserbase_request.json")).read_text())
    assert bundle["final_submit_allowed"] is False
    # 2. the upload flow never started
    assert UPLOAD not in server.operations() and ev["browserbase_evidence"]["live"]["blocked_requests"] == []
    form = ev["browserbase_evidence"]["live"]["form"]
    assert (form["cv_control_detected"], form["cv_upload_status"], form["advance_clicks"]) == (True, "human_required", 1)
    assert [f["field"] for f in form["fields_filled"]] == ["name", "email", "phone", "country"]
    # 11. no CV content/path, candidate values, variables or secrets in the persisted evidence
    evidence = json.dumps(ev["browserbase_evidence"])
    cv_pdf = next((s.root / "submissions/browserbase").rglob("cv.pdf"))
    assert "cv.pdf" not in evidence and str(cv_pdf) not in evidence and bundle["cv"]["sha256"] not in evidence
    assert not any(a["value"] in evidence for a in bundle["answers"] if len(a["value"]) > 3)
    store = b"".join(p.read_bytes() for p in s.root.rglob("*") if p.is_file() and p.name != "cv.pdf"
                     and p.suffix != ".pdf")
    assert cv_pdf.read_bytes() not in store and no_secret(store) and LIVE_URL.encode() not in store
    assert SENTINEL_KEY.encode() not in store
    no_receipt_and_not_applied(s)
