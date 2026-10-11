"""Offline stop-only browser runner against local static fixtures. No network.

Fixtures are served in-process by a Playwright route on the reserved host
jobs.example.test (RFC 2606 .test); every other request is aborted and recorded.
The browser is the locally installed Chrome, headless, with background networking
disabled. Answers are synthetic; nothing reads the live Answer Bank or profile.
"""
from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage  # noqa: E402
from jobintel.submission import browserbase_runner, stop_only  # noqa: E402
from jobintel.submission.browserbase import BrowserbaseBundleAuthorization, write_bundle  # noqa: E402
from jobintel.submission.browserbase_runner import load_bundle, run, to_stop_only_report  # noqa: E402
from jobintel.submission.executors import STOP_ONLY_EXECUTORS  # noqa: E402
from test_handoff_fail_closed import _canonical_bank_values  # noqa: E402
from test_submission_service import approve, setup  # noqa: E402,F401  (fixture)

FIXTURES = Path(__file__).parent / "fixtures" / "browser_forms"
HOST = "jobs.example.test"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
SYNTHETIC_ANSWERS = {
    "legal_first_name": "Sam", "legal_last_name": "Sample", "legal_full_name": "Sam Sample",
    "email": "sam@example.org", "phone_uk": "07111111111",
    "full_address": "2 Sample Road, Sampleton, SA1 2BB, United Kingdom", "country": "United Kingdom",
    "right_to_work_uk": "Yes", "permanent_or_unrestricted_right_to_work": "No",
    "require_sponsorship_now_or_future": "Yes", "nationality": "Samplish",
    "travel": "Yes", "has_driving_licence": "Yes",
}
CHROME_ARGS = ["--disable-background-networking", "--no-first-run", "--disable-component-update", "--disable-sync",
               "--disable-default-apps", "--disable-extensions"]


# --- harness -------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch(channel="chrome", headless=True, args=CHROME_ARGS)
        except Exception as exc:  # pragma: no cover - environment without local Chrome
            pytest.skip(f"local Chrome unavailable: {exc}")
        yield instance
        instance.close()


class Server:
    """In-process fixture server: records every request; nothing leaves the machine."""

    def __init__(self):
        self.requests, self.external = [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname != HOST:
            self.external.append(request.url)
            return route.abort()
        self.requests.append((request.method, parts.path))
        target = (FIXTURES / parts.path.lstrip("/")).resolve()
        if not target.is_relative_to(FIXTURES.resolve()) or not target.is_file():
            return route.fulfill(status=404, body="not found")
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=target.read_text())

    @property
    def submit_requests(self):
        return [r for r in self.requests if r[0] != "GET" or r[1].endswith("/submit")]


@pytest.fixture
def server_page(browser):
    server = Server()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def make_bundle(tmp_path, case, answers=SYNTHETIC_ANSWERS, company="ExampleCo", role="Graduate Engineer"):
    sealed = tmp_path / "sealed" / "cv.pdf"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(b"%PDF-1.4 synthetic offline runner CV " + case.encode())
    sha = hashlib.sha256(sealed.read_bytes()).hexdigest()
    package = ApprovedSubmissionPackage(
        "app:offline-1", "job:offline-1", f"https://{HOST}/{case}/apply.html", "cv-0123456789ab", sha, str(sealed),
        tuple(AnswerSnapshot(f, v, "synthetic", "v1") for f, v in sorted(answers.items())), "input-hash",
        "auto_prepare", (), company=company, role=role)
    auth = BrowserbaseBundleAuthorization("approval:offline", f"request:offline-{case}", package.package_hash,
                                          (NOW + timedelta(days=1)).isoformat(), True, "approved")
    directory = write_bundle(tmp_path / "bundles", package, auth, clock=lambda: NOW)
    return load_bundle(directory), package


def dom_values(page, ids):
    return {i: page.eval_on_selector(f"#{i}", "el => el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value")
            for i in ids}


