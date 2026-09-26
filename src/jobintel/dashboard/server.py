from __future__ import annotations

import html
from hashlib import sha256
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jobintel
from jobintel.analysis.job_enrichment import enrich_job
from jobintel.application_lifecycle import ensure_application, migrate_legacy_workflow_statuses
from jobintel.config import load_ranking_config
from jobintel.connectors.base import ConnectorQuery
from jobintel.connectors.manual_source import ManualSourceConnector, ManualSourceSpec, prepare_single_url_import
from jobintel.dashboard import pages, render_utils
from jobintel.dashboard.operations import (
    build_action_queue,
    build_application_detail,
    build_company_health_view,
    build_cv_workbench,
    build_deadline_view,
    build_manual_watchlist_view,
    build_source_health_view,
    build_today_summary,
)
from jobintel.dashboard.service import (
    DashboardFilters,
    build_dashboard_model,
    build_job_detail_model,
    filters_to_params,
    filters_from_params,
    save_search,
    update_workflow_status,
)
from jobintel.matching.cv_generation import generate_and_save_cv, resolve_cv_pdf
from jobintel.models.taxonomy import ApplicationStatus, LocationMode, SponsorshipFilterMode, WorkflowStatus
from jobintel.pipeline.daily import run_daily_refresh
from jobintel.pipeline.ingestion import ingest_from_connectors
from jobintel.pipeline.refresh import refresh_sources
from jobintel.profile_ingestion import load_candidate_profile
from jobintel.storage.application_store import ApplicationStore
from jobintel.storage.cv_artifact_store import CVArtifactStore
from jobintel.storage.local_store import LocalJobStore
from jobintel.storage.watchlist_store import WatchlistStore


def run_dashboard(host: str = "127.0.0.1", port: int = 8765, store_root: str = "data/local", config_path: str = "config/personal_strategy.json", registry_path: str = "config/target_companies.json") -> None:
    handler = _handler(store_root, config_path, registry_path)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Dashboard running at http://{host}:{port}")
    server.serve_forever()


