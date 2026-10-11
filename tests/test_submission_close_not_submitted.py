"""Human-confirmed `closed_not_submitted` for stop-only requests (synthetic, offline).

After a stop-only attempt (and any human Live View handoff) a named human may confirm that
NO application was submitted. That confirmation -- never missing evidence -- moves the request
to `closed_not_submitted`: terminal, non-blocking for the application, permanently journaled,
not a receipt and never APPLIED. Every precondition fails closed.
"""
from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import patch

import pytest

from jobintel.models.submission import ExecutionResult
from jobintel.models.taxonomy import ApplicationStatus
from test_browserbase_service import (FakeTransport, approve, build, kinds, no_receipt_and_not_applied, queued,
                                      ready_result, status)

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

REVIEWER, REASON = "Sample Reviewer", "Watched the Live View handoff; nothing was submitted; no confirmation email."


def reviewable(tmp_path, transport=None):
    """A Browserbase request stopped at needs_human_input with a kept (active) handoff session."""
    s = build(tmp_path, transport or FakeTransport(lambda b: ready_result(b, cv_upload={})))
    r, c = queued(s)
    out = s.svc.execute(r["id"], claim_token=c["claim_token"], human_handoff=True)
    assert out["status"] in {"needs_human_input", "ready_for_human_review"} and out["handoff"] == "active"
    return s, r, c


def released(tmp_path):
    s, r, c = reviewable(tmp_path)
    s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])
    return s, r, c


def close(s, r, **changes):
    args = dict(reviewer=REVIEWER, confirmed_not_submitted=True, reason=REASON)
    args.update(changes)
    return s.svc.close_not_submitted(r["id"], **args)


def journal(s):
    return (s.root / "submissions/events.jsonl").read_bytes()


# --- 1. the happy path ---------------------------------------------------------------------------

def test_released_needs_human_input_closes_with_an_audited_human_confirmation(tmp_path):
    s, r, _ = released(tmp_path)
    before = journal(s)
    data = close(s, r)
    approval = s.svc.store.approvals()[r["approval_id"]]
    assert data == {"request_id": r["id"], "application_id": "app:j1", "approval_id": approval["id"],
                    "package_hash": r["package_hash"], "executor": "browserbase", "reviewer": REVIEWER,
                    "confirmed_not_submitted": True, "reason": REASON, "closed_at": data["closed_at"],
                    "previous_status": "needs_human_input", "new_status": "closed_not_submitted",
                    "receipt": False, "application_marked_submitted": False}
    assert status(s, r) == "closed_not_submitted"
    # 12. prior history intact: the journal only grew by the one close event.
    after = journal(s)
    assert after.startswith(before) and after[len(before):].count(b"\n") == 1
    assert kinds(s, r)[-1] == "closed_not_submitted"
    assert {"needs_human_input", "browserbase_handoff_started", "browserbase_session_released"} <= set(kinds(s, r))
    # 14. not a receipt, not APPLIED.
    no_receipt_and_not_applied(s)
    assert s.apps.for_job("j1").status == ApplicationStatus.DISCOVERED


@pytest.mark.parametrize("final", ["ready_for_human_review", "outcome_unknown"])
def test_other_uncertain_non_submitted_states_can_also_be_closed(tmp_path, final):
    if final == "ready_for_human_review":
        s, r, c = reviewable(tmp_path, FakeTransport(ready_result))
        s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])
    else:  # interrupted before any session: outcome_unknown, nothing to release
        s = build(tmp_path, FakeTransport(raise_before_session=RuntimeError("provider down")))
        r, c = queued(s)
        with pytest.raises(ValueError):
            s.svc.execute(r["id"], claim_token=c["claim_token"])
    assert status(s, r) == final
    assert close(s, r)["previous_status"] == final and status(s, r) == "closed_not_submitted"


# --- 2. sessions must be released first ------------------------------------------------------------

def test_active_handoff_is_refused_and_nothing_is_released_implicitly(tmp_path):
    s, r, _ = reviewable(tmp_path)
    with pytest.raises(ValueError, match="release it explicitly"):
        close(s, r)
    assert s.transport.release_calls == [] and status(s, r) == "needs_human_input"
    assert "closed_not_submitted" not in kinds(s, r)


def test_unaccounted_session_is_refused(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result, raise_after_session=KeyboardInterrupt()))
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase")
    c = s.svc.claim(r["id"], worker="w", lease_seconds=60)
    with pytest.raises(KeyboardInterrupt):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    s.now[0] += timedelta(minutes=5)
    s.svc.list()  # recovery: outcome_unknown, a session was started and never accounted for
    assert status(s, r) == "outcome_unknown"
    with pytest.raises(ValueError, match="active or unaccounted"):
        close(s, r)


# --- 3-5. explicit human input ---------------------------------------------------------------------

