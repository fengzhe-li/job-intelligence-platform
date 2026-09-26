from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from jobintel.models.job import Location
from jobintel.models.taxonomy import WorkMode


def fetch_json(url: str, timeout_seconds: int = 20, headers: dict[str, str] | None = None, data: dict[str, Any] | None = None) -> Any:
    # `data` (JSON-serialised as the request body) switches this to a POST --
    # needed for Workday's CXS endpoint, which its own careers-site frontend
    # calls with POST + a JSON search body (confirmed live, Phase 2.7). Every
    # other caller omits `data` and gets the original GET behaviour unchanged.
    body = json.dumps(data).encode("utf-8") if data is not None else None
    default_headers = {"Accept": "application/json", "User-Agent": "jobintel/0.1"}
    if data is not None:
        default_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers or default_headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url: str, timeout_seconds: int = 20, headers: dict[str, str] | None = None) -> str:
    request = urllib.request.Request(url, headers=headers or {"Accept": "text/html", "User-Agent": "jobintel/0.1"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8", errors="ignore")


def extract_json_ld(html_text: str, schema_type: str) -> dict[str, Any] | None:
    """Extract the first `<script type="application/ld+json">` block whose
    `@type` matches `schema_type` (e.g. "JobPosting") -- real, standards-based
    structured data sites publish for search engines, not a hidden/private API.
    """
    for match in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html_text, re.DOTALL | re.IGNORECASE):
        try:
            # strict=False: tolerate literal control characters (e.g. raw
            # newlines) inside string values -- invalid per strict JSON, but a
            # common real-world pattern in server-rendered JSON-LD (confirmed
            # live on prospects.ac.uk during the Phase 2.5 source audit) that
            # browsers' JSON.parse tolerates. Rejecting it here would silently
            # treat a real, valid-in-practice JobPosting block as "not found".
            data = json.loads(match.group(1).strip(), strict=False)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == schema_type:
                return item
    return None


def json_ld_organisation_name(value: Any) -> str:
    """`hiringOrganization` in a schema.org JobPosting can be a plain string or
    an `Organization`/`Person` object -- handle both, same as Prospects'
    detail-page parsing (connectors/prospects.py) needed live."""
    if isinstance(value, dict):
        return _text(value.get("name"))
    return _text(value)


def json_ld_location_text(value: Any) -> str:
    """`jobLocation` in a schema.org JobPosting can be a plain string, a single
    `Place`, or a list of several `Place`s for a multi-site role -- handle all
    three. Returns "" (never a fabricated default) when nothing usable is
    found; callers should fall back to their own default, if any."""
    if isinstance(value, list) and value:
        texts = [text for text in (_json_ld_place_text(place) for place in value) if text]
        return "; ".join(dict.fromkeys(texts))
    if isinstance(value, dict):
        return _json_ld_place_text(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _json_ld_place_text(place: Any) -> str:
    if not isinstance(place, dict):
        return ""
    address = place.get("address", place)
    if isinstance(address, str):
        return address.strip()
    if isinstance(address, dict):
        parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
        return ", ".join(str(part) for part in parts if part)
    return ""


def text_or_none(value: Any) -> str | None:
    """Legacy closure-scope fallback helper: a non-empty identifier or None."""
    text = _text(value)
    return text or None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</\s*p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def parse_datetime(value: str | int | float | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(timestamp, timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def build_url(base: str, params: dict[str, Any]) -> str:
    return f"{base}?{urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})}"


def normalise_location(raw: str | None) -> Location:
    value = (raw or "United Kingdom").strip()
    lowered = value.casefold()
    work_mode = WorkMode.UNKNOWN
    if "remote" in lowered:
        work_mode = WorkMode.REMOTE
    elif "hybrid" in lowered:
        work_mode = WorkMode.HYBRID
    elif "onsite" in lowered or "on-site" in lowered:
        work_mode = WorkMode.ONSITE

    uk_tokens = ("united kingdom", "uk", "england", "scotland", "wales", "london", "manchester", "cambridge", "oxford")
    country = "United Kingdom" if any(token in lowered for token in uk_tokens) else value
    city = None
    region = None
    known_cities = (
        "London",
        "Manchester",
        "Cambridge",
        "Oxford",
        "Bristol",
        "Edinburgh",
        "Glasgow",
        "Leeds",
        "Reading",
        "Birmingham",
        "Liverpool",
        "Sheffield",
        "Cardiff",
        "Belfast",
        "Newcastle",
    )
    region_markers = {
        "greater london": ("London", "Greater London"),
        "city of london": ("London", "Greater London"),
        "central london": ("London", "Greater London"),
        "scotland": (None, "Scotland"),
        "wales": (None, "Wales"),
        "england": (None, "England"),
    }
    for marker, (marker_city, marker_region) in region_markers.items():
        if marker in lowered:
            city = marker_city or city
            region = marker_region
            break
    for known in known_cities:
        if known.casefold() in lowered:
            city = known
            break
    if city == "London" and region is None:
        region = "Greater London"
    if city is None and "," in value:
        city = value.split(",", 1)[0].strip() or None
    if "remote" in lowered and city is None:
        country = "United Kingdom" if country == value and any(token in lowered for token in ("uk", "united kingdom")) else country
    return Location(city=city, country=country, region=region, work_mode=work_mode, raw=value)


def salary_text(minimum: Any, maximum: Any, currency: str | None = None) -> str | None:
    if minimum in (None, "") and maximum in (None, ""):
        return None
    prefix = f"{currency} " if currency else ""
    if minimum not in (None, "") and maximum not in (None, ""):
        return f"{prefix}{minimum}-{maximum}"
    return f"{prefix}{minimum or maximum}"
