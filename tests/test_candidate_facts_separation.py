"""Candidate facts are data, not code: the public rules resolve from supplied facts and fail closed
without them. Synthetic candidates only; the real candidate's facts live in the git-ignored
`jobintel.submission.candidate_facts_local` module and are never read here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jobintel.submission import candidate_facts
from jobintel.submission.authoritative_answers import AuthoritativeAnswerBank
from jobintel.submission.question_resolution import AuthoritativeWorkAuthFacts, resolve_application_question

UNKNOWN = AuthoritativeWorkAuthFacts()
STUDENT = AuthoritativeWorkAuthFacts(
    current_uk_visa_status="Sample visa", uk_visa_expiry="2030-01-01", has_current_legal_right_to_work_uk=True,
    may_require_skilled_worker_sponsorship_future=True, has_permanent_or_unrestricted_right_to_work=False)
ANSWERS = {
    "address.full_address": "1 Example Street, Exampletown, EX1 1AA, United Kingdom",
    "address.postcode": "EX1 1AA",
    "identity.legal_first_name": "Alex",
    "identity.email": "alex@example.com",
    "identity.phone_uk": "07000000000",
    "identity.nationality": "Examplish",
    "criminal.direct_convictions_answer": "No",
    "disability.disabled_answer": "No",
    "driving.full_uk_driving_licence_answer": "No",
    "education.ucl.institution": "Example University",
}

CANDIDATE_SPECIFIC = [
    "Do you currently have the legal right to work in the UK?",
    "Will you now or in the future require sponsorship?",
    "Do you have permanent or unrestricted right to work in the UK?",
    "Do you hold a full UK driving licence?",
    "Do you hold a driving licence?",
    "Do you have any criminal convictions?",
    "Do you currently hold UK security clearance?",
    "Do you consider yourself to have a disability?",
    "Do you require any reasonable adjustments?",
    "What is your current address?",
    "Postcode",
    "What is your notice period?",
    "Nationality",
    "First name",
    "Last name",
    "Full name",
    "Email",
    "Phone number",
    "Undergraduate institution",
    "Postgraduate degree",
]


@pytest.mark.parametrize("question", CANDIDATE_SPECIFIC)
def test_without_candidate_facts_every_candidate_specific_rule_fails_closed(question):
    result = resolve_application_question(question, facts=UNKNOWN, answers={})
    assert (result.resolved, result.answer_value, result.requires_human_input) == (False, None, True)
    assert result.rule_applied == "candidate_fact_unknown_fail_closed"


@pytest.mark.parametrize("question,expected", [
    ("What is your current address?", "1 Example Street, Exampletown, EX1 1AA, United Kingdom"),
    ("Postcode", "EX1 1AA"),
    ("First name", "Alex"),
    ("Email", "alex@example.com"),
    ("Phone number", "07000000000"),
    ("Nationality", "Examplish"),
    ("Do you have any criminal convictions?", "No"),
    ("Do you consider yourself to have a disability?", "No"),
    ("Do you hold a full UK driving licence?", "No"),
    ("Postgraduate institution", "Example University"),
])
def test_supplied_answers_resolve_verbatim(question, expected):
    result = resolve_application_question(question, facts=STUDENT, answers=ANSWERS)
    assert (result.resolved, result.answer_value, result.requires_human_input) == (True, expected, False)


def test_work_authorisation_rules_come_from_the_facts():
    rtw = resolve_application_question("Do you currently have the legal right to work in the UK?", facts=STUDENT, answers={})
    sponsor = resolve_application_question("Will you now or in the future require sponsorship?", facts=STUDENT, answers={})
    permanent = resolve_application_question("Do you have permanent or unrestricted right to work in the UK?",
                                             facts=STUDENT, answers={})
    assert (rtw.answer_value, sponsor.answer_value, permanent.answer_value) == ("Yes", "Yes", "No")
    assert "Sample visa" in rtw.provenance and "2030-01-01" in permanent.provenance


def test_a_missing_or_contrary_fact_is_never_turned_into_an_answer():
    """Permanent status is never inferred as Yes, and a sponsorship need is never turned into No."""
    settled = AuthoritativeWorkAuthFacts(has_permanent_or_unrestricted_right_to_work=True,
                                         may_require_skilled_worker_sponsorship_future=False)
    for question in ("Do you have permanent or unrestricted right to work in the UK?",
                     "Will you now or in the future require sponsorship?"):
        result = resolve_application_question(question, facts=settled, answers={})
        assert result.resolved is False and result.requires_human_input is True


@pytest.mark.parametrize("question,resolved", [
    ("Do you have any unspent convictions, cautions or pending proceedings?", False),  # human review
    ("Are you eligible for security clearance?", False),                              # JD + human review
    ("What is your ethnicity?", True),                                                 # "Prefer not to say"
])
def test_policy_rules_do_not_depend_on_candidate_facts(question, resolved):
    result = resolve_application_question(question, facts=UNKNOWN, answers={})
    assert result.resolved is resolved and result.rule_applied != "candidate_fact_unknown_fail_closed"


def test_without_the_local_module_the_bank_holds_policies_only(monkeypatch, tmp_path):
    monkeypatch.setattr(candidate_facts, "_local", None)
    assert candidate_facts.candidate_answer_records() == [] and candidate_facts.question_facts() == {}
    assert not candidate_facts.local_facts_available()
    bank = AuthoritativeAnswerBank(tmp_path)
    assert {r.field_id for r in bank.all_records()} == {"policy.free_text", "policy.final_submission"}


def test_the_local_facts_module_is_git_ignored():
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "src/jobintel/submission/candidate_facts_local.py" in ignore
