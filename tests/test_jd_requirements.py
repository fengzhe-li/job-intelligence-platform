from __future__ import annotations

import unittest
from datetime import datetime, timezone

from jobintel.analysis.jd_requirements import extract_jd_requirements
from jobintel.analysis.job_enrichment import enrich_job
from jobintel.models.job import Job, Location, SourceObservation


def _job(description: str, title: str = "Graduate Backend Engineer") -> Job:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    job = Job(
        id="job-1",
        title=title,
        company="Example Co",
        description=description,
        locations=[Location(city="London", country="United Kingdom")],
        source_observations=[
            SourceObservation(
                source_name="company",
                source_job_id="job-1",
                original_url="https://example.com/jobs/1",
                first_seen_at=now,
                last_seen_at=now,
                posted_at=now,
                raw_description=description,
                canonical_application_url="https://example.com/jobs/1/apply",
            )
        ],
    )
    return enrich_job(job, 2026)


class JDRequirementsTests(unittest.TestCase):
    def test_splits_must_have_and_preferred_skills(self) -> None:
        job = _job("Required: Python, PostgreSQL. Docker is nice to have. AWS preferred.")
        jd = extract_jd_requirements(job)

        self.assertIn("Python", jd.must_have_skills)
        self.assertIn("PostgreSQL", jd.must_have_skills)
        self.assertIn("Docker", jd.preferred_skills)
        self.assertIn("AWS", jd.preferred_skills)

    def test_extracts_seniority_and_experience(self) -> None:
        job = _job("Graduate role. 0-2 years experience. Build Python backend APIs.")
        jd = extract_jd_requirements(job)

        self.assertEqual(jd.seniority, "graduate")

    def test_extracts_role_family_and_domain(self) -> None:
        job = _job("We need a backend engineer to build Python microservices.")
        jd = extract_jd_requirements(job)

        self.assertEqual(jd.role_family, "software_engineering")

    def test_detected_intake_year_is_informational_only(self) -> None:
        job = _job("Join our 2028 Graduate Programme building Python backend services.")
        jd = extract_jd_requirements(job)

        self.assertEqual(jd.detected_intake_year, 2028)
        # Informational only -- must not appear as a rejection/graduation-year mismatch.
        self.assertNotEqual(jd.graduation_year_state, "other_year_restriction")

    def test_education_requirements_are_separated_from_other_eligibility(self) -> None:
        job = _job("Requires a degree in computer science. Right to work in the UK required.")
        jd = extract_jd_requirements(job)

        self.assertTrue(any("degree" in item.casefold() for item in jd.education_requirements))
        self.assertTrue(any("right to work" in item.casefold() for item in jd.other_eligibility))


if __name__ == "__main__":
    unittest.main()


# --- required vs bonus / preferred sections (2026-10-13) --------------------------------------------

def _req(description):
    from jobintel.analysis.role_tracks import extract_skill_requirements
    return {r.name: r.required for r in extract_skill_requirements(description)}


def test_skills_listed_under_a_bonus_heading_are_preferred_not_required():
    jd = ("REQUIREMENTS\nWe do require:\n- Expertise in Python.\n- Knowledge about test design.\n- Bonus:\n"
          "- Experience with Kubernetes.\n- Experience with Stripe.\nWe also require Docker.\nLOCATION\nRemote.")
    req = _req(jd)
    assert req["Python"] is True and req["Kubernetes"] is False
    assert req["Docker"] is True  # the bonus list ends at the next non-bullet line


def test_other_optional_headings_and_inline_bonus_lines():
    assert _req("Requirements:\n- Python\nNice to have:\n- Kubernetes\n- Terraform")["Kubernetes"] is False
    assert _req("Preferred qualifications\n• Go\n• Rust")["Go"] is False
    assert _req("Bonus: Kubernetes experience.\nMust have Python.") == {**_req("Bonus: Kubernetes experience.\nMust have Python."),
                                                                         "Kubernetes": False, "Python": True}


def test_without_an_optional_heading_skills_stay_required():
    assert _req("You will use Kubernetes daily.\n- Python\n- Go") == {"Kubernetes": True, "Python": True, "Go": True}


# --- alternatives: "one or more of", "at least one of", "such as", "X or Y"; mandatory stays mandatory ---

def _groups(description):
    from jobintel.analysis.role_tracks import extract_requirement_groups, extract_skill_requirements
    return extract_requirement_groups(description, extract_skill_requirements(description))


