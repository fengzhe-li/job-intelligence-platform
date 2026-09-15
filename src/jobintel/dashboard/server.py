from __future__ import annotations

import html
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from jobintel.config import load_ranking_config
from jobintel.dashboard.service import (
    DashboardFilters,
    build_dashboard_model,
    build_job_detail_model,
    filters_to_params,
    filters_from_params,
    save_search,
    update_workflow_status,
)
from jobintel.pipeline.refresh import refresh_sources
from jobintel.models.taxonomy import LocationMode, SponsorshipFilterMode, WorkflowStatus
from jobintel.storage.local_store import LocalJobStore


def run_dashboard(host: str = "127.0.0.1", port: int = 8765, store_root: str = "data/local", config_path: str = "config/personal_strategy.json", registry_path: str = "config/target_companies.json") -> None:
    handler = _handler(store_root, config_path, registry_path)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Dashboard running at http://{host}:{port}")
    server.serve_forever()


def _handler(store_root: str, config_path: str, registry_path: str):
    store = LocalJobStore(store_root)
    config = load_ranking_config(config_path)
    refresh_lock = threading.Lock()

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            params = _single_value_params(parsed.query)
            lang = params.get("lang", "en")
            if parsed.path == "/":
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
            self.send_error(404, "Not found")

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query_params = _single_value_params(parsed.query)
            lang = query_params.get("lang", "en")
            if parsed.path == "/refresh":
                if not _run_dashboard_refresh(store, config, registry_path, refresh_lock):
                    self.send_response(303)
                    self.send_header("Location", _refresh_redirect(lang, already_running=True))
                    self.end_headers()
                    return
                self.send_response(303)
                self.send_header("Location", _refresh_redirect(lang))
                self.end_headers()
                return
            if parsed.path == "/saved-search":
                length = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(length).decode("utf-8")
                params = _single_value_params(payload)
                redirect = _save_dashboard_search(params, store)
                if redirect is None:
                    self.send_error(400, "Missing saved search name")
                    return
                self.send_response(303)
                self.send_header("Location", redirect)
                self.end_headers()
                return
            if parsed.path != "/workflow":
                self.send_error(404, "Not found")
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8")
            params = _single_value_params(payload)
            job_id = params.get("job_id")
            status = params.get("status")
            if not job_id or status not in {item.value for item in WorkflowStatus}:
                self.send_error(400, "Invalid workflow update")
                return
            update_workflow_status(job_id, status, store)
            redirect = params.get("return_to") or "/"
            self.send_response(303)
            self.send_header("Location", redirect)
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
  <form class="refresh-form" method="post" action="/refresh?lang={_h(lang)}"><button type="submit">{_h(t["refresh_jobs"])}</button></form>
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
    return f"""<form class="filters" method="get" action="/">
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
        rows.append(
            f"<div><strong>{_h(item.get('label', 'Source'))}</strong>"
            f"<span>{_h(_source_status_label(item.get('status', 'unknown'), t))}</span>"
            f"<span>{_h(t['seen'])}: {_h(item.get('jobs_seen', 0))}</span>"
            f"<span>{_h(t['active'])}: {_h(item.get('jobs_active', 0))}</span>"
            f"<span>{_state_label('NEW', t)}: {_h(counts.get('NEW', 0))}</span>"
            f"<span>{_state_label('CHANGED', t)}: {_h(counts.get('CHANGED', 0))}</span>"
            f"<span>{_h(t['last_synced'])}: {_h(item.get('last_synced', 'never'))}</span>{error}</div>"
        )
    return f"""<section class="source-health" aria-label="{_h(t["source_health"])}">
  {"".join(rows)}
</section>"""


def _render_job_row(job: dict[str, Any], lang: str, return_params: dict[str, str]) -> str:
    return f"""<tr>
  <td><a class="title" href="/job?id={_q(job["id"])}&lang={_h(lang)}">{_h(job["title"])}</a><span>{_h(job["company"])}</span><small>{_h(job["location"])} · {_h(job["work_mode"])}</small>{_render_state_badge(job, lang)}{_render_enrichment_badge(job, lang)}</td>
  <td>{job["application_priority"]:.3f}</td>
  <td>{job["technical_fit"]:.3f}</td>
  <td>{_h(job["seniority"])}</td>
  <td>{_h(job["sponsorship_state"])}</td>
  <td>{_h(job["graduation_year_state"])}</td>
  <td>{_h(", ".join(job["role_tracks"]))}</td>
  <td>{_h(job["recommended_cv"])}{" / hybrid" if job["hybrid_cv"] else ""}</td>
  <td>{_workflow_form(job["id"], job["workflow_status"], lang, return_params)}</td>
  <td>{_render_source_badges(job)}</td>
  <td><small>{_h(_labels_for_lang(lang)["posted"])} {_h(job["posted_at"])}<br>{_h(_labels_for_lang(lang)["first_seen"])} {_h(job["first_seen_at"])}</small></td>
  <td><small>{_h("; ".join(job["strongest_candidate_evidence"][:2]))}<br>{_h("; ".join(job["main_weaknesses"][:3]))}</small></td>
  <td><a class="button" href="{_h(job["application_url"])}" target="_blank" rel="noreferrer">{_h(_labels_for_lang(lang)["apply"])}</a></td>
