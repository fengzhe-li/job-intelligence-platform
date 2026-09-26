# Phase 3.7: CV Tailoring Skill Integration + Historical Regression Harness

## 1. Executive Summary

Phase 3.7 integrates a reusable, authoritative **decision-policy layer** between the verified Evidence Bank and CV generation:
```
Evidence Bank ──► CV Tailoring Skill ──► JD-Specific Project/Bullet/Skill Decisions ──► Grounding Validator ──► Human Review Gate
```

Before Phase 3.7, project selection relied on naive keyword overlap with equal weights, leading to semantic anomalies (e.g. general web apps crowding out embedded systems in motorsport roles, or AI research agents ranking below CRUD tools for AI positions). Furthermore, no automated regression harness prevented semantic degradation or retroactive leakage of future evidence.

In Phase 3.7, we imported two exact, authoritative distillation assets, transformed them into versioned machine-readable artifacts with full cryptographic provenance, implemented the policy engine and policy-aware project selector, and authored an exhaustive regression test suite covering all 28 historical cases (`RT-01` through `RT-28`) and all 4 release-blocking pairs.

### Core Metrics Achieved
- **Authoritative Assets Ingested**: Exactly 2 markdown sources with SHA-256 cryptographic provenance.
- **Canonical Skill Policy**: `data/policies/cv_tailoring_skill_v1.yaml` (schema v1.0, transformation v1.0).
- **Regression Test Cases**: Exactly **28 cases** (`RT-01` to `RT-28`), 100% passing.
- **Release-Blocking Pairs**: Exactly **4 pairs**, 100% verified.
- **Production Isolation**: 100% isolated. Zero imports of fixtures and zero `RT-XX` IDs in `src/jobintel/**`.
- **Test Suite Status**: **606 passed, 1 skipped** (35 new tests added, zero regressions).
- **Grounding Invariant**: Zero evidence rules weakened. Fail-closed behavior strictly preserved.

---

## 2. Authoritative Source Provenance Report

The production policy YAML and the regression fixture YAML were deterministically transformed from the exact distillation sources:

| Asset Role | Authoritative Source File | Source SHA-256 | Transformed Artifact | Transformation Version |
| :--- | :--- | :--- | :--- | :---: |
| **Tailoring Skill** | `data/policies/sources/cv_tailoring_skill_v1_source.md` | `0d1136a9f2f532b6a9f4cb1ca3202d3e281c59fa8ec224e6e7ea06ee890167cf` | `data/policies/cv_tailoring_skill_v1.yaml` | `1.0` |
| **Regression Suite**| `tests/fixtures/sources/cv_tailoring_regression_v1_source.md` | `9b60579621d72948ccaec41d64087f411a3225da683d00ec5d30173d80a432b2` | `tests/fixtures/cv_tailoring_regression_v1.yaml` | `1.0` |

### Cryptographic Guarantees
- Both transformed YAML assets embed `source_asset`, `source_hash`, and `transformation_version`.
- Automated tests (`test_policy_asset_cryptographic_provenance` and `test_regression_fixture_cryptographic_provenance`) compute on-disk SHA-256 digests in real time and fail closed if any source byte is modified.

---

## 3. Lexical Precedence Order & Enforcement

The CV Tailoring Skill enforces a strict lexical precedence hierarchy:
$$\text{evidence\_truth} \succ \text{jd\_relevance} \succ \text{evidence\_strength} \succ \text{recruiter\_clarity} \succ \text{non\_redundancy} \succ \text{one\_page\_readability}$$

1. **`evidence_truth` (Primacy 1)**: Tailoring may only narrow or reframe verified evidence, never strengthen or invent it. If a project has zero verified evidence for a role, affinity cannot manufacture qualification (`skill_score <= 0.0 and role_track_bonus <= 0.0` drops the project).
2. **`jd_relevance` (Primacy 2)**: Direct evidence-backed requirement matches outrank indirect or generic capabilities.
3. **`evidence_strength` (Primacy 3)**: Deep, validated capabilities (e.g. CRIU live migration benchmarks, formal replay testing) outrank superficial or unverified mentions.
4. **`recruiter_clarity` (Primacy 4)**: The project portfolio must immediately convey target-role fit to a human recruiter (e.g., systems engineering for HPC, simulation for motorsport).
5. **`non_redundancy` (Primacy 5)**: Portfolio diversity is enforced *after* relevance. Redundant projects demonstrating identical capabilities receive a greedy discount (0.7x for purpose overlap, 0.5x for financial domain overlap).
6. **`one_page_readability` (Primacy 6)**: The 1-page ceiling is achieved by prioritising strong evidence, compressing low-value prose, and reducing bullet budgets (flagships 3-4, supporting 2-3), *never* by indiscriminately shrinking fonts.

