from __future__ import annotations

from jobintel.models.job import Job
from jobintel.models.taxonomy import LocationMode


def score_location(job: Job, mode: LocationMode, london_weight: float) -> tuple[float, str]:
    has_london = any(location.is_london and location.is_uk for location in job.locations)
    has_uk = any(location.is_uk for location in job.locations)

    if mode == LocationMode.LONDON_ONLY:
        return (1.0, "London role") if has_london else (-1.0, "Not London")
    if mode == LocationMode.UK_WIDE:
        return (0.7, "UK role") if has_uk else (-1.0, "Outside target UK market")
    if has_london:
        return (1.0, "London role")
    if has_uk:
        return (london_weight, "UK role outside London")
    return -1.0, "Outside target UK market"

