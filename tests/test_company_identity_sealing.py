"""Page identity uses company names sealed into the package (2026-10-13).

A stored ATS slug is not always the brand the page shows ("fanvue.com" vs "Fanvue"). The package
seals both the slug and the registry display name at preview time; the runner and the result
importer accept the page company only when it is one of those sealed names, matched
case-insensitively as a whole word. Role and URL checks are unchanged, and nothing reads the
registry after sealing.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from jobintel.models.submission import AnswerSnapshot, ApprovedSubmissionPackage, fingerprint
from jobintel.submission import packages, stop_only

URL = "https://jobs.example.test/brand/apply.html"


def _package(company, names=(), role="Software Engineer (Intern)", url=URL):
    return ApprovedSubmissionPackage("app:x", "job:x", url, "cv-0123456789ab", "0" * 64, "/nowhere/cv.pdf",
                                     (AnswerSnapshot("email", "sam@example.org", "synthetic", "v1"),), "input",
                                     "auto_prepare", (), company=company, role=role,
                                     expected_company_names=tuple(names))


@pytest.fixture
def registry(tmp_path):
    entries = [{"company_name": "Fanvue", "connector_token": "fanvue.com", "ashby_board_name": "fanvue.com"},
               {"company_name": "Primer", "connector_token": "primer.io", "ashby_board_name": "primer.io"},
               {"company_name": "ElevenLabs", "connector_token": "elevenlabs", "ashby_board_name": "elevenlabs"},
               {"company_name": "TransFICC", "connector_token": "transficc", "ashby_board_name": "transficc"}]
    (tmp_path / "target_companies.json").write_text(json.dumps(entries), encoding="utf-8")
    return tmp_path / "personal_strategy.json"


# --- sealing: both the slug and the display name, resolved once at package time --------------------

@pytest.mark.parametrize("slug,sealed", [
    ("fanvue.com", ("fanvue.com", "Fanvue")),
    ("primer.io", ("primer.io", "Primer")),
    ("elevenlabs", ("elevenlabs", "ElevenLabs")),
    ("transficc", ("transficc", "TransFICC")),
    ("unlistedco", ("unlistedco",)),  # no registry entry: only the stored name
])
def test_both_the_slug_and_the_display_name_are_sealed(registry, slug, sealed):
    assert packages.expected_company_names(slug, registry) == sealed


def test_a_reviewed_alias_is_sealed_for_an_employer_outside_the_registry(registry):
    """2026-10-16: Greenhouse board "elwoodtechnologies" shows "Elwood Technologies"; the employer is not
    in the target-company registry, so a reviewed alias file supplies the display name."""
    (registry.parent / "company_aliases.json").write_text(
        json.dumps({"elwoodtechnologies": ["Elwood Technologies"]}), encoding="utf-8")
    sealed = packages.expected_company_names("elwoodtechnologies", registry)
    assert sealed == ("elwoodtechnologies", "Elwood Technologies")
    assert stop_only.company_seen(_package("elwoodtechnologies", sealed),
                                  "Job Application for Graduate/Junior Software Engineer at Elwood Technologies")
    assert packages.expected_company_names("elwood", registry) == ("elwood",)  # exact slug only
    assert packages.expected_company_names("unlistedco", registry) == ("unlistedco",)


def test_the_sealed_names_are_part_of_the_package_hash():
    base = _package("fanvue.com", ("fanvue.com", "Fanvue"))
    assert replace(base, expected_company_names=("fanvue.com", "Other")).package_hash != base.package_hash


def test_a_package_sealed_before_the_field_existed_keeps_its_hash():
    legacy = _package("transficc")
    data = asdict(legacy)
    del data["supervised_acknowledgement"], data["expected_company_names"]
    del data["cover_letter_pdf_sha256"], data["cover_letter_docx_sha256"]  # fields added 2026-10-17, empty here
    assert legacy.package_hash == fingerprint(data)  # the pre-2026-10-13 hash identity
    assert ApprovedSubmissionPackage.from_dict(data).package_hash == legacy.package_hash


def test_the_runner_never_reads_the_registry():
    import inspect
    from jobintel.submission import browserbase_runner
    source = inspect.getsource(browserbase_runner) + inspect.getsource(stop_only)
    assert "target_companies" not in source and "company_display_name" not in source


# --- matching rule ----------------------------------------------------------------------------------

@pytest.mark.parametrize("slug,names,title", [
    ("fanvue.com", ("fanvue.com", "Fanvue"), "Software Engineer (Intern) @ Fanvue"),
    ("primer.io", ("primer.io", "Primer"), "Software Engineer (Intern) @ Primer"),
    ("elevenlabs", ("elevenlabs", "ElevenLabs"), "Software Engineer (Intern) @ ElevenLabs"),
    ("transficc", ("transficc", "TransFICC"), "Software Engineer (Intern) @ TransFICC"),
])
def test_the_page_brand_matches_a_sealed_name(slug, names, title):
    package = _package(slug, names)
    seen = stop_only.company_seen(package, title)
    assert seen is not None
    assert stop_only.check_page(package, company=seen, role="Software Engineer (Intern)", application_url=URL) == "matched"


def test_without_the_sealed_display_name_the_slug_alone_does_not_match_the_brand():
    assert stop_only.company_seen(_package("fanvue.com"), "Software Engineer (Intern) @ Fanvue") is None


@pytest.mark.parametrize("title", [
    "Software Engineer (Intern) @ OtherCorp",
    "Software Engineer (Intern) @ Fanvuetastic",   # a longer word is not the company
    "Software Engineer (Intern) @ Fan vue",
    "Software Engineer (Intern)",
])
def test_a_wrong_or_lookalike_company_does_not_match(title):
    assert stop_only.company_seen(_package("fanvue.com", ("fanvue.com", "Fanvue")), title) is None


def test_names_without_meaning_are_never_identities():
    package = _package("ab", ("ab", "12", " "))
    assert stop_only.expected_company_names(package) == ()
    assert stop_only.company_seen(package, "Engineer @ ab") is None


@pytest.mark.parametrize("company,role,url", [
    ("OtherCorp", "Software Engineer (Intern)", URL),                                  # wrong company
    ("Fanvue", "Senior Platform Engineer", URL),                                       # wrong role
    ("Fanvue", "Software Engineer (Intern)", "https://jobs.example.test/brand/other.html"),  # wrong path
    ("Fanvue", "Software Engineer (Intern)", "https://jobs.example.test/brand/apply.html?redirected=1"),
    ("Fanvue", "Software Engineer (Intern)", "https://other.example.test/brand/apply.html"),  # other host
])
def test_check_page_fails_closed(company, role, url):
    package = _package("fanvue.com", ("fanvue.com", "Fanvue"))
    assert stop_only.check_page(package, company=company, role=role, application_url=url) == "aborted_wrong_job"


# --- the runner, against local fixture pages (no network) ------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from jobintel.submission.browserbase import BrowserbaseBundleAuthorization, write_bundle  # noqa: E402
from jobintel.submission.browserbase_runner import load_bundle, run  # noqa: E402
from test_browserbase_runner_offline import HOST, SYNTHETIC_ANSWERS, browser, server_page  # noqa: E402,F401

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _bundle(tmp_path, case, names, role="Software Engineer (Intern)"):
    sealed = tmp_path / "sealed" / "cv.pdf"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(b"%PDF-1.4 synthetic identity CV")
    sha = hashlib.sha256(sealed.read_bytes()).hexdigest()
    package = replace(_package("fanvue.com", names, role=role, url=f"https://{HOST}/{case}/apply.html"),
                      cv_pdf_sha256=sha, cv_path=str(sealed),
                      answers=tuple(AnswerSnapshot(f, v, "synthetic", "v1") for f, v in sorted(SYNTHETIC_ANSWERS.items())))
    auth = BrowserbaseBundleAuthorization("approval:id", f"request:id-{case}", package.package_hash,
                                          (NOW + timedelta(days=1)).isoformat(), True, "approved")
    return load_bundle(write_bundle(tmp_path / "bundles", package, auth, clock=lambda: NOW))


def test_runner_fanvue_slug_matches_the_sealed_brand(tmp_path, server_page):  # noqa: F811
    server, page = server_page
    bundle = _bundle(tmp_path, "brand", ("fanvue.com", "Fanvue"))
    assert bundle.expected_company_names == ("fanvue.com", "Fanvue")
    result = run(page, bundle)
    assert result.start_identity["state"] == "matched" and result.start_identity["company_matched"] == "fanvue"
    assert result.stop_reason != "page_identity_mismatch"
    assert {f["field"] for f in result.fields_filled} == {"email"}  # the form was read and filled
    assert result.final_submit_clicked is False and server.submit_requests == []


@pytest.mark.parametrize("case,names,role", [
    ("brand", ("fanvue.com",), "Software Engineer (Intern)"),                   # display name not sealed
    ("brand_lookalike", ("fanvue.com", "Fanvue"), "Software Engineer (Intern)"),  # another company
    ("brand_wrong_role", ("fanvue.com", "Fanvue"), "Software Engineer (Intern)"),  # right company, wrong role
])
def test_runner_fails_closed_before_reading_the_form(tmp_path, server_page, case, names, role):  # noqa: F811
    server, page = server_page
    result = run(page, _bundle(tmp_path, case, names, role))
    assert (result.status, result.stop_reason) == ("aborted_wrong_job", "page_identity_mismatch")
    assert result.fields_filled == [] and server.submit_requests == []
