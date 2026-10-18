from jobintel.dedup.v1 import _canonical_url


def test_employer_hosted_greenhouse_ids_stay_distinct():
    a = _canonical_url("https://stripe.com/jobs/search?gh_jid=8130930")
    b = _canonical_url("https://stripe.com/jobs/search?gh_jid=8172487")
    assert a != b
    assert _canonical_url("https://www.squarepoint-capital.com/open-opportunities?id=6040910&gh_jid=6040910") != _canonical_url(
        "https://www.squarepoint-capital.com/open-opportunities?id=2546549&gh_jid=2546549"
    )


def test_tracking_parameters_and_fragments_are_still_dropped():
    assert _canonical_url("https://job-boards.greenhouse.io/x/jobs/1?utm_source=li#apply") == "https://job-boards.greenhouse.io/x/jobs/1"
    assert _canonical_url("https://boards.greenhouse.io/x/jobs/1/?gh_src=abc") == "https://boards.greenhouse.io/x/jobs/1"


def test_same_vacancy_matches_regardless_of_parameter_order_and_case():
    assert _canonical_url("https://Example.com/careers?utm=1&gh_jid=42") == _canonical_url("https://example.com/careers?gh_jid=42")