</tr>"""


def _render_detail(model: dict[str, Any]) -> str:
    t = model["labels"]
    job = model["job"]
    return f"""<!doctype html>
<html lang="{_h(model["lang"])}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{_h(job["title"])}</title><style>{_css()}</style></head>
<body>
  <header><div><h1>{_h(job["title"])}</h1><p>{_h(job["company"])} · {job["application_priority"]:.3f}</p></div><nav><a href="/job?id={_q(job["id"])}&lang=en">EN</a><a href="/job?id={_q(job["id"])}&lang=zh">中文</a><a href="/?lang={_h(model["lang"])}">{_h(t["back"])}</a></nav></header>
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
    return_to = "/?" + urllib.parse.urlencode(return_params)
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
    return html.escape(str(value), quote=True)


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _css() -> str:
    return """
body{margin:0;font-family:Inter,Arial,sans-serif;color:#17202a;background:#f6f7f9}
header{display:flex;justify-content:space-between;gap:24px;align-items:flex-end;padding:24px 28px;background:#ffffff;border-bottom:1px solid #dfe3e8}
h1{margin:0;font-size:28px;letter-spacing:0}h2{font-size:18px;margin:0 0 10px}p{margin:6px 0;color:#52606d}
nav{display:flex;gap:10px}a{color:#075985;text-decoration:none}.title{font-weight:700;color:#102a43}small{color:#52606d;line-height:1.45}
.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;padding:16px 28px}.metrics div{background:#fff;border:1px solid #dfe3e8;border-radius:8px;padding:14px}.metrics strong{display:block;font-size:24px}.metrics span{font-size:13px;color:#52606d}
.presets{display:flex;gap:8px;flex-wrap:wrap;padding:0 28px 14px}.presets a,.button,button{border:1px solid #0f766e;background:#0f766e;color:#fff;border-radius:6px;padding:7px 10px;font-size:13px}
.saved-searches strong{align-self:center;margin-right:4px;color:#334155}.refresh-form{padding:0 28px 14px}.refresh-summary,.source-health{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:0 28px 14px;padding:10px 12px;background:#fff;border:1px solid #dfe3e8;border-radius:8px}.source-health div{display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-right:1px solid #e5e7eb;padding-right:10px}.refresh-summary span,.source-health span{font-size:13px;color:#52606d}
.filters{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:10px;padding:16px 28px;background:#fff;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb}
.save-search{display:flex;gap:10px;align-items:end;padding:0 28px 16px;background:#fff;border-bottom:1px solid #e5e7eb}.save-search label{min-width:220px}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#52606d}.check{justify-content:end}select,input{min-height:34px;border:1px solid #cbd5e1;border-radius:6px;padding:5px 8px;background:#fff}
main{padding:18px 28px}table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dfe3e8}th,td{padding:10px;border-bottom:1px solid #edf1f5;vertical-align:top;text-align:left;font-size:13px}th{background:#f8fafc;color:#334155;font-size:12px}td:first-child span,td:first-child small{display:block;margin-top:4px}
td form{display:flex;gap:6px;align-items:center}.detail{display:grid;grid-template-columns:1fr 1fr;gap:14px}.detail section{background:#fff;border:1px solid #dfe3e8;border-radius:8px;padding:16px}.detail section:last-child{grid-column:1/-1}pre{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#243b53}
.source-badge{display:inline-block;border:1px solid #cbd5e1;border-radius:6px;padding:3px 6px;margin:0 4px 4px 0;background:#f8fafc;color:#334155;font-size:12px}.source-wttj{border-color:#db2777;color:#9d174d;background:#fdf2f8}
.enrichment-badge{display:inline-block;border:1px solid #f59e0b;border-radius:6px;padding:3px 6px;margin-top:6px;background:#fffbeb;color:#92400e;font-size:12px}.enrichment-partially_enriched{border-color:#38bdf8;background:#f0f9ff;color:#075985}
.state-badge{display:inline-block;border:1px solid #94a3b8;border-radius:6px;padding:3px 6px;margin-top:6px;background:#f8fafc;color:#334155;font-size:12px}.state-new,.state-reappeared{border-color:#16a34a;background:#f0fdf4;color:#166534}.state-changed{border-color:#2563eb;background:#eff6ff;color:#1d4ed8}.state-disappeared{border-color:#dc2626;background:#fef2f2;color:#991b1b}
@media(max-width:900px){.metrics,.filters,.detail{grid-template-columns:1fr}main{padding:12px}header{padding:18px;align-items:flex-start;flex-direction:column}table{display:block;overflow-x:auto}.metrics,.presets,.filters{padding-left:12px;padding-right:12px}}
"""
