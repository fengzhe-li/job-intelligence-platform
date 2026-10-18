"""Generic fixes from a real hosted-ATS run (synthetic values only, offline):

* named-profile link labels ("Link to your Github profile") resolve to that profile's own URL only;
* a country combobox that names no listbox (no aria-controls / aria-owns) is filled only from the
  option list this interaction opened, with exactly one exact option;
* the ATS upload file name follows the review copy's <Name>_<Company>_<Role>_CV.pdf naming, without
  touching the sealed bytes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobintel.matching.cv_review_export import ATS_UPLOAD_FILENAME_MAX, ats_upload_filename, review_filename
from jobintel.submission import browserbase_runner, stop_only

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

GITHUB, LINKEDIN = "https://github.com/alex-example", "https://www.linkedin.com/in/alex-example/"
PACKAGE = SimpleNamespace(answers=(SimpleNamespace(field="github_url", value=GITHUB),
                                   SimpleNamespace(field="linkedin_url", value=LINKEDIN),
                                   SimpleNamespace(field="website_url", value=GITHUB)))


def answer(label):
    fields = stop_only.label_fields(label, deterministic=True)
    if stop_only.escalation_reason(label, deterministic=True) is not None or len(fields) != 1:
        return None
    return stop_only.question(PACKAGE, fields[0], label, "", "", deterministic=True).get("value")


# --- 1. profile links -------------------------------------------------------------------------------

GITHUB_LABELS = ["Link to your Github profile", "Link to your GitHub profile", "Your GitHub profile", "GitHub profile",
                 "GitHub URL", "link to your github profile *", "Please provide your GitHub profile URL",
                 "URL for your GitHub profile", "GitHub Profile:", "Link to your GitHub profile?"]
LINKEDIN_LABELS = ["Link to your LinkedIn profile", "Your LinkedIn profile", "LinkedIn profile", "LinkedIn URL",
                   "Please provide your LinkedIn profile", "URL for your LinkedIn profile"]


@pytest.mark.parametrize("label", GITHUB_LABELS)
def test_A_C_github_labels_get_the_github_url_only(label):
    assert stop_only.profile_link_field(label) == "github_url"
    assert stop_only.label_fields(label, deterministic=True) == ["github_url"]
    assert answer(label) == GITHUB != LINKEDIN
    # C: a GitHub field never takes the LinkedIn answer, even when asked for explicitly.
    assert stop_only.question(PACKAGE, "linkedin_url", label, "", "", deterministic=True).get("value") is None


@pytest.mark.parametrize("label", LINKEDIN_LABELS)
def test_B_D_linkedin_labels_get_the_linkedin_url_only(label):
    assert stop_only.profile_link_field(label) == "linkedin_url"
    assert stop_only.label_fields(label, deterministic=True) == ["linkedin_url"]
    assert answer(label) == LINKEDIN
    # D: a LinkedIn field never takes the GitHub answer.
    assert stop_only.question(PACKAGE, "github_url", label, "", "", deterministic=True).get("value") is None


def test_the_exact_real_labels():
    assert answer("Link to your Github profile") == GITHUB
    assert answer("Link to your LinkedIn profile") == LINKEDIN


@pytest.mark.parametrize("label", ["Profile URL", "Social profile", "LinkedIn or GitHub", "Professional profile",
                                   "GitHub or LinkedIn profile", "Your LinkedIn / GitHub", "Link to your profile",
                                   "LinkedIn GitHub", "Link to your work", "Link to your Linked-In profile",
                                   "GitHub repository for this project", "Your GitHub username"])
def test_E_ambiguous_profile_labels_still_escalate(label):
    assert stop_only.profile_link_field(label) is None
    assert stop_only.label_fields(label, deterministic=True) == []
    assert answer(label) is None
    reason = stop_only.escalation_reason(label, deterministic=True)
    assert reason in {"ambiguous_profile_link", "unknown_field"}
    if label in {"Profile URL", "Social profile", "LinkedIn or GitHub", "Professional profile"}:
        assert reason == "ambiguous_profile_link"  # escalated even when optional, never skipped


# --- 2. a country combobox without a listbox reference (real runner, local hosted form) ---------------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser  # noqa: E402,F401  (fixture)
from test_local_cv_upload_and_fields import escalated, server_page, values  # noqa: E402,F401
from test_location_semantics import filled, place_run  # noqa: E402


def test_F_a_country_combobox_without_a_listbox_reference_selects_the_one_exact_option(tmp_path, server_page):  # noqa: F811
    _, page, _, result = place_run(tmp_path, server_page, "?mode=ok&loc=countries&help=country&ref=none")
    assert page.eval_on_selector("#loc", "el => el.hasAttribute('aria-controls') || el.hasAttribute('aria-owns')") is False
    assert values(page, "loc")["loc"] == "United Kingdom"
    assert [f["value"] for f in filled(result, "country")] == ["United Kingdom"]
    assert "location" not in escalated(result)
    assert result.typed_queries and all("Sampleton" not in q for q in result.typed_queries)
    assert result.final_submit_clicked is False


def test_F_an_option_list_open_before_the_interaction_is_never_read(tmp_path, server_page):  # noqa: F811
    _, page, _, result = place_run(tmp_path, server_page, "?mode=ok&loc=countries&help=country&ref=none_decoy")
    assert values(page, "loc")["loc"] == "United Kingdom"
    assert page.evaluate("() => document.body.dataset.decoyClicked || '0'") == "0"


@pytest.mark.parametrize("loc,ref,reason", [
    ("country_none", "none", "country_no_exact_option"),            # G: no result
    ("country_near", "none", "country_no_exact_option"),            # G: only near misses (Kingdom of ..., United States)
    ("country_dup", "none", "country_ambiguous_option"),            # H: duplicate exact options
    ("countries", "none_two", "country_unclear_listbox_ownership"),  # H: two new lists -- whose option?
])
def test_G_H_no_single_owned_exact_country_escalates(tmp_path, server_page, loc, ref, reason):  # noqa: F811
    _, page, _, result = place_run(tmp_path, server_page, f"?mode=ok&loc={loc}&help=country&ref={ref}")
    assert escalated(result)["location"] == reason
    assert filled(result, "country") == [] and filled(result, "location") == []
    assert values(page, "loc")["loc"] in {"United Kingdom", ""}  # typed for the READ, never a chosen option
    assert result.status == "needs_human_input" and result.final_submit_clicked is False


def test_I_a_plain_location_field_with_a_listbox_reference_is_unchanged(tmp_path, server_page):  # noqa: F811
    _, page, _, result = place_run(tmp_path, server_page, "?mode=ok")
    assert values(page, "loc")["loc"] == "Sampleton, England, United Kingdom"
    assert [(f["value"], f["page_label"]) for f in filled(result, "location")] == [("Sampleton, United Kingdom", "Location")]
    assert filled(result, "country") == [] and filled(result, "city") == []


def test_E_precedence_is_unchanged():
    assert stop_only.location_field("Location", "Country you’re currently residing in") == "country"
    assert stop_only.location_field("Location", "Current city") == "city"
    assert stop_only.location_field("Location", "") == "location"


# --- 3. ATS upload file name -------------------------------------------------------------------------

def test_J_the_ats_upload_name_is_the_review_copy_naming():
    role = "Forward Deployed Engineer - Software Engineer - United Kingdom"
    name = ats_upload_filename("Alex Example", "elevenlabs", role)  # registry slug -> display name
    assert name == "Alex_Example_ElevenLabs_Forward_Deployed_Engineer_CV.pdf"
    assert name == review_filename("Alex Example", "ElevenLabs", role)  # one naming rule, not two
    assert name.count(".pdf") == 1 and ats_upload_filename("Alex Example", "elevenlabs", role) == name
    assert ats_upload_filename("Alex Example", "Example Co", "Engineer.pdf") == "Alex_Example_Example_Co_Engineer_CV.pdf"


def test_J_a_long_company_and_role_are_capped_on_word_boundaries():
    name = ats_upload_filename("Alex Example", "Very Long Company Name Holdings International Group",
                               "Senior Staff Principal Platform Infrastructure Reliability Engineer")
    assert len(name) <= ATS_UPLOAD_FILENAME_MAX and name.startswith("Alex_Example_Very_Long") and name.endswith("_CV.pdf")
    assert browserbase_runner._CV_FILENAME.fullmatch(name)
    # Without a cap the review copy keeps its full name.
    assert len(review_filename("Alex Example", "Very Long Company Name Holdings International Group",
                               "Senior Staff Principal Platform Infrastructure Reliability Engineer")) > ATS_UPLOAD_FILENAME_MAX


# K (bytes / SHA-256 unchanged by the name) is proven end to end by a real upload in
# test_local_cv_upload_service.py::test_A_C_L_service_uploads_the_approved_cv_and_hands_over_an_unsubmitted_draft.
