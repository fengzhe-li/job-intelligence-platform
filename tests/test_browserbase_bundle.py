"""Offline Browserbase bundle contract. No network, browser, Browserbase or Stagehand.

The bundle is derived only from an approved, sealed package and its approval /
request identity. "browserbase" is not an executable backend yet.
"""
from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import sys

import pytest

from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage
from jobintel.submission import browserbase
from jobintel.submission.stop_only import LABELS, MODE
from jobintel.submission.browserbase import (BrowserbaseBundleAuthorization, build_bundle_files, build_request,
                                             write_bundle)
from jobintel.submission.executors import STOP_ONLY_EXECUTORS
from test_handoff_fail_closed import _canonical_bank_values
from test_submission_service import approve, setup, write_synthetic_answer_bank  # noqa: F401  (fixture)
from test_supervised_execution import _ack, _approve, _store

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

REQUEST_ID = "request:bundle-contract-test"


def _approved(s, approval=None):
    a = approval or approve(s)
    package = ApprovedSubmissionPackage.from_dict(a["package"])
    auth = BrowserbaseBundleAuthorization(a["id"], REQUEST_ID, a["package_hash"], a["expires_at"],
                                          a["human_reviewed"], a["status"])
    return package, auth


def _build(s, package, auth):
    return build_bundle_files(package, auth, clock=lambda: s.now[0])


def _request(files):
    return json.loads(files["browserbase_request.json"])


def _rebound(package, auth, **changes):
    """A package variant with a consistent authorization (test-only), to exercise the contract itself."""
    changed = replace(package, **changes)
    return changed, replace(auth, package_hash=changed.package_hash)


# --- generation and binding ------------------------------------------------------------

def test_bundle_generation_succeeds_from_approved_sealed_package(setup):
    package, auth = _approved(setup)
    files = _build(setup, package, auth)
    assert set(files) == {"browserbase_request.json", "cv.pdf", "manifest.json"}
    manifest = json.loads(files["manifest.json"])
    assert manifest == {name: hashlib.sha256(files[name]).hexdigest() for name in ("browserbase_request.json", "cv.pdf")}


def test_ids_and_hashes_are_bound(setup):
    s = setup
    package, auth = _approved(s)
    files = _build(s, package, auth)
    request = _request(files)
    assert (request["application_id"], request["job_id"], request["approval_id"], request["request_id"],
            request["package_hash"], request["input_hash"], request["application_url"]) == (
        s.app.id, s.job.id, auth.approval_id, REQUEST_ID, package.package_hash, package.input_hash,
        s.app.application_url)
    assert request["cv"] == {"artifact_id": s.artifact.id, "sha256": package.cv_pdf_sha256, "path": "cv.pdf",
                             "upload_confirmation_required": True, "remote_integrity_verified": False}
    assert hashlib.sha256(files["cv.pdf"]).hexdigest() == package.cv_pdf_sha256 == s.artifact.pdf_sha256
    assert request["executor"] == "browserbase" and request["execution_mode"] == MODE == "STOP_BEFORE_FINAL_SUBMIT"


def test_bundle_carries_no_credentials_session_config_or_timestamps(setup):
    request = _request(_build(setup, *_approved(setup)))
    assert set(request) == {"schema", "executor", "execution_mode", "final_submit_allowed", "request_id",
                            "approval_id", "approval_expires_at", "package_hash", "input_hash", "application_id",
                            "job_id", "application_url", "company", "role", "cv", "answers", "unresolved_fields",
                            "review", "supervised_acknowledgement"}
    text = json.dumps(request).casefold()
    for forbidden in ("api_key", "token", "browserbase_project", "session_id", "connect_url", "proxy", "stealth",
                      "captcha", "model", "generated_at", "nonce"):
        assert forbidden not in text, forbidden