def _resolve(description, evidenced):
    from jobintel.analysis.jd_requirements import resolve_required_skills
    from jobintel.analysis.role_tracks import extract_requirement_groups, extract_skill_requirements
    from types import SimpleNamespace
    reqs = extract_skill_requirements(description)
    jd = SimpleNamespace(must_have_skills=[r.name for r in reqs if r.required],
                         required_alternatives=extract_requirement_groups(description, reqs))
    return resolve_required_skills(jd, lambda name: evidenced.get(name))


PALANTIR = "Proficiency with one or more programming languages, such as Python, Java, C++, TypeScript/JavaScript, or similar."


def test_one_or_more_languages_is_one_group_satisfied_by_python():
    [group] = _groups(PALANTIR)
    assert group["kind"] == "one_of" and set(group["members"]) == {"Python", "Java", "C++", "TypeScript", "JavaScript"}
    assert "C" not in group["members"]  # never the C of "C++"
    matched, missing, partial = _resolve(PALANTIR, {"Python": "strong"})
    assert missing == [] and matched and matched[0].startswith("Python")


def test_at_least_one_of_and_either_or():
    assert _groups("Experience in at least one of Go, Rust or Java.")[0]["kind"] == "one_of"
    assert _groups("Experience with either Python or Go.")[0]["kind"] == "one_of"
    matched, missing, _ = _resolve("Experience in at least one of Go, Rust or Java.", {})
    assert missing == ["one of Go / Rust / Java"]  # ONE reason for the group, never one per alternative


def test_examples_such_as_are_alternatives():
    [group] = _groups("Programming experience in languages such as Java, Python or Go.")
    assert group["kind"] == "one_of" or group["kind"] == "examples"
    assert _resolve("Strong coder in languages such as Java, Go or Python.", {"Python": "strong"})[1] == []


def test_an_individually_mandatory_language_stays_mandatory():
    assert _groups("Java is required. Python is required.") == []
    assert _resolve("Java is required.", {"Python": "strong"})[1] == ["Java"]
    assert _groups("Strong Java and Python experience.") == []  # "and": both required
    assert _groups("Java and Python or Go.") == []  # ambiguous and/or: fail closed, all stay required


def test_mixed_required_and_preferred_alternatives():
    text = "Experience with Python or Go is required; Rust or C++ is a plus."
    from jobintel.analysis.role_tracks import extract_skill_requirements
    req = {r.name: r.required for r in extract_skill_requirements(text)}
    assert req["Python"] is True and req["Rust"] is False and req["C++"] is False
    [group] = _groups(text)
    assert set(group["members"]) == {"Python", "Go"}
    assert _resolve(text, {"Go": "partial"})[2] == ["one of Python / Go"]  # partial-only group is flagged


# --- section-aware requirements: stack descriptions and values are not requirements -----------------------
from jobintel.analysis.role_tracks import extract_skill_requirements as _skills

SECTIONED_JD = ("The Role\nYou will work on Environment Platform: a Kubernetes-based PaaS.\n"
                "Technologies We Use\n- Open-source technologies like Cassandra, Spark and Redux\n"
                "What We Value\n- Eagerness to read the source of libraries (such as Elasticsearch or Chromium).\n"
                "What We Require\n- Experience coding in programming languages, such as Java, C++, Python, or similar.\n"
                "- Strong Docker knowledge.\nLife at Example\nWe use Terraform everywhere.\n")


def _required(text):
    return {r.name for r in _skills(text) if r.required}


def test_with_a_requirements_section_only_its_skills_are_required():
    required = _required(SECTIONED_JD)
    assert {"Python", "Java", "C++", "Docker"} <= required
    assert not required & {"Kubernetes", "Cassandra", "Spark", "Redux", "Elasticsearch", "Terraform"}


def test_without_a_requirements_section_mentions_stay_required_except_descriptive_sections():
    jd = ("We build with Kubernetes and Go.\nTechnologies We Use\n- Cassandra, Spark\n"
          "About the team\nThe team ships daily.\n")
    required = _required(jd)
    assert {"Kubernetes", "Go"} <= required and not required & {"Cassandra", "Spark"}


def test_a_requirements_section_still_keeps_optional_clauses_optional():
    jd = "Requirements\n- Python is required; Rust is a plus.\nAbout us\nRust everywhere.\n"
    assert _required(jd) == {"Python"}


def test_a_skill_required_in_its_section_is_required_even_if_mentioned_earlier_descriptively():
    jd = "The team uses Kafka for events.\nRequirements\n- Production Kafka experience.\n"
    assert "Kafka" in _required(jd)


def test_a_group_uses_the_requirement_occurrence_not_an_earlier_descriptive_mention():
    from jobintel.analysis.role_tracks import extract_requirement_groups
    jd = ("Technologies We Use\n- A variety of languages, including Java and Go for backend\n"
          "What We Require\n- Experience coding in programming languages, such as Java, C++, Python, or similar.\n")
    reqs = _skills(jd)
    groups = extract_requirement_groups(jd, reqs)
    assert [g["members"] for g in groups] == [["Java", "C++", "Python"]]


