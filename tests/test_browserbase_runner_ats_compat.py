"""ATS-like compatibility scenarios for the offline stop-only runner. Synthetic, no network.

Each fixture under tests/fixtures/ats_compat models a generic UI pattern (no vendor
logic). Served in-process on the reserved hosts jobs.example.test and
forms.ats-vendor.test; every other host is aborted and recorded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage  # noqa: E402
from jobintel.submission.browserbase import BrowserbaseBundleAuthorization, write_bundle  # noqa: E402
from jobintel.submission.browserbase_runner import load_bundle, run, validate_write_allowlist  # noqa: E402
from test_browserbase_runner_offline import SYNTHETIC_ANSWERS, browser  # noqa: E402,F401  (fixture)
from test_handoff_fail_closed import _canonical_bank_values  # noqa: E402

ROOT = Path(__file__).parent / "fixtures" / "ats_compat"
HOST, VENDOR = "jobs.example.test", "forms.ats-vendor.test"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
ANSWERS = {**SYNTHETIC_ANSWERS, "postgraduate_start_date": "2025-09-22", "undergraduate_start_date": "2020-09",
           "driving_licence_country": "Sampleland"}
AUTOSAVE = ("POST", f"https://{HOST}/autosave/draft")


class Server:
    def __init__(self):
        self.requests, self.external = [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname not in {HOST, VENDOR}:
            self.external.append(request.url)
            return route.abort()
        self.requests.append((request.method, parts.hostname, parts.path))
        if request.method == "POST" and parts.path in {"/autosave/draft", "/analytics/track"}:
            return route.fulfill(status=204, body="")
        target = (ROOT / parts.path.lstrip("/")).resolve()
        if request.method != "GET" or not target.is_relative_to(ROOT.resolve()) or not target.is_file():
            return route.fulfill(status=404, body="not found")
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=target.read_text())

    def writes(self):
        return [(m, p) for m, h, p in self.requests if m != "GET"]


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = Server()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def bundle_for(tmp_path, case, query=""):
    sealed = tmp_path / "sealed" / "cv.pdf"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(b"%PDF-1.4 synthetic ats-compat CV")
    package = ApprovedSubmissionPackage(
        "app:compat-1", "job:compat-1", f"https://{HOST}/{case}/apply.html{query}", "cv-0123456789ab",
        hashlib.sha256(sealed.read_bytes()).hexdigest(), str(sealed),
        tuple(AnswerSnapshot(f, v, "synthetic", "v1") for f, v in sorted(ANSWERS.items())), "input-hash",
        "auto_prepare", (), company="ExampleCo", role="Graduate Engineer")
    auth = BrowserbaseBundleAuthorization("approval:compat", f"request:compat-{case}", package.package_hash,
                                          (NOW + timedelta(days=1)).isoformat(), True, "approved")
    return load_bundle(write_bundle(tmp_path / "bundles", package, auth, clock=lambda: NOW))


def run_case(tmp_path, server_page, case, query="", **kwargs):
    server, page = server_page
    bundle = bundle_for(tmp_path, case, query)
    result = run(page, bundle, **kwargs)
    # Universal invariants for every scenario.
    assert result.final_submit_clicked is False
    assert not any(p.endswith("/submit") for _, _, p in server.requests)
    assert server.external == []
    sealed = {(a.field, a.value) for a in bundle.answers}
    assert all((f["field"], f["value"]) in sealed for f in result.fields_filled)
    return server, page, bundle, result


def escalations(result):
    return {q["question"]: q["screenshot_or_evidence"].removeprefix("runner:") for q in result.questions_escalated}


def filled(result):
    return [f["field"] for f in result.fields_filled]


# 1. iframes --------------------------------------------------------------------------------

def test_same_origin_iframe_form_is_filled_inside_the_frame(tmp_path, server_page):
    server, page, _, result = run_case(tmp_path, server_page, "iframe")
    assert result.status == "ready_for_human_review"
    assert filled(result) == ["legal_first_name", "email"]
    frame = next(f for f in page.frames if f.url.endswith("/iframe/form.html"))
    assert frame.eval_on_selector("#fn", "el => el.value") == "Sam"
    assert result.final_submit_seen is True and server.writes() == []


def test_cross_origin_iframe_with_form_controls_stops_explicitly(tmp_path, server_page):
    server, page, _, result = run_case(tmp_path, server_page, "iframe_xorigin")
    assert result.status == "needs_human_input" and result.stop_reason == "cross_origin_frame_with_form_controls"
    assert result.fields_filled == []
    frame = next(f for f in page.frames if VENDOR in f.url)
    assert frame.eval_on_selector("#fn", "el => el.value") == ""


def test_page_with_no_form_controls_is_not_treated_as_completed(tmp_path, server_page):
    _, _, _, result = run_case(tmp_path, server_page, "landing")
    assert result.status == "needs_human_input" and result.stop_reason == "no_form_controls_found"


# 2. combobox -------------------------------------------------------------------------------

def test_combobox_selects_only_the_exact_approved_option(tmp_path, server_page):
    _, page, _, result = run_case(tmp_path, server_page, "combobox")
    assert result.status == "ready_for_human_review"
    assert filled(result) == ["legal_first_name", "country"]
    assert page.eval_on_selector("#co", "el => el.value") == "United Kingdom"
    assert page.eval_on_selector("#co-value", "el => el.value") == "United Kingdom"


def test_combobox_with_duplicate_exact_options_stops(tmp_path, server_page):
    _, page, _, result = run_case(tmp_path, server_page, "combobox_ambiguous")
    assert result.status == "needs_human_input"
    assert escalations(result) == {"Country *": "combobox_ambiguous_option"}
    assert page.eval_on_selector("#co", "el => el.value") == ""


# 3. submit-type Next ------------------------------------------------------------------------

def test_submit_type_next_is_never_clicked(tmp_path, server_page):
    server, _, _, result = run_case(tmp_path, server_page, "submit_next")
    assert result.status == "needs_human_input"
    assert result.stop_reason == "advance_control_not_provably_non_submit"
    assert result.advance_clicks == []
    assert server.requests == [("GET", HOST, "/submit_next/apply.html")]


# 4. link / custom-button Continue ------------------------------------------------------------

@pytest.mark.parametrize("case", ["anchor_continue", "role_button"])
def test_same_host_continue_navigation_is_followed(tmp_path, server_page, case):
    server, _, _, result = run_case(tmp_path, server_page, case)
    assert result.status == "ready_for_human_review"
    assert result.advance_clicks == ["Continue"] and result.pages_visited == 2
    assert filled(result) == ["legal_first_name", "legal_last_name"]
    assert server.requests == [("GET", HOST, f"/{case}/apply.html"), ("GET", HOST, f"/{case}/step-2.html")]


# 5. dates ------------------------------------------------------------------------------------

def test_native_date_filled_only_from_an_exact_iso_value(tmp_path, server_page):
    _, page, _, result = run_case(tmp_path, server_page, "dates")
    assert page.eval_on_selector("#pg", "el => el.value") == "2025-09-22"
    assert page.eval_on_selector("#ug", "el => el.value") == ""  # "2020-09" is never padded to a guessed day
    assert escalations(result) == {"Undergraduate start date *": "date_value_format_mismatch"}
    assert result.status == "needs_human_input"


def test_custom_split_date_widget_stops(tmp_path, server_page):
    _, _, _, result = run_case(tmp_path, server_page, "custom_date")
    assert result.status == "needs_human_input"
    assert escalations(result) == {"Day": "unknown_field", "Month": "unknown_field", "Year": "unknown_field"}


# 6 + 9. dynamic and hidden fields ---------------------------------------------------------------

def test_revealed_fields_are_rescanned_filled_or_escalated(tmp_path, server_page):
    _, page, _, result = run_case(tmp_path, server_page, "dynamic")
    assert filled(result) == ["legal_first_name", "has_driving_licence", "driving_licence_country"]
    assert page.eval_on_selector("#dlc", "el => el.value") == "Sampleland"
    assert escalations(result) == {"Licence number *": "unknown_field"}
    assert result.status == "needs_human_input"


def test_hidden_and_disabled_required_fields_are_not_touched(tmp_path, server_page):
    _, page, _, result = run_case(tmp_path, server_page, "hidden")
    assert result.status == "ready_for_human_review" and result.questions_escalated == []
    assert filled(result) == ["legal_first_name"]
    assert [page.eval_on_selector(f"#{i}", "el => el.value") for i in ("h1", "h2", "dis")] == ["", "", ""]


# 7. autosave / network writes --------------------------------------------------------------------

def test_autosave_is_blocked_without_an_explicit_allowlist(tmp_path, server_page):
    server, _, _, result = run_case(tmp_path, server_page, "autosave")
    assert result.status == "needs_human_input" and result.stop_reason == "submission_attempt_blocked"
    assert [r["url"] for r in result.blocked_requests][:1] == [AUTOSAVE[1]]
    assert server.writes() == []


def test_allowlisted_autosave_passes_and_final_submit_stays_blocked(tmp_path, server_page):
    server, _, _, result = run_case(tmp_path, server_page, "autosave", write_allowlist=[AUTOSAVE])
    assert result.status == "ready_for_human_review"
    assert result.blocked_requests == []
    assert {(w["method"], w["url"]) for w in result.allowed_writes} == {AUTOSAVE}
    assert set(server.writes()) == {("POST", "/autosave/draft")}


def test_unexpected_write_is_blocked_even_with_an_allowlist(tmp_path, server_page):
    server, _, _, result = run_case(tmp_path, server_page, "autosave", query="?extra=analytics",
                                    write_allowlist=[AUTOSAVE])
    assert result.status == "needs_human_input" and result.stop_reason == "submission_attempt_blocked"
    assert {r["url"] for r in result.blocked_requests} == {f"https://{HOST}/analytics/track"}
    assert ("POST", "/analytics/track") not in server.writes()


@pytest.mark.parametrize("entry", [("POST", f"https://{HOST}/submit"), ("POST", f"https://{HOST}/apply/finish"),
                                   ("POST", "https://elsewhere.test/autosave"), ("GET", f"https://{HOST}/autosave"),
                                   ("POST", f"http://{HOST}/autosave/draft"), ("POST", f"https://{HOST}/*")])
def test_allowlist_rejects_final_foreign_or_wildcard_endpoints(tmp_path, entry):
    bundle = bundle_for(tmp_path, "autosave")
    with pytest.raises(ValueError):
        validate_write_allowlist(bundle, [entry])


# 8. client-side validation -------------------------------------------------------------------------

def test_client_side_validation_failure_stops(tmp_path, server_page):
    _, _, _, result = run_case(tmp_path, server_page, "validation")
    assert result.status == "needs_human_input"
    assert escalations(result) == {"Email *": "client_validation_failed", "Phone number *": "client_validation_failed"}


# 10. duplicate labels ------------------------------------------------------------------------------

def test_duplicate_labels_are_ambiguous_and_not_filled(tmp_path, server_page):
    _, page, _, result = run_case(tmp_path, server_page, "duplicate")
    assert filled(result) == ["legal_first_name"]
    assert [q["screenshot_or_evidence"] for q in result.questions_escalated] == ["runner:duplicate_label"] * 2
    assert [page.eval_on_selector(f"#{i}", "el => el.value") for i in ("em1", "em2")] == ["", ""]


# 11. shadow DOM ------------------------------------------------------------------------------------

def test_open_shadow_dom_form_is_filled_and_its_submit_is_guarded(tmp_path, server_page):
    server, page, _, result = run_case(tmp_path, server_page, "shadow")
    assert result.status == "ready_for_human_review"
    assert filled(result) == ["legal_first_name", "email"]
    assert result.final_submit_seen is True
    page.press("#em", "Enter")  # deliberate implicit submit inside the shadow root
    page.wait_for_timeout(200)
    assert "submit event" in page.evaluate("() => [...window.__jobintelBlockedSubmits]")
    assert server.writes() == []


# hygiene ------------------------------------------------------------------------------------------

def test_no_candidate_literals_or_vendor_conditionals():
    from jobintel.submission import browserbase_runner
    import inspect
    reference = _canonical_bank_values(skip_if_absent=False)  # empty in a public checkout
    texts = [inspect.getsource(browserbase_runner)] + [p.read_text() for p in ROOT.rglob("*.html")]
    assert sum(1 for v in reference for t in texts if v in t) == 0
    source = inspect.getsource(browserbase_runner).casefold()
    for vendor in ("greenhouse", "workday", "ashby", "lever", "smartrecruiters", "workable"):
        assert vendor not in source
