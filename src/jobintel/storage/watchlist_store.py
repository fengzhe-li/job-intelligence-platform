from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class WatchlistStore:
    """Tracks manual-check state for the manual/partial-coverage watchlist
    (Phase 3 Section J) -- "which of the 46+ manual-required companies and
    manual/partial sources have I actually checked, and when do I need to
    check again." Current-state JSON, same convention as LocalJobStore's
    workflow_status.json/source_health.json -- each check overwrites the
    previous record for that target, it isn't an append-only audit log,
    since only the current due-date/result is actionable here.
    """

    def __init__(self, root: Path | str = "data/local") -> None:
        self.root = Path(root)
        self.path = self.root / "processed" / "manual_watchlist_checks.json"

    def read_all(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def get(self, target: str) -> dict[str, Any] | None:
        return self.read_all().get(target)

    def record_check(self, target: str, result: str, notes: str = "", frequency_days: int | None = None, now: datetime | None = None) -> dict[str, Any]:
        """An explicit human action: "I checked this today." Clears any
        snooze (checking it supersedes a snooze) and schedules the next check
        `frequency_days` out (kept from the previous record, or the caller's
        default, if not given explicitly this call)."""
        now = now or datetime.now(timezone.utc)
        records = self.read_all()
        existing = records.get(target, {})
        days = frequency_days if frequency_days is not None else existing.get("check_frequency_days", 30)
        record = {
            "target": target,
            "last_checked_at": now.isoformat(),
            "check_result": result,
            "notes": notes or existing.get("notes", ""),
            "check_frequency_days": days,
            "next_check_due": (now + timedelta(days=days)).isoformat(),
            "snoozed_until": None,
        }
        records[target] = record
        self._write(records)
        return record

    def snooze(self, target: str, until: datetime, notes: str = "") -> dict[str, Any]:
        records = self.read_all()
        existing = records.get(target, {"target": target, "last_checked_at": None, "check_result": "", "check_frequency_days": 30, "next_check_due": None})
        existing = {**existing, "target": target, "snoozed_until": until.isoformat()}
        if notes:
            existing["notes"] = notes
        records[target] = existing
        self._write(records)
        return existing

    def set_frequency(self, target: str, days: int, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        records = self.read_all()
        existing = records.get(target, {"target": target, "last_checked_at": None, "check_result": "", "notes": "", "snoozed_until": None})
        last_checked = existing.get("last_checked_at")
        anchor = datetime.fromisoformat(last_checked) if last_checked else now
        existing = {**existing, "target": target, "check_frequency_days": days, "next_check_due": (anchor + timedelta(days=days)).isoformat()}
        records[target] = existing
        self._write(records)
        return existing

    def _write(self, records: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
