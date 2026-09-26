from __future__ import annotations

import html
import urllib.parse
from typing import Any


def h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def nav_bar(lang: str, active: str) -> str:
    items = [
        ("today", "/today", "Today" if lang != "zh" else "今日"),
        ("inbox", "/inbox", "Inbox" if lang != "zh" else "收件箱"),
        ("priority", "/priority", "Priority" if lang != "zh" else "优先级"),
        ("deadlines", "/deadlines", "Deadlines" if lang != "zh" else "截止日期"),
        ("applications", "/applications", "Applications" if lang != "zh" else "申请"),
        ("watchlist", "/watchlist", "Watchlist" if lang != "zh" else "人工核查"),
        ("health", "/health", "Source Health" if lang != "zh" else "来源健康"),
        ("manual-import", "/manual-import", "Add Job" if lang != "zh" else "手动添加"),
    ]
    links = "".join(
        f'<a href="{path}?lang={h(lang)}" class="{"active" if key == active else ""}">{h(label)}</a>' for key, path, label in items
    )
    return f'<nav class="topnav">{links}</nav>'


def page_shell(title: str, lang: str, active: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="{h(lang)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)}</title>
  <style>{css()}</style>
</head>
<body>
  {nav_bar(lang, active)}
  {body}
</body>
</html>"""


def css() -> str:
    return """
body{margin:0;font-family:Inter,Arial,sans-serif;color:#17202a;background:#f6f7f9}
header{display:flex;justify-content:space-between;gap:24px;align-items:flex-end;padding:24px 28px;background:#ffffff;border-bottom:1px solid #dfe3e8}
h1{margin:0;font-size:28px;letter-spacing:0}h2{font-size:18px;margin:0 0 10px}p{margin:6px 0;color:#52606d}
nav{display:flex;gap:10px}a{color:#075985;text-decoration:none}.title{font-weight:700;color:#102a43}small{color:#52606d;line-height:1.45}
.topnav{display:flex;gap:4px;flex-wrap:wrap;padding:10px 28px;background:#102a43}
.topnav a{color:#bcccdc;text-decoration:none;font-size:13px;font-weight:600;padding:6px 10px;border-radius:6px}
.topnav a:hover{background:#1e3a5f;color:#fff}.topnav a.active{background:#0f766e;color:#fff}
.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;padding:16px 28px}.metrics div{background:#fff;border:1px solid #dfe3e8;border-radius:8px;padding:14px}.metrics strong{display:block;font-size:24px}.metrics span{font-size:13px;color:#52606d}
.metrics a.tile{display:block;text-decoration:none;color:inherit;transition:box-shadow .1s}.metrics a.tile:hover{box-shadow:0 0 0 2px #0f766e}
.metrics .urgent strong{color:#b91c1c}.metrics .attn strong{color:#b45309}.metrics .good strong{color:#15803d}
.presets{display:flex;gap:8px;flex-wrap:wrap;padding:0 28px 14px}.presets a,.button,button{border:1px solid #0f766e;background:#0f766e;color:#fff;border-radius:6px;padding:7px 10px;font-size:13px;cursor:pointer}
.button.secondary,button.secondary{background:#fff;color:#0f766e}
.saved-searches strong{align-self:center;margin-right:4px;color:#334155}.refresh-form{padding:0 28px 14px}.refresh-summary,.source-health{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:0 28px 14px;padding:10px 12px;background:#fff;border:1px solid #dfe3e8;border-radius:8px}.source-health div{display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-right:1px solid #e5e7eb;padding-right:10px}.refresh-summary span,.source-health span{font-size:13px;color:#52606d}
.filters{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:10px;padding:16px 28px;background:#fff;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb}
.save-search{display:flex;gap:10px;align-items:end;padding:0 28px 16px;background:#fff;border-bottom:1px solid #e5e7eb}.save-search label{min-width:220px}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#52606d}.check{justify-content:end}select,input,textarea{min-height:34px;border:1px solid #cbd5e1;border-radius:6px;padding:5px 8px;background:#fff;font-family:inherit}
main{padding:18px 28px}table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dfe3e8}th,td{padding:10px;border-bottom:1px solid #edf1f5;vertical-align:top;text-align:left;font-size:13px}th{background:#f8fafc;color:#334155;font-size:12px}td:first-child span,td:first-child small{display:block;margin-top:4px}
td form{display:flex;gap:6px;align-items:center}.detail{display:grid;grid-template-columns:1fr 1fr;gap:14px}.detail section{background:#fff;border:1px solid #dfe3e8;border-radius:8px;padding:16px}.detail section:last-child{grid-column:1/-1}pre{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#243b53}
.source-badge{display:inline-block;border:1px solid #cbd5e1;border-radius:6px;padding:3px 6px;margin:0 4px 4px 0;background:#f8fafc;color:#334155;font-size:12px}.source-wttj{border-color:#db2777;color:#9d174d;background:#fdf2f8}
.enrichment-badge{display:inline-block;border:1px solid #f59e0b;border-radius:6px;padding:3px 6px;margin-top:6px;background:#fffbeb;color:#92400e;font-size:12px}.enrichment-partially_enriched{border-color:#38bdf8;background:#f0f9ff;color:#075985}
.state-badge{display:inline-block;border:1px solid #94a3b8;border-radius:6px;padding:3px 6px;margin-top:6px;background:#f8fafc;color:#334155;font-size:12px}.state-new,.state-reappeared{border-color:#16a34a;background:#f0fdf4;color:#166534}.state-changed{border-color:#2563eb;background:#eff6ff;color:#1d4ed8}.state-disappeared{border-color:#dc2626;background:#fef2f2;color:#991b1b}
.pill{display:inline-block;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:700}
.pill-active{background:#dcfce7;color:#166534}.pill-not-configured{background:#e2e8f0;color:#334155}.pill-failed{background:#fee2e2;color:#991b1b}.pill-manual{background:#fef3c7;color:#92400e}.pill-partial{background:#dbeafe;color:#1e40af}.pill-stale{background:#f1f5f9;color:#475569}
.pill-auto_verified{background:#dcfce7;color:#166534}.pill-manual_required{background:#fef3c7;color:#92400e}.pill-temporarily_failed{background:#fee2e2;color:#991b1b}.pill-partial_automation{background:#dbeafe;color:#1e40af}.pill-unknown{background:#f1f5f9;color:#475569}
.pill-review_required{background:#fef3c7;color:#92400e}.pill-auto_prepare{background:#dcfce7;color:#166534}.pill-block_auto_submission{background:#fee2e2;color:#991b1b}
.due{color:#b91c1c;font-weight:700}
.reasons{list-style:none;margin:0;padding:0}.reasons li{font-size:12.5px;padding:2px 0;color:#334155}
.timeline{list-style:none;margin:0;padding:0}.timeline li{padding:10px 0;border-bottom:1px solid #edf1f5}.timeline .date{font-weight:700;color:#0f766e;font-size:12px}.timeline .label{font-weight:600}.timeline .detail{color:#52606d;font-size:13px}
.disclaimer{background:#fffbeb;border:1px solid #f59e0b;border-radius:8px;padding:10px 14px;margin:0 28px 14px;font-size:13px;color:#92400e}
.card-actions{display:flex;gap:6px;flex-wrap:wrap}
@media(max-width:900px){.metrics,.filters,.detail{grid-template-columns:1fr}main{padding:12px}header{padding:18px;align-items:flex-start;flex-direction:column}table{display:block;overflow-x:auto}.metrics,.presets,.filters{padding-left:12px;padding-right:12px}}
"""