---

## 4. Project Purpose Taxonomy

Every canonical candidate project is mapped to a functional engineering purpose:

| Canonical Project ID | Project Name | Primary Purpose (`ProjectPurpose`) | Core Engineering Mechanisms |
| :--- | :--- | :--- | :--- |
| `personal-source-session-migration` | Cross-Machine Session Migration | `SYSTEMS_INFRASTRUCTURE` | CRIU container migration, Linux networking, TCP socket continuity, gratuitous ARP |
| `personal-source-afra` | Auditable Financial Research Agent | `AI_AGENT_ARCHITECTURE` | Deterministic orchestrator, state machine, financial API research, failure classification |
| `personal-source-6` | Financial Knowledge Intelligence Platform | `DATA_INTELLIGENCE` | Hybrid retrieval, dense vector search (Qdrant), PostgreSQL, data ingestion pipeline |
| `personal-source-3` | MeetEat Social Dining Platform | `FULLSTACK_APPLICATION` | FastAPI REST API, React/TypeScript, PostgreSQL, Playwright E2E, accessibility, CI |
| `personal-source-iot-spill` | Edge-to-Cloud Smart Spill Detection | `IOT_CLOUD_EVENT` | AWS IoT Core, Lambda, DynamoDB compare-and-swap, MQTT telemetry, event-driven |
| `personal-source-stm32` | STM32 Embedded Fall Detection | `EMBEDDED_FIRMWARE` | Bare-metal C, STM32, I2C/SPI accelerometer driver, ISR state machine, low-power |
| `personal-source-indy500` | Indy 500 Point-in-Time Decision Support | `SIMULATION_MODELING` | Race strategy simulation, historical telemetry replay, immutable scientific assets |
| `personal-source-2` | DeepBreath Respiratory Sound Analysis | `AI_ML_RESEARCH` | Respiratory audio classification, PyTorch, patient-independent cross-validation |

---

## 5. Metric Semantic Preservation Rules

`validate_tailored_claim` in `src/jobintel/matching/tailoring_skill.py` preserves metric semantics and rejects mutations:
- **Latency is not downtime**: In Session Migration, client-observed peak request latency (e.g. 300ms) cannot be rewritten as "downtime" or "zero downtime" (`forbidden_rewrite:downtime`).
- **Measurement boundary**: Observed TCP four-tuple continuity cannot be inflated to "no second SYN", "zero packet loss", or "packet-level streaming" (`forbidden_claim:exceeds_measurement_boundary`).
- **Metric precision**: Session Migration Docker migration is precisely 8/10, native CRIU 5/5, and Telefork strict 5/5. Conflating validation modes or claiming 100% Docker migration is rejected.
- **Scope qualification**: Benchmark claims on synthetic subsets (e.g. 14 tasks, 126 executions) require the scope qualifier (`tested synthetic subset`) and cannot claim universal elimination of hallucinations in production.
- **Hardware validation boundaries**: Physical validation on STM32 hardware does not transfer to software-hardened builds without hardware re-validation.
- **Flattering metrics without controls**: The legacy ~94% accuracy in respiratory ML is rejected unless patient-independent cross-validation caveats are explicitly retained.

---

## 6. Time-Gated Evidence State Rules

To prevent retroactive evidence leakage in historical regressions:
- Evidence items and claims can be gated by `as_of_phase` or `time_gate`.
- When evaluating pre-Phase 6.5 states (`as_of_phase="pre_6.5"`), claims citing the 126 executions or 82% $\rightarrow$ 0% benchmark are rejected with `no_future_evidence_leakage`.
- When evaluating post-Phase 6.5 states (`as_of_phase="6.5"`), these claims are accepted provided their synthetic evaluation scope is declared.

---

## 7. The 4 Release-Blocking Pairs: Verification Results

