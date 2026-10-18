# Human-approved submission MVP

A local submission boundary after the existing discovery, ranking, eligibility, JD
and CV pipeline. It started as a manual handoff only (the sections below), then gained
stop-only executors: an offline AirTap adapter, and a supervised browser runner with two
transports, Browserbase and local headed Chrome (`local_browser`, the primary path). No
executor ever performs the final submission. The browser executors and the approved CV
upload are described at the end of this document; the reasoning behind them is in
[ARCHITECTURE_EVOLUTION.md](ARCHITECTURE_EVOLUTION.md).

## State and ownership

```text
human reviews exact CV and package hash
  -> approved_for_submission
  -> submission_requested
  -> executing (exclusive claim; separate manual handoff)
  -> pending_verification (reported execution result only)
  -> verified (durable, human-verified receipt)
  -> submitted (reconcile updates ApplicationStore to applied)
```

Alternative request states: `needs_human_input`, `outcome_unknown`, `failed`, and
`cancelled`. No background worker, automatic retry or automatic claim recycling
exists. An expired claim or interrupted executor becomes `outcome_unknown`.
Late evidence can be recorded for the **same attempt** without invoking the
executor again. Unknown outcomes cannot be downgraded to retryable failures.

Approval states are `approved`, `revoked`, and `expired`. Revocation and expiry
always append new events; the original approval is retained. Checks run at queue,
claim, execution and verification boundaries. `submission-list` and
`submission-reconcile` also record detected expiry or invalidation. Expiry is
therefore evaluated on access, not by a background scheduler.

The existing ApplicationStatus enum is unchanged. Queuing, claiming, completing,
and verifying do not write `applied_at`. Only reconciliation of a verified receipt
moves the application to `applied`. Existing explicit manual application recording
continues to work when there is no active submission request.

## Module boundaries

| Module | Responsibility |
|---|---|
| `models/submission.py` | Frozen package, answer, execution-result and evidence contracts; hashes and states |
| `storage/locking.py` | Reentrant thread/process lock shared by application and submission writes |
| `storage/submission_store.py` | Append-only journals, integrity checks, approval and queue projections |
| `submission/packages.py` | Re-read authoritative inputs, recompute gate, fingerprint and seal PDF |
| `submission/answers.py` | Explicit reviewed answer versions and trusted profile field snapshots |
| `submission/executors.py` | Store-free protocol and ManualSubmissionExecutor |
| `submission/receipts.py` | Extensible evidence strategy interface and conservative MVP strategies |
| `submission/service.py` | Approval, queue, claim, handoff, completion, verification and recovery |
| `submission/cli.py` | Independent CLI commands; registered by the existing CLI |
| `submission/stop_only.py` | Shared stop-only contract: field vocabulary, escalation rules, page identity, result import |
| `submission/authoritative_answers.py`, `candidate_facts.py`, `question_resolution.py`, `handoff.py` | Answer Bank (candidate facts from the git-ignored local module), deterministic question rules, package answer snapshots |
| `submission/browserbase.py`, `browserbase_runner.py`, `graphql_policy.py` | Sealed bundle, the stop-only runner and its network guard, GraphQL operation inspection |
| `submission/browser_session.py`, `local_browser.py`, `browserbase_cloud.py`, `browserbase_service.py` | Transports (local headed Chrome, Browserbase) and the service-side executor |

ApplicationStore keeps its original public method set. The submission service uses
an internal receipt-reconciliation method that reads a persisted verified receipt,
not an arbitrary executor dictionary. Optional receipt/event IDs are backward
compatible with existing application and history records.

## Immutable approval package

A package binds the application/job, explicit HTTPS application URL, exact CV
artifact ID, PDF SHA-256, answer values and versions, gate decision and reasons,
and a fingerprint of the job/profile/artifact/configuration/gate inputs. The gate
implementation itself is also fingerprinted. Existing artifact IDs are the CV
version identity; no second integer-version scheme is introduced.

`submission-preview` returns the package and its hash for review. Approval requires
that exact hash, an explicit `--reviewed` flag and reviewer identity. `AUTO_PREPARE`
is not human approval. `REVIEW_REQUIRED` additionally requires acknowledgement
of the reasons. `BLOCK_AUTO_SUBMISSION` is rejected before approval or queuing
(the error lists the blocking review reasons) unless a supervised exception applies.

### Supervised exception (`SUPERVISED_EXECUTION_ONLY`)

