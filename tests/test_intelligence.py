from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from unittest.mock import patch

import pytest

from jobintel.analysis.job_enrichment import enrich_job
from jobintel.config import default_ranking_config
from jobintel.intelligence.grounding import (
    GroundedRequirement, EvidenceRecord, supported_relation, validate_requirements,
    validate_matches, evidence_records, REQUIREMENT_SCHEMA, MATCH_SCHEMA,
)
from jobintel.intelligence.provider import IntelligenceConfig, GeminiProvider, ProviderFailure
from jobintel.intelligence.service import IntelligenceService
from jobintel.models.candidate import CandidateProfile, Capability, CapabilityEvidence, EvidenceSource, Project
from jobintel.models.job import Job
from jobintel.models.taxonomy import EvidenceSourceType, SkillCategory


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-secret-never-persist")


def job(text="REST API required."):
    return enrich_job(Job(id="j1", title="Graduate Backend Engineer", company="Example", description=text, locations=[], source_observations=[]), 2026)


def profile(cap="FastAPI", quote="Built REST API endpoints using FastAPI.", confidence=1.0):
    src = EvidenceSource("s1", EvidenceSourceType.MANUAL_PROJECT_DESCRIPTION, "Backend")
    ev = CapabilityEvidence(src, quote, "p1", confidence, "e1")
    return CandidateProfile("c1", "Private Name", 2026,
                            {cap.casefold(): Capability(cap, SkillCategory.BACKEND, [ev])},
                            [Project("p1", "Backend", "Existing real project", [src])],
                            email="private@example.com")


def requirement(text="REST API required.", label="REST API", kind="skill", importance="required", rid="r1"):
    return {"id": rid, "normalized": label, "excerpt": text, "start": 0, "end": len(text),
            "requirement_type": kind, "modality": importance}


def matching(**changes):
    return {"requirement_id": "r1", "evidence_id": "e1", "project_id": "p1", "source_id": "s1",
            "strength": "partial", "explanation": "fastapi -> rest api; partial support from existing evidence e1.", **changes}


class Fake:
    def __init__(self, reqs=None, matches=None, error=None):
        self.reqs = {"requirements": [requirement()]} if reqs is None else reqs
        self.matches = {"matches": [matching()]} if matches is None else matches
        self.calls = []
        self.error = error

    def generate(self, task, inputs, schema):
        self.calls.append((task, copy.deepcopy(inputs)))
        if self.error:
            raise self.error
        return copy.deepcopy(self.reqs if task == "extract" else self.matches)


def service(fake=None, tmp_path=None, **kwargs):
    return IntelligenceService(provider=fake or Fake(), cache_dir=tmp_path, **kwargs)


def test_no_key_fallback_has_no_calls_or_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY")
    fake = Fake()
    result = service(fake, tmp_path).analyze(job(), profile())
    assert result.metadata.fallback_reason == "not_configured"
    assert not fake.calls and not list(tmp_path.iterdir())
    assert result.deterministic_match.job_id == "j1"


@pytest.mark.parametrize("error", [TimeoutError("test-secret-never-persist"), RuntimeError("429 quota"), OSError("API failure")])
def test_provider_failure_falls_back_without_leaking(error):
    result = service(Fake(error=error)).analyze(job(), profile())
    assert result.metadata.used == "deterministic_fallback"
    assert not result.semantic_matches
    assert str(error) not in json.dumps(result.to_dict(), default=str)


@pytest.mark.parametrize("payload", [None, [], {"requirements": "bad"}, {"requirements": [{}]}])
def test_malformed_extraction_falls_back(payload):
    fake = Fake()
    fake.reqs = payload
    result = service(fake).analyze(job(), profile())
    assert result.metadata.used == "deterministic_fallback"
    assert len(fake.calls) == 1


def test_semantic_relationship_is_grounded_partial_and_advisory():
    candidate, listing = profile(), job()
    before = (asdict(candidate), asdict(listing))
    result = service().analyze(listing, candidate)
    assert result.metadata.used == "gemini_assisted"
    assert result.semantic_matches[0]["strength"] == "partial"
    assert result.combined[0]["combined_evidence"] == "partial"
    assert result.disagreements and result.project_relevance[0]["project_id"] == "p1"
    assert before == (asdict(candidate), asdict(listing))
    assert result.requirements[0].excerpt == listing.description


