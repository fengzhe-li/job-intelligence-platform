from __future__ import annotations

import urllib.parse
from typing import Any

from jobintel.dashboard.operations import (
    ActionQueueItem,
    ApplicationDetail,
    CvWorkbenchView,
    DeadlineEntry,
    SourceHealthRow,
    TodaySummary,
    WatchlistItem,
)
from jobintel.dashboard.i18n import labels
from jobintel.dashboard.render_utils import css, h, nav_bar, q
from jobintel.models.application import Application
from jobintel.models.taxonomy import ApplicationStatus

# ---------------------------------------------------------------------------
# Shared small helpers
# ---------------------------------------------------------------------------


def _shell(title: str, lang: str, active: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="{h(lang)}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{h(title)}</title><style>{css()}</style></head>
<body>
  {nav_bar(lang, active)}
  {body}
</body>
</html>"""


def _dt(value: str | None) -> str:
    if not value:
        return "unknown"
    return value.replace("T", " ").split(".")[0].split("+")[0]


# ---------------------------------------------------------------------------
# Section C: Today view
# ---------------------------------------------------------------------------


def render_today(summary: TodaySummary, lang: str, already_running: bool = False) -> str:
    t = labels(lang)
    tiles = [
        (summary.new_jobs_today, "/inbox?freshness_window=new_today", "New relevant jobs today", ""),
        (summary.high_priority_not_actioned, "/priority", "High priority, not actioned", "urgent" if summary.high_priority_not_actioned else ""),
        (summary.cvs_ready, "/priority", "CVs ready (not yet submitted)", "good" if summary.cvs_ready else ""),
        (summary.cvs_needing_review, "/priority", "CVs needing review", "attn" if summary.cvs_needing_review else ""),
        (summary.applications_submitted_today, "/applications?status=applied", "Submitted today", ""),
        (summary.applications_awaiting_response, "/applications", "Awaiting response", ""),
        (summary.online_assessments, "/applications?status=online_assessment", "Online assessments", "attn" if summary.online_assessments else ""),
        (summary.interviews, "/applications", "Interviews", "attn" if summary.interviews else ""),
        (summary.offers, "/applications?status=offer", "Offers", "good" if summary.offers else ""),
        (summary.rejections, "/applications?status=rejected", "Rejections", ""),
        (summary.deadlines_within_48h, "/deadlines", "Deadlines within 48h", "urgent" if summary.deadlines_within_48h else ""),
        (summary.deadlines_overdue, "/deadlines", "Deadline passed (still listed)", "attn" if summary.deadlines_overdue else ""),
        (summary.manual_checks_due, "/watchlist", "Manual checks due", "attn" if summary.manual_checks_due else ""),
        (summary.source_failures, "/health", "Source failures / partial refreshes", "urgent" if summary.source_failures else ""),
        (summary.company_failures, "/health", "Company fetch failures", "attn" if summary.company_failures else ""),
    ]
    tile_html = "".join(
        f'<a class="tile {css_class}" href="{h(_with_lang(href, lang))}"><strong>{value}</strong><span>{h(label)}</span></a>' for value, href, label, css_class in tiles
    )
    return _shell(
        "Today",
        lang,
        "today",
        f"""
  <header><div><h1>What should I do today?</h1><p>Generated {h(_dt(summary.generated_at.isoformat()))}. Every count links to the real records behind it -- nothing here is invented.</p></div>
  <form method="post" action="/daily-refresh?lang={h(lang)}"><button type="submit">{h(t["refresh_jobs"])}</button></form>
  </header>
  {f'<p class="due">{h(t["refresh_already_running"])}</p>' if already_running else ""}
  <section class="metrics">{tile_html}</section>
  <main>
    {_render_last_refresh(summary)}
    {_render_attention(summary)}
    <p><a href="{h(_with_lang("/priority", lang))}">Open the explainable priority queue &rarr;</a></p>
    <p><a href="{h(_with_lang("/inbox", lang))}">Open the full job inbox &rarr;</a></p>
  </main>
""",
    )


_OUTCOME_PILL = {
    "succeeded": "pill-active",
    "succeeded_zero_results": "pill-active",
    "partial": "pill-partial",
    "failed": "pill-failed",
    "not_configured": "pill-not-configured",
}


def _render_last_refresh(summary: TodaySummary) -> str:
    report = summary.last_daily_refresh
    if report is None:
        return '<section><h2>Last daily refresh</h2><p class="due">Never run. Nothing has been refreshed automatically yet -- press "Run Daily Refresh (all sources)".</p></section>'
    stale = '<p class="due">STALE: the last daily refresh is older than 36 hours. Counts below are from then, not today.</p>' if summary.daily_refresh_freshness == "stale" else ""
    rows = "".join(
        f"""<tr><td>{h(item.get("source_name"))}</td><td><span class="pill {_OUTCOME_PILL.get(item.get("outcome"), "")}">{h(item.get("outcome"))}</span></td>
<td>{h(item.get("jobs_seen"))}</td><td>{h(item.get("new"))}</td><td>{h(item.get("changed"))}</td><td>{h(item.get("closed"))}</td>
<td><small>{h(item.get("error") or item.get("warning") or item.get("closure_withheld") or "")}</small></td></tr>"""
        for item in report.get("source_outcomes", [])
    )
    stages = "".join(f"<li><strong>{h(stage.get('name'))}</strong>: {h(stage.get('outcome'))} &mdash; {h(stage.get('detail'))}</li>" for stage in report.get("stages", []))
    manual = ", ".join(report.get("manual_only_sources", [])) or "none"
    return f"""<section><h2>Last daily refresh: {h(report.get("status"))}</h2>{stale}
<p>Finished {h(_dt(report.get("finished_at")))} &middot; <strong>{h(report.get("new_jobs"))}</strong> new canonical job(s), <strong>{h(report.get("changed_jobs"))}</strong> changed, <strong>{h(report.get("closed_jobs"))}</strong> closed during that refresh (closure is only inferred from a complete snapshot of a source scope).</p>
<table><thead><tr><th>Source</th><th>Outcome</th><th>Jobs seen</th><th>New</th><th>Changed</th><th>Closed</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table>
<ul>{stages}</ul>
<p><small>Manual-only sources (never automated, check them on the watchlist): {h(manual)}</small></p></section>"""


def _render_attention(summary: TodaySummary) -> str:
    if not summary.sources_needing_attention:
        return ""
    items = "".join(f"<li><strong>{h(name)}</strong> <span class=\"pill pill-failed\">{h(state)}</span> {h(detail)}</li>" for name, state, detail in summary.sources_needing_attention)
    return f'<section><h2>Sources needing attention</h2><ul>{items}</ul><p><a href="/health">Source &amp; company health &rarr;</a></p></section>'


def _with_lang(href: str, lang: str) -> str:
    return f"{href}{'&' if '?' in href else '?'}lang={q(lang)}"


# ---------------------------------------------------------------------------
# Section E: priority / action queue
# ---------------------------------------------------------------------------


def render_priority_queue(items: list[ActionQueueItem], lang: str) -> str:
    rows = "".join(
        f"""<tr>
  <td><a href="/job?id={q(item.job_id)}&lang={h(lang)}">{h(item.title)}</a><br><small>{h(item.company)}</small></td>
  <td>{item.priority_score:.2f}<br><small>base {item.base_priority:.2f}</small></td>
  <td><ul class="reasons">{"".join(f"<li>{h(r)}</li>" for r in item.reasons)}</ul></td>
  <td>{h(item.deadline_days_remaining) + " day(s)" if item.deadline_days_remaining is not None else "unknown"}</td>
  <td>{h(item.application_status or "not started")}</td>
  <td>{h(item.cv_status or "not generated")}</td>
  <td><a class="button" href="/cv?job_id={q(item.job_id)}&lang={h(lang)}">CV</a> <a class="button secondary" href="/job?id={q(item.job_id)}&lang={h(lang)}">Open</a></td>
</tr>"""
        for item in items
    ) or '<tr><td colspan="7">No active jobs to prioritise yet -- run a refresh first.</td></tr>'
    return _shell(
        "Priority Queue",
        lang,
        "priority",
        f"""
  <header><div><h1>Priority / Action Queue</h1><p>Priority = candidate-fit score (technical match, role, seniority, sponsorship, etc. -- see each job's own explanation) plus small, named adjustments listed under "why": deadline proximity, not-yet-applied, CV readiness, freshness. Nothing here is a hidden score.</p></div></header>
  <main><table><thead><tr><th>Job</th><th>Priority</th><th>Why</th><th>Deadline</th><th>Application</th><th>CV</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table></main>
""",
    )


# ---------------------------------------------------------------------------
# Section F: deadlines
# ---------------------------------------------------------------------------


def render_deadlines(buckets: dict[str, list[DeadlineEntry]], lang: str) -> str:
    bucket_labels = {
        "overdue": "Overdue / possibly closed",
        "today": "Today",
        "within_48h": "Within 48 hours",
        "within_7d": "Within 7 days",
        "later": "Later",
        "unknown": "Unknown deadline",
    }
    sections = []
    for key, label in bucket_labels.items():
        entries = buckets.get(key, [])
        rows = "".join(
            f"""<tr>
  <td><a href="/job?id={q(entry.job_id)}&lang={h(lang)}">{h(entry.title)}</a><br><small>{h(entry.company)}</small></td>
  <td>{h(_dt(entry.deadline.isoformat()) if entry.deadline else "unknown")}</td>
  <td>{('<span class="pill pill-failed">Sources disagree</span> ' if entry.deadline_conflict else "") + (h("; ".join(f"{source}: {_dt(value)}" for source, value in entry.deadline_observations.items())) or "no source states a deadline")}</td>
</tr>"""
            for entry in entries
        )
        if not entries:
            continue
        sections.append(f'<section><h2>{h(label)} ({len(entries)})</h2><table><thead><tr><th>Job</th><th>Deadline</th><th>Provenance / conflicts</th></tr></thead><tbody>{rows}</tbody></table></section>')
    body = "".join(sections) or "<p>No active jobs yet.</p>"
    return _shell(
        "Deadlines",
        lang,
        "deadlines",
        f"""
  <header><div><h1>Deadlines</h1><p>A missing deadline is always shown as "unknown deadline" here -- never guessed, never silently treated as far away. Sources that disagree on a deadline are both shown, not silently resolved.</p></div></header>
  <main>{body}</main>
""",
    )


# ---------------------------------------------------------------------------
# Section G: CV workbench
# ---------------------------------------------------------------------------


def render_cv_workbench(view: CvWorkbenchView, lang: str) -> str:
    if view.job is None:
        return _shell("CV Workbench", lang, "inbox", f'<main><p>No job found with id {h(view.job_id)}.</p></main>')

    review_pill = {
        "auto_prepare": "pill-auto_prepare",
        "review_required": "pill-review_required",
        "block_auto_submission": "pill-block_auto_submission",
    }

    application = view.application
    submitted = application is not None and application.is_submitted
    submitted_cv = application.submitted_cv_version_id if submitted else None

    def artifact_row(artifact) -> str:
        bullets = "".join(f"<li>{h(bullet.text)}</li>" for bullet in artifact.bullets)
        pdf_html = (
            f'<a class="button secondary" href="/cv/pdf?id={q(artifact.id)}" target="_blank" rel="noreferrer">View PDF</a> <small>{artifact.page_count} page(s), fits one page: {h(artifact.fits_one_page)}</small>'
            + (
                f' <small>layout v{artifact.document_format}'
                + (f" ({artifact.layout_preset}, {artifact.render_margin_mm:g}mm margins)" if artifact.layout_preset else "")
                + (f", {artifact.page_utilization:.0%} of page used" if artifact.page_utilization is not None else "")
                + "</small>"
                if artifact.document_format >= 2
                else ' <span class="pill">legacy layout (generated before the structured CV)</span>'
            )
            if artifact.pdf_path
            else "<small>(PDF not rendered)</small>"
        )
        if submitted:
            actions = f'<p><strong>{"This exact version was submitted." if artifact.id == submitted_cv else ("Not submitted -- this application was submitted without a system CV." if submitted_cv is None else "Not the submitted version.")}</strong></p>'
        else:
            actions = f"""<form method="post" action="/cv/attach?lang={h(lang)}">
    <input type="hidden" name="job_id" value="{h(view.job_id)}">
    <input type="hidden" name="cv_version_id" value="{h(artifact.id)}">
    <button type="submit" class="secondary">Attach this exact version (not yet submitted)</button>
  </form>
  <form method="post" action="/application/submit?lang={h(lang)}">
    <input type="hidden" name="job_id" value="{h(view.job_id)}">
    <input type="hidden" name="cv_version_id" value="{h(artifact.id)}">
    <label><input type="checkbox" name="reviewed" value="yes" required> I have personally reviewed this exact CV ({h(artifact.id)}), its project selection and every bullet</label>
    <input name="note" placeholder="optional note, e.g. submitted via company portal">
    <button type="submit">Mark application SUBMITTED with this version</button>
  </form>"""
        return f"""<section>
  <h2>CV version <code>{h(artifact.id)}</code> &mdash; generated {h(_dt(artifact.generated_at.isoformat()))} <span class="pill {review_pill.get(artifact.review_status.value, '')}">{h(artifact.review_status.value)}</span></h2>
  <p>For job <code>{h(artifact.job_id)}</code>: {h(view.job.title)} @ {h(view.job.company)}</p>
  <p>{pdf_html}</p>
  <p>Selected projects: {h(", ".join(artifact.selected_project_ids) or "none")}</p>
  <p>Evidence IDs used: {h(", ".join(artifact.evidence_ids_used) or "none")}</p>
  {'<p><strong>Review reasons:</strong> ' + h('; '.join(artifact.review_reasons)) + '</p>' if artifact.review_reasons else ''}
  <details><summary>Generated bullets ({len(artifact.bullets)})</summary><ul>{bullets}</ul></details>
  <details><summary>Full CV text</summary><pre>{h(artifact.cv_text)}</pre></details>
  {actions}
</section>"""

    artifacts_html = "".join(artifact_row(a) for a in reversed(view.artifacts)) or "<p>No CV generated yet for this job.</p>"
    manual_submit_html = "" if submitted else f"""<form method="post" action="/application/submit?lang={h(lang)}">
  <input type="hidden" name="job_id" value="{h(view.job_id)}">
  <input name="note" placeholder="optional note, e.g. used my own CV">
  <button type="submit" class="secondary">I applied manually WITHOUT a system CV &mdash; mark Applied</button>
</form>"""
    if application is not None:
        application_html = (
            f"<p>Application: <a href=\"/application?id={q(application.id)}&lang={h(lang)}\">{h(application.id)}</a> "
            f"&middot; status <span class=\"pill pill-{h(application.status.value)}\">{h(application.status.value)}</span> "
            + (
                f"&middot; submitted with system CV <code>{h(submitted_cv)}</code></p>"
                if submitted and submitted_cv
                else "&middot; <strong>No system CV attached</strong> (submitted manually)</p>"
                if submitted
                else f"&middot; attached CV (draft, not submitted): <code>{h(application.cv_version_id or 'none')}</code></p>"
            )
        )
    else:
        application_html = f"""<form method="post" action="/application/create?lang={h(lang)}">
  <input type="hidden" name="job_id" value="{h(view.job_id)}">
  <button type="submit" class="secondary">Start tracking an application for this job (not submitted)</button>
</form>"""

    return _shell(
        f"CV Workbench: {view.job.title}",
        lang,
        "inbox",
        f"""
  <header><div><h1>{h(view.job.title)}</h1><p>{h(view.job.company)}</p></div></header>
  <div class="disclaimer">{h(view.review_disclaimer)}</div>
  <main>
    {application_html}
    <form method="post" action="/cv/generate?lang={h(lang)}">
      <input type="hidden" name="job_id" value="{h(view.job_id)}">
      <button type="submit">{"Regenerate CV (creates a new immutable version)" if view.artifacts else "Generate tailored CV"}</button>
    </form>
    <p><a href="{h(view.job.canonical_application_url)}" target="_blank" rel="noreferrer">Open application page &rarr;</a></p>
    {manual_submit_html}
    {artifacts_html}
  </main>
""",
    )


# ---------------------------------------------------------------------------
# Section H: application tracker list
# ---------------------------------------------------------------------------


def _cv_cell(app: Application) -> str:
    if app.is_submitted:
        return f"{h(app.submitted_cv_version_id)} <small>(submitted)</small>" if app.submitted_cv_version_id else "<strong>No system CV attached</strong> <small>(applied manually)</small>"
    return f"{h(app.cv_version_id)} <small>(attached draft)</small>" if app.cv_version_id else "none yet"


def render_application_list(applications: list[Application], lang: str) -> str:
    rows = "".join(
        f"""<tr>
  <td><a href="/application?id={q(app.id)}&lang={h(lang)}">{h(app.role_title)}</a><br><small>{h(app.company)}</small></td>
  <td><span class="pill pill-{h(app.status.value)}">{h(app.status.value)}</span></td>
  <td>{h(_dt(app.applied_at.isoformat()) if app.applied_at else ("submitted, time not recorded" if app.is_submitted else "not yet applied"))}</td>
  <td>{_cv_cell(app)}</td>
  <td>{h(_dt(app.deadline.isoformat()) if app.deadline else "unknown")}</td>
  <td>{h(app.next_action or "")}</td>
</tr>"""
        for app in applications
    ) or '<tr><td colspan="6">No applications tracked yet.</td></tr>'
    return _shell(
        "Applications",
        lang,
        "applications",
        f"""
  <header><div><h1>Application Tracker</h1><p>The full lifecycle model (discovered &rarr; shortlisted &rarr; materials_ready &rarr; applied &rarr; online_assessment &rarr; phone_screen &rarr; technical_interview &rarr; final_interview &rarr; offer/rejected/withdrawn/expired) -- not a simplified new/saved/applied model.</p></div></header>
  <main><table><thead><tr><th>Role</th><th>Status</th><th>Applied</th><th>CV version</th><th>Deadline</th><th>Next action</th></tr></thead><tbody>{rows}</tbody></table></main>
""",
    )


# ---------------------------------------------------------------------------
# Section I: application detail / timeline
# ---------------------------------------------------------------------------


def render_application_detail(detail: ApplicationDetail, lang: str) -> str:
    app = detail.application
    timeline_html = "".join(
        f'<li><span class="date">{h(_dt(event.occurred_at.isoformat()))}</span> <span class="label">{h(event.label)}</span><div class="detail">{h(event.detail)}</div></li>'
        for event in detail.timeline
    ) or "<li>No events recorded yet.</li>"

    pre_submission = {ApplicationStatus.DISCOVERED, ApplicationStatus.SHORTLISTED, ApplicationStatus.MATERIALS_READY}
    allowed = [status for status in ApplicationStatus if not (app.is_submitted and status in pre_submission)]
    status_options = "".join(f'<option value="{h(status.value)}" {"selected" if status == app.status else ""}>{h(status.value)}</option>' for status in allowed)

    recorded_label = "System CV submitted" if app.is_submitted else "CV attached (not yet submitted)"
    current_cv_html = "<p>No CV version recorded on this application.</p>"
    if detail.current_cv is not None:
        cv = detail.current_cv
        pdf_link = f' &mdash; <a href="/cv/pdf?id={q(cv.id)}" target="_blank" rel="noreferrer">View PDF</a>' if cv.pdf_path else ""
        current_cv_html = f"""<p><strong>{recorded_label}:</strong> <code>{h(cv.id)}</code>, generated {h(_dt(cv.generated_at.isoformat()))}, {cv.page_count} page(s), review status {h(cv.review_status.value)}{pdf_link}</p>
<p>Selected projects: {h(", ".join(cv.selected_project_ids) or "none")}</p>
<p>Evidence IDs: {h(", ".join(cv.evidence_ids_used) or "none")}</p>
<details><summary>Full CV text</summary><pre>{h(cv.cv_text)}</pre></details>"""
    elif app.is_submitted and not app.submitted_cv_version_id:
        current_cv_html = "<p><strong>No system CV attached.</strong> This application was submitted manually without a system-generated CV (e.g. your own CV). Nothing is missing, and no generated CV will be linked to it retroactively.</p>"
    elif app.submitted_cv_version_id or app.cv_version_id:
        current_cv_html = f"<p><strong>{recorded_label}:</strong> <code>{h(app.submitted_cv_version_id or app.cv_version_id)}</code> (artifact record not found in this store)</p>"

    job = detail.job
    provenance_rows = ""
    if job is not None:
        provenance_rows = "".join(
            f"<tr><td>{h(o.source_name)}</td><td>{h(o.source_job_id)}</td><td>{h(_dt(o.first_seen_at.isoformat()))}</td><td>{h(_dt(o.last_seen_at.isoformat()))}</td><td>{'active' if o.active else h(o.latest_observed_state)}</td><td><a href=\"{h(o.original_url)}\" target=\"_blank\" rel=\"noreferrer\">link</a></td></tr>"
            for o in job.source_observations
        )
    provenance_html = (
        f"<table><thead><tr><th>Source</th><th>Source job id</th><th>First seen</th><th>Last seen</th><th>State</th><th>URL</th></tr></thead><tbody>{provenance_rows}</tbody></table>"
        if provenance_rows
        else f"<p>Sources at application creation: {h(', '.join(app.sources) or 'unknown')} (job no longer in the store)</p>"
    )

    return _shell(
        f"Application: {app.role_title}",
        lang,
        "applications",
        f"""
  <header><div><h1>{h(app.role_title)}</h1><p>{h(app.company)} &middot; <span class="pill pill-{h(app.status.value)}">{h(app.status.value)}</span> &middot; application <code>{h(app.id)}</code></p></div>
  <nav><a href="/cv?job_id={q(app.job_id)}&lang={h(lang)}">CV workbench</a><a href="/job?id={q(app.job_id)}&lang={h(lang)}">Job</a></nav></header>
  <main class="detail">
    <section>
      <h2>Status</h2>
      <p>Discovered: {h(_dt(app.discovered_at.isoformat()) if app.discovered_at else "unknown")} &middot; Submitted: {h(_dt(app.applied_at.isoformat()) if app.applied_at else ("yes, time not recorded" if app.is_submitted else "not submitted"))} &middot; Last updated: {h(_dt(app.updated_at.isoformat()) if app.updated_at else "unknown")}</p>
      <p>Deadline: {h(_dt(app.deadline.isoformat()) if app.deadline else "unknown")}</p>
      <form method="post" action="/application/status?lang={h(lang)}">
        <input type="hidden" name="application_id" value="{h(app.id)}">
        <label>New status<select name="status">{status_options}</select></label>
        <label>Note<input name="note" placeholder="e.g. OA invite received"></label>
        <button type="submit">Record status change</button>
      </form>
      <p>Application URL: <a href="{h(app.application_url or '')}" target="_blank" rel="noreferrer">{h(app.application_url or "none recorded")}</a></p>
      <p>Notes: {h(app.notes or "(none)")}</p>
    </section>
    <section><h2>{recorded_label}</h2>{current_cv_html}</section>
    <section><h2>Full history (append-only)</h2><ul class="timeline">{timeline_html}</ul></section>
    <section><h2>Source provenance</h2>{provenance_html}</section>
    <section><h2>JD snapshot (as captured when tracking started)</h2><details><summary>Show JD snapshot</summary><pre>{h(app.jd_snapshot or "(no snapshot captured)")}</pre></details></section>
  </main>
""",
    )


# ---------------------------------------------------------------------------
# Section J: manual watchlist
# ---------------------------------------------------------------------------


def render_watchlist(items: list[WatchlistItem], lang: str) -> str:
    def row(item: WatchlistItem) -> str:
        due_html = '<span class="due">DUE</span>' if item.is_due else ("snoozed" if item.is_snoozed else "not due")
        open_link = f'<a class="button secondary" href="{h(item.careers_url)}" target="_blank" rel="noreferrer">Open</a>' if item.careers_url else ""
        return f"""<tr>
  <td><strong>{h(item.display_name)}</strong><br><small>{h(item.target_type)} &middot; {h(item.state)}</small><br><small>{h(item.reason)}</small></td>
  <td>{due_html}</td>
  <td>{h(_dt(item.last_checked_at))}</td>
  <td>{h(_dt(item.next_check_due))} <small>(every {item.check_frequency_days}d)</small></td>
  <td>{h(item.check_result or "-")}<br><small>{h(item.notes)}</small></td>
  <td class="card-actions">
    {open_link}
    <form method="post" action="/watchlist/check?lang={h(lang)}">
      <input type="hidden" name="target" value="{h(item.target)}">
      <input name="result" placeholder="e.g. no relevant jobs" style="min-width:140px">
      <button type="submit">Mark checked</button>
    </form>
    <form method="post" action="/watchlist/snooze?lang={h(lang)}">
      <input type="hidden" name="target" value="{h(item.target)}">
      <input type="hidden" name="days" value="7">
      <button type="submit" class="secondary">Snooze 7d</button>
    </form>
    <form method="post" action="/manual-import?lang={h(lang)}">
      <input type="hidden" name="source_name" value="{h(item.target)}">
      <input name="url" placeholder="paste job URL" style="min-width:160px">
      <button type="submit" class="secondary">Import job</button>
    </form>
  </td>
</tr>"""

    rows = "".join(row(item) for item in items) or "<tr><td colspan=6>Nothing on the watchlist -- either everything is auto-verified, or the registry is empty.</td></tr>"
    return _shell(
        "Manual Watchlist",
        lang,
        "watchlist",
        f"""
  <header><div><h1>Manual Watchlist</h1><p>Every company/source that still requires a human check, why, and when you last checked it -- so you never have to remember which of {len(items)} you've already done.</p></div></header>
  <main><table><thead><tr><th>Target</th><th>Due?</th><th>Last checked</th><th>Next due</th><th>Last result / notes</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table></main>
""",
    )


# ---------------------------------------------------------------------------
# Section K: manual URL import
# ---------------------------------------------------------------------------


def render_manual_import_form(lang: str, result: dict[str, Any] | None = None) -> str:
    result_html = ""
    if result is not None:
        fields = "".join(f"<li>{h(k)}: {h(v)}</li>" for k, v in result.items())
        result_html = f'<section><h2>Import result</h2><ul>{fields}</ul></section>'
    return _shell(
        "Manual Job Import",
        lang,
        "manual-import",
        f"""
  <header><div><h1>Import a Job You Found Yourself</h1><p>Paste a URL from Trackr, Gradcracker, Bright Network, a company careers site, or anywhere else. This goes through the exact same pipeline as every automated source: structured-data auto-fetch where possible, normalize, dedup, classify, match, persist -- never a separate bypass, and it will merge with an existing observation of the same vacancy rather than creating a duplicate.</p></div></header>
  <main>
    <form method="post" action="/manual-import?lang={h(lang)}">
      <label>Job URL<input name="url" required placeholder="https://..." style="min-width:420px"></label>
      <label>Source label<input name="source_name" placeholder="manual (or trackr/gradcracker/bright_network)" value="manual"></label>
      <label>Title (only if auto-fetch can't find one)<input name="title"></label>
      <label>Company (only if auto-fetch can't find one)<input name="company"></label>
      <label>Location<input name="location"></label>
      <label>Deadline (ISO date, only if known)<input name="deadline"></label>
      <button type="submit">Import</button>
    </form>
    {result_html}
  </main>
""",
    )


# ---------------------------------------------------------------------------
# Section L: source & company health
# ---------------------------------------------------------------------------

_STATE_PILL = {
    "ACTIVE_AND_REFRESHED": "pill-active",
    "ACTIVE_NOT_REFRESHED": "pill-stale",
    "NOT_CONFIGURED": "pill-not-configured",
    "PARTIAL_COVERAGE": "pill-partial",
    "PARTIAL_REFRESH": "pill-partial",
    "MANUAL_ONLY": "pill-manual",
    "FAILED": "pill-failed",
    "STALE": "pill-failed",
    "DEGRADED": "pill-failed",
}


def render_health(source_rows: list[SourceHealthRow], company_counts: dict[str, int], company_total: int, lang: str) -> str:
    def source_row(row: SourceHealthRow) -> str:
        pill = _STATE_PILL.get(row.state, "")
        return f"""<tr>
  <td><strong>{h(row.display_name)}</strong><br><small>{h(row.automation_status)}{f" &middot; {row.configured_identifiers} configured" if row.configured_identifiers else ""}</small></td>
  <td><span class="pill {pill}">{h(row.state)}</span></td>
  <td>{h(_dt(row.last_attempted_refresh))}</td>
  <td>{h(_dt(row.last_successful_refresh))}</td>
  <td>{h(row.jobs_observed) if row.jobs_observed is not None else "-"}</td>
  <td>{h(row.new_jobs) if row.new_jobs is not None else "-"}</td>
  <td>{h(row.changed_jobs) if row.changed_jobs is not None else "-"}</td>
  <td>{h(row.closed_last_refresh) if row.closed_last_refresh is not None else "-"}</td>
  <td>{h(row.error or row.warning or "")}</td>
  <td><small>{h(row.coverage_limitation)}</small></td>
</tr>"""

    source_html = "".join(source_row(row) for row in source_rows)
    company_tiles = "".join(
        f'<div><strong>{count}</strong><span>{h(state)}</span></div>' for state, count in company_counts.items()
    )
    return _shell(
        "Source & Company Health",
        lang,
        "health",
        f"""
  <header><div><h1>Source &amp; Company Health</h1><p>A failed or never-attempted refresh is never shown as "0 jobs" -- it has its own distinct state below.</p></div></header>
  <main>
    <h2>Sources</h2>
    <table><thead><tr><th>Source</th><th>State</th><th>Last attempted</th><th>Last successful</th><th>Jobs observed</th><th>New (last refresh)</th><th>Changed</th><th>Closed</th><th>Error/warning</th><th>Coverage limitation</th></tr></thead><tbody>{source_html}</tbody></table>
    <h2>Companies ({company_total} total in the target registry)</h2>
    <section class="metrics">{company_tiles}</section>
    <p><a href="/watchlist?lang={h(lang)}">Open the manual watchlist for these companies &rarr;</a></p>
  </main>
""",
    )
