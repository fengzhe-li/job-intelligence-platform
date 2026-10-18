# Job Intelligence Platform

A supervised job-intelligence and application-automation platform for UK graduate software roles.

- **Job intelligence.** It discovers jobs from employer ATS feeds, graduate boards and aggregators, merges duplicates across sources, and ranks each job against an evidence-backed candidate profile with a per-component explanation.
- **Grounded CVs.** It generates job-specific one-page CVs from cited evidence, behind a human review gate.
- **Supervised applications.** It turns a human-approved application package into a filled application form in a local browser:
  - deterministic answers are filled;
  - the approved CV is uploaded;
  - every question that needs the candidate is escalated;
  - the window is then handed back. **The final Submit is always the human's.**

The current engineering workflow combines complete-scope ingestion, JD-aware evidence selection, immutable application artifacts, and sequential browser handoff. Private profiles, generated documents and application history remain local.

| | |
|---|---|
| **Stack** | Python 3.12+ (standard library first), Playwright over the Chrome DevTools Protocol (local headed Chrome; Browserbase as an alternative transport), ReportLab PDF rendering, append-only JSON/JSONL stores with file locking, a stdlib HTTP dashboard, pytest and GitHub Actions |
| **Engineering areas** | Multi-source data pipeline and deduplication · explainable ranking · evidence-grounded generation with claim validation · approval and execution state machines over an append-only ledger · browser automation on a real single-page-app ATS · network-level guardrails (exact host / method / GraphQL-operation policy) · security- and regression-focused tests |
| **Safety model** | Sealed approved packages · authoritative candidate facts · deterministic answers only · ambiguity escalates · exact write policy · CV integrity (SHA-256) · no automated final Submit |

It is a personal system published as a software-engineering portfolio project, **not** a hosted product or a general-purpose auto-applier. Personal data (candidate profile, candidate facts, CVs, applications, ledgers, browser runtime state) lives in git-ignored local stores and is not in this repository.

![Dashboard: Today view](docs/images/dashboard-today.png)
<sub>The **Today** page: every count links to the underlying records. All screenshots in this README use synthetic fixture data and a fictional candidate; their displayed counts are illustrative, not the current test results.</sub>

---

## Contents

