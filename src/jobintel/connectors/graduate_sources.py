from __future__ import annotations

import urllib.parse
from typing import Callable

from jobintel.connectors.manual_source import ManualSourceSpec


def _domain_matcher(*domains: str) -> Callable[[str], bool]:
    domain_set = {domain.casefold() for domain in domains}

    def matches(url: str) -> bool:
        netloc = urllib.parse.urlparse(url.strip()).netloc.casefold()
        netloc = netloc.removeprefix("www.")
        return netloc in domain_set

    return matches


# None of these UK graduate-focused boards expose a documented public jobs API
# (unlike Greenhouse/Lever/Ashby/Workable/SmartRecruiters/WelcomeKit). Per the
# project's compliance stance, they are supported only via manual/discovery
# import (see docs/SOURCE_COVERAGE.md) rather than scraping application pages.
# Prospects is the one exception with a live discovery connector -- see
# connectors/prospects.py -- but its ManualSourceSpec below stays as a fallback.
#
# CORRECTED during the Phase 2.5 source audit: the real site is the-trackr.com /
# app.the-trackr.com (hyphenated). thetrackr.com (no hyphen) is an unrelated
# Shopify storefront -- the original domain here was simply wrong, so no manual
# import against the real site could ever have validated before this fix.
TRACKR = ManualSourceSpec(
    source_name="trackr",
    display_name="The Trackr",
    is_source_url=_domain_matcher("the-trackr.com", "app.the-trackr.com"),
)

GRADCRACKER = ManualSourceSpec(
    source_name="gradcracker",
    display_name="Gradcracker",
    is_source_url=_domain_matcher("gradcracker.com"),
)

BRIGHT_NETWORK = ManualSourceSpec(
    source_name="bright_network",
    display_name="Bright Network",
    is_source_url=_domain_matcher("brightnetwork.co.uk"),
)

PROSPECTS = ManualSourceSpec(
    source_name="prospects",
    display_name="Prospects",
    is_source_url=_domain_matcher("prospects.ac.uk"),
)

GRADUATE_SOURCE_SPECS: dict[str, ManualSourceSpec] = {
    spec.source_name: spec for spec in (TRACKR, GRADCRACKER, BRIGHT_NETWORK, PROSPECTS)
}
