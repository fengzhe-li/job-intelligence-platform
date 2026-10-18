"""Human verification (Cloudflare Turnstile / hCaptcha / reCAPTCHA) on a real application form (2026-10-15).

Synthetic forms exercise human-verification holds before supervised execution.
Nothing here solves, bypasses or automates verification. These tests pin:

* the pre-tab form check reports the verification providers the real form loads;
* the batch then HOLDS the item before any tab opens, until the user explicitly decides (manual or confirmed
  supervised automation);
* the in-page submit guard never instruments a verification provider's own frame;
* the Workable profile records the failed validation and stays unvalidated.
"""
from __future__ import annotations

import pytest

from jobintel.pipeline import batch
from jobintel.pipeline.restriction_precheck import FormSnapshot, verification_providers
from jobintel.submission import browserbase_runner
from jobintel.submission.ats_profiles import WORKABLE_HOSTED
from test_batch_pipeline import CONFIG, SYNTHETIC, prepare, store, svc_for  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")


def test_verification_providers_are_recognised_from_loaded_urls():
    urls = ["https://apply.example.test/j/1/apply", "https://challenges.cloudflare.com/turnstile/v0/api.js",
            "https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html", "https://www.google.com/recaptcha/api.js"]
    assert verification_providers(urls) == ("Cloudflare Turnstile", "Google reCAPTCHA", "hCaptcha")
    assert verification_providers(["https://apply.example.test/", "https://evilchallenges.cloudflare.com.test/"]) == ()


def test_a_form_snapshot_is_still_plain_text_for_restriction_wording():
    snap = FormSnapshot("Apply\nFirst name", ["Cloudflare Turnstile"])
    assert snap == "Apply\nFirst name" and snap.verification_providers == ("Cloudflare Turnstile",)


def _approve(store, app):  # noqa: F811
    return batch.approve(store, CONFIG, batch_id="b1", application_id=app, reviewer="human", reviewed=True,
                         acknowledge_review_reasons=True, profiles=SYNTHETIC, svc=svc_for(store))


def _fake_start(calls):
    def start(svc, application_id, **kw):
        calls.append(application_id)  # where a real run would open its tab
        return {"request_id": None, "status": "needs_human_input", "handoff": "local_browser_active",
                "final_submit_clicked": False}
    return start


def test_a_form_with_human_verification_is_held_before_any_tab(store):  # noqa: F811
    prepare(store)
    _approve(store, "app:j1")
    form = lambda url: FormSnapshot("Apply\nFirst name\nResume", ["Cloudflare Turnstile"])  # noqa: E731
    calls = []
    held = batch.run_next(store, CONFIG, batch_id="b1", max_items=3, profiles=SYNTHETIC, svc=svc_for(store),
                          start=_fake_start(calls), form_text=form)
    assert calls == []  # no tab was opened
    assert held[0]["status"] == "human_verification_required" and held[0]["providers"] == ["Cloudflare Turnstile"]
    assert "No tab was opened" in held[0]["warning"] and "never automated" in held[0]["warning"]
    # The user's explicit execution-time decision lets the supervised run start (verification stays theirs).
    ran = batch.run_next(store, CONFIG, batch_id="b1", max_items=3, profiles=SYNTHETIC, svc=svc_for(store),
                         start=_fake_start(calls), form_text=form, confirm_human_verification=("app:j1",))
    assert calls == ["app:j1"] and ran[-1]["status"] == "needs_human_input"


def test_a_form_without_verification_runs_normally(store):  # noqa: F811
    prepare(store)
    _approve(store, "app:j1")
    calls = []
    batch.run_next(store, CONFIG, batch_id="b1", max_items=1, profiles=SYNTHETIC, svc=svc_for(store),
                   start=_fake_start(calls), form_text=lambda url: FormSnapshot("Apply\nFirst name"))
    assert calls == ["app:j1"]


def test_the_submit_guard_never_instruments_a_verification_frame():
    sync_api = pytest.importorskip("playwright.sync_api")
    main_html = ("<html><body><form><input name=q></form>"
                 "<iframe id=v src='https://challenges.cloudflare.com/cdn-cgi/challenge-platform/frame'></iframe>"
                 "</body></html>")
    frame_html = "<html><body><form></form></body></html>"
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # pragma: no cover - environment without local Chrome
            pytest.skip(f"local Chrome unavailable: {exc}")
        try:
            context = browser.new_context()

            def serve(route, request):  # offline: both origins are fulfilled here, nothing leaves the machine
                body = frame_html if "challenges.cloudflare.com" in request.url else main_html
                return route.fulfill(status=200, content_type="text/html", body=body)

            context.route("**/*", serve)
            page = context.new_page()
            page.add_init_script(browserbase_runner.guard_script("a" * 40))
            page.goto("https://apply.example.test/j/1/apply")
            page.wait_for_function("document.getElementById('v') && window.frames.length === 1")
            frame = next(f for f in page.frames if "challenges.cloudflare.com" in f.url)
            frame.wait_for_load_state()
            native = "() => HTMLFormElement.prototype.submit.toString().includes('[native code]') && " \
                     "Element.prototype.attachShadow.toString().includes('[native code]')"
            assert page.main_frame.evaluate(native) is False          # the application page is guarded
            assert frame.evaluate(native) is True                     # the verification frame is untouched
            assert frame.evaluate("() => typeof window.__jobintelReleaseGuard") == "undefined"
        finally:
            browser.close()


def test_workable_records_the_failed_live_validation_and_stays_unvalidated():
    assert WORKABLE_HOSTED.live_validated is False
    assert ("POST", "region1.google-analytics.com") in WORKABLE_HOSTED.nonfatal_blocked_writes
    assert "FAILED validation" in WORKABLE_HOSTED.validated and "Turnstile" in WORKABLE_HOSTED.validated


def test_a_third_party_widget_submit_is_blocked_but_does_not_count_as_an_application_submit():
    """2026-10-15 (Lever): an "apply with ..." widget calls form.submit() on a hidden form posting to its own
    host at page load. It stays blocked (nothing is sent) and is recorded apart; a same-host submit still trips
    the guard exactly as before."""
    sync_api = pytest.importorskip("playwright.sync_api")
    html = ("<html><body><form id=app action='/apply'><input name=q></form>"
            "<form id=w action='https://widget.example.test/bootstrap#x'><input name=t></form>"
            "<script>document.getElementById('w').submit();</script></body></html>")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # pragma: no cover - environment without local Chrome
            pytest.skip(f"local Chrome unavailable: {exc}")
        try:
            context = browser.new_context()
            sent = []
            context.route("**/*", lambda route, request: (sent.append(request.url), route.fulfill(
                status=200, content_type="text/html", body=html if "apply.example.test" in request.url else ""))[1])
            page = context.new_page()
            page.add_init_script(browserbase_runner.guard_script("a" * 40))
            page.goto("https://apply.example.test/j/1/apply")
            assert page.evaluate("() => [...window.__jobintelBlockedSubmits]") == []
            assert page.evaluate("() => [...window.__jobintelForeignSubmits]") == ["form.submit() -> widget.example.test"]
            assert not any("widget.example.test" in url for url in sent)  # nothing reached the widget host
            page.evaluate("() => document.getElementById('app').submit()")
            assert page.evaluate("() => [...window.__jobintelBlockedSubmits]") == ["form.submit()"]
        finally:
            browser.close()
