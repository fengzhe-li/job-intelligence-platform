"""Excluded occupations name the job itself, not a team or domain its JD mentions (2026-10-13)."""
from __future__ import annotations

import pytest

from jobintel.analysis.occupation_filter import is_excluded_occupation
from jobintel.config import default_ranking_config

CONFIG = default_ranking_config()


@pytest.mark.parametrize("title,description", [
    ("Full-Stack Engineer (Back-End Leaning)", "Collaborate with the Frontend, Growth and Sales teams."),
    ("Frontend Engineer", "Build our marketing site and product UI."),
    ("Software Engineer - Card Payments", "Services for the accounting ledger."),
    ("Embedded Linux Field Engineer", "Support the sales team on customer deployments."),
])
def test_a_technical_title_is_never_excluded_by_its_description(title, description):
    assert is_excluded_occupation(title, description, CONFIG) == (False, None)


@pytest.mark.parametrize("title,description,phrase", [
    ("Sales Executive", "Drive new business.", "sales"),
    ("Graduate Marketing Manager", "Campaigns.", "marketing"),
    ("Graduate Programme", "A rotational scheme in sales and account management.", "sales"),  # non-technical title
    ("Mechanical Engineer", "Design gearboxes.", "mechanical engineer"),
])
def test_an_excluded_occupation_is_still_excluded(title, description, phrase):
    assert is_excluded_occupation(title, description, CONFIG) == (True, phrase)


def test_whole_words_only():
    assert is_excluded_occupation("Wholesales Data Engineer", "", CONFIG) == (False, None)
