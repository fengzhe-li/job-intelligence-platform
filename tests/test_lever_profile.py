"""Lever-hosted execution profile (offline): the unchanged stop-only runner against a synthetic
Lever-style form in local headless Chrome (every other host is unreachable).

The sealed CV is selected into the resume input and POSTed once to the same-host /parseResume inside
the CV window (only with the sealed bytes); deterministic fields are filled; sponsorship, AI-prohibited
free text and consent stay untouched; any other same-host write is fatal; final Submit is never
clicked. The production profile exists but is NOT live-validated, so apply_one refuses it by default.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage
from jobintel.submission import browserbase_runner
from jobintel.submission.ats_profiles import LEVER_HOSTED, PROFILES, profile_for_url
from jobintel.submission.browserbase import BrowserbaseBundleAuthorization, write_bundle
from jobintel.submission.browserbase_runner import (CvUploadPolicy, load_bundle, validate_cv_upload_policy,
                                                    validate_page_security_writes)

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")
HOST = "jobs.example.test"
APPLY = f"https://{HOST}/exampleco/0000aaaa-0000-0000-0000-000000000001/apply"
PARSE = ("POST", f"https://{HOST}/parseResume")
FIXTURE = Path(__file__).parent / "fixtures" / "lever" / "apply.html"
CV_BYTES = b"%PDF-1.4 synthetic lever CV bytes"
ANSWERS = {"name": "Sam Sample", "legal_full_name": "Sam Sample", "email": "sam@example.org", "phone": "07111111111",
           "location": "Sampleton, United Kingdom", "linkedin_url": "https://www.linkedin.com/in/sam-sample",
           "github_url": "https://github.com/sam-sample", "require_sponsorship_now_or_future": "Yes",
           "legal_first_name": "Sam", "legal_last_name": "Sample", "full_address": "2 Sample Road, Sampleton, SA1 2BB",
           "right_to_work_uk": "Yes", "permanent_or_unrestricted_right_to_work": "No"}


def bundle_for(tmp_path, query=""):
    sealed = tmp_path / "sealed" / "cv.pdf"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(CV_BYTES)
    package = ApprovedSubmissionPackage(
        "app:lever-1", "lever:1", APPLY + query, "cv-0123456789ab", hashlib.sha256(CV_BYTES).hexdigest(), str(sealed),
        tuple(AnswerSnapshot(f, v, "synthetic", "v1") for f, v in sorted(ANSWERS.items())), "input-hash",
        "auto_prepare", (), company="ExampleCo", role="Graduate Engineer")
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    auth = BrowserbaseBundleAuthorization("approval:lever", "request:lever" + query, package.package_hash,
                                          (now + timedelta(days=1)).isoformat(), True, "approved")
    return load_bundle(write_bundle(tmp_path / "bundles", package, auth, clock=lambda: now))


def policy_for(bundle, **changes):
    return replace(CvUploadPolicy(bundle.request["cv"]["artifact_id"], bundle.request["cv"]["sha256"],
                                  "Sam_Sample_CV.pdf", parse_writes=frozenset({PARSE})), **changes)


# --- profile and policy validation ----------------------------------------------------------------------

def test_the_lever_profile_is_registered_but_not_live_validated():
    profile = profile_for_url("https://jobs.lever.co/palantir/5168e8fd-fec1-4fea-b7a1-81bdaea65850/apply")
    assert profile is LEVER_HOSTED and PROFILES["jobs.lever.co"] is LEVER_HOSTED
    assert profile.live_validated is False
    assert profile.cv_parse_writes == (("POST", "https://jobs.lever.co/parseResume"),)
    assert not (profile.graphql_form_writes or profile.cv_storage_writes)
    assert profile.nonfatal_blocked_writes == (("POST", "www.google-analytics.com"),)  # still blocked, non-fatal
    assert profile.execute_options()["live_cv_parse_writes"] == profile.cv_parse_writes
    assert profile.execute_options()["live_page_security_writes"] == profile.page_security_writes
    validate_page_security_writes(profile_url := "https://jobs.lever.co/x/y/apply", profile.page_security_writes)
    assert {h for _, h, _ in profile.page_security_writes} == {"jobs.lever.co", "hcaptcha.com", "api.hcaptcha.com",
                                                              "api2.hcaptcha.com"}


@pytest.mark.parametrize("entry", [
    ("POST", HOST, "/anything"),                         # same host: only the challenge-platform prefix
    ("POST", HOST, "/parseResume"),
    ("PUT", "api.hcaptcha.com", "/checksiteconfig"),     # POST only
    ("POST", "*.hcaptcha.com", "/checksiteconfig"),      # no wildcard host
    ("POST", "api.hcaptcha.com", "/checksiteconfig*"),   # no wildcard path
    ("POST", "api.hcaptcha.com", "/submit"),             # a final-action word
    ("POST", "sub.jobs.example.test", "/x"),             # the application host's subdomain
])
def test_a_page_security_entry_is_exact_and_never_an_application_write(entry):
    with pytest.raises(ValueError):
        validate_page_security_writes(APPLY, [entry])


@pytest.mark.parametrize("parse", [
    ("POST", f"https://{HOST}/submitResume"),          # no "parse"
    ("POST", f"https://{HOST}/parseAndSubmitResume"),  # a final-action word
    ("PUT", f"https://{HOST}/parseResume"),            # not POST
    ("POST", "https://other.test/parseResume"),        # another host
    ("POST", APPLY),                                   # the form's own URL (the final submit)
    ("POST", f"https://{HOST}/parseResume?x=1"),       # query
])
def test_a_parse_write_is_exact_same_host_and_never_final(tmp_path, parse):
    bundle = bundle_for(tmp_path)
    with pytest.raises(ValueError):
        validate_cv_upload_policy(bundle, policy_for(bundle, parse_writes=frozenset({parse})))


def test_apply_one_refuses_an_unvalidated_profile_by_default(tmp_path):
    from jobintel.submission import apply_one
    from test_browserbase_service import build
    s = build(tmp_path / "store", url="https://jobs.lever.co/exampleco/0000aaaa-0000-0000-0000-000000000001/apply")
    with pytest.raises(ValueError, match="not yet validated live"):
        apply_one.start(s.svc, s.app.id, package_hash="0" * 64, reviewer="human", reviewed=True)


# --- the real runner in local headless Chrome --------------------------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser  # noqa: E402,F401  (fixture)


class LeverServer:
    def __init__(self):
        self.requests, self.external, self.parses, self.submits = [], [], [], []
        self.security = []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname == "api.hcaptcha.com" or parts.path.startswith("/cdn-cgi/challenge-platform/"):
            self.security.append((request.method, parts.hostname, parts.path))
            return route.fulfill(status=200, body="{}", headers={"access-control-allow-origin": "*"})
        if parts.hostname != HOST:
            self.external.append((request.method, parts.hostname))
            return route.abort()
        self.requests.append((request.method, parts.path))
        if request.method == "POST" and parts.path == "/parseResume":
            self.parses.append(request.post_data_buffer or b"")
            status = 500 if "mode=parse_fail" in (request.headers.get("referer") or "") else 200
            return route.fulfill(status=status, content_type="application/json",
                                 body=json.dumps({"resumeStorageId": "rs-123456789"}))
        if request.method == "GET" and parts.path == "/searchLocations":
            return route.fulfill(status=200, content_type="application/json", body="[]")
        if request.method == "POST":
            self.submits.append(parts.path)
            return route.fulfill(status=200, body="")
        if parts.path.endswith("/apply"):
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text())
        return route.fulfill(status=404, body="not found")


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = LeverServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


SECURITY = (("POST", HOST, "/cdn-cgi/challenge-platform/"), ("POST", "api.hcaptcha.com", "/checksiteconfig"))
ANALYTICS = (("POST", "www.google-analytics.com"),)


def run(tmp_path, server_page, query="?mode=ok", security=()):
    server, page = server_page
    bundle = bundle_for(tmp_path, query)
    extra = {"page_security_writes": security, "nonfatal_blocked_writes": ANALYTICS} if security else {}
    result = browserbase_runner.run(page, bundle, live_host=HOST, cv_upload=policy_for(bundle), **extra)
    assert result.final_submit_clicked is False and server.external == []
    assert page.evaluate("() => document.body.dataset.submitted || '0'") == "0"
    assert not any(p.endswith("/apply") for p in server.submits)  # the form itself was never POSTed
    return server, page, result


def value(page, selector):
    return page.eval_on_selector(selector, "el => el.type === 'checkbox' ? el.checked : el.value")


def test_lever_form_fills_deterministic_fields_uploads_the_sealed_cv_and_stops(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page)
    assert result.cv_upload_status == "uploaded"
    assert len(server.parses) == 1 and CV_BYTES in server.parses[0]  # exactly the sealed bytes, once
    assert value(page, "#rsid") == "rs-123456789"
    filled = {f["field"] for f in result.fields_filled}
    assert {"email", "phone", "location", "linkedin_url", "github_url"} <= filled and filled & {"name", "legal_full_name"}
    # human-only: sponsorship, AI-prohibited free text and consent are untouched
    assert value(page, "#spons") == "" and value(page, "#why") == "" and value(page, "#consent") is False
    assert "require_sponsorship_now_or_future" not in filled
    assert result.status in {"needs_human_input", "ready_for_human_review"}


@pytest.mark.parametrize("query,expect", [
    ("?mode=parse_fail", "cv_upload_not_confirmed"),
    ("?mode=no_name", "cv_upload_not_confirmed"),
])
def test_an_unconfirmed_lever_upload_is_escalated_not_claimed(tmp_path, server_page, query, expect):
    server, page, result = run(tmp_path, server_page, query)
    assert result.cv_upload_status != "uploaded"
    assert any(q["screenshot_or_evidence"] == f"runner:{expect}" for q in result.questions_escalated)


def test_any_other_same_host_write_is_fatal(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page, "?mode=extra_post")
    assert result.stop_reason == "unexpected_write_blocked" and "/telemetry" not in server.submits


def test_a_second_parse_post_is_refused(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page, "?mode=parse_twice")
    assert len(server.parses) == 1
    assert any(b.get("kind") == "cv_parse_refused" for b in result.blocked_requests)


# --- page-load security infrastructure (Cloudflare / hCaptcha bootstrap) -----------------------------------

def test_page_load_security_requests_pass_and_the_normal_flow_continues(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page, "?mode=security_ok", security=SECURITY)
    assert result.cv_upload_status == "uploaded" and len(server.parses) == 1
    assert {"email", "phone"} <= {f["field"] for f in result.fields_filled}
    kinds = [w.get("kind") for w in result.allowed_writes]
    assert kinds.count("page_security_infrastructure") == 2
    assert [b["host"] for b in result.nonfatal_blocked_requests] == ["www.google-analytics.com"]  # never sent
    assert sorted(h for _, h, _ in server.security) == ["api.hcaptcha.com", HOST]
    assert value(page, "#spons") == "" and value(page, "#why") == "" and value(page, "#consent") is False
    assert result.stop_reason != "unexpected_write_blocked"


def test_without_the_security_entries_the_same_page_stops_fail_closed(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page, "?mode=security_ok")
    assert result.stop_reason in {"unexpected_write_blocked", "captcha_requires_human"}
    assert server.security == [] and server.parses == [] and result.fields_filled == []


def test_a_captcha_challenge_request_stops_for_the_human_and_is_never_answered(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page, "?mode=captcha_challenge", security=SECURITY)
    assert result.stop_reason == "captcha_requires_human" and result.status == "needs_human_input"
    assert ("POST", "api.hcaptcha.com", "/getcaptcha/abc") not in server.security
    assert any(b["kind"] == "captcha_challenge" for b in result.blocked_requests)


def test_a_security_request_carrying_application_data_is_refused(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page, "?mode=security_leak", security=SECURITY)
    assert any(b["kind"] == "page_security_refused" for b in result.blocked_requests)
    assert server.security == [] and result.stop_reason == "unexpected_write_blocked"


def test_real_lever_labels_with_star_markers_and_button_text_still_fill_and_upload(tmp_path, server_page):
    """Regression: "Full name ✱" / "Email ✱" / "Resume/CV ✱ ATTACH RESUME/CV" escalated."""
    server, page, result = run(tmp_path, server_page, "?mode=security_ok&labels=real", security=SECURITY)
    filled = {f["field"] for f in result.fields_filled}
    assert {"email", "phone", "location"} <= filled and filled & {"name", "legal_full_name"}
    assert result.cv_upload_status == "uploaded" and len(server.parses) == 1
    assert not any(q["screenshot_or_evidence"] == "runner:cv_upload_label_not_exact" for q in result.questions_escalated)
    assert value(page, "#spons") == "" and value(page, "#why") == "" and value(page, "#consent") is False


@pytest.mark.parametrize("label", ["Resume/CV ATTACH COVER LETTER", "Resume autofill ATTACH", "Portfolio ATTACH FILE"])
def test_the_cv_button_suffix_rule_never_widens_the_cv_label(label):
    from jobintel.submission.browserbase_runner import CV_UPLOAD_LABELS, _cv_label, normalise_label
    assert _cv_label(normalise_label(label)) not in CV_UPLOAD_LABELS


# --- the location suggestion picker (the page cleared a directly assigned location on blur) ---

def _location(page):
    return page.eval_on_selector("#location-input", "el => el.value")


def _picker(tmp_path, server_page, loc):
    return run(tmp_path, server_page, f"?mode=security_ok&labels=real&loc={loc}", security=SECURITY)


def test_the_picker_chooses_the_single_uk_suggestion_for_the_city_and_it_is_kept(tmp_path, server_page):
    server, page, result = _picker(tmp_path, server_page, "uk")
    assert _location(page) == "Sampleton, Greater Sampleton, England, GBR"
    assert page.evaluate("() => document.body.dataset.locationPicked") == "Sampleton, Greater Sampleton, England, GBR"
    assert "location" in {f["field"] for f in result.fields_filled}
    assert not any(q["question"].startswith("Current location") for q in result.questions_escalated)
    assert ("GET", "/searchLocations") in server.requests  # the site's own search READ, triggered by typing
    assert page.evaluate("() => document.body.dataset.enterPressed || ''") == ""  # never Enter
    assert result.cv_upload_status == "uploaded"  # the CV parse does not undo the chosen location


@pytest.mark.parametrize("loc,reason", [
    ("ontario", "location_no_matching_suggestion"),     # never London, Ontario / Kentucky
    ("none", "location_no_matching_suggestion"),
    ("ambiguous", "location_ambiguous_suggestions"),
    ("clear", "location_selection_not_kept"),
])
def test_the_picker_escalates_instead_of_guessing(tmp_path, server_page, loc, reason):
    server, page, result = _picker(tmp_path, server_page, loc)
    assert any(q["screenshot_or_evidence"] == f"runner:{reason}" for q in result.questions_escalated)
    assert "location" not in {f["field"] for f in result.fields_filled}
    assert "Ontario" not in _location(page) and "Kentucky" not in _location(page)  # never a non-UK namesake
    assert result.final_submit_clicked is False


def test_the_picker_is_stable_on_a_rerun(tmp_path, server_page):
    first = _picker(tmp_path / "a", server_page, "uk")[2]
    page = server_page[1]
    page.goto("about:blank")
    second = _picker(tmp_path / "b", server_page, "uk")[2]
    assert _location(page) == "Sampleton, Greater Sampleton, England, GBR"
    assert ({f["field"] for f in first.fields_filled} == {f["field"] for f in second.fields_filled})


def test_a_plain_location_field_is_unchanged_by_the_picker(tmp_path, server_page):
    server, page, result = run(tmp_path, server_page, "?mode=ok")
    assert _location(page) == "Sampleton, United Kingdom" and "location" in {f["field"] for f in result.fields_filled}


@pytest.mark.parametrize("text,ok", [
    ("London, Greater London, England, GBR", True), ("London, ON, CAN", False), ("London, OH, USA", False),
    ("London, England, United Kingdom", True), ("London, United Kingdom", True), ("London, UK", True),
    ("London, Ontario, Canada", False), ("London, Kentucky, United States", False), ("London, OH, USA", False),
    ("Londonderry, Northern Ireland, United Kingdom", False), ("East London, Eastern Cape, South Africa", False),
    ("London", False), ("Greater London, England, United Kingdom", False),
])
def test_the_location_suggestion_matcher_accepts_only_london_in_the_uk(text, ok):
    from jobintel.submission.browserbase_runner import _location_suggestion_matches
    assert _location_suggestion_matches(text, "London", "United Kingdom") is ok
