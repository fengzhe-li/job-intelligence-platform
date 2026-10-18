"""Apply-one: preview -> start -> status -> release for ONE application id, offline.

Synthetic candidates and stores only. The end-to-end case drives the real local_browser
transport (headless Chrome) against the synthetic Ashby-style hosted form served in-process
(every other hostname resolves to NOTFOUND), through a test profile for the synthetic host --
the production registry holds only the reviewed Ashby profile, which refuses that host.
"""
from __future__ import annotations

import json

import pytest

from jobintel.submission import apply_one
from jobintel.submission.ats_profiles import ExecutionProfile, UnknownExecutionProfile
from jobintel.submission.local_browser import LocalBrowserTransport
from test_browserbase_graphql_and_identity import HOST
from test_browserbase_live_host import as_real_host  # noqa: F401  (fixture)
from test_browserbase_service import build, no_receipt_and_not_applied
from test_local_browser_executor import OFFLINE, Run, no_browserbase, runs  # noqa: F401
from test_local_cv_upload_and_fields import GEO, HANDLE, SET, STORAGE, TO_FILE, HostedServer
from test_local_cv_upload_service import BANK, URL
from test_supervised_execution import ACK_REASON, UNSUPPORTED, _store

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")


def profile_for(host, **changes):
    values = dict(name="synthetic_hosted", host=host, graphql_reads=(GEO,), graphql_form_writes=(SET,),
                  nonfatal_blocked_writes=(), cv_upload_mutations=(HANDLE,), cv_storage_writes=(("POST", STORAGE),),
                  cv_association_mutations=(TO_FILE,), validated="synthetic test fixture")
    values.update(changes)
    return ExecutionProfile(**values)


SYNTHETIC = {HOST: profile_for(HOST)}


def ledger(svc):
    return {"approvals": dict(svc.store.approvals()), "requests": dict(svc.store.requests())}


def local_store(tmp_path):
    server = HostedServer()
    s = build(tmp_path / "store", url=URL, answers=BANK)
    transport = LocalBrowserTransport(runtime_dir=s.svc.runtime_root / "local_browser", headless=True,
                                      serve=server.handle, extra_args=OFFLINE)
    s.svc._local_browser_transport = transport
    return s, server, transport


class Executed:
    """Stands in for SubmissionService.execute where a browser run is not the point of the test."""

    def __init__(self):
        self.calls = []

    def __call__(self, request_id, **kwargs):
        self.calls.append((request_id, kwargs))
        return {"status": "needs_human_input", "final_submit_clicked": False, "handoff": "local_browser_active"}


# --- unknown hosts: refused before anything happens -------------------------------------------------------

def test_unknown_host_is_refused_at_preview_and_start(tmp_path):
    s = build(tmp_path / "store", url=URL, answers=BANK)  # jobs.example.test: no reviewed production profile
    before = ledger(s.svc)
    with pytest.raises(UnknownExecutionProfile):
        apply_one.preview(s.svc, s.app.id)
    with pytest.raises(UnknownExecutionProfile):
        apply_one.start(s.svc, s.app.id, package_hash="0" * 64, reviewer="human", reviewed=True)
    assert ledger(s.svc) == before and not (s.svc.root / "supervised_acks").exists()


# --- preview ----------------------------------------------------------------------------------------------

def test_preview_shows_the_exact_package_and_changes_nothing(tmp_path):
    s = build(tmp_path / "store", url=URL, answers=BANK)
    before = ledger(s.svc)
    out = apply_one.preview(s.svc, s.app.id, profiles=SYNTHETIC)
    assert out["package_hash"] == s.svc.preview(s.app.id).package_hash
    assert (out["execution_profile"], out["host"], out["executor"]) == ("synthetic_hosted", HOST, "local_browser")
    assert out["acknowledgement"] is None and out["final_submit_allowed"] is False
    assert "alex@example.com" not in json.dumps(out)  # field names only, never values
    assert ledger(s.svc) == before


def test_preview_refuses_reviewer_flags_without_acknowledging(tmp_path):
    s = build(tmp_path / "store", url=URL, answers=BANK)
    with pytest.raises(ValueError, match="only used with --acknowledge-blockers"):
        apply_one.preview(s.svc, s.app.id, reviewer="human", profiles=SYNTHETIC)
    with pytest.raises(ValueError, match="not blocked"):
        apply_one.preview(s.svc, s.app.id, acknowledge_blockers=True, reviewer="human", ack_reason="x", profiles=SYNTHETIC)


# --- acknowledgement of BLOCK_AUTO_SUBMISSION -------------------------------------------------------------

@pytest.fixture
def blocked(tmp_path):
    s = _store(tmp_path)  # BLOCK_AUTO_SUBMISSION (one unsupported must-have), eligible, auto-application policy
    s.profiles = {"example.com": profile_for("example.com")}
    return s


