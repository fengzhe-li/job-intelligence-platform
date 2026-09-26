# Phase 3.5B: Grounded Project Selection & CV Tailoring — Canonical Reconciliation Report

---

## 1. Repository & Commit State

- **Branch:** `main`
- **HEAD Commit SHA:** `18625e9520169b770ced9723507a7006be7c0724`
- **Working Tree Status:** All Phase 3, Phase 3.5A, and Phase 3.5B changes remain strictly in the local working tree (uncommitted). No commits or pushes have been made.
- **Credential Hygiene:** Zero API keys or secrets are stored in code, data, logs, cache, or git history. Credentials are read exclusively from environment variables in memory.

---

## 2. Test Count Reconciliation

- **Current Full Test Suite:** **`554 passed, 1 skipped in 5.12s`** (Exit Code: 0)
- **Phase 3.5B Test Count:** **`21 passed in 0.09s`** in `tests/test_phase35b_grounded_cv.py`

### Explanation of Discrepancy Between Prior Reports
1. **Pre-Phase 3.5B Baseline:** `533 passed, 1 skipped` (established at completion of Phase 3.5A).
2. **Initial Phase 3.5B Report (`550 passed, 1 skipped` / 17 tests):**
   - 17 unit tests were initially written in `tests/test_phase35b_grounded_cv.py` to cover basic hybrid project selection, claim validation, fallback modes, and ReportLab PDF layout.
   - `533 + 17 = 550 passed, 1 skipped`.
3. **Canonical Reconciled Report (`554 passed, 1 skipped` / 21 tests):**
   - When the user requested two additional explicit constraints (validating semantic factual entailment against the counterexample `"Architected a scalable high-performance backend serving thousands of requests"`, and subordinating project diversity to JD relevance), exactly 4 new adversarial tests were added to `tests/test_phase35b_grounded_cv.py`:
     1. `test_bullet_claim_semantic_factual_entailment_counterexample`: Verifies rejection of unsupported ownership, performance, and scale claims.
     2. `test_bullet_claim_unsupported_qualifiers_and_outcomes_rejected`: Verifies rejection of enterprise scope inflation (`enterprise-grade`, `fault-tolerant`) and business outcomes (`reduced costs by 30%`).
     3. `test_project_selection_materially_stronger_jd_evidence_never_loses_to_diversity`: Verifies that a project matching multiple required skills (score 4.5) never loses to a weaker project (score 1.0) merely for diversity.
     4. `test_project_selection_drops_zero_score_projects`: Verifies that unrelated projects (score 0.0) are never artificially padded.
   - `550 + 4 = 554 passed, 1 skipped` (21 Phase 3.5B tests).
   - No tests were removed or duplicated.

---

## 3. Implementation Truth

### A. Project Selection (`src/jobintel/matching/project_selection.py`)
- **Zero-Relevance Projects:** Filtered out unconditionally (`relevant_matches = [m for m in initial_matches if m.score > 0]`). If a project matches no JD requirements and has zero role-track relevance, it receives a score of `0.0` and is discarded. Zero-relevance projects are never selected or artificially padded.
- **Relevance vs. Diversity:** JD evidence relevance is strictly primary. Diversity is evaluated via incremental requirement coverage.
- **Redundancy Discount:** Matched requirements already fulfilled by an earlier selected project receive a **$0.4\times$ redundancy discount**, while uncovered requirements receive full $1.0\times$ weight.
- **Materially Stronger Evidence:** A project matching multiple required JD skills (e.g. base score 3.0) will, even after the $0.4\times$ discount ($3.0 \times 0.4 = 1.2$), strictly beat an unrelated or weak single-preferred-skill project (score 1.0). Diversity acts as a tie-breaker between comparable projects, never overturning materially stronger JD evidence.
- **Invalid Project / Evidence IDs:** If Gemini returns a semantic match referencing a project ID not in `candidate.projects`, or an evidence ID belonging to Project B but claimed for Project A, the hybrid match fails closed immediately to deterministic scoring.
- **Deterministic Fallback:** Operates automatically when Gemini is unavailable, when quota is exhausted, or when inputs are unassisted.

### B. Bullet Grounding (`src/jobintel/matching/claim_grounding.py`)
- **Nature of Validator:** The validator is a **deterministic claim-level grounding validator with explicit rules for unsupported technologies, metrics, ownership, scope, qualifiers, outcomes, and cross-project evidence**. It is not a general open-ended NLI semantic entailment engine; it enforces strict factual boundaries using curated lexicons, token matching, entity taxonomies, and regex patterns against the candidate's verified evidence.
- **Exact Deterministic Checks:**
  1. *Technologies:* Every mentioned tech must exist in the project evidence, project description, or project capabilities. Subsumption traps are explicitly blocked (Docker cannot become Kubernetes, AWS cannot become Azure/GCP, Python cannot become Java/C++/Go/Rust).
  2. *Numbers & Metrics:* Standing metrics (`%`, `x`, `users`, `rps`, `ms`, `seconds`, `mb`, `gb`) and standalone multi-digit numbers must exist verbatim in the source evidence.
  3. *Ownership & Seniority:* Verbs such as `architected`, `led`, `spearheaded`, `managed`, `founded`, `supervised`, `directed`, `principal`, `senior`, `sole developer` are rejected unless explicitly supported by evidence.
  4. *Performance Qualifiers:* Claims such as `high-performance`, `ultra-fast`, `low-latency`, `high-throughput`, `zero-downtime`, `fault-tolerant`, `highly available`, `sub-second`, `optimized for speed` are rejected unless explicitly grounded in evidence.
  5. *Scale & Traffic:* Claims such as `scalable`, `scalability`, `elastic`, `thousands of`, `millions of`, `heavy traffic`, `high traffic`, `high concurrency`, `concurrent users`, `at scale`, `production scale` are rejected unless explicitly grounded in evidence.
  6. *Business Outcomes:* Claims such as `reduced cost`, `saved $`, `increased revenue`, `boosted conversion`, `slashed`, `doubled`, `tripled`, `eliminated bottlenecks` are rejected unless supported by evidence.
  7. *Architectural Scope:* Claims such as `enterprise-grade`, `production-grade`, `mission-critical`, `distributed system`, `event-driven architecture`, `microservices architecture` are rejected unless supported by evidence.
  8. *Domain Scope / Model Training:* RAG or retrieval pipelines cannot claim `trained`, `fine-tuned`, `pre-trained`, or `model weights`.
  9. *Cross-Project Isolation:* Bullets citing evidence belonging to another project are rejected (`cross_project_evidence_citation`).
  10. *Speculative Evidence:* Phrases marked as `planned`, `todo`, or `learning` cannot be presented as completed achievements.