@pytest.mark.parametrize("changes,reason", [
    ({"evidence_id": "invented"}, "unknown_requirement_or_evidence"),
    ({"requirement_id": "invented"}, "unknown_requirement_or_evidence"),
    ({"project_id": "another-project"}, "cross_project_or_source"),
    ({"source_id": "another-source"}, "cross_project_or_source"),
    ({"strength": "strong"}, "evidence_strength_inflation"),
])
def test_invalid_match_rejected(changes, reason):
    result = service(Fake(matches={"matches": [matching(**changes)]})).analyze(job(), profile())
    assert not result.semantic_matches
    assert reason in result.metadata.validation_failures
    assert result.metadata.used == "deterministic_fallback"


@pytest.mark.parametrize("target,cap,quote", [
    ("Kubernetes", "Docker", "Built Docker containers."),
    ("Java", "Python", "Built Python tools."),
    ("Azure", "AWS", "Built AWS infrastructure."),
    ("model training", "RAG", "Built RAG vector retrieval."),
    ("serverless", "AWS", "Built AWS architecture."),
    ("REST API", "FastAPI", "Read about FastAPI."),
])
def test_adversarial_relationships_rejected_even_with_real_ids(target, cap, quote):
    req = GroundedRequirement(**requirement(f"{target} required.", target))
    records = evidence_records(profile(cap, quote))
    accepted, failures = validate_matches({"matches": [matching()]}, [req], records)
    assert not accepted and failures == ["unsupported_capability"]


def test_partial_direct_evidence_cannot_be_upgraded():
    fake = Fake(reqs={"requirements": [requirement("Python required.", "Python")]},
                matches={"matches": [matching(strength="strong")]})
    result = service(fake).analyze(job("Python required."), profile("Python", "Built Python tools.", .3))
    assert result.metadata.validation_failures == ["evidence_strength_inflation"]


def test_evidence_tier_is_local_to_cited_project():
    p = profile("Python", "Built Python tools.", .3)
    cap = p.capabilities["python"]
    cap.evidence.append(replace(cap.evidence[0], id="other", confidence=1, project_id="p2"))
    p.projects.append(Project("p2", "Other", "Other"))
    assert cap.evidence_tier == "strong"
    assert next(e for e in evidence_records(p) if e.id == "e1").tier == "partial"


def test_required_and_preferred_preserve_exact_spans():
    text = "Python required. AWS preferred."
    a = requirement("Python required.", "Python")
    b = requirement("AWS preferred.", "AWS", importance="preferred", rid="r2")
    b.update(start=17, end=len(text))
    accepted, failures = validate_requirements({"requirements": [a, b]}, text)
    assert not failures
    assert [r.modality for r in accepted] == ["required", "preferred"]
    assert all(text[r.start:r.end] == r.excerpt for r in accepted)


def test_clipped_preference_cannot_become_required():
    req = requirement("Python", "Python")
    accepted, failures = validate_requirements({"requirements": [req]}, "Python preferred.")
    assert not accepted and "requirement_modality" in failures


def test_ungrounded_normalization_rejected():
    req = requirement()
    req["normalized"] = "Kubernetes"
    accepted, failures = validate_requirements({"requirements": [req]}, "REST API required.")
    assert not accepted and failures == ["unsupported_interpretation"]


def test_ungrounded_span_rejected():
    req = requirement()
    req["start"] = 2
    result = service(Fake(reqs={"requirements": [req]})).analyze(job(), profile())
    assert result.metadata.used == "deterministic_fallback"


def test_intake_year_remains_informational():
    text = "2027 Graduate Programme"
    req = requirement(text, "2027", "eligibility", "unclear")
    result = service(Fake(reqs={"requirements": [req]})).analyze(job(text), profile())
    assert result.metadata.validation_failures == ["unstated_eligibility"]
    assert result.deterministic_jd.detected_intake_year == 2027
    assert result.deterministic_jd.graduation_year_state == "graduate_friendly"


def test_explicit_graduation_constraint_grounded():
    text = "Candidates must graduate in 2027."
    accepted, failures = validate_requirements({"requirements": [requirement(text, "graduate in 2027", "eligibility")]}, text)
    assert not failures and accepted[0].excerpt == text


def test_cache_hit_avoids_both_calls_and_persists(tmp_path):
    fake = Fake()
    first = service(fake, tmp_path).analyze(job(), profile())
    second = service(fake, tmp_path).analyze(job(), profile())
    assert first.metadata.cache == "miss" and second.metadata.cache == "hit"
    assert len(fake.calls) == 2
    assert first.semantic_matches == second.semantic_matches
    assert "test-secret-never-persist" not in next(tmp_path.iterdir()).read_text()
    assert "private@example.com" not in next(tmp_path.iterdir()).read_text()


