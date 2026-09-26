from __future__ import annotations

import tempfile
import unittest

from jobintel.profile_ingestion import build_candidate_profile, read_contact_details, set_contact_details


class ContactDetailsTests(unittest.TestCase):
    def test_set_and_read_contact_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            set_contact_details(tmp, email="candidate@example.com", phone="07700 900123", github_url="https://github.com/fengzhe-li")
            contact = read_contact_details(tmp)

        self.assertEqual(contact["email"], "candidate@example.com")
        self.assertEqual(contact["phone"], "07700 900123")
        self.assertEqual(contact["github_url"], "https://github.com/fengzhe-li")
        self.assertNotIn("linkedin_url", contact)

    def test_partial_updates_preserve_previously_set_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            set_contact_details(tmp, email="a@example.com", phone="0700000000")
            set_contact_details(tmp, location="London, UK")
            contact = read_contact_details(tmp)

        self.assertEqual(contact["email"], "a@example.com")
        self.assertEqual(contact["phone"], "0700000000")
        self.assertEqual(contact["location"], "London, UK")

    def test_build_candidate_profile_applies_saved_contact_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            set_contact_details(tmp, email="a@example.com", phone="0700000000", location="London, UK", github_url="https://github.com/example")
            profile = build_candidate_profile(tmp, name="Test Candidate")

        self.assertEqual(profile.email, "a@example.com")
        self.assertEqual(profile.phone, "0700000000")
        self.assertEqual(profile.location, "London, UK")
        self.assertEqual(profile.github_url, "https://github.com/example")
        self.assertIsNone(profile.linkedin_url)

    def test_unset_contact_details_are_never_fabricated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = build_candidate_profile(tmp, name="Test Candidate")

        self.assertIsNone(profile.email)
        self.assertIsNone(profile.phone)
        self.assertIsNone(profile.location)
        self.assertIsNone(profile.linkedin_url)
        self.assertIsNone(profile.github_url)


if __name__ == "__main__":
    unittest.main()
