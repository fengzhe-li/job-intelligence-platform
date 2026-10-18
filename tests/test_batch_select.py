"""Batch selection: company cap, same-company priority order, deferred roles reported, ATS families."""
from __future__ import annotations

from jobintel.pipeline.batch_select import Candidate, _competitiveness, ats_family, select


def cand(job_id, company, priority, supported=True, level="unspecified"):
    return Candidate(job_id, company, job_id, "London", f"https://jobs.ashbyhq.com/{company}/{job_id}",
                     "ashby" if supported else "greenhouse", supported, priority, level, None, "unknown", "eligible",
                     0.0, priority)


def test_cap_two_per_company_and_the_rest_are_deferred_in_priority_order():
    pool = [cand("a1", "A", 9), cand("a2", "A", 8), cand("a3", "A", 7), cand("b1", "B", 6), cand("c1", "C", 5),
            cand("d1", "D", 4)]
    selected, order, deferred = select(pool, size=5, per_company_cap=2)
    assert [c.job_id for c in selected] == ["a1", "a2", "b1", "c1", "d1"]
    assert [c.job_id for c in order["a"]] == ["a1", "a2", "a3"]  # internal same-company priority order
    assert [c.job_id for c in deferred["a"]] == ["a3"]  # never silently discarded


def test_an_included_role_counts_towards_its_company_cap():
    pool = [cand("a1", "A", 9), cand("a2", "A", 8), cand("a3", "A", 3), cand("b1", "B", 6)]
    selected, _, deferred = select(pool, size=5, per_company_cap=2, include=("a3",))
    assert [c.job_id for c in selected][:2] == ["a3", "a1"] and "a2" in {c.job_id for c in deferred["a"]}


def test_unsupported_ats_roles_are_not_selected_when_execution_is_required():
    pool = [cand("g1", "G", 9, supported=False), cand("a1", "A", 5)]
    selected, _, _ = select(pool, size=5)
    assert [c.job_id for c in selected] == ["a1"]
    assert [c.job_id for c in select(pool, size=5, require_supported=False)[0]] == ["g1", "a1"]


def test_level_and_years_dominate_raw_keyword_rank():
    junior, _ = _competitiveness("junior", None, "unknown", "eligible")
    senior, _ = _competitiveness("senior", 5, "unknown", "eligible")
    assert junior - senior >= 4.5  # a senior 5-year role cannot outrank a junior one on keywords alone
    no_sponsor, notes = _competitiveness("unspecified", None, "explicit_no_sponsor", "eligible")
    assert no_sponsor < 0 and any("no sponsorship" in n for n in notes)


def test_ats_families():
    assert ats_family("https://job-boards.greenhouse.io/x/jobs/1") == "greenhouse"
    assert ats_family("https://jobs.lever.co/x/1/apply") == "lever"
    assert ats_family("https://lseg.wd3.myworkdayjobs.com/Careers/job/1") == "workday"
    assert ats_family("https://www.prospects.ac.uk/graduate-jobs/x") == "aggregator:prospects"
    assert ats_family("https://recruitment.sinara.co.uk") == "other:recruitment.sinara.co.uk"


def test_new_south_wales_and_ukraine_are_not_the_uk():
    from jobintel.connectors.utils import normalise_location
    assert normalise_location("Sydney, New South Wales, Australia").country != "United Kingdom"
    assert normalise_location("Kyiv, Ukraine").country != "United Kingdom"
    assert normalise_location("Cardiff, Wales").country == "United Kingdom"
    assert normalise_location("Remote - UK").country == "United Kingdom"


def test_explicit_hard_conditions_are_recognised_for_selection():
    from jobintel.pipeline.batch_select import _explicit_hard_condition
    assert _explicit_hard_condition("Active or eligible for DV UK security clearance. Sole British National.")
    assert _explicit_hard_condition("You have the right to work in the UK without restrictions for the internship")
    assert _explicit_hard_condition("Sponsorship is not mentioned; we welcome everyone.") is None
