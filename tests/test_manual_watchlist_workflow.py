from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobintel.dashboard.operations import (
    WATCHLIST_ALWAYS_MANUAL,
    WATCHLIST_PARTIALLY_MANUAL,
    build_manual_watchlist_view,
)
from jobintel.storage.local_store import LocalJobStore
from jobintel.storage.watchlist_store import WatchlistStore

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _registry(tmp: str) -> Path:
    path = Path(tmp) / "registry.json"
    entries = [
        {"company_name": "Acme Co", "connector_type": None, "enabled": False, "verification_status": "pending_ats_discovery", "careers_url": "https://acme.example/careers"},
    ]
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


class WatchlistStoreTests(unittest.TestCase):
    def test_record_check_sets_next_due_from_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WatchlistStore(tmp)
            record = store.record_check("trackr", "No relevant jobs", frequency_days=7, now=NOW)

        self.assertEqual(record["last_checked_at"], NOW.isoformat())
        self.assertEqual(record["next_check_due"], (NOW + timedelta(days=7)).isoformat())
        self.assertIsNone(record["snoozed_until"])

    def test_checking_again_clears_a_previous_snooze(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WatchlistStore(tmp)
            store.snooze("trackr", NOW + timedelta(days=30))
            record = store.record_check("trackr", "checked", now=NOW)

        self.assertIsNone(record["snoozed_until"])

    def test_snooze_persists_until_a_future_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WatchlistStore(tmp)
            record = store.snooze("gradcracker", NOW + timedelta(days=14), notes="no relevant categories this month")

        self.assertEqual(record["snoozed_until"], (NOW + timedelta(days=14)).isoformat())
        self.assertEqual(record["notes"], "no relevant categories this month")

    def test_set_frequency_is_editable_independent_of_the_recommended_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WatchlistStore(tmp)
            store.record_check("trackr", "checked", frequency_days=30, now=NOW)
            updated = store.set_frequency("trackr", 3, now=NOW)

        self.assertEqual(updated["check_frequency_days"], 3)

    def test_reads_reflect_persisted_state_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            WatchlistStore(tmp).record_check("trackr", "checked", now=NOW)
            reloaded = WatchlistStore(tmp).get("trackr")

        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded["check_result"], "checked")


class ManualWatchlistViewTests(unittest.TestCase):
    def test_prospects_appears_as_partially_manual_not_pretended_manual_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp)
            store = LocalJobStore(Path(tmp) / "store")
            items = build_manual_watchlist_view(store, registry_path=registry, now=NOW)

        prospects = next(item for item in items if item.target == "prospects")
        self.assertEqual(prospects.state, WATCHLIST_PARTIALLY_MANUAL)

    def test_trackr_gradcracker_bright_network_are_always_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp)
            store = LocalJobStore(Path(tmp) / "store")
            items = build_manual_watchlist_view(store, registry_path=registry, now=NOW)

        by_target = {item.target: item for item in items}
        for name in ("trackr", "gradcracker", "bright_network"):
            self.assertEqual(by_target[name].state, WATCHLIST_ALWAYS_MANUAL)

    def test_never_checked_item_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp)
            store = LocalJobStore(Path(tmp) / "store")
            items = build_manual_watchlist_view(store, registry_path=registry, now=NOW)

        acme = next(item for item in items if item.target == "Acme Co")
        self.assertTrue(acme.is_due)
        self.assertIsNone(acme.last_checked_at)

    def test_recently_checked_item_with_future_due_date_is_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp)
            store = LocalJobStore(Path(tmp) / "store")
            watchlist_store = WatchlistStore(store.root)
            watchlist_store.record_check("Acme Co", "no relevant jobs", frequency_days=30, now=NOW)
            items = build_manual_watchlist_view(store, registry_path=registry, watchlist_store=watchlist_store, now=NOW)

        acme = next(item for item in items if item.target == "Acme Co")
        self.assertFalse(acme.is_due)
        self.assertEqual(acme.check_result, "no relevant jobs")

    def test_snoozed_item_is_not_due_even_without_a_check_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp)
            store = LocalJobStore(Path(tmp) / "store")
            watchlist_store = WatchlistStore(store.root)
            watchlist_store.snooze("Acme Co", NOW + timedelta(days=10))
            items = build_manual_watchlist_view(store, registry_path=registry, watchlist_store=watchlist_store, now=NOW)

        acme = next(item for item in items if item.target == "Acme Co")
        self.assertTrue(acme.is_snoozed)
        self.assertFalse(acme.is_due)

    def test_expired_snooze_becomes_due_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry(tmp)
            store = LocalJobStore(Path(tmp) / "store")
            watchlist_store = WatchlistStore(store.root)
            watchlist_store.snooze("Acme Co", NOW - timedelta(days=1))
            items = build_manual_watchlist_view(store, registry_path=registry, watchlist_store=watchlist_store, now=NOW)

        acme = next(item for item in items if item.target == "Acme Co")
        self.assertFalse(acme.is_snoozed)
        self.assertTrue(acme.is_due)

    def test_auto_verified_companies_never_appear_on_the_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "registry.json"
            registry_path.write_text(json.dumps([
                {"company_name": "Verified Co", "connector_type": "greenhouse", "connector_token": "x", "enabled": True, "verification_status": "verified"},
            ]))
            store = LocalJobStore(Path(tmp) / "store")
            items = build_manual_watchlist_view(store, registry_path=registry_path, now=NOW)

        self.assertNotIn("Verified Co", {item.target for item in items})


if __name__ == "__main__":
    unittest.main()