A `BLOCK_AUTO_SUBMISSION` package may be prepared, approved and queued only with a
structured `SupervisedReviewAcknowledgement` (`--supervised-ack-file` on
`submission-preview` / `submission-approve`): reviewer, `reviewed: true`, timezone-aware
timestamp, reason, the exact current review reasons being acknowledged, the bound
application/job/CV artifact/PDF SHA-256, `execution_mode: SUPERVISED_EXECUTION_ONLY`
and `final_submit_allowed: false` (any other value is rejected). It is accepted only
when eligibility is `ELIGIBLE` and the application policy is `auto_application_allowed`
— never for `HARD_INELIGIBLE`, eligibility-review or protected Graduate/New Grad/Early
Career roles. The approving reviewer must be the acknowledging reviewer.

The acknowledgement is part of the package hash. It does not change `gate_status`
or `gate_reasons` (both stay as reviewed), does not mark any requirement satisfied,
and does not touch the CV artifact. Approval and request records carry
`execution_mode`; the AirTap bundle carries `authorization`, the unchanged
`review_status`/`review_reasons` and the acknowledgement, while `execution_mode`
stays `STOP_BEFORE_FINAL_SUBMIT` and `final_submit_allowed` stays `false`. Any change
to the CV, PDF, review reasons or bound identities invalidates the approval.

The PDF is verified against its artifact hash and copied once to a content-addressed,
read-only package path. The executor receives this sealed PDF path, not a mutable
CV draft path. Paths are resolved in the selected local store, rather than trusting
old absolute paths copied from another project directory.

Any changed CV ID, source/sealed PDF bytes, application URL, answer snapshot or
version, package hash, or fingerprinted review input invalidates the old approval.
Changing an attached CV also proactively records revocation before a queue exists.
With an active request, legacy application writes and CV replacement are blocked;
they must first be resolved through the submission workflow.

## Local persistence

Under `--store` (default `data/local`):

```text
.application-submission.lock
submissions/
  events.jsonl                    # approvals, revocations, expiry, queue, claims,
                                  # results, answers and reconciliation events
  receipts.jsonl                  # verified receipts only; append-only
  packages/<pdf-sha256>/cv.pdf     # sealed, read-only PDF
  evidence/                       # operator-provided PNG/JPEG receipt evidence
```

The queue and latest answer versions are projections of the event journal. There
is deliberately no independently mutable queue.json or answers.json to drift from
history. Both journals use stable event identities and a hash chain. Writes are
serialized with a shared POSIX flock/thread lock and flushed with fsync. This
implementation targets the existing local Unix runtime, not distributed workers.

The default runtime paths remain covered by the existing `data/local/` ignore rule.
No remote upload, additional dependency, database migration or Git commit is part
of this MVP.

## CLI: one responsibility per command

All commands use the existing entry point:

```sh
PYTHONPATH=src python3 -m jobintel.cli.main <command> --store data/local ...
```

| Command | Required operation-specific options | Effect |
|---|---|---|
| `submission-preview` | `--application-id` | Inspect package/hash; no approval |
| `submission-approve` | `--application-id --package-hash --reviewer --reviewed` | Record approval and seal PDF; no queue |
| `submission-revoke` | `--approval-id --reviewer --reason` | Append revocation; cancel unstarted request or hold in-flight outcome as unknown |
| `submission-enqueue` | `--approval-id` | Queue approved package; no execution |
| `submission-list` | none | Inspect state; record detected expiry/invalidation |
| `submission-claim` | `--request-id --worker` | Exclusive bounded claim; no executor invocation |
| `submission-execute` | `--request-id --claim-token` | Print manual handoff and approved materials; no browser/submission |
| `submission-complete` | `--request-id --claim-token --result-file` | Record execution result; no verification/application update |
| `submission-verify` | `--request-id --reviewer --confirmed` | Persist verified receipt, or mark insufficient evidence unknown |
| `submission-reconcile` | none | Replay verified receipts into application/history and repair interrupted invalidation |
| `submission-answer-set` | `--field --value --reviewer --provenance` | Append a reviewed answer; optional `--job-id` narrows scope |

Approval accepts `--acknowledge-review-reasons` and `--expires-in` (seconds; default
86400, maximum seven days). Claim accepts `--lease-seconds` (default 900). Commands
also accept `--config`; the default is `config/personal_strategy.json`.

A completion file describes what the human actually observed, not a desired outcome:

