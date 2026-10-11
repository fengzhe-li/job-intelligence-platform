"""Explicit, reviewed repeated fields only. No inferred or generated answers."""
from pathlib import Path
from uuid import uuid4
from jobintel.models.submission import AnswerSnapshot, fingerprint
from jobintel.storage.locking import locked
from jobintel.storage.submission_store import SubmissionStore

ALLOWED_FIELDS = frozenset({"name", "email", "phone", "location", "linkedin_url", "github_url"})


class AnswerBank:
    def __init__(self, root):
        self.root = root
        self.store = SubmissionStore(root)

    @locked
    def set(self, field, value, *, reviewed_by, provenance, job_id="*"):
        if field not in ALLOWED_FIELDS:
            raise ValueError("Sensitive or unknown fields require per-application human handling")
        if not all(isinstance(v, str) and v.strip() for v in (value, reviewed_by, provenance, job_id)):
            raise ValueError("Answer requires a value, human reviewer, provenance and scope")
        data = {"field": field, "value": value, "reviewed_by": reviewed_by, "provenance": provenance, "job_id": job_id}
        return self.store.append("answer_recorded", field + ":" + job_id, data, event_id="answer:" + uuid4().hex)

    @locked
    def snapshot(self, profile, job_id):
        entries = {}
        for e in self.store.events():
            if e["kind"] == "answer_recorded":
                d = e["data"]
                entries[(d["field"], d["job_id"])] = e

        auth_snapshots: dict[str, AnswerSnapshot] = {}
        root_path = Path(self.root)
        auth_file = root_path / "profile" / "authoritative_answers.json"
        if not auth_file.exists():
            cand = root_path / "data" / "local" / "profile" / "authoritative_answers.json"
            if cand.exists():
                auth_file = cand

        if auth_file.exists():
            from jobintel.submission.handoff import build_authoritative_snapshots, get_authoritative_answer_bank
            bank = get_authoritative_answer_bank(root=auth_file.parent.parent)
            for snap in build_authoritative_snapshots(bank):
                auth_snapshots[snap.field] = snap

        answers = dict(auth_snapshots)
        for field in sorted(ALLOWED_FIELDS):
            entry = entries.get((field, job_id)) or entries.get((field, "*"))
            if entry:
                answers[field] = AnswerSnapshot(field, entry["data"]["value"], "answer_bank", entry["event_id"])
            elif (value := getattr(profile, field, None)):
                if field not in answers:
                    answers[field] = AnswerSnapshot(field, value, "trusted_profile", fingerprint({"field": field, "value": value}))

        return tuple(answers[k] for k in sorted(answers))

