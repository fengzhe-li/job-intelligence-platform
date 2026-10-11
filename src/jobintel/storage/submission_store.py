"""Append-only approval/request/answer journal and verified receipt journal.

The queue is a read projection, not a second mutable source of truth. Malformed or
truncated journals fail closed; recovery never silently truncates audit history.
"""
from pathlib import Path
import json
import os

from jobintel.models.submission import canonical, fingerprint, utcnow
from jobintel.storage.locking import locked, sync_directory


class SubmissionStore:
    def __init__(self, root="data/local"):
        self.root = Path(root)
        self.directory = self.root / "submissions"

    def _read(self, name):
        path = self.directory / name
        if not path.exists():
            return []
        records, previous, ids = [], "", set()
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise ValueError("Incomplete submission journal; manual recovery required")
        for line in raw.splitlines():
            item = json.loads(line)
            checksum = item.pop("hash")
            if item["previous_hash"] != previous or fingerprint(item) != checksum or item["event_id"] in ids:
                raise ValueError("Invalid submission journal integrity")
            item["hash"] = checksum
            previous = checksum
            ids.add(item["event_id"])
            records.append(item)
        return records

    @locked
    def events(self):
        return self._read("events.jsonl")

    @locked
    def receipts(self):
        return self._read("receipts.jsonl")

    @locked
    def append(self, kind, entity_id, data, *, event_id, at=None, receipt=False):
        name = "receipts.jsonl" if receipt else "events.jsonl"
        records = self._read(name)
        existing = next((r for r in records if r["event_id"] == event_id), None)
        if existing:
            if (existing["kind"], existing["entity_id"], canonical(existing["data"])) != (kind, entity_id, canonical(data)):
                raise ValueError("Conflicting replay of journal event")
            return existing
        item = {"event_id": event_id, "kind": kind, "entity_id": entity_id,
                "at": at or utcnow().isoformat(), "data": data,
                "previous_hash": records[-1]["hash"] if records else ""}
        item["hash"] = fingerprint(item)
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            encoded = (canonical(item) + "\n").encode()
            if os.write(fd, encoded) != len(encoded):
                raise OSError("Incomplete journal append")
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_directory(self.directory)
        return item

    @locked
    def approvals(self):
        approvals = {}
        for e in self.events():
            if e["kind"] == "approved":
                approvals[e["entity_id"]] = {**e["data"], "id": e["entity_id"], "status": "approved"}
            elif e["kind"] in {"revoked", "expired"}:
                approvals[e["entity_id"]]["status"] = e["kind"]
        return approvals

    @locked
    def requests(self):
        requests = {}
        states = {"enqueued": "submission_requested", "claimed": "executing",
                  "completed": "pending_verification", "ready_for_human_review": "ready_for_human_review", "needs_human_input": "needs_human_input",
                  "outcome_unknown": "outcome_unknown", "failed": "failed", "cancelled": "cancelled",
                  "reconciled": "submitted",
                  # Terminal, human-attested: the stop-only attempt ended and a named human confirmed
                  # no application was submitted. Not a receipt; non-blocking for the application.
                  "closed_not_submitted": "closed_not_submitted"}
        for e in self.events():
            if e["kind"] == "enqueued":
                requests[e["entity_id"]] = {**e["data"], "id": e["entity_id"]}
            if e["kind"] == "handoff_started":
                requests[e["entity_id"]]["handoff_started"] = True
            if e["kind"] in states:
                r = requests[e["entity_id"]]
                r.update(e["data"])
                r["status"] = states[e["kind"]]
        for receipt in self.receipts():
            r = requests[receipt["data"]["request_id"]]
            r["receipt_id"] = receipt["event_id"]
            if r["status"] != "submitted":
                r["status"] = "verified"
        return requests

    @locked
    def assert_application_idle(self, application_id):
        blocking = {"submission_requested", "executing", "ready_for_human_review", "needs_human_input", "pending_verification", "outcome_unknown", "verified"}
        if any(r["application_id"] == application_id and r["status"] in blocking for r in self.requests().values()):
            raise ValueError("Application has an active submission; resolve it through submission commands")

    @locked
    def revoke_unqueued(self, application_id, reason):
        for a in self.approvals().values():
            if a["package"]["application_id"] == application_id and a["status"] == "approved":
                self.append("revoked", a["id"], {"reason": reason}, event_id="revoke:" + a["id"])