@pytest.mark.parametrize("change", ["jd", "quote", "confidence", "project", "profile", "model", "prompt"])
def test_relevant_changes_invalidate_cache(tmp_path, monkeypatch, change):
    fake, candidate, listing = Fake(), profile(), job()
    svc = service(fake, tmp_path)
    svc.analyze(listing, candidate)
    if change == "jd":
        listing.description += " More details."
    elif change in {"quote", "confidence", "project"}:
        cap = candidate.capabilities["fastapi"]
        changes = {"quote": {"quote": "Developed FastAPI API endpoints."}, "confidence": {"confidence": .3}, "project": {"project_id": None}}
        cap.evidence[0] = replace(cap.evidence[0], **changes[change])
    elif change == "profile":
        candidate.graduation_year = 2025
    elif change == "model":
        svc = service(fake, tmp_path, config=IntelligenceConfig(model="gemini-other"))
    else:
        monkeypatch.setattr("jobintel.intelligence.service.PROMPT_VERSION", "changed")
    result = svc.analyze(listing, candidate)
    assert result.metadata.cache == "miss"
    assert len(fake.calls) > 2


def test_cache_corruption_is_a_miss(tmp_path):
    fake = Fake()
    service(fake, tmp_path).analyze(job(), profile())
    next(tmp_path.iterdir()).write_text("invalid json")
    result = service(fake, tmp_path).analyze(job(), profile())
    assert result.metadata.used == "gemini_assisted"
    assert result.metadata.cache_warning == "cache_read_failed"
    assert len(fake.calls) == 4


def test_cache_hits_are_revalidated(tmp_path):
    service(Fake(), tmp_path).analyze(job(), profile())
    path = next(tmp_path.iterdir())
    payload = json.loads(path.read_text())
    payload["result"]["semantic"]["matches"][0]["project_id"] = "invented"
    path.write_text(json.dumps(payload))
    fake = Fake()
    result = service(fake, tmp_path).analyze(job(), profile())
    assert result.metadata.used == "deterministic_fallback" and not fake.calls


def test_model_explanation_cannot_invent_metrics():
    result = service(Fake(matches={"matches": [matching(explanation="Trained 100 models with Kubernetes")]})).analyze(job(), profile())
    assert "100 models" not in json.dumps(result.to_dict(), default=str)
    assert not result.semantic_matches
    assert result.metadata.validation_failures == ["unvalidated_explanation"]


def test_unrelated_evidence_and_contact_details_not_transmitted():
    p = profile()
    p.capabilities["java"] = profile("Java", "Private unrelated Java project").capabilities["java"]
    p.capabilities["java"].evidence[0] = replace(p.capabilities["java"].evidence[0], id="e2")
    fake = Fake()
    service(fake).analyze(job(), p)
    sent = json.dumps(fake.calls)
    assert "private@example.com" not in sent and "Private Name" not in sent
    assert "unrelated" not in sent and "Java" not in sent
    assert fake.calls[0][1] == {"jd_text": "REST API required."}


def test_duplicate_evidence_ids_are_excluded():
    p = profile()
    p.capabilities["fastapi"].evidence *= 2
    assert evidence_records(p) == []


def test_invalid_config_and_large_jd_fall_back():
    fake = Fake()
    result = service(fake, config=IntelligenceConfig(model="../../secret")).analyze(job(), profile())
    assert result.metadata.fallback_reason == "invalid_configuration" and not fake.calls
    result = service(fake, config=IntelligenceConfig(max_jd_chars=3)).analyze(job(), profile())
    assert result.metadata.fallback_reason == "jd_empty_or_input_limit" and not fake.calls


def test_transport_key_only_in_header_and_timeout_set():
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, count):
            return json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"requirements": []}'}]}}]}).encode()
    with patch("jobintel.intelligence.provider.urlopen", return_value=Response()) as opened:
        GeminiProvider(IntelligenceConfig()).generate("extract", {"jd_text": "JD"}, {})
    req = opened.call_args.args[0]
    assert "test-secret-never-persist" not in req.full_url
    assert b"test-secret-never-persist" not in req.data
    assert req.get_header("X-goog-api-key") == "test-secret-never-persist"
    assert opened.call_args.kwargs["timeout"] == 15


def test_transport_error_never_exposes_response_body():
    with patch("jobintel.intelligence.provider.urlopen", side_effect=OSError("test-secret-never-persist")):
        with pytest.raises(ProviderFailure, match="^provider_error$") as error:
            GeminiProvider(IntelligenceConfig()).generate("extract", {}, {})
    assert "test-secret" not in str(error.value)


