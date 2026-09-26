"""Regression: profile-rebuild on a store whose sources.json was rewritten by
`promote_staged_evidence` (portable {id, source_type, title, uri} entries, no
`path`) crashed with KeyError: 'path' and, if it had not, would have replaced
curated evidence with raw keyword extraction."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobintel.profile_ingestion import build_candidate_profile, set_contact_details

PROMOTED_SOURCES = [
    {"id": "personal-source-1", "source_type": "cv_text", "title": "Software CV", "uri": "cv/software_cv.docx"},
    {"id": "personal-source-2", "source_type": "readme_markdown", "title": "example/repo", "uri": "https://github.com/example/repo"},
]

CANONICAL_PROFILE = {
    "id": "personal",
    "name": "Real Name",
    "graduation_year": 2025,
    "email": "old@example.com",
    "phone": "0700000000",
    "location": "London, UK",
    "linkedin_url": None,
    "github_url": None,
    "evidence_sources": [
        {"id": "personal-source-2", "source_type": "readme_markdown", "title": "example/repo", "uri": "https://github.com/example/repo"},
        {"id": "personal-source-1", "source_type": "cv_text", "title": "Software CV", "uri": "cv/software_cv.docx"},
    ],
    "capabilities": [
        {
            "name": "Python",
            "category": "language",
            "role_relevance": {},
            "technology": "Python",
            "domain": None,
            "last_verified": None,
            "evidence_tier": "partial",
            "evidence": [
                {
                    "id": "curated-python-1",
                    "source_id": "personal-source-2",
                    "quote": "Built the ingestion service in Python.",
                    "project_id": "personal-source-2",
                    "confidence": 0.9,
                    "category": None,
                    "claim_type": "implementation",
                    "source_file": "README.md",
                    "source_location": "## Architecture",
                    "confidence_tier": "high",
                    "safe_paraphrase_scope": "Python service implementation",
                    "forbidden_extrapolations": ["production scale"],
                    "freshness": None,
                }
            ],
        }
    ],
    "cv_versions": [{"id": "personal-source-1", "category": "software", "text": "CV text", "source_id": "personal-source-1"}],
    "projects": [
        {
            "id": "personal-source-2",
            "name": "Repo",
            "description": "Curated description",
            "evidence_source_ids": ["personal-source-2"],
            "repository_full_name": "example/repo",
            "role_relevance": {},
            "aliases": ["repo-alias"],
            "forbidden_extrapolations": ["no users"],
            "track_mappings": [],
        }
    ],
}


class PromotedProfileRebuildTests(unittest.TestCase):
    def _store(self, tmp: str, sources: list[dict]) -> Path:
        profile_dir = Path(tmp) / "profile"
        profile_dir.mkdir(parents=True)
        (profile_dir / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
        (profile_dir / "candidate_profile.json").write_text(json.dumps(CANONICAL_PROFILE), encoding="utf-8")
        return profile_dir

    def test_rebuild_of_promoted_store_keeps_curated_evidence_and_applies_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = self._store(tmp, PROMOTED_SOURCES)
            set_contact_details(tmp, github_url="https://github.com/example")
            profile = build_candidate_profile(tmp, "Real Name", 2025)
            written = json.loads((profile_dir / "candidate_profile.json").read_text(encoding="utf-8"))

        self.assertEqual(profile.github_url, "https://github.com/example")
        # Fields not in contact.json keep their curated value.
        self.assertEqual(profile.email, "old@example.com")
        self.assertEqual(profile.location, "London, UK")
        expected = {**CANONICAL_PROFILE, "github_url": "https://github.com/example"}
        self.assertEqual(written, expected)

    def test_promoted_store_with_new_raw_import_refuses_instead_of_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = {"path": str(Path(tmp) / "new.md"), "source_type": "readme_markdown", "title": "new"}
            profile_dir = self._store(tmp, PROMOTED_SOURCES + [raw])
            with self.assertRaisesRegex(ValueError, "promoted curated sources"):
                build_candidate_profile(tmp, "Real Name", 2025)
            written = json.loads((profile_dir / "candidate_profile.json").read_text(encoding="utf-8"))

        self.assertEqual(written, CANONICAL_PROFILE)


if __name__ == "__main__":
    unittest.main()
