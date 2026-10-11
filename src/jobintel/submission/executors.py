"""Executors have no store, profile, ranking or generation dependencies."""
from dataclasses import dataclass
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from jobintel.submission.airtap import AirTapHandoff
from jobintel.models.submission import ApprovedSubmissionPackage

# Executors whose attempts must stop before final submit and whose results may
# only enter through a strict stop-only import -- never generic complete(). Every
# executor other than "manual" must be listed here to be enqueueable at all, so
# a new backend cannot silently fall through to manual completion semantics.
STOP_ONLY_EXECUTORS = frozenset({"airtap", "browserbase", "local_browser"})
# Executors that drive the shared stop-only browser runner (browserbase_runner) over CDP: the
# Browserbase cloud transport, or a local headed Chromium on this machine. Same sealed bundle,
# same policies, same handoff/release/close lifecycle; only the browser's owner differs.
BROWSER_EXECUTORS = frozenset({"browserbase", "local_browser"})


@dataclass(frozen=True)
class ManualHandoff:
    request_id: str
    package_hash: str
    package: ApprovedSubmissionPackage
    executor: str = "manual"
    action: str = "Human must submit outside this tool, then record the result and verify evidence. No browser was opened."


class SubmissionExecutor(Protocol):
    name: str

    def submit(self, package: ApprovedSubmissionPackage, *, idempotency_key: str) -> "ManualHandoff | AirTapHandoff": ...


class ManualSubmissionExecutor:
    name = "manual"

    def submit(self, package, *, idempotency_key):
        # Handoff only: never opens URLs, fills forms, claims success or updates stores.
        return ManualHandoff(idempotency_key, package.package_hash, package)
