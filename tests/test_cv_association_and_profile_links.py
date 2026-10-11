"""The CV file-association step (ApiSetFormValueToFile) and the confirmed profile links.

A. the association mutation passes only inside the CV-upload window, after this window's handle and
storage upload succeeded, naming that upload; B. outside the window it is fatal; C. handle ->
storage -> association -> visible name = uploaded; D. a failed / foreign / unauthorised association
is blocked or escalated, never uploaded; E/F. LinkedIn and GitHub fields get their own URL;
G. a generic Website/Portfolio field gets GitHub; H/I. never the other profile's URL; J. an
ambiguous profile-link field escalates; K. never a final submit.

Offline: every host is intercepted (see HostedServer); nothing leaves the machine.
"""
from __future__ import annotations

import pytest

from jobintel.submission import browserbase_runner, stop_only
from test_local_cv_upload_and_fields import (  # noqa: F401  (fixtures)
    ANSWERS, GEO, HOST, SET, TO_FILE, browser, bundle_for, escalated, live_run, policy_for, server_page, values,
)

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

LINKEDIN = "https://www.linkedin.com/in/sample-person-000000/"
GITHUB = "https://github.com/sample-person"
LINK_ANSWERS = {**ANSWERS, "linkedin_url": LINKEDIN, "github_url": GITHUB, "website_url": GITHUB}


def kinds(result):
    return [w.get("kind") for w in result.allowed_writes]


# --- A / C: inside the window, after handle + storage, naming this upload --------------------------------

