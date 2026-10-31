"""JSON / HTML / Markdown rendering. Everything is escaped; secrets were already redacted upstream."""
from __future__ import annotations

import html
import json
from typing import Dict, List

from .models import DatabaseAnalysisResult

_e = lambda s: html.escape("" if s is None else str(s), quote=True)  # noqa: E731
_BADGE = {"confirmed": "#1a7f37", "strong": "#9a6700", "weak": "#6e7781"}
_SEV = {"critical": "#a40e26", "high": "#cf222e", "medium": "#bc4c00", "low": "#9a6700", "info": "#57606a"}


def render_json(result: DatabaseAnalysisResult, indent: int = 2) -> str:
    return json.dumps(result.to_dict(), indent=indent, ensure_ascii=False)


def _table(headers: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return "<p class='muted'>None found.</p>"
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _loc(file, line) -> str:
    return _e(f"{file}:{line}" if line else file)


def render_html_fragment(r: DatabaseAnalysisResult) -> str:
    """Embeddable <section> for the host application's main report."""
    out: List[str] = ["<section class='dbanalysis'><h2>Database analysis</h2>"]
    out.append("<h3>Detected databases</h3>")
    out.append(_table(["Database", "Evidence level", "Evidence", "Notes"], [
        [f"<b>{_e(d.display)}</b>", f"<span class='badge' style='background:{_BADGE[d.status]}'>{_e(d.status)}</span>",
         _e(d.rationale), _e(" ".join(d.notes))] for d in r.databases]))
    out.append("<h3>Connection configuration (secrets masked)</h3>")
    out.append(_table(["Database", "Level", "Kind", "Location", "Host", "Database/file", "User", "Details"], [
        [_e(c.display), _e(c.level), _e(c.kind), _loc(c.file, c.line), _e(c.host), _e(c.database), _e(c.user),
         _e(", ".join(x for x in ["inline password" if c.password_inline else "", "placeholder" if c.placeholder else "",
                                  "TLS off" if c.tls_disabled else "", f"scope: {c.scope}"] if x))]
        for c in r.connections]))
    out.append("<h3>Dependencies &amp; drivers</h3>")
    out.append(_table(["Package", "Version", "Role", "Database", "Location"], [
        [_e(d.name), _e(d.version), _e(d.role), _e(d.db or ""), _loc(d.file, d.line)] for d in r.dependencies]))
    out.append("<h3>Schema objects</h3>")
    out.append(_table(["Kind", "Name", "Columns", "References", "Source", "Location"], [
        [_e(s.kind), _e(s.name), _e(", ".join(s.columns[:12]) + (" …" if len(s.columns) > 12 else "")),
         _e(", ".join(s.references)), _e(s.source), _loc(s.file, s.line)] for s in r.schema_objects]))
    out.append("<h3>ORM / entity models</h3>")
    out.append(_table(["ORM", "Entity", "Table/collection", "Fields", "Location", "Note"], [
        [_e(o.orm), _e(o.name), _e(o.table or ""), _e(o.fields if o.fields is not None else ""), _loc(o.file, o.line), _e(o.detail)]
        for o in r.orm_entities]))
    out.append("<h3>Migrations</h3>")
    out.append(_table(["Tool", "File", "Version", "Creates"], [
        [_e(m.tool), _e(m.file), _e(m.version), _e(", ".join(m.creates))] for m in r.migrations]))
    out.append("<h3>SQL scripts</h3>")
    out.append(_table(["File", "Purpose", "Statements", "Dialect clues"], [
        [_e(s.file), _e(s.purpose), _e(", ".join(f"{k}={v}" for k, v in s.statements.items() if v)),
         _e(", ".join(f"{k}={v}" for k, v in s.dialect_hints.items()))] for s in r.sql_scripts]))
    out.append("<h3>Database-related environment variables</h3>")
    out.append(_table(["Name", "Kind", "Location"], [[_e(e.name), _e(e.kind), _loc(e.file, e.line)] for e in r.env_references]))
    out.append("<h3>Findings</h3>")
    out.append(_table(["ID", "Severity", "Rule", "Title", "Location", "Evidence (masked)", "Explanation / remediation", "Confidence"], [
        [_e(f.id), f"<span class='badge' style='background:{_SEV.get(f.severity, '#57606a')}'>{_e(f.severity)}</span>",
         _e(f.rule), _e(f.title), _loc(f.file, f.line), f"<code>{_e(f.evidence)}</code>",
         _e(f.explanation) + "<br><i>" + _e(f.remediation) + "</i>", _e(f.confidence)] for f in r.findings]))
    out.append("<h3>Limitations</h3><ul>" + "".join(f"<li>{_e(x)}</li>" for x in r.limitations) + "</ul>")
    if r.skipped:
        out.append("<h3>Skipped</h3><ul>" + "".join(f"<li>{_e(k)}: {v}</li>" for k, v in sorted(r.skipped.items())) + "</ul>")
    out.append("</section>")
    return "".join(out)


_CSS = ("body{font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:24px;color:#1f2328;background:#fff}"
        "@media(prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}th{background:#161b22!important}"
        "td,th{border-color:#30363d!important}}"
        "h2{margin-top:0}table{border-collapse:collapse;width:100%;margin:6px 0 18px}"
        "td,th{border:1px solid #d0d7de;padding:5px 8px;text-align:left;vertical-align:top}th{background:#f6f8fa}"
        ".badge{color:#fff;padding:1px 8px;border-radius:10px;font-size:12px}.muted{color:#6e7781}"
        ".scroll{overflow-x:auto}code{word-break:break-all}")


def render_html(r: DatabaseAnalysisResult, title: str = "Database Analysis Report") -> str:
    meta = (f"<p class='muted'>Plug-in v{_e(r.plugin_version)} · generated {_e(r.stats.get('generated_at', ''))} · "
            f"{_e(r.stats.get('files_scanned', 0))} files scanned · static analysis only (nothing executed, "
            f"no database contacted)</p>")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width, initial-scale=1'><title>" + _e(title) + "</title><style>" + _CSS +
            "</style></head><body><h1>" + _e(title) + "</h1>" + meta + render_html_fragment(r) + "</body></html>")


def render_markdown(r: DatabaseAnalysisResult) -> str:
    L: List[str] = ["# Database Analysis", ""]
    L.append("## Detected databases")
    for d in r.databases:
        L.append(f"- **{d.display}** - {d.status} ({d.rationale})")
        for n in d.notes:
            L.append(f"  - note: {n}")
    if not r.databases:
        L.append("- none detected")
    L += ["", "## Connections (secrets masked)"]
    for c in r.connections:
        L.append(f"- {c.display} [{c.level}] {c.kind} at `{c.file}:{c.line}` - `{c.redacted}`")
    L += ["", "## Findings"]
    for f in r.findings:
        L.append(f"- **{f.id} [{f.severity}] {f.title}** - `{f.file}:{f.line}` ({f.confidence} confidence)")
    L += ["", "## Limitations"] + [f"- {x}" for x in r.limitations]
    return "\n".join(L) + "\n"
