from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobintel.models.candidate import CandidateProfile
from jobintel.models.taxonomy import EvidenceSourceType
from jobintel.profile_ingestion import add_profile_source, build_candidate_profile
from jobintel.storage.repository_store import RepositoryStore, RepositorySyncState

GITHUB_API = "https://api.github.com"
GITHUB_USERNAME_ENV = "JOBINTEL_GITHUB_USERNAME"


def extract_github_username(url_or_handle: str | None) -> str | None:
    """Safely extract a GitHub username from a profile URL or handle without hardcoding.
    Never prints or logs the contact value."""
    if not url_or_handle:
        return None
    cleaned = url_or_handle.strip().rstrip("/")
    if not cleaned:
        return None
    if "github.com/" in cleaned:
        parts = cleaned.split("github.com/")
        if len(parts) > 1:
            segment = parts[1].strip().split("/")[0].split("?")[0].split("#")[0]
            return segment if segment else None
    elif "/" not in cleaned and not cleaned.startswith("http"):
        return cleaned
    return None


def resolve_github_username(
    candidate: CandidateProfile | None = None,
    store_root: Path | str | None = None,
) -> str | None:
    """Resolve GitHub username without requiring manual duplicate env configuration.
    Precedence:
    1. JOBINTEL_GITHUB_USERNAME environment variable
    2. candidate.github_url if candidate is provided
    3. candidate_profile.json at store_root
    """
    import os
    env_username = os.getenv(GITHUB_USERNAME_ENV)
    if env_username and env_username.strip():
        return env_username.strip()
    if candidate and candidate.github_url:
        parsed = extract_github_username(candidate.github_url)
        if parsed:
            return parsed
    if store_root is not None:
        from jobintel.profile_ingestion import load_candidate_profile
        loaded = load_candidate_profile(store_root)
        if loaded and loaded.github_url:
            parsed = extract_github_username(loaded.github_url)
            if parsed:
                return parsed
    return None


def check_github_freshness(
    store_root: Path | str,
    candidate: CandidateProfile | None = None,
) -> str:
    """Check the freshness state of GitHub repository evidence in the Evidence Bank.
    Possible states:
    - 'not_configured': No GitHub username configured or detectable in profile.
    - 'current': Repositories have been synced and local evidence is up to date.
    - 'stale': Repositories were previously synced, but sync hasn't run recently or is out of date.
    """
    username = resolve_github_username(candidate=candidate, store_root=store_root)
    if not username:
        return "not_configured"
    root = Path(store_root)
    repo_store = RepositoryStore(root)
    state = repo_store.read_state()
    if not state:
        return "stale"
    return "current"


@dataclass(frozen=True)
class RepositoryMetadata:
    full_name: str
    default_branch: str
    pushed_at: str
    description: str
    html_url: str


@dataclass(frozen=True)
class SyncResult:
    repositories_discovered: int
    repositories_synced: int
    repositories_unchanged: int
    capabilities_extracted: int
    synced_repository_names: list[str]


def discover_repositories(username: str, token: str | None = None, limit: int = 100) -> list[RepositoryMetadata]:
    """List a GitHub account's own (non-fork) public repositories.

    No hardcoded repository list: this is what lets new repos the candidate adds
    later become eligible for matching automatically, per the dynamic-discovery
    requirement. Uses only the documented public GitHub REST API.
    """
    repositories: list[RepositoryMetadata] = []
    page = 1
    per_page = min(100, max(1, limit))
    while len(repositories) < limit:
        url = f"{GITHUB_API}/users/{username}/repos?per_page={per_page}&page={page}&sort=updated&type=owner"
        items = _fetch_json(url, token)
        if not items:
            break
        for item in items:
            if item.get("fork"):
                continue
            repositories.append(
                RepositoryMetadata(
                    full_name=item["full_name"],
                    default_branch=item.get("default_branch") or "main",
                    pushed_at=item.get("pushed_at") or item.get("updated_at") or "",
                    description=item.get("description") or "",
                    html_url=item.get("html_url") or f"https://github.com/{item['full_name']}",
                )
            )
            if len(repositories) >= limit:
                break
        if len(items) < per_page:
            break
        page += 1
    return repositories


