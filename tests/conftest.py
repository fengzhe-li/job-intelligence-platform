from synthetic_answer_bank import synthetic_answer_bank  # noqa: F401  (fixture available suite-wide, opt-in)


import pytest


@pytest.fixture(autouse=True)
def _cv_review_exports_stay_in_tmp(tmp_path_factory, monkeypatch):
    """No test ever writes a CV review copy to the real ~/Downloads."""
    monkeypatch.setenv("JOBINTEL_CV_EXPORT_DIR", str(tmp_path_factory.mktemp("cv-review-exports")))
