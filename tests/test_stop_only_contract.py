"""Shared stop-only contract (jobintel.submission.stop_only).

AirTap re-exports the shared names unchanged and wraps the executor-parameterised
result schema/import with executor="airtap". The import rules are checked here for
an arbitrary executor name so they cannot silently depend on "airtap".
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from jobintel.submission import airtap, browserbase, stop_only
from jobintel.submission.executors import STOP_ONLY_EXECUTORS
from test_handoff_fail_closed import COMPLETE, _canonical_bank_values, _package
from test_submission_service import approve, setup  # noqa: F401  (fixture)

SHARED = ("BARE_ASSERTIONS", "EVIDENCE_KINDS", "LABELS", "MODE", "PAGE_EVIDENCE", "REQUIRED_HANDOFF_ANSWERS",
          "SENSITIVE", "STATUSES", "SUBMISSION_EVIDENCE", "check_page", "escalation_reason",
          "missing_required_answers", "question", "validate_schema")
AIRTAP_ONLY = ("LIVE_BLOCKERS", "CV_UPLOAD_MODE", "AirTapAuthorization", "AirTapHandoff", "AirTapSubmissionExecutor",
               "generate_airtap_task", "supervised_review_lines", "_ANSWER_SECTIONS")
PACKAGE = _package(COMPLETE)  # synthetic values only
SCREEN = "Final review page for Software Engineer at SampleCo; Submit visible, not pressed."


def report(executor, **changes):
    value = {"request_id": "r", "approval_id": "a", "package_hash": PACKAGE.package_hash,
             "application_url": PACKAGE.application_url, "cv_artifact_id": PACKAGE.cv_artifact_id,
             "cv_sha256": PACKAGE.cv_pdf_sha256, "observed_company": "SampleCo", "observed_role": "Software Engineer",
             "observed_url": PACKAGE.application_url, "task_id": "t1", "executor": executor,
             "execution_mode": stop_only.MODE, "status": "ready_for_human_review", "final_submit_clicked": False,
             "cv_upload_confirmed": True, "duplicate_detected": False,
             "fields_filled": [{"field": "email", "value": COMPLETE["email"], "page_label": "Email"}],
             "questions_escalated": [],
             "evidence": [{"kind": "screen_state", "value": SCREEN, "sha256": "", "detail": "observed"}]}
    value.update(changes)
    return value


def shared_import(executor, value):
    return stop_only.import_result(value, package=PACKAGE, request_id="r", approval_id="a",
                                   executor=executor, display_name=executor)


# --- AirTap compatibility -------------------------------------------------------------

@pytest.mark.parametrize("name", SHARED)
def test_airtap_reexports_the_identical_shared_object(name):
    assert getattr(airtap, name) is getattr(stop_only, name)


def test_airtap_wrappers_preserve_exact_behaviour():
    assert airtap.result_schema() == stop_only.result_schema("airtap")
    assert airtap.result_schema()["properties"]["executor"] == {"const": "airtap"}
    for changes in ({}, {"cv_upload_confirmed": False}, {"observed_company": "Other"}, {"duplicate_detected": True},
                    {"status": "failed"}):
        assert airtap.import_result(report("airtap", **changes), package=PACKAGE, request_id="r", approval_id="a") == \
               stop_only.import_result(report("airtap", **changes), package=PACKAGE, request_id="r", approval_id="a",
                                       executor="airtap", display_name="AirTap")
    with pytest.raises(ValueError, match="^AirTap result identity/hash mismatch$"):
        airtap.import_result(report("airtap", package_hash="0"), package=PACKAGE, request_id="r", approval_id="a")


@pytest.mark.parametrize("name", AIRTAP_ONLY)
def test_transport_specifics_stay_in_airtap(name):
    assert hasattr(airtap, name) and not hasattr(stop_only, name)


# --- shared rules are identical / executor-independent ------------------------------------

def test_required_answer_logic_is_the_single_shared_definition():
    assert airtap.missing_required_answers is stop_only.missing_required_answers is browserbase.missing_required_answers
    assert {f for group in stop_only.REQUIRED_HANDOFF_ANSWERS for f in group} <= set(stop_only.LABELS)
    assert stop_only.missing_required_answers(PACKAGE) == []
    without_email = _package({k: v for k, v in COMPLETE.items() if k != "email"})
    assert stop_only.missing_required_answers(without_email) == ["email"]


@pytest.mark.parametrize("term", stop_only.SENSITIVE)
def test_every_sensitive_term_escalates(term):
    assert stop_only.escalation_reason(f"Please answer: {term}") == "sensitive:" + term
    assert stop_only.question(PACKAGE, "email", term, "page", "evidence")["status"] == "needs_human_input"


def test_exact_permitted_label_returns_only_the_package_answer():
    assert stop_only.question(PACKAGE, "email", "Email", "page", "evidence") == {"value": COMPLETE["email"]}
    assert stop_only.escalation_reason("Favourite colour") == "unknown_field"


@pytest.mark.parametrize("executor", ["airtap", "example_executor"])
def test_import_is_parameterised_and_bound_to_the_expected_executor(executor):
    result = shared_import(executor, report(executor))
    assert result.executor == executor and result.outcome == "ready_for_human_review"
    other = "example_executor" if executor == "airtap" else "airtap"
    with pytest.raises(ValueError, match="fixed execution policy"):
        shared_import(executor, report(other))
    with pytest.raises(ValueError, match="Expected executor is required"):
        stop_only.import_result(report(executor), package=PACKAGE, request_id="r", approval_id="a",
                                executor="", display_name="x")


@pytest.mark.parametrize("executor", ["airtap", "example_executor"])
@pytest.mark.parametrize("changes", [{"final_submit_clicked": True}, {"status": "submitted"},
                                     {"execution_mode": "FINAL_SUBMIT"}])
def test_final_submit_or_submitted_report_is_rejected(executor, changes):
    with pytest.raises(ValueError):
        shared_import(executor, report(executor, **changes))


@pytest.mark.parametrize("executor", ["airtap", "example_executor"])
@pytest.mark.parametrize("kind", sorted(stop_only.SUBMISSION_EVIDENCE))
def test_submission_evidence_in_stop_mode_is_outcome_unknown(executor, kind):
    value = report(executor, evidence=[{"kind": kind, "value": "Thanks, your application was received", "sha256": "",
                                        "detail": "page text"}])
    assert shared_import(executor, value).outcome == "outcome_unknown"


@pytest.mark.parametrize("executor", ["airtap", "example_executor"])
def test_ready_for_human_review_is_never_a_receipt(executor):
    outcomes = {shared_import(executor, report(executor, **c)).outcome
                for c in ({}, {"status": "failed"}, {"cv_upload_confirmed": False}, {"duplicate_detected": True},
                          {"evidence": [{"kind": "screen_state", "value": "done", "sha256": "", "detail": "done"}]})}
    assert "reported_submitted" not in outcomes
    assert shared_import(executor, report(executor)).submitted_at is None


# --- purity, literals, registration --------------------------------------------------------

def test_shared_module_has_no_network_env_store_file_or_secret_access():
    tree = ast.parse(inspect.getsource(stop_only))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert imported <= {"pathlib", "Path", "re", "urllib.parse", "urlsplit", "jobintel.models.submission",
                        "ApprovedSubmissionPackage", "ExecutionResult", "ReceiptEvidence"}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & {"environ", "getenv", "putenv", "system", "popen", "urlopen", "connect",
                             "read_text", "read_bytes", "write_text", "write_bytes"}
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "open" not in calls


def test_shared_module_contains_no_candidate_literals_or_executor_names():
    tree = ast.parse(Path(stop_only.__file__).read_text(encoding="utf-8"))
    literals = {node.value.strip() for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    reference = _canonical_bank_values(skip_if_absent=False)  # empty in a public checkout
    leaked = len([v for v in reference if any(v == lit or v in lit for lit in literals)])
    assert leaked == 0  # count only, never the values (docstrings included)

    # Executor coupling: docstrings may name executors as documentation; every other string
    # literal, identifier, attribute and import must not.
    scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))]
    docstrings = {id(s.body[0].value) for s in scopes if s.body and isinstance(s.body[0], ast.Expr)
                  and isinstance(s.body[0].value, ast.Constant) and isinstance(s.body[0].value.value, str)}
    code_literals = [n.value for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
    identifiers = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
    identifiers += [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    identifiers += [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    identifiers += [a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments) for a in n.args + n.kwonlyargs]
    identifiers += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    identifiers += [a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names]
    coupled = [x for x in code_literals + identifiers if "airtap" in x.casefold() or "browserbase" in x.casefold()]
    assert coupled == []


def test_stop_only_executors_are_exactly_airtap_and_browserbase(setup):
    assert STOP_ONLY_EXECUTORS == frozenset({"airtap", "browserbase", "local_browser"})
    approval = approve(setup)
    # Browserbase only exists as a stop-only executor of the service; an unregistered backend is refused.
    with pytest.raises(ValueError, match="Unknown executor"):
        setup.svc.enqueue(approval["id"], executor="unregistered_backend")
    assert setup.svc.store.requests() == {}
