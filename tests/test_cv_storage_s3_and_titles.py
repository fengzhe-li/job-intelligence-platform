"""The observed hosted-ATS S3 storage write, the CV-upload state machine, and radio question titles.

A. the exact observed S3 POST passes only inside the CV-upload window; B. the same POST outside it
is fatal; C. another S3/storage host is fatal; D. another method is fatal; E. handle + storage +
form association -> uploaded; F. a visible file name with a blocked storage write -> blocked;
G. a visible file name alone never counts as uploaded; H. an Ashby-style radio group gets its own
question title, not the section instructions before it; I/J/K. the deterministic wording rules
apply to that title; L. the five open questions stay human-only; M. never a final submit.

Offline: every host is intercepted (see HostedServer); nothing leaves the machine.
"""
from __future__ import annotations

import json

import pytest

from jobintel.submission import browserbase_runner
from test_local_cv_upload_and_fields import (  # noqa: F401  (fixtures)
    CV_BYTES, OPEN_QUESTIONS, S3, browser, escalated, live_run, server_page, values,
)

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

S3_POLICY = {"storage_writes": frozenset({("POST", S3)})}


def s3_run(tmp_path, server_page, query, **kwargs):  # noqa: F811
    return live_run(tmp_path, server_page, query, policy_changes=S3_POLICY, **kwargs)


def form(result):
    return browserbase_runner.form_diagnostics(result)


# --- A / E: the exact observed S3 write, inside the window, completes the approved flow -------------------

