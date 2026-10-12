# CV Impact & Ownership Writing (CV_IMPACT_OWNERSHIP_V1)

Generated CVs were factually strong but often read as noun fragments and architecture inventories
("An event-driven backend: …", "FastAPI REST API backend serving …"). This layer makes verified
evidence read as **action + owned artifact / mechanism + supportable engineering value** without
weakening evidence grounding. Evidence truth remains the highest authority:

`evidence_truth > jd_relevance > evidence_strength > recruiter_clarity > non_redundancy > one_page_readability`

## Where it runs

```text
JD-aware project selection (unchanged)
  → existing evidence selection (unchanged: verbatim quotes of the project's own evidence)
  → Impact & Ownership Bullet Composition      matching/impact_writing.compose_project
  → Engineering Value Translation              matching/impact_writing.translate_value
  → Claim Grounding / Overclaim Review         matching/claim_grounding.validate_bullet_claim (+ absolute/performance review)
  → Writing Quality Gate                       matching/impact_writing.writing_quality_report
  → one-page composition / PDF rendering       matching/cv_generation._PageFitter (real PDF measured)
  → human review (review gate)                 matching/review_gate (writing_review_reasons)
  → immutable artifact                         storage/cv_artifact_store (append-only)
```

All rules are data in `data/policies/cv_impact_ownership_v1.yaml`, hash-bound to its (private)
source asset like the tailoring skill. `generate_cv(writing_policy=None)` reproduces the previous
purely extractive output exactly.

## What the composer may do

| Change | When | Review |
|---|---|---|
| Put an action verb before a noun-fragment opening (`Built` / `Developed` / `Implemented` / `Defined` / `Designed`, chosen from the fragment's head noun, or the fragment's own passive participle) | only when every cited evidence item is the candidate's own project write-up | no (style) |
| Remove an opening performance/prestige adjective (`High-throughput`, `Comprehensive`, …) | when the same evidence contains no measurement of it | no (narrowing) |
| `Supported X by checking A, identifying B` → `Checked A, identified B, in support of X` | internship statements; every activity must be the candidate's own gerund | yes (ownership wording) |
| Append one short value clause per project (`, giving repeatable regression checks`) | the mechanism is in the same evidence, the value is foregrounded for the JD archetype, the bullet stays ≤ 215 chars | yes (value framing) |
| Rotate interchangeable verbs (Built / Developed / Implemented) | a verb would open more than 3 lines | no |

Everything after the noun phrase stays verbatim. `Assisted …`, `Participated …`, `Worked with …`
openers are never upgraded. `Architected / Led / Spearheaded / Owned / Orchestrated` are never inserted.

A rewrite is **refused** (verbatim evidence kept, reason recorded) if it loses a metric span, adds a
number or `%`, drops a qualifier (synthetic, simulator, locally, final, strict, subset, prototype,
supported, …), adds an escalation verb, introduces an overclaim, or adds any claim-grounding
rejection the verbatim evidence did not already carry.

## Engineering Value Translation

Ladder: L1 measured business outcome (never produced: no attributed business measurement exists),
L2 measured engineering outcome (only with its measurement in the same evidence, e.g. bounded
trials), L3 verified engineering delivery, L4 concrete contribution. Translations (CI + tests →
repeatable regression validation, deduplication → duplicate-safe processing, timestamp guards →
out-of-order protection, citations bound to generated text → traceability, strict schemas →
contract validation, Docker → reproducible packaging, …) never produce high availability, low
latency, production scale, revenue or SLA claims.

The JD archetype (`forward_deployed_ai`, `backend`, `platform_systems`, `automation_embedded`,
`fullstack`, `ai_applied`) only re-weights which verified bullets get space first.

## Metric classification

Every number-bearing span keeps its verbatim text, type, scope, denominator and context:
`5/5 final trials` → bounded validation trials (not availability); `8/10` → bounded experiment (not
80% uptime); `102 tests` → test count (not coverage / E2E / reliability %); `513 simulator trials`
→ simulator dataset (not road trials); `15 external evaluation cases` → evaluation sample (not
customer adoption). Counts are never converted to percentages.

## Overclaim review

`claim_grounding.validate_bullet_claim` now also rejects unsupported absolute/performance wording
(zero-downtime, zero-loss, guaranteed, production-ready/-scale, sub-second, real-time,
high-throughput, low-latency, high availability, fault-tolerant, coverage/reliability %, "X%
faster", time saved, customer deployment, "trained an LLM", team leadership — with hyphenation
variants). Measurement-class terms need a measurement in the cited evidence itself.

## Writing Quality Gate (diagnostics only)

Recorded on the artifact as `writing_quality`: action/collaborative/noun-fragment/inventory/
metric-first opening counts and ratios, first-bullet ownership per project, delivery/outcome
bullets, metrics with provenance, repeated verbs and 4-word phrases, unsupported generic value
language, unsupported absolute claims, ownership-escalation verbs, cross-project technology leakage,
lost qualifiers, exact-repo corroboration of risky claims against the project's own cached README,
summary narrowing, and every rewrite / refusal. These never authorise a rewrite.

## Review gate and immutability

Rewrites that change ownership wording or add value framing, unsupported absolute wording, claims
not found in the project's own cached README, and cross-project leakage escalate the CV to
`review_required`. Each bullet carries its audit record in `ProjectBullet.provenance`
(`source_project_id`, `evidence_ids`, `original_text`, `rewritten_text`, `action_verb`,
`owned_artifact`, `technical_mechanism`, `engineering_value`, `value_level`, `metric_scope`,
`jd_relevance`, `ownership_truthfulness`, `overclaim_flags`, `rewrite_rationale`, `refused`,
`review_required`). Existing artifacts are never rewritten; records written before the policy load
with empty provenance.

## Known limits

Head-noun and value rules are deterministic heuristics over English phrasing; anything they do not
recognise is left verbatim. Exact-repo corroboration only sees the cached top-level README of a
repository whose name matches the project's recorded repository exactly; renamed repositories are
reported as `cache_unavailable`.