def test_daily_refresh_is_independent_of_gemini_failures(tmp_path):
    from jobintel.pipeline.daily import run_daily_refresh
    from jobintel.storage.local_store import LocalJobStore
    registry = tmp_path / "registry.json"
    registry.write_text("[]")
    store = LocalJobStore(tmp_path / "store")
    listing = job()
    store.write_jobs([listing])
    result = service(Fake(error=TimeoutError())).analyze(listing, profile())
    assert result.metadata.used == "deterministic_fallback"
    with (patch("jobintel.intelligence.provider.GeminiProvider.generate", side_effect=TimeoutError()) as gemini,
          patch("jobintel.pipeline.daily.refresh_sources", return_value=[]),
          patch("jobintel.connectors.prospects.fetch_text", return_value="<html></html>"),
          patch.dict("os.environ", {"ADZUNA_APP_ID": "", "ADZUNA_APP_KEY": "", "JOBINTEL_GITHUB_USERNAME": ""})):
        report = run_daily_refresh(store, default_ranking_config(), registry)
    gemini.assert_not_called()
    assert store.read_jobs()[0].id == listing.id
    assert report.sources_failed == []


@pytest.mark.parametrize("qualifier", ["5 years of", "expert", "advanced", "production", "professional"])
def test_plain_skill_cannot_satisfy_stronger_jd_expectation(qualifier):
    text = f"{qualifier} Python required."
    req = GroundedRequirement(**requirement(text, "Python"))
    assert supported_relation(req, evidence_records(profile("Python", "Built Python tools."))[0]) is None


@pytest.mark.parametrize("payload", [None, {}, {"matches": "wrong"}, {"matches": [{"bad": "value"}]}])
def test_malformed_matching_falls_back(payload):
    fake = Fake()
    fake.matches = payload
    result = service(fake).analyze(job(), profile())
    assert result.metadata.used == "deterministic_fallback"
    assert not result.semantic_matches


@pytest.mark.parametrize("status,reason", [(429, "quota_exhausted"), (403, "authentication_failed"), (500, "provider_error")])
def test_http_failure_has_safe_reason(status, reason):
    from urllib.error import HTTPError
    error = HTTPError("https://example.invalid/test-secret-never-persist", status, "private body", {}, None)
    with patch("jobintel.intelligence.provider.urlopen", side_effect=error):
        result = IntelligenceService().analyze(job(), profile())
    assert result.metadata.fallback_reason == reason
    assert "test-secret" not in json.dumps(result.to_dict(), default=str)


def test_empty_extraction_is_fallback():
    result = service(Fake(reqs={"requirements": []})).analyze(job(), profile())
    assert result.metadata.fallback_reason == "no_grounded_requirements"


def test_cache_write_failure_does_not_fail_analysis(tmp_path):
    occupied = tmp_path / "file"
    occupied.write_text("occupied")
    result = service(Fake(), occupied / "child").analyze(job(), profile())
    assert result.metadata.used == "gemini_assisted"
    assert result.metadata.cache_warning == "cache_write_failed"


def test_preferred_requirement_stays_preferred_in_hybrid_result():
    text = "Python preferred."
    fake = Fake(reqs={"requirements": [requirement(text, "Python", importance="preferred")]},
                matches={"matches": [matching(strength="strong", explanation="direct; strong support from existing evidence e1.")]})
    result = service(fake).analyze(job(text), profile("Python", "Built Python tools."))
    assert result.requirements[0].modality == "preferred"
    assert result.combined[0]["semantic_modality"] == "preferred"


def test_intake_wording_with_required_does_not_become_eligibility():
    text = "2027 Graduate Programme requires Python."
    req = requirement(text, "2027", "eligibility")
    accepted, failures = validate_requirements({"requirements": [req]}, text)
    assert not accepted and failures == ["unstated_eligibility"]


def test_unsupported_requirement_remains_visible_alongside_supported_match():
    text = "REST API required. Kubernetes required."
    first = requirement()
    second = requirement("Kubernetes required.", "Kubernetes", rid="r2")
    second.update(start=19, end=len(text))
    fake = Fake(reqs={"requirements": [first, second]})
    result = service(fake).analyze(job(text), profile())
    assert result.metadata.used == "gemini_assisted"
    row = next(r for r in result.combined if r["requirement_id"] == "r2")
    assert row["combined_evidence"] == "missing"
    assert result.requirements[1].excerpt == "Kubernetes required."


def test_gemini_schema_omits_max_items_to_avoid_state_space_error():
    assert "maxItems" not in REQUIREMENT_SCHEMA["properties"]["requirements"]
    assert "maxItems" not in MATCH_SCHEMA["properties"]["matches"]
    with pytest.raises(ValueError, match="invalid_schema"):
        validate_requirements({"requirements": [{}] * 81}, "text")
    with pytest.raises(ValueError, match="invalid_schema"):
        validate_matches({"matches": [{}] * 121}, [], [])
