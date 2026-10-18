from jobintel.analysis.jd_requirements import learnable_stack_skills
from jobintel.models.job import SkillRequirement

REQS = [SkillRequirement("Java", True, ""), SkillRequirement("Go", True, ""), SkillRequirement("Python", True, "")]


def test_stack_description_with_explicit_waiver_is_not_a_requirement():
    jd = ("Experience and familiarity with programming. We work mostly in Java, Ruby, Scala, and Go. "
          "We believe new programming languages can be learned if the fundamentals are present. Strong Python required.")
    assert learnable_stack_skills(jd, REQS) == {"Java", "Go"}


def test_without_waiver_or_outside_stack_sentence_nothing_is_waived():
    assert learnable_stack_skills("We work mostly in Java and Go.", REQS) == set()
    assert learnable_stack_skills("You must know Java and Go. New languages can be learned later.", REQS) == set()


def test_stored_majority_reason_is_superseded_only_when_every_named_skill_is_now_preferred():
    from types import SimpleNamespace
    from jobintel.submission.packages import _artifact_reasons
    artifact = SimpleNamespace(review_reasons=["No evidence for a majority of must-have requirements: Go, Ruby",
                                               "Impact/ownership rewrite(s) need human review: 1 line(s)"])
    job = SimpleNamespace(skill_requirements=[])
    learnable = SimpleNamespace(preferred_skills=["Go", "Ruby"], must_have_skills=["Python"], required_alternatives=[])
    still_required = SimpleNamespace(preferred_skills=["Ruby"], must_have_skills=["Go"], required_alternatives=[])
    assert _artifact_reasons(artifact, job, learnable) == ["Impact/ownership rewrite(s) need human review: 1 line(s)"]
    assert len(_artifact_reasons(artifact, job, still_required)) == 2
