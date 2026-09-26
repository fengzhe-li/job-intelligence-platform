from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jobintel.models.application import Application
from jobintel.models.taxonomy import ApplicationStatus
from jobintel.storage.application_store import ApplicationStore


class ApplicationTrackerTests(unittest.TestCase):
    def test_create_application_defaults_to_discovered_and_records_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ApplicationStore(tmp)
            application = Application(id="app-1", job_id="job-1", company="Example Co", role_title="Graduate Software Engineer")
            created = store.create_application(application)

            self.assertEqual(created.status, ApplicationStatus.DISCOVERED)
            self.assertIsNotNone(created.discovered_at)

            history = store.status_history_for("app-1")
            self.assertEqual(len(history), 1)
            self.assertIsNone(history[0].from_status)
            self.assertEqual(history[0].to_status, ApplicationStatus.DISCOVERED)

    def test_update_status_appends_history_without_mutating_prior_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ApplicationStore(tmp)
            store.create_application(Application(id="app-1", job_id="job-1", company="Example Co", role_title="Graduate Software Engineer"))

            store.update_status("app-1", ApplicationStatus.SHORTLISTED, note="Looks like a strong fit")
            updated = store.update_status("app-1", ApplicationStatus.APPLIED, note="Submitted via company site")

            self.assertEqual(updated.status, ApplicationStatus.APPLIED)
            self.assertIsNotNone(updated.applied_at)

            history = store.status_history_for("app-1")
            self.assertEqual(len(history), 3)
            self.assertEqual([event.to_status for event in history], [ApplicationStatus.DISCOVERED, ApplicationStatus.SHORTLISTED, ApplicationStatus.APPLIED])
            self.assertEqual(history[2].from_status, ApplicationStatus.SHORTLISTED)
            self.assertEqual(history[2].note, "Submitted via company site")

    def test_read_applications_round_trips_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ApplicationStore(tmp)
            store.create_application(Application(id="app-1", job_id="job-1", company="Example Co", role_title="Graduate Software Engineer"))
            store.update_status("app-1", ApplicationStatus.ONLINE_ASSESSMENT)

            reloaded = ApplicationStore(tmp).read_applications()

        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].status, ApplicationStatus.ONLINE_ASSESSMENT)

    def test_update_status_unknown_application_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ApplicationStore(tmp)
            with self.assertRaises(ValueError):
                store.update_status("does-not-exist", ApplicationStatus.APPLIED)


if __name__ == "__main__":
    unittest.main()
