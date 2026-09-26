# Dynamic GitHub Repository Discovery

## What It Does

`jobintel.github_sync` syncs a GitHub account's own (non-fork) public repositories into the candidate Evidence Bank. There is no hardcoded repository list — new repositories the candidate adds later are picked up automatically on the next sync, and existing repositories are only re-processed when they've actually changed.

```bash
PYTHONPATH=src python3 -m jobintel.cli.main github-sync --username fengzhe-li --limit 100
```

## How It Works

```text
GitHub account (public REST API)
  -> discover_repositories()        # GET /users/{username}/repos, forks excluded
  -> compare pushed_at against RepositoryStore state
  -> for new/changed repos only:
       fetch latest commit SHA      # GET /repos/{full_name}/commits/{default_branch}
       fetch README (raw)           # GET /repos/{full_name}/readme
       write to data/local/profile/github_cache/{repo}.md
       register as a profile source (source_type = github_repository_sync)
  -> build_candidate_profile()      # same extraction path as manual imports
```

README extraction reuses `profile_ingestion.extract_capabilities` — the same skill-pattern matching manual CV/project imports use — so there is no separate/parallel extraction logic to keep in sync. Each synced repository becomes a `Project` with `repository_full_name` set, and its extracted capabilities carry `CapabilityEvidence.project_id` pointing back to that project, which is what `matching.project_selection` uses to score projects against a specific job.

## Incremental Sync

`storage.repository_store.RepositoryStore` persists per-repository sync state (`data/local/profile/repositories.json`): the last-synced commit SHA and timestamp. A repository is only re-fetched when its GitHub `pushed_at` has advanced past the last recorded sync — unrelated, unchanged repositories are skipped, which keeps repeated syncs cheap and friendly to GitHub's unauthenticated rate limit (60 requests/hour).

## Authentication

No authentication is required to sync a public account's repositories. For a higher rate limit, set a GitHub token:

```bash
export GITHUB_TOKEN="..."
PYTHONPATH=src python3 -m jobintel.cli.main github-sync --username fengzhe-li --token-env GITHUB_TOKEN
```

## Known Limitations / Extension Points

- Polling only (no webhook-triggered sync). A webhook receiver calling `sync_repositories` for a single repo is a natural future extension on top of the same incremental-sync logic — not built in this phase.
- Fork repositories are excluded by default (not the candidate's own original work).
- Private repositories are not synced without a token with appropriate scope; this project has not been used against private repos and that path is untested.
- README-only extraction: language/dependency-file parsing (e.g. `package.json`, `pyproject.toml`) for stronger technology evidence is a possible later improvement, not implemented here.
