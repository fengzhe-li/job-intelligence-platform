# Job-Specific CV Generation

## What It Does

`jobintel.matching.cv_generation.generate_cv` produces a job-specific, evidence-grounded, **really rendered and page-counted** one-page CV (PDF) and persists it as an immutable artifact.

```bash
PYTHONPATH=src python3 -m jobintel.cli.main profile-set-contact --email you@example.com --phone "07700 000000" --github https://github.com/you
PYTHONPATH=src python3 -m jobintel.cli.main profile-rebuild --name "Your Name"
PYTHONPATH=src python3 -m jobintel.cli.main cv-generate --job-id <job-id> [--application-id <id>] [--print-cv]
PYTHONPATH=src python3 -m jobintel.cli.main cv-show --id <artifact-id>
```

## Pipeline

```text
Job (already ingested + enriched)
  -> extract_jd_requirements()          # analysis/jd_requirements.py
  -> score_projects_for_job()           # Phase 1: skill-evidence match + role-track relevance
  -> select_top_projects(max=4)         # dynamic, no hardcoded project list
  -> compose bullets per project        # JD-aware extractive rewriting (see below)
  -> select + reorder skills by JD relevance, dropping any "missing"-tier capability
  -> render_cv_pdf() + measure real page count, iterate to fit one page
  -> evaluate_review_gate()             # AUTO_PREPARE / REVIEW_REQUIRED / BLOCK_AUTO_SUBMISSION
  -> GeneratedCVArtifact                # persisted, immutable, with the PDF path
```

## Evidence-Grounded Bullet Rewriting

Bullets are **composed**, not generated: `matching.cv_generation._compose_bullets_for_project` builds each bullet from one or two real `CapabilityEvidence.quote` fragments, linked to the selected project via `CapabilityEvidence.project_id`.

What it does:
- **Reorders** — fragments are picked and ordered by JD relevance (required-skill matches first, then preferred, then general keyword overlap), not by their original position in the source text.
- **Emphasizes** — the highest-scoring fragment (most JD-relevant, highest confidence) leads; a lower-scoring but still relevant second fragment from the *same project* may be appended (`"; "`-joined) when it clears a relevance threshold.
- **Compresses** — fragments are truncated to a character budget (tighter when combined) with an ellipsis, never mid-word-invented text.

What it never does:
- Invent a technology, metric, responsibility, or outcome not present in the source quote.
- Strengthen a "partial"-tier capability's wording — extractive composition can't add qualifiers, so a partial-evidence bullet reads exactly as uncertain as its source text; the review gate separately flags partial-evidence matches (see below) rather than relying on wording alone.
- Combine evidence across two *different* projects into one bullet — `_collect_scored_quotes` only considers evidence whose `project_id` equals the project currently being composed for.
- Include GitHub language-breakdown evidence ("Python is 39% of the repo by bytes") as bullet prose — that's real, structured metadata used to verify a skill and populate the skills list, but it reads badly as an achievement bullet, so it's excluded from bullet composition specifically.

Every bullet retains `evidence_ids` (plural — a combined bullet cites all its constituent evidence) and `capability_names`, so `GeneratedCVArtifact.evidence_ids_used` is always traceable to real Evidence Bank entries.

## Real One-Page PDF Rendering