def assert_no_submission(server, result):
    assert server.submit_requests == []
    assert server.external == []
    assert result.final_submit_clicked is False
    assert result.blocked_requests == []


def assert_values_from_bundle(result, bundle):
    sealed = {(a.field, a.value) for a in bundle.answers}
    assert all((f["field"], f["value"]) in sealed for f in result.fields_filled)


# --- A. safe one-page form ------------------------------------------------------------------

def test_a_safe_form_filled_from_bundle_and_stops_before_submit(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "safe")
    result = run(page, bundle)
    assert result.status == "ready_for_human_review"
    assert result.stop_reason == "final_page_reached_stop_before_submit"
    assert result.final_submit_seen is True
    assert {f["field"] for f in result.fields_filled} == {"legal_first_name", "legal_last_name", "email", "country",
                                                           "travel", "has_driving_licence"}
    assert_values_from_bundle(result, bundle)
    assert dom_values(page, ["fn", "ln", "em", "co", "dl", "li"]) == {
        "fn": "Sam", "ln": "Sample", "em": "sam@example.org", "co": "uk", "dl": True, "li": ""}
    assert page.eval_on_selector("input[name=travel][value=y]", "el => el.checked") is True
    assert result.fields_skipped == [{"label": "LinkedIn", "reason": "no_approved_answer"}]
    assert server.requests == [("GET", "/safe/apply.html")]
    assert_no_submission(server, result)


# --- B. unknown required field ---------------------------------------------------------------

def test_b_unknown_required_field_stops_and_is_not_invented(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "unknown")
    result = run(page, bundle)
    assert result.status == "needs_human_input" and result.stop_reason == "questions_escalated"
    assert [(q["question"], q["screenshot_or_evidence"]) for q in result.questions_escalated] == [
        ("Favourite programming paradigm *", "runner:unknown_field")]
    assert all(q["allowed_known_answers"] == [] for q in result.questions_escalated)
    assert dom_values(page, ["fav"]) == {"fav": ""}
    assert_values_from_bundle(result, bundle)
    assert_no_submission(server, result)


# --- C. sensitive question --------------------------------------------------------------------

def test_c_sensitive_questions_escalate_even_with_an_approved_value(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "sensitive")
    assert {"nationality", "require_sponsorship_now_or_future"} <= {a.field for a in bundle.answers}
    result = run(page, bundle)
    assert result.status == "needs_human_input"
    reasons = {q["question"]: q["screenshot_or_evidence"] for q in result.questions_escalated}
    assert reasons == {"Nationality": "runner:sensitive:nationality"}
    assert dom_values(page, ["nat"]) == {"nat": ""}
    # The one unambiguous sponsorship wording (stop_only.DETERMINISTIC_QUESTIONS) is answered from the
    # approved package; any other sponsorship / right-to-work wording still escalates.
    assert page.eval_on_selector_all("input[name=spons]", "els => els.filter(e => e.checked).map(e => e.value)") == ["y"]
    assert [f["field"] for f in result.fields_filled] == ["email", "require_sponsorship_now_or_future"]
    assert_no_submission(server, result)


# --- D. multi-page -----------------------------------------------------------------------------

def test_d_multi_page_next_then_stop_at_final_submit(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "multipage")
    result = run(page, bundle)
    assert result.status == "ready_for_human_review"
    assert result.advance_clicks == ["Next"] and result.pages_visited == 2
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email", "legal_last_name", "country"]
    assert result.start_identity["url"] == bundle.application_url and result.page_identity["page"] == 2
    assert server.requests == [("GET", "/multipage/apply.html"), ("GET", "/multipage/step-2.html")]
    assert_no_submission(server, result)


# --- E. CV upload --------------------------------------------------------------------------------

