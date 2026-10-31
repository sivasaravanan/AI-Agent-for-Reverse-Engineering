"""HTML / JSON / Markdown rendering. All text is redacted then escaped."""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from . import __version__
from .redaction import redact_text
from .sections import SEV_ORDER, build_summary, recommend_next_steps, section_status

_SEV = {"critical": "#a40e26", "high": "#cf222e", "medium": "#bc4c00", "low": "#9a6700", "info": "#57606a"}
_CSS = ("body{font:14px/1.5 system-ui,Segoe UI,sans-serif;margin:0;color:#1f2328;background:#fff}"
        "main{max-width:1200px;margin:0 auto;padding:24px}"
        "@media(prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}th{background:#161b22!important}"
        "td,th,.card,pre{border-color:#30363d!important}pre{background:#161b22!important}}"
        "table{border-collapse:collapse;width:100%;margin:6px 0 18px}td,th{border:1px solid #d0d7de;padding:5px 8px;text-align:left;vertical-align:top}"
        "th{background:#f6f8fa}.badge{color:#fff;padding:1px 8px;border-radius:10px;font-size:12px}.muted{color:#6e7781}"
        ".cards{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0 20px}.card{border:1px solid #d0d7de;border-radius:8px;padding:8px 14px;min-width:110px}"
        ".card b{display:block;font-size:22px}pre{background:#f6f8fa;border:1px solid #d0d7de;padding:8px;overflow:auto;max-height:260px;white-space:pre-wrap}"
        ".scroll{overflow-x:auto}.warn{border-left:4px solid #bc4c00;padding:4px 10px;background:rgba(188,76,0,.08)}")


def _e(v: Any) -> str:
    return html.escape("" if v is None else redact_text(str(v)), quote=True)


def _tbl(headers: List[str], rows: List[List[str]], empty: str) -> str:
    if not rows:
        return f"<p class='muted'>{_e(empty)}</p>"
    return ("<div class='scroll'><table><thead><tr>" + "".join(f"<th>{_e(h)}</th>" for h in headers) +
            "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) +
            "</tbody></table></div>")


def _loc(f: Dict[str, Any]) -> str:
    return _e(f"{f.get('file')}:{f.get('line')}" if f.get("line") else (f.get("file") or ""))


def _badge(sev: str) -> str:
    return f"<span class='badge' style='background:{_SEV.get(sev, '#57606a')}'>{_e(sev)}</span>"


def _not_provided(name: str, st: str, reason: str = "") -> str:
    msg = {"not-provided": f"{name}: not provided to the report generator (the step may not have been run).",
           "skipped": f"{name}: skipped. {reason}"}.get(st, "")
    return f"<p class='warn'>{_e(msg)}</p>" if msg else ""


def _out(r: Dict[str, Any], limit: int = 4000) -> str:
    s = redact_text(r or "")
    return html.escape(s if len(s) <= limit else s[:limit] + "\n...[truncated]")


def render_json(n: Dict[str, Any], env: Dict[str, Any] | None = None, indent: int = 2) -> str:
    doc = {"schema_version": "1.0", "generator": f"report_delivery {__version__}",
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "summary": build_summary(n), "report": n, "environment": env or n["environment"],
           "next_steps": recommend_next_steps(n)}
    return redact_text(json.dumps(doc, indent=indent, ensure_ascii=False, default=str))