- [Why this exists](#why-this-exists)
- [Workflow at a glance](#workflow-at-a-glance)
- [Architecture](#architecture)
- [Source coverage model](#source-coverage-model)
- [Deduplication, history and closure](#deduplication-history-and-closure)
- [Evidence-grounded candidate model](#evidence-grounded-candidate-model)
- [Matching and ranking](#matching-and-ranking)
- [CV generation workflow](#cv-generation-workflow)
- [Application lifecycle](#application-lifecycle)
- [Supervised application execution](#supervised-application-execution)
- [Validation boundaries](#validation-boundaries)
- [Architecture evolution](#architecture-evolution)
- [Optional Gemini advisory layer](#optional-gemini-advisory-layer)
- [Dashboard](#dashboard)
- [Repository structure](#repository-structure)
- [Quick start](#quick-start)
- [Testing](#testing)
- [Privacy and local data](#privacy-and-local-data)
- [Known limitations](#known-limitations)
- [Status](#status)

---

## Why this exists

UK graduate hiring is spread across hundreds of employer ATS boards (Greenhouse, Lever, Ashby, Workday and others), aggregators, and graduate boards, several of which have no public API. The same vacancy often appears in three places with slightly different text, and the deadline, sponsorship and graduation-year details that decide eligibility are buried in free-text job descriptions.

The project treats this as an integration and data-quality problem, not a keyword search:

- pull from compliant, documented public endpoints and record honestly what could **not** be checked automatically;
- merge what is really the same vacancy, but never hide two real postings behind a heuristic;
- never infer that a job has closed unless the source's full listing was actually observed;
- score jobs against evidence of what the candidate has actually built, with every score explained;
- tailor CVs by *selecting and narrowing* real evidence, never by inventing it, and keep a human in the loop for every application.

## Workflow at a glance

```text
daily-refresh ─► registry ATS boards + Adzuna + Prospects ─► normalise ─► enrich ─► dedup ─► history/closure
                                                                                                │
manual imports (Trackr, Gradcracker, Bright Network, any URL) ──────────────────────────────────┤
                                                                                                ▼
                        explainable ranking ◄── Evidence Bank (CV, projects, pinned repository evidence)
                                │
                                ▼
       Today / Priority queue ─► CV workbench ─► human review ─► submit manually ─► application tracker
```

A typical day: run **Daily Refresh** (dashboard button or `daily-refresh`), open **Today** to see new relevant jobs, failed sources and manual checks due, work the **Priority** queue, generate a tailored CV for a strong match, review it, apply on the employer's own site, then record the submission. The CV version is then frozen against that application.

## Architecture

![System architecture](docs/images/system-architecture.svg)

- **Connectors** share one `JobSourceConnector` interface and return a `SourceSnapshot`: the payloads plus the scopes (board, site, tenant) whose *entire* listing was observed. That completeness signal is what makes safe closure inference possible.
- **Pipeline** stages are deterministic and plain Python: normalisation to a canonical `Job`/`SourceObservation` model, enrichment (role track, seniority, sponsorship, graduation year), layered deduplication, history tracking and scoring.
- **Candidate side**: an Evidence Bank of cited capabilities, JD requirement extraction, per-JD project selection, CV composition, claim grounding, real PDF rendering, the review gate and application tracking.
- **Interfaces**: a CLI and a local dashboard built on the standard library's `http.server`. Both read the same store.
- **Storage** is plain JSON/JSONL under `data/local/`. `data_contracts/schemas/postgres.sql` is a schema contract for a possible future persistence layer; **no code path connects to PostgreSQL today**.

Runtime dependencies are deliberately small: `reportlab` (real PDF layout and page counting) and `PyYAML` (loading the versioned CV-tailoring policy). Connectors use `urllib`, the dashboard uses `http.server`, and deduplication uses `difflib`.

### Daily refresh and data flow

![Daily refresh pipeline](docs/images/daily-refresh-pipeline.svg)

`daily-refresh` runs the automated sources in isolation, so one failing board never stops the others. It then logs employers that aren't in the registry yet and, optionally, syncs GitHub evidence. It persists a report that keeps five outcomes separate: *succeeded*, *succeeded with zero results*, *partial*, *failed* and *not configured*. A broken source is never shown as "0 new jobs".

## Source coverage model

Coverage metadata is generated from code (`src/jobintel/source_coverage.py`, `company_coverage.py`), not hand-maintained prose. `coverage-today` and the **Source Health** page read it together with live health data.

| Source | Access | Status in code | Notes |
|---|---|---|---|
| Greenhouse, Lever, Ashby, Workable, SmartRecruiters | Public job-board APIs | `PRODUCTION_READY` for registry-configured boards | Only companies already in the registry. This is not "every company on that ATS". |
| Workday | Public CXS careers-site endpoint | `PRODUCTION_READY` for configured tenants | Generic connector; 3 tenants enabled in the published registry. |
| Welcome to the Jungle | Official WelcomeKit Jobs API | Implemented, **key-gated** | Needs `WTTJ_API_KEY` and per-company organisation references. The published registry configures none, so in practice WTTJ is used through `wttj-import` / `manual-job-import`. |
| Adzuna | Official search API | `PARTIAL_COVERAGE` | Needs `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` (reported as *not configured* otherwise). Paginates up to ~1000 results per refresh. Descriptions are often snippets, links may point at Adzuna rather than the employer, and deadlines are usually absent. **Never used for closure.** |
| Prospects | schema.org `JobPosting` data | `PARTIAL_COVERAGE` | Two sector categories (IT, engineering). It gives real deadlines, but it isn't a complete scope, so it can never close a job. |
| Trackr, Gradcracker, Bright Network | none viable | `MANUAL_FALLBACK_ONLY` | Cloudflare-protected or rate-limited to the point of being unusable. Imported by hand through the same pipeline and tracked on a manual **Watchlist**. |

**Company registry.** `config/target_companies.json` holds 83 target employers. `company-coverage-report` classifies them into five states; the published config currently reports **37 AUTO_VERIFIED, 39 MANUAL_REQUIRED, 7 TEMPORARILY_FAILED** (44.6% automatic coverage *of this registry*, not of the UK graduate market). `ats-discover` / `ats-verify` probe the careers URLs of registry companies, and `ats-exclude` makes a manual exclusion persist.

**Company discovery.** `company-discovery-scan` finds employers that appear in ingested jobs but aren't in the registry. It records provenance and an ATS hint when the application URL reveals one. Promotion into the registry is an explicit, reviewed step (`company-discovery-promote`).

![Source and company health](docs/images/source-health.png)
<sub>**Source Health**: each source's state, last attempt vs. last success, closures and its coverage limitation, followed by the five-state company breakdown.</sub>

## Deduplication, history and closure

![Deduplication flow](docs/images/deduplication-flow.svg)

- **Layered matching** (`dedup/v1.py`) checks, in order: same source ID, then same canonical application URL, then same company + title + location, then a fuzzy title match (`SequenceMatcher` ≥ 0.88). The last two also require matching seniority markers. A guard keeps two different IDs from the *same* source apart, because they are real separate postings.
- **Source precedence:** when observations merge, a direct employer/ATS record (company, Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday) supplies the title, JD and salary ahead of aggregator or manual copies. Every observation is still kept for provenance.
- **History:** each observation carries `first_seen_at` / `last_seen_at` and a state (`NEW`, `CHANGED`, `UNCHANGED`, `DISAPPEARED`, `REAPPEARED`). Jobs are never deleted, and deadlines are stored per observation so conflicting deadlines stay visible.
- **Closure invariant:** a job can be marked `DISAPPEARED` only if it is absent from a scope the connector *proved* it observed completely. Page caps, per-identifier limits, keyword filters, partial failures and suspicious collapses (0 results, or <20% of the last healthy count) all withhold closure. Adzuna, Prospects and manual imports can never close anything.
- `repair-historical-dedup` re-applies the current rules to stores merged under older ones and takes a backup first.

## Evidence-grounded candidate model

The candidate profile is an **Evidence Bank**. Each capability (e.g. *PostgreSQL*) carries the exact quote it was extracted from, the source (CV text, project description, README, education), the project it belongs to, a confidence value, and a derived tier: *strong*, *partial* or *missing*.

- `profile-import` / `profile-rebuild` extract capabilities from local files. `profile-set-contact` stores contact details, which are never inferred or fabricated.
- `github-sync` incrementally discovers the account's public repositories, with no hard-coded list. It feeds READMEs and GitHub's language breakdown into the same extraction path.
- **Curated evidence.** A local enrichment step adds hand-curated, atomic evidence items through a *stage → diff → validate → promote* flow, and its validator rejects known over-claims ("forbidden extrapolations"). That content describes my own projects, so the tool and its data are kept out of this repository. What is published is the store-side contract: once a curated bank is promoted, `profile-rebuild` refuses to overwrite it with raw keyword hits (`tests/test_profile_rebuild_promoted.py`).
- The **graduation year** of the candidate is kept separate from a job's **intake year**. A 2027-intake graduate scheme can still be open to a 2026 graduate, and the matcher and review gate treat the two differently.

## Matching and ranking

![Explainable ranking](docs/images/explainable-ranking.svg)

`matching.matcher.match_job` returns 12 named components instead of one opaque score: technical match, role function, role preference, engineering domain, market fit, seniority, required experience, sponsorship, graduation year, location, source quality and freshness. Each result carries the cited candidate evidence that matched, the gaps, and an assembled explanation. Excluded occupations (e.g. sales) short-circuit to a single component and are kept at the bottom of the list, not deleted. Weights live in `config/personal_strategy.json`.

Role tracks (backend, data, cloud, platform, AI/ML, network software, telecoms, embedded, IoT and others) are inferred deterministically from title and JD phrases, with cross-track signals recorded.

<table>
<tr>
<td width="50%"><img src="docs/images/job-detail-explainability.png" alt="Job detail explainability view"></td>
<td width="50%"><img src="docs/images/dashboard-priority-queue.png" alt="Priority queue"></td>
</tr>
<tr>
<td><sub><b>Job detail</b>: normalised fields, source observations, sponsorship / graduation-year / seniority evidence, the role-track explanation and the matched candidate evidence.</sub></td>
<td><sub><b>Priority queue</b>: candidate-fit score plus small, named adjustments (deadline, not yet applied, CV readiness). The "why" is shown per row.</sub></td>
</tr>
</table>

## CV generation workflow

![Evidence Bank to grounded CV](docs/images/evidence-grounded-cv-flow.svg)

`cv-generate` (or **Regenerate CV** in the dashboard) runs this pipeline:

1. **JD requirement extraction** (`analysis/jd_requirements.py`): required vs. preferred skills, years of experience and role track.
2. **Grounded project selection** (`matching/project_selection.py`, `policy_selector.py`): projects are ranked *per job* by skill-evidence match and role-track relevance, under a versioned tailoring policy (`data/policies/cv_tailoring_skill_v1.yaml`). Usually 3–4 projects are chosen, with no fixed quota and no global "best projects" order.
3. **Bullet composition** from that project's own evidence quotes only; evidence is never borrowed across projects.
4. **Claim grounding** (`matching/claim_grounding.py`): every technology, metric, scale or outcome in a bullet must appear in that project's evidence.
5. **Real one-page layout** (`matching/cv_render.py`, `reportlab`): the PDF is actually rendered and its true page count measured. On overflow the generator trims the weakest bullet, then the weakest project, then tightens wording, and allows one modest layout step. It never shrinks to tiny fonts. Page utilisation is also checked, so a half-empty page is flagged.
6. **Review gate** (`matching/review_gate.py`): `AUTO_PREPARE`, `REVIEW_REQUIRED` or `BLOCK_AUTO_SUBMISSION`, with reasons such as missing required skills, partial-only evidence, an explicit graduation-year mismatch, no sponsorship, missing contact details, page count, or missing sections.
7. **Immutable artifact** (`storage/cv_artifact_store.py`): an append-only JSONL record that holds the JD snapshot, the selected project and evidence IDs, the PDF path, the real page count, the full text and the review decision. Regenerating creates a new version.

`AUTO_PREPARE` means the automated checks passed. It does **not** mean the CV is safe to send unread. The dashboard says so on every CV, and submitting requires an explicit "I have reviewed this exact version" confirmation. A regression suite (`tests/test_cv_tailoring_harness.py`) pins the policy's decisions on 28 historical tailoring cases.

<table>
<tr>
<td width="55%"><img src="docs/images/cv-workbench.png" alt="CV workbench"></td>
<td width="45%"><img src="docs/images/cv-sample-output.png" alt="Sample generated one-page CV (fictional candidate)"></td>
</tr>
<tr>
<td><sub><b>CV workbench</b>: version, review status, real page count and utilisation, selected projects and the exact evidence IDs used.</sub></td>
<td><sub><b>Rendered output</b> for the fictional test candidate "Alex Example": every project bullet traces to a cited evidence quote.</sub></td>
</tr>
</table>

## Application lifecycle

```text
discovered → shortlisted → materials_ready → applied → online_assessment
           → phone_screen → technical_interview → final_interview → offer
terminal: rejected · withdrawn · expired
```

- Ownership is split by phase: pre-application triage (`new`, `saved`, `ignore`) lives with the job, and everything from submission onward lives in `ApplicationStore`. There is one read projection, so the Inbox, Priority and Today pages can't disagree.
- Status history is **append-only** JSONL. A submitted application can't regress to a pre-submission state, and each job has at most one application (get-or-create, idempotent).
- At creation, an application snapshots the JD, the application URL, source provenance and the deadline. The CV is **frozen at submission**, and attaching a different version afterwards is refused. You can also record "applied manually without a system CV" honestly, without a CV being linked after the fact.
- Status changes are entered by the user. There is no email parsing and no automatic status detection.

<table>
<tr>
<td width="50%"><img src="docs/images/application-tracker.png" alt="Application tracker"></td>
<td width="50%"><img src="docs/images/application-detail.png" alt="Application detail with append-only history"></td>
</tr>
<tr>
<td><sub><b>Application tracker</b>: lifecycle status, submission time, the CV version used and the deadline.</sub></td>
<td><sub><b>Application detail</b>: append-only history, submitted-CV record, source provenance and the JD snapshot.</sub></td>
</tr>
</table>

## Supervised application execution

The submission subsystem (`src/jobintel/submission/`) takes a job whose CV has been reviewed and carries it to a filled application form. It stops short of the final action.

![Supervised application workflow](docs/images/supervised-application-flow.svg)

**Lifecycle.** Each step is a separate `submission-*` CLI command over an append-only ledger:

```text
preview ─► approve (named reviewer, acknowledged gate reasons, expiry) ─► enqueue (one executor, one exact host)
        ─► claim (lease + claim token) ─► execute (supervised, stop-only) ─► needs_human_input / ready_for_human_review
        ─► human handoff ─► release ─► close_not_submitted, or the human submits and records a verified receipt
```

- Revocation, expiry, supersession and every execution event are append-only.
- An interrupted run becomes `outcome_unknown` and is never retried automatically.
- An application is marked applied only from a verified receipt.

**Apply one application.** For day-to-day use, four `submission-apply-*` commands run that lifecycle for one application id. Each step is still explicit and nothing is collapsed into a single command:

```bash
# 1. Validate, and show the exact package hash. Starts nothing.
PYTHONPATH=src python3 -m jobintel.cli.main submission-apply-preview --application-id <id>
#    A package blocked by CV review needs a named human's explicit acknowledgement of exactly
#    its listed reasons. This authorizes one supervised attempt, never a final submit:
PYTHONPATH=src python3 -m jobintel.cli.main submission-apply-preview --application-id <id> \
    --acknowledge-blockers --reviewer "<name>" --ack-reason "<why>"
# 2. Approve exactly that hash and run ONE supervised attempt. The browser window is handed to you.
PYTHONPATH=src python3 -m jobintel.cli.main submission-apply-start --application-id <id> \
    --package-hash <hash from preview> --reviewer "<name>" --reviewed
# 3. Afterwards:
PYTHONPATH=src python3 -m jobintel.cli.main submission-apply-status  --application-id <id>
PYTHONPATH=src python3 -m jobintel.cli.main submission-apply-release --application-id <id>
PYTHONPATH=src python3 -m jobintel.cli.main submission-close --request-id <id> --reviewer "<name>" \
    --confirmed-not-submitted --reason "<why>"   # only if you did not submit
```

The network policy comes from an **ATS execution profile**, matched on the application URL's exact host (`src/jobintel/submission/ats_profiles.py`). There is no fallback for an unknown host.

| Hosted ATS | Profile state | Execution |
|---|---|---|
| Ashby | Supervised handoff validated | Exact-host policy; human final Submit |
| Greenhouse | Supervised handoff validated | Exact-host policy; sealed CV upload |
| Lever | Fixture-tested; live validation pending | Refused by default; explicit validation-run opt-in required |
| Workable | Fixture-tested; live validation pending | Refused by default; human-verification holds apply |

**Batch preparation.** `batch-prepare` reuses unchanged CV artifacts and prepares a review queue; it does not apply. `batch-approve` binds one human decision to one package hash. `batch-run-next` executes approved items sequentially under one executor lock, placing each application in its own tab. Employer limits and human-verification widgets can hold an item before a tab opens. Each item retains its own state and failure details; unknown ATS hosts remain visible for manual handling.

**Evidence and document preparation.** Repository capability maps inspect source files, tests, manifests and delivery configuration at a recorded commit without executing repository code. JD extraction separates required, optional and alternative skills. The editorial layer selects complementary evidence, aligns supported wording, normalises the target title, and fits the CV to one page. Cover-letter drafts use the selected CV and job description; generated PDF/DOCX files and learning backlogs stay in the local store and require human review.

**Safety model.**

| Guarantee | How it is enforced |
|---|---|
| Only reviewed content is used | The package is sealed: it binds the CV artifact, its PDF SHA-256, every answer with its provenance, the application URL and the review inputs. Any change gives a new hash, and approvals are bound to that hash. |
| Answers come from facts, not generation | The executor fills only values that resolve deterministically from the authoritative Answer Bank: contact, location and LinkedIn / GitHub. Work-authorisation and immigration questions always stay with the human. Each label maps to exactly one field. A LinkedIn field never receives GitHub, and the reverse. |
| Unknown, sensitive or ambiguous questions escalate | Unmapped labels, ambiguous profile-link fields, duplicated labels and free-text questions become `needs_human_input`. They are never generated. |
| The browser can only do what was approved | One exact https host. GraphQL requests are parsed locally and allowed only as exact `(endpoint, operationName)` reads. Per-field autosave mutations are allowed only while one approved field is being filled. Third-party telemetry is aborted but non-fatal only when listed as an exact `(method, host)`. Every other write stops the run. No wildcards, no suffix matching, no learning hosts automatically. |
| The CV is exactly the approved file | It is re-hashed from the sealed bundle and uploaded inside one CV-upload window. The window allows only the upload-handle mutation, the exact object-storage write (whose readable body must contain the sealed bytes) and one form-association mutation that names this upload. `uploaded` requires every step, successful responses and the file name visible in the page. |
| Never a final Submit | Every submission path is blocked in-page and on the network while the runner works. There is no option or code path that clicks Submit. At handoff the runner removes its routes and releases its own guard with a per-run secret, then leaves the window to the human. `final_submit_clicked` is always false. |
| Isolation | Local runs use a fresh, empty Chrome profile under the git-ignored `data/runtime/`, with remote debugging on `127.0.0.1` only. Single-run release ends only its verified process; batch release closes only the request-bound tab. Claim tokens, debug URLs, cookies, headers, form values and request bodies are never logged. |

**Data separation.**

| Kind | Where | In git? |
|---|---|---|
| Authoritative source data: Evidence Bank, long-form experience master, candidate facts / Answer Bank | `data/local/profile/`, git-ignored `candidate_facts_local.py` | no |
| Generated application artifacts: CV versions and PDFs (with SHA-256), applications, approvals, requests, sealed bundles, ledgers | `data/local/` | no |
| Ephemeral runtime state: isolated Chrome profiles, browser session records | `data/runtime/` (never backed up or copied with `data/local`) | no |
| Code, policies, synthetic fixtures and tests | this repository | yes |

Without the local candidate-facts module the public code still runs. The Answer Bank then holds only policies, and every candidate-specific question fails closed to human input. Tests use a synthetic candidate ("Alex Example").

**Executors.**
- `local_browser` (primary): local headed Chrome.
- `browserbase`: the same runner in a cloud browser, kept as an alternative transport and regression path.
- `airtap`: an offline, supervised-only adapter.
- `manual`.

See [Submission MVP](docs/SUBMISSION_MVP.md) for commands, storage, invariants and recovery, and [AirTap integration](docs/AIRTAP_INTEGRATION.md).

**Convenience review copy.** Attaching a CV also writes a byte-identical copy (SHA-256 verified) to `~/Downloads` for reading. It is never the source of truth and never overwrites a different file.

## Validation boundaries

The offline suites use synthetic candidates and locally served ATS-style forms. They test identity checks, exact sealed-file uploads, network guards, human-only questions, sequential batch execution and tab isolation. Ashby and Greenhouse profiles carry supervised-handoff validation status; Lever and Workable remain gated pending a successful validation run. This is not a claim that every employer form on those vendors is supported.

Live Browserbase and source probes are opt-in. The public test run does not require a candidate profile, application history, answer bank, credentials or a signed-in browser. Generated cover letters are drafts: their templates and eligibility assumptions still need human review for the particular candidate and vacancy.

## Architecture evolution

The execution path went through three approaches:
1. **AirTap**: an external agent. It could not receive a file, and an instruction is not an enforced guard.
2. **Browserbase**: a cloud browser. It validated the runner on a real ATS, but quota (HTTP 402) and the third-party dependency made it a poor primary path.
3. **Local headed Chrome**: the same runner, now the primary path.

Real runs also showed:
- the SPA shell renders before its form;
- cloud latency had masked that timing;
- telemetry beacons trip a strict write guard;
- a visible file name is not proof of an upload;
- the CV upload is a four-step flow;
- the submit guard must be released deliberately at handoff.

The full write-up, with the run-by-run evidence and the decision each stop led to, is in **[docs/ARCHITECTURE_EVOLUTION.md](docs/ARCHITECTURE_EVOLUTION.md)**.

## Optional Gemini advisory layer

`src/jobintel/intelligence/` is an **optional, non-authoritative** layer. It runs only when `GEMINI_API_KEY` is present in the environment; the key is never read from files or config. It does two-stage JD interpretation with exact-span validation against the JD text, proposes evidence relations from an audited catalogue, and prints a comparison against the deterministic baseline (`python -m jobintel.intelligence --job-id …`). Results go to a content-addressed cache.

- Without a key it makes no network request and returns the deterministic result.
- Nothing in ranking, eligibility, project selection or the dashboard/CLI CV paths depends on it. The CV generator has a hook that accepts model-proposed bullet rewrites, but only if each one passes the same claim-grounding validator. The default CV commands don't enable that hook.

See [docs/GEMINI_INTELLIGENCE.md](docs/GEMINI_INTELLIGENCE.md).

## Dashboard

`PYTHONPATH=src python3 -m jobintel.cli.main dashboard` serves a local, single-user, bilingual (English/Chinese) UI at `http://127.0.0.1:8765`:

| Page | Route | Purpose |
|---|---|---|
| Today | `/today` | What needs doing now: new relevant jobs, CVs to review, deadlines, failed sources, manual checks due |
| Inbox | `/inbox` | Filterable ranked shortlist with saved searches and refresh summary |
| Job detail | `/job?id=…` | Full explainability for one job |
| Priority | `/priority` | Action queue with the reason for each position |
| Deadlines | `/deadlines` | Jobs bucketed by deadline proximity |
| CV workbench | `/cv?job_id=…` | Generate, inspect, attach and submit CV versions; `/cv/pdf` serves the exact PDF |
| Applications | `/applications`, `/application?id=…` | Lifecycle list and detail |
| Watchlist | `/watchlist` | Manual-only sources and companies, each with a check cadence |
| Add Job | `/manual-import` | Import one job by URL through the canonical pipeline |
| Source Health | `/health` | Per-source and per-company coverage state |

POST actions cover daily refresh, saved searches, workflow status, CV generate/attach, application create/status/submit, watchlist check/snooze, and manual import. The server has no authentication and is meant for `127.0.0.1` only.

![Inbox](docs/images/dashboard-overview.png)
<sub><b>Inbox</b>: metrics, refresh summary, per-source status line, presets, saved searches and filters.</sub>

## Repository structure

```text
src/jobintel/
  connectors/        Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, WTTJ,
                     Adzuna, Prospects + manual_source / graduate_sources (manual import)
  pipeline/          ingestion, refresh (snapshot + health), daily (one-command orchestration)
  dedup/             v1 layered dedup, repair (historical re-dedup with backup)
  analysis/          role tracks, enrichment, job quality, jd_requirements
  matching/          matcher, project_selection, policy_selector, tailoring_skill,
                     claim_grounding, cv_generation, cv_document, cv_render, review_gate
  intelligence/      optional Gemini provider, grounding, service (advisory only)
  submission/        approval/execution service and ledger, sealed packages, Answer Bank and
                     question resolution, stop-only runner, GraphQL policy, local Chrome and
                     Browserbase transports, AirTap adapter, submission CLI
  storage/           local JSON/JSONL stores: jobs, applications, CV artifacts, submission
                     ledger, repositories, company discovery, watchlist; file locking
  dashboard/         server (stdlib http.server), pages, service, operations, i18n
  models/            job, candidate, application, cv_artifact, taxonomy dataclasses
  cli/main.py        40-subcommand CLI
  profile_ingestion.py · evidence_enrichment.py · github_sync.py
  company_registry.py · ats_discovery.py · company_discovery.py
  company_coverage.py · source_coverage.py · application_lifecycle.py · history.py
config/              ranking strategy + 83-company target registry
data/policies/       versioned CV tailoring policy (YAML)
data_contracts/      PostgreSQL schema contract (not wired up)
docs/                design notes per subsystem (see below)
tests/               pytest suite + synthetic fixtures
```

## Quick start

Requires Python ≥ 3.12.

```bash
python3 -m pip install -e ".[dev]"

# Ranked list from built-in synthetic fixtures (no network, no credentials)
PYTHONPATH=src python3 -m jobintel.cli.main list-ranked --fixtures

# Pull live jobs from public boards / Prospects into a scratch store
PYTHONPATH=src python3 -m jobintel.cli.main ingest --greenhouse-board <board> --prospects --store /tmp/jobintel-demo --limit 50

# One daily command over the registry (Adzuna / WTTJ report "not configured" without keys)
PYTHONPATH=src python3 -m jobintel.cli.main daily-refresh --store /tmp/jobintel-demo

# What was checked automatically today, and what still needs a manual look
PYTHONPATH=src python3 -m jobintel.cli.main coverage-today --store /tmp/jobintel-demo

# Dashboard
PYTHONPATH=src python3 -m jobintel.cli.main dashboard --store /tmp/jobintel-demo
```

Building your own Evidence Bank and generating a CV:

```bash
PYTHONPATH=src python3 -m jobintel.cli.main profile-import ./my-project.md --source-type manual_project_description --title "My Project"
PYTHONPATH=src python3 -m jobintel.cli.main github-sync --username <your-github-username>
PYTHONPATH=src python3 -m jobintel.cli.main profile-set-contact --email you@example.com --phone "07700 900000"
PYTHONPATH=src python3 -m jobintel.cli.main profile-rebuild --name "Your Name" --graduation-year 2026
PYTHONPATH=src python3 -m jobintel.cli.main cv-generate --job-id <job-id> --print-cv
```

Optional environment variables: `ADZUNA_APP_ID` / `ADZUNA_APP_KEY`, `WTTJ_API_KEY`, `GITHUB_TOKEN`, `JOBINTEL_GITHUB_USERNAME`, `GEMINI_API_KEY`. Keep them in your shell or secret manager. `.env` files are git-ignored and never loaded automatically.

Run `PYTHONPATH=src python3 -m jobintel.cli.main --help` for all 40 subcommands.

## Testing

```bash
python3 -m pip install -e ".[dev]"      # pytest + pypdf (tests read rendered CV PDFs back)
PYTHONPATH=src python3 -m pytest tests/ -q
```

The public release is tested from a separate snapshot containing only publishable files, without `data/local/`, the private Evidence Bank adapter or candidate facts. Validation: **2248 passed, 38 skipped, 0 failed**. A focused check after the final public-facing profile and helper-path cleanup also passed: **69 passed, 2 skipped, 0 failed**. Skips include optional local integration helpers and unavailable optional integrations.

Private-data integration checks and explicitly opt-in live probes skip when their inputs are absent. Install the optional `browser-test` extra and a local Chrome to exercise the offline browser suites. CI installs the `dev` extra and may skip browser modules.

The default offline suite does not contact an ATS or Browserbase. Browser tests serve synthetic forms in-process to a local headless Chrome with every other hostname resolving to NOTFOUND. They cover the GraphQL read/write policy, autosave windows, non-fatal telemetry, the render wait, the CV-upload state machine (handle → storage → association → visible, plus its blocked and escalated paths), profile-link mapping, human handoff, guard release and teardown.

Coverage includes connector normalisation and pagination, Adzuna health and live-regression fixtures, Workday and Prospects parsing, snapshot/closure invariants, layered and cross-source dedup (including manual vs. automated), source precedence, graduation-year vs. intake-year handling, source and company coverage, company discovery, GitHub sync, promoted-profile rebuild protection, JD requirement extraction, project selection, claim grounding, CV structure / page budget / persistence, the review gate, application lifecycle invariants, dashboard pages and routes, and the optional intelligence layer (with a mocked provider).

## Privacy and local data

- `data/local/` (candidate profile, contact details, CV artifacts and PDFs, applications, raw snapshots, caches) and `data/backups/` are **git-ignored**. This repository contains no real candidate profile, CVs, contact details or application records.
- The raw notes behind the tailoring policy and its regression fixture describe real past applications, so they stay local (`data/policies/sources/`, `tests/fixtures/sources/`). Only the derived YAML policy and test cases are published.
- The curated Evidence Bank content for my own projects, and the enrichment tool that holds it, are local-only for the same reason.
- The candidate's own facts used to answer application questions (contact details, address, immigration status, education, employment and self-declared answers) live in a git-ignored module, `src/jobintel/submission/candidate_facts_local.py`. The public code fails closed without it, and tests that assert the real values are local-only.
- Per-job positioning overrides (`config/cv_positioning_overrides.json`), generated PDF/DOCX files and private Evidence Bank integration checks are excluded.
- Submission ledgers, approvals, sealed bundles, claim tokens and the isolated browser profiles (`data/runtime/`) are git-ignored. Browser debug URLs, cookies, headers, form values and request bodies are never written to logs or evidence.
- Screenshots use `jobintel.fixtures.sample_data` and a fictional test candidate ("Alex Example", `example.com` addresses, a placeholder phone number).
- Credentials are only read from environment variables. Adzuna credential parameters are stripped from stored redirect URLs, and the Gemini key is sent in a request header.

## Known limitations

- **No complete market coverage.** ATS connectors cover only employers in the registry (37 of 83 auto-verified). Prospects covers two categories. Nothing here claims to see the whole UK graduate market.
- **Manual sources stay manual.** Trackr, Gradcracker and Bright Network have no automated path; they depend on the user importing listings.
- **Adzuna** needs credentials, returns snippet descriptions, may link to Adzuna rather than the employer, rarely exposes deadlines, and is never used to close jobs.
- **Welcome to the Jungle** automation needs an API key and organisation references that the published registry doesn't configure, so it is effectively manual import today.
- **No automated final submission, by design.** Supervised form filling covers deterministic fields and the approved CV only. The human reviews the filled form, answers the escalated questions, submits, and records status changes. There are no email-driven status updates.
- **Supervised execution has been validated on one ATS vendor (Ashby) and one real form.** Each new host needs its own exact read, write and telemetry policy. Unknown widgets, cross-origin embedded forms and submit-type "Next" buttons stop the run rather than being guessed.
- **Generated CVs require human review.** Grounding and the claim validator lower the risk of overstated or invented content, but they can't make it impossible. JD skill recognition is also bounded by a curated keyword list.
- The **dashboard** is single-user and unauthenticated, intended only for `127.0.0.1`.
- **PostgreSQL** is a schema contract only; all persistence is local JSON/JSONL.
- The optional Gemini layer is advisory and has had limited live validation (see its doc).

## Status

Actively used for my own UK graduate job search (2026 graduation) and developed alongside it. This is a serious personal system and engineering portfolio project, **not production software**: single user, local store, no deployment. The supervised application path has reached human handoff on a real application form, but every final submission remains a manual action. Design notes for each subsystem are in `docs/`:

- [ARCHITECTURE](docs/ARCHITECTURE.md) · [SOURCES](docs/SOURCES.md) · [SOURCE_COVERAGE](docs/SOURCE_COVERAGE.md) · [GRADUATE_SOURCES](docs/GRADUATE_SOURCES.md) · [WTTJ_INTEGRATION](docs/WTTJ_INTEGRATION.md) · [COMPANY_REGISTRY](docs/COMPANY_REGISTRY.md)
- [JOB_MODEL](docs/JOB_MODEL.md) · [RANKING](docs/RANKING.md) · [CANDIDATE_MODEL](docs/CANDIDATE_MODEL.md) · [PROFILE_INGESTION](docs/PROFILE_INGESTION.md) · [GITHUB_SYNC](docs/GITHUB_SYNC.md)
- [CV_GENERATION](docs/CV_GENERATION.md) · [APPLICATION_TRACKER](docs/APPLICATION_TRACKER.md) · [GEMINI_INTELLIGENCE](docs/GEMINI_INTELLIGENCE.md)
- [SUBMISSION_MVP](docs/SUBMISSION_MVP.md) · [ARCHITECTURE_EVOLUTION](docs/ARCHITECTURE_EVOLUTION.md) · [AIRTAP_INTEGRATION](docs/AIRTAP_INTEGRATION.md)
- Phase reports: [PHASE3_WORKFLOW](docs/PHASE3_WORKFLOW.md) · [PHASE3_VALIDATION](docs/PHASE3_VALIDATION.md) · [LIVE_JOB_VALIDATION](docs/LIVE_JOB_VALIDATION.md) · [3.5B grounded CV](docs/PHASE_3_5B_GROUNDED_CV_REPORT.md) · [3.7 tailoring policy](docs/PHASE_3_7_CV_TAILORING_SKILL.md)
