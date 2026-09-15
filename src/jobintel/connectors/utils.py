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


def fetch_json(url: str, timeout_seconds: int = 20, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, headers=headers or {"Accept": "application/json", "User-Agent": "jobintel/0.1"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


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
