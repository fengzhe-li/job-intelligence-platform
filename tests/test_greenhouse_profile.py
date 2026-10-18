"""Greenhouse-hosted execution profile (offline): the unchanged stop-only runner against a synthetic
Greenhouse-style job-board form in local headless Chrome (every other host is unreachable).

The sealed CV goes into the resume control (titled by its group, its own label only "Attach") and the page
POSTs it once to the exact storage host inside the CV window (only with the sealed bytes); deterministic
fields are filled; sponsorship, relocation, "how did you hear", the acknowledgement and the cover letter stay
untouched; analytics is blocked but non-fatal; any other write is fatal; final Submit is never clicked. The
production profile exists but is NOT live-validated, and the legacy board URL maps to the hosted form URL.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage
from jobintel.submission import browserbase_runner
from jobintel.submission.ats_profiles import GREENHOUSE_HOSTED, PROFILES, profile_for_url
from jobintel.submission.browserbase import BrowserbaseBundleAuthorization, write_bundle
from jobintel.submission.browserbase_runner import CvUploadPolicy, load_bundle
from jobintel.submission.hosted_urls import hosted_application_url

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")
HOST = "boards.example.test"
STORAGE = "uploads.example-storage.test"
ANALYTICS = "c.spl.example-analytics.test"
APPLY = f"https://{HOST}/exampleco/jobs/1234567"
FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse" / "apply.html"
CV_BYTES = b"%PDF-1.4 synthetic greenhouse CV bytes"
ANSWERS = {"name": "Sam Sample", "legal_full_name": "Sam Sample", "legal_first_name": "Sam", "legal_last_name": "Sample",
           "first_name": "Sam", "last_name": "Sample", "email": "sam@example.org", "phone": "07111111111",
           "location": "Sampleton, United Kingdom", "country": "United Kingdom", "city": "Sampleton",
           "linkedin_url": "https://www.linkedin.com/in/sam-sample", "github_url": "https://github.com/sam-sample",
           "require_sponsorship_now_or_future": "Yes", "right_to_work_uk": "Yes",
           "permanent_or_unrestricted_right_to_work": "No", "full_address": "2 Sample Road, Sampleton, SA1 2BB"}


def bundle_for(tmp_path, query=""):
    sealed = tmp_path / "sealed" / "cv.pdf"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(CV_BYTES)
    package = ApprovedSubmissionPackage(
        "app:gh-1", "greenhouse:1", APPLY + query, "cv-0123456789ab", hashlib.sha256(CV_BYTES).hexdigest(), str(sealed),
        tuple(AnswerSnapshot(f, v, "synthetic", "v1") for f, v in sorted(ANSWERS.items())), "input-hash",
        "auto_prepare", (), company="ExampleCo", role="Graduate Engineer")
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    auth = BrowserbaseBundleAuthorization("approval:gh", "request:gh" + query, package.package_hash,
                                          (now + timedelta(days=1)).isoformat(), True, "approved")
    return load_bundle(write_bundle(tmp_path / "bundles", package, auth, clock=lambda: now))


def policy_for(bundle):
    return CvUploadPolicy(bundle.request["cv"]["artifact_id"], bundle.request["cv"]["sha256"], "Sam_Sample_CV.pdf",
                          storage_writes=frozenset({("POST", STORAGE)}))


# --- profile and URL mapping ------------------------------------------------------------------------------

def test_the_greenhouse_profile_is_registered_and_live_validated():
    profile = profile_for_url("https://job-boards.greenhouse.io/cloudflare/jobs/8264687?gh_jid=8264687")
    assert profile is GREENHOUSE_HOSTED and PROFILES["job-boards.greenhouse.io"] is GREENHOUSE_HOSTED
    assert profile.live_validated is True
    assert profile.cv_storage_writes == (("POST", "grnhse-prod-jben-us-east-1.s3.amazonaws.com"),)
    assert not (profile.cv_upload_mutations or profile.cv_association_mutations or profile.cv_parse_writes
                or profile.graphql_form_writes or profile.page_security_writes)
    assert profile.nonfatal_blocked_writes == (("POST", "c.spl.greenhouse.io"),)


def test_the_legacy_board_url_maps_only_to_its_hosted_form_url():
    assert (hosted_application_url("https://boards.greenhouse.io/cloudflare/jobs/8264687?gh_jid=8264687")
            == "https://job-boards.greenhouse.io/cloudflare/jobs/8264687?gh_jid=8264687")
    for other in ("https://www.cloudflare.com/careers/jobs/?gh_jid=1", "https://boards.greenhouse.io/x/jobs/abc",
                  "http://boards.greenhouse.io/x/jobs/1", "https://jobs.lever.co/x/1/apply"):
        assert hosted_application_url(other) == other


def test_an_application_url_moves_only_along_the_hosted_mapping(tmp_path):
    from jobintel.models.application import Application
    from jobintel.storage.application_store import ApplicationStore
    store = ApplicationStore(tmp_path)
    legacy = "https://boards.greenhouse.io/exampleco/jobs/42?gh_jid=42"
    store.create_application(Application("app:greenhouse:42", "greenhouse:42", "ExampleCo", "Engineer", application_url=legacy))
    with pytest.raises(ValueError, match="hosted form URL"):
        store.update_application_url("app:greenhouse:42", "https://evil.example/jobs/42")
    updated = store.update_application_url("app:greenhouse:42", hosted_application_url(legacy))
    assert updated.application_url == "https://job-boards.greenhouse.io/exampleco/jobs/42?gh_jid=42"
    assert "hosted form URL" in store.status_history_for("app:greenhouse:42")[-1].note


# --- the real runner in local headless Chrome --------------------------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser  # noqa: E402,F401  (fixture)


class GreenhouseServer:
    def __init__(self):
        self.requests, self.external, self.stored, self.submits = [], [], [], []

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
        if request.method == "GET" and parts.path == "/uploads/presign":
            return route.fulfill(status=200, content_type="application/json", body="{}")
        if request.method == "POST":
            self.submits.append(parts.path)
            return route.fulfill(status=200, body="")
        if parts.path.startswith("/exampleco/jobs/"):
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text())
        return route.fulfill(status=404, body="not found")


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = GreenhouseServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def run(tmp_path, server_page, query="?mode=ok"):
    server, page = server_page
    bundle = bundle_for(tmp_path, query)
    result = browserbase_runner.run(page, bundle, live_host=HOST, cv_upload=policy_for(bundle),
                                    nonfatal_blocked_writes=(("POST", ANALYTICS),))
    assert result.final_submit_clicked is False and server.external == []
    assert page.evaluate("() => document.body.dataset.submitted || '0'") == "0"
    assert not any(p.startswith("/exampleco/jobs/") for p in server.submits)  # the form itself was never POSTed
    return server, page, result


def value(page, selector):
    return page.eval_on_selector(selector, "el => el.type === 'checkbox' ? el.checked : el.value")


def test_greenhouse_form_fills_deterministic_fields_uploads_the_sealed_cv_once_and_stops(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page)
    assert result.cv_upload_status == "uploaded"
    assert len(server.stored) == 1 and CV_BYTES in server.stored[0]  # exactly the sealed bytes, once
    assert ("GET", "/uploads/presign") in server.requests
    filled = {f["field"] for f in result.fields_filled}
    assert {"email", "phone"} <= filled and filled & {"first_name", "legal_first_name"} and filled & {"last_name", "legal_last_name"}
    shown = lambda sel: page.eval_on_selector(sel, "el => el.parentElement.querySelector('.select__single-value').textContent")
    assert shown("#country") == "+44"  # "United Kingdom +44" picked; the widget shows the dialling code
    assert shown("#candidate-location") == "Sampleton, England, United Kingdom"  # never East / Ontario / -derry
    assert {"country", "location"} <= filled
    # a React-Select pick (empty input, choice shown beside it) is an answered field, never "unanswered"
    assert not any(q["screenshot_or_evidence"] == "runner:required_field_unanswered" for q in result.questions_escalated)
    # human-only / judgement / consent / the cover letter: untouched
    assert value(page, "#q_spons") == "" and value(page, "#q_heard") == "" and value(page, "#q_ack") is False
    assert page.eval_on_selector("#cover_letter", "el => el.files.length") == 0
    assert "require_sponsorship_now_or_future" not in filled
    assert [b["host"] for b in result.nonfatal_blocked_requests] == [ANALYTICS]  # blocked, never sent
    assert result.status in {"needs_human_input", "ready_for_human_review"}


@pytest.mark.parametrize("query,expect", [("?mode=storage_fail", "cv_upload_not_confirmed"),
                                          ("?mode=no_name", "cv_upload_not_confirmed")])
def test_an_unconfirmed_greenhouse_upload_is_escalated_not_claimed(tmp_path, server_page, query, expect):
    server, page, result = run(tmp_path, server_page, query)
    assert result.cv_upload_status != "uploaded"
    assert any(q["screenshot_or_evidence"] == f"runner:{expect}" for q in result.questions_escalated)


def test_a_second_storage_upload_is_refused(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page, "?mode=storage_twice")
    assert len(server.stored) == 1
    assert any(b.get("kind") == "cv_storage_refused" for b in result.blocked_requests)


def test_any_other_same_host_write_is_fatal(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page, "?mode=extra_post")
    assert result.stop_reason == "unexpected_write_blocked" and "/telemetry" not in server.submits
