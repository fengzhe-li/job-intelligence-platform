# AirTap integration contract (checked 2026-09-29; CV upload policy updated 2026-10-05)

Status: **adapter READY, supervised execution only** (`LIVE_BLOCKERS = ()`; CV upload is a
manual human step during the run). No AirTap task has been created and no real
application has been touched.

## Official sources (read directly, not summarised)

- https://github.com/airtap-ai/airtap-skill
- https://raw.githubusercontent.com/airtap-ai/airtap-skill/main/skills/airtap/SKILL.md
- https://raw.githubusercontent.com/airtap-ai/airtap-skill/main/skills/airtap/scripts/airtap.py
- https://raw.githubusercontent.com/airtap-ai/airtap-skill/main/skills/airtap/scripts/airtap_common.py

The skill file is `SKILL.md` (not `SKILLS.md`). It instructs an agent runtime
(Claude/Codex) to run its bundled Python CLI, which needs `requests` and
`python-dotenv` and authenticates with `AIRTAP_PERSONAL_ACCESS_TOKEN`
(Bearer header) against `https://airtap.ai/cortex/api`. The skill's
`scripts/.env` **overrides** the shell environment; `--add-token` writes the token
there. Keep that file outside this repository.

CLI surface (complete):

| Command | API |
| --- | --- |
| `receiver list` | `/receiver/v1/rcvrGetList` |
| `model list` | `/mreg/v1/mregGetModels` |
| `task create --receiver-id --message [--image-file] [--model-id]` | `/task/v1/taskCreate` |
| `task list`, `task get-details --task-id` | `/task/v1/taskGetList`, `taskGetDetails` |
| `task poll --task-id` (local; optional OpenClaw relay flags) | `taskGetDetails` |
| `task add-user-message --task-id --message [--image-file]` | `/task/v1/taskAddUserMessage` |
| `task cancel --task-id` | `/task/v1/taskCancel` |
| `user update-location --payload` | `/user/v1/userUpdateLocation` |

Prefer `--receiver-id cloud`; physical devices need explicit permission. Never
enable OpenClaw forwarding here. `COMPLETED` describes an agent task, not an ATS
submission. Waiting states need human input. Do not recreate a task after a
timeout or ambiguous failure; recover with `get-details` on the recorded task ID.

## PDF transfer: not available — manual human CV upload during the run

The only attachment mechanism is `--image-file`: the file is base64-encoded into
a chat message part `{"type": "image", "contentBase64": ...}` sent to the AirTap
agent. There is no documented generic file, PDF, device-storage push, file
injection or file-picker API, and no screenshot download command
(`summarize_message` only reports `hasImage`). An image message part is model
input, not a file on the cloud phone's storage, so it cannot feed an ATS upload.

Rejected workarounds: converting the CV to images; uploading the PDF to any public
URL or temporary file host; inventing an upload API.

Decision (2026-10-05): the CV no longer has to be on the cloud device before the task
starts, so `LIVE_BLOCKERS = ()` and `submission-airtap-check` reports
`live_execution: supervised_only`, `cv_upload: manual_human_upload`,
`final_submit_allowed: false`. The sealed local `cv.pdf` is still SHA-256-verified at
bundle creation and again by `submission-airtap-check`. During the run the agent fills
resolved fields; at a required CV/resume upload control it STOPS and asks the
supervising human to upload exactly the approved file (`cv.pdf` in the bundle = CV
artifact id, stored as `<artifact id>.pdf`, with its SHA-256). Asking is not uploading:
`cv_upload_confirmed` may be true only after the human confirms the exact file and the
form visibly shows it attached; otherwise the run stays `needs_human_input`, and the
importer downgrades any `ready_for_human_review` with `cv_upload_confirmed: false` to
`needs_human_input`. A browser upload yields no cloud-side SHA-256 and none may be
claimed. Screenshot availability as evidence is still unverified.

## Architecture

```
SubmissionService (ledger, approvals, claims)       ← only writer of SubmissionStore
  └─ execute(request, claim_token) for executor=airtap
       └─ AirTapSubmissionExecutor(authorization from live approval)
            → bundle: submission_request.json, airtap_task.md,
                      expected_result_schema.json, cv.pdf, manifest.json (all 0400)
  submission-airtap-check   → re-verify approval, package, sealed PDF, manifest hashes
  [human operator + AirTap-enabled Claude/Codex run the official skill — supervised;
   human uploads the CV at the upload control; never final submit]
  submission-airtap-import  → strict schema + identity + policy → ExecutionResult
  existing verify/reconcile → only reported_submitted with receipt evidence → APPLIED
```

The adapter implements the existing `SubmissionExecutor` protocol, imports only
`jobintel.models.submission`, and has no ApplicationStore/SubmissionStore,
network, subprocess or environment access (enforced by an AST test). It cannot
choose or modify a CV, decide eligibility, or generate answers.

