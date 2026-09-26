"""Regression: the real Dashboard "Regenerate CV" route must produce the
structured six-section CV, and "View PDF" must serve that exact version.

Dogfood failure: a dashboard process started before the CV-structure change
kept the old modules in memory and persisted a new immutable version with the
legacy flat layout, which the workbench then showed as the latest CV. The
dashboard now refuses to generate once its source changed since start-up.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import re
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader

from jobintel.dashboard import server as dashboard_server
from jobintel.dashboard.server import _handler
from jobintel.matching.cv_document import CV_DOCUMENT_FORMAT
from jobintel.profile_ingestion import _write_profile
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from test_cv_document_structure import _candidate, _job

JOB_ID = "job-structure"


class DashboardCVGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        LocalJobStore(root).write_jobs([_job()])
        _write_profile(root, _candidate())
        self.root = root
        self._start()

    def _start(self) -> None:
        handler = _handler(str(self.root), "config/personal_strategy.json", "config/target_companies.json")
        self.server = dashboard_server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def _request(self, method: str, path: str, form: dict | None = None) -> tuple[int, dict, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=60)
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, data

    def test_dashboard_regenerate_produces_structured_cv_and_view_pdf_serves_that_exact_version(self) -> None:
        status, headers, _ = self._request("POST", "/cv/generate?lang=en", {"job_id": JOB_ID})
        self.assertEqual(status, 303)

        artifacts = CVArtifactStore(self.root).for_job(JOB_ID)
        self.assertEqual(len(artifacts), 1)
        artifact = artifacts[0]
        self.assertEqual(artifact.document_format, CV_DOCUMENT_FORMAT)
        self.assertEqual(artifact.document["sections_missing"], [])
        self.assertTrue(any(project["emphasis"] == "focus" for project in artifact.document["projects"]))

        # The workbench the redirect lands on offers View PDF for this exact version.
        status, _, page = self._request("GET", headers["location"])
        self.assertEqual(status, 200)
        view_links = re.findall(rb"/cv/pdf\?id=(cv-[0-9a-f]{12})", page)
        self.assertEqual(view_links[0].decode(), artifact.id)

        status, headers, pdf = self._request("GET", f"/cv/pdf?id={artifact.id}")
        self.assertEqual(status, 200)
        self.assertIn(artifact.id, headers["content-disposition"])
        self.assertEqual(hashlib.sha256(pdf).hexdigest(), artifact.pdf_sha256)

        reader = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 1)
        width, height = (round(float(value)) for value in reader.pages[0].mediabox[2:])
        self.assertEqual((width, height), (595, 842))  # A4
        text = reader.pages[0].extract_text()
        for heading in ("PROFESSIONAL SUMMARY", "EDUCATION", "TECHNICAL SKILLS", "KEY PROJECTS", "INTERNSHIP EXPERIENCE"):
            self.assertIn(heading, text)
        self.assertIn("Programming:", text)  # grouped, not one flat keyword list
        self.assertNotIn("…", text)
        self.assertNotIn("...", text)

    def test_regenerate_creates_a_new_version_and_view_pdf_keeps_serving_each_exact_version(self) -> None:
        self._request("POST", "/cv/generate?lang=en", {"job_id": JOB_ID})
        self._request("POST", "/cv/generate?lang=en", {"job_id": JOB_ID})
        first, second = sorted(CVArtifactStore(self.root).for_job(JOB_ID), key=lambda artifact: artifact.generated_at)

        self.assertNotEqual(first.id, second.id)
        for artifact in (first, second):
            status, headers, pdf = self._request("GET", f"/cv/pdf?id={artifact.id}")
            self.assertEqual(status, 200)
            self.assertIn(artifact.id, headers["content-disposition"])
            self.assertEqual(hashlib.sha256(pdf).hexdigest(), artifact.pdf_sha256)
        status, _, page = self._request("GET", f"/cv?job_id={JOB_ID}&lang=en")
        self.assertEqual(re.findall(rb"/cv/pdf\?id=(cv-[0-9a-f]{12})", page)[0].decode(), second.id)

    def test_stale_dashboard_refuses_to_generate_after_source_changed(self) -> None:
        with patch.object(dashboard_server, "_source_fingerprint", return_value="changed-on-disk"):
            status, _, body = self._request("POST", "/cv/generate?lang=en", {"job_id": JOB_ID})

        self.assertEqual(status, 409)
        self.assertIn(b"restart the dashboard", body)
        self.assertEqual(CVArtifactStore(self.root).for_job(JOB_ID), [])


if __name__ == "__main__":
    unittest.main()
