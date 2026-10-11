from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from jobintel.models.submission import ApprovedSubmissionPackage, ExecutionResult, instant, utcnow, canonical
from jobintel.storage.locking import locked, store_lock
from jobintel.storage.submission_store import SubmissionStore
from jobintel.submission.executors import BROWSER_EXECUTORS, STOP_ONLY_EXECUTORS, ManualSubmissionExecutor
from jobintel.submission.packages import prepare_package, seal_pdf, verify_sealed_pdf
from jobintel.submission.receipts import sufficient_evidence


class ExecutorInterrupted(ValueError):
    """The attempt is outcome_unknown and must not be retried. `failure` is the sanitized,
    journaled reason (the message itself stays generic)."""

    def __init__(self, failure=None):
        super().__init__("Executor interrupted; investigate this attempt, do not retry")
        self.failure = dict(failure or {})


class SubmissionService:
    def __init__(self, root="data/local", config_path="config/personal_strategy.json", *, clock=utcnow,
                 browserbase_transport=None, browserbase_config=None, local_browser_transport=None,
                 runtime_root=None):
        self.root = Path(root).resolve()
        self.config_path = Path(config_path)
        self.clock = clock
        self.store = SubmissionStore(self.root)
        # Runtime-only Browserbase settings: the transport provides a browser and is untrusted
        # (its output goes through the strict stop-only import); neither is ever serialized.
        self._browserbase_transport = browserbase_transport
        self._browserbase_config = browserbase_config
        self._local_browser_transport = local_browser_transport
        # Ephemeral runtime (local-browser profiles): a sibling of the store, never inside it.
        self.runtime_root = Path(runtime_root).resolve() if runtime_root else self.root.parent / "runtime"

    def _browserbase_runtime(self, executor="browserbase"):
        """(transport, config) for a browser executor: Browserbase cloud, or a local headed
        Chromium (no Browserbase API or credentials; runtime state under the ignored store)."""
        from jobintel.submission.browserbase_service import BrowserbaseExecutionConfig
        config = self._browserbase_config or BrowserbaseExecutionConfig()
        if executor == "local_browser":
            from jobintel.submission.local_browser import LocalBrowserTransport
            return (self._local_browser_transport
                    or LocalBrowserTransport(runtime_dir=self.runtime_root / "local_browser",
                                             screenshot_dir=self.root / "submissions/local_browser_evidence",
                                             legacy_runtime_dirs=(self.root / "submissions/local_browser",))), config
        from jobintel.submission.browserbase_cloud import CloudTransport
        transport = self._browserbase_transport or CloudTransport(screenshot_dir=self.root / "submissions/browserbase_evidence")
        return transport, config

    @locked
    def preview(self, application_id, supervised_acknowledgement=None):
        return prepare_package(self.root, application_id, self.config_path, supervised_acknowledgement)

    @locked
    def approve(self, application_id, *, package_hash, reviewer, reviewed, acknowledge_reasons=False, ttl_seconds=86400,
                supervised_acknowledgement=None):
        if not reviewed or not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("Explicit human review and reviewer are required even for AUTO_PREPARE")
        if not 0 < ttl_seconds <= 7 * 86400:
            raise ValueError("Approval lifetime must be positive and at most seven days")
        self.store.assert_application_idle(application_id)
        package = self.preview(application_id, supervised_acknowledgement)
        if package.package_hash != package_hash:
            raise ValueError("Package changed since human review; inspect and review again")
        if package.gate_status == "review_required" and not acknowledge_reasons:
            raise ValueError("Human must acknowledge every review gate reason")
        if supervised_acknowledgement is not None and supervised_acknowledgement.reviewer != reviewer:
            raise ValueError("The approving reviewer must be the reviewer who made the supervised acknowledgement")
        for a in self.store.approvals().values():
            if a["package"]["application_id"] != application_id or a["status"] != "approved":
                continue
            if instant(a["expires_at"]) <= self.clock():
                self._invalidate(a["id"], "expired", "approval_lifetime_elapsed")
            elif a["package_hash"] == package_hash:
                return a
            else:
                self._invalidate(a["id"], "revoked", "package_inputs_changed")
        seal_pdf(self.root, package)
        aid = "approval:" + uuid4().hex
        self.store.append("approved", aid, {"package": asdict(package), "package_hash": package_hash,
                          "reviewer": reviewer, "human_reviewed": True, "acknowledged_reasons": list(package.gate_reasons),
                          "execution_mode": package.execution_mode,
                          "approved_at": self.clock().isoformat(), "expires_at": (self.clock() + timedelta(seconds=ttl_seconds)).isoformat()},
                          event_id=aid, at=self.clock().isoformat())
        return self.store.approvals()[aid]

    def _invalidate(self, approval_id, status, reason):
        a = self.store.approvals().get(approval_id)
        if a is None:
            raise ValueError("Unknown approval")
        requests = [r for r in self.store.requests().values() if r["approval_id"] == approval_id]
        if any(r["status"] in {"verified", "submitted"} for r in requests):
            raise ValueError("A verified submission cannot be revoked retroactively")
        if a["status"] == "approved":
            self.store.append(status, approval_id, {"reason": reason}, event_id=status + ":" + approval_id, at=self.clock().isoformat())
        for r in requests:
            if r["status"] == "submission_requested":
                self._request_event("cancelled", r["id"], {"reason": reason})
            elif r["status"] in {"executing", "pending_verification", "needs_human_input", "ready_for_human_review"}:
                self._request_event("outcome_unknown", r["id"], {"reason": reason})

    @locked
    def revoke(self, approval_id, *, reviewer, reason):
        if not reviewer.strip() or not reason.strip():
            raise ValueError("Revocation requires actor and reason")
        self._invalidate(approval_id, "revoked", f"human:{reviewer}: {reason}")
        return self.store.approvals()[approval_id]

    def _valid_package(self, approval_id):
        a = self.store.approvals().get(approval_id)
        if a is None:
            raise ValueError("Unknown approval")
        if a["status"] != "approved":
            self._invalidate(approval_id, a["status"], "approval_no_longer_active")
            raise ValueError("Approval is not active")
        if instant(a["expires_at"]) <= self.clock():
            self._invalidate(approval_id, "expired", "approval_lifetime_elapsed")
            raise ValueError("Approval expired")
        try:
            approved = ApprovedSubmissionPackage.from_dict(a["package"])
            current = self.preview(approved.application_id, approved.supervised_acknowledgement)
            verify_sealed_pdf(self.root, current)
        except (ValueError, OSError, KeyError):
            self._invalidate(approval_id, "revoked", "review_inputs_unavailable_or_changed")
            raise ValueError("Approval invalidated: review inputs unavailable or changed") from None
        if current.package_hash != a["package_hash"] or ApprovedSubmissionPackage.from_dict(a["package"]).package_hash != a["package_hash"]:
            self._invalidate(approval_id, "revoked", "package_inputs_changed")
            raise ValueError("Approval invalidated: package inputs changed")
        return current

    @locked
    def enqueue(self, approval_id, *, executor="manual", live_host=None):
        """`live_host` (browserbase only) queues ONE request for the exact real host of the approved
        URL. It is bound to this request only; execution still needs the explicit live-host opt-in."""
        if executor not in {"manual"} | STOP_ONLY_EXECUTORS:
            raise ValueError("Unknown executor")
        if live_host is not None and executor not in BROWSER_EXECUTORS:
            raise ValueError("A live host is only available for the browserbase executor or the local_browser executor")
        existing = next((r for r in self.store.requests().values() if r["approval_id"] == approval_id), None)
        if existing and existing["executor"] != executor:
            raise ValueError("An existing request cannot change executor")
        if existing and existing.get("live_host") != live_host:
            raise ValueError("An existing request cannot change its live host")
        if existing and existing["status"] in {"verified", "submitted"}:
            return existing
        package = self._valid_package(approval_id)
        if package.eligibility_state == "hard_ineligible":
            raise ValueError("HARD_INELIGIBLE package cannot be queued for execution")
        if package.gate_status == "block_auto_submission" and package.supervised_acknowledgement is None:
            raise ValueError("Blocked package cannot be queued for execution without a supervised acknowledgement")
        if executor in STOP_ONLY_EXECUTORS:
            from jobintel.submission.stop_only import missing_required_answers
            missing = missing_required_answers(package)
            if missing:
                # Fail closed before any request exists: the handoff never fills a
                # missing candidate answer from anywhere but the approved package.
                raise ValueError(f"Approved package is missing required candidate answers: {missing}; "
                                 "resolve them in the authoritative answer bank and review a new package")
        if executor in BROWSER_EXECUTORS:
            from jobintel.submission.browserbase_service import check_target, validate_live_host
            if live_host is None:
                check_target(package.application_url, self._browserbase_runtime(executor)[1])
            else:
                validate_live_host(live_host, package.application_url)
        if existing:
            if existing["status"] in {"cancelled", "failed", "outcome_unknown"}:
                raise ValueError("Existing request cannot be retried automatically")
            return existing
        self.store.assert_application_idle(package.application_id)
        rid = "request:" + uuid4().hex
        data = {"approval_id": approval_id, "application_id": package.application_id,
                "package_hash": package.package_hash, "executor": executor, "execution_mode": package.execution_mode}
        if live_host is not None:
            data["live_host"] = live_host
        self.store.append("enqueued", rid, data, event_id=rid, at=self.clock().isoformat())
        return self.store.requests()[rid]

    def _request_event(self, kind, request_id, data):
        self.store.append(kind, request_id, data, event_id=kind + ":" + uuid4().hex, at=self.clock().isoformat())

    def _request(self, request_id):
        r = self.store.requests().get(request_id)
        if r is None:
            raise ValueError("Unknown submission request")
        return r

    @locked
    def claim(self, request_id, *, worker, lease_seconds=900):
        if not worker.strip() or not 0 < lease_seconds <= 86400:
            raise ValueError("Claim requires worker and bounded lease")
        r = self._request(request_id)
        if r["status"] != "submission_requested":
            raise ValueError("Only a never-claimed queued request can be claimed")
        self._valid_package(r["approval_id"])
        self._request_event("claimed", request_id, {"worker": worker, "claim_token": uuid4().hex,
                            "claimed_at": self.clock().isoformat(), "lease_until": (self.clock() + timedelta(seconds=lease_seconds)).isoformat()})
        return self._request(request_id)

    def _cv_upload_policy(self, package, mutations, storage_writes, associations=()):
        """The approved CV, bound to this application (it must still be the attached CV), this sealed
        package (artifact id + PDF SHA-256) and this request. The file name is derived from the
        package's legal name; the bytes are only ever the sealed bundle copy."""
        import re
        from jobintel.storage.application_store import ApplicationStore
        from jobintel.submission.browserbase_runner import CvUploadPolicy, validate_cv_upload_for
        application = next((a for a in ApplicationStore(self.root).read_applications()
                            if a.id == package.application_id), None)
        if application is None or application.cv_version_id != package.cv_artifact_id:
            raise ValueError("The approved CV is no longer the CV attached to this application")
        name = next((a.value for a in package.answers if a.field == "legal_full_name"), "") or ""
        stem = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
        policy = CvUploadPolicy(package.cv_artifact_id, package.cv_pdf_sha256, f"{stem}_CV.pdf" if stem else "CV.pdf",
                                frozenset(tuple(e) for e in mutations), frozenset(tuple(e) for e in storage_writes),
                                frozenset(tuple(e) for e in associations))
        return validate_cv_upload_for(package.application_url, package.cv_artifact_id, package.cv_pdf_sha256, policy)

    def _check_token(self, request, claim_token):
        if not claim_token or request.get("claim_token") != claim_token:
            raise ValueError("Invalid claim token")

    def execute(self, request_id, *, claim_token, executor=None, human_handoff=False, review_seconds=1800,
                allow_live_host=False, live_host=None, live_write_allowlist=(), live_graphql_reads=(),
                live_graphql_form_writes=(), live_nonfatal_blocked_writes=(), live_cv_upload=False,
                live_cv_upload_mutations=(), live_cv_storage_writes=(), live_cv_association_mutations=()):
        """`allow_live_host` + `live_host` (+ optional exact `live_write_allowlist`) run a request
        queued for a real host: once, under mandatory human handoff, recording/logging off. There
        is no final-submit option anywhere on this path.

        `live_cv_upload` (local_browser only) lets the runner upload the approved package's sealed
        CV once -- the artifact attached to the application and sealed in the package; no path is
        accepted from the caller -- with only the exact upload-handle mutations, (method, host)
        storage writes and file-association mutation given here, inside the runner's CV-upload window."""
        with store_lock(self.root):
            r = self._request(request_id)
            self._check_token(r, claim_token)
            if human_handoff:
                from jobintel.submission.browserbase_cloud import MAX_REVIEW_SECONDS
                if r["executor"] not in BROWSER_EXECUTORS:
                    raise ValueError("Human handoff is only available for the browserbase executor or the local_browser executor")
                if not 60 <= review_seconds <= MAX_REVIEW_SECONDS:
                    raise ValueError(f"Review window must be 60..{MAX_REVIEW_SECONDS} seconds")
            live_mode = bool(allow_live_host or live_host is not None or live_write_allowlist or live_graphql_reads
                             or live_graphql_form_writes or live_nonfatal_blocked_writes or r.get("live_host")
                             or live_cv_upload or live_cv_upload_mutations or live_cv_storage_writes
                             or live_cv_association_mutations)
            if live_mode:
                if r["executor"] not in BROWSER_EXECUTORS:
                    raise ValueError("Live-host mode is only available for the browserbase executor or the local_browser executor")
                if allow_live_host is not True or not live_host:
                    raise ValueError("Live-host execution needs the explicit live-host opt-in and the exact live host")
                if not human_handoff:
                    raise ValueError("Live-host execution requires human handoff")
                if r.get("live_host") != live_host:
                    raise ValueError("Live host does not match the host this request was queued for")
            if (live_cv_upload_mutations or live_cv_storage_writes or live_cv_association_mutations) and live_cv_upload is not True:
                raise ValueError("CV upload operations need the explicit CV upload opt-in")
            if live_cv_upload is not False and (live_cv_upload is not True or r["executor"] != "local_browser"):
                raise ValueError("An approved CV upload is only available for a local_browser live-host run")
            if r["status"] != "executing" or r.get("handoff_started"):
                raise ValueError("Execution cannot be started or repeated")
            if instant(r["lease_until"]) <= self.clock():
                self._request_event("outcome_unknown", request_id, {"reason": "claim_expired"})
                raise ValueError("Claim expired; investigate, do not retry")
            package = self._valid_package(r["approval_id"])
            if r["executor"] in STOP_ONLY_EXECUTORS and executor is not None:
                raise ValueError("AirTap executor is constructed only from a live service approval")
            if r["executor"] == "airtap":
                from jobintel.submission.airtap import AirTapSubmissionExecutor, AirTapAuthorization
                a = self.store.approvals()[r["approval_id"]]
                executor = AirTapSubmissionExecutor(self.root / "submissions/airtap", clock=self.clock,
                    authorization=AirTapAuthorization(a["id"], request_id, a["package_hash"],
                                                     a["expires_at"], a["human_reviewed"], a["status"]))
            elif r["executor"] in BROWSER_EXECUTORS:
                from jobintel.submission.browserbase import BrowserbaseBundleAuthorization
                from jobintel.submission.browserbase_service import (BrowserbaseServiceExecutor, LiveHostAuthorization,
                                                                     validate_live_host, validate_live_write_allowlist)
                from jobintel.submission.browserbase_runner import validate_nonfatal_blocked_writes
                from jobintel.submission.graphql_policy import validate_form_writes, validate_reads
                a = self.store.approvals()[r["approval_id"]]
                transport, config = self._browserbase_runtime(r["executor"])
                live = None
                if live_mode:
                    from jobintel.submission.browserbase_cloud import SESSION_TIMEOUT_SECONDS
                    # Expires with this request's review window (never later than the approval).
                    window_end = self.clock() + timedelta(seconds=SESSION_TIMEOUT_SECONDS + review_seconds)
                    live = LiveHostAuthorization(
                        request_id, package.application_id, a["id"], package.package_hash,
                        validate_live_host(live_host, package.application_url),
                        min(window_end, instant(a["expires_at"])).isoformat(),
                        validate_live_write_allowlist(package.application_url, live_write_allowlist),
                        tuple(sorted(validate_reads(package.application_url, live_graphql_reads))),
                        tuple(sorted(validate_form_writes(package.application_url, live_graphql_form_writes))),
                        tuple(sorted(validate_nonfatal_blocked_writes(package.application_url,
                                                                      live_nonfatal_blocked_writes))),
                        self._cv_upload_policy(package, live_cv_upload_mutations, live_cv_storage_writes,
                                               live_cv_association_mutations)
                        if live_cv_upload else None)
                executor = BrowserbaseServiceExecutor(self.root / "submissions" / r["executor"], transport=transport,
                    config=config, clock=self.clock, live=live, name=r["executor"],
                    authorization=BrowserbaseBundleAuthorization(a["id"], request_id, a["package_hash"],
                                                                 a["expires_at"], a["human_reviewed"], a["status"]))
                executor.preflight(package)  # target + credentials: refuse before anything is journaled
                if live is not None:
                    from jobintel.submission.browserbase_cloud import LIVE_BROWSER_SETTINGS
                    # Audit only: never read back as permission; the request cannot be executed again.
                    self.store.append("browserbase_live_host_authorized", request_id, {
                        "request_id": request_id, "application_id": live.application_id,
                        "approval_id": live.approval_id, "package_hash": live.package_hash, "host": live.host,
                        "expires_at": live.expires_at, "human_handoff_required": True,
                        "review_seconds": review_seconds,
                        "session_recording": LIVE_BROWSER_SETTINGS["recordSession"],
                        "session_logging": LIVE_BROWSER_SETTINGS["logSession"], "screenshots": "not_captured",
                        "write_allowlist_count": len(live.write_allowlist),
                        "graphql_read_allowlist_count": len(live.graphql_reads),
                        "graphql_form_write_allowlist_count": len(live.graphql_form_writes),
                        "nonfatal_blocked_write_count": len(live.nonfatal_blocked_writes),
                        **({"cv_upload_authorized": True, "cv_artifact_id": live.cv_upload.artifact_id,
                            "cv_pdf_sha256": live.cv_upload.sha256,
                            "cv_upload_mutation_count": len(live.cv_upload.graphql_mutations),
                            "cv_storage_write_count": len(live.cv_upload.storage_writes),
                            "cv_association_mutation_count": len(live.cv_upload.association_mutations)}
                           if live.cv_upload is not None else {}),
                        "final_submit_allowed": False,
                        "automatic_retry": False,
                        **({"executor": r["executor"]} if r["executor"] != "browserbase" else {})},
                        event_id="bb-live:" + request_id, at=self.clock().isoformat())
            else:
                executor = executor or ManualSubmissionExecutor()
            if executor.name != r["executor"]:
                raise ValueError("Executor identity does not match request")
            # Persist intent BEFORE invoking even a handoff. Reconcile treats interruption
            # as unknown. Never hold the store lock over external executor work.
            self.store.append("handoff_started", request_id, {}, event_id="handoff:" + request_id, at=self.clock().isoformat())
        if r["executor"] in BROWSER_EXECUTORS:
            return self._run_browserbase(request_id, claim_token, executor, package, human_handoff=human_handoff,
                                         review_seconds=review_seconds)
        try:
            handoff = executor.submit(package, idempotency_key=request_id)
            if r["executor"] == "airtap":
                import json
                with store_lock(self.root):
                    self.store.append("airtap_bundle_prepared", request_id,
                        {"manifest": json.loads((Path(handoff.bundle_path) / "manifest.json").read_text()),
                         "bundle_path": handoff.bundle_path}, event_id="bundle:" + request_id,
                        at=self.clock().isoformat())
            return handoff
        except Exception:
            with store_lock(self.root):
                self._request_event("outcome_unknown", request_id, {"reason": "executor_interrupted"})
            raise ValueError("Executor interrupted; investigate this attempt, do not retry") from None

    def _run_browserbase(self, request_id, claim_token, executor, package, *, human_handoff=False, review_seconds=0):
        """Bundle -> one transport run -> strict stop-only import. Any failure before a trusted
        import leaves the attempt outcome_unknown; nothing here can produce a receipt.

        human_handoff (opt-in): if the imported result is reviewable, the still-filled session is
        left alive and journaled as a handoff bound to this request/application/approval/package;
        its Live View link is returned at runtime only. Otherwise the session is released."""
        def journal(kind, data, event_id):
            with store_lock(self.root):
                self.store.append(kind, request_id, data, event_id=event_id, at=self.clock().isoformat())
        stage = "bundle_prepare"
        try:
            handoff = executor.prepare(package, idempotency_key=request_id)
            journal("browserbase_bundle_prepared", {"manifest": handoff.manifest, "bundle_path": handoff.bundle_path},
                    "bb-bundle:" + request_id)
            stage = "local_browser_run" if executor.name == "local_browser" else "browserbase_run"
            report, evidence = executor.run(handoff, on_session=lambda session_id: journal(
                "browserbase_session_started", {"session_id": session_id}, "bb-session:" + request_id),
                keep_alive=human_handoff, review_seconds=review_seconds if human_handoff else 0)
        except Exception as exc:
            from jobintel.submission.browserbase_cloud import safe_failure
            # Sanitized reason only (closed-set stage/category, HTTP status, class name): never the
            # exception text, which may echo a URL, header or response body.
            failure = safe_failure(exc, stage)
            with store_lock(self.root):
                self._request_event("outcome_unknown", request_id, {"reason": "executor_interrupted", "failure": failure})
            raise ExecutorInterrupted(failure) from None
        kept = {k: evidence.get(k) for k in ("session_id", "replay_url", "released", "release_error", "final_url",
                                             "screenshot_sha256", "screenshot_path", "allowed_domains", "handoff_kept",
                                             "identity_state")}
        if "page_guard_released" in evidence:  # local browser only
            kept["page_guard_released"] = evidence["page_guard_released"]
        if getattr(executor, "live", None) is not None:
            # Minimal live diagnostics only: privacy mode, stop reason, blocked method/host/path/type.
            live = evidence.get("live") or {}
            kept["live"] = {"host": executor.live.host, "recording": live.get("recording"),
                            "browser_logging": live.get("browser_logging"), "screenshot": live.get("screenshot"),
                            "stop_reason": live.get("stop_reason"), "blocked_requests": live.get("blocked_requests", []),
                            "allowed_requests": live.get("allowed_requests", []),
                            "nonfatal_blocked_requests": live.get("nonfatal_blocked_requests", []),
                            "form": live.get("form", {})}
        journal("browserbase_evidence", kept, "bb-evidence:" + request_id)
        try:
            recorded = self.complete_browserbase(request_id, claim_token=claim_token, value=report,
                                                 identity_state=kept["identity_state"])
        except Exception:
            if kept.get("handoff_kept"):
                self._release_session(request_id, kept["session_id"], executor.transport, reason="import_failed")
            raise
        out = {"request_id": request_id, "status": recorded["status"], "bundle_path": handoff.bundle_path,
               "session_id": kept["session_id"], "session_released": kept["released"],
               "final_submit_clicked": report.get("final_submit_clicked")}
        if not kept.get("handoff_kept"):
            return out
        if recorded["status"] not in {"ready_for_human_review", "needs_human_input"}:
            out["session_released"] = self._release_session(request_id, kept["session_id"], executor.transport,
                                                            reason="result_not_reviewable")["released"]
            out["handoff"] = "not_started"
            return out
        a = self.store.approvals()[recorded["approval_id"]]
        expires_at = (self.clock() + timedelta(seconds=review_seconds)).isoformat()
        handoff_data = {"session_id": kept["session_id"], "request_id": request_id,
                        "application_id": recorded["application_id"], "approval_id": a["id"],
                        "package_hash": recorded["package_hash"], "review_expires_at": expires_at,
                        "live_view_persisted": False}
        if getattr(executor, "live", None) is not None:
            handoff_data["live_host"] = executor.live.host
        if executor.name != "browserbase":
            handoff_data["executor"] = executor.name
        journal("browserbase_handoff_started", handoff_data, "bb-handoff:" + request_id)
        if executor.name == "local_browser":
            # The visible local window IS the handoff: no Live View link exists or is needed.
            out.update({"handoff": "local_browser_active", "review_expires_at": expires_at,
                        "page_guard_released": kept.get("page_guard_released") is True})
            return out
        out.update({"handoff": "active", "review_expires_at": expires_at})
        out.update(self._live_view(kept["session_id"], executor.transport, expires_at))
        return out

    def _live_view(self, session_id, transport, expires_at):
        """Runtime-only Live View link (never journaled); a failure leaves the handoff active."""
        remaining = int((instant(expires_at) - self.clock()).total_seconds())
        if remaining < 60:
            return {"live_view_url": None, "live_view_error": "review window elapsed"}
        try:
            view = transport.live_view(session_id, expires_in=remaining)
            return {"live_view_url": view["live_view_url"], "live_view_expires_in": view["expires_in"],
                    "live_view_page_urls": view.get("page_urls", [])}
        except Exception as exc:
            return {"live_view_url": None, "live_view_error": type(exc).__name__}

    def _handoff_events(self, request_id):
        events = [e for e in self.store.events() if e["entity_id"] == request_id]
        pick = lambda kind: next((e["data"] for e in events if e["kind"] == kind), None)
        return {"session": pick("browserbase_session_started"), "handoff": pick("browserbase_handoff_started"),
                "released": pick("browserbase_session_released"), "evidence": pick("browserbase_evidence"),
                "release_failures": [e["data"] for e in events if e["kind"] == "browserbase_release_failed"]}

    def _release_session(self, request_id, session_id, transport, *, reason):
        """Release one session bound to this request; journaled; idempotent. Never touches the
        request's result or the application."""
        with store_lock(self.root):
            if self._handoff_events(request_id)["released"] is not None:
                return {"released": True, "already_released": True}
        try:
            provider = transport.session_status(session_id)
        except Exception:
            provider = ""
        from jobintel.submission.browserbase_cloud import ENDED_SESSION_STATUSES
        if provider in ENDED_SESSION_STATUSES:
            data = {"session_id": session_id, "reason": "session_already_ended", "provider_status": provider}
        else:
            try:
                transport.release(session_id)
            except Exception as exc:
                with store_lock(self.root):
                    self._request_event("browserbase_release_failed", request_id,
                                        {"session_id": session_id, "error": type(exc).__name__, "reason": reason})
                raise ValueError("Session release failed; it stays alive until its timeout. Retry release.") from None
            data = {"session_id": session_id, "reason": reason}
        with store_lock(self.root):
            self.store.append("browserbase_session_released", request_id, data, event_id="bb-released:" + request_id,
                              at=self.clock().isoformat())
        return {"released": True, "already_released": False, **data}

    def handoff_status(self, request_id):
        """Ledger view of a Browserbase handoff plus (runtime only) provider status and a fresh Live View link."""
        with store_lock(self.root):
            r = self._request(request_id)
            if r["executor"] not in BROWSER_EXECUTORS:
                raise ValueError("Not a browser-executor request")
            ev = self._handoff_events(request_id)
        session_id = (ev["session"] or {}).get("session_id")
        state = ("released" if ev["released"] else "active" if ev["handoff"] else
                 "unaccounted_session" if session_id and not (ev["evidence"] or {}).get("released") else "none")
        out = {"request_id": request_id, "request_status": r["status"], "handoff_state": state,
               "session_id": session_id, "release_failures": len(ev["release_failures"]),
               "review_expires_at": (ev["handoff"] or {}).get("review_expires_at")}
        if state in {"active", "unaccounted_session"} and session_id:
            transport, _ = self._browserbase_runtime(r["executor"])
            try:
                out["provider_status"] = transport.session_status(session_id)
            except Exception as exc:
                out["provider_status"] = f"unavailable:{type(exc).__name__}"
            if out["provider_status"] == "RUNNING" and ev["handoff"]:
                if r["executor"] == "local_browser":
                    out["handoff"] = "local_browser_active"  # the visible window; no Live View
                else:
                    out.update(self._live_view(session_id, transport, ev["handoff"]["review_expires_at"]))
        return out

    def release_handoff(self, request_id, *, session_id, claim_token):
        """Explicitly end a Browserbase session after review. Bound to the request's own journaled
        session; never changes the request's recorded result and never marks anything submitted."""
        with store_lock(self.root):
            r = self._request(request_id)
            self._check_token(r, claim_token)
            if r["executor"] not in BROWSER_EXECUTORS:
                raise ValueError("Not a browser-executor request")
            journaled = (self._handoff_events(request_id)["session"] or {}).get("session_id")
            if not journaled or session_id != journaled:
                raise ValueError("Session does not belong to this request")
        transport, _ = self._browserbase_runtime(r["executor"])
        transport.preflight()  # credentials: refuse before any provider call or journal entry
        result = self._release_session(request_id, session_id, transport, reason="explicit_release")
        return {"request_id": request_id, "session_id": session_id, **result,
                "request_status": self._request(request_id)["status"]}

    CLOSABLE_STATUSES = frozenset({"needs_human_input", "ready_for_human_review", "outcome_unknown"})

    def close_not_submitted(self, request_id, *, reviewer, confirmed_not_submitted, reason):
        """A named human confirms that a stop-only attempt ended WITHOUT any application being
        submitted (including during any human handoff). The confirmation is the authority: it is
        never inferred from missing evidence. Records `closed_not_submitted` -- terminal,
        non-blocking for the application, not a receipt, never APPLIED. It contacts no provider
        and releases nothing: an active or unaccounted Browserbase session must be released first.
        Repeating the identical confirmation returns the existing record; anything else fails."""
        from jobintel.submission.stop_only import SUBMISSION_EVIDENCE
        if confirmed_not_submitted is not True:
            raise ValueError("Closing requires the explicit --confirmed-not-submitted human confirmation")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("Closing requires a named reviewer")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("Closing requires a human reason (at most 2000 characters)")
        reviewer, reason = reviewer.strip(), reason.strip()
        with store_lock(self.root):
            r = self._request(request_id)
            events = [e for e in self.store.events() if e["entity_id"] == request_id]
            if r["status"] == "closed_not_submitted":
                existing = next(e["data"] for e in events if e["kind"] == "closed_not_submitted")
                if (existing["reviewer"], existing["reason"]) != (reviewer, reason):
                    raise ValueError("Request is already closed by a different confirmation; it cannot be re-closed")
                return existing
            if r["executor"] not in STOP_ONLY_EXECUTORS:
                raise ValueError("Only stop-only executor requests can be closed without submission")
            if r["status"] not in self.CLOSABLE_STATUSES:
                raise ValueError(f"A request in status {r['status']} cannot be closed without submission")
            if r.get("receipt_id") or any(e["data"].get("request_id") == request_id for e in self.store.receipts()):
                raise ValueError("A submission receipt exists for this request")
            kinds = {e["kind"] for e in events}
            if kinds & {"completed", "reconciled", "receipt_verified"}:
                raise ValueError("This request reported or recorded a submission; it cannot be closed as not submitted")
            result = r.get("result") or {}
            if (result.get("submitted_at") or result.get("outcome") == "reported_submitted"
                    or any(e.get("kind") in SUBMISSION_EVIDENCE for e in result.get("evidence") or [])):
                raise ValueError("The recorded result indicates a possible submission")
            for e in events:
                if e["kind"] in {"browserbase_result", "airtap_result"} and (
                        e["data"].get("final_submit_clicked") is not False
                        or any(x.get("kind") in SUBMISSION_EVIDENCE for x in e["data"].get("evidence") or [])):
                    raise ValueError("An imported report indicates a final-submit click or submission evidence")
            from jobintel.storage.application_store import ApplicationStore
            app = next((a for a in ApplicationStore(self.root).read_applications() if a.id == r["application_id"]), None)
            if app is not None and app.is_submitted:
                raise ValueError("The application is already marked submitted/APPLIED")
            if r["executor"] in BROWSER_EXECUTORS:
                ev = self._handoff_events(request_id)
                session = (ev["session"] or {}).get("session_id")
                released = ev["released"] is not None or (ev["evidence"] is not None and ev["evidence"].get("released") is True
                                                          and ev["handoff"] is None)
                if session and not released:
                    raise ValueError("The Browserbase session is active or unaccounted; release it explicitly "
                                     "with submission-handoff-release before closing")
            data = {"request_id": request_id, "application_id": r["application_id"], "approval_id": r["approval_id"],
                    "package_hash": r["package_hash"], "executor": r["executor"], "reviewer": reviewer,
                    "confirmed_not_submitted": True, "reason": reason, "closed_at": self.clock().isoformat(),
                    "previous_status": r["status"], "new_status": "closed_not_submitted",
                    "receipt": False, "application_marked_submitted": False}
            self.store.append("closed_not_submitted", request_id, data, event_id="closed-not-submitted:" + request_id,
                              at=data["closed_at"])
            return data

    def _refuse_if_closed(self, request):
        if request["status"] == "closed_not_submitted":
            raise ValueError("Request was closed by a human as not submitted; it accepts no further results")

    @locked
    def complete_browserbase(self, request_id, *, claim_token, value, identity_state=None):
        """Strict stop-only import of a Browserbase report; never manufactures a submitted receipt.
        `identity_state` is what this process observed from the runner (journaled with the evidence)."""
        from jobintel.submission.stop_only import import_result
        from jobintel.models.submission import fingerprint
        r = self._request(request_id)
        self._check_token(r, claim_token)
        self._refuse_if_closed(r)
        if r["executor"] not in BROWSER_EXECUTORS or not r.get("handoff_started"):
            raise ValueError("Not a started browser-executor handoff")
        if len(canonical(value).encode()) > 1_000_000:
            raise ValueError("Browserbase result too large")
        package = self._valid_package(r["approval_id"])
        previous = [e["data"] for e in self.store.events()
                    if e["kind"] == "browserbase_result" and e["entity_id"] == request_id]
        try:
            if previous and value.get("task_id") != previous[0]["task_id"]:
                raise ValueError("Task identity cannot change or retry")
            result = import_result(value, package=package, request_id=request_id, approval_id=r["approval_id"],
                                   executor=r["executor"],
                                   display_name="Local browser" if r["executor"] == "local_browser" else "Browserbase",
                                   identity_state=identity_state, deterministic_questions=True)
        except (ValueError, TypeError, KeyError, AttributeError):
            if r["status"] not in {"submitted", "verified", "failed", "cancelled"}:
                self._request_event("outcome_unknown", request_id, {"reason": "invalid_browserbase_report"})
            raise ValueError("Invalid Browserbase report; investigate, never retry automatically") from None
        self.store.append("browserbase_result", request_id, value, event_id="browserbase-result:" + fingerprint(value),
                          at=self.clock().isoformat())
        return self._complete(request_id, claim_token=claim_token, result=result)

    @locked
    def complete(self, request_id, *, claim_token, result: ExecutionResult):
        if self._request(request_id)["executor"] in STOP_ONLY_EXECUTORS:
            raise ValueError("Use strict AirTap result import; generic completion is forbidden")
        return self._complete(request_id, claim_token=claim_token, result=result)

    def _complete(self, request_id, *, claim_token, result: ExecutionResult):
        r = self._request(request_id)
        self._check_token(r, claim_token)
        self._refuse_if_closed(r)
        if not r.get("handoff_started"):
            raise ValueError("Record executor handoff before completing an attempt")
        # Late evidence is allowed for the SAME attempt. This is not a retry.
        if r["status"] not in {"executing", "needs_human_input", "ready_for_human_review", "outcome_unknown", "pending_verification"}:
            if canonical(r.get("result")) == canonical(asdict(result)):
                return r
            raise ValueError("Request cannot accept an execution result")
        p = ApprovedSubmissionPackage.from_dict(self.store.approvals()[r["approval_id"]]["package"])
        if (result.request_id, result.package_hash, result.job_id, result.cv_artifact_id, result.application_url, result.executor) != (
            request_id, r["package_hash"], p.job_id, p.cv_artifact_id, p.application_url, r["executor"]):
            raise ValueError("Execution result does not match the approved package")
        # Validate the typed contract even for callers that instantiate a dataclass directly.
        ExecutionResult.from_dict(asdict(result))
        if result.submitted_at and not instant(r["claimed_at"]) <= instant(result.submitted_at) <= self.clock():
            raise ValueError("Submitted timestamp must lie within this attempt")
        kind = {"reported_submitted": "completed", "needs_human_input": "needs_human_input",
                "outcome_unknown": "outcome_unknown", "failed": "failed",
                "ready_for_human_review": "ready_for_human_review"}[result.outcome]
        if result.unanswered_fields:
            kind = "needs_human_input"
        if r["status"] in {"outcome_unknown", "pending_verification"} and result.outcome == "failed":
            kind = "outcome_unknown"
        if canonical(r.get("result")) == canonical(asdict(result)):
            return r
        self._request_event(kind, request_id, {"result": asdict(result)})
        return self._request(request_id)

    @locked
    def verify(self, request_id, *, reviewer, confirmed, strategies=None):
        r = self._request(request_id)
        if r["status"] in {"verified", "submitted"}:
            return next(e for e in self.store.receipts() if e["event_id"] == r["receipt_id"])["data"]
        if not confirmed or not reviewer.strip():
            raise ValueError("Receipt verification requires explicit human confirmation")
        if r["status"] not in {"pending_verification", "outcome_unknown"} or not r.get("result"):
            raise ValueError("No reported execution result to verify")
        package = self._valid_package(r["approval_id"])
        result = ExecutionResult.from_dict(r["result"])
        enough = (result.outcome == "reported_submitted" and not result.unanswered_fields and result.submitted_at
                  and sufficient_evidence(result.evidence, package=package, root=self.root, strategies=strategies))
        if not enough:
            self._request_event("outcome_unknown", request_id, {"reason": "insufficient_submission_evidence"})
            return {"request_id": request_id, "status": "outcome_unknown"}
        receipt_id = "receipt:" + request_id
        receipt = {"receipt_id": receipt_id, "request_id": request_id, "approval_id": r["approval_id"],
                   "application_id": package.application_id, "job_id": package.job_id,
                   "package_hash": package.package_hash, "cv_artifact_id": package.cv_artifact_id,
                   "cv_pdf_sha256": package.cv_pdf_sha256, "application_url": package.application_url,
                   "executor": result.executor, "submitted": True, "submitted_at": result.submitted_at,
                   "evidence": [asdict(e) for e in result.evidence], "verified_by": reviewer,
                   "verified_at": self.clock().isoformat(), "verification_strategy": "human_attested_v1"}
        # Durable verified receipt is the recovery authority. Verification itself never
        # updates ApplicationStore; reconcile has that one responsibility.
        self.store.append("receipt_verified", receipt_id, receipt, event_id=receipt_id, receipt=True, at=self.clock().isoformat())
        return receipt

    @locked
    def list(self):
        requests = self.store.requests()
        finished_approvals = {r["approval_id"] for r in requests.values() if r["status"] in {"verified", "submitted"}}
        for a in self.store.approvals().values():
            if a["status"] in {"revoked", "expired"} and a["id"] not in finished_approvals:
                self._invalidate(a["id"], a["status"], "approval_no_longer_active")
            if a["status"] == "approved" and a["id"] not in finished_approvals:
                try:
                    self._valid_package(a["id"])
                except ValueError:
                    pass
        for r in self.store.requests().values():
            if r["status"] == "executing" and instant(r["lease_until"]) <= self.clock():
                self._request_event("outcome_unknown", r["id"], {"reason": "claim_expired"})
        return {"approvals": list(self.store.approvals().values()), "requests": list(self.store.requests().values())}

    @locked
    def reconcile(self):
        from jobintel.storage.application_store import ApplicationStore
        self.list()
        results = []
        for e in self.store.receipts():
            receipt = e["data"]
            try:
                app = ApplicationStore(self.root)._apply_verified_receipt(e["event_id"])
            except ValueError as exc:
                results.append({"request_id": receipt["request_id"], "status": "conflict", "reason": str(exc)})
                continue
            self.store.append("reconciled", receipt["request_id"], {"receipt_id": e["event_id"]},
                              event_id="reconcile:" + e["event_id"], at=self.clock().isoformat())
            results.append({"request_id": receipt["request_id"], "application_id": app.id, "status": "submitted"})
        return results


    @locked
    def check_airtap(self, request_id, *, claim_token):
        """Revalidate a prepared bundle immediately before the supervised runtime start."""
        import hashlib
        r = self._request(request_id)
        self._check_token(r, claim_token)
        if r["executor"] != "airtap" or r["status"] != "executing" or not r.get("handoff_started"):
            raise ValueError("AirTap request is not executable; do not restart")
        if instant(r["lease_until"]) <= self.clock():
            self._request_event("outcome_unknown", request_id, {"reason": "claim_expired"})
            raise ValueError("Claim expired; do not restart")
        self._valid_package(r["approval_id"])
        bundle = next((e["data"] for e in self.store.events()
                       if e["event_id"] == "bundle:" + request_id), None)
        if bundle is None:
            raise ValueError("No durable bundle manifest; investigate interrupted preparation")
        path = Path(bundle["bundle_path"])
        if path.is_symlink() or not path.resolve().is_relative_to(self.root / "submissions/airtap"):
            raise ValueError("Invalid bundle path")
        for name, expected in bundle["manifest"].items():
            target = path / name
            if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                self._request_event("outcome_unknown", request_id, {"reason": "bundle_modified"})
                raise ValueError("Bundle changed; do not execute")
        from jobintel.submission.airtap import CV_UPLOAD_MODE, LIVE_BLOCKERS, MODE
        return {"request_id": request_id, "execution_mode": MODE, "bundle_path": str(path),
                "status": "validated_for_supervised_handoff", "runtime_started": False,
                "live_execution": "blocked" if LIVE_BLOCKERS else "supervised_only",
                "live_execution_blockers": list(LIVE_BLOCKERS), "cv_upload": CV_UPLOAD_MODE,
                "final_submit_allowed": False}

    @locked
    def complete_airtap(self, request_id, *, claim_token, value):
        from jobintel.submission.airtap import import_result
        from jobintel.models.submission import fingerprint
        r = self._request(request_id)
        self._check_token(r, claim_token)
        self._refuse_if_closed(r)
        if r["executor"] != "airtap" or not r.get("handoff_started"):
            raise ValueError("Not a started AirTap handoff")
        if len(canonical(value).encode()) > 1_000_000:
            raise ValueError("AirTap result too large")
        package = self._valid_package(r["approval_id"])
        previous = [e["data"] for e in self.store.events()
                    if e["kind"] == "airtap_result" and e["entity_id"] == request_id]
        try:
            if previous and value.get("task_id") != previous[0]["task_id"]:
                raise ValueError("Task identity cannot change or retry")
            result = import_result(value, package=package, request_id=request_id, approval_id=r["approval_id"])
        except (ValueError, TypeError, KeyError, AttributeError):
            if r["status"] not in {"submitted", "verified", "failed", "cancelled"}:
                self._request_event("outcome_unknown", request_id, {"reason": "invalid_airtap_report"})
            raise ValueError("Invalid AirTap report; investigate, never retry automatically") from None
        self.store.append("airtap_result", request_id, value, event_id="airtap-result:" + fingerprint(value),
                          at=self.clock().isoformat())
        return self._complete(request_id, claim_token=claim_token, result=result)
