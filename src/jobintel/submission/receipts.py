"""Extensible evidence strategies; MVP success always requires human attestation.

Strategy acceptance means evidence is suitable for human verification, not that
arbitrary executor text independently proves an ATS submission.
"""
import hashlib
from pathlib import Path
import re
from typing import Protocol
from urllib.parse import urlsplit

from jobintel.models.submission import ReceiptEvidence

EVIDENCE_KINDS = frozenset({"confirmation_id", "confirmation_page", "confirmation_text",
                          "screenshot_evidence", "application_portal_status", "manual_verified"})


class EvidenceStrategy(Protocol):
    def verify(self, evidence: ReceiptEvidence, *, package, root: Path) -> bool: ...


class HumanEvidenceStrategy:
    def verify(self, evidence, *, package, root):
        if evidence.kind not in EVIDENCE_KINDS or not isinstance(evidence.value, str):
            return False
        value = evidence.value.strip()
        if evidence.kind == "screenshot_evidence":
            directory = (root / "submissions" / "evidence").resolve()
            path = (directory / value).resolve()
            if Path(value).is_absolute() or not path.is_relative_to(directory) or not directory.is_relative_to(root.resolve()):
                return False
            if not path.is_file() or not evidence.sha256:
                return False
            data = path.read_bytes()
            image_header = data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
            return image_header and hashlib.sha256(data).hexdigest() == evidence.sha256
        if evidence.kind == "confirmation_id":
            return len(value) >= 3 and value.casefold() not in {"done", "yes", "true", "success"}
        if evidence.kind in {"confirmation_page", "application_portal_status"}:
            parts = urlsplit(value)
            return (parts.scheme == "https" and parts.hostname == urlsplit(package.application_url).hostname
                    and not parts.username and not parts.password and _positive_confirmation(evidence.detail))
        if evidence.kind == "confirmation_text":
            return _positive_confirmation(value)
        return len(value) >= 20 and value.casefold() not in {"the agent says done."}


def sufficient_evidence(items, *, package, root, strategies=None):
    registry = strategies if strategies is not None else {kind: HumanEvidenceStrategy() for kind in EVIDENCE_KINDS}
    # Unknown/malformed attached evidence is not silently ignored.
    return bool(items) and all(e.kind in registry and registry[e.kind].verify(e, package=package, root=root) for e in items)


def _positive_confirmation(text):
    # Conservative English recognizer for this MVP. Other ATS languages can use
    # independently checked screenshot/manual evidence or a later strategy.
    if re.search(r"\b(not|no|failed|error|pending|unsuccessful)\b", text, re.I):
        return False
    return len(text.strip()) >= 20 and bool(re.search(r"\b(submitted|received|successfully|confirmation)\b", text, re.I))
