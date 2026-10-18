"""The application URL a hosted ATS actually serves the form on.

Greenhouse's legacy board URL https://boards.greenhouse.io/<board>/jobs/<id> answers with a server-side
redirect to https://job-boards.greenhouse.io/<board>/jobs/<id> (same query). The runner treats any
main-frame host change as fatal (host drift), so an application must be approved, sealed and run on
the host that really serves the form. Only this exact, observed mapping is applied; every other URL is
returned unchanged.

Workable's short URL https://apply.workable.com/j/<shortcode>/apply redirects to the account's form
https://apply.workable.com/<account>/j/<shortcode>/apply; the account (its public subdomain, recorded by the
connector) is required to name it, and is never guessed.
"""
from __future__ import annotations

import re

_GREENHOUSE_LEGACY = re.compile(r"^https://boards\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/(\d+)(\?[^#]*)?$")
_WORKABLE_SHORT = re.compile(r"^https://apply\.workable\.com/j/([A-Za-z0-9]+)(?:/apply)?/?$")
_ACCOUNT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def hosted_application_url(url: str | None, *, workable_account: str | None = None) -> str | None:
    short = _WORKABLE_SHORT.match(url or "")
    if short:
        if not workable_account or not _ACCOUNT.fullmatch(workable_account):
            return url
        return f"https://apply.workable.com/{workable_account}/j/{short.group(1)}/apply"
    match = _GREENHOUSE_LEGACY.match(url or "")
    if not match:
        return url
    board, job_id, query = match.groups()
    return f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}{query or ''}"