@pytest.mark.parametrize("changes,match", [
    (dict(confirmed_not_submitted=False), "confirmed-not-submitted"),
    (dict(confirmed_not_submitted="yes"), "confirmed-not-submitted"),
    (dict(reviewer=""), "named reviewer"),
    (dict(reviewer="   "), "named reviewer"),
    (dict(reason=""), "human reason"),
    (dict(reason="  "), "human reason"),
    (dict(reason="x" * 2001), "human reason"),
])
def test_missing_or_implicit_human_input_is_refused(tmp_path, changes, match):
    s, r, _ = released(tmp_path)
    with pytest.raises(ValueError, match=match):
        close(s, r, **changes)
    assert status(s, r) == "needs_human_input"


# --- 6-10. anything suggesting a submission, or a non-stop-only executor ----------------------------

def forge(s, r, kind, data):
    """Simulate ledger state the normal stop-only flow cannot produce (guards must still hold)."""
    s.svc.store.append(kind, r["id"], data, event_id=f"forged-{kind}:{r['id']}", at=s.now[0].isoformat())


def test_submitted_at_present_is_refused(tmp_path):
    s, r, _ = released(tmp_path)
    result = dict(s.svc.store.requests()[r["id"]]["result"], submitted_at=s.now[0].isoformat())
    forge(s, r, "outcome_unknown", {"result": result})
    with pytest.raises(ValueError, match="possible submission"):
        close(s, r)


def test_submission_evidence_in_the_result_is_refused(tmp_path):
    s, r, _ = released(tmp_path)
    result = dict(s.svc.store.requests()[r["id"]]["result"],
                  evidence=[{"kind": "confirmation_text", "value": "Thanks for applying", "sha256": "", "detail": "x"}])
    forge(s, r, "outcome_unknown", {"result": result})
    with pytest.raises(ValueError, match="possible submission"):
        close(s, r)


def test_receipt_present_is_refused(tmp_path):
    s, r, _ = released(tmp_path)
    s.svc.store.append("receipt_verified", "receipt:" + r["id"], {"request_id": r["id"]},
                       event_id="receipt:" + r["id"], receipt=True, at=s.now[0].isoformat())
    with pytest.raises(ValueError):
        close(s, r)
    assert "closed_not_submitted" not in kinds(s, r)


def test_final_submit_clicked_is_refused(tmp_path):
    s, r, _ = released(tmp_path)
    forge(s, r, "browserbase_result", {"final_submit_clicked": True, "evidence": []})
    with pytest.raises(ValueError, match="final-submit click"):
        close(s, r)


def test_reported_submission_is_refused(tmp_path):
    s, r, _ = released(tmp_path)
    forge(s, r, "completed", {})
    with pytest.raises(ValueError):
        close(s, r)


def test_applied_application_is_refused(tmp_path):
    from dataclasses import replace
    s, r, _ = released(tmp_path)
    with pytest.raises(ValueError):  # the application store itself refuses APPLIED while a submission is active
        s.apps.update_status("app:j1", ApplicationStatus.APPLIED, note="human applied elsewhere")
    # Simulate an APPLIED record anyway (e.g. written by another tool): closing must still refuse.
    s.apps._write_applications([replace(a, status=ApplicationStatus.APPLIED, applied_at=s.now[0])
                                if a.id == "app:j1" else a for a in s.apps.read_applications()])
    with pytest.raises(ValueError, match="already marked submitted"):
        close(s, r)
    assert status(s, r) == "needs_human_input"


def test_non_stop_only_executor_is_refused(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result))
    r = s.svc.enqueue(approve(s)["id"], executor="manual")
    c = s.svc.claim(r["id"], worker="w")
    s.svc.execute(r["id"], claim_token=c["claim_token"])
    s.svc.complete(r["id"], claim_token=c["claim_token"], result=ExecutionResult(
        r["id"], r["package_hash"], "j1", "cv-0123456789ab", "https://jobs.example.test/cloud/apply.html", "manual",
        "needs_human_input", (), ("question",)))
    assert status(s, r) == "needs_human_input"
    with pytest.raises(ValueError, match="Only stop-only executor"):
        close(s, r)


@pytest.mark.parametrize("state", ["submission_requested", "executing"])
def test_states_that_are_not_finished_attempts_are_refused(tmp_path, state):
    s = build(tmp_path, FakeTransport(ready_result))
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase")
    if state == "executing":
        s.svc.claim(r["id"], worker="w")
    with pytest.raises(ValueError, match="cannot be closed"):
        close(s, r)


# --- 11. non-blocking: a fresh approval and request become possible ---------------------------------

