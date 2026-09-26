# Application Lifecycle Tracker

## What It Is

A richer, append-only-history tracker for actual submitted/in-progress applications, separate from the lightweight `WorkflowStatus` filter (`new`/`saved`/`applied`/`oa`/`interview`/`rejected`/`offer`/`ignore`) already used to tag every tracked job in the dashboard. `Application` represents one specific application a candidate has actually started or submitted, with a fuller status lifecycle and full status history — not every job that's merely been seen.

## Ownership (Phase 3): one source of truth per phase

Two stores used to hold overlapping status with nothing keeping them consistent. Ownership is now explicit (`jobintel.application_lifecycle`):

| Phase | States | Owner |
|---|---|---|
| Pre-application triage | `new`, `saved`, `ignore` (CV-ready / review-required are derived from CV artifacts, not stored) | `workflow_status.json` (LocalJobStore) |
| From submission on | `applied`, `online_assessment`, interviews, `offer`, `rejected`, `withdrawn`, `expired` | `ApplicationStore` only |

- `set_job_status` is the single write path from the inbox. A post-submission value creates the job's **one** application (`app:<job_id>`, get-or-create, idempotent) and appends a transition. It is never written to `workflow_status.json`.
- `effective_workflow_status` / `overlay_lifecycle` is the single read projection used by the inbox, priority queue and Today, so they can't disagree.
- A submitted application can't move back to a triage or pre-submission state (`ValueError`, HTTP 409). Setting the coarse `applied` again never regresses a later stage.
- `create_application` returns the existing record if the job already has one, so duplicate observations or double clicks never create a second application.
- The CV is frozen at submission. `submitted_cv_version_id` is recorded, and `attach_cv` refuses a different version afterwards. Every attachment is appended to history as a `CV attached: <id>` event.
- The application snapshots the JD, application URL, source provenance and deadline at creation.
- Legacy data: `migrate_legacy_workflow_statuses` (run at dashboard start) converts any old post-submission `workflow_status.json` value into the job's application. `applied_at` stays unknown rather than invented. The workflow entry is reset to `saved`, and the original value is kept in history and `applications/legacy_workflow_migration.jsonl`.

## Status Lifecycle

`ApplicationStatus` (`jobintel.models.taxonomy`):

```text
discovered -> shortlisted -> materials_ready -> applied -> online_assessment
  -> phone_screen -> technical_interview -> final_interview -> offer
```

Terminal states: `rejected`, `withdrawn`, `expired`. Transitions aren't restricted to this exact order (real hiring processes skip stages), but the status history always records every transition that actually happened.

## Storage

`jobintel.storage.application_store.ApplicationStore` follows the same convention as `LocalJobStore`/`HistoricalJobStore`:

- `data/local/applications/applications.json` — current state per application (overwritten on update).
- `data/local/applications/status_history.jsonl` — append-only; a row is written on every status transition and never mutated or deleted, so "what status was this application in on a given date" stays answerable.

`Application.cv_version_id`, `selected_project_ids`, and `evidence_ids_used` are populated by `ApplicationStore.attach_cv` once a CV is generated for the application (see [CV_GENERATION.md](CV_GENERATION.md)) -- together with `storage.cv_artifact_store.CVArtifactStore`, this answers the project goal of "what exact CV did I submit to this company?": `application-inspect` gives the `cv_version_id`, `cv-show` gives the exact CV text, selected projects, and evidence used.

## CLI

```bash
PYTHONPATH=src python3 -m jobintel.cli.main application-create --job-id job-42 --company "Example Co" --role-title "Graduate Software Engineer"
PYTHONPATH=src python3 -m jobintel.cli.main application-status --id example-co:graduate-software-engineer:job-42 --status shortlisted --note "Strong fit"
PYTHONPATH=src python3 -m jobintel.cli.main application-list --status shortlisted
PYTHONPATH=src python3 -m jobintel.cli.main application-inspect --id example-co:graduate-software-engineer:job-42
PYTHONPATH=src python3 -m jobintel.cli.main cv-generate --job-id job-42 --application-id example-co:graduate-software-engineer:job-42
```

## What's Implemented vs. Not Yet

Implemented: the data model, append-only status history, local storage, CLI create/transition/list/inspect commands, and CV-artifact attachment (`cv_version_id`/`selected_project_ids`/`evidence_ids_used`) via `cv-generate --application-id`.

Not yet implemented: dashboard surfacing of applications (deadlines, review queue, conversion analytics), reminders tied to `next_action_deadline`, and automatic status detection from email.