# --- HTML JDs (hosted job boards) and long optional headings ----------------------------------------------

HTML_JD = ("<h3>What you'll do</h3><ul><li>Ship services that use Python.</li></ul>"
           "<h3>Desirable Skills, Knowledge, and Experience</h3><ul><li>Fluency in Rust or TypeScript/JavaScript.</li>"
           "<li>Experience with Kubernetes.</li></ul><p>We value curiosity.</p>")


def test_an_html_jd_keeps_its_sections_and_bullets():
    from jobintel.analysis.role_tracks import requirement_text
    text = requirement_text(HTML_JD)
    assert "Desirable Skills, Knowledge, and Experience" in text.splitlines()
    assert "- Experience with Kubernetes." in text.splitlines()
    assert requirement_text("Plain\n- text stays") == "Plain\n- text stays"


def test_skills_under_a_long_desirable_heading_in_html_are_preferred():
    req = {r.name: r.required for r in _skills(HTML_JD)}
    assert req["Python"] is True
    assert req["Rust"] is False and req["TypeScript"] is False and req["Kubernetes"] is False


# --- e.g. inside a clause, "Tech stack and tools", "Added Bonus", "we don't ask ... all of this" ----------

def test_an_eg_example_list_is_one_group_satisfied_by_any_member():
    from jobintel.analysis.role_tracks import extract_requirement_groups
    jd = ("You should apply if:\n- You have developed component-oriented JavaScript applications using frameworks "
          "and libraries (e.g React, Vue, Angular, Webpack etc).\n")
    groups = extract_requirement_groups(jd, _skills(jd))
    assert any({"React", "Vue", "Angular"} <= set(g["members"]) for g in groups)


def test_tech_stack_and_tools_heading_is_descriptive_and_added_bonus_is_optional():
    jd = ("Tech stack and tools:\n- Ruby, Kubernetes\nAbout You:\n- Practical cloud experience in AWS.\n"
          "Added Bonus:\n- Exposure to Helm and GitHub Actions.\n")
    req = {r.name: r.required for r in _skills(jd)}
    assert req.get("AWS") is True
    assert not any(req.get(s) for s in ("Ruby", "Kubernetes", "Helm", "GitHub Actions"))


def test_a_we_dont_ask_for_all_of_this_stack_line_is_descriptive_even_inside_requirements():
    jd = ("Requirements\nYou build robust systems in Python.\n"
          "The main part of our Tech Stack is listed below, we don't ask that you have experience in all of this.\n"
          "Python Java, which makes up the backend; AWS & GCP; Terraform\nInterview process\nStage 1\n")
    req = {r.name: r.required for r in _skills(jd)}
    assert req["Python"] is True  # still required by its own requirement sentence
    assert not any(req.get(s) for s in ("Java", "GCP", "Terraform"))


def test_considered_an_advantage_is_optional():
    jd = "What excites us\n- Experience with IaC tooling, such as Terraform;\n- Experience with Kubernetes will be considered an advantage;\n"
    req = {r.name: r.required for r in _skills(jd)}
    assert req["Terraform"] is True and req["Kubernetes"] is False


def test_what_we_look_for_is_a_requirements_heading_so_stack_examples_elsewhere_are_not_required():
    """2026-10-16: a JD whose requirements sit under "What we look for" (traits only) named stacks only as
    responsibility examples ("gRPC to REST, Kubernetes to Docker, Python to Rust"); none is a must-have."""
    jd = ("A few examples of your responsibilities\n"
          "Master your toolkit. From monoliths to microservices, gRPC to REST, Kubernetes to Docker, Python to Rust.\n"
          "What we look for\nReasoning ability. You can solve complex problems.\n"
          "What we offer\nWe go to great lengths to help you grow.\n")
    assert _required(jd) == set()


def test_a_curly_apostrophe_requirements_heading_still_scopes_required_skills():
    """2026-10-16: "What you’ll need" (U+2019) was not seen as a heading, so a stack sentence elsewhere
    ("Our stack includes Java 17/21, GCP, Kubernetes ...") made every listed tool a must-have."""
    jd = ("About the role\nOur stack includes Java 17/21, GCP, Kubernetes, Docker, PostgreSQL and Redis.\n"
          "What you’ll need\n- A portfolio of coding projects\n- Solid problem-solving skills\n")
    assert _required(jd) == set()
    assert _required(jd.replace("- Solid", "- Python experience\n- Solid")) == {"Python"}
