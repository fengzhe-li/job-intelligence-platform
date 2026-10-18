"""Approved CV upload end to end through SubmissionService and the local_browser transport (offline).

The service builds the upload permission only from the approved package: the CV attached to the
application, sealed in the package (artifact id + PDF SHA-256) and bound to this request. The
caller never supplies a path. The handed-off window shows the uploaded CV; nothing is submitted.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from jobintel.submission.local_browser import LocalBrowserTransport
from test_browserbase_graphql_and_identity import HOST
from test_browserbase_live_host import as_real_host  # noqa: F401  (fixture)
from test_browserbase_service import approve, build, no_receipt_and_not_applied
from test_local_browser_executor import OFFLINE, Run, assert_released, no_browserbase, runs  # noqa: F401
from test_local_cv_upload_and_fields import GEO, HANDLE, SET, STORAGE, TO_FILE, HostedServer

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

sync_api = pytest.importorskip("playwright.sync_api")

BANK = {
    "identity.legal_first_name": "Alex", "identity.legal_last_name": "Example", "identity.legal_full_name": "Alex Example",
    "identity.email": "alex@example.com", "identity.phone_uk": "07000000000",
    "address.address_line_1": "1 Example Street", "address.city": "Sampleton", "address.postcode": "EX1 1AA",
    "address.country": "United Kingdom",
    "work_auth.current_legal_right_to_work_uk_answer": "Yes",
    "work_auth.permanent_or_unrestricted_right_to_work_answer": "No",
    "sponsorship.now_or_future_sponsorship_required_answer": "Yes",
}
URL = f"https://{HOST}/graphql/hosted_form.html?mode=ok"
UPLOAD = {"live_cv_upload": True, "live_cv_upload_mutations": [HANDLE], "live_cv_storage_writes": [("POST", STORAGE)],
          "live_cv_association_mutations": [TO_FILE]}


def queued_local(tmp_path):
    server = HostedServer()
    s = build(tmp_path / "store", url=URL, answers=BANK)
    transport = LocalBrowserTransport(runtime_dir=s.svc.runtime_root / "local_browser", headless=True,
                                      serve=server.handle, extra_args=OFFLINE)
    s.svc._local_browser_transport = transport
    r = s.svc.enqueue(approve(s)["id"], executor="local_browser", live_host=HOST)
    c = s.svc.claim(r["id"], worker="local")
    return s, r, c, server, transport


def live(**extra):
    return {"human_handoff": True, "allow_live_host": True, "live_host": HOST, "live_graphql_reads": [GEO],
            "live_graphql_form_writes": [SET], **extra}


def resume_shown(run):
    profile = Path(run.record()["profile_dir"])
    port, path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
    with sync_api.sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"ws://127.0.0.1:{int(port)}{path}")
        page = next(pg for ctx in browser.contexts for pg in ctx.pages if "/hosted_form.html" in pg.url)
        return page.inner_text("#resume-name"), page.evaluate("() => document.body.dataset.submitted || '0'")


def test_A_C_L_service_uploads_the_approved_cv_and_hands_over_an_unsubmitted_draft(tmp_path, runs, as_real_host):  # noqa: F811
    s, r, c, server, transport = queued_local(tmp_path)
    out = s.svc.execute(r["id"], claim_token=c["claim_token"], **live(**UPLOAD))
    run = Run(s, r, c, out, server, transport)
    runs.append(run)
    events = {e["kind"]: e["data"] for e in s.svc.store.events() if e["entity_id"] == r["id"]}
    audit = events["browserbase_live_host_authorized"]
    assert (audit["cv_upload_authorized"], audit["cv_artifact_id"], audit["cv_pdf_sha256"]) == (
        True, s.artifact.id, s.artifact.pdf_sha256)
    assert (audit["cv_upload_mutation_count"], audit["cv_storage_write_count"], audit["cv_association_mutation_count"],
            audit["final_submit_allowed"]) == (1, 1, 1, False)
    form = events["browserbase_evidence"]["live"]["form"]
    assert (form["cv_upload_status"], form["cv_artifact_id"], form["cv_upload_visible_in_page"]) == (
        "uploaded", s.artifact.id, True)
    filled = {f["field"] for f in form["fields_filled"]}
    assert "location" in filled  # work authorisation is human-only: never filled
    assert not filled & {"require_sponsorship_now_or_future", "permanent_or_unrestricted_right_to_work"}
    # The storage host received exactly the approved PDF's bytes.
    assert [h for _, h, _ in server.storage] == [STORAGE]
    assert Path(s.artifact.pdf_path).read_bytes() in server.storage[0][2]
    # Handoff: the window shows the uploaded CV, nothing was submitted, the human finishes.
    assert out["final_submit_clicked"] is False and out["handoff"] == "local_browser_active"
    assert out["page_guard_released"] is True
    # J: the ATS shows the review copy's naming <Name>_<Company>_<Role>_CV.pdf (company/role from the package).
    assert resume_shown(run) == ("Alex_Example_ExampleCo_Graduate_Engineer_CV.pdf", "0")
    # K: only the name changed -- the uploaded bytes are the sealed PDF, same SHA-256.
    sealed = Path(s.artifact.pdf_path).read_bytes()
    assert hashlib.sha256(sealed).hexdigest() == s.artifact.pdf_sha256 == audit["cv_pdf_sha256"]
    assert "ApiSubmitApplication" not in server.operations and server.external == []
    no_receipt_and_not_applied(s)
    assert_released(run)


def test_B_upload_operations_need_the_explicit_opt_in(tmp_path, runs, as_real_host):  # noqa: F811
    s, r, c, server, transport = queued_local(tmp_path)
    with pytest.raises(ValueError, match="explicit CV upload opt-in"):
        s.svc.execute(r["id"], claim_token=c["claim_token"],
                      **live(live_cv_upload_mutations=[HANDLE], live_cv_storage_writes=[("POST", STORAGE)]))
    assert server.requests == []  # no browser was started
    assert not list(transport.runtime_dir.glob("sessions/*.json"))


def test_B_the_upload_is_refused_once_the_attached_cv_is_no_longer_the_approved_one(tmp_path, runs, as_real_host,  # noqa: F811
                                                                                    monkeypatch):
    from dataclasses import replace
    from jobintel.storage.application_store import ApplicationStore
    s, r, c, server, transport = queued_local(tmp_path)
    original = ApplicationStore.read_applications
    monkeypatch.setattr(ApplicationStore, "read_applications",
                        lambda self: [replace(a, cv_version_id="cv-ffffffffffff") for a in original(self)])
    # Refused before any browser starts: the approval itself no longer matches the application.
    with pytest.raises(ValueError, match="Approval invalidated|no longer the CV attached"):
        s.svc.execute(r["id"], claim_token=c["claim_token"], **live(**UPLOAD))
    assert server.storage == [] and server.requests == []


def test_B_the_upload_is_refused_for_the_browserbase_executor(tmp_path, as_real_host):  # noqa: F811
    from test_browserbase_live_host import FakeTransport
    from test_browserbase_service import ready_result
    s = build(tmp_path / "store", FakeTransport(ready_result), url=URL, answers=BANK)
    r = s.svc.enqueue(approve(s)["id"], executor="browserbase", live_host=HOST)
    c = s.svc.claim(r["id"], worker="runner")
    with pytest.raises(ValueError, match="only available for a local_browser"):
        s.svc.execute(r["id"], claim_token=c["claim_token"], **live(**UPLOAD))
    assert s.transport.calls == 0


def test_cli_offers_the_upload_switch_without_any_path_option():
    import argparse
    from jobintel.submission.cli import add_submission_commands
    parser = argparse.ArgumentParser()
    add_submission_commands(parser.add_subparsers(dest="command"))
    execute = parser._subparsers._group_actions[0].choices["submission-execute"]
    options = {o for a in execute._actions for o in a.option_strings}
    assert {"--live-cv-upload", "--live-cv-upload-operation", "--live-cv-storage-write",
            "--live-cv-association-operation"} <= options
    assert not any(w in o for o in options for w in ("path", "file", "final", "submit"))