def render_html(n: Dict[str, Any], env: Dict[str, Any] | None = None, title: str | None = None) -> str:
    env = env or n["environment"]
    s, st = build_summary(n), section_status(n)
    proj = n["project"]
    title = title or f"Engineering Analysis Report - {proj.get('name', 'project')}"
    o: List[str] = [f"<h1>{_e(title)}</h1><p class='muted'>Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                    f"by report_delivery {__version__}. Archive: {_e(proj.get('archive', 'n/a'))}. "
                    "Findings are static detections unless their verification status says otherwise.</p>"]
    o.append("<div class='cards'>" + "".join(f"<div class='card'><b>{s['findings_by_severity'][k]}</b>{_e(k)}</div>" for k in SEV_ORDER) +
             f"<div class='card'><b>{s['dependencies_total']}</b>dependencies</div>"
             f"<div class='card'><b>{s['static_analyzers_run']}</b>analyzers run</div></div>")
    o.append("<h2>Report coverage</h2>" + _tbl(["Section", "Status"], [[_e(k), _e(v)] for k, v in st.items()], ""))
    # inventory
    o.append("<h2>Project inventory</h2>" + _not_provided("Inventory", st["inventory"]))
    inv = n["inventory"]
    if inv:
        tot = inv.get("totals", {})
        o.append(_tbl(["Metric", "Value"], [[_e(k), _e(v)] for k, v in tot.items()], "No totals supplied."))
        files = inv.get("files", [])
        if files:
            o.append(_tbl(["Path", "Size", "Language"], [[_e(f.get("path")), _e(f.get("size")), _e(f.get("language"))] for f in files[:500]], ""))
            if len(files) > 500:
                o.append(f"<p class='muted'>Showing 500 of {len(files)} files. Full list is in the JSON report.</p>")
    # dependencies
    o.append("<h2>Dependency report</h2>" + _not_provided("Dependencies", st["dependencies"]))
    if n["dependencies"]:
        o.append(_tbl(["Name", "Version / constraint", "Ecosystem", "Declared in"],
                      [[_e(d.get("name")), _e(d.get("version")), _e(d.get("ecosystem")), _loc(d)] for d in n["dependencies"]], ""))
    # static analysis
    o.append("<h2>Static analysis report</h2>" + _not_provided("Static analysis", st["static_analysis"], n["static_analysis"]["skipped_reason"] or ""))
    for r in n["static_analysis"]["results"]:
        o.append(f"<h3>{_e(r.get('analyzer'))} <span class='muted'>{_e(r.get('version') or 'version unknown')}</span></h3>"
                 f"<p>Command: <code>{_e(r.get('command'))}</code> · exit code: <b>{_e(r.get('exit_code'))}</b> · "
                 f"result type: {_e(r.get('kind') or 'unspecified')}</p>")
        if r.get("stdout"):
            o.append(f"<pre>{_out(r['stdout'])}</pre>")
        if r.get("stderr"):
            o.append(f"<pre>{_out(r['stderr'])}</pre>")
    # build/test
    o.append("<h2>Build and test results</h2>" + _not_provided("Build/test", st["build_test"], n["build_test"]["skipped_reason"] or ""))
    for r in n["build_test"]["runs"]:
        o.append(f"<h3><code>{_e(r.get('command'))}</code></h3><p>Status: <b>{_e(r.get('status') or 'unknown')}</b> · exit code "
                 f"{_e(r.get('exit_code'))} · duration {_e(r.get('duration_s'))}s</p>")
        for k in ("stdout", "stderr"):
            if r.get(k):
                o.append(f"<pre>{_out(r[k])}</pre>")
    # findings
    o.append("<h2>Security and quality findings</h2>")
    o.append(_tbl(["ID", "Severity", "Category", "Location", "Evidence", "Explanation / remediation", "Source", "Confidence", "Verification"],
                  [[_e(f.get("id")), _badge(f["severity"]), _e(f.get("category")), _loc(f), f"<code>{_e(f.get('evidence'))}</code>",
                    _e(f.get("explanation")) + ("<br><i>" + _e(f.get("remediation")) + "</i>" if f.get("remediation") else ""),
                    _e(f.get("source")), _e(f.get("confidence")), _e(f.get("verification_status"))] for f in n["findings"]],
                  "No findings were reported in the supplied data (this does not prove the absence of issues)."))
    # database
    db = n["database_analysis"]
    o.append("<h2>Database analysis</h2>" + _not_provided("Database analysis", st["database_analysis"]))
    if db:
        o.append(_tbl(["Database", "Evidence level", "Evidence"], [[_e(d.get("display") or d.get("db")), _e(d.get("status")), _e(d.get("rationale"))]
                                                                   for d in db.get("databases", [])], "No databases detected."))
        conns = db.get("connections", [])
        if conns:
            o.append("<h3>Connection configuration (secrets masked)</h3>" + _tbl(["Database", "Level", "Location", "Details"],
                     [[_e(c.get("display")), _e(c.get("level")), _loc(c), f"<code>{_e(c.get('redacted'))}</code>"] for c in conns], ""))
    # environment
    o.append("<h2>Environment and tool versions</h2>")
    if env:
        rows = [[_e(k), _e(v)] for k, v in env.items() if k != "tools"]
        for t, info in (env.get("tools") or {}).items():
            rows.append([_e(t), _e(info.get("version") if info.get("installed") else "not installed")])
        o.append(_tbl(["Item", "Value"], rows, ""))
    else:
        o.append("<p class='warn'>Environment information was not collected.</p>")
    o.append("<h2>Known limitations</h2><ul>" + "".join(f"<li>{_e(x)}</li>" for x in (n["limitations"] or ["None supplied."])) + "</ul>")
    if n["known_issues"]:
        o.append("<h2>Known issues</h2><ul>" + "".join(f"<li>{_e(x)}</li>" for x in n["known_issues"]) + "</ul>")
    o.append("<h2>Recommended next steps</h2><ol>" + "".join(f"<li>{_e(x)}</li>" for x in recommend_next_steps(n)) + "</ol>")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><main>" + "".join(o) + "</main></body></html>")


