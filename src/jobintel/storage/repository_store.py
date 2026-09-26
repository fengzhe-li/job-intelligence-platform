from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RepositorySyncState:
    full_name: str
    last_synced_sha: str | None
    last_synced_at: str
    evidence_source_id: str


class RepositoryStore:
    """Persists per-repository GitHub sync state so re-syncs are incremental.

    A repository is only re-fetched (README + languages) when its latest commit SHA
    differs from what's recorded here, which is what lets `github_sync.sync_repositories`
    pick up new/changed repos automatically without reprocessing everything each run.
    """

    def __init__(self, root: Path | str = "data/local") -> None:
        self.root = Path(root)
        self.path = self.root / "profile" / "repositories.json"

    def read_state(self) -> dict[str, RepositorySyncState]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return {key: _state_from_dict(value) for key, value in payload.items()}

    def write_state(self, states: dict[str, RepositorySyncState]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: _state_to_dict(value) for key, value in states.items()}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _state_to_dict(state: RepositorySyncState) -> dict[str, Any]:
    return {
        "full_name": state.full_name,
        "last_synced_sha": state.last_synced_sha,
        "last_synced_at": state.last_synced_at,
        "evidence_source_id": state.evidence_source_id,
    }


def _state_from_dict(payload: dict[str, Any]) -> RepositorySyncState:
    return RepositorySyncState(
        full_name=payload["full_name"],
        last_synced_sha=payload.get("last_synced_sha"),
        last_synced_at=payload["last_synced_at"],
        evidence_source_id=payload["evidence_source_id"],
    )
