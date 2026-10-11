"""Local headed-browser transport: the shared stop-only runner in a Chromium on THIS machine.

An alternative to the Browserbase cloud transport with the same transport interface
(preflight / run / session_status / live_view / release), so the submission service drives it
through the same sealed bundle, the same `browserbase_runner.run` (exact host, GraphQL read
policy, ApiSetFormValue fill windows, explicit non-fatal telemetry, identity checks, the CV
left human_required on a real host unless the request carries an approved CV-upload policy,
never a final Submit) and the same handoff/release/close ledger.
No Browserbase API, key, quota or network call is involved.

The browser: the installed Google Chrome binary (the same "chrome" channel the offline suite
uses), or Playwright's own Chromium if Chrome is absent, started as a separate, detached process
with a FRESH, empty user-data-dir under the ignored ephemeral runtime directory (data/runtime/...).
The user's own Chrome profile is never used: nothing is imported from any personal browser
profile -- no passwords, cookies, extensions or history. Remote debugging listens on 127.0.0.1
only. The runner connects over CDP exactly as it does to a Browserbase session.

Human handoff: the visible browser window IS the handoff. For a reviewable result the runner's
route handler is removed, its in-page submit guard is released (per-run secret; the page itself
cannot do this), and only then is the CDP connection dropped. The window stays open, exactly as
the runner left it, and behaves as an ordinary browser: the human can edit, upload the CV and
click Submit without reloading. Nothing is clicked on their behalf. Any other outcome (or any exception) closes the browser and deletes its profile.
`release` later ends a handed-off browser (only one this transport started, verified by its
exact profile directory) and deletes its profile.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from urllib.parse import urlsplit
from uuid import uuid4

from jobintel.submission import browser_session
from jobintel.submission.browserbase_cloud import BrowserbaseSpikeError

NAME = "local_browser"
CHROME_BINARY = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
LAUNCH_TIMEOUT_SECONDS = 30
# Fresh, isolated, quiet profile. No sync, extensions, default apps, first-run UI or background
# service traffic; macOS keychain is not touched (mock keychain).
CHROMIUM_ARGS = ("--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check", "--disable-sync",
                 "--disable-extensions", "--disable-default-apps", "--disable-component-update",
                 "--disable-background-networking", "--use-mock-keychain", "--disable-features=Translate")


class LocalBrowserError(BrowserbaseSpikeError):
    """Fixed-text local transport failure carrying only safe stage/category fields."""


@dataclass
class LocalEvidence:
    """What the local transport keeps. No CDP endpoint, no page content."""
    session_id: str = ""
    allowed_domains: list = field(default_factory=list)
    replay_url: str = ""  # no recording exists
    final_url: str = ""
    screenshot_sha256: str = ""
    screenshot_path: str = ""
    released: bool = False
    release_error: str = ""
    handoff_kept: bool = False  # browser window deliberately left open for the human
    guard_detached: bool = False
    page_guard_released: bool = False  # handoff: in-page submit guard removed so the human can submit
    live: dict = field(default_factory=dict)
    browser: str = NAME


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_line(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=10).stdout
    except Exception:
        return ""


class LocalBrowserTransport:
    """Runs the shared runner in a local Chromium. `runtime_dir` is the ephemeral runtime tree
    (the service uses data/runtime/local_browser: ignored by git, outside the application store,
    never backed up). `legacy_runtime_dirs` are only SEARCHED for existing sessions (profiles
    created before the move), so they can still be checked and released. `headless` exists for
    the offline test suite only; the operational default is a visible (headed) window.

    Stale profiles are explicit: a handed-off profile (and its localhost debug port) stays until
    `release`; a browser that exited on its own keeps its profile until `release`; nothing is
    swept automatically."""

    def __init__(self, *, runtime_dir, executable=None, headless=False, serve=None, playwright_factory=None,
                 screenshot_dir=None, launch_timeout=LAUNCH_TIMEOUT_SECONDS, extra_args=(), legacy_runtime_dirs=()):
        self.runtime_dir = Path(runtime_dir)
        self.legacy_runtime_dirs = tuple(Path(d) for d in legacy_runtime_dirs)
        self.extra_args = tuple(extra_args)  # tests only (e.g. a NOTFOUND host resolver)
        self.executable, self.headless, self.serve = executable, headless, serve
        self.playwright_factory, self.screenshot_dir, self.launch_timeout = playwright_factory, screenshot_dir, launch_timeout

    def __repr__(self) -> str:
        return f"LocalBrowserTransport(headless={self.headless})"

    # --- browser process ----------------------------------------------------------------------------

    def _factory(self):
        if self.playwright_factory is None:
            from playwright.sync_api import sync_playwright  # transport-only dependency
            return sync_playwright
        return self.playwright_factory

    def _executable(self) -> Path:
        if self.executable is None:
            if CHROME_BINARY.is_file():
                self.executable = CHROME_BINARY  # the binary only; always with its own fresh profile
            else:
                with self._factory()() as p:
                    self.executable = p.chromium.executable_path
        return Path(self.executable)

    def preflight(self) -> None:
        """No credentials needed; fail before any process starts if no Chromium is installed."""
        try:
            path = self._executable()
        except Exception:
            raise ValueError("No local Chrome/Chromium: install Google Chrome or "
                             "`python3 -m playwright install chromium`") from None
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError("No local Chrome/Chromium: install Google Chrome or "
                             "`python3 -m playwright install chromium`")

    def _record_path(self, session_id: str, runtime_dir=None) -> Path:
        if not (session_id.startswith("local-") and len(session_id) == 38 and session_id[6:].isalnum()):
            raise ValueError("Not a local browser session id")
        return Path(runtime_dir or self.runtime_dir) / "sessions" / f"{session_id}.json"

    def _find_record(self, session_id: str):
        """(record path, record) from the runtime dir or a legacy dir; (None, None) if absent. A
        record is trusted only if its profile is exactly <that dir>/profiles/<session id>."""
        for directory in (self.runtime_dir, *self.legacy_runtime_dirs):
            path = self._record_path(session_id, directory)
            if path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
                expected = (Path(directory) / "profiles" / session_id).resolve()
                if Path(record["profile_dir"]).resolve() != expected or record.get("session_id") != session_id:
                    raise ValueError("Local browser session record does not match its runtime directory")
                return path, record
        return None, None

    def _launch(self):
        session_id = f"local-{uuid4().hex}"
        self.runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        for sub in ("profiles", "sessions"):
            (self.runtime_dir / sub).mkdir(mode=0o700, exist_ok=True)
        profile = self.runtime_dir / "profiles" / session_id
        profile.mkdir(mode=0o700)
        args = [str(self._executable()), *CHROMIUM_ARGS, *self.extra_args, f"--user-data-dir={profile}"]
        if self.headless:
            args.append("--headless=new")
        args.append("about:blank")
        # Own session/process group: it outlives this process (the handoff) and ignores the
        # terminal's Ctrl-C. No output is captured (an unread pipe could stall the browser).
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        port_file, deadline = profile / "DevToolsActivePort", time.monotonic() + self.launch_timeout
        while time.monotonic() < deadline and proc.poll() is None:
            lines = port_file.read_text().splitlines() if port_file.exists() else []
            if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].startswith("/devtools/browser/"):
                record = {"session_id": session_id, "pid": proc.pid, "profile_dir": str(profile)}
                self._record_path(session_id).write_text(json.dumps(record), encoding="utf-8")
                return session_id, proc, profile, f"ws://127.0.0.1:{int(lines[0])}{lines[1].strip()}"
            time.sleep(0.05)
        self._stop(proc.pid, profile)
        raise LocalBrowserError("Local browser did not start", category="local_browser_launch_failed",
                                stage="local_browser_launch")

    def _stop(self, pid: int, profile: Path) -> None:
        """End one browser this transport started (its process group) and delete its profile."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _alive(pid):
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
                time.sleep(0.05)
            if not _alive(pid):
                break
        shutil.rmtree(profile, ignore_errors=True)

    # --- transport interface -------------------------------------------------------------------------

    def run(self, bundle, *, write_allowlist, on_session, keep_alive=False, review_seconds=0, live_host=None,
            graphql_reads=(), graphql_form_writes=(), nonfatal_blocked_writes=(), cv_upload=None):
        """Launch -> journal -> connect -> runner.run -> handoff (window stays open) or close.
        `cv_upload` (live only): the service-built policy to upload the sealed, approved CV once.
        Exactly once; never retried."""
        live = live_host is not None
        host = urlsplit(bundle.application_url).hostname
        if live and (not keep_alive or live_host != host):
            raise ValueError("A live-host run needs human handoff and exactly its one authorized host")
        if cv_upload is not None and not live:
            raise ValueError("An approved CV upload is only defined for a live-host run")
        evidence = LocalEvidence(allowed_domains=[host])
        if live:
            evidence.live = {"host": live_host, "recording": False, "browser_logging": False,
                             "screenshot": "not_captured"}
        shots = None
        if not live:
            shots = Path(self.screenshot_dir) if self.screenshot_dir else self.runtime_dir / "evidence"
            shots.mkdir(parents=True, exist_ok=True)
        try:
            session_id, proc, profile, endpoint = self._launch()
        except LocalBrowserError:
            raise
        except Exception as exc:
            raise LocalBrowserError("Local browser did not start", category="local_browser_launch_failed",
                                    stage="local_browser_launch", exception_type=type(exc).__name__) from None
        evidence.session_id = session_id
        try:
            on_session(session_id)
            result = browser_session.run_connected(
                endpoint, bundle, evidence, playwright_factory=self._factory(),
                connect_error=lambda exc: LocalBrowserError(
                    "Local browser CDP connection failed", category="local_browser_cdp_connect_failed",
                    stage="local_browser_cdp_connect", exception_type=type(exc).__name__),
                serve=self.serve, write_allowlist=write_allowlist, keep_alive=keep_alive, live_host=live_host,
                graphql_reads=graphql_reads, graphql_form_writes=graphql_form_writes,
                nonfatal_blocked_writes=nonfatal_blocked_writes, screenshot_dir=shots, release_guard_on_handoff=True,
                cv_upload=cv_upload)
        finally:
            if not evidence.handoff_kept:
                self._stop(proc.pid, profile)
                evidence.released = True
                self._record_path(session_id).unlink(missing_ok=True)
        return result, asdict(evidence)

    def session_status(self, session_id: str) -> str:
        """RUNNING only for a live browser this transport started (pid + exact profile dir)."""
        _, record = self._find_record(session_id)
        if record is None:
            return "COMPLETED"
        running = _alive(record["pid"]) and f"--user-data-dir={record['profile_dir']}" in _command_line(record["pid"])
        return "RUNNING" if running else "COMPLETED"

    def live_view(self, session_id: str, *, expires_in: int) -> dict:
        raise LocalBrowserError("A local browser has no Live View: use the visible window",
                                category="executor_internal_error", stage="local_browser_run")

    def release(self, session_id: str) -> None:
        """Close a handed-off local browser (verified as ours) and delete its profile."""
        path, record = self._find_record(session_id)
        if record is None:
            return
        profile = Path(record["profile_dir"])
        if self.session_status(session_id) == "RUNNING":
            self._stop(record["pid"], profile)
        else:
            shutil.rmtree(profile, ignore_errors=True)
        path.unlink(missing_ok=True)
