# Phase 3.5A — Gemini intelligence foundation

Status: implemented as an optional, on-demand advisory layer. No Gemini-generated
text is used in CVs, eligibility decisions, ranking, project selection, or application
answers. Phase 3, Adzuna coverage, historical dedup and manual application behavior
are preserved.

## Use

Run from the repository root after making `GEMINI_API_KEY` available in the process
environment through your local secret manager. Do not put credentials in commands,
job/profile records, configuration JSON or source control. This module does not load
`.env` files automatically.

```sh
PYTHONPATH=src python3 -m jobintel.intelligence --job-id EXISTING_JOB_ID
```

Options: `--store` (default `data/local`) and `--config` (default
`config/personal_strategy.json`). The command reads a real job and profile and prints
a JSON comparison. It writes only a separate cache when analysis succeeds; it does
not rewrite the job/profile stores. Without a key it makes no request or cache write
and returns the existing deterministic interpretation and match.

`GEMINI_MODEL` selects the model (default `gemini-2.5-flash`).
`JOBINTEL_INTELLIGENCE_PROVIDER` defaults to `gemini`; unsupported providers safely
fall back. `IntelligenceConfig` also exposes API version, timeout and input limits.
Secrets are not dataclass fields and are excluded from configuration repr/asdict.

Python integration:

```python
from pathlib import Path
from jobintel.intelligence import IntelligenceService

result = IntelligenceService(
    cache_dir=Path("data/local/intelligence_cache")
).analyze(job, candidate, ranking_config)
```

## A. Architecture added

- `provider.py`: a small JSON provider protocol, optional configuration and stdlib
  Gemini REST adapter. Business logic has no dependency on a Gemini SDK.
- `grounding.py`: structured requirement schemas, exact span validation, an audited
  directional relation catalog, evidence selection and match validation.
- `service.py`: deterministic baseline, optional two-stage analysis, hybrid comparison,
  project signals, content-addressed cache and safe metadata.
- `__main__.py`: one-job comparison command.
- `tests/test_intelligence.py`: mocked provider, grounding, cache, transport and refresh
  isolation regressions.

The existing `extract_jd_requirements`, `match_job`, `score_projects_for_job` and
`generate_cv` are reused or left untouched. No existing source files were edited.

## B. Configuration behavior

The key is read only from `GEMINI_API_KEY` and sent in the `x-goog-api-key` header,
never in the URL. Provider/model/API versions are explicit. The default timeout is
15 seconds per request, with no automatic retries. A job needs at most two requests
on a cache miss (extraction followed by matching); matching is skipped when no
relevant evidence is available. Inputs are bounded to 24,000 JD characters, 40
selected evidence entries and 18,000 evidence quote characters by default.

The REST adapter uses Google's documented `generateContent` structured-output
interface: https://ai.google.dev/api/generate-content and
https://ai.google.dev/gemini-api/docs/structured-output . Model availability still
requires live verification with the user's account.

## C. Deterministic fallback

Missing credentials, invalid configuration, oversized/empty JD, timeout, quota,
authentication/API failures, malformed output and grounding failures preserve the
complete deterministic result. No partially validated batch is promoted. Cache
read/write errors do not make analysis fail; cache hits are grounded again.
Exceptions are reduced to fixed safe reason codes, never raw exception messages or
HTTP bodies. Free-form provider explanations are rejected unless they exactly match
the audited proposition format.

Daily Refresh has no dependency on this on-demand service. Setting a key cannot
cause a discovery-wide call burst. The refresh regression exercises successful
refresh after a failed intelligence request and verifies no provider invocation in
refresh itself.

## D. JD extraction result

Each accepted requirement contains `id`, `normalized`, `excerpt`, `start`, `end`,
`requirement_type`, and `modality` (`required`, `preferred`, `unclear`). Types cover
role family, seniority, early career, eligibility, skill/language/framework,
cloud/platform, systems/networking/embedded, AI/ML, domain, responsibilities and
experience.

The initial normalized label is extractive: it must occur in the quoted JD span.
Canonical aliases are used by the matching validator. This deliberately restricts
free paraphrasing because a real quote alone cannot establish entailment of an
arbitrary model interpretation. Surrounding sentence context protects against
clipping `Python` out of `Python preferred` and calling it required.

Eligibility requires explicit supporting constraint language. Intake/start years
cannot become graduation exclusions. The existing candidate graduation year is
2026; deterministic eligibility remains authoritative. Unclear wording stays
unclear or causes conservative fallback.

## E. Evidence grounding

Every accepted match references a requirement ID, existing evidence ID, exact
project ID (or null for nonproject evidence), source ID, strength and explanation.
The validator checks both IDs and the underlying quotation/capability. Duplicate
ambiguous evidence IDs are excluded. An evidence item cannot borrow confidence
from other projects: the ceiling is the lesser of capability tier and the tier of
that specific citation. Partial cannot become strong.

