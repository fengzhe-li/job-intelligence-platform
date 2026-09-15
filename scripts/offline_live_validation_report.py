from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.company_registry import load_company_registry, registry_summary
from jobintel.config import load_ranking_config
from jobintel.connectors.adzuna import AdzunaConnector
from jobintel.connectors.base import RawJobPayload
from jobintel.connectors.greenhouse import GreenhouseConnector
from jobintel.connectors.lever import LeverConnector
from jobintel.dedup.v1 import deduplicate_jobs
from jobintel.matching.matcher import match_job
from jobintel.profile_ingestion import load_candidate_profile


NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)


def main() -> None:
    profile = load_candidate_profile("data/local")
    if profile is None:
        raise SystemExit("No profile found. Run profile-import/profile-rebuild first.")
    config = load_ranking_config()
    jobs = _fixture_jobs(config.graduation_year)
    results = sorted(
        [(job, match_job(profile, job, config, NOW)) for job in jobs],
        key=lambda item: item[1].overall_priority,
        reverse=True,
    )
    _write_report(profile, results)


def _fixture_jobs(graduation_year: int):
    greenhouse = GreenhouseConnector(("fixture-greenhouse",))
    lever = LeverConnector(("fixture-lever",))
    adzuna = AdzunaConnector("fixture", "fixture")
    raw_jobs = [
        _greenhouse_raw(
            "gh-data-platform",
            "Software Engineer - Data Platform",
            "Fixture FinTech",
            "London, United Kingdom",
            "Build Python and SQL services for a data platform with PostgreSQL and Docker. Visa sponsorship is available. No specific graduation year requirement.",
        ),
        _lever_raw(
            "lv-fullstack",
            "Full-stack Engineer",
            "Fixture SaaS",
            "London, United Kingdom",
            "Build React and TypeScript frontend features plus FastAPI backend APIs with PostgreSQL. Sponsorship unknown.",
        ),
        _greenhouse_raw(
            "gh-network",
            "Network Software Engineer",
            "Fixture Internet Infra",
            "Cambridge, United Kingdom",
            "Develop Python network automation and TCP/IP packet-processing software for internet edge systems. Recent graduates welcome.",
        ),
        _adzuna_raw(
            "adz-telecom",
            "Telecommunications Software Engineer",
            "Fixture Telecom",
            "Manchester, United Kingdom",
            "Work on 5G telecommunications platforms, Linux services and communication network tooling. Must have right to work in the UK without sponsorship.",
        ),
        _lever_raw(
            "lv-2027",
            "2027 Software Engineering Graduate Programme",
            "Fixture Bank",
            "London, United Kingdom",
            "Python graduate programme. 2027 graduates only. Applications require graduating in 2027.",
        ),
        _adzuna_raw(
            "adz-frontend",
            "Frontend Engineer",
            "Fixture Product",
            "Remote - UK",
            "Create React and TypeScript UI components for analytics workflows. No graduation year requirement stated.",
        ),
        _greenhouse_raw(
            "gh-embedded",
            "IoT Embedded Software Engineer",
            "Fixture Connected Systems",
            "Oxford, United Kingdom",
            "Develop C++ embedded software for IoT connected systems using Linux and MQTT. Sponsorship unknown.",
        ),
    ]
    connector_by_source = {"greenhouse": greenhouse, "lever": lever, "adzuna": adzuna}
    jobs = [connector_by_source[raw.source_name].normalise(raw) for raw in raw_jobs]
    return [enrich_job(job, graduation_year) for job in deduplicate_jobs(jobs)]


def _greenhouse_raw(job_id: str, title: str, company: str, location: str, content: str) -> RawJobPayload:
    payload = {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://fixtures.example/greenhouse/{job_id}",
        "content": f"<p>{content}</p>",
        "offices": [{"name": location, "location": location}],
        "updated_at": NOW.isoformat(),
        "_board_token": company,
        "_company": company,
    }
    return RawJobPayload("greenhouse", job_id, payload["absolute_url"], payload["absolute_url"], payload, NOW, NOW)


def _lever_raw(job_id: str, title: str, company: str, location: str, description: str) -> RawJobPayload:
    payload = {
        "id": job_id,
        "text": title,
        "hostedUrl": f"https://fixtures.example/lever/{job_id}",
        "applyUrl": f"https://fixtures.example/lever/{job_id}/apply",
        "createdAt": int(NOW.timestamp() * 1000),
        "categories": {"location": location},
        "descriptionPlain": description,
        "lists": [],
        "_site": company,
    }
    return RawJobPayload("lever", job_id, payload["hostedUrl"], payload["applyUrl"], payload, NOW, NOW)


def _adzuna_raw(job_id: str, title: str, company: str, location: str, description: str) -> RawJobPayload:
    payload = {
        "id": job_id,
        "title": title,
        "company": {"display_name": company},
        "description": description,
        "redirect_url": f"https://fixtures.example/adzuna/{job_id}",
        "created": NOW.isoformat(),
        "location": {"display_name": location, "area": ["UK", location.split(",", 1)[0]]},
    }
    return RawJobPayload("adzuna", job_id, payload["redirect_url"], payload["redirect_url"], payload, NOW, NOW)


