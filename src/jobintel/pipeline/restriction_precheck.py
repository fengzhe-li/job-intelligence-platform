"""Pre-execution application-restriction check: read the REAL application form's text before any handoff tab
is opened, so an employer's one-active / company-wide / ambiguous restriction wording stops the batch before
automation touches the page.

The form is loaded once in a separate, throwaway headless browser that only READS: every non-GET/HEAD request
(analytics, beacons, form writes) is aborted and nothing is typed, clicked or uploaded. Only the visible page
text is returned; it is classified like JD wording (analysis.application_restrictions) and recorded with its
source. A form that cannot be read is reported as unavailable, and the batch holds the item (fail closed).
"""
from __future__ import annotations

FORM_SETTLE_MS = 4000
# Human-verification providers (exact hosts or their subdomains).
VERIFICATION_PROVIDERS = {"challenges.cloudflare.com": "Cloudflare Turnstile", "hcaptcha.com": "hCaptcha",
                          "recaptcha.net": "Google reCAPTCHA"}


class PrecheckUnavailable(RuntimeError):
    """The application form could not be read; the item is held for the user."""


class FormSnapshot(str):
    """The form's visible text (a str, as before) plus the human-verification providers the form loads."""
    verification_providers: tuple = ()

    def __new__(cls, text: str, verification_providers=()):
        obj = super().__new__(cls, text)
        obj.verification_providers = tuple(verification_providers)
        return obj


def verification_providers(urls) -> tuple[str, ...]:
    """Providers named by any loaded script / frame / request URL (sorted, unique)."""
    from urllib.parse import urlsplit
    found = set()
    for url in urls:
        host = (urlsplit(url or "").hostname or "").casefold()
        for suffix, name in VERIFICATION_PROVIDERS.items():
            if host == suffix or host.endswith("." + suffix):
                found.add(name)
    if any("/recaptcha/" in (url or "") and (urlsplit(url).hostname or "").endswith("google.com") for url in urls):
        found.add("Google reCAPTCHA")
    return tuple(sorted(found))


def fetch_form_text(url: str, *, timeout_ms: int = 30000) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment without Playwright
        raise PrecheckUnavailable("Playwright is not installed") from exc
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                loaded: list[str] = []

                def read_only(route, request):
                    if request.method in {"GET", "HEAD"}:
                        loaded.append(request.url)
                        return route.fallback()
                    return route.abort()

                page.route("**/*", read_only)
                page.goto(url, wait_until="load", timeout=timeout_ms)
                page.wait_for_timeout(FORM_SETTLE_MS)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                frames = [frame.url for frame in page.frames]
                return FormSnapshot(text, verification_providers([*loaded, *frames]))
            finally:
                browser.close()
    except PrecheckUnavailable:
        raise
    except Exception as exc:
        raise PrecheckUnavailable(f"{type(exc).__name__}") from None