| Release-Blocking Pair | Test Method | Evaluated Behavior | Outcome |
| :--- | :--- | :--- | :---: |
| **1. McLaren vs KPMG** | `test_release_blocking_pair_1_mclaren_vs_kpmg` | Same candidate portfolio evaluated on McLaren HPC vs KPMG Tax SWE. McLaren prioritizes Session Migration (#1) and Indy 500 in top 4; KPMG prioritizes Full-Stack/AFRA and strictly excludes Indy 500. Order materially reverses. | **PASSED** |
| **2. AFRA Pre vs Post Phase 6.5** | `test_release_blocking_pair_2_afra_pre_vs_post_phase_6_5` | Pre-Phase 6.5 rejects 126-execution benchmark claim (`no_future_evidence_leakage`); Post-Phase 6.5 accepts scoped benchmark claim but rejects universal production generalization. | **PASSED** |
| **3. Session TCP Observation vs Packet Claim** | `test_release_blocking_pair_3_session_tcp_observation_vs_packet_claim` | Observed TCP four-tuple continuity accepted; "no second SYN" / "packet-level continuity" rejected; rewriting latency to downtime rejected. | **PASSED** |
| **4. Red Bull PU vs Barclays/KPMG** | `test_release_blocking_pair_4_red_bull_pu_vs_barclays_kpmg` | Red Bull Powertrains includes STM32 and strictly excludes AFRA; Barclays includes AFRA and excludes STM32. Flagship project cleanly disappears when domain does not justify it. | **PASSED** |

---

## 8. Historical Regression Suite Summary (All 28 Cases)

All 28 historical cases in `tests/fixtures/cv_tailoring_regression_v1.yaml` are verified in `tests/test_cv_tailoring_harness.py`:

| Case ID | Context | Key Policy Assertion Tested | Status |
| :--- | :--- | :--- | :---: |
| **RT-01** | Morgan Stanley General SWE | General SWE relevance outranks motorsport novelty; Indy 500 / STM32 omitted | **PASSED** |
| **RT-02** | Morgan Stanley Space Allocation | Flagship projects receive 3-4 bullets; supporting receive 2-3 bullets | **PASSED** |
| **RT-03** | Morgan Stanley Internship | Factual data reconciliation; ownership verbs match actual scope | **PASSED** |
| **RT-04** | Barclays General Developer | Broad SWE portfolio: systems, AI agent, full-stack, cloud IoT; financial projects distinct | **PASSED** |
| **RT-05** | Barclays Skills | Weak/partial C++ is not prominently featured despite JD mention | **PASSED** |
| **RT-06** | McLaren HPC | Systems project (Session Migration) moves to #1; full-stack downranked | **PASSED** |
| **RT-07** | McLaren HPC Missing Skills | Slurm/HPC cluster administration is not evidenced and must remain missing | **PASSED** |
| **RT-08** | McLaren HPC Repositioning | AFRA repositioned to Python state machine orchestrator; finance wording dropped | **PASSED** |
| **RT-09** | McLaren HPC Fourth Slot | Indy 500 retained for scientific modeling/reproducibility over generic CRUD | **PASSED** |
| **RT-10** | McLaren Metric Precision | Docker 8/10, CRIU 5/5, Telefork 5/5 distinct; 100% claim rejected | **PASSED** |
| **RT-11** | Session TCP Measurement | Four-tuple continuity accepted; "no second SYN" / packet-level rejected | **PASSED** |
| **RT-12** | Session Latency vs Downtime | Rewriting latency measurement as "zero downtime" rejected | **PASSED** |
| **RT-13** | KPMG Tax SWE | Product/enterprise SWE reverses systems ordering; Indy 500 strictly excluded | **PASSED** |
| **RT-14** | KPMG Full-Stack Ranking | Full-stack web app (MeetEat) outranks deeper systems work for product role | **PASSED** |
| **RT-15** | KPMG Domain Separation | Indy 500 removed despite high test quality; no artificial novelty | **PASSED** |
| **RT-16** | KPMG Bullet Budgets | Unequal bullet allocation follows project importance (flagship $\ge$ supporting) | **PASSED** |
| **RT-17** | Auditable Pre-Phase 6.5 | Future benchmark evidence rejected under pre-6.5 time gate | **PASSED** |
| **RT-18** | Auditable Post-Phase 6.5 | 126-execution benchmark accepted with synthetic scope qualifier; production claim rejected | **PASSED** |
| **RT-19** | Auditable Technology Title | Antigravity CLI title verified; direct Gemini API title rejected | **PASSED** |
| **RT-20** | IoT Mechanisms vs Services | Event mechanisms beat AWS service enumeration; "exactly-once" rejected | **PASSED** |
| **RT-21** | Full-Stack Concrete CI | Concrete CI mechanisms (typecheck, unit test, Playwright) beat generic buzzwords | **PASSED** |
| **RT-22** | Red Bull Powertrains | STM32 restored; AFRA strictly excluded; missing controls skills not claimed | **PASSED** |
| **RT-23** | Motorsport Society Ownership | Contribution verbs permitted; vehicle design/powertrain ownership claims rejected | **PASSED** |
| **RT-24** | STM32 Versioned Evidence | Validation does not transfer from physical prototype to unvalidated software build | **PASSED** |
| **RT-25** | Respiratory ML Metrics | ~94% accuracy rejected without grouped patient-independent evaluation | **PASSED** |
| **RT-26** | Financial Project Separation | AFRA and FKIP remain distinct entities; no metric or entity leakage | **PASSED** |
| **RT-27** | One-Page Overflow Strategy | Redundancy removal and prose compression precede any layout/font adjustments | **PASSED** |
| **RT-28** | FDM General SWE | General graduate SWE does not over-tailor to motorsport | **PASSED** |

---

## 9. Controlled Dogfooding on the 4 Real Target Roles

Dogfooding evaluated the policy-aware selector against the pre-3.7 selector across the 4 real target jobs:

### Role 1: Graduate Software / Backend (ashby:0beef3e9-2ce0-4bba-81c2-caafc0a17457)
- **Title / Company**: Junior Software Engineer, transficc
- **JD Requirements**: `Java` (must-have)
- **Pre-3.7 Selection**: 0 projects
- **Phase 3.7 Selection**: **0 projects**
- **Analysis**: Candidate has Java coursework on CV but 0 project evidence in the Evidence Bank. `evidence_truth` is strictly primary: affinity cannot manufacture evidence from thin air. Zero artificial padding.
- **Review Gate**: `block_auto_submission` (*No evidence for must-have requirement: Java*).
- **PDF Layout**: Exactly 1 page (`fits_one_page: True`, `page_count: 1`).

### Role 2: AI / ML Engineering (adzuna:5855703872)
- **Title / Company**: Junior AI Backend Engineer, Transparency Technology
- **JD Requirements**: `Python`, `TypeScript`, `SQL`
- **Pre-3.7 Selection**: FKIP (#1), MeetEat (#2), Smart Spill (#3), Indy 500 (#4)
- **Phase 3.7 Selection**:
  1. `personal-source-6` (FKIP, score: 7.799, purpose: `data_intelligence`, bullet budget: 4)
  2. `personal-source-3` (MeetEat, score: 5.724, purpose: `fullstack_application`, bullet budget: 4)
  3. `personal-source-2` (DeepBreath, score: 4.500, purpose: `ai_ml_research`, bullet budget: 3)
  4. `personal-source-iot-spill` (Smart Spill, score: 4.424, purpose: `iot_cloud_event`, bullet budget: 3)
- **Analysis**: Indy 500 (motorsport simulation) was dropped. DeepBreath (respiratory ML research) was promoted into the top 3.
- **Review Gate**: `review_required` (*Missing contact details*).
- **PDF Layout**: Exactly 1 page (`fits_one_page: True`, `page_count: 1`).

### Role 3: Cloud / Platform / Systems (ashby:d0efa2af-b8d0-4feb-aa14-72351b264879)
- **Title / Company**: Junior SRE (Endpoint focus), transficc
- **JD Requirements**: `Linux`, `Ansible` (Track: `infrastructure_engineering`)
- **Pre-3.7 Selection**: Cross-Machine Session Migration (1 project)
- **Phase 3.7 Selection**: **Cross-Machine Session Migration** (1 project, score: 6.300, purpose: `systems_infrastructure`, bullet budget: 4)
- **Analysis**: Only Session Migration possessed verified Linux systems capability evidence. Unrelated projects without matching capability evidence were correctly excluded.
- **Review Gate**: `review_required` (*No evidence for required skill: Ansible*).
- **PDF Layout**: Exactly 1 page (`fits_one_page: True`, `page_count: 1`).

### Role 4: Motorsport Software / Simulation (adzuna:5892806489)
- **Title / Company**: Simulation Software Engineer, Langham Recruitment
- **JD Requirements**: `C`, `C++`, `CI/CD` (Track: `motorsport_engineering`)
- **Pre-3.7 Selection**: Indy 500 (#1), Session Migration (#2), AFRA (#3), MeetEat (#4)
- **Phase 3.7 Selection**:
  1. `personal-source-indy500` (Indy 500, score: 9.000, purpose: `simulation_modeling`, bullet budget: 4)
  2. `personal-source-session-migration` (Session Migration, score: 5.300, purpose: `systems_infrastructure`, bullet budget: 4)
  3. `personal-source-stm32` (STM32 Fall Detection, score: 5.000, purpose: `embedded_firmware`, bullet budget: 3)
  4. `personal-source-iot-spill` (Smart Spill, score: 4.300, purpose: `iot_cloud_event`, bullet budget: 3)
- **Analysis**: **Pre-3.7 anomalies completely resolved**. MeetEat and AFRA were eliminated. STM32 embedded firmware and Smart Spill IoT telemetry were elevated, creating an authentic motorsport/systems portfolio.
- **Review Gate**: `review_required` (*Missing contact details*).
- **PDF Layout**: Exactly 1 page (`fits_one_page: True`, `page_count: 1`).

---

## 10. Production Code Isolation Verification

- **Invariant**: Production code in `src/jobintel/**` must never import test fixtures or reference test case IDs.
- **Automated Check**: `test_production_code_never_imports_or_references_regression_fixtures` scans all python source files in `src/jobintel/**/*.py` for `cv_tailoring_regression` and regex `\bRT-\d{2}\b`.
- **Result**: Zero occurrences found. 100% clean isolation.

---

## 11. Verification & Test Suite Status

- **Phase 3.6 Baseline**: 571 passed, 1 skipped.
- **Phase 3.7 Tests Added**: 35 tests in `tests/test_cv_tailoring_harness.py`.
- **Final Test Suite Run**: `PYTHONPATH=src pytest tests/ -q` $\rightarrow$ **606 passed, 1 skipped in 5.23s**.
- **Working Tree Cleanliness**: `git diff --check` passed with 0 errors. No credentials printed, echoed, or committed.

---

## 12. Human Review & One-Page Invariants

1. **Human Review Gate**:
   - Automated CV tailoring drafts recommendations, but human review remains mandatory for final project order, rewritten claims, and submission.
   - `submission_without_review: false` is enforced in the policy and review gate.
2. **One-Page Constraint**:
   - The 1-page ceiling is verified against actual ReportLab rendered PDF page counts.
   - In all dogfooding runs, `page_count == 1` and `fits_one_page == True`.
   - Overflow strategy removes redundancy and compresses weak wording before applying modest layout adjustments.
3. **Grounding Integrity**:
   - Zero grounding rules were weakened.
   - Fail-closed deterministic fallback remains fully active if policy execution encounters missing data or unexpected conditions.

---

## 13. Remaining Limitations & Recommendation for Phase 3.8

### Remaining Limitations
1. **Interactive Review UI**: While the review gate flags gaps and contact omissions, the human review interaction currently happens via CLI/artifacts rather than an interactive approval screen with side-by-side claim diffing.
2. **Dynamic Bullet Allocation Fine-Tuning**: While bullet budgets (3-4 for flagships, 2-3 for supporting) are recommended by the policy selector, text wrapping on specific long technical quotes occasionally requires ReportLab modest margin compression to guarantee exactly 1 page.

### Recommendation for Phase 3.8
Phase 3.7 has successfully established the decision-policy layer and verified historical regression invariants.
For **Phase 3.8**, we recommend:
1. **Interactive CV Review & Approval Workbench**: Implement the web dashboard review workflow allowing the user to review the policy-tailored project selections, toggle candidate bullets, inspect claim-level grounding provenance, and approve the generated 1-page PDF before application packaging.
2. **Application Package Assembly**: Bundle the approved CV PDF, role-specific metadata, and contact details into the application tracking system without starting automated submission.