def _write_report(profile, results) -> None:
    companies = load_company_registry()
    summary = registry_summary(companies)
    role_counts: Counter[str] = Counter()
    sponsorship_counts: Counter[str] = Counter()
    graduation_counts: Counter[str] = Counter()
    location_counts: Counter[str] = Counter()
    cross_track_count = 0
    for job, match in results:
        for track in match.primary_role_tracks + match.secondary_role_tracks:
            role_counts[track.value] += 1
        sponsorship_counts[job.sponsorship.state.value if job.sponsorship else "unknown"] += 1
        graduation_counts[job.graduation_year.state.value if job.graduation_year else "unknown"] += 1
        if any(location.is_london for location in job.locations):
            location_counts["london"] += 1
        elif any(location.is_uk for location in job.locations):
            location_counts["rest_of_uk"] += 1
        else:
            location_counts["outside_uk"] += 1
        if len(match.primary_role_tracks + match.secondary_role_tracks) > 1:
            cross_track_count += 1

    lines = [
        "# Live Job Validation",
        "",
        "This report was generated offline because the current environment could not reach public Greenhouse/Lever ATS hosts. It does not claim that live jobs were fetched.",
        "",
        "## Completed Offline Validation",
        "",
        f"- registry companies configured: {summary['companies_configured']}",
        f"- enabled live connectors: {summary['enabled_companies']}",
        f"- pending source verifications: {summary['pending_verification_companies']}",
        f"- imported candidate evidence sources: {len(profile.cv_versions) + len(profile.projects)} profile records from {len(_source_ids(profile))} evidence sources",
        f"- extracted candidate capabilities: {len(profile.capabilities)}",
        f"- fixture jobs normalised/ranked: {len(results)}",
        f"- fixture cross-track opportunities: {cross_track_count}",
        "",
        "## Live Validation Not Executed",
        "",
        "- Public ATS network access failed in this environment during Greenhouse/Lever probing.",
        "- Therefore raw live jobs fetched: 0.",
        "- No live source quality conclusions are claimed.",
        "",
        "## Offline Fixture Metrics",
        "",
        f"- London vs rest-of-UK: {dict(location_counts)}",
        f"- role-track distribution: {dict(role_counts)}",
        f"- sponsorship distribution: {dict(sponsorship_counts)}",
        f"- graduation-year distribution: {dict(graduation_counts)}",
        "",
        "## Fixture-Based Ranking Examples",
        "",
    ]
    for job, match in results:
        technical = next(component.score for component in match.components if component.name == "technical_match")
        location = "; ".join(location.raw or "" for location in job.locations)
        lines.extend(
            [
                f"### {job.title} - {job.company}",
                "",
                f"- source-shaped fixture: {job.source_observations[0].source_name}",
                f"- location: {location}",
                f"- work mode: {', '.join(sorted({location.work_mode.value for location in job.locations}))}",
                f"- direct application URL: {job.canonical_application_url}",
                f"- posted / first seen: {job.posted_at.isoformat() if job.posted_at else 'unknown'} / {job.source_observations[0].first_seen_at.isoformat()}",
                f"- role tracks: {[track.value for track in match.primary_role_tracks + match.secondary_role_tracks]}",
                f"- sponsorship: {job.sponsorship.state.value if job.sponsorship else 'unknown'}",
                f"- graduation-year: {job.graduation_year.state.value if job.graduation_year else 'unknown'}",
                f"- technical match: {technical}",
                f"- application priority: {match.overall_priority}",
                f"- recommended CV: {match.best_existing_cv_category}",
                f"- hybrid CV: {match.hybrid_cv_recommended}",
                f"- strongest evidence: {match.strongest_supporting_evidence[:3]}",
                f"- missing/weak skills: {match.missing_or_weak_evidence[:5]}",
                f"- explanation: {match.explanation}",
                "",
            ]
        )
    lines.extend(
        [
            "## Exact Live Commands To Run On A Networked Machine",
            "",
            "After verifying a Greenhouse board token or Lever site token, set `enabled` to `true`, set `source_connector_type` to `greenhouse` or `lever`, set `verification_status` to `verified`, and fill the token field in `config/target_companies.json`.",
            "",
            "```bash",
            "PYTHONPATH=src python3 -m jobintel.cli.main registry-summary",
            "PYTHONPATH=src python3 -m jobintel.cli.main ingest-registry --limit 100",
            "PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --limit 30",
            "PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --sponsorship sponsor_only --limit 30",
            "PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --location-mode uk_wide --limit 30",
            "PYTHONPATH=src python3 -m jobintel.cli.main validation-report --output docs/PHASE3_VALIDATION.md",
            "PYTHONPATH=src python3 -m jobintel.cli.main new-jobs --since 2026-08-25T00:00:00+00:00 --limit 30",
            "```",
            "",
            "## Expected Artifacts After Live Run",
            "",
            "- `data/local/raw_snapshots/*.jsonl`: raw source payloads",
            "- `data/local/processed/canonical_jobs.json`: deduplicated canonical jobs",
            "- `docs/PHASE3_VALIDATION.md`: measured live validation metrics",
            "- CLI shortlist output from `list-ranked`",
            "",
            "## Issues To Watch During Live Run",
            "",
            "- Some company careers pages may not use public Greenhouse/Lever feeds.",
            "- Greenhouse/Lever tokens may differ from company names and must be verified before enabling.",
            "- Some feeds may expose global roles; UK relevance depends on location normalisation.",
            "- Sponsorship is often unknown because many descriptions do not mention it.",
            "- Graduate-year wording can be sparse; unknown should remain visible rather than being treated as rejection.",
        ]
    )
    Path("docs/LIVE_JOB_VALIDATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _source_ids(profile) -> set[str]:
    ids = set()
    ids.update(cv.evidence_source.id for cv in profile.cv_versions)
    for project in profile.projects:
        ids.update(source.id for source in project.evidence_sources)
    for capability in profile.capabilities.values():
        ids.update(evidence.source.id for evidence in capability.evidence)
    return ids


if __name__ == "__main__":
    main()