def ack_files(s):
    return sorted((s.root / "supervised_acks").glob("*.json")) if (s.root / "supervised_acks").exists() else []


def test_a_blocked_package_needs_an_explicit_acknowledgement(blocked):
    with pytest.raises(ValueError, match="--acknowledge-blockers") as error:
        apply_one.preview(blocked.svc, blocked.app.id, profiles=blocked.profiles)
    assert UNSUPPORTED in str(error.value) and ack_files(blocked) == []
    with pytest.raises(ValueError, match="needs --reviewer and --ack-reason"):
        apply_one.preview(blocked.svc, blocked.app.id, acknowledge_blockers=True, reviewer="candidate",
                          profiles=blocked.profiles)
    assert ack_files(blocked) == []


def test_acknowledging_writes_exactly_one_binding_acknowledgement_then_reuses_it(blocked):
    created = apply_one.preview(blocked.svc, blocked.app.id, acknowledge_blockers=True, reviewer="candidate",
                                ack_reason=ACK_REASON, profiles=blocked.profiles)
    assert created["acknowledgement"]["source"] == "created" and created["execution_mode"] == "SUPERVISED_EXECUTION_ONLY"
    [path] = ack_files(blocked)
    data = json.loads(path.read_text())
    assert (data["application_id"], data["job_id"], data["cv_artifact_id"], data["cv_pdf_sha256"]) == (
        blocked.app.id, blocked.job.id, blocked.artifact.id, blocked.artifact.pdf_sha256)
    assert data["acknowledged_blockers"] == [UNSUPPORTED] and data["final_submit_allowed"] is False
    again = apply_one.preview(blocked.svc, blocked.app.id, profiles=blocked.profiles)
    assert again["acknowledgement"]["source"] == "existing" and again["package_hash"] == created["package_hash"]
    assert len(ack_files(blocked)) == 1  # reused, never duplicated


def test_an_acknowledgement_for_other_reasons_or_another_cv_is_not_reused(blocked):
    directory = blocked.root / "supervised_acks"
    directory.mkdir()
    base = dict(reviewer="candidate", reviewed=True, acknowledged_at="2026-10-01T09:00:00+00:00", reason=ACK_REASON,
                application_id=blocked.app.id, job_id=blocked.job.id, cv_artifact_id=blocked.artifact.id,
                cv_pdf_sha256=blocked.artifact.pdf_sha256, execution_mode="SUPERVISED_EXECUTION_ONLY",
                final_submit_allowed=False)
    (directory / "other-reasons.json").write_text(json.dumps({**base, "acknowledged_blockers": ["Some other reason"]}))
    (directory / "other-cv.json").write_text(json.dumps({**base, "acknowledged_blockers": [UNSUPPORTED],
                                                         "cv_pdf_sha256": "f" * 64}))
    (directory / "garbage.json").write_text("{not json")
    with pytest.raises(ValueError, match="--acknowledge-blockers"):
        apply_one.preview(blocked.svc, blocked.app.id, profiles=blocked.profiles)


def test_start_with_an_acknowledged_package_approves_it_supervised_only(blocked):
    shown = apply_one.preview(blocked.svc, blocked.app.id, acknowledge_blockers=True, reviewer="candidate",
                              ack_reason=ACK_REASON, profiles=blocked.profiles)
    blocked.svc.execute = executed = Executed()
    with pytest.raises(ValueError, match="approving reviewer must be the reviewer"):
        apply_one.start(blocked.svc, blocked.app.id, package_hash=shown["package_hash"], reviewer="someone-else",
                        reviewed=True, profiles=blocked.profiles)
    out = apply_one.start(blocked.svc, blocked.app.id, package_hash=shown["package_hash"], reviewer="candidate",
                          reviewed=True, profiles=blocked.profiles)
    approval = blocked.svc.store.approvals()[out["approval_id"]]
    assert approval["package"]["supervised_acknowledgement"]["reviewer"] == "candidate"
    assert approval["execution_mode"] == "SUPERVISED_EXECUTION_ONLY"
    [(request_id, kwargs)] = executed.calls
    assert blocked.svc.store.requests()[request_id]["live_host"] == "example.com"
    assert kwargs["human_handoff"] is True and kwargs["live_host"] == "example.com" and kwargs["live_cv_upload"] is True


def test_start_never_creates_an_acknowledgement(blocked):
    with pytest.raises(ValueError, match="--acknowledge-blockers"):
        apply_one.start(blocked.svc, blocked.app.id, package_hash="0" * 64, reviewer="candidate", reviewed=True,
                        profiles=blocked.profiles)
    assert ack_files(blocked) == [] and blocked.svc.store.approvals() == {}


