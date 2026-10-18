"""Source-code corroboration for a project's own repository, pinned to one commit.

The README cache (github_sync) is the first corroboration source; a library the project really uses
is often absent from its README. This records, for named terms only, the VERBATIM lines that show the
project directly uses that library:

* an ``import`` statement naming it (Python ``import x`` / ``from x... import``, JS/TS ``import ... from
  'x'`` / ``require('x')``), or
* a dependency entry naming it in a manifest (pyproject.toml, requirements*.txt, setup.cfg, package.json).

Each term is classified (strongest evidence first):

* ``directly_implemented`` -- imported by the project's own (non-test) source code;
* ``tested``               -- imported by the project's tests;
* ``dependency_declared``  -- named as a dependency in a manifest;
* ``documented_only``      -- mentioned only in the README / docs (recorded, NEVER corroborates);
* ``absent``               -- not found.

Only the first three corroborate an implementation claim. Comments, docstrings, prose and
documentation never count. The repository is read from a fresh
shallow clone (or an explicit local checkout whose origin is that exact repository); no repository
code is ever executed. The record is keyed by the exact repository full name and keeps the commit SHA,
so a claim is corroborated only by the project's own code at a known revision -- never fuzzily.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import tempfile

CACHE_NAME = "github_code_evidence.json"
_SKIP_DIRS = {".git", "node_modules", "dist", "build", ".venv", "venv", "__pycache__", "docs", "site-packages"}
_CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_MANIFESTS = re.compile(r"^(?:pyproject\.toml|setup\.cfg|package\.json|requirements[\w.-]*\.txt)$", re.I)
MAX_EXCERPTS = 3  # per term and evidence kind
_DOC_SUFFIXES = {".md", ".rst", ".txt"}
_KIND_STATUS = (("import", "directly_implemented"), ("test_import", "tested"), ("manifest", "dependency_declared"),
                ("documentation", "documented_only"))
CORROBORATING_KINDS = frozenset({"import", "test_import", "manifest"})


def _is_test_path(rel: Path) -> bool:
    name = rel.name.casefold()
    return (any(part.casefold() in {"test", "tests", "__tests__", "spec"} for part in rel.parts[:-1])
            or name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".test.js",
                                                          ".spec.ts", ".spec.tsx", ".spec.js")))


def classify(found_for_term: list[dict]) -> dict:
    """{"status": strongest, "evidence": [every status present]} for one term's excerpts."""
    kinds = {item["kind"] for item in found_for_term}
    present = [status for kind, status in _KIND_STATUS if kind in kinds]
    return {"status": present[0] if present else "absent", "evidence": present or ["absent"]}


def cache_path(root: Path | str) -> Path:
    return Path(root) / "profile" / CACHE_NAME


def _term(term: str) -> re.Pattern:
    return re.compile(rf"(?<![\w+#.-]){re.escape(term)}(?![\w+#])", re.I)


def _import_line(line: str, term: re.Pattern, suffix: str) -> bool:
    stripped = line.strip()
    if suffix == ".py":
        match = re.match(r"(?:from\s+([\w.]+)\s+import\b|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))", stripped)
        modules = (match.group(1) or match.group(2) or "") if match else ""
        return any(term.fullmatch(m.strip().split(".")[0]) for m in modules.split(",") if m.strip())
    if stripped.startswith(("//", "/*", "*")) or not re.match(r"(?:import|export|const|let|var)\b", stripped):
        return False  # comments and prose never count; only an import/export/require statement
    match = re.search(r"""(?:\bfrom\s+|\brequire\s*\(\s*|^import\s+)['"](@?[\w./-]+)['"]""", stripped)
    return bool(match and term.fullmatch(match.group(1).lstrip("@").split("/")[0]))


def _manifest_line(line: str, term: re.Pattern, name: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "//", ";")):
        return False
    if name.casefold() == "package.json":
        match = re.match(r'"(@?[\w./-]+)"\s*:\s*"', stripped)
        return bool(match and term.fullmatch(match.group(1).lstrip("@").split("/")[0]))
    if re.match(r"(?:name|version|description|readme|license|requires-python|authors?|keywords|urls?)\s*=", stripped, re.I):
        return False  # project metadata, not a dependency
    # requirement specifiers: a bare name, or a quoted entry in a TOML/cfg dependency list
    for spec in re.findall(r"""["']([\w.-]+(?:\[[^\]]*\])?\s*(?:[<>=!~;][^"']*)?)["']""", stripped) or [stripped]:
        name_part = re.match(r"[\w.-]+", spec.strip())
        if name_part and term.fullmatch(name_part.group(0)):
            return True
    return False