def test_e_exact_sealed_cv_selected_after_hash_check(tmp_path, server_page):
    server, page = server_page
    bundle, package = make_bundle(tmp_path, "cv")
    result = run(page, bundle)
    sealed = bundle.cv_path.read_bytes()
    assert hashlib.sha256(sealed).hexdigest() == package.cv_pdf_sha256 == bundle.request["cv"]["sha256"]
    assert result.cv_upload["sha256_verified_before_selection"] is True
    assert result.cv_upload["sealed_sha256"] == package.cv_pdf_sha256
    assert result.cv_upload["dom_selected_files"] == [{"name": "cv.pdf", "size": len(sealed)}]
    assert result.cv_upload["dom_matches_sealed_file"] is True
    assert result.cv_upload["remote_integrity_verified"] is False
    assert result.cv_upload["human_confirmation_required"] is True
    assert result.status == "ready_for_human_review"
    assert_no_submission(server, result)


def test_e_cv_hash_mismatch_selects_nothing(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "cv")
    wrong = replace(bundle, request={**bundle.request, "cv": {**bundle.request["cv"], "sha256": "0" * 64}})
    result = run(page, wrong)
    assert result.status == "needs_human_input"
    assert [q["screenshot_or_evidence"] for q in result.questions_escalated] == ["runner:sealed_cv_hash_mismatch"]
    assert result.cv_upload == {}
    assert page.eval_on_selector("#cvfile", "el => el.files.length") == 0
    assert_no_submission(server, result)


def test_e_tampered_bundle_is_refused_before_any_browser_use(tmp_path):
    bundle, _ = make_bundle(tmp_path, "cv")
    cv = bundle.cv_path
    cv.chmod(0o600)
    cv.write_bytes(b"%PDF-1.4 edited after sealing")
    with pytest.raises(ValueError, match="changed after sealing"):
        load_bundle(cv.parent)


# --- F. decoys -----------------------------------------------------------------------------------

def test_f_only_the_safe_next_is_ever_clicked(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "decoy")
    result = run(page, bundle)
    assert result.advance_clicks == ["Next"]
    assert result.status == "ready_for_human_review" and result.pages_visited == 2
    assert server.requests == [("GET", "/decoy/apply.html"), ("GET", "/decoy/step-2.html")]
    assert_no_submission(server, result)


def test_f_decoy_page_click_log_contains_only_next(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "decoy")
    clicks = []
    page.expose_function("__recordClick", lambda text: clicks.append(text))
    page.add_init_script("document.addEventListener('click', e => { const b = e.target.closest('button');"
                         " if (b) window.__recordClick(b.innerText.trim()); }, true);")
    result = run(page, bundle)
    assert clicks == ["Next"]
    assert_no_submission(server, result)


def test_f_next_that_may_be_a_submit_button_stops_without_clicking(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "decoy_unsafe")
    result = run(page, bundle)
    assert result.status == "needs_human_input"
    assert result.stop_reason == "advance_control_not_provably_non_submit"
    assert result.advance_clicks == [] and result.pages_visited == 1
    assert_no_submission(server, result)


# --- G. implicit submission --------------------------------------------------------------------

def test_g_enter_cannot_submit_the_form(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "enter")
    result = run(page, bundle)
    assert result.status == "needs_human_input" and result.stop_reason == "no_safe_navigation"
    assert [f["field"] for f in result.fields_filled] == ["email"]
    # Even a deliberate Enter in the field (the runner never presses keys) is blocked by the guard.
    page.press("#em", "Enter")
    page.wait_for_timeout(200)
    assert page.evaluate("() => [...window.__jobintelBlockedSubmits]") == ["submit event"]
    assert page.url == bundle.application_url
    assert_no_submission(server, result)


def test_g_page_script_calling_request_submit_is_blocked_and_stops(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "autosubmit")
    result = run(page, bundle)
    assert result.status == "needs_human_input" and result.stop_reason == "submission_attempt_blocked"
    assert result.blocked_submissions == ["form.requestSubmit()"]
    assert_no_submission(server, result)


# --- H. page mismatch ---------------------------------------------------------------------------

