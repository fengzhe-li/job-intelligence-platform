"""Bounded, policy-safe wait for a JS-rendered form surface (offline).

A real Ashby page was scanned right after "load", before the SPA drew its form, and stopped with
`no_safe_navigation` and zero controls. The runner now waits -- at most RENDER_WAIT_MS -- until the
same non-empty control/action counts are seen on two consecutive scans RENDER_POLL_MS apart, both
before the first page and after every safe advance. Every poll runs the full guard check. A page
that never renders stops with `form_not_rendered`.

Unit tests drive `_wait_for_render` with scripted scans; browser tests use a synthetic SPA fixture
in a local headless Chrome whose resolver answers NOTFOUND (nothing leaves the machine).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from jobintel.submission import browserbase_runner
from jobintel.submission.browserbase_runner import _Stop

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")


# --- the algorithm (scripted scans, no browser) ------------------------------------------------------

class FakePage:
    def __init__(self):
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


LABELS = ["First name", "Email", "Phone", "Location", "LinkedIn", "Cover letter", "Sponsorship"]


def control(label, kind="text", required=True, value=""):
    return {"kind": kind, "marker": f"m-{label}", "label": label, "name": label.lower(), "required": required,
            "has_value": bool(value), "invalid": False, "value_never_used": value}


def snapshot(controls, actions=(), url="https://jobs.example.test/apply"):
    """`controls`: an int (the first n LABELS) or a list of labels / control dicts."""
    if isinstance(controls, int):
        controls = LABELS[:controls]
    items = [c if isinstance(c, dict) else control(c) for c in controls]
    return [(SimpleNamespace(url=url), {"doc": "d1", "controls": items, "actions": list(actions)})]


SUBMIT = {"text": "Submit application", "type": "submit", "tag": "button", "role": "", "raw_href": "", "href": ""}
NEXT = {"text": "Next", "type": "button", "tag": "button", "role": "", "raw_href": "", "href": ""}
COOKIE = {"text": "Accept cookies", "type": "button", "tag": "button", "role": "", "raw_href": "", "href": ""}
BUNDLE = SimpleNamespace(application_url="https://jobs.example.test/apply")
HOSTNAME = "jobs.example.test"


def drive(monkeypatch, sequence, previous=None, guard=None):
    scans = iter(sequence)
    polls = []

    def fake_scan(page, bundle, result):
        polls.append(1)
        try:
            return next(scans)
        except StopIteration:
            return sequence[-1]
    monkeypatch.setattr(browserbase_runner, "_scan", fake_scan)
    monkeypatch.setattr(browserbase_runner, "_guard_tripped", guard or (lambda page, result: None))
    page = FakePage()
    try:
        out = browserbase_runner._wait_for_render(page, BUNDLE, browserbase_runner.RunResult(), previous=previous)
        return out, page.waits, len(polls), None
    except _Stop as stop:
        return None, page.waits, len(polls), stop.reason


def fingerprint(scans):
    return browserbase_runner._render_fingerprint(scans, HOSTNAME)


def test_constants_match_the_policy():
    assert (browserbase_runner.RENDER_POLL_MS, browserbase_runner.RENDER_WAIT_MS) == (500, 15000)


def test_fingerprint_is_structural_and_never_holds_values():
    filled = snapshot([control("First name", value="SECRET-VALUE-SENTINEL"), control("Email")], [SUBMIT, COOKIE])
    empty = snapshot([control("First name"), control("Email")], [SUBMIT, COOKIE])
    assert fingerprint(filled) == fingerprint(empty)  # typing a value never changes the structure
    assert "SECRET-VALUE-SENTINEL" not in repr(fingerprint(filled))
    assert "Accept cookies" not in repr(fingerprint(filled))  # unrelated actions are not part of the surface
    assert fingerprint(snapshot(["First name"])) != fingerprint(snapshot([control("First name", required=False)]))
    assert fingerprint(snapshot(["First name"])) != fingerprint(snapshot([control("First name", kind="email")]))
    assert fingerprint(snapshot([], [COOKIE])) == ()  # nothing to decide on: empty


def test_A_same_count_different_controls_is_not_stable(monkeypatch):
    sequence = [snapshot(["First name", "Email"]), snapshot(["Phone", "Location"]),
                snapshot(["LinkedIn", "Cover letter"]), snapshot(["LinkedIn", "Cover letter"])]
    out, waits, polls, stop = drive(monkeypatch, sequence)
    assert stop is None and polls == 4 and out is sequence[3]  # only the repeated STRUCTURE counts
    alternating = [snapshot(["First name", "Email"] if n % 2 else ["Phone", "Location"]) for n in range(40)]
    assert drive(monkeypatch, alternating)[3] == "form_render_unstable"  # same count forever, never stable


def test_B_same_controls_twice_is_stable(monkeypatch):
    sequence = [snapshot(2, [SUBMIT]), snapshot(2, [SUBMIT])]
    out, waits, polls, stop = drive(monkeypatch, sequence)
    assert stop is None and polls == 2 and waits == [500]


def test_C_progressive_render_waits_for_the_final_structure(monkeypatch):
    sequence = [snapshot(0), snapshot(3), snapshot(7), snapshot(7, [SUBMIT]), snapshot(7, [SUBMIT])]
    out, waits, polls, stop = drive(monkeypatch, sequence)
    assert stop is None and polls == 5 and waits == [500] * 4 and out is sequence[4]


def test_F_nothing_rendered_is_form_not_rendered_after_the_bounded_wait(monkeypatch):
    out, waits, polls, stop = drive(monkeypatch, [snapshot(0, [COOKIE])])  # an unrelated button is no surface
    assert stop == "form_not_rendered" and sum(waits) == 15000 and polls == 31


def test_E_after_advance_the_previous_structure_does_not_count(monkeypatch):
    step1 = snapshot(["First name"], [NEXT])
    still_old = drive(monkeypatch, [step1], previous=fingerprint(step1))
    assert still_old[3] == "advance_had_no_effect" and sum(still_old[1]) == 15000
    step2 = snapshot(["Email"], [SUBMIT])
    sequence = [step1, step1, snapshot(0), step2, step2]
    out, waits, polls, stop = drive(monkeypatch, sequence, previous=fingerprint(step1))
    assert stop is None and polls == 5 and out is sequence[4]


def test_G_guards_run_on_every_poll_and_stop_the_wait(monkeypatch):
    calls = []

    def guard(page, result):
        calls.append(1)
        if len(calls) == 3:
            raise _Stop("needs_human_input", "unexpected_write_blocked")
    out, waits, polls, stop = drive(monkeypatch, [snapshot(0)], guard=guard)
    assert stop == "unexpected_write_blocked" and len(calls) == 3 and polls == 2  # checked before every scan


# --- the real runner, live-host mode, local headless Chrome ------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_graphql_and_identity import HOST, no_secret  # noqa: E402
from test_browserbase_live_host import browser, live_bundle  # noqa: E402,F401  (fixture)

FIXTURE = Path(__file__).parent / "fixtures" / "graphql" / "delayed.html"
RUM = ("POST", "browser-intake-datadoghq.com")


class DelayedServer:
    def __init__(self):
        self.requests, self.external, self.bodies = [], [], []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname != HOST:
            self.external.append((request.method, parts.hostname))
            return route.abort()
        self.requests.append((request.method, parts.path))
        if request.method != "GET":
            self.bodies.append(request.post_data)
            return route.fulfill(status=200, content_type="application/json", body='{"data": {}}')
        if not parts.path.endswith("/delayed.html"):
            return route.fulfill(status=404, body="not found")
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text())


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = DelayedServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def live_run(tmp_path, server_page, mode, **kwargs):
    server, page = server_page
    bundle = server.bundle = live_bundle(tmp_path, "/graphql/delayed.html", f"?mode={mode}")
    result = browserbase_runner.run(page, bundle, live_host=HOST, **kwargs)
    assert result.final_submit_clicked is False and server.bodies == []
    assert not any(p.endswith("/submit") for _, p in server.requests)
    return server, page, result


def test_A_delayed_initial_render_is_waited_for_and_filled(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "delayed")
    assert result.status == "ready_for_human_review" and result.stop_reason == "final_page_reached_stop_before_submit"
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email"]
    assert result.controls_seen == 2 and result.final_submit_seen is True


def test_B_progressive_render_is_scanned_only_once_complete(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "progressive")
    assert result.status == "ready_for_human_review"
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email"]


def test_C_never_rendered_page_stops_with_form_not_rendered(tmp_path, server_page, monkeypatch):
    monkeypatch.setattr(browserbase_runner, "RENDER_WAIT_MS", 2000)  # same algorithm, shorter bound
    server, page, result = live_run(tmp_path, server_page, "never")
    assert result.status == "needs_human_input" and result.stop_reason == "form_not_rendered"
    assert result.controls_seen == 0 and result.advance_clicks == [] and result.fields_filled == []
    report = browserbase_runner.to_stop_only_report(result, server.bundle, task_id="t")
    assert [q["question"] for q in report["questions_escalated"]] == ["Runner stopped: form_not_rendered"]


def test_D_unauthorized_write_during_the_wait_is_fatal(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "write")
    assert result.stop_reason == "unexpected_write_blocked" and result.fields_filled == []
    assert (result.blocked_requests[0]["host"], result.blocked_requests[0]["path"]) == (HOST, "/api/collect")
    assert no_secret(str(result.blocked_requests))


def test_E_configured_rum_beacon_during_the_wait_stays_nonfatal(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "rum", nonfatal_blocked_writes=[RUM])
    assert result.status == "ready_for_human_review" and result.blocked_requests == []
    assert result.nonfatal_blocked_requests and all(r["host"] == RUM[1] for r in result.nonfatal_blocked_requests)
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email"]
    assert server.external == []  # aborted in the browser, never sent


def test_E_unconfigured_rum_beacon_during_the_wait_is_fatal(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "rum")
    assert result.stop_reason == "unexpected_write_blocked" and result.fields_filled == []


def test_F_host_drift_during_the_wait_is_fatal(tmp_path, server_page):
    server, page, result = live_run(tmp_path, server_page, "drift")
    assert result.status == "aborted_wrong_job" and result.stop_reason == "host_drift_blocked"
    assert result.fields_filled == []


@pytest.mark.parametrize("mode", ["steps", "steps_linger"])
def test_G_late_step_two_after_next_is_waited_for_and_filled(tmp_path, server_page, mode):
    server, page, result = live_run(tmp_path, server_page, mode)
    assert result.status == "ready_for_human_review" and result.stop_reason != "advance_had_no_effect"
    assert result.advance_clicks == ["Next"]
    assert [f["field"] for f in result.fields_filled] == ["legal_first_name", "email"]
