"""Subprocess probe for test_browserbase_teardown.py (not a test module).

Runs ONE handoff-mode `run_in_cloud` against a local headless Chrome CDP endpoint (fake
Browserbase API, reserved host served in-process, every DNS name mapped to NOTFOUND) on a page
that never stops POSTing. A short pause after the runner's result lets route events queue up,
as they do over a slow remote link, so teardown races are visible on stderr. Asyncio teardown
noise is only printed at interpreter exit, hence a separate process.

usage: browserbase_teardown_probe.py CHROME_PATH fix|nofix
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

HERE = Path(__file__).parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]

from jobintel.submission import browserbase_runner  # noqa: E402
from jobintel.submission.browserbase_cloud import BrowserbaseCredentials, run_in_cloud  # noqa: E402

HOST = "jobs.example.test"
FIXTURE = (HERE / "fixtures" / "teardown" / "apply.html").read_text()


def serve(route, request):
    if urlsplit(request.url).hostname != HOST:
        return route.abort()
    if request.method == "GET":
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE)
    return route.fulfill(status=204, body="")


def main(chrome: str, mode: str) -> None:
    from test_browserbase_live_host import live_bundle
    tmp = Path(tempfile.mkdtemp())
    proc = subprocess.Popen([chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={tmp / 'p'}",
                             "--host-resolver-rules=MAP * ~NOTFOUND", "--no-first-run", "--disable-extensions",
                             "about:blank"], stderr=subprocess.PIPE, text=True)
    try:
        url = next(m.group(1) for line in proc.stderr if (m := re.search(r"DevTools listening on (ws://\S+)", line)))
        api = lambda u, h, b, t: ({"id": "probe-session", "status": "RUNNING", "connectUrl": url}  # noqa: E731
                                  if u.endswith("/sessions") else {})
        original = browserbase_runner.detach_guard

        def delayed(page):
            time.sleep(0.5)  # route events pile up while this process is busy elsewhere
            return original(page) if mode == "fix" else False

        browserbase_runner.detach_guard = delayed
        bundle = live_bundle(tmp, "/teardown/apply.html", "?mode=beacon")
        result, evidence = run_in_cloud(bundle, creds=BrowserbaseCredentials("probe-key"), serve=serve,
                                        allowed_domains=[HOST], screenshot_dir=tmp, post=api, keep_alive=True,
                                        review_seconds=600, live_host=HOST)
        print("PROBE_RESULT " + json.dumps({"status": result.status, "stop_reason": result.stop_reason,
                                            "final_submit_clicked": result.final_submit_clicked,
                                            "handoff_kept": evidence.handoff_kept,
                                            "guard_detached": evidence.guard_detached}), flush=True)
    finally:
        proc.kill()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