def render_markdown(n: Dict[str, Any], env: Dict[str, Any] | None = None) -> str:
    s = build_summary(n)
    L = [f"# Engineering Analysis Summary - {n['project'].get('name', 'project')}", "",
         "## Overview", "- Findings: " + ", ".join(f"{k} {v}" for k, v in s["findings_by_severity"].items()),
         f"- Dependencies: {s['dependencies_total']}  |  Static analyzers run: {s['static_analyzers_run']}",
         "- Build/test runs: " + (", ".join(f"{k} {v}" for k, v in s["build_test_runs"].items()) or "none provided"),
         "- Section coverage: " + ", ".join(f"{k}={v}" for k, v in s["sections"].items()), "", "## Top findings"]
    for f in n["findings"][:15]:
        L.append(f"- **[{f['severity']}] {f.get('title') or f.get('id')}** - `{f.get('file')}:{f.get('line')}` ({f.get('verification_status')})")
    if not n["findings"]:
        L.append("- none reported")
    L += ["", "## Known limitations"] + [f"- {x}" for x in (n["limitations"] or ["none supplied"])]
    L += ["", "## Recommended next steps"] + [f"{i}. {x}" for i, x in enumerate(recommend_next_steps(n), 1)]
    return redact_text("\n".join(L)) + "\n"


def render_section_markdown(n: Dict[str, Any], section: str) -> str:
    """section: dependencies | static_analysis | security | build_test | inventory"""
    st = section_status(n)
    if section == "dependencies":
        L = ["# Dependency report", ""]
        L += [f"- `{d.get('name')}` {d.get('version', '')} ({d.get('ecosystem', '?')}) - {d.get('file')}:{d.get('line')}" for d in n["dependencies"]] \
            or ["Not provided to the report generator."]
    elif section == "static_analysis":
        L = ["# Static analysis report", ""]
        for r in n["static_analysis"]["results"]:
            L += [f"## {r.get('analyzer')} {r.get('version') or '(version unknown)'}", f"- command: `{r.get('command')}`",
                  f"- exit code: {r.get('exit_code')}  type: {r.get('kind') or 'unspecified'}", ""]
        if not n["static_analysis"]["results"]:
            L.append(f"Status: {st['static_analysis']} {n['static_analysis']['skipped_reason'] or ''}")
    elif section == "security":
        L = ["# Security findings", ""]
        L += [f"- **{f['id'] if f.get('id') else ''} [{f['severity']}]** {f.get('title', '')} - `{f.get('file')}:{f.get('line')}` "
              f"- {f.get('verification_status')}" for f in n["findings"]] or ["No findings reported in supplied data."]
    elif section == "build_test":
        L = ["# Build and test results", ""]
        for r in n["build_test"]["runs"]:
            L += [f"- `{r.get('command')}` -> {r.get('status') or 'unknown'} (exit {r.get('exit_code')}, {r.get('duration_s')}s)"]
        if not n["build_test"]["runs"]:
            L.append(f"Status: {st['build_test']} {n['build_test']['skipped_reason'] or ''}")
    else:
        L = ["# Project inventory", ""] + [f"- {k}: {v}" for k, v in n["inventory"].get("totals", {}).items()] or ["Not provided."]
    return redact_text("\n".join(L)) + "\n"
