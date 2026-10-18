"""The AirTap executor/handoff layer is not a candidate-profile store.

Candidate answers come only from the approved package (resolved from the
authoritative answer bank before sealing). A missing answer is never filled
from a literal: required answers fail closed, optional ones are UNRESOLVED.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jobintel.models.answer_bank import ResolutionMode
from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage
from jobintel.submission import airtap, handoff
from jobintel.submission.airtap import (
    REQUIRED_HANDOFF_ANSWERS,
    AirTapAuthorization,
    AirTapSubmissionExecutor,
    generate_airtap_task,
    missing_required_answers,
)
from jobintel.models.answer_bank import AnswerRecord, AnswerType
from jobintel.submission import authoritative_answers, candidate_facts
from jobintel.submission.authoritative_answers import AuthoritativeAnswerBank
from jobintel.submission.handoff import generate_airtap_task_markdown, resolve_application_form
from test_submission_service import approve, setup  # noqa: F401  (fixture)

COMPLETE = {
    "legal_first_name": "Sam", "legal_last_name": "Sample", "legal_full_name": "Sam Sample",
    "email": "sam@example.org", "phone_uk": "07111111111",
    "full_address": "2 Sample Road, Sampleton, SA1 2BB, United Kingdom", "address_line_1": "2 Sample Road",
    "city": "Sampleton", "postcode": "SA1 2BB", "country": "United Kingdom",
    "right_to_work_uk": "Yes", "permitted_to_work_uk": "Yes", "permanent_or_unrestricted_right_to_work": "No",
    "require_sponsorship_now_or_future": "Yes", "require_skilled_worker_sponsorship": "Yes",
    "visa_status": "Graduate visa", "visa_expiry": "2028-05-31", "nationality": "Samplish", "citizenship": "Samplish",
    "undergraduate_institution": "Sample University", "employment_1_organisation": "Sample Ltd",
}


def _package(answers: dict[str, str]) -> ApprovedSubmissionPackage:
    return ApprovedSubmissionPackage(
        "app:1", "job:1", "https://example.org/jobs/1/apply", "cv-0123456789ab", "a" * 64, "/nonexistent/cv.pdf",
        tuple(AnswerSnapshot(field, value, "test", "v1") for field, value in sorted(answers.items())),
        "input-hash", "auto_prepare", (), company="SampleCo", role="Software Engineer",
    )


def _without(*fields: str) -> dict[str, str]:
    return {k: v for k, v in COMPLETE.items() if k not in fields}


# Captured at import, before any synthetic_answer_bank fixture can replace the seed,
# so literal-leak checks always compare against the real code-default records.
_REAL_DEFAULT_RECORDS = authoritative_answers.build_authoritative_answer_records


def _canonical_bank_values(*, skip_if_absent: bool = True) -> set[str]:
    """The real candidate's deterministic values, for literal-leak checks (compared in memory,
    never printed). In a public checkout the git-ignored candidate facts are absent: there is
    nothing that could leak, so a pure leak check skips (or, with skip_if_absent=False, the
    caller gets an empty set and runs its other checks)."""
    if not candidate_facts.local_facts_available():
        if skip_if_absent:
            pytest.skip("no real candidate facts in this checkout (git-ignored local module): nothing to leak")
        return set()
    values = set()
    for record in _REAL_DEFAULT_RECORDS():
        if record.resolution_mode == ResolutionMode.DETERMINISTIC and isinstance(record.value, str):
            values.add(record.value.strip())
    # Generic answers and public UK immigration-route names are policy/question
    # vocabulary (they appear in question wording), not candidate facts.
    generic = {"none", "true", "false", "united kingdom", "skilled worker sponsorship"}
    return {v for v in values if len(v) >= 4 and v.casefold() not in generic}


# A. nationality
def test_a_missing_nationality_is_never_a_literal() -> None:
    task = generate_airtap_task(_package(_without("nationality", "citizenship")), {})
    assert "- Country of Nationality: HUMAN-ONLY (never fill; leave untouched for the candidate and STOP if required)" in task
    assert "- Country of Citizenship: HUMAN-ONLY (never fill; leave untouched for the candidate and STOP if required)" in task
    for value in _canonical_bank_values():
        assert value not in task, value


# B. address
def test_b_missing_address_fails_closed() -> None:
    package = _package(_without("full_address", "address_line_1", "city", "postcode"))
    assert missing_required_answers(package) == ["full_address / address"]
    with pytest.raises(ValueError, match="full_address / address"):
        generate_airtap_task(package, {})


def test_b_partial_address_parts_are_unresolved_not_guessed() -> None:
    task = generate_airtap_task(_package(_without("postcode", "address_line_1")), {})
    assert "- Postcode: UNRESOLVED" in task
    assert "- Address Line 1: UNRESOLVED" in task


# C. immigration / right to work / sponsorship
@pytest.mark.parametrize("field", ["right_to_work_uk", "permanent_or_unrestricted_right_to_work", "require_sponsorship_now_or_future"])
def test_c_missing_work_authorization_answer_fails_closed(field) -> None:
    with pytest.raises(ValueError, match=field):
        generate_airtap_task(_package(_without(field)), {})


def test_c_missing_visa_details_are_human_only_not_guessed() -> None:
    task = generate_airtap_task(_package(_without("visa_status", "visa_expiry", "require_skilled_worker_sponsorship")), {})
    assert "- Current UK Visa Status: HUMAN-ONLY (never fill; leave untouched for the candidate and STOP if required)" in task
    assert "- Current Visa Expiry: HUMAN-ONLY (never fill; leave untouched for the candidate and STOP if required)" in task
    assert "- Will you require Skilled Worker sponsorship: HUMAN-ONLY (never fill; leave untouched for the candidate and STOP if required)" in task
    assert "Student visa" not in task and "Graduate Route" not in task


# D. fail closed before AirTap execution
@pytest.mark.parametrize("fields", [("legal_first_name",), ("email",), ("phone_uk",), ("right_to_work_uk",)])
def test_d_executor_refuses_before_creating_a_bundle(tmp_path, fields) -> None:
    package = _package(_without(*fields))
    auth = AirTapAuthorization("approval:x", "request:x", package.package_hash, "2999-01-01T00:00:00+00:00", True, "approved")
    with pytest.raises(ValueError, match="missing required candidate answers"):
        AirTapSubmissionExecutor(tmp_path / "bundles", authorization=auth).submit(package, idempotency_key="request:x")
    assert not (tmp_path / "bundles").exists() or not any((tmp_path / "bundles").iterdir())


def test_d_enqueue_refuses_airtap_request_when_required_answer_missing(setup) -> None:  # noqa: F811
    from test_submission_service import write_synthetic_answer_bank, SYNTHETIC_REQUIRED_ANSWERS
    key = "sponsorship.now_or_future_sponsorship_required_answer"
    incomplete = {k: v for k, v in SYNTHETIC_REQUIRED_ANSWERS.items() if k != key}
    write_synthetic_answer_bank(setup.root, incomplete, human_required=(key,))
    approval = approve(setup)
    with pytest.raises(ValueError, match="require_sponsorship_now_or_future"):
        setup.svc.enqueue(approval["id"], executor="airtap")
    assert setup.svc.store.requests() == {}


# E. resolved authoritative answers still produce the same instructions
def test_e_complete_package_renders_every_approved_answer_exactly() -> None:
    package = _package(COMPLETE)
    task = generate_airtap_task(package, {})
    assert missing_required_answers(package) == []
    for label, value in [("Legal First Name", "Sam"), ("Email Address", "sam@example.org"),
                         ("Full Current Address", COMPLETE["full_address"]), ("Postcode", "SA1 2BB"),
                         ("City / Town", "Sampleton")]:
        assert f"- {label}: {value}" in task
    # Work authorisation / immigration is human-only (2026-10-13): no value reaches the agent.
    for label in ("Current legal right to work in the UK", "Will you now or in future require visa sponsorship",
                  "Current UK Visa Status", "Current Visa Expiry", "Country of Nationality"):
        assert f"- {label}: HUMAN-ONLY" in task
    for value in ("Graduate visa", "2028-05-31", "Samplish"):
        assert value not in task
    # G (hard stop) is unchanged
    assert "Do NOT click Submit" in task and "Never click final Submit under any circumstances" in task
    # The handoff module uses the very same package-only generator.
    assert generate_airtap_task_markdown(package) == task


def test_e_form_resolution_values_come_from_the_bank_not_rule_literals(tmp_path) -> None:
    bank = AuthoritativeAnswerBank(tmp_path)
    synthetic = {"answer_type": AnswerType.AUTHORITATIVE_FACT, "provenance": "synthetic-test-fixture",
                 "verified_at": "2026-01-01"}
    bank._add_record(AnswerRecord(field_id="identity.email", value="bank-value@example.org",
                                  aliases=("email", "email address"), **synthetic))
    bank._add_record(AnswerRecord(field_id="identity.nationality", value=None, aliases=("nationality",),
                                  resolution_mode=ResolutionMode.HUMAN_REQUIRED, **synthetic))
    resolved, unresolved = resolve_application_form(["Email address", "What is your nationality?"], bank=bank)
    assert resolved["Email address"] == "bank-value@example.org"
    assert "What is your nationality?" in unresolved


# H. no candidate-specific literal remains in the executor/handoff path
@pytest.mark.parametrize("module", [airtap, handoff])
def test_h_no_candidate_literals_in_executor_or_handoff(module) -> None:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    literals = {node.value.strip() for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    reference = _canonical_bank_values()
    leaked = len([v for v in reference if any(v == lit or v in lit for lit in literals)])
    assert leaked == 0  # count only, never the values


def test_h_required_answer_list_holds_field_names_only() -> None:
    fields = {f for group in REQUIRED_HANDOFF_ANSWERS for f in group}
    assert fields <= set(airtap.LABELS)