def scan_checkout(checkout: Path | str, terms) -> dict[str, list[dict]]:
    """{term: [{"path", "line", "kind", "text"}]} in path order; kind is import (non-test source),
    test_import, manifest or documentation (README/docs mention; recorded, never corroborating)."""
    checkout = Path(checkout)
    found: dict[str, list[dict]] = {term: [] for term in terms}
    patterns = {term: _term(term) for term in terms}
    for path in sorted(p for p in checkout.rglob("*") if p.is_file()):
        rel = path.relative_to(checkout)
        if any(part.startswith(".") or (part in _SKIP_DIRS and part != "docs") for part in rel.parts[:-1]):
            continue
        manifest = bool(_MANIFESTS.match(path.name))
        documentation = not manifest and path.suffix.casefold() in _DOC_SUFFIXES
        in_docs = "docs" in rel.parts[:-1]
        if not (manifest or documentation) and (path.suffix not in _CODE_SUFFIXES or in_docs):
            continue
        test_file = _is_test_path(rel)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, 1):
            for term, pattern in patterns.items():
                if not pattern.search(line):
                    continue
                if manifest and _manifest_line(line, pattern, path.name):
                    kind = "manifest"
                elif documentation:
                    kind = "documentation"
                elif not manifest and _import_line(line, pattern, path.suffix):
                    kind = "test_import" if test_file else "import"
                else:
                    continue
                if sum(1 for item in found[term] if item["kind"] == kind) >= MAX_EXCERPTS:
                    continue
                found[term].append({"path": rel.as_posix(), "line": number, "kind": kind, "text": line.strip()})
    return found


def _git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}).stdout.strip()


def _origin_matches(checkout: Path, full_name: str) -> bool:
    url = _git("remote", "get-url", "origin", cwd=checkout)
    normalised = re.sub(r"\.git$", "", url.rstrip("/"))
    return bool(re.search(rf"github\.com[:/]{re.escape(full_name)}$", normalised, re.I))


def collect(root: Path | str, full_name: str, terms, *, checkout: Path | str | None = None, clock=None) -> dict:
    """Scan the project's own repository for `terms` and record the verbatim evidence lines.

    Without `checkout` a fresh shallow clone of https://github.com/<full_name> is read. An explicit
    `checkout` must have that exact repository as its origin. The record replaces any earlier one for
    the repository (one commit at a time) and is returned."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name or ""):
        raise ValueError(f"Not an exact owner/name repository: {full_name!r}")
    terms = sorted({t.strip() for t in terms if t and t.strip()}, key=str.casefold)
    if not terms:
        raise ValueError("At least one term is required")
    with tempfile.TemporaryDirectory(prefix="jobintel-code-evidence-") as tmp:
        if checkout is None:
            checkout = Path(tmp) / "repo"
            _git("clone", "--quiet", "--depth", "1", "--no-tags", f"https://github.com/{full_name}.git", str(checkout))
        checkout = Path(checkout)
        if not _origin_matches(checkout, full_name):
            raise ValueError(f"Checkout origin is not github.com/{full_name}")
        commit = _git("rev-parse", "HEAD", cwd=checkout)
        found = scan_checkout(checkout, terms)
    record = {"commit": commit, "source_url": f"https://github.com/{full_name}",
              "collected_at": (clock() if clock else datetime.now(timezone.utc)).isoformat(),
              "terms": found, "classification": {term: classify(items) for term, items in found.items()}}
    path = cache_path(root)
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cache[full_name] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def code_evidence_text(root: Path | str, full_name: str | None) -> str | None:
    """The recorded evidence lines of the project's own repository (exact name), or None."""
    if not full_name:
        return None
    try:
        record = json.loads(cache_path(root).read_text(encoding="utf-8")).get(full_name)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    lines = [item["text"] for items in (record.get("terms") or {}).values() for item in items
             if item.get("text") and item.get("kind") in CORROBORATING_KINDS]
    return "\n".join(lines) or None