### C. Skills Section (`src/jobintel/matching/cv_generation.py`)
- **Candidate-Only Semantic Ranking:** Enforced in `_select_skills`. Only capabilities present in `candidate.capabilities` with an evidence tier of `strong` or `partial` can appear.
- Gemini can never introduce new skills. Semantic matches elevate candidate capability priority without importing unsupported JD requirements.

### D. Fallback Behavior
- **Gemini Unavailable / Quota Exhaustion:** Catches provider errors cleanly and executes deterministic project selection and extractive quote composition (`generation_mode = "deterministic"`).
- **Invalid JSON / Schema:** Schema validation drops malformed responses and falls back to deterministic extractive mode.
- **Individual Bullet Rejection:** Operates per-bullet. Rejected suggestions are logged in `rejected_bullet_suggestions` and replaced with deterministic extractive bullets (`generation_mode = "gemini_with_fallback"`).
- **PDF Overflow Fitting:** `_fit_to_pdf_budget` trims weakest bullets, drops lowest-scoring projects (down to floor 2), compresses text lengths (110 $\rightarrow$ 80 $\rightarrow$ 60 chars), and applies a single modest font/margin adjustment. ReportLab guarantees an exact 1-page PDF.

---

## 4. Live Validation Reconciliation

Tested across 4 real roles from the local database:

| Role Category | Job ID | Title | Company | Selected Projects | Mode | Page Count |
| :--- | :--- | :--- | :--- | :--- | :--- | :---: |
| **Graduate Software / Backend** | `ashby:0beef3e9-2ce0-4bba-81c2-caafc0a17457` | Junior Software Engineer | transficc | `[]` (0 projects, Java gap) | Deterministic | 1 |
| **AI/ML Engineering** | `adzuna:5855703872` | Junior AI Backend Engineer | Transparency Technology | `['personal-source-3', 'personal-source-6', 'personal-source-2', 'personal-source-7']` | Deterministic (Quota) | 1 |
| **Cloud / Platform / Systems** | `ashby:d0efa2af-b8d0-4feb-aa14-72351b264879` | Junior SRE (Endpoint focus) | transficc | `['personal-source-2']` (1 project, Linux) | Deterministic | 1 |
| **Motorsport Software / Tech** | `adzuna:5892806489` | Simulation Software Engineer | Langham Recruitment | `[]` (0 projects, C/C++ gap) | Deterministic | 1 |

### Notes on Live Execution:
- **Model:** `gemini-2.5-flash` via official Google Generative AI API protocol.
- **Fail-Closed Verification on Quota Exhaustion:** When API quota was exhausted, HTTP 429 was captured cleanly without leaking secrets, triggering `deterministic_fallback` with zero crashes or data loss.
- **Cache Hit Verification:** Disk cache lookup (`data/local/intelligence_cache/<sha256>.json`) operated with zero network I/O and zero cost.
- **One-Page PDF Verification:** 100% of generated PDFs rendered as exactly 1-page documents verified via ReportLab Flowable layout.
- **Zero Credential Leakage:** Audited across disk cache files, generated PDFs, JSON records, and terminal output.

---

## 5. Evidence Density Limitation

- **Explicit Principle:** Sparse project READMEs or evidence sources limit tailoring quality.
- The system **must not invent around missing evidence**. If a candidate project only contains two sentences of technical details in its README, the system will only generate bullets substantiated by those two sentences.
- Expanding candidate evidence density requires the candidate to add technical documentation to `data/local/profile/sources.json` or sync repositories via GitHub integration; the AI layer will never fabricate missing details.

---

## 6. Safety & Dogfooding Recommendation

### **Recommendation: Ready for controlled real-application dogfooding with mandatory human review.**

### Critical Safety Clarifications:
1. **Adversarial Tests Cover Known Failure Classes:** The test suite verifies protection against known failure modes (tech substitution, fabricated metrics, scope inflation, cross-project leakage, ungrounded leadership claims).
2. **Rule-Based Grounding Validator Scope:** The deterministic claim validator checks explicit rules, entity sets, and pattern matchers. It does **not** constitute an open-ended mathematical proof against every conceivable natural language nuance or subtle distortion.
3. **Mandatory Human Review Gate:** Automated CV submission is strictly blocked. Every generated CV artifact records its full generation mode, selected projects, and rejected suggestions for human inspection before application.