# --- start: explicit, exact, once -------------------------------------------------------------------------

@pytest.mark.parametrize("change,message", [
    ({"package_hash": "0" * 64}, "hash mismatch"),
    ({"reviewed": False}, "requires --reviewed"),
    ({"reviewer": " "}, "requires --reviewer"),
])
def test_start_refuses_without_the_exact_reviewed_hash(tmp_path, change, message):
    s = build(tmp_path / "store", url=URL, answers=BANK)
    s.svc.execute = executed = Executed()
    args = {"package_hash": apply_one.preview(s.svc, s.app.id, profiles=SYNTHETIC)["package_hash"],
            "reviewer": "human", "reviewed": True, **change}
    with pytest.raises(ValueError, match=message):
        apply_one.start(s.svc, s.app.id, profiles=SYNTHETIC, **args)
    assert s.svc.store.approvals() == {} and s.svc.store.requests() == {} and executed.calls == []


def test_start_passes_exactly_the_profile_policy_and_keeps_the_token_out_of_the_output(tmp_path):
    s = build(tmp_path / "store", url=URL, answers=BANK)
    s.svc.execute = executed = Executed()
    shown = apply_one.preview(s.svc, s.app.id, profiles=SYNTHETIC)
    out = apply_one.start(s.svc, s.app.id, package_hash=shown["package_hash"], reviewer="human", reviewed=True,
                          profiles=SYNTHETIC)
    [(request_id, kwargs)] = executed.calls
    token = kwargs.pop("claim_token")
    assert kwargs == {"human_handoff": True, "review_seconds": 1800, **SYNTHETIC[HOST].execute_options()}
    request = s.svc.store.requests()[request_id]
    assert (request["executor"], request["live_host"], request["worker"], request["status"]) == (
        "local_browser", HOST, "local-browser", "executing")
    assert token not in json.dumps(out) and out["request_id"] == request_id and out["superseded_approvals"] == []
    # Once: no second attempt while this one is active.
    with pytest.raises(ValueError, match="already has an active attempt"):
        apply_one.start(s.svc, s.app.id, package_hash=shown["package_hash"], reviewer="human", reviewed=True,
                        profiles=SYNTHETIC)
    with pytest.raises(ValueError, match="already has an active attempt"):
        apply_one.preview(s.svc, s.app.id, profiles=SYNTHETIC)
    assert len(executed.calls) == 1


def test_a_consumed_approval_is_superseded_visibly_for_a_new_attempt(tmp_path):
    s = build(tmp_path / "store", url=URL, answers=BANK)
    s.svc.execute = Executed()
    shown = apply_one.preview(s.svc, s.app.id, profiles=SYNTHETIC)
    first = apply_one.start(s.svc, s.app.id, package_hash=shown["package_hash"], reviewer="human", reviewed=True,
                            profiles=SYNTHETIC)
    s.svc.store.append("needs_human_input", first["request_id"], {}, event_id="nhi:" + first["request_id"])
    s.svc.close_not_submitted(first["request_id"], reviewer="human", confirmed_not_submitted=True,
                              reason="reviewed, not submitted")
    second = apply_one.start(s.svc, s.app.id, package_hash=shown["package_hash"], reviewer="human", reviewed=True,
                             profiles=SYNTHETIC)
    assert second["superseded_approvals"] == [first["approval_id"]] and second["approval_id"] != first["approval_id"]
    old = s.svc.store.approvals()[first["approval_id"]]
    assert old["status"] == "revoked"
    revocation = next(e for e in s.svc.store.events() if e["entity_id"] == first["approval_id"] and e["kind"] == "revoked")
    assert "superseded: approval already used by " + first["request_id"] in revocation["data"]["reason"]
    assert second["request_id"] != first["request_id"]


def test_a_failed_execution_is_reported_sanitized_and_never_retried(tmp_path):
    s = build(tmp_path / "store", url=URL, answers=BANK)

    def boom(request_id, **kwargs):
        raise RuntimeError("secret detail https://user:pw@example.invalid ws://127.0.0.1/devtools/browser/x")
    s.svc.execute = boom
    shown = apply_one.preview(s.svc, s.app.id, profiles=SYNTHETIC)
    out = apply_one.start(s.svc, s.app.id, package_hash=shown["package_hash"], reviewer="human", reviewed=True,
                          profiles=SYNTHETIC)
    assert out["error"] == "RuntimeError" and "secret" not in json.dumps(out) and "devtools" not in json.dumps(out)
    with pytest.raises(ValueError, match="already has an active attempt"):  # no retry path
        apply_one.start(s.svc, s.app.id, package_hash=shown["package_hash"], reviewer="human", reviewed=True,
                        profiles=SYNTHETIC)


