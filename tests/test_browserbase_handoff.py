"""Supervised human handoff for the Browserbase executor (opt-in keep-alive). Synthetic, offline.

A reviewable stop (ready_for_human_review / needs_human_input) can leave the SAME filled
session alive for Live View review. The Live View link is runtime-only; the handoff is
journaled and bound to request/application/approval/package/session; an explicit,
idempotent release ends it. Nothing here submits, records a receipt or marks APPLIED.
"""
from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import patch

import pytest

from jobintel.submission.service import SubmissionService
from test_browserbase_service import (LIVE_URL, FakeTransport, build, kinds, no_receipt_and_not_applied, queued,
                                      ready_result, status)

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")


def events(s, r, kind):
    return [e["data"] for e in s.svc.store.events() if e["entity_id"] == r["id"] and e["kind"] == kind]


def store_text(s):
    return b"".join(p.read_bytes() for p in s.root.rglob("*") if p.is_file())


def handoff_run(tmp_path, transport=None, **execute_kwargs):
    s = build(tmp_path, transport or FakeTransport(ready_result))
    r, c = queued(s)
    out = s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, **execute_kwargs)
    return s, r, c, out


# --- opt-in and binding ---------------------------------------------------------------------------

def test_default_execution_still_releases_immediately(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result))
    r, c = queued(s)
    out = s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert s.transport.keep_alive_requests == [(False, 0)]
    assert out["session_released"] is True and "handoff" not in out and "live_view_url" not in out
    assert events(s, r, "browserbase_handoff_started") == []
    assert s.svc.handoff_status(r["id"])["handoff_state"] == "none"


def test_opt_in_handoff_keeps_the_filled_session_and_binds_it(tmp_path):
    s, r, c, out = handoff_run(tmp_path, review_seconds=1200)
    assert s.transport.keep_alive_requests == [(True, 1200)]
    assert out["status"] == status(s, r) == "ready_for_human_review"
    assert out["handoff"] == "active" and out["session_released"] is False
    assert out["live_view_url"] == LIVE_URL and out["live_view_expires_in"] == 1200  # runtime only
    assert s.transport.session_status("fake-session-1") == "RUNNING" and s.transport.release_calls == []
    [handoff] = events(s, r, "browserbase_handoff_started")
    approval = s.svc.store.approvals()[r["approval_id"]]
    assert handoff == {"session_id": "fake-session-1", "request_id": r["id"], "application_id": "app:j1",
                       "approval_id": approval["id"], "package_hash": r["package_hash"],
                       "review_expires_at": handoff["review_expires_at"], "live_view_persisted": False}
    assert LIVE_URL.encode() not in store_text(s) and b"SECRET-TOKEN" not in store_text(s)
    assert out["final_submit_clicked"] is False
    no_receipt_and_not_applied(s)
    with pytest.raises(ValueError, match="No reported execution result to verify"):
        s.svc.verify(r["id"], reviewer="human", confirmed=True)


def test_needs_human_input_is_also_reviewable(tmp_path):
    s, r, _, out = handoff_run(tmp_path, FakeTransport(lambda b: ready_result(b, cv_upload={})))
    assert out["status"] == "needs_human_input" and out["handoff"] == "active"


def test_handoff_is_refused_for_other_executors_and_bad_windows(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result))
    approval = s.svc.approve(s.app.id, package_hash=s.svc.preview(s.app.id).package_hash, reviewer="human", reviewed=True)
    r = s.svc.enqueue(approval["id"], executor="airtap")
    c = s.svc.claim(r["id"], worker="w")
    with pytest.raises(ValueError, match="only available for the browserbase executor"):
        s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True)
    assert "handoff_started" not in kinds(s, r)
    s2 = build(tmp_path / "b", FakeTransport(ready_result))
    r2, c2 = queued(s2)
    for bad in (59, 21301):
        with pytest.raises(ValueError, match="Review window"):
            s2.svc.execute(r2["id"], claim_token=c2["claim_token"], human_handoff=True, review_seconds=bad)
    assert s2.transport.calls == 0 and "handoff_started" not in kinds(s2, r2)


