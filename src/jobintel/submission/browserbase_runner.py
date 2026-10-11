"""Stop-only browser runner for a sealed bundle, shared by the Browserbase cloud transport and the
local headed-Chrome transport. It never performs the final submission.

Drives a caller-supplied Playwright-compatible `page` (this module imports no browser,
Browserbase, Stagehand or model library) through an application form using ONLY the
sealed bundle written by `submission.browserbase.write_bundle`:

* the bundle manifest and sealed CV hash are verified before anything else;
* page identity (company, role, URL) is checked with the shared `stop_only.check_page`
  before any field is touched; later pages must stay on the approved host;
* controls are found in the page, in same-origin frames and in open shadow roots; a
  cross-origin frame containing form controls stops the run (never silently skipped);
* the page is rescanned after every interaction, so revealed / newly-required controls
  are handled, and every visible required control must end up answered;
* every label goes through the shared `escalation_reason` / `question` rules. Sensitive,
  unknown, ambiguous, duplicate-labelled or unanswered questions are escalated, never
  filled -- even when the bundle holds an approved value. Values are used verbatim:
  exact select/radio/combobox options, ISO dates only, no reformatting or guessing;
* a control left invalid by client-side validation stops the run;
* the only controls ever clicked are a single exact "Next"/"Continue" that is provably
  not a form submission (type=button, a custom role=button element, or a same-host
  link) and the option of a combobox being filled;
* every form submission path (submit buttons, Enter, form.submit(), requestSubmit(),
  submits inside shadow roots) is blocked in-page, and every non-GET request is blocked
  unless it is an explicitly allowlisted, non-final, same-host XHR/fetch endpoint;
* the run ends in a stop-only status; final_submit_clicked is always False and
  ready_for_human_review is never a receipt.

Live-host mode (`live_host=...`, one exact authorized host, see browserbase_service) adds:
a main-frame navigation off that exact https host is aborted and stops the run
(aborted_wrong_job); a server-side HTTP redirect cannot be intercepted (Playwright routes
only the first URL of a redirect chain), so the main-frame host is also re-checked before
every step and any drift stops the run; any blocked write stops the run at the next step
instead of the next page; blocked requests keep only method/host/path/resource type (no
query, body, headers); URLs after the verified start page are kept without query or fragment.

GraphQL (any mode): a GraphQL POST is never allowed by the URL write allowlist. It passes
only as an explicitly allowlisted read (`graphql_reads`: exact endpoint + operationName,
see graphql_policy); mutations, unknown or unnamed operations stay blocked. Its diagnostics
add only the operation name and type -- never variables, query text or body.

Non-fatal blocked telemetry (`nonfatal_blocked_writes`, opt-in): an explicit per-run set of
exact (method, host) pairs for third-party telemetry (e.g. a RUM beacon) on a host other than the
application host. Such a request is STILL aborted -- it never leaves the browser -- and is recorded
in `nonfatal_blocked_requests` (method/host/path/resource type only); it just does not stop the
run. Every other blocked write stays fatal: anything on the application host, unknown or final
GraphQL operations, and any write to an unlisted host.

CV in live-host mode: by default the runner never selects a file on a real host (selecting one
makes the page start its own upload flow, e.g. an upload-handle mutation and a storage host). A
CV/resume file control is left untouched and marked `human_required`; the run continues with the
remaining controls and normal safe navigation, and the report carries one escalation,
CV_HUMAN_REQUIRED, so the result is never ready_for_human_review without a human. In synthetic
mode the sealed CV is still selected and verified in the DOM as before.

Approved CV upload (`cv_upload=CvUploadPolicy(...)`, live host only, opt-in per request): the
runner uploads exactly the SEALED bundle CV -- re-hashed against the sealed request, and the
policy must name that same artifact id and SHA-256 -- into exactly one control whose label is
plainly a resume/CV label (never "autofill", never a second CV-like control: both escalate). No
caller-supplied path exists; the bytes come only from the sealed bundle. The upload is done LAST
on its page, after every other control, inside a dedicated CV-upload window. Only inside that
window are allowed: the policy's exact upload-handle GraphQL mutations (endpoint + name, no
final-action name), the exact (method, host) storage writes, and the form-value autosave for the
"cv" field. Every other write -- including an upload-handle or storage write outside the window,
an unknown upload/storage operation, or any final/submit mutation -- stays fatal. A storage write
whose request body is readable must carry the sealed CV's exact bytes. The upload counts as
`uploaded` only when the WHOLE approved flow succeeded in order -- upload handle created, storage
write answered 2xx, then the policy's exact file-association mutation (allowed once, only after
that handle + storage succeeded and only naming that upload) -- AND the page shows the file name; a
visible file name alone never counts. Statuses: selected -> upload_handle_created -> uploaded;
`blocked` when a write inside the window was blocked (the run stops); otherwise escalated.
Uploading never clicks anything and never submits.

GraphQL form-value writes (`graphql_form_writes`, opt-in, e.g. a vendor's per-field
autosave mutation): an exact (endpoint, operationName) entry makes a single named `mutation` ELIGIBLE,
and the guard allows it ONLY while an approved-field-fill window is open. The window opens
immediately before the runner fills one control with an exact approved value from the sealed
bundle (never for unresolved, sensitive, ambiguous or escalated controls, never for the CV
upload) and closes once that interaction has settled (blur + FORM_WRITE_SETTLE_MS). Page load,
idle time, rescans, Next/Continue, the final-page stop and teardown/handoff all run with the
window closed, so the same mutation is blocked there. Allowed form writes keep only
endpoint/operation metadata, reason `approved_field_autosave` and the canonical field key.

Teardown: once the result is decided the guard is "finished" -- every later non-GET/HEAD
request (and any off-host navigation in live mode) is aborted and NOT recorded, so nothing
after the stop is attributed to the run and nothing new is authorized. The caller then
removes the guard with `detach_guard(page)` before it disconnects or closes the browser, so
no route handler is still running when the connection goes away.

`identity_state(result)` -- verified / mismatch / not_observed (the run stopped before the
start page's identity was checked) -- is handed to the importer next to the report, so an
early stop is never judged by placeholder strings.

"browserbase" is not in STOP_ONLY_EXECUTORS; the submission service cannot run this.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from jobintel.models.submission import AnswerSnapshot
from jobintel.submission import graphql_policy, stop_only

REQUEST_FILE = "browserbase_request.json"
CV_FILE = "cv.pdf"
MANIFEST_FILE = "manifest.json"
EXECUTOR = "browserbase"
MAX_PAGES = 5
MAX_INTERACTIONS = 200
SETTLE_MS = 60
# JS-rendered forms (e.g. a hosted SPA) draw their controls after "load", once their data arrives.
# Before deciding anything about a page the runner waits -- bounded -- for a stable render
# surface: the same non-empty control/action counts on two consecutive scans RENDER_POLL_MS apart.
RENDER_POLL_MS = 500
RENDER_WAIT_MS = 15000
# How long an approved-field-fill window stays open after blur, for debounced autosave requests.
FORM_WRITE_SETTLE_MS = 1000

# Exact (normalised) texts of the only controls that may ever be clicked to advance.
SAFE_ADVANCE_TEXTS = frozenset({"next", "continue"})
# Any control (or link target) containing one of these is a possible final action: never clicked.
FINAL_ACTION_TERMS = ("submit", "apply", "send", "finish", "complete", "confirm", "done")
CV_LABEL_TERMS = ("cv", "resume", "résumé", "curriculum vitae")
CV_HUMAN_REQUIRED = "CV/resume upload requires human action during handoff."
# Labels that are plainly the resume/CV upload of the application (normalised, exact).
CV_UPLOAD_LABELS = frozenset({"resume", "cv", "résumé", "resume/cv", "cv/resume", "resume / cv", "cv / resume",
                              "curriculum vitae", "upload resume", "upload cv", "upload your resume", "upload your cv",
                              "resume or cv", "cv or resume"})
CV_UPLOAD_EXCLUDED_TERMS = ("autofill", "auto-fill", "parse", "cover letter", "portfolio", "transcript")
CV_UPLOAD_WAIT_MS = 20000
CV_UPLOAD_POLL_MS = 250
MAX_CV_UPLOAD_MUTATIONS = 2
MAX_CV_STORAGE_WRITES = 2
# A CV upload-handle mutation may name an upload; it may never name a final action.
MAX_CV_ASSOCIATION_MUTATIONS = 1
CV_UPLOAD_FINAL_TERMS = ("submit", "apply", "send", "finish", "complete", "confirm", "done", "final")
_CV_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.pdf")
SUPPORTED_TEXT_TYPES = frozenset({"text", "email", "tel", "url", "search", ""})
DATE_FORMATS = {"date": re.compile(r"\d{4}-\d{2}-\d{2}"), "month": re.compile(r"\d{4}-\d{2}")}
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})
MAX_NONFATAL_BLOCKED_WRITES = 3
_DNS_HOST = re.compile(r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")

# Installed before any page script runs, in every frame. Blocks every form-submission path
# and records it; shadow roots get the same capturing listener (submit events do not cross them).
SUBMIT_GUARD_SCRIPT = """
(() => {
  const releaseToken = __JOBINTEL_RELEASE_TOKEN__;  // null: this guard can never be released
  let active = true;
  const blocked = [];
  Object.defineProperty(window, '__jobintelBlockedSubmits', {value: blocked, writable: false});
  const record = (how) => blocked.push(how);
  const stop = (event) => { if (!active) return; event.preventDefault(); event.stopImmediatePropagation(); record('submit event'); };
  const original = {submit: HTMLFormElement.prototype.submit, requestSubmit: HTMLFormElement.prototype.requestSubmit,
                    attachShadow: Element.prototype.attachShadow};
  HTMLFormElement.prototype.submit = function () { record('form.submit()'); };
  HTMLFormElement.prototype.requestSubmit = function () { record('form.requestSubmit()'); };
  window.addEventListener('submit', stop, true);
  const guarded = new WeakSet();
  const roots = [];
  const guardRoot = (root) => { if (active && root && !guarded.has(root)) { guarded.add(root); roots.push(root); root.addEventListener('submit', stop, true); } };
  Object.defineProperty(window, '__jobintelGuardRoot', {value: guardRoot, writable: false});
  const attach = original.attachShadow;
  Element.prototype.attachShadow = function (init) { const root = attach.call(this, init); guardRoot(root); return root; };
  // Human handoff (local browser only): the runner, holding the per-run secret, restores the page
  // to its own behaviour so the HUMAN can submit. Without the exact secret this does nothing.
  Object.defineProperty(window, '__jobintelReleaseGuard', {writable: false, value: (token) => {
    if (releaseToken === null || token !== releaseToken || !active) return false;
    active = false;
    HTMLFormElement.prototype.submit = original.submit;
    HTMLFormElement.prototype.requestSubmit = original.requestSubmit;
    Element.prototype.attachShadow = original.attachShadow;
    window.removeEventListener('submit', stop, true);
    roots.forEach((root) => root.removeEventListener('submit', stop, true));
    return true;
  }});
})();
"""

COLLECT_SCRIPT = """
() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const shown = (el) => el.checkVisibility ? el.checkVisibility({visibilityProperty: true})
                                            : !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  if (!window.__jobintelDoc) Object.defineProperty(window, '__jobintelDoc', {value: crypto.randomUUID()});
  const roots = [document];
  const walk = (root) => { for (const el of root.querySelectorAll('*')) if (el.shadowRoot) { roots.push(el.shadowRoot); walk(el.shadowRoot); } };
  walk(document);
  roots.slice(1).forEach((root) => window.__jobintelGuardRoot && window.__jobintelGuardRoot(root));
  const mark = (el, attr, prefix) => {
    let m = el.getAttribute(attr);
    if (!m) { window.__jobintelSeq = (window.__jobintelSeq || 0) + 1; m = prefix + window.__jobintelSeq; el.setAttribute(attr, m); }
    return m;
  };
  const byId = (root, id) => (root.getElementById ? root.getElementById(id) : document.getElementById(id));
  const labelEl = (el) => {
    const root = el.getRootNode();
    if (el.id && root.querySelector) { const l = root.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l; }
    return el.closest('label');
  };
  const TITLE = 'legend, [role=heading], [class*="question-title"], [class*="field-title"]';
  // A radio option's own label: the wrapping <label>, else a label[for] that is not a question title
  // (a title may point its for= at the first option; that title is the GROUP's, not the option's).
  const optionLabelEl = (el) => {
    const wrap = el.closest('label'); if (wrap) return wrap;
    const root = el.getRootNode();
    if (!el.id || !root.querySelectorAll) return null;
    return [...root.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`)].filter((l) => !l.matches(TITLE)).pop() || null;
  };
  const textOf = (root, ids) => norm((ids || '').split(/\\s+/).filter(Boolean)
    .map((i) => (byId(root, i) || {}).innerText || '').join(' '));
  const labelFor = (el) => {
    const l = el.type === 'radio' ? optionLabelEl(el) : labelEl(el); if (l) return norm(l.innerText);
    if (el.getAttribute('aria-label')) return norm(el.getAttribute('aria-label'));
    const by = el.getAttribute('aria-labelledby');
    if (by) return norm(by.split(/\\s+/).map((i) => (byId(el.getRootNode(), i) || {}).innerText || '').join(' '));
    return '';
  };
  // Hosted ATS forms often title a question with a <label>/heading in the field's
  // container that is not tied to the input. Walk up while the container holds ONLY this control
  // (`mine`): the first title-like element there that is not some control's own label is the title.
  // Within a level, a structural question title (legend / heading / *question-title / *field-title)
  // beats a plain <label>, and the one NEAREST before the control wins: section-level instructions
  // that precede the field's own title never become its label.
  const containerLabel = (el, mine, ownOf) => {
    let node = el.parentElement;
    const before = (t) => !!(t.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
    for (let i = 0; node && i < 6; i++, node = node.parentElement) {
      const fields = [...node.querySelectorAll('input:not([type=hidden]), select, textarea')];
      if (!fields.every(mine)) return '';
      const own = new Set(fields.map(ownOf || labelEl).filter(Boolean));
      const titles = [...node.querySelectorAll('label, ' + TITLE)]
        .filter((t) => !own.has(t) && !t.contains(el) && !t.querySelector('input, select, textarea') && norm(t.innerText));
      const pick = (list) => list.filter(before).pop() || list[0];
      const best = pick(titles.filter((t) => t.matches(TITLE))) || pick(titles);
      if (best) return norm(best.innerText);
    }
    return '';
  };
  const invalid = (el) => el.getAttribute('aria-invalid') === 'true'
    || (el.willValidate && !el.validity.valid && !el.validity.valueMissing);
  const controls = []; const groups = {};
  roots.forEach((root, r) => {
    for (const el of root.querySelectorAll('input, select, textarea')) {
      const tag = el.tagName.toLowerCase();
      let type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
      if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || el.disabled) continue;
      const lab = labelEl(el);
      const visible = shown(el) || (['radio', 'checkbox', 'file'].includes(type) && lab && shown(lab));
      if (!visible) continue;
      if (tag === 'input' && el.getAttribute('role') === 'combobox') type = 'combobox';
      const required = el.required || el.getAttribute('aria-required') === 'true';
      if (type === 'radio') {
        const key = r + '|' + (el.form ? mark(el.form, 'data-jobintel-form', 'f') : '') + '|' + (el.name || mark(el, 'data-jobintel-control', 'c'));
        if (!groups[key]) {
          const fs = el.closest('fieldset'); const legend = fs && fs.querySelector('legend');
          const rg = el.closest('[role=radiogroup]');
          const named = [rg, fs].find((n) => n && n.getAttribute('aria-labelledby'));
          groups[key] = {kind: 'radio', marker: 'g' + mark(el, 'data-jobintel-control', 'c'), required: false, options: [],
                         has_value: false, invalid: false,
                         label: norm(legend ? legend.innerText : '')
                                || (named ? textOf(named.getRootNode(), named.getAttribute('aria-labelledby')) : '')
                                || norm(rg ? (rg.getAttribute('aria-label') || '') : '')
                                || containerLabel(el, (f) => f.type === 'radio' && f.name === el.name && f.form === el.form,
                                                  (f) => (f.type === 'radio' ? optionLabelEl(f) : labelEl(f)))};
          controls.push(groups[key]);
        }
        const g = groups[key];
        g.required = g.required || required; g.has_value = g.has_value || el.checked; g.invalid = g.invalid || invalid(el);
        g.options.push({marker: mark(el, 'data-jobintel-control', 'c'), text: labelFor(el), value: el.value});
        continue;
      }
      const item = {kind: type, marker: mark(el, 'data-jobintel-control', 'c'),
                    label: labelFor(el) || containerLabel(el, (f) => f === el), required,
                    invalid: invalid(el), name: el.name || ''};
      item.has_value = type === 'checkbox' ? el.checked : (type === 'file' ? el.files.length > 0 : String(el.value).trim() !== '');
      if (type === 'select') item.options = [...el.options].map((o) => ({text: norm(o.text), value: o.value}));
      controls.push(item);
    }
  });
  const actions = [];
  for (const root of roots) {
    for (const el of root.querySelectorAll('button, input[type=submit], input[type=button], input[type=image], [role=button], a')) {
      if (!shown(el)) continue;
      const tag = el.tagName.toLowerCase();
      const type = tag === 'button' ? (el.getAttribute('type') || 'submit').toLowerCase()
                 : (tag === 'input' ? (el.getAttribute('type') || '').toLowerCase() : tag);
      actions.push({marker: mark(el, 'data-jobintel-action', 'a'), tag, type, role: el.getAttribute('role') || '',
                    text: norm(el.innerText || el.value || el.getAttribute('aria-label') || ''),
                    in_form: !!el.closest('form'), href: tag === 'a' && el.hasAttribute('href') ? el.href : '',
                    raw_href: tag === 'a' ? (el.getAttribute('href') || '') : '', target: el.getAttribute('target') || ''});
    }
  }
  const headings = [...document.querySelectorAll('h1, h2')].map((h) => norm(h.innerText));
  return {doc: window.__jobintelDoc, title: norm(document.title), headings, controls, actions};
}
"""

COMBOBOX_OPTION_SCRIPT = """
([el, value]) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const shown = (n) => n.checkVisibility ? n.checkVisibility({visibilityProperty: true}) : !!n.getClientRects().length;
  const id = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  if (!id) return 'no_listbox_reference';
  const root = el.getRootNode();
  const list = root.getElementById ? root.getElementById(id) : document.getElementById(id);
  if (!list || !shown(list)) return 'listbox_not_open';
  const exact = [...list.querySelectorAll('[role=option]')].filter((o) => shown(o) && norm(o.innerText) === value);
  if (exact.length !== 1) return exact.length ? 'ambiguous_option' : 'no_exact_option';
  exact[0].setAttribute('data-jobintel-option', el.getAttribute('data-jobintel-control'));
  return 'ok';
}
"""

LOCATION_OPTION_SCRIPT = """
([el, value]) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const fold = (s) => norm(s).toLowerCase();
  const shown = (n) => n.checkVisibility ? n.checkVisibility({visibilityProperty: true}) : !!n.getClientRects().length;
  const id = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  if (!id) return 'no_listbox_reference';
  const root = el.getRootNode();
  const list = root.getElementById ? root.getElementById(id) : document.getElementById(id);
  if (!list || !shown(list)) return 'listbox_not_open';
  const options = [...list.querySelectorAll('[role=option]')].filter(shown);
  const parts = value.split(',').map(fold);
  const city = parts[0], country = parts[parts.length - 1];
  let pick = options.filter((o) => fold(o.innerText) === fold(value));
  if (!pick.length) pick = options.filter((o) => { const p = o.innerText.split(',').map(fold);
                                                    return p.length >= 2 && p[0] === city && p[p.length - 1] === country; });
  if (pick.length !== 1) return pick.length ? 'ambiguous_option' : 'no_matching_option';
  pick[0].setAttribute('data-jobintel-option', el.getAttribute('data-jobintel-control'));
  return 'ok:' + norm(pick[0].innerText);
}
"""

CONTROL_STATE_SCRIPT = """
(el) => ({value: el.value, invalid: el.getAttribute('aria-invalid') === 'true'
          || (el.willValidate && !el.validity.valid && !el.validity.valueMissing)})
"""


def normalise_label(text: str) -> str:
    return " ".join(text.replace("*", " ").split()).rstrip(":").strip().casefold()


@dataclass(frozen=True)
class SealedBundle:
    """The verified bundle: request data plus the sealed CV path. Nothing else is read."""
    request: dict
    cv_path: Path
    company: str
    role: str
    application_url: str
    answers: tuple[AnswerSnapshot, ...]


def load_bundle(directory) -> SealedBundle:
    """Verify the manifest and the sealed CV before the runner may use anything."""
    directory = Path(directory)
    for name in (REQUEST_FILE, CV_FILE, MANIFEST_FILE):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Bundle file missing or not a regular file: {name}")
    manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
    if set(manifest) != {REQUEST_FILE, CV_FILE}:
        raise ValueError("Bundle manifest has unexpected entries")
    for name, digest in manifest.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Bundle file changed after sealing: {name}")
    request = json.loads((directory / REQUEST_FILE).read_text(encoding="utf-8"))
    if (request.get("executor") != EXECUTOR or request.get("execution_mode") != stop_only.MODE
            or request.get("final_submit_allowed") is not False):
        raise ValueError("Bundle is not a stop-only Browserbase bundle")
    if hashlib.sha256((directory / CV_FILE).read_bytes()).hexdigest() != request["cv"]["sha256"]:
        raise ValueError("Sealed CV hash does not match the bundle")
    answers = tuple(AnswerSnapshot(a["field"], a["value"], a["source"], a["version"]) for a in request["answers"])
    return SealedBundle(request, directory / CV_FILE, request["company"], request["role"],
                        request["application_url"], answers)


def _looks_final(text: str) -> bool:
    lowered = text.casefold()
    return any(term in lowered for term in FINAL_ACTION_TERMS)


def validate_write_allowlist(bundle: SealedBundle, entries) -> frozenset:
    """Explicit (method, exact URL) pairs a caller permits as benign background writes.
    Same https host as the approved URL, no wildcards, nothing that looks like a final action."""
    return validate_write_entries(bundle.application_url, entries)


def validate_nonfatal_blocked_writes(application_url: str, entries) -> frozenset:
    """Exact (method, host) pairs whose blocked writes do not stop the run. They are never
    allowed through. Host: a plain lowercase DNS name, not an IP, no wildcard/port/scheme, and
    never the application host or one of its subdomains (application writes always stay fatal)."""
    app_host = urlsplit(application_url).hostname or ""
    entries = tuple((str(m), str(h)) for m, h in entries)
    if len(entries) > MAX_NONFATAL_BLOCKED_WRITES or len(set(entries)) != len(entries):
        raise ValueError(f"At most {MAX_NONFATAL_BLOCKED_WRITES} distinct exact non-fatal blocked-write entries")
    for method, host in entries:
        if method not in WRITE_METHODS:
            raise ValueError(f"Non-fatal blocked-write entry must be a write method: {method}")
        if not _DNS_HOST.fullmatch(host):
            raise ValueError("Non-fatal blocked-write entry must be one exact lowercase DNS host (no wildcard)")
        if host == app_host or host.endswith("." + app_host) or app_host.endswith("." + host):
            raise ValueError("Writes to the application host (or its parent/subdomains) are always fatal")
    return frozenset(entries)


@dataclass(frozen=True)
class CvUploadPolicy:
    """Per-request permission to upload the sealed, approved CV once. Built by the submission
    service from the approved package; never read from the page or a caller-supplied path."""
    artifact_id: str
    sha256: str
    filename: str  # the name the page shows; the bytes are always the sealed bundle CV
    graphql_mutations: frozenset = frozenset()  # exact (endpoint, operationName) upload-handle mutations
    storage_writes: frozenset = frozenset()  # exact (method, third-party host) storage uploads
    # exact (endpoint, operationName) mutation that attaches the stored file to the form field; allowed
    # only after this window's handle + storage succeeded, and only naming this window's upload
    association_mutations: frozenset = frozenset()


def validate_cv_upload_policy(bundle: SealedBundle, policy: CvUploadPolicy) -> CvUploadPolicy:
    """The policy must name exactly the sealed artifact and hash, a plain .pdf file name, exact
    upload-handle mutations on the approved host with no final-action name, and exact storage
    writes to plain DNS hosts other than the application host (whose writes stay fatal)."""
    cv = bundle.request["cv"]
    return validate_cv_upload_for(bundle.application_url, cv["artifact_id"], cv["sha256"], policy)


def validate_cv_upload_for(application_url: str, artifact_id: str, sha256: str, policy: CvUploadPolicy) -> CvUploadPolicy:
    """validate_cv_upload_policy against an approved package's application URL and CV."""
    if not isinstance(policy, CvUploadPolicy) or (policy.artifact_id, policy.sha256) != (artifact_id, sha256):
        raise ValueError("CV upload policy does not name the sealed, approved CV")
    if not _CV_FILENAME.fullmatch(policy.filename):
        raise ValueError("CV upload file name must be a plain .pdf name")
    mutations = tuple((str(e), str(n)) for e, n in policy.graphql_mutations)
    if len(mutations) > MAX_CV_UPLOAD_MUTATIONS or len(set(mutations)) != len(mutations):
        raise ValueError(f"At most {MAX_CV_UPLOAD_MUTATIONS} distinct exact CV upload mutations")
    app_host = urlsplit(application_url).hostname
    for endpoint, name in mutations:
        parts = urlsplit(endpoint)
        if (parts.scheme != "https" or parts.hostname != app_host or parts.username or parts.password
                or parts.query or parts.fragment or "*" in endpoint or not parts.path):
            raise ValueError("CV upload mutation endpoint must be an exact https URL on the approved host")
        words = graphql_policy.name_words(name)
        if (not graphql_policy.NAME.fullmatch(name) or "upload" not in words
                or any(term in word for word in words for term in CV_UPLOAD_FINAL_TERMS)
                or any(word in graphql_policy.DANGEROUS_WORDS for word in words)):
            raise ValueError("CV upload mutation must name one exact upload operation with no final-action meaning")
    associations = tuple((str(e), str(n)) for e, n in policy.association_mutations)
    if len(associations) > MAX_CV_ASSOCIATION_MUTATIONS or len(set(associations)) != len(associations):
        raise ValueError(f"At most {MAX_CV_ASSOCIATION_MUTATIONS} exact CV file-association mutation")
    for endpoint, name in associations:
        parts = urlsplit(endpoint)
        if (parts.scheme != "https" or parts.hostname != app_host or parts.username or parts.password
                or parts.query or parts.fragment or "*" in endpoint or not parts.path):
            raise ValueError("CV file-association endpoint must be an exact https URL on the approved host")
        words = graphql_policy.name_words(name)
        if (not graphql_policy.NAME.fullmatch(name) or "file" not in words or (endpoint, name) in mutations
                or any(term in word for word in words for term in CV_UPLOAD_FINAL_TERMS)
                or any(word in graphql_policy.DANGEROUS_WORDS for word in words)):
            raise ValueError("CV file-association mutation must name one exact file operation with no final-action meaning")
    storage = tuple((str(m), str(h)) for m, h in policy.storage_writes)
    if len(storage) > MAX_CV_STORAGE_WRITES or len(set(storage)) != len(storage):
        raise ValueError(f"At most {MAX_CV_STORAGE_WRITES} distinct exact CV storage writes")
    for method, host in storage:
        if method not in {"POST", "PUT"}:
            raise ValueError("CV storage write must be POST or PUT")
        if not _DNS_HOST.fullmatch(host):
            raise ValueError("CV storage write must name one exact lowercase DNS host (no wildcard)")
        if host == app_host or host.endswith("." + app_host) or app_host.endswith("." + host):
            raise ValueError("CV storage host cannot be the application host or its parent/subdomains")
    return CvUploadPolicy(policy.artifact_id, policy.sha256, policy.filename, frozenset(mutations), frozenset(storage),
                          frozenset(associations))


def validate_write_entries(application_url: str, entries) -> frozenset:
    host = urlsplit(application_url).hostname
    allowed = set()
    for entry in entries:
        method, url = entry
        parts = urlsplit(url)
        if method not in WRITE_METHODS:
            raise ValueError(f"Allowlist entry must be a write method: {method}")
        if "graphql" in parts.path.casefold():
            raise ValueError("GraphQL endpoints cannot be URL-allowlisted; authorize exact read operations instead")
        if parts.scheme != "https" or parts.hostname != host or parts.username or parts.password or parts.fragment:
            raise ValueError("Allowlist entry must be an https URL on the approved host")
        if "*" in url or _looks_final(parts.path + "?" + parts.query):
            raise ValueError("Allowlist entry must be an exact, non-final endpoint")
        allowed.add((method, url))
    return frozenset(allowed)


@dataclass
class RunResult:
    """Local evidence of one stop-only run. Not a receipt; nothing here claims submission."""
    status: str = "needs_human_input"
    stop_reason: str = ""
    current_url: str = ""
    page_identity: dict = field(default_factory=dict)
    start_identity: dict = field(default_factory=dict)  # page 1: verified with check_page
    pages_visited: int = 0
    frames_used: list = field(default_factory=list)
    controls_seen: int = 0
    controls_detected: list = field(default_factory=list)  # safe keys only: label/kind/required, never values
    fields_filled: list = field(default_factory=list)
    fields_skipped: list = field(default_factory=list)
    questions_escalated: list = field(default_factory=list)
    cv_upload: dict = field(default_factory=dict)
    # not_seen / human_required (live) / selected / upload_handle_created / uploaded (live, approved policy:
    # complete flow) / blocked (a write in the CV-upload window was blocked) / escalated / skipped
    cv_upload_status: str = "not_seen"
    cv_control: dict = field(default_factory=dict)  # safe key of the CV control: label/kind/required only
    final_submit_seen: bool = False
    final_submit_clicked: bool = False
    live_host: str = ""  # set only in single-request live-host mode
    advance_clicks: list = field(default_factory=list)
    blocked_submissions: list = field(default_factory=list)
    blocked_requests: list = field(default_factory=list)
    allowed_writes: list = field(default_factory=list)
    nonfatal_blocked_requests: list = field(default_factory=list)  # aborted, recorded, did not stop the run
    evidence: list = field(default_factory=list)


class _Stop(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _on_host(url: str, host: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == host and not parts.username and not parts.password


def _without_query(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme in {"http", "https"} else url.split("?", 1)[0]


def _diagnostic(request, kind: str) -> dict:
    """Minimum metadata about a blocked request: never the query, body, headers or cookies."""
    parts = urlsplit(request.url)
    return {"kind": kind, "method": request.method, "host": parts.hostname or "", "path": parts.path,
            "resource_type": getattr(request, "resource_type", "") or ""}


def _is_main_navigation(page, request) -> bool:
    try:
        return request.is_navigation_request() and request.frame == page.main_frame
    except Exception:  # e.g. service-worker requests have no frame: not a page navigation
        return False


def _post_body(request):
    try:
        return request.post_data
    except Exception:  # binary / undecodable body: never inspected
        return None


def _post_body_bytes(request) -> bytes | None:
    """Raw request body for an in-memory check only (never recorded); None when unavailable."""
    try:
        return request.post_data_buffer
    except Exception:
        return None


def guard_script(release_token: str | None = None) -> str:
    """The in-page submit guard. With a per-run secret `release_token` it can later be released
    (local human handoff only, see release_submit_guard); with None it can never be released."""
    if release_token is not None and not (isinstance(release_token, str) and len(release_token) >= 32
                                          and release_token.isalnum()):
        raise ValueError("Guard release token must be a long random alphanumeric secret")
    return SUBMIT_GUARD_SCRIPT.replace("__JOBINTEL_RELEASE_TOKEN__", json.dumps(release_token))


def release_submit_guard(page, release_token: str) -> bool:
    """Human handoff: restore every frame's own submit behaviour. Call only AFTER the runner has
    finished (its result exists) and its route handler is removed, immediately before the
    automation disconnects. True if the main frame's guard was released and no frame refused."""
    outcomes = []
    for frame in page.frames:
        try:
            outcomes.append((frame == page.main_frame, frame.evaluate(
                "t => window.__jobintelReleaseGuard ? window.__jobintelReleaseGuard(t) : null", release_token)))
        except Exception:
            outcomes.append((frame == page.main_frame, False))
    return any(main and ok is True for main, ok in outcomes) and all(ok is not False for _, ok in outcomes)


def run(page, bundle: SealedBundle, *, write_allowlist=(), live_host: str | None = None, graphql_reads=(),
        graphql_form_writes=(), nonfatal_blocked_writes=(), guard_release_token: str | None = None,
        cv_upload: CvUploadPolicy | None = None) -> RunResult:
    """Run one stop-only attempt. `page` must be a fresh Playwright(-compatible) page.
    `write_allowlist` is an explicit, caller-supplied set of benign background writes.
    `live_host` (single-request live-host mode) must be exactly the approved https host.
    `graphql_reads` is an explicit per-run set of (endpoint, operationName) GraphQL reads.
    `graphql_form_writes` is an explicit per-run set of (endpoint, operationName) form-value
    mutations, allowed only inside an approved-field-fill window.
    `nonfatal_blocked_writes` is an explicit per-run set of (method, exact third-party host) whose
    writes are still aborted and recorded but do not stop the run.
    `cv_upload` (live host only) authorizes uploading the sealed CV once, see the module docs."""
    if cv_upload is not None and live_host is None:
        raise ValueError("An approved CV upload is only defined for a single-request live-host run")
    if live_host is not None and not (_on_host(bundle.application_url, live_host)
                                      and urlsplit(bundle.application_url).port is None):
        raise ValueError("Live host does not match the approved application host")
    allowed_writes = validate_write_allowlist(bundle, write_allowlist)
    allowed_reads = graphql_policy.validate_reads(bundle.application_url, graphql_reads)
    allowed_form_writes = graphql_policy.validate_form_writes(bundle.application_url, graphql_form_writes)
    nonfatal = validate_nonfatal_blocked_writes(bundle.application_url, nonfatal_blocked_writes)
    policy = validate_cv_upload_policy(bundle, cv_upload) if cv_upload is not None else None
    result = RunResult(live_host=live_host or "")
    guard = {"finished": False}
    # Canonical key of the approved field being filled right now; None outside a fill window.
    # "cv" is True only inside the approved CV-upload window (field "cv").
    window = {"field": None, "settle_ms": FORM_WRITE_SETTLE_MS if allowed_form_writes else 0, "cv": False,
              "policy": policy, "cv_bytes": None, "cv_flow": []}

    def network_guard(route, request):
        if guard["finished"]:
            # The result is already decided: no allowlist applies any more and nothing is recorded.
            off_host = live_host and _is_main_navigation(page, request) and not _on_host(request.url, live_host)
            return route.fallback() if request.method in {"GET", "HEAD"} and not off_host else route.abort()
        if live_host and _is_main_navigation(page, request) and not _on_host(request.url, live_host):
            result.blocked_requests.append(_diagnostic(request, "navigation"))
            return route.abort()
        if request.method in {"GET", "HEAD"}:
            return route.fallback()
        parts = urlsplit(request.url)
        if (request.method, parts.hostname or "") in nonfatal and not _is_main_navigation(page, request):
            # Still aborted (never leaves the browser); recorded apart so it does not stop the run.
            result.nonfatal_blocked_requests.append(_diagnostic(request, "nonfatal_blocked_write"))
            return route.abort()
        if (window["cv"] and policy is not None and (request.method, parts.hostname or "") in policy.storage_writes
                and parts.scheme == "https" and request.resource_type in {"xhr", "fetch"}
                and not _is_main_navigation(page, request)):
            # The approved CV's bytes going to the exact storage host, only inside the upload window.
            body = _post_body_bytes(request)
            if body and window["cv_bytes"] and window["cv_bytes"] not in body:
                result.blocked_requests.append(_diagnostic(request, "cv_storage_body_mismatch"))
                return route.abort()
            result.allowed_writes.append({**_diagnostic(request, "cv_storage_upload"), "field": "cv",
                                          "sealed_bytes_seen": bool(body)})
            window["cv_flow"].append(("storage", request))
            window["cv_storage_tokens"] = window.get("cv_storage_tokens", set()) | _multipart_tokens(body)
            return route.fallback()
        url = request.url.split("#", 1)[0]
        body = _post_body(request)
        if graphql_policy.looks_like_graphql(url, body):
            info = graphql_policy.inspect(url, body)
            diagnostic = {**_diagnostic(request, "write"), "graphql_operation_name": info["graphql_operation_name"],
                          "graphql_operation_type": info["graphql_operation_type"]}
            if (window["cv"] and policy is not None and request.method == "POST"
                    and request.resource_type in {"xhr", "fetch"} and info["graphql_operation_type"] == "mutation"
                    and info["single_operation"] and info["graphql_operation_name"] is not None
                    and (graphql_policy.endpoint_of(url), info["graphql_operation_name"]) in policy.association_mutations
                    and _on_host(url, live_host)):
                # Attach the stored file to the form: only after THIS window's handle + storage succeeded,
                # once, and only naming THIS upload (a value from its handle answer / storage form fields).
                problem = _cv_association_problem(window, body, policy)
                if problem is None:
                    result.allowed_writes.append({**diagnostic, "kind": "cv_file_association", "field": "cv"})
                    window["cv_flow"].append(("associate", request))
                    return route.fallback()
                result.blocked_requests.append({**diagnostic, "kind": "cv_association_refused", "reason": problem})
                return route.abort()
            if (window["cv"] and policy is not None and request.method == "POST"
                    and request.resource_type in {"xhr", "fetch"} and info["graphql_operation_type"] == "mutation"
                    and info["single_operation"] and info["graphql_operation_name"] is not None
                    and (graphql_policy.endpoint_of(url), info["graphql_operation_name"]) in policy.graphql_mutations
                    and _on_host(url, live_host)):
                result.allowed_writes.append({**diagnostic, "kind": "cv_upload_handle", "field": "cv"})
                window["cv_flow"].append(("handle", request))
                return route.fallback()
            if (graphql_policy.is_allowed_read(request.method, request.resource_type, url, info, allowed_reads)
                    and (not live_host or _on_host(url, live_host))):
                result.allowed_writes.append({**diagnostic, "kind": "graphql_read"})
                return route.fallback()
            if (window["field"] is not None
                    and graphql_policy.is_allowed_form_write(request.method, request.resource_type, url, info,
                                                             allowed_form_writes)
                    and (not live_host or _on_host(url, live_host))):
                result.allowed_writes.append({**diagnostic, "kind": "graphql_form_write",
                                              "reason": "approved_field_autosave", "field": window["field"]})
                return route.fallback()
            result.blocked_requests.append(diagnostic)
            return route.abort()
        if (request.method, url) in allowed_writes and request.resource_type in {"xhr", "fetch"}:
            result.allowed_writes.append({"method": request.method, "url": url})
            return route.fallback()
        result.blocked_requests.append(_diagnostic(request, "write") if live_host else {"method": request.method, "url": url})
        return route.abort()

    page.add_init_script(guard_script(guard_release_token))
    page.route("**/*", network_guard)
    state: dict = {}
    try:
        page.goto(bundle.application_url, wait_until="load")  # "load" includes subframes
        page.wait_for_timeout(SETTLE_MS)
        _live_check(page, result)
        _wait_for_render(page, bundle, result)
        for _ in range(MAX_PAGES):
            result.pages_visited += 1
            _check_identity(page, bundle, page.main_frame.evaluate(COLLECT_SCRIPT), result)
            scans = _process_page(page, bundle, state, result, window)
            _guard_tripped(page, result)
            if result.questions_escalated:
                raise _Stop("needs_human_input", "questions_escalated")
            frame, advance = _next_action(scans, bundle, result)
            if advance is None:
                if result.controls_seen == 0:
                    raise _Stop("needs_human_input", "no_form_controls_found")
                raise _Stop("ready_for_human_review", "final_page_reached_stop_before_submit")
            before = _render_fingerprint(scans, urlsplit(bundle.application_url).hostname)
            result.advance_clicks.append(advance["text"])
            frame.click(f'[data-jobintel-action="{advance["marker"]}"]')
            frame.wait_for_load_state("load")
            page.wait_for_load_state("load")
            page.wait_for_timeout(SETTLE_MS)
            # The next step gets the same render opportunity; "no effect" only once it is over.
            _wait_for_render(page, bundle, result, previous=before)
        raise _Stop("needs_human_input", "too_many_pages")
    except _Stop as stop:
        result.status, result.stop_reason = stop.status, stop.reason
    except Exception as exc:  # any unexpected browser state: never claim anything
        result.status, result.stop_reason = "outcome_unknown", f"runner_error:{type(exc).__name__}"
        if result.live_host:
            try:
                _live_check(page, result)
            except _Stop as stop:
                result.status, result.stop_reason = stop.status, stop.reason
            except Exception:
                pass
    finally:
        window["field"], window["cv"], window["cv_bytes"] = None, False, None
        guard["finished"] = True
        _finish(page, result)
    return result


def detach_guard(page) -> bool:
    """Remove the runner's route handler AFTER its result exists and BEFORE the caller
    disconnects or closes the browser. `behavior="wait"` lets any handler call already in
    flight (it can only abort or fall back, see the finished guard) complete instead of being
    cancelled by the disconnect. Returns False if the page/connection is already gone."""
    try:
        page.unroute_all(behavior="wait")
        # Route events already queued before interception was switched off are answered by
        # Playwright with internal continue messages; one round trip lets them complete before
        # the caller tears the connection down (otherwise they are cancelled mid-send).
        page.evaluate("0")
        return True
    except Exception:
        return False


def _check_identity(page, bundle, snapshot, result):
    text = " | ".join([snapshot["title"], *snapshot["headings"]]).casefold()
    company_seen = bundle.company.casefold() in text
    role_seen = bundle.role.casefold() in text
    url = page.url
    first = result.pages_visited == 1
    if first:
        state = stop_only.check_page(bundle, company=bundle.company if company_seen else snapshot["title"],
                                     role=bundle.role if role_seen else snapshot["title"], application_url=url)
    else:
        same_host = urlsplit(url).hostname == urlsplit(bundle.application_url).hostname
        state = "matched" if (company_seen and role_seen and same_host) else "aborted_wrong_job"
    if result.live_host and not first:
        url = _without_query(url)
    result.page_identity = {"url": url, "title": snapshot["title"], "company_seen": company_seen,
                            "role_seen": role_seen, "state": state, "page": result.pages_visited}
    if first:
        result.start_identity = dict(result.page_identity)
    if state != "matched":
        raise _Stop("aborted_wrong_job", "page_identity_mismatch")


def _usable_frames(page, bundle):
    """Main frame plus same-origin child frames. A cross-origin frame with form controls stops."""
    host = urlsplit(bundle.application_url).hostname
    frames = [page.main_frame]
    for frame in page.frames:
        if frame is page.main_frame or frame.is_detached():
            continue
        parts = urlsplit(frame.url)
        if (parts.scheme == "https" and parts.hostname == host) or frame.url in {"about:blank", "about:srcdoc"}:
            frames.append(frame)
            continue
        count = frame.evaluate("() => document.querySelectorAll('input:not([type=hidden]), select, textarea').length")
        if count:
            raise _Stop("needs_human_input", "cross_origin_frame_with_form_controls")
    return frames


def _live_check(page, result):
    """Live-host mode only: stop on host drift or on any blocked request before touching anything else."""
    if not result.live_host:
        return
    if any(b.get("kind") == "navigation" for b in result.blocked_requests):
        raise _Stop("aborted_wrong_job", "host_drift_blocked")
    if result.blocked_requests:
        raise _Stop("needs_human_input", "unexpected_write_blocked")
    if not _on_host(page.main_frame.url, result.live_host):
        raise _Stop("aborted_wrong_job", "host_drift")


def _scan(page, bundle, result):
    _live_check(page, result)
    scans = []
    for frame in _usable_frames(page, bundle):
        snapshot = frame.evaluate(COLLECT_SCRIPT)
        scans.append((frame, snapshot))
        if frame.url not in result.frames_used:
            result.frames_used.append(frame.url)
    return scans


def _key(frame, snapshot, control):
    return (frame, snapshot["doc"], control["marker"])


def _process_page(page, bundle, state, result, window):
    """Handle every visible control, rescanning after each interaction; then require that
    every visible required control is answered and every filled control is valid."""
    for _ in range(MAX_INTERACTIONS):
        scans = _scan(page, bundle, result)
        labelled = [(f, s, c) for f, s, c in ((f, s, c) for f, s in scans for c in s["controls"]) if c["kind"] != "file"]
        counts: dict = {}
        for _, _, c in labelled:
            if normalise_label(c["label"]):
                counts[normalise_label(c["label"])] = counts.get(normalise_label(c["label"]), 0) + 1
        pending = [(f, s, c) for f, s in scans for c in s["controls"] if _key(f, s, c) not in state]
        if not pending:
            break
        uploading = window is not None and window.get("policy") is not None
        if uploading:  # the approved CV upload goes last on its page, after every other control
            pending.sort(key=lambda item: item[2]["kind"] == "file" and _is_cv_control(item[2]))
        frame, snapshot, control = pending[0]
        result.controls_seen += 1
        result.controls_detected.append(_control_key(control))
        context = f"page {result.pages_visited}: {snapshot['title']}"
        if control["kind"] != "file" and counts.get(normalise_label(control["label"]), 0) > 1:
            _escalate(result, control["label"], "duplicate_label", context)
            state[_key(frame, snapshot, control)] = "escalated"
            continue
        if uploading and control["kind"] == "file" and _is_cv_control(control):
            state[_key(frame, snapshot, control)] = _upload_cv(page, frame, bundle, control, result, context, window, scans)
        else:
            state[_key(frame, snapshot, control)] = _process_control(frame, bundle, control, result, context, window)
        page.wait_for_timeout(SETTLE_MS)
    else:
        raise _Stop("needs_human_input", "too_many_interactions")
    for frame, snapshot in scans:
        context = f"page {result.pages_visited}: {snapshot['title']}"
        for control in snapshot["controls"]:
            outcome = state.get(_key(frame, snapshot, control))
            if outcome in {"escalated", "human_required"}:
                continue
            if control["required"] and not control["has_value"]:
                _escalate(result, control["label"], "required_field_unanswered", context)
                state[_key(frame, snapshot, control)] = "escalated"
            elif outcome == "filled" and control["invalid"]:
                _escalate(result, control["label"], "client_validation_failed", context)
                state[_key(frame, snapshot, control)] = "escalated"
    return scans


def _escalate(result, label, reason, page_context):
    result.questions_escalated.append({"status": "needs_human_input", "question": label or "(unlabelled control)",
                                       "page_context": page_context, "allowed_known_answers": [],
                                       "screenshot_or_evidence": f"runner:{reason}"})


def _resolve(bundle, label):
    """(value, None) for an exact, unambiguous approved answer; (None, reason) otherwise."""
    reason = stop_only.escalation_reason(label, deterministic=True)
    if reason is not None:
        return None, reason
    key = normalise_label(label)
    fields = stop_only.label_fields(key, deterministic=True)
    values = {stop_only.question(bundle, f, key, "", "", deterministic=True).get("value") for f in fields}
    values.discard(None)
    if not values:
        return None, "no_approved_answer"
    if len(values) > 1:
        return None, "ambiguous_label"
    return values.pop(), None


def _control_key(control) -> dict:
    """Safe description of a detected control: normalised page label, kind, required flag and
    the canonical package fields that label maps to. Never a value."""
    label = normalise_label(control["label"])
    if control["kind"] == "file":
        fields = ["cv"] if any(term in label for term in CV_LABEL_TERMS) else []
    else:
        fields = sorted(stop_only.label_fields(label, deterministic=True))
    return {"label": label[:120], "kind": control["kind"], "required": bool(control["required"]),
            "canonical_fields": fields}


def _process_control(frame, bundle, control, result, context, window=None) -> str:
    """Returns the control's state: 'filled', 'skipped' or 'escalated'."""
    label, kind, required = control["label"], control["kind"], control["required"]
    if kind == "file" and result.live_host and _is_cv_control(control):
        return _defer_cv_to_human(control, result)
    if kind == "file":
        return _handle_file(frame, bundle, control, result, context)
    if kind not in SUPPORTED_TEXT_TYPES | {"textarea", "select", "radio", "checkbox", "combobox", *DATE_FORMATS}:
        return _skip_or_escalate(result, label, f"unsupported_control:{kind}", required, context)
    value, reason = _resolve(bundle, normalise_label(label))
    if value is None:
        if reason.startswith("sensitive:") or reason == "ambiguous_profile_link":
            _escalate(result, label, reason, context)
            return "escalated"
        return _skip_or_escalate(result, label, reason, required, context)
    field_name = next(f for f in stop_only.label_fields(normalise_label(label), deterministic=True)
                      if stop_only.question(bundle, f, normalise_label(label), "", "", deterministic=True).get("value")
                      == value)
    marker = control["options"][0]["marker"] if kind == "radio" else control["marker"]
    selector = f'[data-jobintel-control="{marker}"]'
    # Approved-field-fill window: the only time an allowlisted form-value mutation may pass.
    # It covers exactly this control's fill, blur and settle, and is always closed afterwards.
    if window is not None:
        window["field"] = field_name
    try:
        failure = _fill(frame, control, value, field_name)
        if failure:
            _escalate(result, label, failure, context)
            return "escalated"
        result.fields_filled.append({"field": field_name, "value": value, "page_label": label})
        frame.eval_on_selector(selector, "el => el.blur()")
        frame.wait_for_timeout((window or {}).get("settle_ms") or SETTLE_MS)
    finally:
        if window is not None:
            window["field"] = None
    if frame.eval_on_selector(selector, CONTROL_STATE_SCRIPT)["invalid"]:
        _escalate(result, label, "client_validation_failed", context)
        return "escalated"
    return "filled"


def _skip_or_escalate(result, label, reason, required, context) -> str:
    if required:
        _escalate(result, label, reason, context)
        return "escalated"
    result.fields_skipped.append({"label": label, "reason": reason})
    return "skipped"


def _fill(frame, control, value, field=None) -> str | None:
    """Mechanical fill of an exact approved value. Never presses keys. Returns a failure reason or None."""
    kind = control["kind"]
    selector = f'[data-jobintel-control="{control["marker"]}"]'
    if kind == "select":
        option = next((o for o in control["options"] if o["text"] == value or o["value"] == value), None)
        if option is None:
            return "approved_value_not_offered_by_control"
        frame.select_option(selector, value=option["value"])
        return None
    if kind == "radio":
        option = next((o for o in control["options"] if normalise_label(o["text"]) == value.casefold()
                       or o["value"] == value), None)
        if option is None:
            return "approved_value_not_offered_by_control"
        frame.check(f'[data-jobintel-control="{option["marker"]}"]')
        return None
    if kind == "checkbox":
        if value.casefold() not in {"yes", "no", "true", "false"}:
            return "approved_value_not_offered_by_control"
        frame.set_checked(selector, value.casefold() in {"yes", "true"})
        return None
    if kind in DATE_FORMATS:
        if not DATE_FORMATS[kind].fullmatch(value):
            return "date_value_format_mismatch"
        frame.fill(selector, value)
        return None if frame.eval_on_selector(selector, "el => el.value") == value else "date_value_rejected_by_control"
    if kind == "combobox" and field == "location":
        return _fill_location_combobox(frame, selector, value)
    if kind == "combobox":
        return _fill_combobox(frame, selector, value)
    frame.fill(selector, value)
    return None


def _fill_combobox(frame, selector, value) -> str | None:
    """Open the combobox, click the single option whose visible text is exactly the value."""
    frame.click(selector)
    outcome = "listbox_not_open"
    for _ in range(10):
        outcome = frame.eval_on_selector(selector, f"el => ({COMBOBOX_OPTION_SCRIPT})([el, {json.dumps(value)}])")
        if outcome != "listbox_not_open":
            break
        frame.wait_for_timeout(SETTLE_MS)
    if outcome != "ok":
        return f"combobox_{outcome}"
    marker = frame.eval_on_selector(selector, "el => el.getAttribute('data-jobintel-control')")
    frame.click(f'[data-jobintel-option="{marker}"]')
    frame.wait_for_timeout(SETTLE_MS)
    if frame.eval_on_selector(selector, "el => el.value.trim()") != value:
        return "combobox_selection_not_reflected"
    return None


LOCATION_WAIT_MS = 8000


def _fill_location_combobox(frame, selector, value) -> str | None:
    """Location autocomplete: type the city (the site's own suggestion READ), then click the single
    suggestion that is exactly the approved "<city>, <country>" or starts with the city and ends with
    the country. None or several such suggestions -> escalate; nothing is guessed."""
    if "," not in value:
        return "location_value_not_city_and_country"
    frame.fill(selector, value.split(",")[0].strip())
    outcome, waited = "listbox_not_open", 0
    while waited <= LOCATION_WAIT_MS:
        outcome = frame.eval_on_selector(selector, f"el => ({LOCATION_OPTION_SCRIPT})([el, {json.dumps(value)}])")
        if outcome.startswith("ok:") or outcome in {"ambiguous_option", "no_listbox_reference"}:
            break
        frame.wait_for_timeout(CV_UPLOAD_POLL_MS)
        waited += CV_UPLOAD_POLL_MS
    if not outcome.startswith("ok:"):
        return f"location_{outcome}"
    chosen = outcome[3:]
    marker = frame.eval_on_selector(selector, "el => el.getAttribute('data-jobintel-control')")
    frame.click(f'[data-jobintel-option="{marker}"]')
    frame.wait_for_timeout(SETTLE_MS)
    if frame.eval_on_selector(selector, "el => el.value.trim()") != chosen:
        return "location_selection_not_reflected"
    return None


def _is_cv_control(control) -> bool:
    return any(term in normalise_label(control["label"]) for term in CV_LABEL_TERMS)


def _defer_cv_to_human(control, result) -> str:
    """Live host: never touch the file input. Record a safe marker; the human uploads in handoff."""
    result.cv_upload_status = "human_required"
    if not result.cv_control:
        result.cv_control = _control_key(control)
    return "human_required"


def _handle_file(frame, bundle, control, result, context) -> str:
    label = control["label"]
    if not _is_cv_control(control):
        return _skip_or_escalate(result, label, "unknown_file_upload", control["required"], context)
    expected = bundle.request["cv"]["sha256"]
    data = bundle.cv_path.read_bytes()
    local_sha = hashlib.sha256(data).hexdigest()
    if local_sha != expected:
        _escalate(result, label, "sealed_cv_hash_mismatch", context)
        return "escalated"
    selector = f'[data-jobintel-control="{control["marker"]}"]'
    frame.set_input_files(selector, str(bundle.cv_path))
    selected = frame.eval_on_selector(selector, "el => [...el.files].map(f => ({name: f.name, size: f.size}))")
    ok = selected == [{"name": bundle.cv_path.name, "size": len(data)}]
    result.cv_upload = {"label": label, "file": bundle.cv_path.name, "sha256_verified_before_selection": True,
                        "sealed_sha256": local_sha, "dom_selected_files": selected, "dom_matches_sealed_file": ok,
                        "remote_integrity_verified": False, "human_confirmation_required": True}
    if not ok:
        _escalate(result, label, "cv_selection_not_reflected_in_dom", context)
        result.cv_upload_status = "escalated"
        return "escalated"
    result.cv_upload_status = "selected"
    return "filled"


FILENAME_VISIBLE_SCRIPT = "(name) => (document.body ? document.body.innerText : '').includes(name)"


def _upload_cv(page, frame, bundle, control, result, context, window, scans) -> str:
    """Upload the sealed, approved CV into exactly one plain Resume/CV control, inside the
    CV-upload window. 'filled' once the page visibly shows the uploaded file; otherwise escalated.
    Any write the window does not allow is blocked and stops the run (fatal) at the next check."""
    policy = window["policy"]
    label = normalise_label(control["label"])
    if not result.cv_control:
        result.cv_control = _control_key(control)

    def refuse(reason):
        _escalate(result, control["label"], reason, context)
        result.cv_upload_status = "escalated"
        return "escalated"

    if result.cv_upload:
        return refuse("cv_upload_second_control")
    if label not in CV_UPLOAD_LABELS or any(term in label for term in CV_UPLOAD_EXCLUDED_TERMS):
        return refuse("cv_upload_label_not_exact")
    cv_like = [c for _, snapshot in scans for c in snapshot["controls"] if c["kind"] == "file" and _is_cv_control(c)]
    if len(cv_like) != 1:
        return refuse("cv_upload_ambiguous_control")
    data = bundle.cv_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != bundle.request["cv"]["sha256"] or digest != policy.sha256:
        return refuse("sealed_cv_hash_mismatch")
    selector = f'[data-jobintel-control="{control["marker"]}"]'
    result.cv_upload = {"artifact_id": policy.artifact_id, "file_name": policy.filename,
                        "sha256_verified_before_selection": True, "visible_in_page": False,
                        "remote_integrity_verified": False, "steps": {}}
    window["field"], window["cv"], window["cv_bytes"], window["cv_flow"] = "cv", True, data, []
    window["cv_storage_tokens"] = set()

    def visible():
        try:
            return bool(frame.evaluate(FILENAME_VISIBLE_SCRIPT, policy.filename))
        except Exception:
            return False

    def observe():
        steps = _cv_flow_steps(window["cv_flow"])
        steps["visible_in_page"] = result.cv_upload["visible_in_page"] = (
            result.cv_upload["visible_in_page"] or visible())
        result.cv_upload["steps"] = steps
        if steps["upload_handle_created"] and result.cv_upload_status == "selected":
            result.cv_upload_status = "upload_handle_created"
        return steps

    try:
        frame.set_input_files(selector, files=[{"name": policy.filename, "mimeType": "application/pdf", "buffer": data}])
        result.cv_upload_status = "selected"
        waited = 0
        while True:
            _guard_tripped(page, result)  # an unauthorized upload/storage/final write is fatal here
            steps = observe()
            if steps["form_associated"] and steps["visible_in_page"]:
                break
            if waited >= CV_UPLOAD_WAIT_MS:
                break
            page.wait_for_timeout(CV_UPLOAD_POLL_MS)
            waited += CV_UPLOAD_POLL_MS
        page.wait_for_timeout(window.get("settle_ms") or SETTLE_MS)  # the field's own autosave, still in-window
        _guard_tripped(page, result)
    except _Stop:
        observe()  # a write inside the window was blocked: say so, never "not_seen"
        result.cv_upload_status = "blocked"
        raise
    finally:
        window["field"], window["cv"], window["cv_bytes"] = None, False, None
    steps = observe()
    steps["responses_ok"] = _cv_flow_responses_ok(window["cv_flow"])
    result.cv_upload["steps"] = steps
    # A visible file name alone never counts: the whole approved flow must have succeeded, in order.
    if not (steps["form_associated"] and steps["visible_in_page"] and steps["responses_ok"]):
        return refuse("cv_upload_not_confirmed")
    result.cv_upload_status = "uploaded"
    return "filled"


def _cv_flow_steps(flow) -> dict:
    """Which steps of handle -> storage -> form-value write happened, in that order."""
    kinds = [kind for kind, _ in flow]
    handle = kinds.index("handle") if "handle" in kinds else None
    storage = next((i for i, k in enumerate(kinds) if k == "storage" and handle is not None and i > handle), None)
    associate = next((i for i, k in enumerate(kinds) if k == "associate" and storage is not None and i > storage), None)
    return {"upload_handle_created": handle is not None, "storage_written": storage is not None,
            "form_associated": associate is not None}


_TOKEN_MIN, _TOKEN_MAX = 8, 512
_MULTIPART_FIELD = re.compile(rb'name="[^"\r\n]{1,100}"\r\n\r\n([^\r\n]{8,512})\r\n')


def _multipart_tokens(body: bytes | None) -> set:
    """Text form-field values of a multipart storage upload (e.g. its object key); never the file."""
    out = set()
    for value in _MULTIPART_FIELD.findall(body or b""):
        try:
            out.add(value.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return out


def _json_tokens(value, out: set) -> set:
    if isinstance(value, dict):
        for item in value.values():
            _json_tokens(item, out)
    elif isinstance(value, list):
        for item in value:
            _json_tokens(item, out)
    elif isinstance(value, str) and _TOKEN_MIN <= len(value) <= _TOKEN_MAX:
        out.add(value)
    return out


def _upload_tokens(window, policy) -> set:
    """Identifiers of THIS window's upload: string values of the upload-handle answer and of the
    storage form fields -- minus generic ones (URLs, the MIME type, the file name, whitespace)."""
    tokens = set(window.get("cv_storage_tokens") or ())
    for kind, request in window["cv_flow"]:
        if kind == "handle":
            try:
                _json_tokens(request.response().json(), tokens)
            except Exception:
                pass
    return {t for t in tokens if not t.casefold().startswith(("http:", "https:")) and " " not in t
            and t not in {"application/pdf", policy.filename}}


def _cv_association_problem(window, body, policy) -> str | None:
    """None when the file-association mutation may pass: this window's handle then storage write both
    answered 2xx (GraphQL without errors), nothing was associated yet, and the mutation names this upload."""
    flow = window["cv_flow"]
    steps = _cv_flow_steps(flow)
    if not (steps["upload_handle_created"] and steps["storage_written"]):
        return "upload_not_complete"
    if steps["form_associated"] or any(kind == "associate" for kind, _ in flow):
        return "already_associated"
    if not _cv_flow_responses_ok(flow):
        return "upload_step_failed"
    tokens = _upload_tokens(window, policy)
    if not isinstance(body, str) or not tokens or not any(token in body for token in tokens):
        return "not_this_upload"
    return None


def _cv_flow_responses_ok(flow) -> bool:
    """Every allowed request of the upload flow got a 2xx answer (and GraphQL ones no errors)."""
    if not flow:
        return False
    for kind, request in flow:
        try:
            response = request.response()
            if response is None or not 200 <= response.status < 300:
                return False
            if kind in {"handle", "associate"}:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("errors"):
                    return False
        except Exception:
            return False
    return True


def _is_final(action) -> bool:
    return action["type"] in {"submit", "image"} or _looks_final(action["text"]) or _looks_final(action["raw_href"])


def _is_safe_advance(action, host) -> bool:
    """Exact Next/Continue that cannot submit a form: a type=button <button>, a custom
    role=button element (not a native button/input/link), or a same-host https link."""
    if normalise_label(action["text"]) not in SAFE_ADVANCE_TEXTS or _is_final(action):
        return False
    if action["tag"] == "button":
        return action["type"] == "button"
    if action["tag"] == "a":
        if action["raw_href"] in {"", "#"} and action["role"] == "button":
            return True  # script-driven custom control, same as a role=button element
        parts = urlsplit(action["href"])
        return (parts.scheme == "https" and parts.hostname == host and action["target"] in {"", "_self"}
                and not parts.username and not parts.password)
    return action["tag"] != "input" and action["role"] == "button"


def _next_action(scans, bundle, result):
    """(frame, the single safe advance control), (None, None) on a final page; stops when unsure."""
    host = urlsplit(bundle.application_url).hostname
    safe, final, other = [], [], []
    for frame, snapshot in scans:
        for action in snapshot["actions"]:
            if _is_safe_advance(action, host):
                safe.append((frame, action))
            elif _is_final(action):
                final.append(action)
            else:
                other.append(action)
    result.final_submit_seen = result.final_submit_seen or bool(final)
    if len(safe) > 1:
        raise _Stop("needs_human_input", "ambiguous_advance_controls")
    if safe:
        return safe[0]
    if any(normalise_label(a["text"]) in SAFE_ADVANCE_TEXTS for a in final + other):
        # An advance-looking control that is (or may be) a submit or off-host: unsure, so stop.
        raise _Stop("needs_human_input", "advance_control_not_provably_non_submit")
    if final:
        return None, None
    raise _Stop("needs_human_input", "no_safe_navigation")


def _action_class(action, host) -> str:
    if _is_safe_advance(action, host):
        return "safe_advance"
    if _is_final(action):
        return "final"
    if normalise_label(action["text"]) in SAFE_ADVANCE_TEXTS:
        return "advance_like"
    return "other"


def _render_fingerprint(scans, host) -> tuple:
    """Non-sensitive STRUCTURE of the decision surface, in DOM order, per frame (scheme/host/path):
    each supported control's kind, normalised label, name attribute, required flag and option
    count; each decision action's tag/type/role, normalised text and classification (safe
    advance / final / advance-looking). Never a value, option value, validity or typed answer.
    Disabled controls are never discovered, so enabling one changes the fingerprint. Kept in
    memory only."""
    frames = []
    for frame, snapshot in scans:
        parts = urlsplit(frame.url)
        controls = tuple((c["kind"], normalise_label(c.get("label", "")), c.get("name", ""), bool(c.get("required")),
                          len(c.get("options") or ())) for c in snapshot["controls"])
        actions = tuple((a["tag"], a["type"], a["role"], normalise_label(a["text"]), cls)
                        for a in snapshot["actions"] for cls in (_action_class(a, host),) if cls != "other")
        if controls or actions:
            frames.append((f"{parts.scheme}://{parts.hostname}{parts.path}", controls, actions))
    return tuple(frames)


def _wait_for_render(page, bundle, result, *, previous=None):
    """Bounded wait for a stable render surface before any decision about the page: the SAME
    non-empty structural fingerprint (see _render_fingerprint) on two consecutive polls
    RENDER_POLL_MS apart. Every poll first runs the full guard check (blocked writes incl.
    unknown/final mutations, blocked in-page submits, host drift), so nothing is relaxed while
    waiting; nothing is clicked or filled. `previous` (after an advance): the fingerprint of the
    step clicked away from -- the new stable fingerprint must differ from it. Returns the stable
    scans, or stops with form_not_rendered / advance_had_no_effect / form_render_unstable."""
    host = urlsplit(bundle.application_url).hostname
    last, waited = None, 0
    while True:
        _guard_tripped(page, result)
        scans = _scan(page, bundle, result)
        fingerprint = _render_fingerprint(scans, host)
        current = fingerprint if fingerprint and fingerprint != previous else None
        if current is not None and current == last:
            return scans
        last = current
        if waited >= RENDER_WAIT_MS:
            break
        page.wait_for_timeout(RENDER_POLL_MS)
        waited += RENDER_POLL_MS
    if previous is not None and fingerprint == previous:
        raise _Stop("needs_human_input", "advance_had_no_effect")
    if not fingerprint:
        raise _Stop("needs_human_input", "form_not_rendered")
    raise _Stop("needs_human_input", "form_render_unstable")


def _guard_tripped(page, result):
    _live_check(page, result)
    for frame in page.frames:
        if frame.is_detached():
            continue
        blocked = frame.evaluate("() => [...(window.__jobintelBlockedSubmits || [])]")
        result.blocked_submissions.extend(b for b in blocked if b not in result.blocked_submissions)
    if result.blocked_submissions or result.blocked_requests:
        raise _Stop("needs_human_input", "submission_attempt_blocked")


def _finish(page, result):
    result.final_submit_clicked = False
    try:
        result.current_url = _without_query(page.url) if result.live_host else page.url
        title = page.title()
    except Exception:
        title = ""
    result.evidence = [{
        "kind": "screen_state",
        "value": (f"Stopped on {result.current_url} (title '{title}') with status {result.status}: "
                  f"{result.stop_reason}. Final submit control seen: {result.final_submit_seen}; not pressed."),
        "sha256": "", "detail": "observed by offline runner via DOM inspection",
    }]


def form_diagnostics(result: RunResult) -> dict:
    """Safe, persistable form progress: control keys, canonical fields filled, labels/reasons of
    skipped and escalated controls. Never a candidate value, variable or request body."""
    return {
        "controls_seen_count": result.controls_seen,
        "controls_detected": [dict(c) for c in result.controls_detected],
        "fields_filled": [{"field": f["field"], "page_label": normalise_label(f["page_label"])}
                          for f in result.fields_filled],
        "fields_skipped": [{"label": normalise_label(f["label"]), "reason": f["reason"]} for f in result.fields_skipped],
        "fields_escalated": [{"label": normalise_label(q["question"]),
                              "reason": q["screenshot_or_evidence"].removeprefix("runner:")}
                             for q in result.questions_escalated],
        "cv_control_detected": bool(result.cv_control) or bool(result.cv_upload),
        "cv_control": dict(result.cv_control),
        "cv_upload_status": result.cv_upload_status,
        "cv_upload_attempted": bool(result.cv_upload),
        "cv_artifact_id": result.cv_upload.get("artifact_id"),
        "cv_upload_visible_in_page": result.cv_upload.get("visible_in_page"),
        "cv_upload_steps": dict(result.cv_upload.get("steps") or {}),
        "advance_clicks": len(result.advance_clicks),
        "stop_reason": result.stop_reason,
    }


def identity_state(result: RunResult) -> str:
    """verified / mismatch for a checked start page; not_observed if the run stopped first."""
    if not result.start_identity:
        return "not_observed"
    return "verified" if result.start_identity.get("state") == "matched" else "mismatch"


def to_stop_only_report(result: RunResult, bundle: SealedBundle, *, task_id: str, executor: str = EXECUTOR) -> dict:
    """Shape the local evidence as a shared stop-only report (executor "browserbase").

    cv_upload_confirmed is true only with observable form evidence: the native file input
    shows exactly the sealed file (name and size) selected after the local SHA-256 check.
    No remote integrity is claimed. final_submit_clicked is copied, never forced, so the
    shared importer rejects any run that reports a click."""
    request = bundle.request
    identity = result.start_identity or result.page_identity
    return {
        "request_id": request["request_id"], "approval_id": request["approval_id"],
        "package_hash": request["package_hash"], "application_url": request["application_url"],
        "cv_artifact_id": request["cv"]["artifact_id"], "cv_sha256": request["cv"]["sha256"],
        "observed_company": bundle.company if identity.get("company_seen") else (identity.get("title") or "unknown"),
        "observed_role": bundle.role if identity.get("role_seen") else (identity.get("title") or "unknown"),
        "observed_url": identity.get("url") or "unknown",
        "task_id": task_id, "executor": executor, "execution_mode": stop_only.MODE,
        "status": result.status, "final_submit_clicked": result.final_submit_clicked,
        "cv_upload_confirmed": (result.cv_upload.get("dom_matches_sealed_file") is True
                                or (result.cv_upload_status == "uploaded" and result.cv_upload.get("visible_in_page") is True)),
        "duplicate_detected": False,
        "fields_filled": [{"field": f["field"], "value": f["value"], "page_label": normalise_label(f["page_label"])}
                          for f in result.fields_filled],
        "questions_escalated": list(result.questions_escalated) + _stop_escalation(result) + _cv_human_escalation(result),
        "evidence": list(result.evidence),
    }


def _cv_human_escalation(result: RunResult) -> list:
    """The CV left for the human (live host) is reported as one escalation, so the imported result
    is needs_human_input with an explicit reason instead of a reviewable final state."""
    if result.cv_upload_status != "human_required":
        return []
    return [{"status": "needs_human_input", "question": CV_HUMAN_REQUIRED,
             "page_context": f"control: {result.cv_control.get('label', '')}"[:200], "allowed_known_answers": [],
             "screenshot_or_evidence": "runner:cv_upload_human_required"}]


def _stop_escalation(result: RunResult) -> list:
    """A runner-level stop (blocked write, host drift, no controls, runner error, ...) is reported as
    an explicit escalation, so the importer's generic fallbacks (e.g. cv_upload_unconfirmed) can
    never stand in for the real reason. Field-level escalations already carry their own reasons."""
    if result.status == "ready_for_human_review" or not result.stop_reason or result.stop_reason == "questions_escalated":
        return []
    return [{"status": "needs_human_input", "question": f"Runner stopped: {result.stop_reason}",
             "page_context": f"pages visited: {result.pages_visited}", "allowed_known_answers": [],
             "screenshot_or_evidence": f"runner:{result.stop_reason}"[:200]}]
