"""Convenience copy of a CV artifact's PDF for human review (e.g. in ~/Downloads).

The registered artifact in the CV store stays the ONLY source of truth: nothing here is read back,
attached, approved or uploaded. The export copies the stored PDF's exact bytes (never regenerates
or edits it), verifies the SHA-256 of both sides, and records each export in the store's own
manifest (cv_exports/exports.jsonl).

Collisions: an existing file at the target name is replaced only when it is the exact same
intended export -- the same bytes already, or this tool's own unmodified earlier export for the
same application/job (per the manifest). Anything else (a file you saved or edited, another
job's CV, a symlink) is left untouched and the copy goes to `<name>_<artifact id>.pdf`; if that
name is also taken by different content, the export is refused.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from jobintel.matching.cv_generation import resolve_cv_pdf
from jobintel.storage.cv_artifact_store import CVArtifactStore

EXPORT_DIR_ENV = "JOBINTEL_CV_EXPORT_DIR"
MAX_SEGMENT = 40


@dataclass(frozen=True)
class CVReviewExport:
    artifact_id: str
    source_path: str
    export_path: str
    source_sha256: str
    export_sha256: str
    action: str  # written | replaced | unchanged

    @property
    def matches(self) -> bool:
        return self.source_sha256 == self.export_sha256


def default_export_dir() -> Path:
    return Path(os.environ.get(EXPORT_DIR_ENV) or Path.home() / "Downloads").expanduser()


def _segment(text: str | None) -> str:
    """ASCII letters/digits/hyphens only; words joined by '-'; lower-case-only input title-cased."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    words = re.findall(r"[A-Za-z0-9]+", ascii_text)
    if ascii_text == ascii_text.lower():
        words = [w[:1].upper() + w[1:] for w in words]
    return "-".join(words)[:MAX_SEGMENT].strip("-")


def review_filename(full_name: str, company: str | None = None, role: str | None = None) -> str:
    """`<First_Last>_CV.pdf`, or `<First_Last>_<Company>_<Role>_CV.pdf` for a job-specific CV."""
    name = "_".join(filter(None, (_segment(part) for part in (full_name or "").split())))
    parts = [name or "Candidate", *filter(None, (_segment(company), _segment(role))), "CV"]
    return "_".join(parts) + ".pdf"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(store_root: Path) -> Path:
    return Path(store_root) / "cv_exports" / "exports.jsonl"


def _last_export(store_root: Path, target: Path) -> dict | None:
    path = _manifest(store_root)
    if not path.is_file():
        return None
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return next((e for e in reversed(entries) if e.get("export_path") == str(target)), None)


def _may_replace(store_root: Path, target: Path, source_sha: str, slot: dict) -> str | None:
    """'unchanged' / 'replaced' when the existing target is this same intended export, else None."""
    if target.is_symlink() or not target.is_file():
        return None
    existing = _sha256(target)
    if existing == source_sha:
        return "unchanged"
    last = _last_export(store_root, target)
    if last and last.get("export_sha256") == existing and all(last.get(k) == v for k, v in slot.items()):
        return "replaced"  # our own unmodified earlier export of the same application/job
    return None


def _atomic_copy(data: bytes, target: Path) -> None:
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".cv-export-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def export_cv_for_review(store_root: Path | str, artifact_id: str, filename: str, *,
                         export_dir: Path | str | None = None, application_id: str | None = None) -> CVReviewExport:
    """Copy the registered artifact's PDF byte-for-byte to `export_dir/filename` and verify it."""
    store_root = Path(store_root)
    if Path(filename).name != filename or not filename.endswith(".pdf") or filename.startswith("."):
        raise ValueError("Export file name must be a plain .pdf name")
    resolved = resolve_cv_pdf(CVArtifactStore(store_root), artifact_id)
    if resolved is None:
        raise ValueError(f"No registered CV artifact {artifact_id} with its PDF in the store")
    source, artifact = resolved
    data = source.read_bytes()
    source_sha = hashlib.sha256(data).hexdigest()
    if not artifact.pdf_sha256 or source_sha != artifact.pdf_sha256:
        raise ValueError(f"CV artifact {artifact_id} PDF does not match its recorded SHA-256; not exported")

    directory = Path(export_dir) if export_dir is not None else default_export_dir()
    directory.mkdir(parents=True, exist_ok=True)
    slot = {"application_id": application_id or artifact.application_id, "job_id": artifact.job_id, "filename": filename}
    target = directory / filename
    action = "written" if not (target.exists() or target.is_symlink()) else _may_replace(store_root, target, source_sha, slot)
    if action is None:
        target = directory / f"{Path(filename).stem}_{artifact.id}.pdf"
        action = "written" if not (target.exists() or target.is_symlink()) else _may_replace(store_root, target, source_sha, slot)
        if action is None:
            raise ValueError(f"{target.name} already exists with different content; not overwritten")
    if action != "unchanged":
        _atomic_copy(data, target)
    export_sha = _sha256(target)
    if export_sha != source_sha:
        raise ValueError(f"Exported copy {target.name} does not match the artifact's SHA-256")

    result = CVReviewExport(artifact.id, str(source), str(target), source_sha, export_sha, action)
    manifest = _manifest(store_root)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**asdict(result), **slot, "convenience_copy_only": True,
                                 "exported_at": datetime.now(timezone.utc).isoformat()}, sort_keys=True) + "\n")
    return result