# --- end to end on the real local transport ---------------------------------------------------------------

def test_end_to_end_fill_upload_escalate_handoff_release(tmp_path, runs, as_real_host):  # noqa: F811
    pytest.importorskip("playwright.sync_api")
    s, server, transport = local_store(tmp_path)
    shown = apply_one.preview(s.svc, s.app.id, profiles=SYNTHETIC)
    out = apply_one.start(s.svc, s.app.id, package_hash=shown["package_hash"], reviewer="human", reviewed=True,
                          profiles=SYNTHETIC)
    request = s.svc.store.requests()[out["request_id"]]
    runs.append(Run(s, request, {"claim_token": request["claim_token"]}, out, server, transport))
    assert out["identity_state"] == "verified" and out["status"] == "needs_human_input"
    assert out["cv_upload_status"] == "uploaded" and out["cv_upload_visible_in_page"] is True
    assert out["cv_upload_steps"] == {"upload_handle_created": True, "storage_written": True, "form_associated": True,
                                      "visible_in_page": True, "responses_ok": True}
    filled = {f["field"] for f in out["fields_filled"]}
    assert "location" in filled and "require_sponsorship_now_or_future" not in filled  # work auth is human-only
    assert out["fields_escalated"] and out["blocked_fatal_operations"] == []
    assert (out["final_submit_clicked"], out["handoff"], out["page_guard_released"], out["browser_process_state"]) == (
        False, "local_browser_active", True, "open")
    assert "ApiSubmitApplication" not in server.operations and server.external == []
    printed = json.dumps(out)
    assert request["claim_token"] not in printed and "ws://" not in printed and "devtools" not in printed
    assert "alex@example.com" not in printed and "07000000000" not in printed  # no form values
    # status: same sanitized outcome, changes nothing
    assert apply_one.status(s.svc, application_id=s.app.id)["cv_upload_status"] == "uploaded"
    # release: ends only this attempt's browser; the request still needs the human's close
    released = apply_one.release(s.svc, application_id=s.app.id)
    assert (released["browser_before"], released["released"], released["browser_after"]) == ("open", True, "closed")
    assert released["request_status"] == "needs_human_input" and request["claim_token"] not in json.dumps(released)
    assert apply_one.release(s.svc, request_id=out["request_id"])["already_released"] is True
    with pytest.raises(ValueError, match="already has an active attempt"):
        apply_one.preview(s.svc, s.app.id, profiles=SYNTHETIC)
    s.svc.close_not_submitted(out["request_id"], reviewer="human", confirmed_not_submitted=True, reason="not submitted")
    assert apply_one.preview(s.svc, s.app.id, profiles=SYNTHETIC)["package_hash"] == shown["package_hash"]
    no_receipt_and_not_applied(s)
    assert runs[-1].record() is None  # profile and session record are gone


# --- status / release targeting ---------------------------------------------------------------------------

def test_status_and_release_need_exactly_one_target_and_a_local_browser_attempt(tmp_path):
    s = build(tmp_path / "store", url=URL, answers=BANK)
    with pytest.raises(ValueError, match="exactly one"):
        apply_one.status(s.svc)
    with pytest.raises(ValueError, match="exactly one"):
        apply_one.release(s.svc, application_id=s.app.id, request_id="request:x")
    with pytest.raises(ValueError, match="No local_browser attempt"):
        apply_one.release(s.svc, application_id=s.app.id)


# --- CLI ----------------------------------------------------------------------------------------------------

def test_cli_has_the_four_steps_and_no_path_policy_host_or_token_options():
    import argparse
    from jobintel.submission.cli import add_submission_commands
    parser = argparse.ArgumentParser()
    add_submission_commands(parser.add_subparsers(dest="command"))
    choices = parser._subparsers._group_actions[0].choices
    options = {name: {o for a in choices[name]._actions for o in a.option_strings}
               for name in ("submission-apply-preview", "submission-apply-start", "submission-apply-status",
                            "submission-apply-release")}
    assert {"--application-id", "--acknowledge-blockers", "--reviewer", "--ack-reason"} <= options["submission-apply-preview"]
    assert {"--application-id", "--package-hash", "--reviewer", "--reviewed"} <= options["submission-apply-start"]
    for opts in options.values():
        assert not any(w in o for o in opts for w in ("path", "file", "token", "host", "graphql", "write", "storage",
                                                      "final", "submit", "retry", "profile"))
    start = choices["submission-apply-start"]
    with pytest.raises(SystemExit):
        start.parse_args(["--application-id", "a", "--package-hash", "h", "--reviewer", "r"])  # --reviewed missing
    with pytest.raises(SystemExit):
        choices["submission-apply-status"].parse_args([])  # needs exactly one target
