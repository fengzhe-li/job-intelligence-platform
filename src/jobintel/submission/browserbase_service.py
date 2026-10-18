"""Browserbase as a stop-only executor inside the existing submission lifecycle.

The SubmissionService constructs `BrowserbaseServiceExecutor` itself, only from a live
approval in the ledger (callers cannot inject one). The executor:

1. writes the deterministic, sealed Browserbase bundle from the approved package
   (`browserbase.write_bundle`), then re-verifies it (`browserbase_runner.load_bundle`);
2. hands the verified bundle to a transport (default: Browserbase cloud) that drives the
   existing stop-only runner and reports the session id as soon as a session exists;
3. returns the runner evidence shaped as the shared stop-only report. The service
   imports it with `stop_only.import_result(executor="browserbase")`; nothing here is
   trusted as a result, and nothing here can produce a receipt.

It holds no store, no candidate facts, no credentials and no final-submit option.
Credentials live only in the transport's runtime environment.

Hosts: by default only reserved synthetic hosts may be run. A real host needs a
`LiveHostAuthorization`: built by the service for ONE request from the ledger plus the
operator's explicit opt-in, bound to request/application/approval/package hash and the
exact canonical host of the approved URL, expiring with the review window, never stored
as configuration and never reusable. It requires human handoff and carries no
final-submit permission (there is none to carry).
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from jobintel.models.submission import ApprovedSubmissionPackage, instant, utcnow
from jobintel.submission import browserbase, browserbase_runner, graphql_policy

NAME = "browserbase"
# RFC 2606 / RFC 6761 reserved names: guaranteed never to be a real employer or ATS.
SYNTHETIC_SUFFIXES = (".test", ".example", ".invalid", ".localhost")
SYNTHETIC_DOMAINS = ("example.com", "example.org", "example.net")


class BrowserbaseTransport(Protocol):
    def preflight(self) -> None:
        """Fail (ValueError) before any session if the runtime cannot run, e.g. no credentials."""

    def run(self, bundle, *, write_allowlist, on_session: Callable[[str], None], keep_alive=False, review_seconds=0,
            live_host=None, graphql_reads=(), graphql_form_writes=(), nonfatal_blocked_writes=()):
        """Drive `browserbase_runner.run` once; return (RunResult, evidence dict). Never retried.
        keep_alive (human handoff, opt-in) leaves a reviewable session running; see browserbase_cloud.
        live_host, graphql_reads, graphql_form_writes and nonfatal_blocked_writes are passed only for a single-request live-host
        run (recording/logging off)."""

    def session_status(self, session_id: str) -> str:
        """Provider status of a session (e.g. RUNNING, COMPLETED, TIMED_OUT)."""

    def live_view(self, session_id: str, *, expires_in: int) -> dict:
        """Runtime-only, time-limited Live View link for a session. Never persisted."""

    def release(self, session_id: str) -> None:
        """Request release of a session."""


@dataclass(frozen=True)
class BrowserbaseExecutionConfig:
    """Runtime-only execution policy for synthetic runs. Never serialized; holds no credentials or
    candidate data. It has no live-host switch: a real host is only ever authorized per request."""
    write_allowlist: tuple = ()


@dataclass(frozen=True)
class LiveHostAuthorization:
    """Single-request permission to run ONE exact real host under human handoff. Runtime-only:
    built by SubmissionService.execute, journaled as an audit event, never read back as permission."""
    request_id: str
    application_id: str
    approval_id: str
    package_hash: str
    host: str
    expires_at: str
    write_allowlist: tuple = ()  # exact (method, URL) pairs for this run only
    graphql_reads: tuple = ()  # exact (endpoint URL, operationName) GraphQL READS for this run only
    # exact (endpoint URL, operationName) form-value MUTATIONS, honoured only inside an approved field fill
    graphql_form_writes: tuple = ()
    # exact (method, third-party host) whose writes stay blocked and recorded but do not stop the run
    nonfatal_blocked_writes: tuple = ()
    # local_browser only: upload exactly the package's sealed CV once (browserbase_runner.CvUploadPolicy)
    cv_upload: object = None
    # local_browser only: exact (method, host, path) page-load security/CAPTCHA-bootstrap requests that pass
    page_security_writes: tuple = ()


MAX_LIVE_WRITE_ALLOWLIST = 5


def is_synthetic_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    return host.endswith(SYNTHETIC_SUFFIXES) or any(host == d or host.endswith("." + d) for d in SYNTHETIC_DOMAINS)


def canonical_host(application_url: str) -> str:
    """The exact host of an approved application URL: https, DNS name (ASCII/punycode), no port,
    no credentials, not an IP literal."""
    parts = urlsplit(application_url)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme != "https" or not host or port is not None or parts.username or parts.password:
        raise ValueError("Live-host mode needs an https application URL without port or credentials")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Live-host mode does not accept IP-address hosts")
    if not host.isascii() or host.endswith(".") or "*" in host:
        raise ValueError("Live-host mode needs a plain ASCII DNS host")
    return host


def validate_live_host(requested: str, application_url: str) -> str:
    """The operator-typed host must be byte-for-byte the canonical approved host. No wildcards,
    no parent/sibling/sub-domains, no case or trailing-dot variants."""
    if not isinstance(requested, str) or not requested or "*" in requested:
        raise ValueError("An exact live host is required; wildcards are not accepted")
    if requested != canonical_host(application_url):
        raise ValueError("Live host must exactly equal the approved application URL's host")
    return requested


def validate_live_write_allowlist(application_url: str, entries) -> tuple:
    entries = tuple((str(m), str(u)) for m, u in entries)
    if len(entries) > MAX_LIVE_WRITE_ALLOWLIST or len(set(entries)) != len(entries):
        raise ValueError(f"A live write allowlist holds at most {MAX_LIVE_WRITE_ALLOWLIST} distinct exact entries")
    browserbase_runner.validate_write_entries(application_url, entries)
    return entries


def check_target(application_url: str, config: BrowserbaseExecutionConfig, live: LiveHostAuthorization | None = None) -> None:
    if live is not None:
        if live.host != canonical_host(application_url):
            raise ValueError("Live-host authorization is for a different host")
        return
    if not is_synthetic_host(application_url):
        raise ValueError("Browserbase live execution is not enabled: only reserved synthetic hosts may be run "
                         "without a single-request live-host authorization")


@dataclass(frozen=True)
class BrowserbaseHandoff:
    request_id: str
    package_hash: str
    bundle_path: str
    manifest: dict
    executor: str = NAME
    execution_mode: str = browserbase.MODE


class BrowserbaseServiceExecutor:
    name = NAME

    def __init__(self, bundle_root, *, authorization, transport, config, clock=utcnow, live=None, name=NAME):
        # `name` is the request's executor: "browserbase" (cloud transport) or "local_browser"
        # (local headed Chromium transport). Same sealed bundle, same runner, same policies.
        self.name = name
        self.bundle_root = Path(bundle_root)
        self.authorization = authorization
        self.transport = transport
        self.config = config
        self.clock = clock
        self.live = live

    def _check_live(self, package: ApprovedSubmissionPackage) -> None:
        live, a = self.live, self.authorization
        if (not isinstance(live, LiveHostAuthorization)
                or (live.request_id, live.approval_id, live.package_hash, live.application_id)
                != (a.request_id, a.approval_id, package.package_hash, package.application_id)
                or instant(live.expires_at) <= self.clock()):
            raise ValueError("Live-host authorization does not match this request or has expired")
        if self.config.write_allowlist:
            raise ValueError("A live-host run takes only its own per-request write allowlist")
        validate_live_write_allowlist(package.application_url, live.write_allowlist)
        graphql_policy.validate_reads(package.application_url, live.graphql_reads)
        graphql_policy.validate_form_writes(package.application_url, live.graphql_form_writes)
        browserbase_runner.validate_nonfatal_blocked_writes(package.application_url, live.nonfatal_blocked_writes)
        browserbase_runner.validate_page_security_writes(package.application_url, live.page_security_writes)
        if live.page_security_writes and self.name != "local_browser":
            raise ValueError("Page-security entries are only available for the local_browser executor")
        if live.cv_upload is not None:
            if self.name != "local_browser":
                raise ValueError("An approved CV upload is only available for the local_browser executor")
            if not isinstance(live.cv_upload, browserbase_runner.CvUploadPolicy) or (
                    live.cv_upload.artifact_id, live.cv_upload.sha256) != (package.cv_artifact_id, package.cv_pdf_sha256):
                raise ValueError("CV upload authorization does not name this package's approved CV")

    def preflight(self, package: ApprovedSubmissionPackage) -> None:
        a = self.authorization
        if (not isinstance(a, browserbase.BrowserbaseBundleAuthorization) or a.status != "approved"
                or a.human_reviewed is not True or instant(a.expires_at) <= self.clock()):
            raise ValueError("A live, service-validated human approval is required")
        if self.live is not None:
            self._check_live(package)
        check_target(package.application_url, self.config, self.live)
        self.transport.preflight()

    def prepare(self, package: ApprovedSubmissionPackage, *, idempotency_key: str) -> BrowserbaseHandoff:
        if self.authorization.request_id != idempotency_key:
            raise ValueError("Authorization is bound to a different request")
        directory = browserbase.write_bundle(self.bundle_root, package, self.authorization, clock=self.clock)
        manifest = json.loads((directory / browserbase.MANIFEST_FILE).read_text(encoding="utf-8"))
        return BrowserbaseHandoff(idempotency_key, package.package_hash, str(directory), manifest)

    def run(self, handoff: BrowserbaseHandoff, *, on_session: Callable[[str], None], keep_alive=False,
            review_seconds=0) -> tuple[dict, dict]:
        """Verify the sealed bundle again, run the transport once, return (report, evidence)."""
        bundle = browserbase_runner.load_bundle(handoff.bundle_path)
        if bundle.request["package_hash"] != handoff.package_hash or bundle.request["request_id"] != handoff.request_id:
            raise ValueError("Bundle no longer matches the approved request")
        write_allowlist, extra = self.config.write_allowlist, {}
        if self.live is not None:
            if not keep_alive:
                raise ValueError("A live-host run requires human handoff")
            if instant(self.live.expires_at) <= self.clock() or canonical_host(bundle.application_url) != self.live.host:
                raise ValueError("Live-host authorization no longer matches this bundle")
            write_allowlist = self.live.write_allowlist
            extra = {"live_host": self.live.host, "graphql_reads": self.live.graphql_reads,
                     "graphql_form_writes": self.live.graphql_form_writes,
                     "nonfatal_blocked_writes": self.live.nonfatal_blocked_writes}
            if self.live.page_security_writes:
                extra["page_security_writes"] = self.live.page_security_writes
            if self.live.cv_upload is not None:
                # Bound to the sealed bundle too: the runner re-hashes the sealed CV before any upload.
                browserbase_runner.validate_cv_upload_policy(bundle, self.live.cv_upload)
                extra["cv_upload"] = self.live.cv_upload
        result, evidence = self.transport.run(bundle, write_allowlist=write_allowlist,
                                              on_session=on_session, keep_alive=keep_alive,
                                              review_seconds=review_seconds, **extra)
        task_id = evidence.get("session_id") or "no-session"
        report = browserbase_runner.to_stop_only_report(result, bundle, task_id=task_id, executor=self.name)
        # Observed by this process from the runner itself, not taken from the report.
        return report, {**evidence, "identity_state": browserbase_runner.identity_state(result)}

