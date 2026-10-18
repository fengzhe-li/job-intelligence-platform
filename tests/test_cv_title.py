

def test_a_programme_prefix_is_noise_but_its_graduate_marker_is_kept():
    """2026-10-16: "Graduate Programme 2027: Software Engineer (Java)" printed the whole posting title."""
    from jobintel.matching.cv_title import cv_title
    assert cv_title("Graduate Programme 2027: Software Engineer (Java)", grounded={"java"}).title == \
        "Graduate Software Engineer (Java)"
    assert cv_title("Engineering: Software Engineer").title == "Engineering: Software Engineer"  # not noise


def test_an_ungrounded_technology_specialisation_is_not_presented():
    """A title's technology qualifier the candidate's evidence does not ground (e.g. a user-approved
    refresh skill) never becomes the CV's identity; a grounded one is kept."""
    from jobintel.matching.cv_title import cv_title
    assert cv_title("Graduate Programme 2027: Software Engineer (Java)", grounded={"python"}).title == \
        "Graduate Software Engineer"
    assert cv_title("Backend Engineer - Python", grounded={"python"}).title == "Backend Engineer - Python"
    assert cv_title("Backend Engineer - Go", grounded={"python"}).title == "Backend Engineer"
    assert cv_title("Software Engineer (Java)").title == "Software Engineer (Java)"  # no evidence view given


def test_campus_recruiting_label_becomes_graduate():
    from jobintel.matching.cv_title import normalise_job_title
    title, _, _ = normalise_job_title("Campus Systems Engineer (Full-Time)")
    assert title is not None and title.startswith("Graduate Systems Engineer") and "Campus" not in title
