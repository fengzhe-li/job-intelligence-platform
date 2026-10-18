"""batch-* commands: prepare -> queue -> approve -> run-next -> status (see jobintel.pipeline.batch)."""
from __future__ import annotations

import json
import sys

from jobintel.pipeline import batch


def add_batch_commands(subparsers):
    specs = {
        "prepare": "Batch step 1: eligibility, ranking, role-specific CV (reused if unchanged), sealed-package "
                   "preview and ATS routing for up to --limit jobs (or exactly --job-id); applies nothing",
        "queue": "Batch step 2: the consolidated review queue (also written to data/local/batch/<id>/review_queue.md)",
        "approve": "Batch step 3: a named human approves ONE item's exact current package for one supervised attempt",
        "run-next": "Batch step 4: automate approved items one after another, each in a NEW tab of one shared headed "
                    "browser, stopping at each human handoff (tabs stay open); never clicks Submit",
        "status": "What is running, what completed or failed (and why), what needs human action",
        "remove": "A named human removes one item from the batch (kept with its CV/package for later batches)",
        "note-restriction": "Record an employer reapplication/frequency warning a human saw (exact wording), "
                            "classify it and compare job ids with your applications to that company; changes no state",
    }
    for name, help_text in specs.items():
        p = subparsers.add_parser("batch-" + name, help=help_text)
        p.add_argument("--store", default="data/local")
        p.add_argument("--config", default="config/personal_strategy.json")
        p.add_argument("--batch-id", required=True)
        if name == "prepare":
            p.add_argument("--limit", type=int, default=5)
            p.add_argument("--job-id", action="append", default=[])
            p.add_argument("--max-projects", type=int, default=4)
            p.add_argument("--regenerate", action="append", default=[],
                           help="Job id whose CV must be regenerated (recorded); otherwise unchanged CVs are reused")
        if name == "approve":
            p.add_argument("--application-id", required=True)
            p.add_argument("--reviewer", required=True)
            p.add_argument("--reviewed", action="store_true", required=True)
            p.add_argument("--acknowledge-review-reasons", action="store_true")
            p.add_argument("--acknowledge-blockers", action="store_true")
            p.add_argument("--ack-reason", default=None)
            p.add_argument("--company-limit-decision", choices=batch.LIMIT_DECISIONS, default=None,
                           help="Required when the company restricts applications: automation or manual (your choice)")
        if name == "run-next":
            p.add_argument("--max-items", type=int, default=1)
            p.add_argument("--allow-unvalidated-profile", action="store_true",
                           help="ONLY for the one explicitly approved validation run of a new ATS profile")
            p.add_argument("--confirm-human-verification", action="append", default=[],
                           help="Your explicit decision, at execution time, to run supervised automation on a form "
                                "that loads a human-verification widget (verification itself stays yours)")
            p.add_argument("--confirm-company-limit", action="append", default=[],
                           help="Second confirmation, at execution time, for an application under a company restriction")
        if name == "remove":
            p.add_argument("--application-id", required=True)
            p.add_argument("--reviewer", required=True)
            p.add_argument("--reason", required=True)
        if name == "note-restriction":
            p.add_argument("--application-id", required=True)
            p.add_argument("--wording", required=True, help="The employer's exact wording")
            p.add_argument("--source", default="page_warning (human-reported)")
        if name in {"queue", "status"}:
            p.add_argument("--json", action="store_true")


def run_batch_command(args):
    name = args.command.removeprefix("batch-")
    try:
        if name == "prepare":
            out = batch.prepare(args.store, args.config, batch_id=args.batch_id, job_ids=tuple(args.job_id),
                                limit=args.limit, max_projects=args.max_projects, regenerate=tuple(args.regenerate))
        elif name == "queue":
            path, rows = batch.write_queue(args.store, args.config, args.batch_id)
            if not args.json:
                print(batch.render_queue(rows, args.batch_id))
                print(f"(written to {path})")
                return
            out = rows
        elif name == "approve":
            out = batch.approve(args.store, args.config, batch_id=args.batch_id, application_id=args.application_id,
                                reviewer=args.reviewer, reviewed=args.reviewed,
                                acknowledge_review_reasons=args.acknowledge_review_reasons,
                                acknowledge_blockers=args.acknowledge_blockers, ack_reason=args.ack_reason,
                                company_limit_decision=args.company_limit_decision)
        elif name == "run-next":
            out = batch.run_next(args.store, args.config, batch_id=args.batch_id, max_items=args.max_items,
                                 allow_unvalidated_profile=args.allow_unvalidated_profile,
                                 confirm_company_limit=tuple(args.confirm_company_limit),
                                 confirm_human_verification=tuple(args.confirm_human_verification))
        elif name == "remove":
            out = batch.remove(args.store, batch_id=args.batch_id, application_id=args.application_id,
                               reviewer=args.reviewer, reason=args.reason)
        elif name == "note-restriction":
            out = batch.note_restriction(args.store, batch_id=args.batch_id, application_id=args.application_id,
                                         wording=args.wording, source=args.source)
        else:
            out = batch.status(args.store, args.config, args.batch_id)
    except ValueError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(out, indent=2, default=str))