def sync_repositories(
    store_root: Path | str,
    username: str,
    token: str | None = None,
    limit: int = 100,
    name: str = "Personal Candidate",
    graduation_year: int = 2026,
) -> SyncResult:
    """Incrementally sync a GitHub account's repositories into the Evidence Bank.

    Only repos whose `pushed_at` timestamp has advanced since the last recorded
    sync are re-fetched (README + latest commit SHA); unchanged repos are skipped
    so repeated syncs stay cheap and don't hit GitHub's unauthenticated rate limit.
    README extraction reuses `profile_ingestion.extract_capabilities` via the same
    manifest/rebuild path as manually imported sources -- no separate extraction logic.
    """
    root = Path(store_root)
    repo_store = RepositoryStore(root)
    state = repo_store.read_state()
    repositories = discover_repositories(username, token, limit)

    languages_cache = _read_languages_cache(root)
    synced_names: list[str] = []
    for repo in repositories:
        existing = state.get(repo.full_name)
        if existing is not None and existing.last_synced_at >= repo.pushed_at:
            continue
        latest_sha = _fetch_latest_commit_sha(repo.full_name, repo.default_branch, token)
        readme_text = _fetch_readme_text(repo.full_name, token)
        languages_cache[repo.full_name] = _fetch_languages(repo.full_name, token)
        cache_path = _cache_path(root, repo.full_name)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        content = readme_text or repo.description or f"# {repo.full_name}\n\n{repo.description}"
        cache_path.write_text(content, encoding="utf-8")
        add_profile_source(root, cache_path, EvidenceSourceType.GITHUB_REPOSITORY_SYNC, title=repo.full_name)
        evidence_source_id = f"github:{repo.full_name}"
        state[repo.full_name] = RepositorySyncState(
            full_name=repo.full_name,
            last_synced_sha=latest_sha,
            last_synced_at=repo.pushed_at or datetime.now(timezone.utc).isoformat(),
            evidence_source_id=evidence_source_id,
        )
        synced_names.append(repo.full_name)

    repo_store.write_state(state)
    _write_languages_cache(root, languages_cache)

    profile: CandidateProfile | None = None
    if synced_names:
        profile = build_candidate_profile(root, name=name, graduation_year=graduation_year)

    return SyncResult(
        repositories_discovered=len(repositories),
        repositories_synced=len(synced_names),
        repositories_unchanged=len(repositories) - len(synced_names),
        capabilities_extracted=len(profile.capabilities) if profile else 0,
        synced_repository_names=synced_names,
    )


def _cache_path(root: Path, full_name: str) -> Path:
    safe_name = full_name.replace("/", "__")
    return root / "profile" / "github_cache" / f"{safe_name}.md"


def _languages_cache_path(root: Path) -> Path:
    return root / "profile" / "github_languages.json"


def _read_languages_cache(root: Path) -> dict[str, dict[str, int]]:
    path = _languages_cache_path(root)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_languages_cache(root: Path, cache: dict[str, dict[str, int]]) -> None:
    path = _languages_cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _fetch_languages(full_name: str, token: str | None) -> dict[str, int]:
    url = f"{GITHUB_API}/repos/{full_name}/languages"
    try:
        payload = _fetch_json(url, token)
    except urllib.error.HTTPError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _fetch_latest_commit_sha(full_name: str, default_branch: str, token: str | None) -> str | None:
    url = f"{GITHUB_API}/repos/{full_name}/commits/{default_branch}"
    try:
        payload = _fetch_json(url, token)
    except urllib.error.HTTPError:
        return None
    if isinstance(payload, dict):
        return payload.get("sha")
    return None


def _fetch_readme_text(full_name: str, token: str | None) -> str:
    url = f"{GITHUB_API}/repos/{full_name}/readme"
    headers = _headers(token, accept="application/vnd.github.raw")
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return ""
        raise


def _fetch_json(url: str, token: str | None) -> Any:
    request = urllib.request.Request(url, headers=_headers(token, accept="application/vnd.github+json"))
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _headers(token: str | None, accept: str) -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": "jobintel-github-sync/0.1", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
