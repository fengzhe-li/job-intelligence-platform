"""Approved CV upload and deterministic location / work-authorisation fields (offline).

A. the exact approved, sealed CV is uploaded automatically (and is visible on the page);
B. no other file can be uploaded (wrong artifact/hash, tampered sealed copy, no path option);
C. uploading never submits; D. the upload operations pass only inside the CV-upload window;
E. unknown upload/storage operations stay fatal; F. location from the authoritative profile;
G. future sponsorship -> Yes; H. permanent/unrestricted right to work -> No; I. ambiguous
work-authorisation wording escalates; J. LinkedIn stays unresolved; K. the five open questions
stay human-only; L. final_submit_clicked is always False.

Every host is reserved or intercepted (the browser resolves no names): nothing leaves the machine.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage
from jobintel.submission import browserbase_runner, stop_only
from jobintel.submission.browserbase import BrowserbaseBundleAuthorization, write_bundle
from jobintel.submission.browserbase_runner import CvUploadPolicy, load_bundle, validate_cv_upload_policy
from test_browserbase_graphql_and_identity import ENDPOINT, HOST, no_secret

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")

STORAGE = "uploads.storage.test"
S3 = "ashbyhq-infra-prd-main-app-uploaded-files-us-east-1.s3.us-east-1.amazonaws.com"  # observed 2026-10-11
SET = (ENDPOINT, "ApiSetFormValue")
HANDLE = (ENDPOINT, "ApiCreateFileUploadHandle")
TO_FILE = (ENDPOINT, "ApiSetFormValueToFile")
GEO = (ENDPOINT, "ApiAutocompleteGeoLocation")
CV_BYTES = b"%PDF-1.4 synthetic approved CV for the upload tests"
ANSWERS = {
    "name": "Sam Sample", "legal_full_name": "Sam Sample", "legal_first_name": "Sam", "legal_last_name": "Sample",
    "email": "sam@example.org", "phone_uk": "07111111111", "full_address": "2 Sample Road, Sampleton, SA1 2BB, United Kingdom",
    "location": "Sampleton, United Kingdom",
    "right_to_work_uk": "Yes", "permanent_or_unrestricted_right_to_work": "No",
    "require_sponsorship_now_or_future": "Yes",
}
OPEN_QUESTIONS = [
    "1. what aspects of software development technology or process are you most interested in and why?",
    "2. what specific technology do you dislike the most and why?",
    "3. what does good software look like in your opinion?",
    "4. please describe an interesting technical challenge you have faced in your work and how you overcame it.",
    "5. what are you looking for in your next role?",
]


# --- G / H / I: the deterministic wording rules (pure) -------------------------------------------------

@pytest.mark.parametrize("question", [
    "Will you now or in the future require visa sponsorship?",
    "Will you now or in the future require visa sponsorship to work in the UK?",
    "Do you require visa sponsorship now or in the future?",
    "Do you require sponsorship to work in the UK?",
    "Will you require sponsorship in the future?",
    "Do you or will you require sponsorship to work in the United Kingdom?",
    "Visa sponsorship required?",
])
def test_G_sponsorship_now_or_future_wording_maps_to_the_sponsorship_answer(question):
    assert stop_only.deterministic_field(question) == "require_sponsorship_now_or_future"
    assert stop_only.escalation_reason(question, deterministic=True) is None
    assert stop_only.escalation_reason(question) is not None  # other executors (AirTap) still escalate


@pytest.mark.parametrize("question", [
    "Do you have the permanent or unrestricted right to work in the UK?",
    "Do you have unrestricted right to work in the UK?",
    "Do you have the right to work in the UK without sponsorship?",
    "Are you eligible to work in the UK without visa sponsorship?",
    "Do you have indefinite leave to remain in the UK?",
])
def test_H_permanent_or_unrestricted_wording_maps_to_the_unrestricted_answer(question):
    assert stop_only.deterministic_field(question) == "permanent_or_unrestricted_right_to_work"


def test_current_right_to_work_needs_explicit_current_wording():
    assert stop_only.deterministic_field("Do you currently have the right to work in the UK?") == "right_to_work_uk"
    assert stop_only.deterministic_field("Are you currently eligible to work in the UK?") == "right_to_work_uk"


@pytest.mark.parametrize("question", [
    "Do you have the right to work in the UK?",          # current or unrestricted? -> human
    "Are you eligible to work in the UK?",
    "Right to work",
    "Work authorization status",
    "What is your visa status?",
    "Do you require sponsorship to work in the US?",     # another country
    "Will you require sponsorship for a role in Ireland?",
    "Please describe your visa situation",
])
def test_I_ambiguous_or_other_work_authorisation_wording_escalates(question):
    assert stop_only.deterministic_field(question) is None
    assert stop_only.escalation_reason(question, deterministic=True) is not None


def test_F_location_wording():
    assert stop_only.label_fields("Location") == ["location"]
    assert stop_only.label_fields("Where are you currently based?") == ["location"]


@pytest.fixture
def bank_root(tmp_path):
    from synthetic_answer_bank import write_synthetic_answer_bank
    values = {"address.city": "Sampleton", "address.country": "United Kingdom", "identity.email": "sam@example.org"}
    write_synthetic_answer_bank(tmp_path, values)
    return tmp_path


def test_F_location_answer_is_city_and_country_from_the_authoritative_records(bank_root):
    from jobintel.submission.handoff import build_authoritative_snapshots
    snapshots = {s.field: s for s in build_authoritative_snapshots(root=bank_root)}
    assert snapshots["location"].value == "Sampleton, United Kingdom"
    assert snapshots["location"].source == "authoritative_answer_bank:address.city+address.country"
    assert snapshots["city"].value == "Sampleton"


def test_J_linkedin_is_never_derived_without_an_authoritative_url(bank_root):
    from jobintel.submission.handoff import build_authoritative_snapshots
    assert "linkedin_url" not in {s.field for s in build_authoritative_snapshots(root=bank_root)}


# --- B: the policy can only name the sealed, approved CV ----------------------------------------------

def bundle_for(tmp_path, query="", answers=ANSWERS):
    sealed = tmp_path / "sealed" / "cv.pdf"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(CV_BYTES)
    package = ApprovedSubmissionPackage(
        "app:upload-1", "job:upload-1", f"https://{HOST}/graphql/hosted_form.html{query}", "cv-0123456789ab",
        hashlib.sha256(CV_BYTES).hexdigest(), str(sealed),
        tuple(AnswerSnapshot(f, v, "synthetic", "v1") for f, v in sorted(answers.items())), "input-hash",
        "auto_prepare", (), company="ExampleCo", role="Graduate Engineer")
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    auth = BrowserbaseBundleAuthorization("approval:upload", "request:upload" + query, package.package_hash,
                                          (now + timedelta(days=1)).isoformat(), True, "approved")
    return load_bundle(write_bundle(tmp_path / "bundles", package, auth, clock=lambda: now))


def policy_for(bundle, **changes):
    base = CvUploadPolicy(bundle.request["cv"]["artifact_id"], bundle.request["cv"]["sha256"], "Sam_Sample_CV.pdf",
                          frozenset({HANDLE}), frozenset({("POST", STORAGE)}), frozenset({TO_FILE}))
    return dataclasses.replace(base, **changes)


def test_B_the_policy_must_name_exactly_the_sealed_cv_and_has_no_path(tmp_path):
    bundle = bundle_for(tmp_path)
    assert validate_cv_upload_policy(bundle, policy_for(bundle))
    assert {f.name for f in dataclasses.fields(CvUploadPolicy)} == {
        "artifact_id", "sha256", "filename", "graphql_mutations", "storage_writes",
        "association_mutations"}  # no file path at all
    for bad in (dict(artifact_id="cv-999999999999"), dict(sha256="0" * 64), dict(filename="../../etc/passwd"),
                dict(filename="cv.exe"), dict(filename="/Users/x/cv.pdf")):
        with pytest.raises(ValueError):
            validate_cv_upload_policy(bundle, policy_for(bundle, **bad))


@pytest.mark.parametrize("mutations,storage", [
    ({(ENDPOINT, "ApiSubmitApplication")}, set()),                 # final action
    ({(ENDPOINT, "ApiCompleteFileUpload")}, set()),                # "complete" is a final-action word
    ({(ENDPOINT, "ApiSetFormValue")}, set()),                      # not an upload operation
    ({("https://elsewhere.test/api/non-user-graphql", "ApiCreateFileUploadHandle")}, set()),  # other host
    (set(), {("POST", HOST)}),                                     # the application host stays fatal
    (set(), {("POST", "*.storage.test")}), (set(), {("GET", STORAGE)}), (set(), {("POST", "192.0.2.1")}),
])
def test_B_D_upload_operations_are_exact_and_never_final(tmp_path, mutations, storage):
    bundle = bundle_for(tmp_path)
    with pytest.raises(ValueError):
        validate_cv_upload_policy(bundle, policy_for(bundle, graphql_mutations=frozenset(mutations),
                                                      storage_writes=frozenset(storage)))


def test_cv_upload_needs_a_live_host(tmp_path):
    bundle = bundle_for(tmp_path)
    with pytest.raises(ValueError, match="live-host"):
        browserbase_runner.run(object(), bundle, cv_upload=policy_for(bundle))


# --- the real runner, live-host mode, local headless Chrome --------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from test_browserbase_live_host import browser  # noqa: E402,F401  (fixture)

FIXTURE = Path(__file__).parent / "fixtures" / "graphql" / "hosted_form.html"


class HostedServer:
    """The approved host and the synthetic storage host; everything else would have left the machine."""

    def __init__(self):
        self.requests, self.external, self.operations, self.storage = [], [], [], []
        self.query = ""

    def handle(self, route, request):
        parts = urlsplit(request.url)
        if parts.hostname in {STORAGE, "other-storage.test", S3}:
            self.storage.append((request.method, parts.hostname, request.post_data_buffer or b""))
            return route.fulfill(status=403 if "mode=storage_fail" in self.query else 204, body="")
        if parts.hostname != HOST:
            self.external.append((request.method, parts.hostname))
            return route.abort()
        self.requests.append((request.method, parts.path))
        if request.method == "POST" and parts.path == "/api/non-user-graphql":
            operation = json.loads(request.post_data)["operationName"]
            self.operations.append(operation)
            body = {"data": {}}
            if operation == "ApiCreateFileUploadHandle":
                body = {"data": {"createFileUploadHandle": {"handle": "hdl-0f3c9a7e5b21",
                                                            "url": f"https://{STORAGE}/bucket"}}}
            if operation == "ApiSetFormValueToFile" and "mode=assoc_fail" in self.query:
                body = {"errors": [{"message": "synthetic association failure"}]}
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        if request.method != "GET" or not parts.path.endswith("/hosted_form.html"):
            return route.fulfill(status=404, body="not found")
        self.query = parts.query
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text())


@pytest.fixture
def server_page(browser):  # noqa: F811
    server = HostedServer()
    context = browser.new_context()
    context.route("**/*", server.handle)
    page = context.new_page()
    yield server, page
    context.close()


def live_run(tmp_path, server_page, query="?mode=ok", *, upload=True, policy_changes=None, **kwargs):
    server, page = server_page
    bundle = bundle_for(tmp_path, query)
    policy = policy_for(bundle, **(policy_changes or {})) if upload else None
    result = browserbase_runner.run(page, bundle, live_host=HOST, graphql_reads=[GEO], graphql_form_writes=[SET],
                                    cv_upload=policy, **kwargs)
    # L: never a final submit, nothing outside the synthetic hosts, the page never submitted.
    assert result.final_submit_clicked is False and server.external == []
    assert "ApiSubmitApplication" not in server.operations
    assert not any(p.endswith("/submit") for _, p in server.requests)
    assert page.evaluate("() => document.body.dataset.submitted || '0'") == "0"
    return server, page, bundle, result


def escalated(result):
    return {stop_only.normalise_question(q["question"]): q["screenshot_or_evidence"].removeprefix("runner:")
            for q in result.questions_escalated}


def values(page, *ids):
    return {i: page.eval_on_selector(f"#{i}", "el => el.value") for i in ids}


def test_A_C_F_G_H_J_K_L_an_almost_complete_draft(tmp_path, server_page):
    server, page, bundle, result = live_run(tmp_path, server_page)
    # A: the exact sealed CV was uploaded, last on the page, and the page shows it.
    assert result.cv_upload_status == "uploaded"
    form = browserbase_runner.form_diagnostics(result)
    assert (form["cv_artifact_id"], form["cv_upload_visible_in_page"]) == ("cv-0123456789ab", True)
    assert [(m, h) for m, h, _ in server.storage] == [("POST", STORAGE)]
    assert CV_BYTES in server.storage[0][2]  # the bytes the storage host received are the sealed CV
    assert page.inner_text("#resume-name") == "Sam_Sample_CV.pdf"
    assert server.operations.count("ApiCreateFileUploadHandle") == 1
    kinds = [w.get("kind") for w in result.allowed_writes]
    assert kinds.index("cv_upload_handle") > max(i for i, k in enumerate(kinds) if k == "graphql_form_write"
                                                 and result.allowed_writes[i]["field"] != "cv")
    # F / G / H: deterministic fields from the approved package.
    assert {f["field"] for f in result.fields_filled} >= {
        "location", "require_sponsorship_now_or_future", "permanent_or_unrestricted_right_to_work", "name", "email"}
    assert values(page, "loc")["loc"] == "Sampleton, England, United Kingdom"
    assert page.eval_on_selector("input[name=spons]:checked", "el => el.value") == "Yes"
    assert page.eval_on_selector("input[name=rtw]:checked", "el => el.value") == "No"
    # J: LinkedIn stays unresolved.
    assert {"label": "linkedin profile", "reason": "no_approved_answer"} in form["fields_skipped"]
    assert values(page, "li") == {"li": ""}
    # K: the five open questions stay human-only and empty.
    assert all(escalated(result).get(question.rstrip(".?")) == "unknown_field" or
               escalated(result).get(stop_only.normalise_question(question)) == "unknown_field"
               for question in OPEN_QUESTIONS)
    assert set(values(page, "q1", "q2", "q3", "q4", "q5").values()) == {""}
    # C / L: an uploaded CV is a draft for the human, never a submission.
    assert result.status == "needs_human_input" and result.stop_reason == "questions_escalated"
    report = browserbase_runner.to_stop_only_report(result, bundle, task_id="t")
    assert report["final_submit_clicked"] is False and report["cv_upload_confirmed"] is True
    assert no_secret(json.dumps(form)) and "/sealed/" not in json.dumps(form)


def test_C_a_final_mutation_after_the_upload_is_still_fatal(tmp_path, server_page):
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=submit_after_upload")
    assert result.stop_reason == "unexpected_write_blocked" and result.cv_upload_status != "uploaded"
    assert result.blocked_requests[-1]["graphql_operation_name"] == "ApiSubmitApplication"


def test_D_upload_operations_outside_the_window_are_fatal(tmp_path, server_page):
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=handle_at_load")
    assert result.stop_reason == "unexpected_write_blocked"
    assert result.blocked_requests[0]["graphql_operation_name"] == "ApiCreateFileUploadHandle"
    assert result.cv_upload_status != "uploaded" and server.storage == []


def test_D_without_the_upload_policy_the_cv_stays_with_the_human(tmp_path, server_page):
    server, page, bundle, result = live_run(tmp_path, server_page, upload=False)
    assert result.cv_upload_status == "human_required" and server.storage == []
    assert "ApiCreateFileUploadHandle" not in server.operations
    assert {f["field"] for f in result.fields_filled} >= {"location", "require_sponsorship_now_or_future"}


@pytest.mark.parametrize("mode,blocked", [
    ("unknown_op", ("graphql", "ApiParseResumeForAutofill")),
    ("unknown_storage", ("host", "other-storage.test")),
])
def test_E_unknown_upload_or_storage_operations_are_fatal(tmp_path, server_page, mode, blocked):
    server, page, bundle, result = live_run(tmp_path, server_page, f"?mode={mode}")
    assert result.stop_reason == "unexpected_write_blocked" and result.cv_upload_status != "uploaded"
    last = result.blocked_requests[-1]
    assert (last.get("graphql_operation_name") if blocked[0] == "graphql" else last["host"]) == blocked[1]
    assert all(host == STORAGE for _, host, _ in server.storage)  # the unknown host never received anything


def test_E_an_unconfigured_storage_host_is_fatal_and_reveals_only_method_host_path(tmp_path, server_page):
    server, page, bundle, result = live_run(tmp_path, server_page, policy_changes={"storage_writes": frozenset()})
    assert result.stop_reason == "unexpected_write_blocked" and server.storage == []
    assert {k: result.blocked_requests[-1][k] for k in ("method", "host", "path")} == (
        {"method": "POST", "host": STORAGE, "path": "/bucket"})
    # The upload is done last: everything else was already filled before it stopped.
    assert {f["field"] for f in result.fields_filled} >= {"location", "require_sponsorship_now_or_future", "email"}


def test_B_a_tampered_sealed_copy_is_never_uploaded(tmp_path, server_page):
    server, page = server_page
    bundle = bundle_for(tmp_path)
    policy = policy_for(bundle)
    bundle.cv_path.chmod(0o600)  # the sealed copy is read-only; simulate someone swapping it anyway
    bundle.cv_path.write_bytes(b"%PDF-1.4 something else entirely")
    result = browserbase_runner.run(page, bundle, live_host=HOST, graphql_reads=[GEO], graphql_form_writes=[SET],
                                    cv_upload=policy)
    assert result.cv_upload_status == "escalated" and escalated(result)["resume"] == "sealed_cv_hash_mismatch"
    assert server.storage == [] and "ApiCreateFileUploadHandle" not in server.operations
    assert result.final_submit_clicked is False


def test_B_an_autofill_or_second_cv_like_control_is_never_used(tmp_path, server_page):
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=autofill")
    reasons = escalated(result)
    assert reasons["autofill from resume"] == "cv_upload_label_not_exact"
    assert reasons["resume"] == "cv_upload_ambiguous_control"
    assert server.storage == [] and result.cv_upload_status == "escalated"


def test_A_an_upload_the_page_never_shows_is_escalated(tmp_path, server_page):
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=no_show")
    assert result.cv_upload_status == "escalated" and escalated(result)["resume"] == "cv_upload_not_confirmed"


def test_G_I_sponsorship_wording_variants_in_the_page(tmp_path, server_page):
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=ok&spons=future&rtw=current")
    assert page.eval_on_selector("input[name=spons]:checked", "el => el.value") == "Yes"
    assert page.eval_on_selector("input[name=rtw]:checked", "el => el.value") == "Yes"
    assert {"require_sponsorship_now_or_future", "right_to_work_uk"} <= {f["field"] for f in result.fields_filled}


def test_I_ambiguous_work_authorisation_questions_are_escalated_not_guessed(tmp_path, server_page):
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=ok&spons=ambiguous&rtw=ambiguous")
    reasons = escalated(result)
    assert reasons["do you have the right to work in the uk"] == "sensitive:right to work"
    assert reasons["what is your visa sponsorship situation"].startswith("sensitive:")
    assert page.eval_on_selector_all("input[type=radio]", "els => els.filter(e => e.checked).length") == 0


def test_F_an_ambiguous_location_suggestion_is_escalated(tmp_path, server_page):
    server, page, bundle, result = live_run(tmp_path, server_page, "?mode=ok&loc=ambiguous")
    assert escalated(result)["location"] == "location_ambiguous_option"
    assert "location" not in {f["field"] for f in result.fields_filled}