def test_closed_request_no_longer_blocks_a_fresh_approval_and_request(tmp_path):
    s, r, _ = released(tmp_path)
    with pytest.raises(ValueError, match="active submission"):
        approve(s)  # blocked before the human confirmation
    close(s, r)
    s.svc.revoke(r["approval_id"], reviewer=REVIEWER, reason="superseded after closed_not_submitted")
    assert status(s, r) == "closed_not_submitted"  # revocation does not touch a closed request
    fresh = approve(s)
    assert fresh["id"] != r["approval_id"] and fresh["status"] == "approved"
    r2 = s.svc.enqueue(fresh["id"], executor="browserbase")
    assert r2["id"] != r["id"] and r2["status"] == "submission_requested"
    assert status(s, r) == "closed_not_submitted" and "closed_not_submitted" in kinds(s, r)


# --- 13. idempotency and no contradictory later transitions ------------------------------------------

def test_repeating_the_same_confirmation_is_idempotent(tmp_path):
    s, r, _ = released(tmp_path)
    first = close(s, r)
    size = len(journal(s))
    assert close(s, r) == first and len(journal(s)) == size
    with pytest.raises(ValueError, match="different confirmation"):
        close(s, r, reason="another story")
    with pytest.raises(ValueError, match="different confirmation"):
        close(s, r, reviewer="Someone Else")
    assert len(journal(s)) == size


def test_closed_request_accepts_no_contradictory_transition(tmp_path):
    s, r, c = released(tmp_path)
    report = next(e["data"] for e in s.svc.store.events() if e["kind"] == "browserbase_result")
    close(s, r)
    with pytest.raises(ValueError, match="closed by a human"):
        s.svc.complete_browserbase(r["id"], claim_token=c["claim_token"], value=report)
    with pytest.raises(ValueError):
        s.svc.verify(r["id"], reviewer=REVIEWER, confirmed=True)
    with pytest.raises(ValueError):
        s.svc.execute(r["id"], claim_token=c["claim_token"])
    with pytest.raises(ValueError):
        s.svc.claim(r["id"], worker="w2")
    assert s.svc.release_handoff(r["id"], session_id="fake-session-1", claim_token=c["claim_token"])["already_released"]
    s.svc.list()
    s.now[0] += timedelta(days=3)
    s.svc.list()  # approval expiry does not reopen or downgrade a closed request
    assert status(s, r) == "closed_not_submitted"
    assert [e["kind"] for e in s.svc.store.events() if e["entity_id"] == r["id"]].count("closed_not_submitted") == 1
    no_receipt_and_not_applied(s)


def test_airtap_request_can_be_closed_after_an_expired_claim(tmp_path):
    s = build(tmp_path, FakeTransport(ready_result))
    r = s.svc.enqueue(approve(s)["id"], executor="airtap")
    c = s.svc.claim(r["id"], worker="w", lease_seconds=60)
    s.svc.execute(r["id"], claim_token=c["claim_token"])  # local bundle only
    s.now[0] += timedelta(minutes=5)
    s.svc.list()
    assert status(s, r) == "outcome_unknown"
    assert close(s, r)["executor"] == "airtap" and status(s, r) == "closed_not_submitted"
    with pytest.raises(ValueError, match="closed by a human"):
        s.svc.complete_airtap(r["id"], claim_token=c["claim_token"], value={})


# --- 15. no provider / network / browser call ---------------------------------------------------------

def test_closing_contacts_no_provider_network_or_browser(tmp_path):
    s, r, _ = released(tmp_path)

    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"transport.{name} must not be used when closing")

    s.svc._browserbase_transport = Untouchable()
    with patch("urllib.request.urlopen", side_effect=AssertionError("no network")), \
            patch("jobintel.submission.browserbase_cloud.CloudTransport", side_effect=AssertionError("no transport")):
        close(s, r)
    assert status(s, r) == "closed_not_submitted"


# --- CLI -------------------------------------------------------------------------------------------------

def test_cli_close_requires_every_explicit_flag(tmp_path, capsys):
    from jobintel.cli.main import main
    s, r, _ = released(tmp_path)
    base = ["jobintel", "submission-close", "--request-id", r["id"], "--store", str(s.root)]
    for missing in (["--reviewer", REVIEWER, "--reason", REASON],
                    ["--confirmed-not-submitted", "--reason", REASON],
                    ["--confirmed-not-submitted", "--reviewer", REVIEWER]):
        with patch("sys.argv", base + missing), pytest.raises(SystemExit):
            main()
    capsys.readouterr()
    assert status(s, r) == "needs_human_input"
    with patch("sys.argv", base + ["--reviewer", REVIEWER, "--confirmed-not-submitted", "--reason", REASON]):
        main()
    out = json.loads(capsys.readouterr().out)
    assert out["new_status"] == "closed_not_submitted" and out["receipt"] is False
    assert status(s, r) == "closed_not_submitted"