def _source_fingerprint() -> str:
    """Identity of the jobintel source currently on disk (paths, sizes, mtimes)."""
    digest = sha256()
    for path in sorted(Path(jobintel.__file__).parent.rglob("*.py")):
        stat = path.stat()
        digest.update(f"{path}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def _handler(store_root: str, config_path: str, registry_path: str):
    # A long-running dashboard keeps the modules it imported at start-up. If the
    # source changes afterwards, generating a CV would silently persist a new
    # immutable version built by the OLD code (a real dogfood failure), so CV
    # generation refuses until the dashboard is restarted.
    code_fingerprint = _source_fingerprint()
    store = LocalJobStore(store_root)
    config = load_ranking_config(config_path)
    application_store = ApplicationStore(store.root)
    cv_store = CVArtifactStore(store.root)
    watchlist_store = WatchlistStore(store.root)
    refresh_lock = threading.Lock()
    migrate_legacy_workflow_statuses(store, application_store)

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            params = _single_value_params(parsed.query)
            lang = params.get("lang", "en")

            if parsed.path == "/":
                # Bare "/" opens Today; "/?<filters>" (saved searches, preset
                # links, the filter form, refresh redirects) is an inbox query
                # and must keep its filters rather than landing on Today.
                has_filters = any(key != "lang" for key in params)
                self._redirect(f"/inbox?{parsed.query}" if has_filters else f"/today?lang={_q(lang)}")
                return
            if parsed.path == "/today":
                summary = build_today_summary(store, application_store, cv_store, config, registry_path)
                self._send_html(pages.render_today(summary, lang, already_running=params.get("refresh") == "already_running"))
                return
            if parsed.path == "/inbox":
                model = build_dashboard_model(store=store, config=config, filters=filters_from_params(params), lang=lang)
                self._send_html(_render_dashboard(model))
                return
            if parsed.path == "/job":
                job_id = params.get("id")
                if not job_id:
                    self.send_error(400, "Missing job id")
                    return
                try:
                    model = build_job_detail_model(job_id, store=store, config=config, lang=lang)
                except KeyError:
                    self.send_error(404, "Job not found")
                    return
                self._send_html(_render_detail(model))
                return
            if parsed.path == "/priority":
                items = build_action_queue(store, application_store, cv_store, config)
                self._send_html(pages.render_priority_queue(items, lang))
                return
            if parsed.path == "/deadlines":
                buckets = build_deadline_view(store, config)
                self._send_html(pages.render_deadlines(buckets, lang))
                return
            if parsed.path == "/cv":
                job_id = params.get("job_id")
                if not job_id:
                    self.send_error(400, "Missing job_id")
                    return
                view = build_cv_workbench(job_id, store, cv_store, application_store)
                self._send_html(pages.render_cv_workbench(view, lang))
                return
            if parsed.path == "/cv/pdf":
                self._send_cv_pdf(params.get("id", ""))
                return
            if parsed.path == "/applications":
                applications = application_store.read_applications()
                status_filter = params.get("status")
                if status_filter:
                    applications = [item for item in applications if item.status.value == status_filter]
                applications.sort(key=lambda item: item.updated_at or item.discovered_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
                self._send_html(pages.render_application_list(applications, lang))
                return
            if parsed.path == "/application":
                application_id = params.get("id")
                if not application_id:
                    self.send_error(400, "Missing application id")
                    return
                try:
                    detail = build_application_detail(application_id, store, application_store, cv_store)
                except KeyError:
                    self.send_error(404, "Application not found")
                    return
                self._send_html(pages.render_application_detail(detail, lang))
                return
            if parsed.path == "/watchlist":
                items = build_manual_watchlist_view(store, registry_path, watchlist_store)
                self._send_html(pages.render_watchlist(items, lang))
                return
            if parsed.path == "/manual-import":
                self._send_html(pages.render_manual_import_form(lang))
                return
            if parsed.path == "/health":
                source_rows = build_source_health_view(store, registry_path)
                company_summary = build_company_health_view(registry_path)
                self._send_html(pages.render_health(source_rows, company_summary.counts, company_summary.total, lang))
                return
            self.send_error(404, "Not found")

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query_params = _single_value_params(parsed.query)
            lang = query_params.get("lang", "en")
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            params = _single_value_params(body) if parsed.path != "/refresh" else query_params

            if parsed.path == "/refresh":
                if not _run_dashboard_refresh(store, config, registry_path, refresh_lock):
                    self._redirect(_refresh_redirect(lang, already_running=True))
                    return
                self._redirect(_refresh_redirect(lang))
                return
            if parsed.path == "/daily-refresh":
                if not refresh_lock.acquire(blocking=False):
                    self._redirect(f"/today?lang={_q(lang)}&refresh=already_running")
                    return
                try:
                    run_daily_refresh(store=store, config=config, registry_path=registry_path)
                finally:
                    refresh_lock.release()
                self._redirect(f"/today?lang={_q(lang)}")
                return
            if parsed.path == "/saved-search":
                redirect = _save_dashboard_search(params, store)
                if redirect is None:
                    self.send_error(400, "Missing saved search name")
                    return
                self._redirect(redirect)
                return
            if parsed.path == "/workflow":
                job_id = params.get("job_id")
                status = params.get("status")
                if not job_id or status not in {item.value for item in WorkflowStatus}:
                    self.send_error(400, "Invalid workflow update")
                    return
                try:
                    update_workflow_status(job_id, status, store)
                except KeyError:
                    self.send_error(404, "Job not found")
                    return
                except ValueError as exc:
                    self.send_error(409, str(exc))
                    return
                self._redirect(_safe_return_to(params.get("return_to")))
                return
            if parsed.path == "/cv/generate":
                if _source_fingerprint() != code_fingerprint:
                    self.send_error(409, "Dashboard code changed since it was started -- restart the dashboard before generating a CV")
                    return
                job_id = params.get("job_id")
                if not job_id:
                    self.send_error(400, "Missing job_id")
                    return
                jobs = {job.id: job for job in store.read_jobs()}
                job = jobs.get(job_id)
                if job is None:
                    self.send_error(404, "Job not found")
                    return
                job = enrich_job(job, config.candidate_graduation_year)
                candidate = load_candidate_profile(store.root)
                if candidate is None:
                    self.send_error(400, "No candidate profile found -- run profile-rebuild first")
                    return
                generate_and_save_cv(candidate, job, cv_store, store_root=store.root)
                self._redirect(f"/cv?job_id={_q(job_id)}&lang={_q(lang)}")
                return
            if parsed.path == "/cv/attach":
                job_id = params.get("job_id")
                cv_version_id = params.get("cv_version_id")
                if not job_id or not cv_version_id:
                    self.send_error(400, "Missing job_id or cv_version_id")
                    return
                application = application_store.for_job(job_id)
                if application is None:
                    self.send_error(400, "No application exists for this job yet -- create one first")
                    return
                artifact = cv_store.get(cv_version_id)
                if artifact is None or artifact.job_id != job_id:
                    self.send_error(400, "Unknown CV version for this job")
                    return
                try:
                    application_store.attach_cv(application.id, cv_version_id, artifact.selected_project_ids, artifact.evidence_ids_used)
                except ValueError as exc:
                    self.send_error(409, str(exc))
                    return
                self._redirect(f"/cv?job_id={_q(job_id)}&lang={_q(lang)}")
                return
            if parsed.path == "/application/submit":
                # "I have actually submitted this application, with this exact
                # CV version" -- the one explicit human action that hands the
                # job over to ApplicationStore's lifecycle and freezes the CV.
                job_id = params.get("job_id")
                cv_version_id = params.get("cv_version_id") or None
                job = next((item for item in store.read_jobs() if item.id == job_id), None) if job_id else None
                if job is None:
                    self.send_error(404, "Job not found")
                    return
                if cv_version_id and params.get("reviewed") != "yes":
                    # The human-review gate: a generated CV is always a draft
                    # until the user confirms they reviewed that exact version.
                    self.send_error(400, "Confirm you have reviewed this exact CV version before marking it submitted")
                    return
                artifact = cv_store.get(cv_version_id) if cv_version_id else None
                if cv_version_id and (artifact is None or artifact.job_id != job_id):
                    self.send_error(400, "Unknown CV version for this job")
                    return
                application = ensure_application(job, application_store)
                try:
                    if artifact is not None:
                        application_store.attach_cv(application.id, artifact.id, artifact.selected_project_ids, artifact.evidence_ids_used)
                    # No cv_version_id = a manual application without a system
                    # CV: recorded as exactly that, never inferred from a draft.
                    application = application_store.update_status(application.id, ApplicationStatus.APPLIED, note=params.get("note", ""), submitted_cv_version_id=artifact.id if artifact is not None else None)
                except ValueError as exc:
                    self.send_error(409, str(exc))
                    return
                self._redirect(f"/application?id={_q(application.id)}&lang={_q(lang)}")
                return
            if parsed.path == "/application/create":
                job_id = params.get("job_id")
                job = next((item for item in store.read_jobs() if item.id == job_id), None) if job_id else None
                if job is None:
                    self.send_error(404 if job_id else 400, "Job not found" if job_id else "Missing job_id")
                    return
                # Idempotent: at most one application per canonical vacancy.
                application = ensure_application(job, application_store)
                self._redirect(f"/application?id={_q(application.id)}&lang={_q(lang)}")
                return
            if parsed.path == "/application/status":
                application_id = params.get("application_id")
                status = params.get("status")
                if not application_id or status not in {item.value for item in ApplicationStatus}:
                    self.send_error(400, "Invalid application status update")
                    return
                try:
                    application_store.update_status(application_id, ApplicationStatus(status), note=params.get("note", ""))
                except ValueError as exc:
                    self.send_error(409, str(exc))
                    return
                self._redirect(f"/application?id={_q(application_id)}&lang={_q(lang)}")
                return
            if parsed.path == "/watchlist/check":
                target = params.get("target")
                if not target:
                    self.send_error(400, "Missing target")
                    return
                watchlist_store.record_check(target, params.get("result", ""), notes=params.get("notes", ""))
                self._redirect(f"/watchlist?lang={_q(lang)}")
                return
            if parsed.path == "/watchlist/snooze":
                target = params.get("target")
                if not target:
                    self.send_error(400, "Missing target")
                    return
                days = int(params.get("days") or 7)
                watchlist_store.snooze(target, datetime.now(timezone.utc) + timedelta(days=days))
                self._redirect(f"/watchlist?lang={_q(lang)}")
                return
            if parsed.path == "/manual-import":
                url = params.get("url", "").strip()
                if not url:
                    self._send_html(pages.render_manual_import_form(lang, {"error": "A URL is required"}))
                    return
                source_name = params.get("source_name") or "manual"
                overrides = {key: params[key] for key in ("title", "company", "location", "deadline") if params.get(key)}
                prepared = prepare_single_url_import(url, overrides)
                result = {
                    "url": prepared.url,
                    "status": prepared.status,
                    "auto_fetched_fields": ", ".join(prepared.auto_fetched_fields) or "(none)",
                    "note": prepared.fetch_error or "",
                }
                if prepared.status == "missing_required_fields":
                    self._send_html(pages.render_manual_import_form(lang, result))
                    return
                spec = ManualSourceSpec(source_name=source_name, display_name=source_name, is_source_url=lambda _url: True)
                connector = ManualSourceConnector(spec, (prepared.record,))
                jobs = ingest_from_connectors([connector], query=ConnectorQuery(keywords=(), location="United Kingdom", limit=1), store=store, config=config)
                result["title"] = prepared.record.get("title", "")
                result["company"] = prepared.record.get("company", "")
                result["canonical_job_id"] = jobs[0].id if jobs else "(merged into an existing job -- see the inbox)"
                self._send_html(pages.render_manual_import_form(lang, result))
                return
            self.send_error(404, "Not found")

        def _send_cv_pdf(self, cv_version_id: str) -> None:
            pdf = resolve_cv_pdf(cv_store, cv_version_id)
            if pdf is None:
                self.send_error(404, "Unknown CV version or PDF not available")
                return
            path, artifact = pdf
            data = path.read_bytes()
            if artifact.pdf_sha256 and sha256(data).hexdigest() != artifact.pdf_sha256:
                # The file on disk is no longer the PDF that was generated (and
                # possibly submitted) -- never serve it as if it were.
                self.send_error(409, "CV PDF on disk does not match the recorded checksum for this version")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'inline; filename="{artifact.id}.pdf"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return DashboardHandler


def _safe_return_to(value: str | None) -> str:
    """Only same-site relative paths -- never an open redirect."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/inbox"


def _run_dashboard_refresh(store: LocalJobStore, config, registry_path: str, refresh_lock: threading.Lock) -> bool:
    if not refresh_lock.acquire(blocking=False):
        return False
    try:
        refresh_sources(registry_path=registry_path, store=store, config=config, limit=100)
    finally:
        refresh_lock.release()
    return True


def _refresh_redirect(lang: str, already_running: bool = False) -> str:
    params = {"lang": lang}
    if already_running:
        params["refresh"] = "already_running"
    else:
        params["freshness_window"] = "new_since_last_refresh"
    return "/?" + urllib.parse.urlencode(params)


def _save_dashboard_search(params: dict[str, str], store: LocalJobStore) -> str | None:
    params = dict(params)
    name = params.pop("name", "").strip()
    if not name:
        return None
    lang = params.pop("lang", "en")
    filters = filters_from_params(params)
    save_search(name, filters, store)
    redirect_params = filters_to_params(filters)
    redirect_params["lang"] = lang
    return "/?" + urllib.parse.urlencode(redirect_params)


def _render_dashboard(model: dict[str, Any]) -> str:
    t = model["labels"]
    lang = model["lang"]
    filters: DashboardFilters = model["filters"]
    return_params = filters_to_params(filters)
    return_params["lang"] = lang
    cards = "\n".join(_render_job_row(job, lang, return_params) for job in model["jobs"]) or f'<tr><td colspan="13">{_h(t["empty_state"])}</td></tr>'
    return f"""<!doctype html>
<html lang="{_h(lang)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_h(t["app_title"])}</title>
  <style>{_css()}</style>
</head>
<body>
  {render_utils.nav_bar(lang, "inbox")}
  <header>
    <div>
      <h1>{_h(t["daily_shortlist"])}</h1>
      <p>{_h(t["english_summary"])} / {_h(t["chinese_summary"])}</p>
    </div>
    <nav><a href="{_language_url(filters, "en")}">EN</a><a href="{_language_url(filters, "zh")}">中文</a></nav>
  </header>
  <section class="metrics">
    <div><strong>{model["counts"]["active_jobs"]}</strong><span>{_h(t["active_jobs"])}</span></div>
    <div><strong>{model["counts"]["new_today"]}</strong><span>{_h(t["new_today"])}</span></div>
    <div><strong>{model["refresh_summary"]["new_since_last_refresh"]}</strong><span>{_h(t["new_since_refresh"])}</span></div>
    <div><strong>{model["refresh_summary"]["changed"]}</strong><span>{_h(t["changed"])}</span></div>
    <div><strong>{model["refresh_summary"]["disappeared"]}</strong><span>{_h(t["disappeared"])}</span></div>
    <div><strong>{model["counts"]["high_priority"]}</strong><span>{_h(t["high_priority"])}</span></div>
    <div><strong>{model["counts"]["sponsor_positive"]}</strong><span>{_h(t["sponsor_likely"])}</span></div>
    <div><strong>{model["counts"]["london_jobs"]}</strong><span>{_h(t["london_jobs"])}</span></div>
    <div><strong>{model["counts"]["graduate_junior_associate"]}</strong><span>{_h(t["grad_junior"])}</span></div>
    <div><strong>{model["counts"]["wttj_jobs"]}</strong><span>{_h(t["wttj_jobs"])}</span></div>
    <div><strong>{model["counts"]["filtered"]}/{model["counts"]["total_ranked"]}</strong><span>{_h(t["filters"])}</span></div>
  </section>
  {_render_refresh_panel(t, model["refresh_summary"])}
  <form class="refresh-form" method="post" action="/daily-refresh?lang={_h(lang)}"><button type="submit">{_h(t["refresh_jobs"])}</button></form>
  <form class="refresh-form" method="post" action="/refresh?lang={_h(lang)}"><button type="submit" class="secondary">{_h(t["ats_only_refresh"])}</button> <small>{_h(t["ats_only_refresh_help"])}</small></form>
  <section class="presets" aria-label="{_h(t["presets"])}">
    {_preset_link("London Focus" if lang != "zh" else "伦敦优先", "london_focus", lang)}
    {_preset_link("UK-wide Search" if lang != "zh" else "英国范围", "uk_wide", lang)}
    {_preset_link("Sponsor First" if lang != "zh" else "签证优先", "sponsor_first", lang)}
    {_preset_link("All Opportunities" if lang != "zh" else "全部机会", "all_opportunities", lang)}
    {_preset_link(t["new_today"], "new_today", lang)}
    {_preset_link(t["wttj_jobs"], "wttj_jobs", lang)}
  </section>
  {_render_saved_searches(t, model["saved_searches"], lang)}
  {_render_source_health(t, model["source_health"])}
  {_render_filters(t, model["options"], filters, lang)}
  <main>
    <table>
      <thead>
        <tr>
          <th>{_h(t["job"])}</th><th>{_h(t["priority"])}</th><th>{_h(t["technical_fit"])}</th><th>{_h(t["seniority"])}</th>
          <th>{_h(t["sponsorship"])}</th><th>{_h(t["graduation"])}</th><th>{_h(t["tracks"])}</th><th>{_h(t["recommended_cv"])}</th>
          <th>{_h(t["workflow"])}</th><th>{_h(t["source"])}</th><th>{_h(t["dates"])}</th><th>{_h(t["evidence"])}</th><th>{_h(t["direct_apply"])}</th>
        </tr>
      </thead>
      <tbody>{cards}</tbody>
    </table>
  </main>
</body>
</html>"""


def _render_filters(t: dict[str, str], options: dict[str, list[str]], filters: DashboardFilters, lang: str) -> str:
    return f"""<form class="filters" method="get" action="/inbox">
  <input type="hidden" name="lang" value="{_h(lang)}">
  <label>{_h(t["search"])}<input name="search" value="{_h(filters.search or "")}" placeholder="backend, Python, London"></label>
  <label>{_h(t["sponsorship"])}{_select("sponsorship", [item.value for item in SponsorshipFilterMode], filters.sponsorship.value)}</label>
  <label>{_h(t["location"])}{_select("location_mode", [item.value for item in LocationMode], filters.location_mode.value)}</label>
  <label>{_h(t["role_track"])}{_select("role_track", [""] + options["role_tracks"], filters.role_track.value if filters.role_track else "")}</label>
  <label>{_h(t["seniority"])}{_select("seniority", [""] + options["seniorities"], filters.seniority or "")}</label>
  <label>{_h(t["freshness"])}{_select("freshness_window", options["freshness_windows"], filters.freshness_window or "")}</label>
  <label>{_h(t["freshness"])}<input name="freshness_days" value="{_h(str(filters.freshness_days or ""))}" placeholder="days"></label>
  <label>{_h(t["company"])}<input name="company" value="{_h(filters.company or "")}"></label>
  <label>{_h(t["source"])}{_source_select("source", [""] + options["sources"], filters.source or "")}</label>
  <label>{_h(t["enrichment_state"])}{_select("enrichment_state", [""] + options["enrichment_states"], filters.enrichment_state or "")}</label>
  <label>{_h(t["workflow"])}{_workflow_filter_select("workflow_status", filters.workflow_status.value if filters.workflow_status else "", lang)}</label>
  <label class="check"><input type="checkbox" name="new_today" value="1" {"checked" if filters.new_today else ""}> {_h(t["new_today"])}</label>
  <label>Sort{_select("sort", options.get("sorts", ["priority"]), filters.sort)}</label>
  <button type="submit">{_h(t["apply_filters"])}</button>
</form>
<form class="save-search" method="post" action="/saved-search">
  <input type="hidden" name="lang" value="{_h(lang)}">
  {_hidden_filter_inputs(filters)}
  <label>{_h(t["view_name"])}<input name="name" placeholder="London Backend"></label>
  <button type="submit">{_h(t["save_view"])}</button>
</form>"""


def _render_refresh_panel(t: dict[str, str], summary: dict[str, Any]) -> str:
    states = summary.get("state_counts", {})
    return f"""<section class="refresh-summary" aria-label="{_h(t["refresh_summary"])}">
  <strong>{_h(t["refresh_summary"])}</strong>
  <span>{_h(t["last_success"])}: {_h(summary.get("last_successful_refresh", "never"))}</span>
  <span>{_h(t["started"])}: {_h(summary.get("started_at", "never"))}</span>
  <span>{_h(t["finished"])}: {_h(summary.get("finished_at", "never"))}</span>
  <span>{_h(t["seen"])}: {_h(summary.get("total_jobs_seen", 0))}</span>
  <span>{_state_label("NEW", t)} {_h(states.get("NEW", 0))}</span>
  <span>{_state_label("CHANGED", t)} {_h(states.get("CHANGED", 0))}</span>
  <span>{_state_label("UNCHANGED", t)} {_h(states.get("UNCHANGED", 0))}</span>
  <span>{_state_label("DISAPPEARED", t)} {_h(states.get("DISAPPEARED", 0))}</span>
  <span>{_state_label("REAPPEARED", t)} {_h(states.get("REAPPEARED", 0))}</span>
</section>"""


def _render_saved_searches(t: dict[str, str], searches: list[dict[str, Any]], lang: str) -> str:
    links = []
    for item in searches:
        params = dict(item.get("params") or {})
        params["lang"] = lang
        links.append(f'<a href="/?{_h(urllib.parse.urlencode(params))}">{_h(item.get("name", "Saved Search"))}</a>')
    return f'<section class="presets saved-searches" aria-label="{_h(t["saved_searches"])}"><strong>{_h(t["saved_searches"])}</strong>{"".join(links)}</section>'


def _render_source_health(t: dict[str, str], source_health: dict[str, dict[str, Any]]) -> str:
    rows = []
    for item in source_health.values():
        counts = item.get("state_counts") or {}
        error = f"<span>{_h(t['error'])}: {_h(item.get('last_error', ''))}</span>" if item.get("last_error") else ""
        warning = f'<span class="warning">&#9888; {_h(item.get("warning"))}</span>' if item.get("warning") else ""
        failed = item.get("failed_identifiers") or {}
        failed_span = f'<span class="warning">Coverage incomplete: {_h(", ".join(failed))}</span>' if failed else ""
        rows.append(
            f"<div><strong>{_h(item.get('label', 'Source'))}</strong>"
            f"<span>{_h(_source_status_label(item.get('status', 'unknown'), t))}</span>"
            f"<span>{_h(t['seen'])}: {_h(item.get('jobs_seen', 0))}</span>"
            f"<span>{_h(t['active'])}: {_h(item.get('jobs_active', 0))}</span>"
            f"<span>{_state_label('NEW', t)}: {_h(counts.get('NEW', 0))}</span>"
            f"<span>{_state_label('CHANGED', t)}: {_h(counts.get('CHANGED', 0))}</span>"
            f"<span>{_h(t['last_synced'])}: {_h(item.get('last_synced', 'never'))}</span>{error}{warning}{failed_span}</div>"
        )
    return f"""<section class="source-health" aria-label="{_h(t["source_health"])}">
  {"".join(rows)}
</section>"""


def _render_job_row(job: dict[str, Any], lang: str, return_params: dict[str, str]) -> str:
    t = _labels_for_lang(lang)
    deadline = _h(job["deadline"][:10]) if job.get("deadline") else "unknown"
    if job.get("deadline_conflict"):
        deadline += f'<br><span class="pill pill-failed">sources disagree</span> <small>{_h(job["deadline_observations"])}</small>'
    grad = _h(job["graduation_year_state"])
    if job.get("intake_year"):
        grad += f"<br><small>intake {_h(job['intake_year'])}</small>"
    if job.get("eligibility"):
        grad += "<br><small>" + _h("; ".join(job["eligibility"][:2])) + "</small>"
    status_html = _workflow_form(job["id"], job["workflow_status"], lang, return_params)
    if job.get("application_id"):
        status_html += f'<small><a href="/application?id={_q(job["application_id"])}&lang={_h(lang)}">application: {_h(job["application_status"])}</a></small>'
    projects = "<br><small>projects: " + _h("; ".join(job["matched_projects"])) + "</small>" if job.get("matched_projects") else ""
    return f"""<tr>
  <td><a class="title" href="/job?id={_q(job["id"])}&lang={_h(lang)}">{_h(job["title"])}</a><span>{_h(job["company"])}</span><small>{_h(job["location"])} · {_h(job["work_mode"])}</small>{_render_state_badge(job, lang)}{_render_enrichment_badge(job, lang)}</td>
  <td>{job["application_priority"]:.3f}</td>
  <td>{job["technical_fit"]:.3f}</td>
  <td>{_h(job["seniority"])}</td>
  <td>{_h(job["sponsorship_state"])}</td>
  <td>{grad}</td>
  <td>{_h(", ".join(job["role_tracks"]))}</td>
  <td>{_h(job["recommended_cv"])}{" / hybrid" if job["hybrid_cv"] else ""}<br><a href="/cv?job_id={_q(job["id"])}&lang={_h(lang)}">CV workbench</a></td>
  <td>{status_html}</td>
  <td>{_render_source_badges(job)}</td>
  <td><small>{_h(t["posted"])} {_h(job["posted_at"][:10])}<br>{_h(t["first_seen"])} {_h(job["first_seen_at"][:10])}<br>last seen {_h(job["last_seen_at"][:10])}<br>deadline {deadline}</small></td>
  <td><small>{_h("; ".join(job["strongest_candidate_evidence"][:2]))}<br>{_h("; ".join(job["main_weaknesses"][:3]))}</small>{projects}<br><small>{_h(job["ranking_explanation"])}</small></td>
  <td><a class="button" href="{_h(job["application_url"])}" target="_blank" rel="noreferrer">{_h(t["apply"])}</a></td>
</tr>"""


def _render_detail(model: dict[str, Any]) -> str:
    t = model["labels"]
    job = model["job"]
    return f"""<!doctype html>
<html lang="{_h(model["lang"])}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{_h(job["title"])}</title><style>{_css()}</style></head>
<body>
  {render_utils.nav_bar(model["lang"], "inbox")}
  <header><div><h1>{_h(job["title"])}</h1><p>{_h(job["company"])} · {job["application_priority"]:.3f}</p></div><nav><a href="/cv?job_id={_q(job["id"])}&lang={_h(model["lang"])}">CV workbench</a><a href="/job?id={_q(job["id"])}&lang=en">EN</a><a href="/job?id={_q(job["id"])}&lang=zh">中文</a><a href="/inbox?lang={_h(model["lang"])}">{_h(t["back"])}</a></nav></header>
  <main class="detail">
    <section><h2>{_h(t["english_summary"])}</h2><p>{_h(job["english_summary"])}</p></section>
    <section><h2>{_h(t["chinese_summary"])}</h2><p>{_h(job["chinese_summary"])}</p></section>
    <section><h2>{_h(t["normalized_fields"])}</h2><pre>{_h(_format_mapping(model["normalized"]))}</pre></section>
    <section><h2>{_h(t["source_observations"])}</h2>{_render_source_observations(model["source_observations"], t)}</section>
    <section><h2>{_h(t["sponsorship_evidence"])}</h2><p>{_h(model["sponsorship_evidence"])}</p></section>
    <section><h2>{_h(t["graduation_evidence"])}</h2><p>{_h(model["graduation_evidence"])}</p></section>
    <section><h2>{_h(t["seniority_evidence"])}</h2><p>{_h(model["seniority_evidence"])}</p></section>
    <section><h2>{_h(t["role_track_explanation"])}</h2><p>{_h(model["role_track_explanation"])}</p></section>
    <section><h2>{_h(t["candidate_match"])}</h2><ul>{"".join(f"<li>{_h(item)}</li>" for item in model["candidate_match_evidence"])}</ul></section>
    <section><h2>{_h(t["project_emphasis"])}</h2><p>{_h(model["project_emphasis"])}</p></section>
    <section><h2>{_h(t["explanation"])}</h2><p>{_h(job["ranking_explanation"])}</p></section>
    <section><h2>{_h(t["original_jd"])}</h2><pre>{_h(model["original_jd"])}</pre></section>
  </main>
</body>
</html>"""


def _workflow_form(job_id: str, current: str, lang: str, return_params: dict[str, str]) -> str:
    return_to = "/inbox?" + urllib.parse.urlencode(return_params)
    return f"""<form method="post" action="/workflow">
  <input type="hidden" name="job_id" value="{_h(job_id)}">
  <input type="hidden" name="return_to" value="{_h(return_to)}">
  {_workflow_select("status", current, lang)}
  <button type="submit">{_h(_labels_for_lang(lang)["save"])}</button>
</form>"""


def _render_source_observations(observations: list[dict[str, Any]], t: dict[str, str]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{_h(item['label'])}</td>"
        f"<td>{_h(item['source_job_id'])}</td>"
        f"<td>{_h(item['ingestion_method'])}</td>"
        f"<td>{_h(item['enrichment_state'])}</td>"
        f"<td>{_h(item['latest_observed_state'])}</td>"
        f"<td><a href=\"{_h(item['original_url'])}\" target=\"_blank\" rel=\"noreferrer\">{_h(t['source'])}</a></td>"
        f"<td><a href=\"{_h(item['application_url'])}\" target=\"_blank\" rel=\"noreferrer\">{_h(t['apply'])}</a></td>"
        "</tr>"
        for item in observations
    )
    return f"<table class=\"observations\"><thead><tr><th>{_h(t['source'])}</th><th>ID</th><th>Method</th><th>{_h(t['enrichment_state'])}</th><th>{_h(t['seen'])}</th><th>URL</th><th>{_h(t['apply'])}</th></tr></thead><tbody>{rows}</tbody></table>"


def _render_enrichment_badge(job: dict[str, Any], lang: str) -> str:
    state = job.get("enrichment_state")
    if state == "fully_enriched":
        return ""
    label = _enrichment_label(state, _labels_for_lang(lang))
    return f'<small><span class="enrichment-badge enrichment-{_h(state)}">{_h(label)}</span></small>'


def _render_state_badge(job: dict[str, Any], lang: str) -> str:
    state = job.get("latest_observed_state")
    if not state:
        return ""
    return f'<small><span class="state-badge state-{_h(str(state).casefold())}">{_h(_state_label(str(state), _labels_for_lang(lang)))}</span></small>'


def _hidden_filter_inputs(filters: DashboardFilters) -> str:
    params = filters_to_params(filters)
    return "".join(f'<input type="hidden" name="{_h(key)}" value="{_h(value)}">' for key, value in params.items())


def _select(name: str, values: list[str], selected: str) -> str:
    return f'<select name="{_h(name)}">' + "".join(
        f'<option value="{_h(value)}" {"selected" if value == selected else ""}>{_h(value or "all")}</option>' for value in values
    ) + "</select>"


def _workflow_select(name: str, selected: str, lang: str) -> str:
    return f'<select name="{_h(name)}">' + "".join(
        f'<option value="{_h(item.value)}" {"selected" if item.value == selected else ""}>{_h(_workflow_label(item.value, lang))}</option>' for item in WorkflowStatus
    ) + "</select>"


def _workflow_filter_select(name: str, selected: str, lang: str) -> str:
    values = [""] + [item.value for item in WorkflowStatus]
    return f'<select name="{_h(name)}">' + "".join(
        f'<option value="{_h(value)}" {"selected" if value == selected else ""}>{_h("all" if not value and lang != "zh" else "全部" if not value else _workflow_label(value, lang))}</option>' for value in values
    ) + "</select>"


def _source_select(name: str, values: list[str], selected: str) -> str:
    return f'<select name="{_h(name)}">' + "".join(
        f'<option value="{_h(value)}" {"selected" if value == selected else ""}>{_h(_source_label(value))}</option>' for value in values
    ) + "</select>"


def _render_source_badges(job: dict[str, Any]) -> str:
    badges = job.get("source_badges") or []
    if not badges:
        return _h(job["source"])
    return " ".join(
        f'<span class="source-badge {_h(_source_class(badge["name"]))}">{_h(badge["label"])}</span>' for badge in badges
    )


def _source_label(value: str) -> str:
    return {"": "all", "welcome_to_the_jungle": "Welcome to the Jungle"}.get(value, value)


def _source_class(value: str) -> str:
    return "source-wttj" if value == "welcome_to_the_jungle" else "source-other"


def _preset_link(label: str, preset: str, lang: str) -> str:
    return f'<a href="/?preset={_h(preset)}&lang={_h(lang)}">{_h(label)}</a>'


def _language_url(filters: DashboardFilters, lang: str) -> str:
    params = filters_to_params(filters)
    params["lang"] = lang
    return "/?" + urllib.parse.urlencode(params)


def _labels_for_lang(lang: str) -> dict[str, str]:
    from jobintel.dashboard.i18n import labels

    return labels(lang)


def _workflow_label(value: str, lang: str) -> str:
    if lang != "zh":
        return value
    return {
        "new": "新",
        "saved": "已收藏",
        "applied": "已申请",
        "oa": "笔试/OA",
        "interview": "面试",
        "rejected": "已拒",
        "offer": "Offer",
        "withdrawn": "已撤回",
        "expired": "已过期",
        "ignore": "忽略",
    }.get(value, value)


def _state_label(state: str, t: dict[str, str]) -> str:
    return {
        "NEW": t["new"],
        "CHANGED": t["changed"],
        "UNCHANGED": t["unchanged"],
        "DISAPPEARED": t["disappeared"],
        "REAPPEARED": t["reappeared"],
    }.get(state, state)


def _enrichment_label(state: str, t: dict[str, str]) -> str:
    return {
        "discovery_only": t["needs_jd_enrichment"],
        "partially_enriched": t["partially_enriched"],
        "fully_enriched": t["fully_enriched"],
    }.get(state, state)


def _source_status_label(status: str, t: dict[str, str]) -> str:
    return {
        "Live API": t["live_api"],
        "Auth required": t["auth_required"],
        "Manual only": t["manual_only"],
        "Error": t["error"],
    }.get(status, status)


def _single_value_params(query: str) -> dict[str, str]:
    return {key: values[-1] for key, values in urllib.parse.parse_qs(query, keep_blank_values=True).items()}


def _format_mapping(value: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {item}" for key, item in value.items())


def _h(value: Any) -> str:
    return render_utils.h(value)


def _q(value: str) -> str:
    return render_utils.q(value)


def _css() -> str:
    return render_utils.css()
