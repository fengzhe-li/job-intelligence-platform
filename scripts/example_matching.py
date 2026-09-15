from __future__ import annotations

from datetime import datetime, timezone

from jobintel.config import default_ranking_config
from jobintel.fixtures.sample_data import sample_candidate, sample_jobs
from jobintel.matching.matcher import match_job


def main() -> None:
    candidate = sample_candidate()
    config = default_ranking_config()
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    results = sorted(
        (match_job(candidate, job, config, now) for job in sample_jobs()),
        key=lambda result: result.overall_priority,
        reverse=True,
    )
    for result in results[:5]:
        print(f"{result.overall_priority:>5} | {result.title} | {result.company}")
        print(f"      tracks: {[track.value for track in result.primary_role_tracks + result.secondary_role_tracks]}")
        print(f"      cv: {result.best_existing_cv_category}, hybrid={result.hybrid_cv_recommended}")
        print(f"      evidence: {result.strongest_supporting_evidence[:2]}")
        print(f"      weak: {result.missing_or_weak_evidence}")


if __name__ == "__main__":
    main()