```json
{
  "request_id": "request:<from-enqueue>",
  "package_hash": "<approved-package-hash>",
  "job_id": "<actual-job-id>",
  "cv_artifact_id": "<approved-artifact-id>",
  "application_url": "https://example.com/jobs/actual-job",
  "executor": "manual",
  "outcome": "reported_submitted",
  "submitted_at": "<actual-ISO-timestamp-with-timezone>",
  "evidence": [
    {
      "kind": "confirmation_text",
      "value": "Your application has been submitted successfully."
    }
  ],
  "unanswered_fields": []
}
```

Use the real identities, timestamp and observed evidence; the example is not a
receipt. `submitted_at` must fall within the attempt. Other result outcomes are
`needs_human_input`, `outcome_unknown` and `failed`. Reported unanswered questions
force the request into `needs_human_input` even if the executor claims success.

## Receipt evidence rules

There is **no mandatory external confirmation ID**. Strategies support:

- `confirmation_id`: an actual external reference, independently checked by the human.
- `confirmation_page`: HTTPS page URL on the approved application host, plus `detail`
  containing observed affirmative confirmation text. A URL alone is insufficient.
- `confirmation_text`: observed affirmative confirmation, not simply “done”.
- `screenshot_evidence`: relative PNG/JPEG path under `submissions/evidence/` plus its
  SHA-256. Path containment, image header and file hash are checked.
- `application_portal_status`: same-host HTTPS URL and affirmative observed status
  in `detail`.
- `manual_verified`: an explicit substantive note from the human who independently
  confirmed submission.

MVP verification always requires a named human and `--confirmed`, including when
a custom evidence strategy is injected by tests. Text/page strategies use a
conservative English recognizer; other languages or redirect hosts require manual
or screenshot evidence, or a future ATS-specific strategy. This is a local
single-user attestation model, not authenticated remote-user identity or an
independent browser observer. Hash chains detect corruption, not a malicious user
with full access to rewrite the store and code.

A verified receipt records the request/approval/application/job IDs, package and PDF
hashes, exact CV artifact, URL, executor, actual submission time, evidence and human
verification provenance. An executor's boolean or “done” message alone never
updates the application. Unsupported or insufficient evidence yields
`outcome_unknown` and no verified receipt. Verification does not reconcile implicitly.

## Answer bank rules

Only these repeatable fields are allowed: name, email, phone, location, LinkedIn URL
and GitHub URL. Values come from the existing trusted profile or an explicit human-
reviewed bank entry with provenance, scope and version. A job-specific answer wins
over a global answer; unrelated job answers are excluded. Updates append versions,
and relevant answer/version changes invalidate approvals.

Sponsorship, right to work, visa status, nationality, security clearance, criminal
or compliance declarations, disability/adjustments, salary expectation, notice
period, relocation, conflicts of interest, cover letters, motivation and unknown
free-text questions are not generated or inferred and cannot be inserted through
this repeated-field API. They require per-application human handling.

## Crash recovery and invariants

The successful write order is:

```text
verified receipt (fsync)
  -> application submission history event (stable receipt-derived event ID, fsync)
  -> atomic application snapshot update
  -> reconciled event
```

`submission-reconcile` can be rerun after an interruption at any boundary. It does
not create a second submission event or application and does not regress a later
interview/offer status. Receipt/CV/application conflicts are reported for review.
A crash between approval revocation and queue cancellation is also repaired by
reconciliation, without rewriting the approval event.

Execution intent is persisted before invoking the manual executor. An exception or
expired claim becomes unknown; it is never handed to another worker for automatic
retry. Claim tokens bind completion to the original attempt. Result identity must
match the approved request, job, URL, CV and package hash.

A physically truncated/corrupt journal fails closed and is not silently truncated
or repaired by deleting audit data. Such filesystem damage needs manual forensic
recovery. An approval revoked/expired while an external outcome is unresolved also
stays blocked; this MVP has no bypass that approves a new attempt over that unknown
outcome. Late verification requires the approval and its inputs still to be valid.
These holds favor avoiding duplicate submissions over unattended recovery.

## Validation

Original local baseline: **645 passed, 1 skipped**. Added **94 tests** across:

- `test_submission_store.py`: hash/replay integrity, append-only approval changes,
  nested/thread/process locks and corrupt journals;
- `test_submission_service.py`: package input invalidation, human/gate requirements,
  claim exclusivity, executor boundaries, answer restrictions, evidence strategies,
  identity checks, insufficient evidence and no retry;
- `test_submission_recovery.py`: legacy-path protection, sealed PDFs, application
  uniqueness, receipt replay, crash boundaries, revocation recovery and no regression;