def test_unreviewable_or_rejected_results_release_the_kept_session(tmp_path):
    confirmed = [{"kind": "confirmation_text", "value": "Thanks, application received", "sha256": "", "detail": "x"}]
    s, r, _, out = handoff_run(tmp_path, FakeTransport(lambda b: ready_result(b, evidence=confirmed)))
    assert out["status"] == "outcome_unknown" and out["handoff"] == "not_started" and out["session_released"] is True
    assert events(s, r, "browserbase_session_released")[0]["reason"] == "result_not_reviewable"
    assert "live_view_url" not in out

    s2 = build(tmp_path / "b", FakeTransport(lambda b: ready_result(b, final_submit_clicked=True)))
    r2, c2 = queued(s2)
    with pytest.raises(ValueError, match="Invalid Browserbase report"):
        s2.svc.execute(r2["id"], claim_token=c2["claim_token"], human_handoff=True)
    assert events(s2, r2, "browserbase_session_released")[0]["reason"] == "import_failed"
    assert status(s2, r2) == "outcome_unknown" and s2.transport.release_calls == ["fake-session-1"]
    no_receipt_and_not_applied(s2)


# --- status ----------------------------------------------------------------------------------------

def test_status_gives_a_fresh_runtime_link_only_while_running(tmp_path):
    s, r, c, _ = handoff_run(tmp_path)
    view = s.svc.handoff_status(r["id"])
    assert view["handoff_state"] == "active" and view["provider_status"] == "RUNNING"
    assert view["session_id"] == "fake-session-1" and view["live_view_url"] == LIVE_URL
    assert LIVE_URL.encode() not in store_text(s)
    s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])
    calls = len(s.transport.live_view_calls)
    view = s.svc.handoff_status(r["id"])
    assert view["handoff_state"] == "released" and "live_view_url" not in view
    assert len(s.transport.live_view_calls) == calls


# --- explicit release ------------------------------------------------------------------------------

def test_release_is_bound_idempotent_and_records_no_result(tmp_path):
    s, r, c, _ = handoff_run(tmp_path)
    with pytest.raises(ValueError, match="does not belong to this request"):
        s.svc.release_handoff(r["id"], session_id="another-requests-session", claim_token=c["claim_token"])
    with pytest.raises(ValueError, match="Invalid claim token"):
        s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token="wrong")
    assert s.transport.release_calls == [] and events(s, r, "browserbase_session_released") == []

    first = s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])
    again = s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])
    assert first["released"] is True and first["already_released"] is False and first["reason"] == "explicit_release"
    assert again["already_released"] is True
    assert s.transport.release_calls == ["fake-session-1"]
    assert len(events(s, r, "browserbase_session_released")) == 1
    assert first["request_status"] == again["request_status"] == status(s, r) == "ready_for_human_review"
    no_receipt_and_not_applied(s)


def test_release_failure_is_journaled_and_retryable(tmp_path):
    s, r, c, _ = handoff_run(tmp_path, FakeTransport(ready_result, release_errors=1))
    with pytest.raises(ValueError, match="Session release failed"):
        s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])
    assert len(events(s, r, "browserbase_release_failed")) == 1
    assert s.svc.handoff_status(r["id"])["handoff_state"] == "active"
    assert s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])["released"]
    assert s.svc.handoff_status(r["id"])["release_failures"] == 1
    no_receipt_and_not_applied(s)


# --- expiry / lease / recovery ----------------------------------------------------------------------

def test_session_expired_before_human_opened_it(tmp_path):
    s, r, c, _ = handoff_run(tmp_path)
    s.transport.provider_status["fake-session-1"] = "TIMED_OUT"
    view = s.svc.handoff_status(r["id"])
    assert view["provider_status"] == "TIMED_OUT" and "live_view_url" not in view
    out = s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])
    assert out["reason"] == "session_already_ended" and s.transport.release_calls == []
    assert status(s, r) == "ready_for_human_review"
    no_receipt_and_not_applied(s)


