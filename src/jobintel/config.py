from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from jobintel.models.taxonomy import (
    GraduationYearState,
    LocationMode,
    RoleTrack,
    SponsorshipState,
)


@dataclass(frozen=True)
class RankingConfig:
    role_preferences: dict[RoleTrack, float]
    source_weights: dict[str, float]
    freshness_half_life_days: float
    sponsorship_weights: dict[SponsorshipState, float]
    graduation_year_weights: dict[GraduationYearState, float]
    location_mode: LocationMode
    london_preference_weight: float
    excluded_occupations: tuple[str, ...]
    target_markets: tuple[str, ...]
    graduation_year: int = 2026


def default_ranking_config() -> RankingConfig:
    return RankingConfig(
        role_preferences={
            RoleTrack.SOFTWARE_ENGINEERING: 0.85,
            RoleTrack.BACKEND_ENGINEERING: 1.0,
            RoleTrack.FRONTEND_ENGINEERING: 0.75,
            RoleTrack.FULL_STACK_ENGINEERING: 0.85,
            RoleTrack.DATA_ENGINEERING: 1.0,
            RoleTrack.DATA_PLATFORM: 1.0,
            RoleTrack.CLOUD_ENGINEERING: 0.9,
            RoleTrack.PLATFORM_ENGINEERING: 0.9,
            RoleTrack.INFRASTRUCTURE_ENGINEERING: 0.75,
            RoleTrack.DEVOPS_SRE: 0.8,
            RoleTrack.AI_ML_ENGINEERING: 0.8,
            RoleTrack.TELECOMMUNICATIONS: 0.75,
            RoleTrack.NETWORK_ENGINEERING: 0.8,
            RoleTrack.NETWORK_SOFTWARE: 0.9,
            RoleTrack.INTERNET_ENGINEERING: 0.75,
            RoleTrack.ELECTRONIC_COMMUNICATION: 0.65,
            RoleTrack.IOT: 0.75,
            RoleTrack.EMBEDDED_SOFTWARE: 0.75,
            RoleTrack.EDGE_CONNECTED_SYSTEMS: 0.75,
        },
        source_weights={
            "company": 1.0,
            "greenhouse": 0.95,
            "lever": 0.95,
            "adzuna": 0.7,
            "welcome_to_the_jungle": 0.85,
            "linkedin": 0.6,
            "indeed": 0.55,
        },
        freshness_half_life_days=14.0,
        sponsorship_weights={
            SponsorshipState.EXPLICIT_SPONSOR: 1.0,
            SponsorshipState.LIKELY_SPONSOR: 0.7,
            SponsorshipState.UNKNOWN: 0.2,
            SponsorshipState.LIKELY_NO_SPONSOR: -0.5,
            SponsorshipState.EXPLICIT_NO_SPONSOR: -1.0,
        },
        graduation_year_weights={
            GraduationYearState.NO_YEAR_STATED: 0.8,
            GraduationYearState.GRADUATE_FRIENDLY: 0.8,
            GraduationYearState.YEAR_2026_ACCEPTED: 1.0,
            GraduationYearState.YEAR_2027_MENTIONED: -0.35,
            GraduationYearState.YEAR_2027_ONLY_STRICT: -1.0,
            GraduationYearState.OTHER_YEAR_RESTRICTION: -0.5,
        },
        location_mode=LocationMode.LONDON_FIRST_UK_WIDE,
        london_preference_weight=0.55,
        excluded_occupations=(
            "sales",
            "front desk",
            "marketing",
            "accounting",
            "investment banking analyst",
            "civil engineer",
            "mechanical engineer",
            "chemical engineer",
            "power systems engineer",
            "high voltage",
        ),
        target_markets=("United Kingdom",),
    )


def load_ranking_config(path: Path | str = "config/personal_strategy.json") -> RankingConfig:
    config = default_ranking_config()
    path = Path(path)
    if not path.exists():
        return config
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RankingConfig(
        role_preferences=config.role_preferences,
        source_weights={**config.source_weights, **payload.get("source_weights", {})},
        freshness_half_life_days=payload.get("freshness_half_life_days", config.freshness_half_life_days),
        sponsorship_weights=config.sponsorship_weights,
        graduation_year_weights={
            key: payload.get("graduation_year_weights", {}).get(key.value, value)
            for key, value in config.graduation_year_weights.items()
        },
        location_mode=LocationMode(payload.get("location_mode", config.location_mode.value)),
        london_preference_weight=payload.get("london_preference_weight", config.london_preference_weight),
        excluded_occupations=config.excluded_occupations,
        target_markets=(payload.get("primary_market", "United Kingdom"),),
        graduation_year=payload.get("graduation_year", config.graduation_year),
    )