Allowed relations are directional and nontransitive. For example, implemented
FastAPI API endpoints can partially support REST API requirements; AWS evidence
can partially support a general cloud requirement. Docker cannot support Kubernetes,
AWS cannot support Azure, Python cannot support Java, and RAG cannot support model
training. Plain AWS also cannot prove serverless use. Stronger JD qualifiers such as
expert, production or years of experience require richer validation and are
currently rejected for semantic matching. Negated/planned/learning quotations are
not promoted.

Model prose cannot introduce metrics or capabilities through an otherwise valid
ID reference. The explanation must match the approved relationship, evidence ID
and strength exactly; extra claims reject the match.

## F. Hybrid matching

The result includes original deterministic JD/match objects, grounded Gemini
requirements, validated semantic matches, per-requirement evidence comparisons,
modality disagreements, and a list of deterministic requirements omitted by Gemini.
`combined` is explicitly advisory. A disagreement is marked for review, not hidden
by changing the original match score, eligibility or CV review gate.

## G. Cache design

SHA-256 identity covers exact JD text, a local fingerprint of the complete profile
(including evidence, source/project attribution and confidence), provider/model/API
configuration, prompt version, schema version and grounding-policy version.
The profile fingerprint is calculated locally; contact details and the whole
profile are not transmitted. Evidence/profile, JD, model or prompt changes cause
misses. A persistent hit avoids both provider calls. Cached payloads contain only
validated requirements and matches, never raw responses or credentials.

The command stores atomic, private-file cache entries under the already-ignored
`data/local/intelligence_cache`; custom stores must be kept private by their caller.
Failed calls are not cached, so a later explicit retry can recover. There is no
background retry, cache eviction policy, cross-process call coalescing or TTL;
model alias changes require an explicit model/version change or cache invalidation.

## H. Tests added

62 deterministic cases cover missing key; timeout/API/quota/authentication failure;
malformed extraction and matching; missing or cross-project/source IDs; Docker /
Kubernetes, AWS / Azure, Python / Java, RAG / training, AWS / serverless boundaries;
partial and cross-project confidence inflation; exact JD grounding; modality and
intake eligibility; genuinely supported REST/FastAPI relationships; explanation
invention; cache hits, input invalidation, corruption, revalidation and write failure;
input privacy; transport header/timeout behavior; and Daily Refresh isolation.

## I. Full regression results

Before changes: **470 passed, 1 skipped**. Initial sandbox execution had eight
localhost bind permission failures; rerunning with localhost permission confirmed
the stated baseline without code changes.

Final: **532 passed, 1 skipped** using:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

SHA-256 verification of all 150 pre-existing tracked/untracked source, configuration,
documentation and test files found no changes. No reset, clean, revert, checkout,
commit or push was performed.

## J. Live validation

**Not performed: GEMINI_API_KEY was absent in this environment.** No successful live
Gemini result is claimed. The real store was read successfully (520 jobs, 70 evidence
entries, candidate graduation year 2026). Four stored role-category smoke checks
returned `deterministic_fallback / not_configured`. No real job or profile was sent
or rewritten. These are fallback checks, not evidence of live model quality.

## K. Concrete improvements demonstrated with mocks

- `REST API required` with an existing `Built REST API endpoints using FastAPI`
  citation: deterministic capability-name matching has no REST capability; the
  validated semantic overlay exposes **partial** support and the exact project/source.
- A separate `Kubernetes required` remains **missing** alongside that supported REST
  relationship. Neither its JD grounding nor its unsupported status is lost.
- `Python preferred` retains preferred modality in the hybrid view.
- A 2027 graduate programme remains intake information, without excluding the
  candidate who graduates in 2026.

These prove the validation and integration behavior, not Gemini's live extraction
accuracy. Semantic recall is intentionally limited by the audited catalog.

## L. Grounding failures discovered

Adversarial mocked responses exposed the need to reject explanation-level invention
as well as invalid IDs. The final validator rejects extra explanation claims instead
of merely sanitizing them. Tests also cover context clipping, partial-evidence
inflation and borrowing confidence from other projects. No live hallucination rate
can be reported without credentials.

## M. Remaining limitations

Exact spans, extractive labels, conservative sentence modality and the relation
catalog can reject valid paraphrases, multiline headings and nuanced experience
requirements. This is a safety/recall tradeoff, not proof of complete semantic
understanding. RAG/serverless relations require suitable real capability entries;
the module does not manufacture missing tags or evidence. The full local profile
fingerprint can invalidate a cache for an unrelated profile edit. There is no
background/dashboard integration, learned relevance score or CV rewriting.

## N. Readiness for the next phase

Suitable as a conservative advisory foundation for a separately reviewed
project-selection experiment. **Not yet validated for unrestricted Gemini-assisted
CV wording or automatic project replacement.** Before activating those features,
run the small live role-diverse comparison with credentials, evaluate rejection and
miss rates, and add claim-level validation for any expanded relation or generated
wording. This phase stops here; email monitoring, auto-application, browser/login
automation, question drafting and Gemini CV rewriting remain out of scope.
