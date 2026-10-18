"""Reviewed per-ATS execution profiles for supervised local_browser runs.

A profile is the exact network policy one ATS vendor's hosted application form needs, taken
from a real supervised run and reviewed: the GraphQL reads, the per-field autosave mutation,
the third-party telemetry that may be blocked without stopping the run, and the three exact
operations of the approved CV-upload window. It holds no candidate data and no application-,
company- or job-specific value.

Lookup is by the application URL's exact host (https, no port, no credentials, no case or
trailing-dot variant, no sub/parent domain). There is no default or fallback profile: a host
without a reviewed profile is refused before anything is approved, queued or started. A new
vendor gets a profile only after its own supervised run has been reviewed.

A profile never relaxes anything by itself. `SubmissionService.execute` validates every value
against the approved application URL exactly as it does for hand-supplied options.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


class UnknownExecutionProfile(ValueError):
    """No reviewed execution profile exists for this application URL's exact host."""


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    host: str
    graphql_reads: tuple[tuple[str, str], ...]
    graphql_form_writes: tuple[tuple[str, str], ...]
    nonfatal_blocked_writes: tuple[tuple[str, str], ...]
    cv_upload_mutations: tuple[tuple[str, str], ...]
    cv_storage_writes: tuple[tuple[str, str], ...]
    cv_association_mutations: tuple[tuple[str, str], ...]
    validated: str
    # Same-host resume-parse upload (Lever-style); see browserbase_runner.CvUploadPolicy.parse_writes.
    cv_parse_writes: tuple[tuple[str, str], ...] = ()
    # Page-load bot-management / CAPTCHA bootstrap requests that must pass for the form to load
    # (browserbase_runner.validate_page_security_writes). Never a challenge answer; never application data.
    page_security_writes: tuple[tuple[str, str, str], ...] = ()
    # False until ONE reviewed, human-approved supervised live run has validated this profile. An
    # unvalidated profile routes jobs as "supported, pending live validation" but apply_one refuses to
    # start it unless the caller explicitly opts into that one validation run.
    live_validated: bool = True

    def execute_options(self) -> dict:
        """The live-host options for `SubmissionService.execute` (validated there, unchanged)."""
        return {
            "allow_live_host": True,
            "live_host": self.host,
            "live_write_allowlist": (),
            "live_graphql_reads": self.graphql_reads,
            "live_graphql_form_writes": self.graphql_form_writes,
            "live_nonfatal_blocked_writes": self.nonfatal_blocked_writes,
            "live_cv_upload": True,
            "live_cv_upload_mutations": self.cv_upload_mutations,
            "live_cv_storage_writes": self.cv_storage_writes,
            "live_cv_association_mutations": self.cv_association_mutations,
            **({"live_cv_parse_writes": self.cv_parse_writes} if self.cv_parse_writes else {}),
            **({"live_page_security_writes": self.page_security_writes} if self.page_security_writes else {}),
        }


_ASHBY_GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql"

# Exactly the policy of the first successful supervised run on an Ashby-hosted form (2026-10-11):
# identity verified, deterministic fields filled, CV handle -> storage -> association -> visible,
# no fatal blocked operation, final Submit never clicked.
ASHBY_HOSTED = ExecutionProfile(
    name="ashby_hosted",
    host="jobs.ashbyhq.com",
    graphql_reads=(
        (_ASHBY_GRAPHQL, "ApiJobPosting"),
        (_ASHBY_GRAPHQL, "ApiOrganizationFromHostedJobsPageName"),
        (_ASHBY_GRAPHQL, "ApiAutocompleteGeoLocation"),
    ),
    graphql_form_writes=((_ASHBY_GRAPHQL, "ApiSetFormValue"),),
    # Third-party RUM telemetry and browser-generated CSP reports: still aborted, recorded, non-fatal.
    nonfatal_blocked_writes=(("POST", "browser-intake-datadoghq.com"), ("POST", "csp.withgoogle.com")),
    cv_upload_mutations=((_ASHBY_GRAPHQL, "ApiCreateFileUploadHandle"),),
    cv_storage_writes=(("POST", "ashbyhq-infra-prd-main-app-uploaded-files-us-east-1.s3.us-east-1.amazonaws.com"),),
    cv_association_mutations=((_ASHBY_GRAPHQL, "ApiSetFormValueToFile"),),
    validated="2026-10-11: one real supervised Ashby-hosted application reached human handoff",
)