- `test_submission_cli.py`: complete separate-command workflow and invalid inputs.

Final full local result: **739 passed, 1 skipped, 0 failed** (740 collected).
Existing tests were not edited. One intermediate compatibility failure from adding
a public ApplicationStore method was fixed by retaining its public API and using
an internal receipt entry point.

```sh
PYTHONDONTWRITEBYTECODE=1 RUN_LIVE_SOURCE_TESTS=0 \
PYTHONPYCACHEPREFIX="$PWD/.validation-pycache" PYTHONPATH=src \
python3 -m pytest -q -p no:cacheprovider
```

No actual application was submitted during validation. This phase ends at the
manual/test executor MVP. Phase 2 adds an offline, stop-only AirTap handoff adapter
(supervised execution only, manual human CV upload); see [AIRTAP_INTEGRATION.md](AIRTAP_INTEGRATION.md).

## Browserbase stop-only executor (experimental)

`browserbase` is the second stop-only executor (`STOP_ONLY_EXECUTORS = {"airtap",
"browserbase"}`). It uses the same approval ledger, one-request-per-approval rule,
claim tokens/leases, stale-package revalidation and recovery as AirTap; generic
`submission-complete` is refused for it.

Lifecycle: approved package → `submission-enqueue --executor browserbase` →
`submission-claim` → `submission-execute`. Execute builds the executor itself from the
live approval (callers cannot inject one), refuses before journaling anything if the
target host or credentials are not acceptable, journals `handoff_started`, writes the
sealed Browserbase bundle (`submissions/browserbase/`), journals the session id as soon
as a session exists, runs the stop-only browser runner once, and imports the result with
the shared strict importer (`stop_only.import_result`, executor `browserbase`). The
request ends `ready_for_human_review`, `needs_human_input` or `outcome_unknown` —
never submitted, never a receipt, never APPLIED. Any interruption before a trusted import
leaves the attempt `outcome_unknown` (an expired lease is marked so by `submission-list`);
it is never retried automatically.

What it can and cannot do:

- It fills only exact, sealed, approved answers into recognised fields; sensitive,
  unknown, ambiguous, duplicate-labelled, invalid or unanswered required questions
  escalate to `needs_human_input`. It never generates answers.
- It can select the exact sealed CV after a local SHA-256 check; `cv_upload_confirmed`
  is true only when the native file input shows exactly that file. No remote integrity
  is claimed.
- It can click only a single exact, provably non-submitting "Next"/"Continue". It can
  never final-submit: no option, flag or code path exists, and every submission path is
  blocked in-page and on the network.
- Credentials (`BROWSERBASE_API_KEY`, optional `BROWSERBASE_PROJECT_ID`) are read from
  the runtime environment only and never stored in packages, bundles, approvals,
  requests, manifests, evidence or logs.
- By default only reserved synthetic hosts (`.test`, `example.com`, …) may be run. A real
  host needs the single-request live-host mode below; there is no global switch.

Current limitations: submit-type "Next" buttons stop the run; cross-origin embedded
forms stop the run; vendor-specific custom widgets (non-ARIA comboboxes, custom date
pickers, drag-and-drop uploads) are unsupported; the network-write policy for real ATS
pages (telemetry, autosave, third-party beacons) is not production-hardened. This is
not production-ready.

### Browserbase human handoff (opt-in, experimental)

`submission-execute --human-handoff [--review-minutes N]` (Browserbase only) creates the
session with `keepAlive` and a timeout of run budget + review window (1–355 minutes;
Browserbase's hard maximum session lifetime is 6 hours and a running session's timeout is
not extended). If the imported result is reviewable (`ready_for_human_review` or
`needs_human_input`) the SAME filled session is left running and a
`browserbase_handoff_started` event binds it to the request, application, approval,
package hash and session id. Any other outcome — including an import failure — releases
the session immediately. Without `--human-handoff` sessions are released as before.

- Live View: the official link (`GET /v1/sessions/{id}/debug`, `debuggerFullscreenUrl`,
  time-limited with `expiresIn`) is printed at runtime only and never journaled — it may
  act as a bearer link to watch and control the browser. `submission-handoff-status
  --request-id` shows the ledger state, the provider status and, while the session is
  running and the review window is open, a fresh link.
- Release: `submission-handoff-release --request-id --session-id --claim-token` ends the
  session. It must name the session journaled for that request, is idempotent, journals
  `browserbase_session_released` (or `browserbase_release_failed`, retryable), and never
  changes the recorded result.