`jobintel.matching.cv_render` renders an actual PDF via `reportlab` (the project's one third-party runtime dependency, added specifically for this) using a conventional UK graduate software-engineering CV layout: Name + contact line → Education → Technical Skills → Projects → Experience. `SimpleDocTemplate.build()` performs real text flow/word-wrapping/pagination against real font metrics, and `.page` after `build()` is the true final page count — **not a line-count heuristic**. The rendered PDF is saved (`data/local/cv_artifacts/pdfs/<artifact-id>.pdf`) and its path is on the artifact.

Overflow is resolved by iteratively re-rendering and checking the real page count, in this priority order:

1. Drop the weakest bullet, keeping at least one bullet per selected project.
2. Drop the weakest whole project once every remaining project is down to one bullet (floor of 2 projects).
3. Compress bullet/skill wording further (progressively shorter character budgets).
4. **One** modest layout adjustment: margins 18mm → 15mm, body font 10pt → 9.5pt. Still comfortably professional — never tiny fonts or extreme margins, and never more than this one step.

If the CV still doesn't fit after all of that, `fits_one_page` is `False` and `page_count` reports the real number honestly — the review gate escalates to `REVIEW_REQUIRED` (2 pages) or `BLOCK_AUTO_SUBMISSION` (3+ pages) rather than silently claiming success.

## Candidate Contact/Identity Fields

`CandidateProfile.email`, `.phone`, `.location`, `.linkedin_url`, `.github_url` are set explicitly and only explicitly — never fabricated or inferred:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main profile-set-contact --email you@example.com --phone "07700 000000" --location "London, UK" --linkedin https://linkedin.com/in/you --github https://github.com/you
```

Stored in `data/local/profile/contact.json` (git-ignored, per-user runtime state) and merged into the profile on every `profile-rebuild`. `email` and `phone` are treated as required for a submittable CV; missing either downgrades the review gate from `AUTO_PREPARE` to at least `REVIEW_REQUIRED`, with a reason naming exactly which field is missing, rather than rendering a blank or placeholder value.

## Capability Recognition Beyond a Fixed Keyword List

Two complementary, both evidence-grounded, capability-discovery paths:

- **Keyword pattern matching** (`analysis.role_tracks.SKILL_PATTERNS`) — deterministic, ~115 entries (expanded from the original ~38 in this hardening pass) covering common languages, frameworks, databases, cloud/DevOps tooling, ML/AI libraries, and testing tools. Used for both JD requirement extraction and README/CV-text evidence extraction. Still a fixed, curated vocabulary by necessity — an open-vocabulary technology recognizer isn't feasible without an LLM, and a JD's free text has no structured signal to fall back on.
- **GitHub language-breakdown API** (`github_sync._fetch_languages`, `profile_ingestion.capabilities_from_github_languages`) — GitHub's own static analysis of a synced repo's language composition. This is real, structured, *verified* provenance (not a guess), so a language becomes a candidate capability even if its name never appears in `SKILL_PATTERNS` and without any source-code change — a future repo written in a language nobody thought to add to the keyword list is still automatically recognized. A language below 3% of a repo's bytes is dropped as noise. This still requires real evidence with real provenance; an unrecognized name never silently becomes a "verified" skill from JD text alone.

## Human Review Gate

`jobintel.matching.review_gate.evaluate_review_gate` (`models.taxonomy.ReviewGateStatus`) classifies every generated CV, escalating severity (never downgrading) across all of:

- **Missing required-skill evidence** — none missing: `AUTO_PREPARE`. A minority missing: `REVIEW_REQUIRED`. A majority missing: `BLOCK_AUTO_SUBMISSION`.
- **Partial-only evidence** for a required skill (evidence exists but isn't strong) — escalates to at least `REVIEW_REQUIRED`, with the specific skill(s) named.
- **Explicit graduation-year exclusion** — `BLOCK_AUTO_SUBMISSION`.
- **Explicit no-sponsorship statement** — escalates to at least `REVIEW_REQUIRED`.
- **3+ years' experience required** — escalates to at least `REVIEW_REQUIRED` (verify it's genuinely graduate-accessible).
- **Missing required contact details** (email/phone) — escalates to at least `REVIEW_REQUIRED`.
- **Doesn't fit one page** even after full trimming — `REVIEW_REQUIRED` at 2 pages, `BLOCK_AUTO_SUBMISSION` at 3+.

A high superficial keyword match never bypasses this: the gate looks at which required skills are actually evidence-backed (and how strongly), not at the JD text alone.

## Persistence: "What Exact CV Did I Submit?"

`storage.cv_artifact_store.CVArtifactStore` persists every generation as an append-only, immutable JSONL record — generating a new CV for the same job never overwrites a previous one. Each `GeneratedCVArtifact` carries the job id, candidate id, JD snapshot, selected project ids, evidence ids used, the rendered PDF path and real page count, the full CV text, and the review decision.

```bash
PYTHONPATH=src python3 -m jobintel.cli.main cv-generate --job-id <job-id> --application-id <application-id>
PYTHONPATH=src python3 -m jobintel.cli.main application-inspect --id <application-id>   # shows cv_version_id
PYTHONPATH=src python3 -m jobintel.cli.main cv-show --id <artifact-id>                  # exact text, PDF path, evidence used
```

`--application-id` calls `ApplicationStore.attach_cv`, setting `Application.cv_version_id`, `selected_project_ids`, and `evidence_ids_used`.

## Production-Readiness Classification

See the Phase 2B section of `README.md`'s Status for the full per-feature classification (`PRODUCTION-READY` / `PARTIAL — HUMAN REVIEW REQUIRED` / `NOT READY`). In short: evidence-grounding, contact-field honesty, real page counting, and the review gate's escalation logic are production-ready; bullet wording quality (grammatically rough since it's extractive, not rewritten) and project selection for niche/cross-domain roles remain human-review items; JD requirement recognition is bounded by the fixed keyword list on the JD side (no GitHub-languages-style structured fallback exists for free-text JDs).
