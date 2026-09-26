"""Regression from a real PDF review: under page pressure the fitter kept wide
18mm margins while deleting structurally useful content -- a degree lost its
whole module line and an internship was left as a heading with zero bullets.

Structural floors: every displayed degree keeps a (concise) supporting detail
line, every displayed internship keeps >= 1 grounded bullet. Denser layout
presets are used before useful content is deleted, but never just to shrink
the margins.
"""
from __future__ import annotations

import tempfile
import unittest

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth

from jobintel.matching.cv_generation import MIN_PAGE_UTILIZATION, generate_cv
from jobintel.matching.cv_render import LAYOUT_PRESETS
from test_cv_document_structure import EXTRA_PROJECTS, PROJECT_EVIDENCE, _candidate, _job

DENSE_AUTHORED_CV = """ALEX EXAMPLE | Graduate Software Engineer
PROFESSIONAL SUMMARY
Graduate Software Engineer with experience building backend services and cloud applications using Python and SQL. Skilled in REST APIs, databases and automated testing.
EDUCATION
Example University: MSc Computer SystemsSep 2025 - Sep 2026 (Expected)
Relevant Modules (MSc): Distributed Systems, Machine Learning, Cloud Computing, Software Architecture, Information Security, Wireless Networks
Sample Institute: BEng Electronic Engineering (First Class)Sep 2021 - Jun 2024
Relevant Modules (BEng): Computer Programming, Signals and Systems, Embedded Systems, Digital Design, Engineering Mathematics
INTERNSHIP EXPERIENCE
Software Intern, Example Ltd (UK)Jun 2024 - Aug 2024
- Built internal Python tooling to validate operational data and automate weekly reporting for the operations team.
- Migrated reporting queries into PostgreSQL views and documented the new data model for analysts.
- Shadowed the platform team during on-call handovers and incident reviews.
Operations Assistant, Sample Retail (UK)Jul 2023 - Aug 2023
- Coordinated stock counts across three shop floors and reconciled discrepancies with the store manager.
- Prepared weekly staffing rotas and handled supplier delivery schedules.
- Supported customer service during seasonal peaks.
"""
PRESET_NAMES = {preset.name: preset for preset in LAYOUT_PRESETS}


def _dense():
    candidate = _candidate(projects={**PROJECT_EVIDENCE, **EXTRA_PROJECTS}, authored_cv=DENSE_AUTHORED_CV)
    with tempfile.TemporaryDirectory() as tmp:
        artifact = generate_cv(candidate, _job(), store_root=tmp, max_projects=6)
        reader = PdfReader(artifact.pdf_path)
        return candidate, artifact, reader


def _text_runs(page) -> list[tuple[float, float, float]]:
    """(x_start, x_end, baseline_y) of every rendered text run."""
    runs = []

    def visit(text, cm, tm, font, size):
        if text.strip() and font:
            x = tm[4] + cm[4]
            runs.append((x, x + stringWidth(text.rstrip("\n"), font["/BaseFont"].lstrip("/"), size * tm[0]), tm[5] + cm[5]))

    page.extract_text(visitor_text=visit)
    return runs


class CVPageBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.candidate, cls.artifact, cls.reader = _dense()
        cls.text = cls.reader.pages[0].extract_text()

    def test_dense_cv_is_one_a4_page_without_ellipses_or_large_empty_area(self) -> None:
        self.assertEqual(len(self.reader.pages), 1)
        self.assertEqual(tuple(round(float(value)) for value in self.reader.pages[0].mediabox[2:]), (595, 842))
        self.assertNotIn("…", self.text)
        self.assertNotIn("...", self.text)
        self.assertGreaterEqual(self.artifact.page_utilization, MIN_PAGE_UTILIZATION)

    def test_every_degree_keeps_a_concise_supporting_module_line(self) -> None:
        education = self.artifact.document["education"]
        source_modules = {
            "Example University: MSc Computer Systems": "Distributed Systems, Machine Learning, Cloud Computing, Software Architecture, Information Security, Wireless Networks",
            "Sample Institute: BEng Electronic Engineering (First Class)": "Computer Programming, Signals and Systems, Embedded Systems, Digital Design, Engineering Mathematics",
        }
        self.assertEqual([entry["heading"] for entry in education], list(source_modules))
        for entry in education:
            self.assertEqual(len(entry["details"]), 1, entry)
            label, modules = entry["details"][0].split(": ", 1)
            self.assertTrue(label.startswith("Relevant Modules"))
            names = modules.split(", ")
            self.assertGreaterEqual(len(names), 2)
            # Whole, verbatim module names only -- a subset, never sliced text.
            for name in names:
                self.assertIn(name, source_modules[entry["heading"]].split(", "))

    def test_every_displayed_internship_has_at_least_one_grounded_bullet(self) -> None:
        experience = {entry["heading"]: entry["details"] for entry in self.artifact.document["experience"]}
        self.assertEqual(len(experience), 2)
        for heading, details in experience.items():
            self.assertGreaterEqual(len(details), 1, heading)
            for detail in details:
                self.assertIn(detail, DENSE_AUTHORED_CV)
        # The JD-unrelated role is compressed to exactly one bullet; generic
        # internship detail never takes space from project evidence.
        self.assertEqual(len(experience["Operations Assistant, Sample Retail (UK)"]), 1)
        self.assertLessEqual(len(experience["Software Intern, Example Ltd (UK)"]), 2)

    def test_focus_projects_still_receive_more_space_than_weaker_content(self) -> None:
        projects = self.artifact.document["projects"]
        focus = [len(project["bullets"]) for project in projects if project["emphasis"] == "focus"]
        secondary = [len(project["bullets"]) for project in projects if project["emphasis"] == "secondary"]
        internship = [len(entry["details"]) for entry in self.artifact.document["experience"]]
        self.assertTrue(focus and secondary)
        self.assertGreater(max(focus), max(secondary))
        self.assertGreaterEqual(min(focus), max(secondary))
        self.assertGreater(min(focus), max(internship))

    def test_denser_preset_is_used_before_deleting_useful_content(self) -> None:
        # This CV cannot hold all its useful evidence at normal margins, so it
        # must not stay at 18mm while secondary/focus content is being cut.
        self.assertIn(self.artifact.layout_preset, {"compact", "narrow"})
        preset = PRESET_NAMES[self.artifact.layout_preset]
        self.assertEqual(self.artifact.render_margin_mm, preset.margin_mm)
        self.assertEqual(self.artifact.render_body_pt, preset.body_pt)
        self.assertLessEqual(self.artifact.render_margin_mm, 15.0)
        # Bounded professional geometry: never tiny text or near-edge margins.
        self.assertGreaterEqual(self.artifact.render_margin_mm, 10.0)
        self.assertGreaterEqual(self.artifact.render_body_pt, 9.5)

    def test_no_text_is_clipped_or_placed_outside_the_margins(self) -> None:
        margin = self.artifact.render_margin_mm * mm
        width, height = A4
        runs = _text_runs(self.reader.pages[0])
        self.assertTrue(runs)
        for x_start, x_end, y in runs:
            self.assertGreaterEqual(x_start, margin - 1)
            self.assertLessEqual(x_end, width - margin + 2)
            self.assertGreaterEqual(y, margin - 1)
            self.assertLessEqual(y, height - margin)


class LayoutPresetSelectionTests(unittest.TestCase):
    def test_normal_margins_are_kept_when_nothing_useful_needs_cutting(self) -> None:
        projects = {"proj-api": PROJECT_EVIDENCE["proj-api"], "proj-cloud": PROJECT_EVIDENCE["proj-cloud"]}
        with tempfile.TemporaryDirectory() as tmp:
            artifact = generate_cv(_candidate(projects=projects), _job(), store_root=tmp)

        self.assertEqual(artifact.page_count, 1)
        self.assertEqual(artifact.layout_preset, "normal")
        self.assertEqual(artifact.render_margin_mm, 18.0)
        self.assertFalse(any(action.startswith("layout:") for action in artifact.fit_actions))

    def test_weakly_related_internship_keeps_exactly_one_bullet_under_pressure(self) -> None:
        _, artifact, _ = _dense()
        retail = next(entry for entry in artifact.document["experience"] if entry["heading"].startswith("Operations Assistant"))
        self.assertEqual(len(retail["details"]), 1)


if __name__ == "__main__":
    unittest.main()
