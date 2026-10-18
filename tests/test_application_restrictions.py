"""Employer reapplication / frequency restrictions: exact wording, five kinds, never over-inferred."""
from __future__ import annotations

import pytest

from jobintel.analysis.application_restrictions import applies_to, classify


def kinds(text):
    return [r.kind for r in classify(text, source="job_description", job_id="j1", company="ExampleCo")]


@pytest.mark.parametrize("text,kind", [
    ("You have already applied for this position.", "exact_role"),
    ("You can reapply to this role after 90 days.", "exact_role"),
    ("Candidates may submit one application every 6 months.", "company_frequency"),
    ("If you have applied to any role at ExampleCo in the last 90 days, please wait before applying again.",
     "company_frequency"),
    ("Please apply for one role at a time.", "simultaneous_active"),
    ("You may hold a maximum of 3 active applications.", "simultaneous_active"),
    ("We accept only one application per candidate at a time.", "simultaneous_active"),
    ("We encourage you to apply to the roles that best match your skills.", "advisory"),
    ("You can apply again in 90 days.", "ambiguous"),  # names neither this role nor any role: a human decides
])
def test_each_kind_is_classified_from_its_exact_wording(text, kind):
    assert kinds(text) == [kind]


def test_no_restriction_wording_means_none():
    assert kinds("We build voice AI. Python expertise required.") == []


def test_wording_and_source_are_preserved():
    [r] = classify("Hello. Candidates may submit one application every 6 months. Thanks.", source="page_warning",
                   job_id="j1", company="ExampleCo")
    assert r.wording == "Candidates may submit one application every 6 months." and r.source == "page_warning"
    assert r.company_wide is True


def test_an_exact_role_restriction_binds_only_its_own_job_and_is_never_widened():
    [r] = classify("You can reapply to this role after 90 days.", source="page", job_id="j1", company="ExampleCo")
    assert r.company_wide is False
    assert applies_to(r, job_id="j1", company="ExampleCo") is True
    assert applies_to(r, job_id="j2", company="ExampleCo") is False  # another role at the same company


def test_a_company_wide_restriction_binds_every_role_at_that_company_only():
    [r] = classify("One application every 12 months.", source="jd", job_id="j1", company="ExampleCo")
    assert applies_to(r, job_id="j2", company="exampleco") is True
    assert applies_to(r, job_id="j3", company="OtherCo") is False


def test_advisory_and_ambiguous_wording_bind_nothing():
    for text in ("We encourage you to apply to the roles that best match.", "You can apply again in 90 days."):
        [r] = classify(text, source="jd", job_id="j1", company="ExampleCo")
        assert applies_to(r, job_id="j1", company="ExampleCo") is False
