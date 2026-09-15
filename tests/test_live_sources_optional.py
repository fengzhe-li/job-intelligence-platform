from __future__ import annotations

import os
import unittest

from jobintel.connectors.adzuna import AdzunaConnector
from jobintel.connectors.base import ConnectorQuery


@unittest.skipUnless(os.getenv("RUN_LIVE_SOURCE_TESTS") == "1", "live source tests are opt-in")
class OptionalLiveSourceTests(unittest.TestCase):
    def test_adzuna_live_search_when_credentials_are_available(self) -> None:
        connector = AdzunaConnector(os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY"))
        health = connector.health_check()
        self.assertTrue(health.ok)
        jobs = connector.fetch_jobs(ConnectorQuery(keywords=("python developer",), location="London", limit=1))
        self.assertLessEqual(len(jobs), 1)


if __name__ == "__main__":
    unittest.main()