# Lever-hosted application form (audited 2026-10-13 from a public apply page and its scripts):
# one multipart HTML form on jobs.lever.co/<site>/<posting>/apply; selecting the resume POSTs it once to
# /parseResume (same host) which autofills only untouched fields; location suggestions are GET
# /searchLocations; per-posting "cards" questions; the final form POST sits behind hCaptcha and is only
# ever the human's. No GraphQL, no third-party storage. Every other write stays fatal.
# Page load requires three observed writes: the
# same-host Cloudflare challenge-platform beacon, hCaptcha's site-config bootstrap (api/api2/bare host)
# and a Google Analytics hit. The first two now pass as page-security infrastructure only (no challenge
# answer, no application data); analytics stays blocked but non-fatal. Any other CAPTCHA write stops
# the run for the human (captcha_requires_human).
LEVER_HOSTED = ExecutionProfile(
    name="lever_hosted",
    host="jobs.lever.co",
    graphql_reads=(),
    graphql_form_writes=(),
    nonfatal_blocked_writes=(("POST", "www.google-analytics.com"),),
    cv_upload_mutations=(),
    cv_storage_writes=(),
    cv_association_mutations=(),
    cv_parse_writes=(("POST", "https://jobs.lever.co/parseResume"),),
    page_security_writes=(("POST", "jobs.lever.co", "/cdn-cgi/challenge-platform/"),
                          ("POST", "hcaptcha.com", "/checksiteconfig"),
                          ("POST", "api.hcaptcha.com", "/checksiteconfig"),
                          ("POST", "api2.hcaptcha.com", "/checksiteconfig")),
    validated="Fixture-tested; live validation pending for page-load security writes",
    live_validated=False,
)

# Greenhouse-hosted job board form on job-boards.greenhouse.io (the legacy
# boards.greenhouse.io URL redirects here, see submission/hosted_urls): one React form;
# page load writes only Snowplow analytics (blocked, non-fatal); selecting the resume presigns with a GET and
# POSTs the file once to the board's S3 bucket (the only allowed write, inside the CV window, sealed bytes in
# its body). reCAPTCHA Enterprise sits on the final submit and is only ever the human's. Every other write
# stays fatal. See `validated` for the supervised-handoff validation boundary.
GREENHOUSE_HOSTED = ExecutionProfile(
    name="greenhouse_hosted",
    host="job-boards.greenhouse.io",
    graphql_reads=(),
    graphql_form_writes=(),
    nonfatal_blocked_writes=(("POST", "c.spl.greenhouse.io"),),
    cv_upload_mutations=(),
    cv_storage_writes=(("POST", "grnhse-prod-jben-us-east-1.s3.amazonaws.com"),),
    cv_association_mutations=(),
    validated="Supervised handoff validated: country/city controls and sealed CV upload; final Submit never clicked",
    live_validated=True,
)

# Workable-hosted application form on apply.workable.com. The short /j/<code>/apply URL
# redirects to /<account>/j/<code>/apply; see submission/hosted_urls:
# a single-page React form; page load writes only the same-host Cloudflare challenge-platform beacon (page
# security, as Lever); selecting the resume POSTs the file to Workable's S3 bucket (the only allowed write,
# once, inside the CV window, sealed bytes in its body); Rollbar error reports are blocked but non-fatal.
# Every other write stays fatal; final Submit is only ever the human's. NOT yet validated live.
WORKABLE_HOSTED = ExecutionProfile(
    name="workable_hosted",
    host="apply.workable.com",
    graphql_reads=(),
    graphql_form_writes=(),
    # Google Analytics collect beacons (region host) are blocked but non-fatal, as Lever's www host.
    nonfatal_blocked_writes=(("POST", "api.rollbar.com"), ("POST", "region1.google-analytics.com")),
    cv_upload_mutations=(),
    cv_storage_writes=(("POST", "workable-application-form.s3.us-east-1.amazonaws.com"),),
    cv_association_mutations=(),
    page_security_writes=(("POST", "apply.workable.com", "/cdn-cgi/challenge-platform/"),),
    validated=("Fixture-tested; FAILED validation: live validation incomplete. Unlisted analytics and human verification can prevent "
               "CV association. A form with Turnstile is held for an explicit human-verification decision; "
               "awaiting a successful supervised validation run"),
    live_validated=False,
)

PROFILES: dict[str, ExecutionProfile] = {ASHBY_HOSTED.host: ASHBY_HOSTED, LEVER_HOSTED.host: LEVER_HOSTED,
                                         GREENHOUSE_HOSTED.host: GREENHOUSE_HOSTED, WORKABLE_HOSTED.host: WORKABLE_HOSTED}


def profile_for_url(application_url: str, profiles: dict[str, ExecutionProfile] | None = None) -> ExecutionProfile:
    """The reviewed profile for the URL's exact host, or UnknownExecutionProfile. No fallback."""
    profiles = PROFILES if profiles is None else profiles
    parts = urlsplit(application_url or "")
    if parts.scheme != "https" or "@" in parts.netloc or ":" in parts.netloc:
        raise UnknownExecutionProfile("Application URL must be plain https on its default port to match a profile")
    profile = profiles.get(parts.netloc)  # exact: no case-folding, trailing dot, suffix or subdomain match
    if profile is None or profile.host != parts.netloc:
        raise UnknownExecutionProfile(
            f"No reviewed execution profile for host {parts.netloc!r}; supervised runs are refused for unreviewed hosts")
    return profile