def test_generation_is_deterministic(setup, tmp_path):
    s = setup
    package, auth = _approved(s)
    assert _build(s, package, auth) == _build(s, package, auth)
    first = write_bundle(tmp_path / "a", package, auth, clock=lambda: s.now[0])
    second = write_bundle(tmp_path / "b", package, auth, clock=lambda: s.now[0])
    assert first.name == second.name == hashlib.sha256(REQUEST_ID.encode()).hexdigest()
    for name in ("browserbase_request.json", "cv.pdf", "manifest.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
        assert oct((first / name).stat().st_mode & 0o777) == "0o400"


def test_existing_bundle_is_never_overwritten(setup, tmp_path):
    s = setup
    package, auth = _approved(s)
    write_bundle(tmp_path, package, auth, clock=lambda: s.now[0])
    with pytest.raises(FileExistsError):
        write_bundle(tmp_path, package, auth, clock=lambda: s.now[0])


# --- final submit ----------------------------------------------------------------------

def test_final_submit_is_always_false(setup, tmp_path):
    s = setup
    request = _request(_build(s, *_approved(s)))
    assert request["final_submit_allowed"] is False
    supervised = _store(tmp_path / "supervised")
    ack = _ack(supervised)
    _, approval = _approve(supervised, ack)
    package, auth = _approved(supervised, approval)
    request = _request(_build(supervised, package, auth))
    assert request["final_submit_allowed"] is False
    assert request["supervised_acknowledgement"]["final_submit_allowed"] is False
    # No caller-controlled switch exists ...
    for function in (build_request, build_bundle_files, write_bundle):
        assert not any("final" in name or "submit" in name for name in inspect.signature(function).parameters)
    # ... and every final_submit_allowed literal in the adapter is False.
    tree = ast.parse(Path(browserbase.__file__).read_text(encoding="utf-8"))
    values = [node.values[i] for node in ast.walk(tree) if isinstance(node, ast.Dict)
              for i, key in enumerate(node.keys) if isinstance(key, ast.Constant) and key.value == "final_submit_allowed"]
    assert values and all(isinstance(v, ast.Constant) and v.value is False for v in values)


def test_supervised_package_keeps_review_result_and_acknowledgement(tmp_path):
    s = _store(tmp_path)
    ack = _ack(s)
    _, approval = _approve(s, ack)
    package, auth = _approved(s, approval)
    request = _request(_build(s, package, auth))
    assert request["review"] == {"gate_status": "block_auto_submission", "gate_reasons": list(package.gate_reasons),
                                 "submission_policy": package.submission_policy,
                                 "eligibility_state": package.eligibility_state,
                                 "authorization": "SUPERVISED_EXECUTION_ONLY"}
    assert request["supervised_acknowledgement"]["acknowledged_blockers"] == list(ack.acknowledged_blockers)


# --- answer provenance -----------------------------------------------------------------

def test_answers_are_exactly_the_sealed_package_answers(setup):
    package, auth = _approved(setup)
    request = _request(_build(setup, package, auth))
    assert request["answers"] == [{"field": a.field, "value": a.value, "source": a.source, "version": a.version}
                                  for a in sorted(package.answers, key=lambda a: a.field)]


def test_live_answer_bank_changes_after_approval_do_not_reach_the_bundle(setup):
    s = setup
    package, auth = _approved(s)
    before = _build(s, package, auth)
    changed = {"identity.email": "changed-after-approval@example.org", "identity.legal_first_name": "Changed"}
    from test_submission_service import SYNTHETIC_REQUIRED_ANSWERS
    write_synthetic_answer_bank(s.root, {**SYNTHETIC_REQUIRED_ANSWERS, **changed})
    after = _build(s, package, auth)
    assert after == before
    assert "changed-after-approval" not in after["browserbase_request.json"].decode()


def test_adapter_never_consults_the_answer_bank_or_resolution(setup, monkeypatch):
    import jobintel.submission.authoritative_answers as bank_module
    import jobintel.submission.question_resolution as resolution_module

    def boom(*args, **kwargs):
        raise AssertionError("live answer lookup during bundle generation")

    package, auth = _approved(setup)  # approval (which resolves answers) happens before the patch
    monkeypatch.setattr(bank_module.AuthoritativeAnswerBank, "__init__", boom)
    monkeypatch.setattr(resolution_module, "resolve_application_question", boom)
    assert _request(_build(setup, package, auth))["answers"]


def test_unanswered_optional_field_stays_unresolved_with_no_fallback(setup):
    s = setup
    package, auth = _approved(s)
    answered = {a.field for a in package.answers}
    optional = sorted(set(LABELS) - answered)
    assert optional, "fixture should leave some permitted fields unanswered"
    request = _request(_build(s, package, auth))
    assert request["unresolved_fields"] == optional
    assert not {a["field"] for a in request["answers"]} & set(optional)

    # Dropping an optional answer that WAS present makes it unresolved -- the field never
    # reappears with a fallback value from anywhere.
    dropped = next(a for a in package.answers if a.field == "postcode")
    variant, variant_auth = _rebound(package, auth, answers=tuple(a for a in package.answers if a is not dropped))
    variant_request = _request(_build(s, variant, variant_auth))
    assert "postcode" in variant_request["unresolved_fields"]
    assert "postcode" not in {a["field"] for a in variant_request["answers"]}
    assert variant_request["answers"] == [a for a in _request(_build(s, package, auth))["answers"] if a["field"] != "postcode"]


def test_missing_required_answer_and_non_permitted_field_fail_closed(setup):
    s = setup
    package, auth = _approved(s)
    without_email = tuple(a for a in package.answers if a.field != "email")
    variant, variant_auth = _rebound(package, auth, answers=without_email)
    with pytest.raises(ValueError, match="missing required candidate answers"):
        _build(s, variant, variant_auth)
    extra = package.answers + (AnswerSnapshot("salary_expectation", "x", "invented", "v"),)
    variant, variant_auth = _rebound(package, auth, answers=extra)
    with pytest.raises(ValueError, match="Non-permitted answer"):
        _build(s, variant, variant_auth)


# --- CV / package binding --------------------------------------------------------------

def test_changed_sealed_cv_bytes_fail_closed(setup):
    s = setup
    package, auth = _approved(s)
    sealed = Path(package.cv_path)
    sealed.chmod(0o600)
    sealed.write_bytes(b"%PDF-1.4 edited after approval")
    with pytest.raises(ValueError, match="CV hash mismatch"):
        _build(s, package, auth)


@pytest.mark.parametrize("changes", [{"cv_pdf_sha256": "0" * 64}, {"cv_artifact_id": "cv-ffffffffffff"},
                                     {"application_url": "https://example.com/other"}, {"job_id": "j-other"}])
def test_package_differing_from_the_approval_is_rejected(setup, changes):
    s = setup
    package, auth = _approved(s)
    with pytest.raises(ValueError, match="Approved package hash mismatch"):
        _build(s, replace(package, **changes), auth)


def test_rebinding_changes_the_bundle(setup):
    s = setup
    package, auth = _approved(s)
    base = _build(s, package, auth)
    variant, variant_auth = _rebound(package, auth, application_url="https://example.com/jobs/j1/apply-v2")
    other = _build(s, variant, variant_auth)
    assert other["browserbase_request.json"] != base["browserbase_request.json"]
    assert other["manifest.json"] != base["manifest.json"]


@pytest.mark.parametrize("changes", [{"status": "revoked"}, {"human_reviewed": False}, {"approval_id": ""},
                                     {"request_id": ""}, {"expires_at": "2000-01-01T00:00:00+00:00"}])
def test_inactive_or_unreviewed_approval_is_rejected(setup, changes):
    s = setup
    package, auth = _approved(s)
    with pytest.raises(ValueError, match="live, service-validated human approval"):
        _build(s, package, replace(auth, **changes))


def test_blocked_without_acknowledgement_and_hard_ineligible_are_rejected(setup):
    s = setup
    package, auth = _approved(s)
    variant, variant_auth = _rebound(package, auth, gate_status="block_auto_submission")
    with pytest.raises(ValueError, match="supervised acknowledgement"):
        _build(s, variant, variant_auth)
    variant, variant_auth = _rebound(package, auth, eligibility_state="hard_ineligible")
    with pytest.raises(ValueError, match="HARD_INELIGIBLE"):
        _build(s, variant, variant_auth)


def test_non_https_or_credentialed_url_is_rejected(setup):
    s = setup
    package, auth = _approved(s)
    for url in ("http://example.com/jobs/j1", "https://user:pw@example.com/jobs/j1"):
        variant, variant_auth = _rebound(package, auth, application_url=url)
        with pytest.raises(ValueError, match="HTTPS application URL"):
            _build(s, variant, variant_auth)


# --- purity and non-executability --------------------------------------------------------

def test_adapter_has_no_network_env_store_secret_or_browser_access():
    tree = ast.parse(inspect.getsource(browserbase))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert imported <= {"dataclasses", "dataclass", "hashlib", "os", "pathlib", "Path", "urllib.parse", "urlsplit",
                        "jobintel.models.submission", "ApprovedSubmissionPackage", "canonical", "instant", "utcnow",
                        "jobintel.submission.stop_only", "LABELS", "MODE", "missing_required_answers"}
    assert "jobintel.submission.airtap" not in imported  # shared rules come from stop_only, not the AirTap adapter
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & {"environ", "getenv", "putenv", "system", "popen", "urlopen", "connect"}


def test_no_browserbase_stagehand_or_playwright_dependency_is_imported():
    # Fresh interpreter: other test modules (the offline runner harness) legitimately load
    # Playwright into this process, so sys.modules here would not measure the adapters.
    import subprocess
    code = ("import sys, jobintel.submission.browserbase, jobintel.submission.browserbase_runner; "
            "print(sorted({m.split('.')[0] for m in sys.modules} & "
            "{'browserbase', 'stagehand', 'playwright', 'requests', 'httpx', 'openai', 'anthropic'}))")
    src = str(Path(browserbase.__file__).resolve().parents[2])
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": src}, check=True)
    assert out.stdout.strip() == "[]"


def test_no_candidate_literals_in_browserbase_adapter():
    tree = ast.parse(Path(browserbase.__file__).read_text(encoding="utf-8"))
    literals = {node.value.strip() for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    reference = _canonical_bank_values()
    leaked = len([v for v in reference if any(v == lit or v in lit for lit in literals)])
    assert leaked == 0  # count only, never the values


def test_browserbase_is_a_stop_only_executor_and_unregistered_backends_are_refused(setup, capsys):
    s = setup
    assert STOP_ONLY_EXECUTORS == frozenset({"airtap", "browserbase", "local_browser"})
    approval = approve(s)
    # Browserbase only exists as a stop-only executor of the service; an unregistered backend is refused.
    with pytest.raises(ValueError, match="Unknown executor"):
        s.svc.enqueue(approval["id"], executor="unregistered_backend")
    assert s.svc.store.requests() == {}
    from unittest.mock import patch
    from jobintel.cli.main import main
    with patch("sys.argv", ["jobintel", "submission-enqueue", "--approval-id", approval["id"], "--executor",
                            "unregistered_backend", "--store", str(s.root)]), pytest.raises(SystemExit):
        main()
    assert "invalid choice: 'unregistered_backend'" in capsys.readouterr().err
    assert s.svc.store.requests() == {}
