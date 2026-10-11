"""Small, single-responsibility commands for the local submission MVP."""
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path

from jobintel.models.submission import ExecutionResult, SupervisedReviewAcknowledgement
from jobintel.submission.answers import AnswerBank, ALLOWED_FIELDS
from jobintel.submission.service import SubmissionService


def add_submission_commands(subparsers):
    specs = {
        "preview": "Inspect the exact package/hash before human review (no approval)",
        "approve": "Record human approval of the exact inspected package",
        "revoke": "Append an approval revocation event",
        "enqueue": "Queue an already-approved package; does not execute",
        "list": "Inspect approvals/queue and record detected expiration or invalidation",
        "claim": "Exclusively claim one queued request; does not execute",
        "execute": "Produce executor handoff; opens no browser and submits nothing",
        "airtap-check": "Validate an AirTap bundle immediately before supervised runtime use",
        "airtap-import": "Import a strict stop-only AirTap report; never marks submitted",
        "complete": "Record one execution result; does not verify or update applications",
        "verify": "Human-verify receipt evidence; does not update applications",
        "reconcile": "Recover verified receipts into idempotent application/history updates",
        "answer-set": "Append a reviewed answer for a permitted repeated field",
        "handoff-status": "Show a Browserbase human-review handoff (runtime-only Live View link); changes nothing",
        "handoff-release": "Explicitly release a Browserbase handoff session bound to this request; records no result",
        "close": "Human confirms a stop-only request ended with NO application submitted: closed_not_submitted "
                 "(terminal, not a receipt, never APPLIED)",
    }
    for name, help_text in specs.items():
        p = subparsers.add_parser("submission-" + name, help=help_text)
        p.add_argument("--store", default="data/local")
        p.add_argument("--config", default="config/personal_strategy.json")
        if name in {"preview", "approve"}:
            p.add_argument("--application-id", required=True)
            p.add_argument("--supervised-ack-file", type=Path,
                           help="Structured SupervisedReviewAcknowledgement JSON (supervised execution only; never final submit)")
        if name in {"approve", "revoke", "verify", "answer-set", "close"}:
            p.add_argument("--reviewer", required=True)
        if name == "approve":
            p.add_argument("--package-hash", required=True)
            p.add_argument("--reviewed", action="store_true", required=True)
            p.add_argument("--acknowledge-review-reasons", action="store_true")
            p.add_argument("--expires-in", type=int, default=86400)
        if name in {"revoke", "enqueue"}:
            p.add_argument("--approval-id", required=True)
        if name == "enqueue":
            p.add_argument("--executor", choices=["manual", "airtap", "browserbase", "local_browser"], default="manual",
                           help="local_browser: the same stop-only runner in a local headed Chromium (isolated "
                                "profile, no Browserbase API); its visible window is the human handoff")
            p.add_argument("--live-host", default=None,
                           help="Browser executors only: queue THIS request for the exact host of the approved URL "
                                "(no wildcards). Execution still needs --allow-live-host and --human-handoff")
        if name == "revoke":
            p.add_argument("--reason", required=True)
        if name in {"claim", "execute", "complete", "verify", "airtap-check", "airtap-import", "handoff-status",
                    "handoff-release", "close"}:
            p.add_argument("--request-id", required=True)
        if name == "claim":
            p.add_argument("--worker", required=True)
            p.add_argument("--lease-seconds", type=int, default=900)
        if name in {"execute", "complete", "airtap-check", "airtap-import", "handoff-release"}:
            p.add_argument("--claim-token", required=True)
        if name == "execute":
            p.add_argument("--human-handoff", action="store_true",
                           help="Browserbase only: keep a reviewable session alive for human Live View review. "
                                "Never submits; release it with submission-handoff-release")
            p.add_argument("--review-minutes", type=int, default=30, help="Human-review window (1..355 minutes)")
            p.add_argument("--allow-live-host", action="store_true",
                           help="Browserbase only: explicitly run this one request on its queued real host. "
                                "Requires --human-handoff and --live-host; recording/logging off; never submits")
            p.add_argument("--live-host", default=None, help="Exact host this request was queued for")
            p.add_argument("--live-write-allow", nargs=2, action="append", default=[], metavar=("METHOD", "URL"),
                           help="Exact same-host, non-final background write to permit for this run only "
                                "(repeatable, at most 5; never a GraphQL endpoint)")
            p.add_argument("--live-graphql-read", nargs=2, action="append", default=[],
                           metavar=("ENDPOINT", "OPERATION_NAME"),
                           help="Exact GraphQL READ (query) operation to permit on this run only: exact https "
                                "endpoint on the live host + exact operationName (repeatable, at most 10). "
                                "Mutations and unknown operations are always blocked")
            p.add_argument("--live-graphql-form-write", nargs=2, action="append", default=[],
                           metavar=("ENDPOINT", "OPERATION_NAME"),
                           help="Exact GraphQL form-value MUTATION (e.g. Ashby ApiSetFormValue autosave) to permit "
                                "on this run only, and only while the runner fills an approved field: exact https "
                                "endpoint on the live host + exact non-final operationName (repeatable, at most 3)"),
            p.add_argument("--live-nonfatal-blocked-write", nargs=2, action="append", default=[],
                           metavar=("METHOD", "HOST"),
                           help="Exact third-party telemetry host (e.g. a RUM beacon) whose writes stay BLOCKED and "
                                "recorded but do not stop the run (repeatable, at most 3; never the application "
                                "host, no wildcards). Every other blocked write still stops the run"),
            p.add_argument("--live-cv-upload", action="store_true",
                           help="local_browser only: upload the approved package's sealed CV (the CV attached to the "
                                "application) into the one plain Resume/CV control. No file path is accepted")
            p.add_argument("--live-cv-upload-operation", nargs=2, action="append", default=[],
                           metavar=("ENDPOINT", "OPERATION_NAME"),
                           help="Exact upload-handle GraphQL mutation allowed only inside the CV-upload window "
                                "(repeatable, at most 2; never a final-action name)")
            p.add_argument("--live-cv-storage-write", nargs=2, action="append", default=[], metavar=("METHOD", "HOST"),
                           help="Exact storage upload (POST/PUT + exact host, not the application host) allowed only "
                                "inside the CV-upload window (repeatable, at most 2)")
            p.add_argument("--live-cv-association-operation", nargs=2, action="append", default=[],
                           metavar=("ENDPOINT", "OPERATION_NAME"),
                           help="Exact GraphQL mutation that attaches the uploaded CV to the form field, allowed once "
                                "inside the CV-upload window and only after its handle + storage upload succeeded "
                                "(at most 1)")
        if name == "handoff-release":
            p.add_argument("--session-id", required=True)
        if name in {"complete", "airtap-import"}:
            p.add_argument("--result-file", type=Path, required=True)
        if name == "verify":
            p.add_argument("--confirmed", action="store_true", required=True)
        if name == "close":
            p.add_argument("--confirmed-not-submitted", action="store_true", required=True,
                           help="You reviewed what happened (including any Live View handoff) and NO application "
                                "was submitted")
            p.add_argument("--reason", required=True)
        if name == "answer-set":
            p.add_argument("--field", choices=sorted(ALLOWED_FIELDS), required=True)
            p.add_argument("--value", required=True)
            p.add_argument("--provenance", required=True)
            p.add_argument("--job-id", default="*")