def test_A_C_K_handle_storage_association_then_visible_is_uploaded(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=ok")
    order = [k for k in kinds(result) if k in {"cv_upload_handle", "cv_storage_upload", "cv_file_association"}]
    assert order == ["cv_upload_handle", "cv_storage_upload", "cv_file_association"]
    assert server.operations.count("ApiSetFormValueToFile") == 1
    assert result.cv_upload_status == "uploaded"
    assert result.cv_upload["steps"] == {"upload_handle_created": True, "storage_written": True,
                                         "form_associated": True, "visible_in_page": True, "responses_ok": True}
    assert result.final_submit_clicked is False


# --- B: outside the window -------------------------------------------------------------------------------

def test_B_the_association_outside_the_window_is_fatal(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=assoc_at_load")
    assert result.stop_reason == "unexpected_write_blocked"
    assert result.blocked_requests[0]["graphql_operation_name"] == "ApiSetFormValueToFile"
    assert "ApiSetFormValueToFile" not in server.operations and server.storage == []
    assert result.cv_upload_status != "uploaded"


def test_B_without_the_association_rule_it_is_blocked_like_the_real_run(tmp_path, server_page):  # noqa: F811
    """request:0202e0e7 (2026-10-11): handle + storage passed, the association was blocked."""
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=ok",
                                            policy_changes={"association_mutations": frozenset()})
    assert result.stop_reason == "unexpected_write_blocked"
    assert result.blocked_requests[-1]["graphql_operation_name"] == "ApiSetFormValueToFile"
    steps = result.cv_upload["steps"]
    assert (steps["upload_handle_created"], steps["storage_written"], steps["form_associated"]) == (True, True, False)
    assert result.cv_upload_status == "blocked"


# --- D: a failed or foreign association is never "uploaded" ----------------------------------------------

def test_D_an_association_naming_another_file_is_blocked(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=assoc_wrong_handle")
    refused = result.blocked_requests[-1]
    assert (refused["kind"], refused["reason"], refused["graphql_operation_name"]) == (
        "cv_association_refused", "not_this_upload", "ApiSetFormValueToFile")
    assert result.cv_upload_status == "blocked" and "ApiSetFormValueToFile" not in server.operations


def test_D_an_association_after_a_failed_storage_upload_is_never_sent(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=storage_fail")
    assert "ApiSetFormValueToFile" not in server.operations  # the page itself gave up
    assert result.cv_upload_status == "escalated" and escalated(result)["resume"] == "cv_upload_not_confirmed"


def test_D_an_association_answered_with_errors_is_not_uploaded(tmp_path, server_page):  # noqa: F811
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=assoc_fail")
    assert result.cv_upload_status == "escalated" and escalated(result)["resume"] == "cv_upload_not_confirmed"
    assert result.cv_upload["steps"]["responses_ok"] is False
    assert browserbase_runner.to_stop_only_report(result, bundle, task_id="t")["cv_upload_confirmed"] is False


def test_D_the_association_is_allowed_only_once(tmp_path, server_page, monkeypatch):  # noqa: F811
    flow = [("handle", None), ("storage", None), ("associate", None)]
    assert browserbase_runner._cv_association_problem({"cv_flow": flow}, "{}", None) == "already_associated"
    assert browserbase_runner._cv_association_problem({"cv_flow": [("handle", None)]}, "{}", None) == (
        "upload_not_complete")


@pytest.mark.parametrize("bad", [
    {(TO_FILE[0], "ApiSubmitApplicationFile")},                       # final-action meaning
    {(TO_FILE[0], "ApiSetFormValue")},                                # not a file operation
    {("https://elsewhere.test/api/non-user-graphql", "ApiSetFormValueToFile")},  # another host
    {TO_FILE, (TO_FILE[0], "ApiSetFormValueToFileTwo")},              # more than one
])
def test_D_the_association_rule_itself_is_exact(tmp_path, bad):
    bundle = bundle_for(tmp_path)
    with pytest.raises(ValueError):
        browserbase_runner.validate_cv_upload_policy(bundle, policy_for(bundle, association_mutations=frozenset(bad)))


# --- E-J: profile links ----------------------------------------------------------------------------------

@pytest.mark.parametrize("label,field", [
    ("LinkedIn", "linkedin_url"), ("LinkedIn Profile", "linkedin_url"), ("LinkedIn Profile URL", "linkedin_url"),
    ("GitHub", "github_url"), ("GitHub Profile", "github_url"),
    ("Website", "website_url"), ("Portfolio", "website_url"), ("Personal Website", "website_url"),
    ("Portfolio / Website", "website_url"), ("Other URL", "website_url"),
])
def test_E_F_G_exact_profile_link_labels(label, field):
    assert stop_only.label_fields(label, deterministic=True) == [field]
    assert stop_only.escalation_reason(label, deterministic=True) is None


@pytest.mark.parametrize("label", ["Profile URL", "LinkedIn or GitHub", "Social media", "Online profile",
                                   "Link to your work", "Twitter URL", "LinkedIn / Portfolio"])
def test_J_ambiguous_profile_link_labels_escalate(label):
    assert stop_only.label_fields(label, deterministic=True) == []
    assert stop_only.escalation_reason(label, deterministic=True) in {"ambiguous_profile_link", "unknown_field"}
    if any(term in label.casefold() for term in stop_only.PROFILE_LINK_TERMS):
        assert stop_only.escalation_reason(label, deterministic=True) == "ambiguous_profile_link"


def test_H_I_explicit_profile_fields_never_take_the_other_url():
    assert "github_url" not in stop_only.label_fields("LinkedIn Profile", deterministic=True)
    assert "website_url" not in stop_only.label_fields("LinkedIn Profile", deterministic=True)
    assert "linkedin_url" not in stop_only.label_fields("GitHub Profile", deterministic=True)
    for label in ("Website", "Portfolio", "Other URL"):
        assert "linkedin_url" not in stop_only.label_fields(label, deterministic=True)


def test_E_to_K_links_on_the_page(tmp_path, server_page):  # noqa: F811
    server, page = server_page
    bundle = bundle_for(tmp_path, "?mode=ok&links=1", answers=LINK_ANSWERS)
    result = browserbase_runner.run(page, bundle, live_host=HOST, graphql_reads=[GEO], graphql_form_writes=[SET],
                                    cv_upload=policy_for(bundle))
    filled = {f["field"]: f["page_label"] for f in result.fields_filled}
    # E / F / G: each field gets exactly its own URL; the generic website field gets GitHub.
    assert values(page, "li", "gh", "web") == {"li": LINKEDIN, "gh": GITHUB, "web": GITHUB}
    assert {"linkedin_url", "github_url", "website_url"} <= set(filled)
    # J: the ambiguous field is escalated and left empty.
    assert escalated(result)["profile url"] == "ambiguous_profile_link" and values(page, "amb") == {"amb": ""}
    # K
    assert result.final_submit_clicked is False


def test_H_a_linkedin_field_stays_empty_without_a_linkedin_answer(tmp_path, server_page):  # noqa: F811
    server, page = server_page
    answers = {k: v for k, v in LINK_ANSWERS.items() if k != "linkedin_url"}
    bundle = bundle_for(tmp_path, "?mode=ok&links=1", answers=answers)
    result = browserbase_runner.run(page, bundle, live_host=HOST, graphql_reads=[GEO], graphql_form_writes=[SET],
                                    cv_upload=policy_for(bundle))
    assert values(page, "li") == {"li": ""}  # GitHub is never substituted into the LinkedIn field
    assert {"label": "linkedin profile", "reason": "no_approved_answer"} in browserbase_runner.form_diagnostics(
        result)["fields_skipped"]