- Lease: the request's result is recorded before the handoff starts, so the claim lease
  is no longer involved; its expiry neither retries nor starts another session.
- No automated submit is added. On Browserbase the in-page submit guard installed by the
  runner stays active in the handed-off page, so a Submit attempt in that page is blocked;
  behaviour after a page reload by the reviewer is unverified. (The local headed-Chrome
  transport instead releases the guard at handoff; see below.) Whatever the human does is not
  observed: nothing is marked submitted, receipted or APPLIED.
- Recording/privacy (undecided for real candidate data): Browserbase records sessions and
  logs CDP events by default (`browserSettings.recordSession` / `logSession`), retained up
  to 31 days (24 h on BYOS); the Live View link exposes the live page to whoever holds it.
  Before any live ATS use decide: recording on/off, log retention/BYOS, who may receive a
  Live View link and for how long.

### Single-request live-host mode (Browserbase and local headed Chrome)

One request, one exact host, always supervised:

```
submission-enqueue --approval-id A --executor browserbase --live-host jobs.employer.example
submission-claim   --request-id R --worker W
submission-execute --request-id R --claim-token T --human-handoff \
                   --allow-live-host --live-host jobs.employer.example \
                   [--live-write-allow POST https://jobs.employer.example/exact/autosave]
```

- Binding: the host must be byte-for-byte the host of the approved application URL
  (https, no port, no credentials, no IP literal, no wildcard, no sub/parent/sibling domain,
  no case or trailing-dot variant). It is recorded on that request at enqueue and must be
  restated at execute. The service then builds a runtime-only `LiveHostAuthorization`
  bound to request, application, approval, package hash and host; it expires with the
  review window (never after the approval) and is never stored as configuration.
  `BrowserbaseExecutionConfig` has no live-host switch.
- Refused before any session: missing `--allow-live-host`/`--live-host`, missing
  `--human-handoff`, host mismatch, invalid write allowlist, missing credentials.
- Privacy: the session is created with `recordSession: false` and `logSession: false`
  (documented create-session `browserSettings`; Browserbase documents that disabling both
  means no data is recorded). Not configurable; the session body is refused if either
  would be true. No screenshot is taken, there is no replay URL, and URLs after the
  verified start page are kept without query/fragment. Browserbase's `allowedDomains`
  is set to the one host but also admits subdomains, so the runner enforces the exact host.
- Writes: GET/HEAD allowed; every other method is blocked unless it is one of at most 5
  exact same-host, non-final POST/PUT/PATCH URLs given for this run only (no wildcards,
  no DELETE, not reusable). A blocked write stops the run at the next step
  (`unexpected_write_blocked`) and keeps only method, host, path and resource type.
- Host drift: a main-frame navigation off the exact host is aborted
  (`host_drift_blocked`). A server-side HTTP redirect cannot be intercepted (Playwright
  routes only the first URL of a redirect chain), so the browser follows it; the
  main-frame host is re-checked before every step and the run stops (`host_drift`) without
  filling. Both end as `aborted_wrong_job`, which releases the session (no handoff).
- Audit: `browserbase_live_host_authorized` (request/application/approval/package hash,
  host, expiry, handoff required, recording/logging off, screenshots not captured, write
  allowlist count, `final_submit_allowed: false`, `automatic_retry: false`);
  `browserbase_evidence.live` (privacy mode, stop reason, minimal blocked-request
  diagnostics); `browserbase_handoff_started.live_host`. Never journaled: API key,
  Live View URL, cookies, request bodies, page snapshots.
- No retry and no final submit, exactly as for every Browserbase request.

## Local headed-Chrome executor (`local_browser`, primary path)

The same sealed bundle, runner, live-host binding and ledger as Browserbase, in a Chrome
process on the local machine. No Browserbase API, key or quota is involved.

- Each run gets a fresh, empty Chrome profile under the git-ignored `data/runtime/`;
  the user's own profile, cookies and extensions are never used. Remote debugging
  listens on `127.0.0.1` only.
- Reviewable results (`needs_human_input` / `ready_for_human_review`) leave the visible
  window open. Before disconnecting, the runner removes its network routes and releases
  its in-page submit guard with a per-run secret, so the human can finish and submit.
  Nothing is clicked on the human's behalf. Any other outcome closes the browser and
  deletes its profile.
- `release` ends only a browser this transport started, verified by its exact profile
  directory, and deletes only that profile.
