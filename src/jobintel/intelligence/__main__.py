"""Compare a single stored JD with its optional advisory intelligence result."""
import argparse
import json
from pathlib import Path

from jobintel.config import load_ranking_config
from jobintel.intelligence.service import IntelligenceService
from jobintel.profile_ingestion import load_candidate_profile
from jobintel.storage.local_store import LocalJobStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--store", default="data/local")
    parser.add_argument("--config", default="config/personal_strategy.json")
    args = parser.parse_args()
    store = LocalJobStore(args.store)
    job = next((j for j in store.read_jobs() if j.id == args.job_id), None)
    profile = load_candidate_profile(args.store)
    if job is None or profile is None:
        parser.error("A real stored job and candidate profile are required.")
    result = IntelligenceService(cache_dir=Path(args.store) / "intelligence_cache").analyze(job, profile, load_ranking_config(args.config))
    print(json.dumps(result.to_dict(), default=str, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
