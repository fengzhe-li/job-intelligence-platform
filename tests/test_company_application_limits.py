"""Company application limits (2026-10-14): one-active / company-wide / ambiguous restrictions need the
user's own decision before approval (automation or manual) AND a second confirmation before execution; a
company-wide limit caps selection at one role and lists the others; an exact-role restriction binds only its
own job; a previous application to the company is never by itself a restriction."""
from __future__ import annotations

import pytest

from jobintel.analysis.application_restrictions import category, classify, window
from jobintel.pipeline import batch
from jobintel.pipeline.batch_select import Candidate, select
from test_batch_pipeline import CONFIG, NO_FORM_RESTRICTIONS, SYNTHETIC, prepare, states, store, svc_for  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("synthetic_answer_bank")


@pytest.mark.parametrize("text,cat,win", [
    ("You may only submit one application per recruitment cycle.", "COMPANY_WIDE_LIMIT", "per recruitment cycle"),
    ("You can apply to one position per year.", "COMPANY_WIDE_LIMIT", "per year"),
    ("Please only apply for one role.", "COMPANY_WIDE_LIMIT", ""),
    ("Candidates may submit one application every 6 months.", "COMPANY_WIDE_LIMIT", "6 months"),
    ("Candidates can only apply for one role at a time.", "ONE_ACTIVE_APPLICATION", ""),
    ("You have already applied for this position.", "EXACT_ROLE", ""),
    ("If you reapply within 90 days your application will not be considered.", "AMBIGUOUS", "90 days"),
    ("We recommend you apply to the roles that best match your skills.", "NONE", ""),
])
def test_wording_is_classified_with_its_scope_and_window(text, cat, win):
    found = classify(text, source="job_description", job_id="j", company="c")
    assert [category(r.kind) for r in found] == [cat]
    assert window(found[0].wording) == win
    assert found[0].wording == text  # the exact wording is preserved


def test_no_restriction_wording_means_none():
    assert classify("Build great software in Python.", source="jd", job_id="j", company="c") == []


def _candidate(job_id, company, priority, restrictions=()):
    return Candidate(job_id=job_id, company=company, title=f"Engineer {job_id}", location="London",
                     application_url="", ats="ashby", ats_supported=True, rank_score=priority, level="unspecified",
                     years_required=None, sponsorship="unknown", eligibility="eligible", competitiveness=0.0,
                     priority=priority, rationale=[], restrictions=list(restrictions))


def test_a_company_wide_limit_caps_selection_at_its_strongest_role():
    limit = classify("Please only apply for one role.", source="job_description", job_id="a2", company="Acme")[0].to_dict()
    pool = [_candidate("a1", "Acme", 9.0), _candidate("a2", "Acme", 8.0, [limit]), _candidate("a3", "Acme", 7.0),
            _candidate("b1", "Beta", 6.0), _candidate("b2", "Beta", 5.0)]
    selected, _, deferred = select(pool, size=5)
    assert [c.job_id for c in selected] == ["a1", "b1", "b2"]  # Acme: one role only, its strongest
    assert [c.job_id for c in deferred["acme (COMPANY LIMIT APPLIES)"]] == ["a2", "a3"]


def test_without_a_limit_a_previous_application_never_restricts(store):  # noqa: F811
    prepare(store)
    assert batch.limit_findings(store, "app:j2", "j2", "ExampleCo") == []
    assert states(store)["app:j2"] == "cv_review_required"


def _approve(store, app, **kw):  # noqa: F811
    return batch.approve(store, CONFIG, batch_id="b1", application_id=app, reviewer="human", reviewed=True,
                         acknowledge_review_reasons=True, profiles=SYNTHETIC, svc=svc_for(store), **kw)


def test_a_company_limit_needs_the_users_decision_before_approval(store):  # noqa: F811
    prepare(store)
    batch.note_restriction(store, batch_id="b1", application_id="app:j1",
                           wording="You may only submit one application per recruitment cycle.")
    with pytest.raises(batch.BatchError, match="COMPANY APPLICATION LIMIT — USER DECISION REQUIRED") as err:
        _approve(store, "app:j2")
    assert "per recruitment cycle" in str(err.value) and "nothing was approved" in str(err.value)
    assert states(store)["app:j2"] == "cv_review_required"
    manual = _approve(store, "app:j2", company_limit_decision="manual")
    assert manual["company_limit_decision"] == "manual" and states(store)["app:j2"] == "cv_review_required"
    rows = {r["application_id"]: r for r in batch.queue_rows(store, CONFIG, "b1", svc=svc_for(store))}
    assert rows["app:j1"]["limit_findings"][0]["category"] == "COMPANY_WIDE_LIMIT"
    text = batch.write_queue(store, CONFIG, "b1", svc=svc_for(store))[0].read_text()
    assert "COMPANY APPLICATION LIMIT — USER DECISION REQUIRED" in text