def run_submission_command(args, parser):
    svc = SubmissionService(args.store, args.config)
    name = args.command.removeprefix("submission-")
    try:
        ack = None
        if getattr(args, "supervised_ack_file", None) is not None:
            if args.supervised_ack_file.stat().st_size > 100_000:
                raise ValueError("Supervised acknowledgement file too large")
            ack = SupervisedReviewAcknowledgement.from_dict(json.loads(args.supervised_ack_file.read_text(encoding="utf-8")))
        if name == "preview":
            package = svc.preview(args.application_id, ack)
            result = {"package": asdict(package), "package_hash": package.package_hash}
        elif name == "approve":
            result = svc.approve(args.application_id, package_hash=args.package_hash, reviewer=args.reviewer,
                                 reviewed=args.reviewed, acknowledge_reasons=args.acknowledge_review_reasons,
                                 ttl_seconds=args.expires_in, supervised_acknowledgement=ack)
        elif name == "revoke":
            result = svc.revoke(args.approval_id, reviewer=args.reviewer, reason=args.reason)
        elif name == "enqueue":
            result = svc.enqueue(args.approval_id, executor=args.executor, live_host=args.live_host)
        elif name == "list":
            result = svc.list()
        elif name == "claim":
            result = svc.claim(args.request_id, worker=args.worker, lease_seconds=args.lease_seconds)
        elif name == "execute":
            result = svc.execute(args.request_id, claim_token=args.claim_token, human_handoff=args.human_handoff,
                                 review_seconds=args.review_minutes * 60, allow_live_host=args.allow_live_host,
                                 live_host=args.live_host,
                                 live_write_allowlist=tuple(tuple(e) for e in args.live_write_allow),
                                 live_graphql_reads=tuple(tuple(e) for e in args.live_graphql_read),
                                 live_graphql_form_writes=tuple(tuple(e) for e in args.live_graphql_form_write),
                                 live_nonfatal_blocked_writes=tuple(tuple(e) for e in args.live_nonfatal_blocked_write),
                                 live_cv_upload=args.live_cv_upload,
                                 live_cv_upload_mutations=tuple(tuple(e) for e in args.live_cv_upload_operation),
                                 live_cv_storage_writes=tuple(tuple(e) for e in args.live_cv_storage_write),
                                 live_cv_association_mutations=tuple(tuple(e) for e in args.live_cv_association_operation))
        elif name == "handoff-status":
            result = svc.handoff_status(args.request_id)
        elif name == "close":
            result = svc.close_not_submitted(args.request_id, reviewer=args.reviewer,
                                             confirmed_not_submitted=args.confirmed_not_submitted, reason=args.reason)
        elif name == "handoff-release":
            result = svc.release_handoff(args.request_id, session_id=args.session_id, claim_token=args.claim_token)
        elif name == "airtap-check":
            result = svc.check_airtap(args.request_id, claim_token=args.claim_token)
        elif name == "airtap-import":
            if args.result_file.stat().st_size > 1_000_000:
                raise ValueError("Execution result file too large")
            result = svc.complete_airtap(args.request_id, claim_token=args.claim_token,
                value=json.loads(args.result_file.read_text(encoding="utf-8")))
        elif name == "complete":
            if args.result_file.stat().st_size > 1_000_000:
                raise ValueError("Execution result file too large")
            result = svc.complete(args.request_id, claim_token=args.claim_token,
                                  result=ExecutionResult.from_dict(json.loads(args.result_file.read_text(encoding="utf-8"))))
        elif name == "verify":
            result = svc.verify(args.request_id, reviewer=args.reviewer, confirmed=args.confirmed)
        elif name == "reconcile":
            result = svc.reconcile()
        elif name == "answer-set":
            result = AnswerBank(args.store).set(args.field, args.value, reviewed_by=args.reviewer,
                                                provenance=args.provenance, job_id=args.job_id)
        else:
            raise ValueError("Unknown submission command")
    except (ValueError, OSError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(result) if is_dataclass(result) else result, ensure_ascii=False, indent=2))
