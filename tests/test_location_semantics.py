"""Location-family field semantics: country vs city vs general current location (offline).

A control whose primary label is a location / city / country wording asks for exactly one approved
fact. Its OWN helper text (description, aria-describedby, the field's own container) may narrow an
ambiguous label such as "Location"; precedence is country-only > city-only > general location.
Section instructions outside the field's entry never count. A country selector only ever receives
the approved country, and no / several exact suggestions escalate.

Synthetic facts only ("Sampleton"); the real values come from the private authoritative profile.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jobintel.models.submission import AnswerSnapshot
from jobintel.submission import browserbase_runner, stop_only

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

FACTS = {"location": "Sampleton, United Kingdom", "city": "Sampleton", "country": "United Kingdom"}
PACKAGE = SimpleNamespace(answers=tuple(AnswerSnapshot(f, v, "synthetic", "v1") for f, v in FACTS.items()))
COUNTRY_HELPER = "Country you’re currently residing in"


def resolve(label, helper=""):
    control = {"label": label, "helper": helper}
    semantic = browserbase_runner._semantic_label(control)
    return browserbase_runner._resolve(PACKAGE, browserbase_runner.normalise_label(semantic))


# --- A-H: the semantic resolver (pure) ---------------------------------------------------------------

@pytest.mark.parametrize("label,helper,expected", [
    ("Location", COUNTRY_HELPER, "United Kingdom"),                      # A (the observed form)
    ("Location *", "Country you're currently residing in", "United Kingdom"),
    ("Country", "", "United Kingdom"),                                   # B
    ("Country of residence", "", "United Kingdom"),                      # C
    ("Current country", "", "United Kingdom"),
    ("Country where you currently reside", "", "United Kingdom"),
    ("Country of current residence", "", "United Kingdom"),
    ("Country you're currently residing in", "", "United Kingdom"),
    ("City", "", "Sampleton"),                                           # D
    ("Current city", "", "Sampleton"),
    ("City of residence", "", "Sampleton"),
    ("Which city are you based in?", "", "Sampleton"),
    ("Where are you currently based?", "", "Sampleton, United Kingdom"),  # E
    ("Location", "", "Sampleton, United Kingdom"),                       # F
    ("Current location", "", "Sampleton, United Kingdom"),
    ("Where do you currently live?", "", "Sampleton, United Kingdom"),
    ("Location", "Current city", "Sampleton"),                           # G
    ("Where are you currently based?", "Country of residence", "United Kingdom"),  # H
    ("Location", "Please enter the country you are currently living in.", "United Kingdom"),
    ("Location", "Which city do you live in? Your country of residence also helps.", "United Kingdom"),  # country > city
    ("Location", "We are a remote-first company.", "Sampleton, United Kingdom"),  # no qualifier: general
    ("Country", "Country of residence", "United Kingdom"),               # agreeing helper
])
def test_semantic_resolution(label, helper, expected):
    assert resolve(label, helper) == (expected, None)


def test_H_country_helper_overrides_an_ambiguous_location_label():
    assert stop_only.location_field("Location") == "location"
    assert stop_only.location_field("Location", COUNTRY_HELPER) == "country"
    semantic = stop_only.semantic_label("Location", COUNTRY_HELPER)
    assert semantic == "Location || " + COUNTRY_HELPER
    assert stop_only.label_fields(semantic, deterministic=True) == ["country"]
    assert stop_only.escalation_reason(semantic, deterministic=True) is None


def test_the_label_is_unchanged_when_the_helper_does_not_change_the_field():
    assert stop_only.semantic_label("Location", "") == "Location"
    assert stop_only.semantic_label("Location", "Start typing to search") == "Location"
    assert stop_only.semantic_label("Country", "Country of residence") == "Country"


@pytest.mark.parametrize("label,helper", [("City", COUNTRY_HELPER), ("Country", "Current city")])
def test_a_helper_contradicting_an_explicit_label_escalates(label, helper):
    assert resolve(label, helper) == (None, stop_only.CONFLICTING_LOCATION)


@pytest.mark.parametrize("label", ["Employment 1 country", "Driving licence country", "Country of nationality",
                                   "Nationality", "Country of citizenship"])
def test_helper_text_never_affects_a_non_location_field(label):
    assert stop_only.semantic_label(label, COUNTRY_HELPER) == label


def test_without_an_approved_fact_the_field_is_not_answered():
    empty = SimpleNamespace(answers=(AnswerSnapshot("location", "Sampleton, United Kingdom", "synthetic", "v1"),))
    semantic = stop_only.semantic_label("Location", COUNTRY_HELPER)
    assert browserbase_runner._resolve(empty, browserbase_runner.normalise_label(semantic)) == (
        None, "no_approved_answer")  # never falls back to "<city>, <country>"


def test_other_executors_never_see_helper_semantics():
    semantic = stop_only.semantic_label("Location", COUNTRY_HELPER)
    assert stop_only.label_fields(semantic) == []  # AirTap & co: exact labels only, so it escalates
    assert stop_only.label_fields("Location") == ["location"]


# --- I, J, K: the real runner against the local hosted form (headless Chrome, no network) -----------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser  # noqa: E402,F401  (fixture)
from test_local_cv_upload_and_fields import (  # noqa: E402,F401  (fixtures and helpers)
    ANSWERS, GEO, HOST, SET, bundle_for, escalated, policy_for, server_page, values)

PLACE_ANSWERS = {**ANSWERS, "city": "Sampleton", "country": "United Kingdom"}


def place_run(tmp_path, server_page, query, answers=PLACE_ANSWERS):  # noqa: F811
    server, page = server_page
    bundle = bundle_for(tmp_path, query, answers)
    typed = []  # every suggestion READ's search text
    page.on("request", lambda r: typed.append(json.loads(r.post_data)["variables"]["q"])
            if r.method == "POST" and r.post_data and '"ApiAutocompleteGeoLocation"' in r.post_data else None)
    result = browserbase_runner.run(page, bundle, live_host=HOST, graphql_reads=[GEO], graphql_form_writes=[SET],
                                    cv_upload=policy_for(bundle))
    assert result.final_submit_clicked is False and server.external == []
    assert "ApiSubmitApplication" not in server.operations
    assert page.evaluate("() => document.body.dataset.submitted || '0'") == "0"
    result.typed_queries = typed
    return server, page, bundle, result


def filled(result, field):
    return [f for f in result.fields_filled if f["field"] == field]


@pytest.mark.parametrize("help_mode", ["country", "aria"])
def test_A_a_location_field_whose_helper_asks_for_the_country_gets_the_country(tmp_path, server_page, help_mode):  # noqa: F811
    server, page, bundle, result = place_run(tmp_path, server_page, f"?mode=ok&loc=countries&help={help_mode}")
    assert values(page, "loc")["loc"] == "United Kingdom"
    assert [f["value"] for f in filled(result, "country")] == ["United Kingdom"]
    assert filled(result, "location") == []
    assert "location" not in escalated(result)
    # The service re-checks every filled answer against the approved package at import time.
    report = browserbase_runner.to_stop_only_report(result, bundle, task_id="t")
    package = SimpleNamespace(answers=bundle.answers)
    for f in report["fields_filled"]:
        assert stop_only.question(package, f["field"], f["page_label"], "result", "result",
                                  deterministic=True) == {"value": f["value"]}
    assert report["final_submit_clicked"] is False


def test_G_a_location_field_whose_helper_asks_for_the_city_gets_the_city(tmp_path, server_page):  # noqa: F811
    _, page, _, result = place_run(tmp_path, server_page, "?mode=ok&loc=cities&help=city")
    assert values(page, "loc")["loc"] == "Sampleton"
    assert [f["value"] for f in filled(result, "city")] == ["Sampleton"]


def test_F_J_a_plain_location_field_keeps_city_and_country(tmp_path, server_page):  # noqa: F811
    _, page, _, result = place_run(tmp_path, server_page, "?mode=ok")
    assert values(page, "loc")["loc"] == "Sampleton, England, United Kingdom"
    assert [(f["value"], f["page_label"]) for f in filled(result, "location")] == [("Sampleton, United Kingdom", "Location")]
    assert filled(result, "country") == [] and filled(result, "city") == []


def test_section_instructions_outside_the_field_never_narrow_it(tmp_path, server_page):  # noqa: F811
    _, page, _, result = place_run(tmp_path, server_page, "?mode=ok&help=section")
    assert values(page, "loc")["loc"] == "Sampleton, England, United Kingdom"
    assert [f["value"] for f in filled(result, "location")] == ["Sampleton, United Kingdom"]


@pytest.mark.parametrize("loc,reason", [("country_none", "country_no_exact_option"),
                                        ("country_dup", "country_ambiguous_option")])
def test_I_a_country_selector_without_one_exact_match_escalates(tmp_path, server_page, loc, reason):  # noqa: F811
    _, page, _, result = place_run(tmp_path, server_page, f"?mode=ok&loc={loc}&help=country")
    assert escalated(result)["location"] == reason
    assert filled(result, "country") == [] and filled(result, "location") == []
    assert page.eval_on_selector("#loc", "el => el.getAttribute('aria-expanded')") in {"true", "false"}
    assert result.status == "needs_human_input"


def test_I_the_city_value_is_never_typed_into_a_country_selector(tmp_path, server_page):  # noqa: F811
    _, page, _, result = place_run(tmp_path, server_page, "?mode=ok&loc=countries&help=country")
    assert result.typed_queries and all("Sampleton" not in q for q in result.typed_queries)
    assert values(page, "loc")["loc"] == "United Kingdom"


def test_I_a_country_helper_without_an_approved_country_escalates(tmp_path, server_page):  # noqa: F811
    _, page, _, result = place_run(tmp_path, server_page, "?mode=ok&loc=countries&help=country", answers=ANSWERS)
    # Never falls back to "<city>, <country>": the (optional) field is left for the human.
    assert {"label": "Location", "reason": "no_approved_answer"} in result.fields_skipped
    assert result.fields_filled and filled(result, "location") == [] and values(page, "loc")["loc"] == ""