def test_execution_needs_a_second_confirmation_after_an_automation_decision(store):  # noqa: F811
    prepare(store)
    batch.note_restriction(store, batch_id="b1", application_id="app:j1",
                           wording="Candidates can only apply for one role at a time.")
    _approve(store, "app:j1", company_limit_decision="automation")
    assert states(store)["app:j1"] == "ready_for_execution"
    calls = []

    def fake_start(svc, application_id, **kw):
        calls.append(application_id)
        return {"request_id": None, "status": "needs_human_input", "handoff": "local_browser_active",
                "final_submit_clicked": False}

    held = batch.run_next(store, CONFIG, form_text=NO_FORM_RESTRICTIONS, batch_id="b1", max_items=3, profiles=SYNTHETIC, svc=svc_for(store), start=fake_start)
    assert calls == [] and held[0]["status"] == "company_limit_confirmation_required"
    assert "You previously chose this role" in held[0]["warning"]
    ran = batch.run_next(store, CONFIG, form_text=NO_FORM_RESTRICTIONS, batch_id="b1", max_items=3, profiles=SYNTHETIC, svc=svc_for(store),
                         start=fake_start, confirm_company_limit=("app:j1",))
    assert calls == ["app:j1"] and ran[-1]["status"] == "needs_human_input"


def test_a_manual_decision_is_never_executed_even_with_confirmation(store):  # noqa: F811
    prepare(store)
    batch.note_restriction(store, batch_id="b1", application_id="app:j1", wording="Please only apply for one role.")
    _approve(store, "app:j2", company_limit_decision="manual")
    assert batch.run_next(store, CONFIG, form_text=NO_FORM_RESTRICTIONS, batch_id="b1", max_items=3, profiles=SYNTHETIC, svc=svc_for(store),
                          start=lambda *a, **k: pytest.fail("must not run"), confirm_company_limit=("app:j2",)) == []


# --- the pre-tab form check: restriction wording on the REAL form stops the batch before any tab opens --------

def _fake_start(calls):
    def start(svc, application_id, **kw):
        calls.append(application_id)  # this is where a real run would open its tab
        return {"request_id": None, "status": "needs_human_input", "handoff": "local_browser_active",
                "final_submit_clicked": False}
    return start


def test_a_company_wide_limit_on_the_form_blocks_tab_creation_until_the_user_confirms(store):  # noqa: F811
    prepare(store)
    _approve(store, "app:j1")  # approved before anyone knew about the form's wording
    form = lambda url: "Apply\nPlease note: you may only submit one application per recruitment cycle.\nFirst name"
    calls = []
    held = batch.run_next(store, CONFIG, batch_id="b1", max_items=3, profiles=SYNTHETIC, svc=svc_for(store),
                          start=_fake_start(calls), form_text=form)
    assert calls == []  # no tab was opened
    assert held[0]["status"] == "company_limit_confirmation_required"
    assert "COMPANY APPLICATION LIMIT — USER DECISION REQUIRED" in held[0]["warning"]
    assert "No tab was opened" in held[0]["warning"] and "application_form (pre-check)" in held[0]["warning"]
    ran = batch.run_next(store, CONFIG, batch_id="b1", max_items=3, profiles=SYNTHETIC, svc=svc_for(store),
                         start=_fake_start(calls), form_text=form, confirm_company_limit=("app:j1",))
    assert calls == ["app:j1"] and ran[-1]["status"] == "needs_human_input"


def test_a_form_that_cannot_be_read_holds_the_item_fail_closed(store):  # noqa: F811
    from jobintel.pipeline.restriction_precheck import PrecheckUnavailable
    prepare(store)
    _approve(store, "app:j1")

    def unreadable(url):
        raise PrecheckUnavailable("TimeoutError")

    calls = []
    held = batch.run_next(store, CONFIG, batch_id="b1", max_items=3, profiles=SYNTHETIC, svc=svc_for(store),
                          start=_fake_start(calls), form_text=unreadable)
    assert calls == [] and held[0]["status"] == "restriction_precheck_unavailable"


def test_no_restriction_wording_on_the_form_runs_normally(store):  # noqa: F811
    prepare(store)
    _approve(store, "app:j1")
    calls = []
    batch.run_next(store, CONFIG, batch_id="b1", max_items=1, profiles=SYNTHETIC, svc=svc_for(store),
                   start=_fake_start(calls), form_text=lambda url: "Apply for this job\nFirst name\nResume/CV")
    assert calls == ["app:j1"]


def test_already_applied_wording_on_this_jobs_own_form_holds_it_as_a_possible_duplicate(store):  # noqa: F811
    prepare(store)
    _approve(store, "app:j1")
    _approve(store, "app:j2")
    calls = []
    forms = {"j1": "You have already applied for this position.", "j2": "Apply\nFirst name"}
    out = batch.run_next(store, CONFIG, batch_id="b1", max_items=3, profiles=SYNTHETIC, svc=svc_for(store),
                         start=_fake_start(calls), form_text=lambda url: forms["j1" if "/j1/" in url else "j2"])
    assert "app:j1" not in calls and "app:j2" in calls  # never widened to the company's other role
    assert any(r.get("application_id") == "app:j1" and "MAY ALREADY HAVE APPLIED" in r.get("warning", "") for r in out)


def test_defer_is_never_executed(store):  # noqa: F811
    prepare(store)
    batch.note_restriction(store, batch_id="b1", application_id="app:j1", wording="Please only apply for one role.")
    out = _approve(store, "app:j1", company_limit_decision="defer")
    assert out["company_limit_decision"] == "defer"
    calls = []
    batch.run_next(store, CONFIG, batch_id="b1", max_items=3, profiles=SYNTHETIC, svc=svc_for(store),
                   start=_fake_start(calls), form_text=lambda url: "", confirm_company_limit=("app:j1",))
    assert "app:j1" not in calls