def test_human_opens_after_the_ledger_lease_expired(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result))
    r = s.svc.enqueue(s.svc.approve(s.app.id, package_hash=s.svc.preview(s.app.id).package_hash, reviewer="human",
                                    reviewed=True)["id"], executor="browserbase")
    c = s.svc.claim(r["id"], worker="w", lease_seconds=60)
    s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True, review_seconds=1800)
    s.now[0] += timedelta(minutes=10)  # lease long gone, review window still open
    s.svc.list()  # recovery pass must not touch a completed (non-executing) request
    assert status(s, r) == "ready_for_human_review"
    view = s.svc.handoff_status(r["id"])
    assert view["handoff_state"] == "active" and view["live_view_url"] == LIVE_URL
    assert view["live_view_url"] and s.svc._browserbase_transport.live_view_calls[-1][1] == 1200
    assert s.transport.calls == 1  # no second session, no retry
    s.now[0] += timedelta(hours=1)  # review window elapsed: no new links are handed out
    assert s.svc.handoff_status(r["id"])["live_view_url"] is None
    no_receipt_and_not_applied(s)


def test_process_dies_after_link_was_produced(tmp_path):
    transport = FakeTransport(ready_result, live_view_error=KeyboardInterrupt())
    s = build(tmp_path, transport)
    r, c = queued(s)
    with pytest.raises(KeyboardInterrupt):
        s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True)
    transport.live_view_error = None
    assert status(s, r) == "ready_for_human_review" and events(s, r, "browserbase_handoff_started")
    assert s.svc.handoff_status(r["id"])["live_view_url"] == LIVE_URL  # a new process can still review ...
    assert s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])["released"]
    no_receipt_and_not_applied(s)


def test_process_dies_while_session_alive_before_handoff_is_journaled(tmp_path, monkeypatch):
    s = build(tmp_path, FakeTransport(ready_result))
    r, c = queued(s)
    original = SubmissionService.complete_browserbase

    def die_after_import(self, *a, **k):
        original(self, *a, **k)
        raise KeyboardInterrupt
    monkeypatch.setattr(SubmissionService, "complete_browserbase", die_after_import)
    with pytest.raises(KeyboardInterrupt):
        s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True)
    monkeypatch.undo()
    view = s.svc.handoff_status(r["id"])
    assert view["handoff_state"] == "unaccounted_session" and view["provider_status"] == "RUNNING"
    assert "live_view_url" not in view  # only a journaled handoff hands out links
    assert s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])["released"]
    assert s.svc.handoff_status(r["id"])["handoff_state"] == "released"
    no_receipt_and_not_applied(s)


# --- CLI ---------------------------------------------------------------------------------------------

def test_cli_has_handoff_commands_but_no_submit_option(tmp_path, capsys, monkeypatch):
    import argparse
    from jobintel.cli.main import main
    from jobintel.submission.cli import add_submission_commands
    parser = argparse.ArgumentParser()
    add_submission_commands(parser.add_subparsers(dest="command"))
    subs = parser._subparsers._group_actions[0].choices
    assert {"submission-handoff-status", "submission-handoff-release"} <= set(subs)
    options = {o for sub in subs.values() for a in sub._actions for o in a.option_strings}
    assert not any(word in o for o in options for word in ("final", "force", "auto-submit", "--submit"))

    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    s, r, c, _ = handoff_run(tmp_path)

    def cli(*args):
        with patch("sys.argv", ["jobintel", *args, "--store", str(s.root)]):
            main()
        return json.loads(capsys.readouterr().out)

    view = cli("submission-handoff-status", "--request-id", r["id"])  # no credentials: ledger view only
    assert view["handoff_state"] == "active" and view["provider_status"].startswith("unavailable")
    assert "live_view_url" not in view
    with patch("sys.argv", ["jobintel", "submission-handoff-release", "--request-id", r["id"], "--session-id",
                            "fake-session-1", "--claim-token", c["claim_token"], "--store", str(s.root)]), \
            pytest.raises(SystemExit):
        main()
    assert "BROWSERBASE_API_KEY is not set" in capsys.readouterr().err
    assert events(s, r, "browserbase_release_failed") == [] and events(s, r, "browserbase_session_released") == []
