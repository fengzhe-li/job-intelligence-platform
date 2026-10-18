"""Batch candidate pool: "Remote" alone never implies the UK; explicit UK country/region/city evidence
is required, and same-named non-UK places never count."""
import pytest

from jobintel.pipeline.batch_select import is_uk_market


@pytest.mark.parametrize("location", [
    "London, United Kingdom", "London Hybrid", "United Kingdom Remote, Dublin, Spain, United States",
    "UK Remote, Portugal, Ireland", "Remote - UK", "Remote (United Kingdom)", "Edinburgh, Scotland",
    "Manchester (GB) / London", "Remote, New York, Poland, London, Amsterdam", "Cardiff, Wales",
    "Lisboa, Lisboa, Portugal, London, United Kingdom", "Cambridge, England",
])
def test_explicit_uk_evidence_is_kept(location):
    assert is_uk_market(location)


@pytest.mark.parametrize("location", [
    "Remote", "Remote Remote", "Dublin Remote", "Paris Remote", "Berlin Metropolitain Area Remote",
    "EMEA Remote", "Europe (Remote)", "Remote, New York, Brazil, Paris, Poland, Amsterdam", "United States Remote",
    "Sydney, New South Wales", "Cambridge, MA", "Boston / Cambridge, Massachusetts", "London, Ontario, Canada",
    "Reading, PA", "Birmingham, AL", "New England Remote", "", None,
])
def test_remote_alone_or_non_uk_places_never_count(location):
    assert not is_uk_market(location)
