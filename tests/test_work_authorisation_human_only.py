"""Work authorisation / immigration questions are human-only for every executor (2026-10-13 policy).

Right to work, permanent/unrestricted right, visa status/expiry, sponsorship now/future, basis of
work eligibility and immigration status are never answered automatically, whatever the approved
package holds; a form question about them never by itself makes a job ineligible.
"""
from __future__ import annotations

import pytest

from jobintel.submission import stop_only


@pytest.mark.parametrize("label", [
    "Do you have the right to work in the UK?",
    "Do you currently have the right to work in the UK?",
    "Do you have the permanent or unrestricted right to work in the UK?",
    "Will you now or in the future require visa sponsorship?",
    "Visa status",
    "Visa expiry",
    "Immigration status",
    "Indefinite leave to remain",
    "Settled status",
    "Permitted to work in the UK",
    "Are you eligible to work in the UK?",
    "Work Eligibility",
    "Please state on what grounds you have the legal right to work permanently in the UK",
    "Are you authorised to work in the UK?",
    "Work authorization status",
    "Are you legally entitled to work in the UK?",
    "Can you legally work in the UK?",
])
@pytest.mark.parametrize("deterministic", [True, False])
def test_every_work_authorisation_question_escalates(label, deterministic):
    reason = stop_only.escalation_reason(label, deterministic=deterministic)
    assert reason is not None and reason.startswith(("sensitive:", stop_only.HUMAN_ONLY_WORK_AUTHORISATION))


@pytest.mark.parametrize("label", ["First name", "Email", "Phone"])
def test_ordinary_contact_fields_are_not_affected(label):
    assert stop_only.escalation_reason(label, deterministic=True) is None