## Bundle (`submission_request.json`)

`request_id, approval_id, package_hash, job_id, application_id, application_url,
company, role, cv_artifact_id, cv_sha256, cv_path ("cv.pdf", in-bundle), expires_at,
answers[{field,value}] (approved snapshot, permitted fields only), execution_mode
("STOP_BEFORE_FINAL_SUBMIT"), executor ("airtap"), receiver ("cloud"),
final_submit_allowed (false), live_execution_blockers`.
No profile, gate reasons, input hash, store paths or token. The only mode is
`STOP_BEFORE_FINAL_SUBMIT`; no final-submit mode exists in code.

Exception: a supervised package (see SUBMISSION_MVP.md, "Supervised exception") adds
`authorization: SUPERVISED_EXECUTION_ONLY`, the unchanged `review_status` and
`review_reasons`, and the `supervised_acknowledgement` (with `final_submit_allowed:
false`); `airtap_task.md` gains a "Supervised Execution Only" section. The hard stop
before final submit is unchanged.

## Candidate answers in `airtap_task.md`: package only, fail closed

The task generator (`airtap.generate_airtap_task`, also used by
`handoff.generate_airtap_task_markdown`) renders candidate answers **only** from the
approved package, which was resolved from the authoritative answer bank before
sealing. The executor/handoff layer holds no candidate facts and never substitutes a
literal value. `REQUIRED_HANDOFF_ANSWERS` (legal first/last name, email, phone, full
address, current right to work, permanent/unrestricted right to work, sponsorship now
or in future) must be present: otherwise `submission-enqueue --executor airtap` and the
executor both refuse, naming the missing fields, before any request or bundle exists.
Every other field absent from the package is listed as `UNRESOLVED` and the agent must
STOP and escalate if the form requires it. `handoff.resolve_application_form` uses
question-resolution rules only to pick the canonical bank field; the value always comes
from the bank.

## Result (`airtap_result.json`) and import

Required, no extras: `request_id, approval_id, package_hash, application_url,
cv_artifact_id, cv_sha256, observed_company, observed_role, observed_url, task_id,
executor ("airtap"), execution_mode (const), status, final_submit_clicked (const
false), cv_upload_confirmed, duplicate_detected, fields_filled[], questions_escalated[],
evidence[]`.

Statuses → ExecutionResult outcome:

| Reported / derived | Outcome | Why |
| --- | --- | --- |
| ready_for_human_review (all checks pass) | ready_for_human_review | not a receipt; cannot be verified or reconciled |
| needs_human_input, escalations, CV upload unconfirmed | needs_human_input | |
| already_applied, duplicate_detected | needs_human_input | a prior application may exist; blocks new approvals |
| aborted_wrong_job, or observed company/role/URL mismatch | failed | |
| failed | outcome_unknown | a live form was touched; cannot prove nothing was sent |
| outcome_unknown; ready without page evidence; any confirmation evidence | outcome_unknown | never retried automatically |

Rejected outright (request recorded `outcome_unknown`): malformed/extra/missing
fields, identity or hash mismatch with the approved package, `final_submit_clicked:
true`, a non-`STOP_BEFORE_FINAL_SUBMIT` mode, a `submitted` status, filled answers that
are not the exact approved value for an exact permitted label, a changed task ID.

## Escalation

Only exact labels for `name, email, phone, location, linkedin_url, github_url` may be
filled from the approved snapshot. Any label containing a sensitive term (sponsorship,
right to work, work permit, visa, immigration, nationality, citizenship, clearance,
salary, compensation, notice period, relocation, criminal/conviction, compliance,
disability, adjustments/accommodation, conflict of interest, cover letter, motivation,
"why do you/are you", free text, equal-opportunity questions), and any unknown or
ambiguous label, returns `needs_human_input` with `allowed_known_answers: []`.

## Evidence

Page evidence for `ready_for_human_review` must be one of: `screenshot_evidence`
(relative path + 64-hex SHA-256), `application_portal_status` (HTTPS on the approved
ATS host), or `screen_state` (≥ 40 chars of observed page description). Bare
assertions (`done`, `success`, `completed`, …) and `task_history` never count.
Receipt kinds (`confirmation_*`, `manual_verified`) in stop-only mode force
`outcome_unknown`. Evidence is carried into the existing `ReceiptEvidence` model.

## Open gaps before a first supervised PoC

1. PDF transfer to the cloud device: replaced by the supervised manual upload above.
2. Screenshot retrieval for evidence (unverified).
3. No recorded path yet from `ready_for_human_review` → human clicks Submit →
   receipt. Needs an explicit, human-attested command; deliberately not built here.

A task instruction is not a hardware-enforced UI restriction; a supervised operator
must inspect the final page.
