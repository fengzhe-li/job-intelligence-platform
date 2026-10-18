"""Workable-hosted execution profile (offline): the unchanged stop-only runner against a synthetic
Workable-style application form in local headless Chrome (every other host is unreachable).

The sealed CV goes into the resume control (its own label only "Choose file"; its aria-labelledby names
"Resume") and the page POSTs it once to the exact storage host inside the CV window (sealed bytes only); the
same-host challenge beacon passes as page security; error reports are blocked but non-fatal; deterministic
fields (incl. the free-text current-location question) are filled; the right-to-work document, background-check
radios, the essay and GDPR consent stay untouched; any other write is fatal; final Submit is never clicked.
The short /j/<code>/apply URL maps to the account's form URL only with the recorded account.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage
from jobintel.submission import browserbase_runner
from jobintel.submission.ats_profiles import PROFILES, WORKABLE_HOSTED, profile_for_url
from jobintel.submission.browserbase import BrowserbaseBundleAuthorization, write_bundle
from jobintel.submission.browserbase_runner import CvUploadPolicy, load_bundle
from jobintel.submission.hosted_urls import hosted_application_url

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")
HOST = "apply.example.test"
STORAGE = "forms.example-storage.test"
ERRORS = "api.example-errors.test"
APPLY = f"https://{HOST}/exampleco/j/ABC123/apply"
FIXTURE = Path(__file__).parent / "fixtures" / "workable" / "apply.html"
CV_BYTES = b"%PDF-1.4 synthetic workable CV bytes"
ANSWERS = {"name": "Sam Sample", "legal_full_name": "Sam Sample", "legal_first_name": "Sam", "legal_last_name": "Sample",
           "first_name": "Sam", "last_name": "Sample", "email": "sam@example.org", "phone": "07111111111",
           "location": "Sampleton, United Kingdom", "country": "United Kingdom", "city": "Sampleton",
           "linkedin_url": "https://www.linkedin.com/in/sam-sample", "github_url": "https://github.com/sam-sample",
           "require_sponsorship_now_or_future": "Yes", "right_to_work_uk": "Yes",
           "permanent_or_unrestricted_right_to_work": "No", "full_address": "2 Sample Road, Sampleton, SA1 2BB"}
SECURITY = (("POST", HOST, "/cdn-cgi/challenge-platform/"),)


def bundle_for(tmp_path, query=""):
    sealed = tmp_path / "sealed" / "cv.pdf"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(CV_BYTES)
    package = ApprovedSubmissionPackage(
        "app:wk-1", "workable:1", APPLY + query, "cv-0123456789ab", hashlib.sha256(CV_BYTES).hexdigest(), str(sealed),
        tuple(AnswerSnapshot(f, v, "synthetic", "v1") for f, v in sorted(ANSWERS.items())), "input-hash",
        "auto_prepare", (), company="ExampleCo", role="Graduate Engineer")
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    auth = BrowserbaseBundleAuthorization("approval:wk", "request:wk" + query, package.package_hash,
                                          (now + timedelta(days=1)).isoformat(), True, "approved")
    return load_bundle(write_bundle(tmp_path / "bundles", package, auth, clock=lambda: now))


def policy_for(bundle):
    return CvUploadPolicy(bundle.request["cv"]["artifact_id"], bundle.request["cv"]["sha256"], "Sam_Sample_CV.pdf",
                          storage_writes=frozenset({("POST", STORAGE)}))


def test_the_workable_profile_is_registered_but_not_live_validated():
    profile = profile_for_url("https://apply.workable.com/starling-bank/j/5D5584B013/apply")
    assert profile is WORKABLE_HOSTED and PROFILES["apply.workable.com"] is WORKABLE_HOSTED
    assert profile.live_validated is False
    assert profile.cv_storage_writes == (("POST", "workable-application-form.s3.us-east-1.amazonaws.com"),)
    assert profile.page_security_writes == (("POST", "apply.workable.com", "/cdn-cgi/challenge-platform/"),)
    assert profile.nonfatal_blocked_writes == (("POST", "api.rollbar.com"), ("POST", "region1.google-analytics.com"))
    assert not (profile.cv_upload_mutations or profile.cv_association_mutations or profile.cv_parse_writes)


def test_the_short_url_maps_to_the_account_form_only_with_the_recorded_account():
    short = "https://apply.workable.com/j/5D5584B013/apply"
    assert hosted_application_url(short, workable_account="starling-bank") == \
        "https://apply.workable.com/starling-bank/j/5D5584B013/apply"
    assert hosted_application_url(short) == short  # never guessed
    assert hosted_application_url(short, workable_account="Bad Account!") == short


sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser  # noqa: E402,F401  (fixture)


class WorkableServer:
    def __init__(self):
        self.requests, self.external, self.stored, self.submits, self.security = [], [], [], [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname == STORAGE and request.method == "POST":
            self.stored.append(request.post_data_buffer or b"")
            status = 500 if b"storage_fail" in (request.post_data_buffer or b"") else 204
            return route.fulfill(status=status, headers={"access-control-allow-origin": "*"}, body="")
        if parts.hostname != HOST:
            self.external.append((request.method, parts.hostname))
            return route.abort()
        self.requests.append((request.method, parts.path))
        if parts.path.startswith("/cdn-cgi/challenge-platform/"):
            self.security.append(parts.path)
            return route.fulfill(status=200, body="")
        if request.method == "POST":
            self.submits.append(parts.path)
            return route.fulfill(status=200, body="")
        if parts.path.startswith("/exampleco/j/"):
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text())
        return route.fulfill(status=404, body="not found")


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = WorkableServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def run(tmp_path, server_page, query="?mode=ok"):
    server, page = server_page
    bundle = bundle_for(tmp_path, query)
    result = browserbase_runner.run(page, bundle, live_host=HOST, cv_upload=policy_for(bundle),
                                    nonfatal_blocked_writes=(("POST", ERRORS),), page_security_writes=SECURITY)
    assert result.final_submit_clicked is False and server.external == []
    assert page.evaluate("() => document.body.dataset.submitted || '0'") == "0"
    assert not any(p.startswith("/exampleco/j/") for p in server.submits)
    return server, page, result


def value(page, selector):
    return page.eval_on_selector(selector, "el => el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value")


def test_workable_form_fills_deterministic_fields_uploads_the_sealed_cv_once_and_stops(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page)
    assert result.cv_upload_status == "uploaded"
    assert len(server.stored) == 1 and CV_BYTES in server.stored[0]
    assert len(server.security) == 1  # the challenge beacon passed as page security
    assert [b["host"] for b in result.nonfatal_blocked_requests] == [ERRORS]
    filled = {f["field"] for f in result.fields_filled}
    assert {"email", "phone", "location"} <= filled
    assert filled & {"first_name", "legal_first_name"} and filled & {"last_name", "legal_last_name"}
    assert value(page, "#loc") == "Sampleton, United Kingdom"
    # human-only: right-to-work document, background-check radios, essay, consent
    assert value(page, "#rtw") == "" and value(page, "#why") == "" and value(page, "#gdpr") is False
    assert not any(page.eval_on_selector_all("input[name=CA_3]", "els => els.map(e => e.checked)"))


@pytest.mark.parametrize("query", ["?mode=storage_fail", "?mode=no_name"])
def test_an_unconfirmed_workable_upload_is_escalated_not_claimed(tmp_path, server_page, query):
    server, page, result = run(tmp_path, server_page, query)
    assert result.cv_upload_status != "uploaded"
    assert any(q["screenshot_or_evidence"] == "runner:cv_upload_not_confirmed" for q in result.questions_escalated)


def test_any_other_same_host_write_is_fatal(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page, "?mode=extra_post")
    assert result.stop_reason == "unexpected_write_blocked" and "/api/v1/telemetry" not in server.submits
