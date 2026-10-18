"""Reviewed ATS execution profiles: exact-host lookup, no fallback, Ashby pinned to the validated run.

The Ashby profile is the network policy of the first successful supervised Ashby-hosted run
(2026-10-11). It is pinned value by value here, so any change is a deliberate, reviewed edit.
Every value must pass the same validators SubmissionService.execute applies.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from jobintel.submission import ats_profiles, browserbase_runner
from jobintel.submission.ats_profiles import ASHBY_HOSTED, PROFILES, UnknownExecutionProfile, profile_for_url
from jobintel.submission.graphql_policy import validate_form_writes, validate_reads
from jobintel.submission.service import SubmissionService

GQL = "https://jobs.ashbyhq.com/api/non-user-graphql"
S3 = "ashbyhq-infra-prd-main-app-uploaded-files-us-east-1.s3.us-east-1.amazonaws.com"
APPLICATION = "https://jobs.ashbyhq.com/employer/00000000-0000-0000-0000-000000000000/application"


def test_the_ashby_profile_is_exactly_the_validated_policy():
    assert ASHBY_HOSTED.name == "ashby_hosted" and ASHBY_HOSTED.host == "jobs.ashbyhq.com"
    assert ASHBY_HOSTED.graphql_reads == ((GQL, "ApiJobPosting"), (GQL, "ApiOrganizationFromHostedJobsPageName"),
                                          (GQL, "ApiAutocompleteGeoLocation"))
    assert ASHBY_HOSTED.graphql_form_writes == ((GQL, "ApiSetFormValue"),)
    assert ASHBY_HOSTED.nonfatal_blocked_writes == (("POST", "browser-intake-datadoghq.com"), ("POST", "csp.withgoogle.com"))
    assert ASHBY_HOSTED.cv_upload_mutations == ((GQL, "ApiCreateFileUploadHandle"),)
    assert ASHBY_HOSTED.cv_storage_writes == (("POST", S3),)
    assert ASHBY_HOSTED.cv_association_mutations == ((GQL, "ApiSetFormValueToFile"),)


def test_only_reviewed_vendors_have_a_profile():
    assert set(PROFILES) == {"jobs.ashbyhq.com", "jobs.lever.co", "job-boards.greenhouse.io",
                             "apply.workable.com"}  # no Workday / ... until reviewed
    # Ashby and Greenhouse have passed supervised live runs; Lever is implemented but gated until validated.
    assert {h for h, p in PROFILES.items() if p.live_validated} == {"jobs.ashbyhq.com", "job-boards.greenhouse.io"}


def test_every_profile_value_passes_the_service_validators():
    for profile in PROFILES.values():
        url = f"https://{profile.host}/employer/x/application"
        assert validate_reads(url, profile.graphql_reads) == frozenset(profile.graphql_reads)
        assert validate_form_writes(url, profile.graphql_form_writes) == frozenset(profile.graphql_form_writes)
        assert browserbase_runner.validate_nonfatal_blocked_writes(url, profile.nonfatal_blocked_writes) == frozenset(
            profile.nonfatal_blocked_writes)
        policy = browserbase_runner.CvUploadPolicy(
            "cv-0123456789ab", "a" * 64, "Sam_Sample_CV.pdf", frozenset(profile.cv_upload_mutations),
            frozenset(profile.cv_storage_writes), frozenset(profile.cv_association_mutations))
        checked = browserbase_runner.validate_cv_upload_for(url, "cv-0123456789ab", "a" * 64, policy)
        assert (checked.graphql_mutations, checked.storage_writes, checked.association_mutations) == (
            frozenset(profile.cv_upload_mutations), frozenset(profile.cv_storage_writes),
            frozenset(profile.cv_association_mutations))


def test_execute_options_are_exactly_live_host_options_of_the_service():
    options = ASHBY_HOSTED.execute_options()
    params = inspect.signature(SubmissionService.execute).parameters
    assert set(options) <= set(params)
    assert options["allow_live_host"] is True and options["live_host"] == "jobs.ashbyhq.com"
    assert options["live_cv_upload"] is True and options["live_write_allowlist"] == ()
    assert not any(k in options for k in ("claim_token", "human_handoff", "executor"))


def test_the_exact_host_is_found():
    assert profile_for_url(APPLICATION) is ASHBY_HOSTED


@pytest.mark.parametrize("url", [
    "http://jobs.ashbyhq.com/employer/x/application",            # not https
    "https://JOBS.ashbyhq.com/employer/x/application",           # case variant
    "https://jobs.ashbyhq.com./employer/x/application",          # trailing dot
    "https://jobs.ashbyhq.com:443/employer/x/application",       # explicit port
    "https://user@jobs.ashbyhq.com/employer/x/application",      # credentials
    "https://careers.jobs.ashbyhq.com/employer/x/application",   # subdomain
    "https://ashbyhq.com/employer/x/application",                # parent domain
    "https://jobs.ashbyhq.com.evil.test/employer/x/application",  # suffix trick
    "https://careers.example.com/jobs/1",                        # an employer's own domain
    "https://boards.greenhouse.io/example/jobs/1",               # unreviewed vendors: refused, no fallback
    "https://example.wd3.myworkdayjobs.com/External/job/London/Engineer_R1",  # Workday: not yet reviewed
    "https://jobs.smartrecruiters.com/Example/1",
    "",
])
def test_anything_else_is_refused_with_no_fallback(url):
    with pytest.raises(UnknownExecutionProfile):
        profile_for_url(url)


def test_there_is_no_hidden_fallback_profile():
    with pytest.raises(UnknownExecutionProfile):
        profile_for_url(APPLICATION, profiles={})
    assert not any(callable(getattr(ats_profiles, n)) and n.startswith(("default", "fallback")) for n in dir(ats_profiles))


def test_the_local_operator_helper_if_present_used_exactly_this_policy():
    """Optional local integration helper must use the same execution policy."""
    import importlib.util
    helper_path = Path(__file__).resolve().parents[1] / "scratchpad" / "local_profile_helper.py"
    if not helper_path.exists():
        pytest.skip("local operational helper not present")
    spec = importlib.util.spec_from_file_location("validated_run_helper", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    endpoint = f"https://{helper.HOST}/api/non-user-graphql"
    assert helper.HOST == ASHBY_HOSTED.host
    assert helper.READS == ASHBY_HOSTED.graphql_reads and helper.FORM_WRITES == ASHBY_HOSTED.graphql_form_writes
    assert helper.NONFATAL_BLOCKED_WRITES == ASHBY_HOSTED.nonfatal_blocked_writes
    assert tuple((endpoint, n) for n in helper.CV_UPLOAD_OPERATIONS) == ASHBY_HOSTED.cv_upload_mutations
    assert helper.CV_STORAGE_WRITES == ASHBY_HOSTED.cv_storage_writes
    assert tuple((endpoint, n) for n in helper.CV_ASSOCIATION_OPERATIONS) == ASHBY_HOSTED.cv_association_mutations
