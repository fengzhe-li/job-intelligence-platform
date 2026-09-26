from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobintel.github_sync import RepositoryMetadata, discover_repositories, sync_repositories
from jobintel.profile_ingestion import load_candidate_profile
from jobintel.storage.repository_store import RepositoryStore


REPO_PAYLOAD = [
    {
        "full_name": "fengzhe-li/example-backend",
        "fork": False,
        "default_branch": "main",
        "pushed_at": "2026-08-01T00:00:00Z",
        "description": "A Python backend project",
        "html_url": "https://github.com/fengzhe-li/example-backend",
    },
    {
        "full_name": "fengzhe-li/forked-repo",
        "fork": True,
        "default_branch": "main",
        "pushed_at": "2026-08-01T00:00:00Z",
        "description": "Not the candidate's own work",
        "html_url": "https://github.com/fengzhe-li/forked-repo",
    },
]


class GithubDiscoveryTests(unittest.TestCase):
    def test_discover_repositories_filters_forks(self) -> None:
        with patch("jobintel.github_sync._fetch_json", side_effect=[REPO_PAYLOAD, []]):
            repositories = discover_repositories("fengzhe-li", token=None, limit=10)

        self.assertEqual(len(repositories), 1)
        self.assertEqual(repositories[0].full_name, "fengzhe-li/example-backend")


class GithubSyncTests(unittest.TestCase):
    def test_sync_writes_evidence_source_and_extracts_capabilities(self) -> None:
        repo = RepositoryMetadata(
            full_name="fengzhe-li/example-backend",
            default_branch="main",
            pushed_at="2026-08-01T00:00:00Z",
            description="A Python backend project",
            html_url="https://github.com/fengzhe-li/example-backend",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("jobintel.github_sync.discover_repositories", return_value=[repo]),
                patch("jobintel.github_sync._fetch_latest_commit_sha", return_value="abc123"),
                patch("jobintel.github_sync._fetch_readme_text", return_value="Built with Python, PostgreSQL, and Docker."),
                patch("jobintel.github_sync._fetch_languages", return_value={"Python": 9000, "Dockerfile": 200}),
            ):
                result = sync_repositories(tmp, "fengzhe-li", limit=10)

            self.assertEqual(result.repositories_discovered, 1)
            self.assertEqual(result.repositories_synced, 1)
            self.assertEqual(result.repositories_unchanged, 0)
            self.assertGreater(result.capabilities_extracted, 0)

            profile = load_candidate_profile(tmp)
            self.assertIsNotNone(profile)
            self.assertIn("python", profile.capabilities)
            self.assertTrue(any(project.repository_full_name == "fengzhe-li/example-backend" for project in profile.projects))

            state = RepositoryStore(tmp).read_state()
            self.assertIn("fengzhe-li/example-backend", state)
            self.assertEqual(state["fengzhe-li/example-backend"].last_synced_sha, "abc123")

    def test_language_api_discovers_capability_outside_the_fixed_skill_keyword_list(self) -> None:
        # "Zig" is not in analysis.role_tracks.SKILL_PATTERNS and never appears in
        # the README text -- it can only enter the Evidence Bank via GitHub's own
        # structured language-breakdown API, with real provenance (byte share).
        repo = RepositoryMetadata(
            full_name="fengzhe-li/systems-project",
            default_branch="main",
            pushed_at="2026-08-01T00:00:00Z",
            description="A systems project",
            html_url="https://github.com/fengzhe-li/systems-project",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("jobintel.github_sync.discover_repositories", return_value=[repo]),
                patch("jobintel.github_sync._fetch_latest_commit_sha", return_value="abc123"),
                patch("jobintel.github_sync._fetch_readme_text", return_value="A low-level systems project."),
                patch("jobintel.github_sync._fetch_languages", return_value={"Zig": 8000, "Makefile": 100}),
            ):
                sync_repositories(tmp, "fengzhe-li", limit=10)

            profile = load_candidate_profile(tmp)

        self.assertIn("zig", profile.capabilities)
        zig = profile.capabilities["zig"]
        self.assertTrue(zig.evidence)
        self.assertEqual(zig.evidence[0].project_id, "personal-source-1")
        # A tiny, noise-level language share (Makefile, ~1.2%) must not become a
        # "verified" capability -- provenance/evidence-strength still applies.
        self.assertNotIn("makefile", profile.capabilities)

    def test_second_sync_skips_unchanged_repository(self) -> None:
        repo = RepositoryMetadata(
            full_name="fengzhe-li/example-backend",
            default_branch="main",
            pushed_at="2026-08-01T00:00:00Z",
            description="A Python backend project",
            html_url="https://github.com/fengzhe-li/example-backend",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("jobintel.github_sync.discover_repositories", return_value=[repo]),
                patch("jobintel.github_sync._fetch_latest_commit_sha", return_value="abc123") as mock_sha,
                patch("jobintel.github_sync._fetch_readme_text", return_value="Built with Python.") as mock_readme,
                patch("jobintel.github_sync._fetch_languages", return_value={"Python": 9000}),
            ):
                sync_repositories(tmp, "fengzhe-li", limit=10)
                self.assertEqual(mock_readme.call_count, 1)

                second_result = sync_repositories(tmp, "fengzhe-li", limit=10)

                self.assertEqual(mock_readme.call_count, 1, "unchanged repo should not be re-fetched")
                self.assertEqual(second_result.repositories_synced, 0)
                self.assertEqual(second_result.repositories_unchanged, 1)


if __name__ == "__main__":
    unittest.main()
