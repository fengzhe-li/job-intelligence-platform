# Architecture evolution: what was tried and what it taught

The supervised application path was not designed up front. It came out of a series of
real, supervised runs against one live Ashby-hosted application form (October 2026), each
of which stopped safely on something the design had not anticipated. This page records the
approaches that were tried, including the ones that were superseded, and the evidence that
shaped the current architecture.

Every run below was supervised, used a sealed and human-approved package, and stopped before
the final action. None submitted an application. "Stopped" means the safety design worked as
intended; the problem was that the design was still incomplete.

## The run log, condensed

Source: the local append-only submission ledger (sanitised; request and session ids omitted).

| # | Executor | What stopped the run | Change it led to |
|---|---|---|---|
| 1 | Browserbase | POSTs to the application host blocked: the GraphQL endpoint carries reads *and* writes behind one URL | Inspect GraphQL operations locally; allow only exact `(endpoint, operationName)` **queries** |
| 2 | Browserbase | Page-load reads (`ApiJobPosting`, `ApiOrganizationFromHostedJobsPageName`) blocked | Explicit per-run read allowlist |
| 3 | Browserbase | Location autocomplete read (`ApiAutocompleteGeoLocation`) blocked | Added as an exact read |
| 4 | Browserbase | Per-field autosave mutation (`ApiSetFormValue`) blocked | Mutation allowed only inside a short window around filling one approved field |
| 5 | Browserbase | Selecting the CV started the page's own upload flow (`ApiCreateFileUploadHandle`) | On a real host the CV became `human_required` by default |
| 6 | Browserbase | Reached the full form (13 controls), filled the deterministic fields, escalated the open questions | First end-to-end handoff, CV still uploaded by the human |
| 7 | Browserbase | Third-party RUM telemetry POST blocked before the form rendered | Explicit **non-fatal** telemetry list: still aborted, recorded, but doesn't stop the run |
| 8 | Browserbase | Session creation refused with **HTTP 402** (plan quota) | Failure classification; move the primary path to local headed Chrome |
| 9 | Local Chrome | Zero controls: scanned right after `load`, before the SPA drew the form | Bounded structural render wait |
| 10 | Local Chrome | Browser-generated CSP violation report blocked | Added to the non-fatal list (exact method + host) |
| 11 | Local Chrome | Full form, CV `human_required`, open questions escalated | Same result as run 6, now on the local transport |
| 12 | Local Chrome | Approved CV upload: object-storage write blocked | Exact storage `(method, host)` allowed only inside the CV-upload window, body must carry the sealed bytes |
| 13 | Local Chrome | Handle and storage write succeeded; the form-association mutation (`ApiSetFormValueToFile`) blocked | Association allowed once, only after this upload's handle + storage succeeded, and only if it names that upload |
| 14 | Local Chrome | **Handoff**: identity verified, deterministic fields filled, CV uploaded and associated, open questions escalated, nothing fatal blocked, Submit never clicked | Current architecture |

## Approaches that were superseded

### AirTap (cloud-phone agent)

- **Attempt.** Hand a sealed package to an external agent through its official skill. The
  repository wrote an offline task bundle that told the agent to stop before final submit, and
  imported the agent's result through a strict importer.
- **Why it was tried.** It needed no browser automation in this repository, and the agent
  could drive arbitrary sites.
- **What worked.** The bundle/importer contract, the stop-only result vocabulary and the
  "package is the only source of answers" rule all carried over to later executors
  (`stop_only.py` is shared).
- **What failed.** The documented API has no way to put a file on the device: attachments are
  model-input images, not files an upload control can use. A stop instruction to a model is
  also not an enforced UI restriction.
- **Lesson.** If the system has to guarantee "never submit" and "exactly this CV", that
  guarantee has to come from code the system controls, not from instructions to an agent.
- **Decision.** AirTap stays an offline, supervised-only adapter (the CV is uploaded by the
  human). It was never used on a real application. Execution moved to a browser runner owned
  by this codebase.

### Browserbase cloud browser

- **Attempt.** Run the repository's own stop-only runner (Playwright over CDP) inside a
  Browserbase session, with a human handoff through Live View.