def test_A_E_M_the_exact_s3_post_completes_the_upload_inside_the_window(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=ok&storage=s3")
    assert [(m, h) for m, h, _ in server.storage] == [("POST", S3)]
    assert CV_BYTES in server.storage[0][2]  # the exact sealed CV bytes, nothing else
    storage = [w for w in result.allowed_writes if w.get("kind") == "cv_storage_upload"]
    assert [(w["method"], w["host"], w["path"], w["resource_type"], w["field"]) for w in storage] == [
        ("POST", S3, "/", "fetch", "cv")]
    assert result.cv_upload_status == "uploaded"
    steps = result.cv_upload["steps"]
    assert steps == {"upload_handle_created": True, "storage_written": True, "form_associated": True,
                     "visible_in_page": True, "responses_ok": True}
    assert (form(result)["cv_upload_status"], form(result)["cv_upload_visible_in_page"]) == ("uploaded", True)
    report = browserbase_runner.to_stop_only_report(result, bundle, task_id="t")
    assert report["cv_upload_confirmed"] is True and report["final_submit_clicked"] is False


# --- B / C / D: everything else stays fatal --------------------------------------------------------------

def test_B_the_same_s3_post_outside_the_window_is_fatal(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=storage_at_load&storage=s3")
    assert result.stop_reason == "unexpected_write_blocked"
    first = result.blocked_requests[0]
    assert (first["method"], first["host"], first["path"]) == ("POST", S3, "/")
    assert server.storage == [] and result.cv_upload_status != "uploaded"


def test_C_another_storage_host_is_fatal_even_with_the_s3_rule(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=unknown_storage&storage=s3")
    assert result.stop_reason == "unexpected_write_blocked"
    assert result.blocked_requests[-1]["host"] == "other-storage.test" and server.storage == []
    assert result.cv_upload_status == "blocked"


def test_C_another_s3_host_cannot_be_reached_by_suffix(tmp_path, server_page):  # noqa: F811
    other = "ashbyhq-infra-prd-main-app-uploaded-files-us-west-2.s3.us-west-2.amazonaws.com"
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=ok&storage=s3",
                                            policy_changes={"storage_writes": frozenset({("POST", other)})})
    assert result.stop_reason == "unexpected_write_blocked" and server.storage == []
    assert result.blocked_requests[-1]["host"] == S3 and result.cv_upload_status == "blocked"


def test_D_another_method_to_the_same_host_is_fatal(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=storage_put&storage=s3")
    assert result.stop_reason == "unexpected_write_blocked" and server.storage == []
    assert (result.blocked_requests[-1]["method"], result.blocked_requests[-1]["host"]) == ("PUT", S3)


def test_a_storage_body_that_is_not_the_sealed_cv_is_fatal(tmp_path, server_page, monkeypatch):  # noqa: F811
    monkeypatch.setattr(browserbase_runner, "_post_body_bytes", lambda request: b"--boundary some other file--")
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=ok&storage=s3")
    assert result.blocked_requests[-1]["kind"] == "cv_storage_body_mismatch" and server.storage == []
    assert result.cv_upload_status == "blocked"


# --- F / G: a visible file name is never proof of an upload ----------------------------------------------

def test_F_name_visible_but_storage_blocked_reports_blocked(tmp_path, server_page):  # noqa: F811
    """The real 2026-10-11 run: the page shows the name on selection; the storage write is blocked."""
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=show_first&storage=s3",
                                            policy_changes={"storage_writes": frozenset()})
    assert result.stop_reason == "unexpected_write_blocked" and server.storage == []
    diagnostics = form(result)
    assert (diagnostics["cv_upload_status"], diagnostics["cv_upload_visible_in_page"]) == ("blocked", True)
    assert result.cv_upload["steps"]["upload_handle_created"] is True
    assert result.cv_upload["steps"]["storage_written"] is False
    report = browserbase_runner.to_stop_only_report(result, bundle, task_id="t")
    assert report["cv_upload_confirmed"] is False and report["final_submit_clicked"] is False


def test_G_a_visible_name_without_any_upload_is_not_uploaded(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=visible_only&storage=s3")
    assert page.inner_text("#resume-name") == "Sam_Sample_CV.pdf"
    assert result.cv_upload_status == "escalated" and escalated(result)["resume"] == "cv_upload_not_confirmed"
    assert result.cv_upload["visible_in_page"] is True and result.cv_upload["steps"]["upload_handle_created"] is False
    assert browserbase_runner.to_stop_only_report(result, bundle, task_id="t")["cv_upload_confirmed"] is False


def test_G_a_failed_storage_answer_is_not_uploaded_even_if_shown(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=storage_fail&storage=s3")
    assert result.cv_upload_status == "escalated" and escalated(result)["resume"] == "cv_upload_not_confirmed"
    assert result.cv_upload["steps"]["form_associated"] is False


def test_status_moves_through_selected_and_upload_handle_created(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=ok&storage=s3",
                                            policy_changes={"storage_writes": frozenset()})
    assert result.cv_upload_status == "blocked"  # stopped after the handle, at the storage write
    assert result.cv_upload["steps"]["upload_handle_created"] is True


# --- H / I / J / K: radio question titles, then the deterministic rules ----------------------------------

def test_H_I_ashby_style_radio_gets_its_own_title_and_sponsorship_stays_human_only(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=ok&storage=s3&layout=ashby")
    labels = {c["label"] for c in form(result)["controls_detected"] if c["kind"] == "radio"}
    assert "will you now or in the future require visa sponsorship to work in the uk?" in labels
    assert not any("please answer each question" in label for label in labels)
    assert page.eval_on_selector_all("input[name=spons-a]:checked", "els => els.length") == 0
    assert "require_sponsorship_now_or_future" not in {f["field"] for f in form(result)["fields_filled"]}
    reasons = [q["screenshot_or_evidence"] for q in result.questions_escalated]
    assert "runner:human_only:work_authorisation:require_sponsorship_now_or_future" in reasons


def test_H_I_ashby_style_future_wording_variant(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=ok&storage=s3&layout=ashby&spons=future")
    assert page.eval_on_selector_all("input[name=spons-a]:checked", "els => els.length") == 0


def test_H_J_aria_labelledby_radio_group_and_permanent_stays_human_only(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=ok&storage=s3&layout=aria&rtw=perm")
    labels = {c["label"] for c in form(result)["controls_detected"] if c["kind"] == "radio"}
    assert "do you have the permanent or unrestricted right to work in the uk?" in labels
    assert page.eval_on_selector_all("input[name=rtw-a]:checked", "els => els.length") == 0
    assert "permanent_or_unrestricted_right_to_work" not in {f["field"] for f in result.fields_filled}


@pytest.mark.parametrize("query,question", [
    ("layout=aria&rtw=ambiguous", "do you have the right to work in the uk"),
    ("layout=ashby&spons=ambiguous", "what is your visa sponsorship situation"),
])
def test_K_ambiguous_wording_with_a_resolved_title_still_escalates(tmp_path, server_page, query, question):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, f"?mode=ok&storage=s3&{query}")
    assert escalated(result)[question].startswith("sensitive:")
    assert page.eval_on_selector_all("input[name=rtw-a]:checked, input[name=spons-a]:checked",
                                     "els => els.length") == 0


def test_options_keep_their_own_labels_when_the_title_points_at_the_first_option(tmp_path, server_page):  # noqa: F811
    server, page = server_page
    s3_run(tmp_path, server_page, "?layout=ashby&mode=visible_only")
    snapshot = page.main_frame.evaluate(browserbase_runner.COLLECT_SCRIPT)
    group = next(c for c in snapshot["controls"] if c["kind"] == "radio" and "sponsorship" in c["label"])
    assert [o["text"] for o in group["options"]] == ["Yes", "No"]


# --- L / M ------------------------------------------------------------------------------------------------

def test_L_M_open_questions_and_linkedin_stay_with_the_human(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = s3_run(tmp_path, server_page, "?mode=ok&storage=s3&layout=ashby")
    reasons = escalated(result)
    from jobintel.submission import stop_only
    assert all(reasons.get(stop_only.normalise_question(q)) == "unknown_field" for q in OPEN_QUESTIONS)
    assert set(values(page, "q1", "q2", "q3", "q4", "q5", "li").values()) == {""}
    assert result.final_submit_clicked is False
    assert "secret" not in json.dumps(form(result)).casefold()


def test_the_run_helper_names_exactly_the_observed_s3_write():
    import importlib.util
    from pathlib import Path
    helper_path = Path(__file__).resolve().parents[1] / "scratchpad" / "local_profile_helper.py"
    if not helper_path.exists():
        pytest.skip("local operational helper not present")
    spec = importlib.util.spec_from_file_location("local_profile_helper_s3_under_test", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    assert helper.CV_STORAGE_WRITES == (("POST", S3),)
    assert helper.CV_UPLOAD_OPERATIONS == ("ApiCreateFileUploadHandle",)
    policy = browserbase_runner.CvUploadPolicy(
        "cv-0123456789ab", "a" * 64, "Sam_Sample_CV.pdf",
        frozenset({(f"https://{helper.HOST}/api/non-user-graphql", "ApiCreateFileUploadHandle")}),
        frozenset(helper.CV_STORAGE_WRITES))
    checked = browserbase_runner.validate_cv_upload_for(
        f"https://{helper.HOST}/employer/x/application", "cv-0123456789ab", "a" * 64, policy)
    assert checked.storage_writes == frozenset({("POST", S3)})