def test_h_page_mismatch_fills_nothing(tmp_path, server_page):
    server, page = server_page
    bundle, _ = make_bundle(tmp_path, "mismatch")
    result = run(page, bundle)
    assert result.status == "aborted_wrong_job" and result.stop_reason == "page_identity_mismatch"
    assert result.fields_filled == [] and result.questions_escalated == []
    assert dom_values(page, ["fn", "em"]) == {"fn": "", "em": ""}
    assert_no_submission(server, result)


# --- shared stop-only contract ------------------------------------------------------------------

@pytest.mark.parametrize("case,expected", [("safe", "needs_human_input"), ("sensitive", "needs_human_input"),
                                           ("mismatch", "failed")])
def test_evidence_maps_onto_the_shared_stop_only_import(tmp_path, server_page, case, expected):
    _, page = server_page
    bundle, package = make_bundle(tmp_path, case)
    result = run(page, bundle)
    report = to_stop_only_report(result, bundle, task_id="offline-run-1")
    imported = stop_only.import_result(report, package=package, request_id=bundle.request["request_id"],
                                       approval_id=bundle.request["approval_id"], executor="browserbase",
                                       display_name="Browserbase", deterministic_questions=True)
    # cv_upload_confirmed stays False until a human confirms, so even a clean run is not "ready"; never a receipt.
    assert imported.outcome == expected and imported.submitted_at is None
    assert report["final_submit_clicked"] is False and report["execution_mode"] == stop_only.MODE


# --- purity, literals, registration -------------------------------------------------------------

def test_runner_imports_no_answer_bank_profile_resolution_or_cloud_dependency():
    tree = ast.parse(inspect.getsource(browserbase_runner))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert imported <= {"__future__", "annotations", "dataclasses", "dataclass", "field", "hashlib", "json", "pathlib",
                        "Path", "re", "urllib.parse", "urlsplit", "jobintel.models.submission", "AnswerSnapshot",
                        "jobintel.submission", "stop_only", "graphql_policy"}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & {"environ", "getenv", "putenv", "system", "popen", "urlopen", "connect_over_cdp"}
    # Executable code only (docstrings may say what the module does NOT use).
    scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef)))]
    docstrings = {id(x.body[0].value) for x in scopes if x.body and isinstance(x.body[0], ast.Expr)
                  and isinstance(x.body[0].value, ast.Constant) and isinstance(x.body[0].value.value, str)}
    code = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]
    code += [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)] + sorted(imported) + sorted(attributes)
    for forbidden in ("stagehand", "playwright", "browserbase_api", "openai", "anthropic", "authoritative_answers",
                      "question_resolution", "profile_ingestion", "local_store", "application_store", "api_key",
                      "answerbank", "answer_bank"):
        assert not any(forbidden in token.casefold() for token in code), forbidden


def test_no_candidate_literals_in_runner_or_fixtures():
    reference = _canonical_bank_values()
    texts = [inspect.getsource(browserbase_runner)] + [p.read_text() for p in FIXTURES.rglob("*.html")]
    leaked = sum(1 for v in reference for t in texts if v in t)
    assert leaked == 0  # count only, never the values


def test_browserbase_is_a_stop_only_executor_and_unregistered_backends_are_refused(setup, capsys):
    assert STOP_ONLY_EXECUTORS == frozenset({"airtap", "browserbase", "local_browser"})
    approval = approve(setup)
    # Browserbase only exists as a stop-only executor of the service; an unregistered backend is refused.
    with pytest.raises(ValueError, match="Unknown executor"):
        setup.svc.enqueue(approval["id"], executor="unregistered_backend")
    assert setup.svc.store.requests() == {}
    from jobintel.cli.main import main
    with patch("sys.argv", ["jobintel", "submission-enqueue", "--approval-id", approval["id"], "--executor",
                            "unregistered_backend", "--store", str(setup.root)]), pytest.raises(SystemExit):
        main()
    assert "invalid choice: 'unregistered_backend'" in capsys.readouterr().err
    assert setup.svc.store.requests() == {}