- **Why it was tried.** Real Chromium, isolated from the user's machine and profile, with
  session recording and logging that could be switched off per session.
- **What worked.** It validated the core design on a real ATS: page-identity checks, exact
  host binding, the in-page submit guard, GraphQL read/write inspection, and the handoff and
  release lifecycle. Runs 1–7 found most of the network-policy gaps listed above.
- **What failed / limits.** The account's plan quota ended a run at session creation (HTTP 402).
  Every attempt depended on a third-party service and API key. Live View links are bearer
  links and had to be kept out of every log. Cloud round-trip latency also hid a timing
  assumption (see below).
- **Lesson.** The runner was the valuable part; the browser host was interchangeable.
- **Decision.** The runner was separated from its transport (`browser_session.py`).
  Browserbase stays as a supported transport and regression path. **Local headed Chrome**
  became the primary supervised route.

### Local headed Chrome (current primary path)

- A fresh, empty Chrome profile per run under a git-ignored runtime directory (`data/runtime/`).
  The user's own Chrome profile, cookies and extensions are never used.
- Remote debugging listens on `127.0.0.1` only. The runner connects over CDP exactly as it did
  to Browserbase, with the same sealed bundle and the same rules.
- The visible window *is* the handoff. Release ends only the process started for that request,
  verified by its exact profile directory, and deletes only that profile.

## Real-world discoveries

**An SPA shell appears before its form.** The page fires `load` while the form is still
fetching its definition. A scan at that moment sees zero controls and, correctly, refuses to
navigate. The runner now waits, for a bounded time, until two consecutive scans 500 ms apart
show the same non-empty *structural fingerprint* (control and action counts). It runs every
network-guard check while waiting, and stops with `form_not_rendered` rather than guessing.

**Cloud latency can mask local timing.** On Browserbase, runs reached the full form without
an explicit wait. The first local run scanned too early. The most likely explanation is that
cloud round-trips had been giving the SPA time to render. The runner had depended on timing
it didn't control, and moving to a faster transport exposed it.

**A strict network guard meets real-page telemetry.** RUM beacons and browser-generated CSP
reports are writes too. Allowing them broadly would weaken the guard; making them fatal stops
every run. The compromise is an explicit per-run list of exact `(method, host)` pairs, on hosts
other than the application host. Those requests are still aborted and never leave the browser;
they are recorded as non-fatal. Every other blocked write stays fatal.

**A GraphQL endpoint cannot be allowlisted by URL.** One URL carries reads, autosaves, upload
handles and the final submission. Requests are parsed locally and allowed only as an exact
`(endpoint, operationName, operation type)`. Mutations are allowed only inside narrow windows
tied to a specific approved action.

**A visible file name is not an upload.** In one run the page showed the CV's file name while
the upload had in fact been blocked mid-way. The upload is now a small state machine:

```text
selected ─► upload handle created ─► storage write (sealed bytes, 2xx) ─► form association ─► visible in page
                                                                                         └─► uploaded
any blocked write in the window ─► blocked (run stops)      anything incomplete ─► escalated to the human
```

`uploaded` requires every step plus successful responses. The association mutation must name
the handle created in the same window. A storage write whose body is readable must contain
the sealed CV's exact bytes.

**The submit guard has to be released deliberately.** The runner blocks every submission path
in the page while it works. If that guard stayed in the handed-off page, the human couldn't
submit either. The guard can be released only by the runner, using a per-run secret the page
doesn't know, after the runner's network routes are removed and immediately before the
browser is handed over. Nothing is clicked on the human's behalf.

**Exact policies beat broad allowlists.** Each gap above was closed with the narrowest rule
that fixed it: one host, one method, one operation name, one window, one use. No wildcards, no
suffix matching, no learning hosts automatically, no automatic retry. Every relaxation is
named in a test, and so is its fatal counterpart.

## What carried through unchanged

- The sealed, human-approved package is the only source of answers and of the CV.
- Unknown, sensitive or ambiguous questions escalate. Free-text questions the employer
  reserves for the candidate are never generated.
- `final_submit_clicked` is always false. No code path, flag or option clicks final Submit.
