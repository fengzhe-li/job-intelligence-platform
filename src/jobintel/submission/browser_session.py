"""One runner attempt inside an ALREADY RUNNING Chromium reached over CDP.

Shared by the Browserbase cloud transport and the local headed-browser transport: whoever owns
the browser (a Browserbase session, or a local Chromium process) creates it, journals it, and
later releases it. This module only connects, hands the page to the UNCHANGED
`browserbase_runner.run` (same exact-host, GraphQL, form-write, non-fatal telemetry, identity,
CV and stop-before-final-submit rules), records minimal evidence, removes the runner's route
handler and then either closes the browser or -- for a reviewable human handoff -- only
disconnects, leaving the browser exactly as the runner left it.

The CDP endpoint URL may embed a signing key (Browserbase): it is never logged, stored or
carried in an exception message.
"""
from __future__ import annotations

import hashlib
import secrets

from jobintel.submission import browserbase_runner

HANDOFF_RUNNER_STATUSES = frozenset({"ready_for_human_review", "needs_human_input"})


def run_connected(connect_url, bundle, evidence, *, playwright_factory, connect_error, serve=None, write_allowlist=(),
                  keep_alive=False, live_host=None, graphql_reads=(), graphql_form_writes=(),
                  nonfatal_blocked_writes=(), screenshot_dir=None, close=None, release_guard_on_handoff=False,
                  cv_upload=None, new_tab=False, on_page=None, page_security_writes=()):
    """Connect, run once, detach the guard, then close (or only disconnect for a handoff).

    `evidence` is the caller's evidence object (needs final_url, live, screenshot_sha256,
    screenshot_path, handoff_kept, guard_detached, session_id). `connect_error(exc)` returns the
    exception to raise if the CDP connection fails (it must not carry the endpoint URL).
    `close(browser)` overrides how a non-handoff browser is closed (default: browser.close()).

    `release_guard_on_handoff` (local browser): the in-page submit guard is installed with a
    per-run secret held only here; at a reviewable handoff -- after the runner has finished and
    its route handler is removed -- the guard is released in every frame so the HUMAN's own
    Submit works without a reload, then the automation disconnects. While the runner runs the
    guard is exactly as strict as without it. Off (Browserbase): the guard is never releasable.

    `new_tab` (shared local browser): the runner gets a NEW tab of its own -- never an existing
    page, so a tab handed to the human earlier is never touched again -- and `on_page(page)` binds
    it (journals its target) before anything runs. A non-handoff outcome then closes only that
    tab, never the browser. The runner's network guard and submit guard are page-scoped, so other
    tabs (including handoff tabs the human is working in) are never intercepted.
    Returns the RunResult, or raises whatever the runner raised."""
    live = live_host is not None
    token = secrets.token_hex(32) if release_guard_on_handoff else None
    with playwright_factory() as p:
        try:
            browser = p.chromium.connect_over_cdp(connect_url)
        except Exception as exc:  # its message can embed the endpoint URL: never carried
            raise connect_error(exc) from None
        page = None
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            if serve is not None:
                context.route("**/*", serve)
            if new_tab:
                page = context.new_page()
                if on_page is not None:
                    on_page(page)
            else:
                page = context.pages[0] if context.pages else context.new_page()
            if live:
                result = browserbase_runner.run(page, bundle, write_allowlist=write_allowlist, live_host=live_host,
                                                graphql_reads=graphql_reads, graphql_form_writes=graphql_form_writes,
                                                nonfatal_blocked_writes=nonfatal_blocked_writes,
                                                guard_release_token=token,
                                                **({"page_security_writes": page_security_writes}
                                                   if page_security_writes else {}),
                                                **({"cv_upload": cv_upload} if cv_upload is not None else {}))
                evidence.final_url = result.current_url  # already without query/fragment
                evidence.live.update(stop_reason=result.stop_reason, blocked_requests=list(result.blocked_requests),
                                     allowed_requests=list(result.allowed_writes),
                                     nonfatal_blocked_requests=list(result.nonfatal_blocked_requests),
                                     human_verification_seen=list(result.human_verification_seen),
                                     foreign_submits_blocked=list(result.foreign_submits_blocked),
                                     form=browserbase_runner.form_diagnostics(result))
            else:
                result = browserbase_runner.run(page, bundle, write_allowlist=write_allowlist,
                                                guard_release_token=token)
                evidence.final_url = page.url
                if screenshot_dir is not None:
                    image = page.screenshot(full_page=True)
                    evidence.screenshot_sha256 = hashlib.sha256(image).hexdigest()
                    path = screenshot_dir / f"{evidence.session_id}.png"
                    path.write_bytes(image)
                    evidence.screenshot_path = str(path)
            evidence.handoff_kept = bool(keep_alive and result.status in HANDOFF_RUNNER_STATUSES)
        finally:
            # The runner's result already exists here. Remove its route handler before the
            # connection goes away (close, or disconnect when leaving `with`), so no handler is
            # cancelled mid-call during teardown and the human's browsing is never intercepted.
            if page is not None:
                if serve is not None:  # the controlled-host route this function installed itself
                    try:
                        page.context.unroute_all(behavior="wait")
                    except Exception:
                        pass
                evidence.guard_detached = browserbase_runner.detach_guard(page)
                if token is not None and evidence.handoff_kept:
                    # Human handoff: the runner is done and no longer intercepts anything; give the
                    # page back its own submit behaviour, then only disconnect (leaving `with`).
                    evidence.page_guard_released = browserbase_runner.release_submit_guard(page, token)
            if not evidence.handoff_kept:
                if new_tab:
                    if page is not None:  # only this run's own tab; the shared browser stays
                        try:
                            page.close()
                        except Exception:
                            pass
                else:
                    (close or (lambda b: b.close()))(browser)  # handoff: only disconnect when leaving `with`
    return result