- Before the first decision on any page the runner waits, for a bounded time, for a
  stable structural fingerprint (`form_not_rendered` if the form never appears).

### Approved CV upload (`local_browser` only, opt-in per request)

`submission-execute ... --live-cv-upload --live-cv-upload-operation ENDPOINT OPERATION
--live-cv-storage-write METHOD HOST --live-cv-association-operation ENDPOINT OPERATION`
lets the runner upload exactly the sealed CV into the one control labelled as a
resume/CV. All three are exact and valid only inside the CV-upload window that opens
after every other control on the page:

1. the upload-handle mutation (exact endpoint + operation name);
2. the exact `(method, host)` object-storage write; a readable body must carry the
   sealed CV's bytes;
3. one form-association mutation, only after that handle and storage write succeeded,
   and only when it names that upload.

`cv_upload_status = uploaded` requires all three to succeed, with 2xx responses and no
GraphQL errors, and the file name visible in the page. A blocked write in the window
stops the run (`blocked`); anything incomplete escalates to the human. Outside the window
every one of these writes is fatal, as are unknown file/form mutations and any final or
submit mutation. Run summaries report `cv_upload_steps` (`upload_handle_created`,
`storage_written`, `form_associated`, `visible_in_page`, `responses_ok`).

## Apply one application (`submission-apply-*`)

Generic orchestration over the commands above, for one application id. It adds no permission.
Package preparation, approval, enqueue, claim, execute, release and close are the same service
calls, with the same checks.

| Step | Command | What it does |
|---|---|---|
| 1 | `submission-apply-preview --application-id A` | Loads the application. Prepares the package normally (CV hash, JD match, eligibility, review gate, required answers). Matches the reviewed ATS execution profile by the URL's exact host, and refuses while another attempt is active. Prints the package hash, gate status, review reasons, CV id and answer field names, never values. Approves, queues and starts nothing. |
| 1b | `... --acknowledge-blockers --reviewer R --ack-reason TEXT` | Only for a `BLOCK_AUTO_SUBMISSION` package. Writes one structured `SupervisedReviewAcknowledgement` to `supervised_acks/<cv-id>-<time>.json` for exactly the current review reasons (`SUPERVISED_EXECUTION_ONLY`, `final_submit_allowed: false`). It is written only after the normal package check accepts it, so eligibility and the application policy still apply, and it never overwrites a file. An existing acknowledgement that still binds this application, job, CV, PDF hash and reasons is reused instead. |
| 2 | `submission-apply-start --application-id A --package-hash H --reviewer R --reviewed` | Re-prepares the package and refuses if the hash differs from H. An approval for the same hash that already backs an earlier request is revoked with the reason `superseded: approval already used by <request> (<status>)`. It then approves (the reviewer must be the acknowledging reviewer), queues one `local_browser` request for the profile's exact host, claims it and executes it once, with the profile's policy and a 30-minute human handoff. It prints a sanitized summary. It never creates an acknowledgement and never retries. |
| 3 | `submission-apply-status --application-id A` (or `--request-id`) | The sanitized outcome of the latest `local_browser` attempt. Changes nothing. |
| 4 | `submission-apply-release --application-id A` (or `--request-id`) | Ends the browser started for this attempt: only the journaled session, using the claim token stored for the request in the local store. The token is read in-process and never printed. Records no result. |
| 5 | `submission-close --request-id R ...` | Unchanged. The human confirms nothing was submitted. |

**Execution profiles** (`ats_profiles.py`) use exact-host matching: HTTPS, no port, credentials,
case or trailing-dot variant, and no sub- or parent-domain fallback. Ashby and Greenhouse carry
supervised-handoff validation; Lever and Workable are fixture-tested and require an explicit
validation-run opt-in. Profiles specify exact reads, permitted fill/upload writes and nonfatal
blocked telemetry. Every value is validated by `execute`.

Work-authorisation and immigration fields are human-only for every executor. Cover-letter drafts
remain local; their PDF/DOCX hashes can be bound into a sealed package without automating a free-text
answer or final submission.

`batch-prepare`, `batch-queue`, `batch-approve`, `batch-run-next` and `batch-status` orchestrate the
same single-application services. The hash-chained batch ledger records decisions while item state
is derived from the existing stores. Only one executor runs at a time; each handoff uses a new tab,
and releasing that request closes only its bound tab. Employer restrictions and verification widgets
can require a separate human decision before execution.

After the human submits, `submission-human-submitted` records an explicit human-attested receipt
for the sealed CV and package. It does not infer submission from a completed automation run.
