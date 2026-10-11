"""Browserbase cloud transport for the offline stop-only runner. SPIKE ONLY.

Creates one Browserbase session over the documented REST API, connects Playwright to
its remote Chromium over CDP, hands the session's page to the UNCHANGED
`browserbase_runner.run`, and always releases the session. It adds no form logic,
no answers and no network relaxation: the runner's own submit guard and non-GET
network guard apply exactly as offline.

Credentials come only from the environment (BROWSERBASE_API_KEY, optional
BROWSERBASE_PROJECT_ID). They are never returned in results, evidence, reprs,
exceptions or logs; the session connectUrl (which embeds a signing key) is treated as
a secret too. Only the session id is kept as evidence.

Not an executor: "browserbase" is not in STOP_ONLY_EXECUTORS and nothing in the
submission service or CLI calls this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from jobintel.submission import browser_session, browserbase_runner

API_BASE = "https://api.browserbase.com/v1"
API_KEY_ENV = "BROWSERBASE_API_KEY"
PROJECT_ID_ENV = "BROWSERBASE_PROJECT_ID"
# Conservative session for a synthetic spike: no CAPTCHA solving, no proxies, no stealth,
# no keep-alive, short timeout. Recording stays on (synthetic data only) for audit.
SESSION_TIMEOUT_SECONDS = 300
# Human handoff (opt-in): the session is created with keepAlive so it survives our disconnect,
# and its timeout covers the run plus the review window. Browserbase's hard maximum is 6 hours;
# extending a running session is not documented, so the window is fixed at creation.
MAX_SESSION_SECONDS = 21600
MAX_REVIEW_SECONDS = MAX_SESSION_SECONDS - SESSION_TIMEOUT_SECONDS
HANDOFF_RUNNER_STATUSES = browser_session.HANDOFF_RUNNER_STATUSES
ENDED_SESSION_STATUSES = frozenset({"COMPLETED", "TIMED_OUT", "ERROR"})
BROWSER_SETTINGS = {"solveCaptchas": False, "blockAds": True, "recordSession": True, "logSession": True}
# Single-request live-host mode (a real host carries real candidate data): session recording
# (video + rrweb DOM replay) and session logging are OFF, using the documented create-session
# browserSettings. Not configurable. Browserbase's allowedDomains also admits subdomains, so it is
# only an outer fence; the runner enforces the exact host. No screenshot is captured.
LIVE_BROWSER_SETTINGS = {"solveCaptchas": False, "blockAds": True, "recordSession": False, "logSession": False}
LIVE_PURPOSE = "jobintel-supervised-stop-only-live-host"


class MissingBrowserbaseCredentials(RuntimeError):
    pass


# Safe failure reporting: a closed set of stages/categories, an integer HTTP status and a bare
# exception class name. Never raw exception text, response/request bodies, headers or URLs.
FAILURE_STAGES = frozenset({"bundle_prepare", "browserbase_session_create", "browserbase_cdp_connect",
                            "browserbase_run", "local_browser_launch", "local_browser_cdp_connect",
                            "local_browser_run"})
FAILURE_CATEGORIES = frozenset({"browserbase_http_error", "browserbase_unreachable",
                                "browserbase_malformed_session_response", "browserbase_cdp_connect_failed",
                                "browserbase_session_create_failed", "browserbase_runner_incomplete",
                                "local_browser_launch_failed", "local_browser_cdp_connect_failed",
                                "executor_internal_error"})


def _safe_type_name(value) -> bool:
    return isinstance(value, str) and value.isascii() and value.isidentifier() and len(value) <= 64


class BrowserbaseSpikeError(RuntimeError):
    """Messages are fixed text. `category`, `http_status`, `stage` and `exception_type` are the
    structured, safe reason (see `safe_failure`); provider text is never attached."""

    def __init__(self, message, *, category=None, http_status=None, stage=None, exception_type=None):
        super().__init__(message)
        self.category, self.http_status, self.stage, self.exception_type = category, http_status, stage, exception_type


def safe_failure(exc: BaseException, default_stage: str) -> dict:
    """Sanitized, persistable reason for an interrupted Browserbase attempt. Only closed-set
    stage/category values, an integer HTTP status and a bare exception class name survive."""
    stage = getattr(exc, "stage", None) if isinstance(exc, BrowserbaseSpikeError) else None
    category = getattr(exc, "category", None) if isinstance(exc, BrowserbaseSpikeError) else None
    status = getattr(exc, "http_status", None) if isinstance(exc, BrowserbaseSpikeError) else None
    exc_type = getattr(exc, "exception_type", None) if isinstance(exc, BrowserbaseSpikeError) else None
    if not _safe_type_name(exc_type):
        exc_type = type(exc).__name__
    failure = {"stage": stage if stage in FAILURE_STAGES else
               (default_stage if default_stage in FAILURE_STAGES else "browserbase_run"),
               "category": category if category in FAILURE_CATEGORIES else "executor_internal_error"}
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        failure["http_status"] = status
    if _safe_type_name(exc_type):
        failure["exception_type"] = exc_type
    return failure


def _http_error(exc):
    return BrowserbaseSpikeError(f"Browserbase API returned HTTP {exc.code}", category="browserbase_http_error",
                                 http_status=exc.code if isinstance(exc.code, int) else None,
                                 exception_type=type(exc).__name__)


def _unreachable(exc):
    reason = getattr(exc, "reason", exc)
    return BrowserbaseSpikeError(f"Browserbase API unreachable: {type(reason).__name__}",
                                 category="browserbase_unreachable", exception_type=type(reason).__name__)


@dataclass(frozen=True)
class BrowserbaseCredentials:
    api_key: str = field(repr=False)
    project_id: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "BrowserbaseCredentials(api_key=<redacted>, project_id=<redacted>)"

    __str__ = __repr__


def load_credentials(environ=None) -> BrowserbaseCredentials:
    env = os.environ if environ is None else environ
    api_key = (env.get(API_KEY_ENV) or "").strip()
    if not api_key:
        raise MissingBrowserbaseCredentials(
            f"{API_KEY_ENV} is not set; export it (and optionally {PROJECT_ID_ENV}) in your local shell "
            "before running the Browserbase spike. Nothing was sent.")
    return BrowserbaseCredentials(api_key, (env.get(PROJECT_ID_ENV) or "").strip() or None)


@dataclass(frozen=True)
class CloudSession:
    id: str
    status: str
    connect_url: str = field(repr=False)  # embeds a signing key: never logged or stored

    def __repr__(self) -> str:
        return f"CloudSession(id={self.id!r}, status={self.status!r}, connect_url=<redacted>)"


@dataclass
class CloudEvidence:
    """What the spike keeps. No credentials, no connect URL."""
    session_id: str = ""
    session_status_at_create: str = ""
    allowed_domains: list = field(default_factory=list)
    replay_url: str = ""
    final_url: str = ""
    screenshot_sha256: str = ""
    screenshot_path: str = ""
    released: bool = False
    release_error: str = ""
    handoff_kept: bool = False  # session deliberately left alive for human review (keepAlive)
    guard_detached: bool = False  # runner route handler removed before disconnect/close
    live: dict = field(default_factory=dict)  # live-host mode only: privacy mode + minimal stop diagnostics


def _urllib_post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:  # status only: response bodies are not echoed
        raise _http_error(exc) from None
    except OSError as exc:  # URLError, timeouts, refused/reset connections
        raise _unreachable(exc) from None


def _urllib_get(url: str, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from None
    except OSError as exc:
        raise _unreachable(exc) from None


def _headers(creds: BrowserbaseCredentials) -> dict:
    return {"X-BB-API-Key": creds.api_key, "Content-Type": "application/json"}


def session_request_body(creds: BrowserbaseCredentials, allowed_domains: list[str], *, keep_alive=False,
                         timeout=SESSION_TIMEOUT_SECONDS, live=False) -> dict:
    if not SESSION_TIMEOUT_SECONDS <= timeout <= MAX_SESSION_SECONDS or (timeout != SESSION_TIMEOUT_SECONDS and not keep_alive):
        raise ValueError("Session timeout outside the supported window")
    if live and (not keep_alive or len(allowed_domains) != 1 or "*" in allowed_domains[0]):
        raise ValueError("A live-host session needs human handoff and exactly one exact host")
    settings = LIVE_BROWSER_SETTINGS if live else BROWSER_SETTINGS
    body = {"timeout": timeout, "keepAlive": bool(keep_alive),
            "browserSettings": {**settings, "allowedDomains": list(allowed_domains)},
            "userMetadata": {"purpose": LIVE_PURPOSE if live else "jobintel-synthetic-stop-only-spike"}}
    if live and (body["browserSettings"]["recordSession"] is not False or body["browserSettings"]["logSession"] is not False):
        raise ValueError("Refusing a live-host session that could record or log")  # fail closed
    if creds.project_id:
        body["projectId"] = creds.project_id
    return body


def create_session(creds, *, allowed_domains, post=_urllib_post, keep_alive=False,
                   timeout=SESSION_TIMEOUT_SECONDS, live=False) -> CloudSession:
    body = session_request_body(creds, allowed_domains, keep_alive=keep_alive, timeout=timeout, live=live)
    data = post(f"{API_BASE}/sessions", _headers(creds), body, 30)
    if not data.get("id") or not str(data.get("connectUrl", "")).startswith("ws"):
        raise BrowserbaseSpikeError("Browserbase session response missing id/connectUrl",
                                    category="browserbase_malformed_session_response")
    return CloudSession(str(data["id"]), str(data.get("status", "")), str(data["connectUrl"]))


def release_session(creds, session_id: str, *, post=_urllib_post) -> None:
    body = {"status": "REQUEST_RELEASE"}
    if creds.project_id:
        body["projectId"] = creds.project_id
    post(f"{API_BASE}/sessions/{session_id}", _headers(creds), body, 30)


def get_session_status(creds, session_id: str, *, get=_urllib_get) -> str:
    return str(get(f"{API_BASE}/sessions/{session_id}", _headers(creds), 30).get("status", ""))


def get_live_view(creds, session_id: str, *, expires_in: int, get=_urllib_get) -> dict:
    """Official Live View links (GET /v1/sessions/{id}/debug), time-limited with expiresIn.
    The URL may act as a bearer link to watch/control the browser: runtime-only, never stored."""
    expires_in = max(60, min(int(expires_in), MAX_SESSION_SECONDS))
    data = get(f"{API_BASE}/sessions/{session_id}/debug?expiresIn={expires_in}", _headers(creds), 30)
    url = str(data.get("debuggerFullscreenUrl") or "")
    if not url.startswith("https://"):
        raise BrowserbaseSpikeError("Browserbase returned no Live View URL")
    return {"live_view_url": url, "expires_in": expires_in,
            "page_urls": [str(p.get("url", "")) for p in data.get("pages", []) if isinstance(p, dict)]}


def run_in_cloud(bundle, *, creds, serve, allowed_domains, screenshot_dir, playwright_factory=None,
                 post=_urllib_post, on_session=None, write_allowlist=(), keep_alive=False, review_seconds=0,
                 live_host=None, graphql_reads=(), graphql_form_writes=(), nonfatal_blocked_writes=()):
    """Exactly one session: create -> connect -> runner.run -> screenshot -> release. Never retried.

    `serve(route, request)` is installed on the remote context so the controlled synthetic
    form is served from this process; it must abort every other host. `serve=None` installs no
    route (the remote browser loads the approved URL itself). `on_session(session_id)` is called
    as soon as the session exists so the caller can journal it before anything runs.

    `keep_alive=True` (human handoff, opt-in): the session is created with keepAlive and a
    timeout of run budget + `review_seconds`, and is left running ONLY if the runner stopped in
    a reviewable state (ready_for_human_review / needs_human_input). Every other outcome,
    including any exception, still releases the session.

    `live_host` (single-request live-host mode): requires keep_alive, allowed_domains must be
    exactly [live_host], recording and logging are off, no screenshot is taken, and the runner
    runs in live-host mode (exact host, stop on any blocked write, minimal diagnostics)."""
    if playwright_factory is None:
        from playwright.sync_api import sync_playwright as playwright_factory  # transport-only dependency
    if keep_alive and not 60 <= review_seconds <= MAX_REVIEW_SECONDS:
        raise ValueError(f"Review window must be 60..{MAX_REVIEW_SECONDS} seconds")
    live = live_host is not None
    if live and (not keep_alive or list(allowed_domains) != [live_host]):
        raise ValueError("A live-host run needs human handoff and exactly its one authorized host")
    evidence = CloudEvidence(allowed_domains=list(allowed_domains))
    try:
        session = create_session(creds, allowed_domains=allowed_domains, post=post, keep_alive=keep_alive,
                                 timeout=SESSION_TIMEOUT_SECONDS + review_seconds if keep_alive else SESSION_TIMEOUT_SECONDS,
                                 live=live)
    except BrowserbaseSpikeError as exc:
        exc.stage = "browserbase_session_create"
        exc.category = exc.category or "browserbase_session_create_failed"
        raise
    except Exception as exc:  # never carry its text: it may echo a request or response
        raise BrowserbaseSpikeError("Browserbase session creation failed", category="browserbase_session_create_failed",
                                    stage="browserbase_session_create", exception_type=type(exc).__name__) from None
    evidence.session_id, evidence.session_status_at_create = session.id, session.status
    # With recording off there is no replay to point at.
    evidence.replay_url = "" if live else f"https://www.browserbase.com/sessions/{session.id}"
    if live:
        evidence.live = {"host": live_host, "recording": LIVE_BROWSER_SETTINGS["recordSession"],
                         "browser_logging": LIVE_BROWSER_SETTINGS["logSession"], "screenshot": "not_captured"}
    result = None
    try:
        if on_session is not None:
            on_session(session.id)
        result = browser_session.run_connected(
            session.connect_url, bundle, evidence, playwright_factory=playwright_factory,
            connect_error=lambda exc: BrowserbaseSpikeError(
                "Browserbase CDP connection failed", category="browserbase_cdp_connect_failed",
                stage="browserbase_cdp_connect", exception_type=type(exc).__name__),
            serve=serve, write_allowlist=write_allowlist, keep_alive=keep_alive, live_host=live_host,
            graphql_reads=graphql_reads, graphql_form_writes=graphql_form_writes,
            nonfatal_blocked_writes=nonfatal_blocked_writes, screenshot_dir=None if live else screenshot_dir)
    finally:
        if not evidence.handoff_kept:
            try:
                release_session(creds, session.id, post=post)
                evidence.released = True
            except BrowserbaseSpikeError as exc:
                evidence.release_error = str(exc)
    if result is None:
        raise BrowserbaseSpikeError("Runner did not complete; investigate the session, do not retry",
                                    category="browserbase_runner_incomplete", stage="browserbase_run")
    return result, evidence


class CloudTransport:
    """Browserbase cloud transport for the submission service. Credentials are read from the
    runtime environment at preflight and again at run; they are never stored on this object."""

    def __init__(self, *, serve=None, screenshot_dir=None, environ=None, post=_urllib_post, get=_urllib_get,
                 playwright_factory=None):
        self.serve, self.screenshot_dir, self.environ = serve, screenshot_dir, environ
        self.post, self.get, self.playwright_factory = post, get, playwright_factory

    def __repr__(self) -> str:
        return "CloudTransport()"

    def preflight(self) -> None:
        try:
            load_credentials(self.environ)
        except MissingBrowserbaseCredentials as exc:
            raise ValueError(str(exc)) from None

    def run(self, bundle, *, write_allowlist, on_session, keep_alive=False, review_seconds=0, live_host=None,
            graphql_reads=(), graphql_form_writes=(), nonfatal_blocked_writes=()):
        shots = Path(self.screenshot_dir) if self.screenshot_dir else bundle.cv_path.parent.parent / "evidence"
        shots.mkdir(parents=True, exist_ok=True)
        result, evidence = run_in_cloud(bundle, creds=load_credentials(self.environ), serve=self.serve,
                                        allowed_domains=[urlsplit(bundle.application_url).hostname],
                                        screenshot_dir=shots, playwright_factory=self.playwright_factory,
                                        post=self.post, on_session=on_session, write_allowlist=write_allowlist,
                                        keep_alive=keep_alive, review_seconds=review_seconds, live_host=live_host,
                                        graphql_reads=graphql_reads, graphql_form_writes=graphql_form_writes,
                                        nonfatal_blocked_writes=nonfatal_blocked_writes)
        return result, asdict(evidence)

    def session_status(self, session_id: str) -> str:
        return get_session_status(load_credentials(self.environ), session_id, get=self.get)

    def live_view(self, session_id: str, *, expires_in: int) -> dict:
        return get_live_view(load_credentials(self.environ), session_id, expires_in=expires_in, get=self.get)

    def release(self, session_id: str) -> None:
        release_session(load_credentials(self.environ), session_id, post=self.post)
