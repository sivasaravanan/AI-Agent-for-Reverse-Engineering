"""JSON and HTML report generation. All dynamic text is escaped / redacted."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict

from models.analysis_result import AnalysisResult
from security.secret_scanner import redact

SEV_COLOR = {"critical": "#7f1d1d", "high": "#b91c1c", "medium": "#c2410c", "low": "#a16207", "info": "#475569"}
CONF_COLOR = {"strong": "#15803d", "moderate": "#b45309", "weak": "#6b7280"}


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _size(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{n} B"


def to_json(result: AnalysisResult) -> str:
    return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)


def _table(headers, rows) -> str:
    if not rows:
        return "<p class='muted'>None detected.</p>"
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _badge(text: str, color: str) -> str:
    return f"<span class='badge' style='background:{color}'>{_e(text)}</span>"


def _detections(items) -> str:
    rows = []
    for d in items:
        ev = "<br>".join(f"<code>{_e(x.file)}:{_e(x.line or '-')}</code> {_e(x.text)}" for x in d.evidence)
        if d.evidence_count > len(d.evidence):
            ev += f"<br><span class='muted'>+{d.evidence_count - len(d.evidence)} more</span>"
        rows.append([_e(d.name), _e(d.kind), _badge(d.confidence, CONF_COLOR.get(d.confidence, "#444")),
                     ev, _e(d.note)])
    return _table(["Name", "Kind", "Confidence", "Evidence", "Note"], rows)



def _tree_html(node, depth: int = 0) -> str:
    """Nested <details> tree (iterative-safe: depth is capped at build time)."""
    label = _e(node.name) + ("/" if node.is_dir and node.path else "")
    meta = ""
    if node.is_dir:
        meta = f" <span class='muted'>({node.file_count} files{', vendored - not expanded' if node.vendored else ''}{', truncated' if node.truncated else ''})</span>"
    else:
        meta = f" <span class='muted'>({_size(node.size)})</span>"
    if not node.children:
        return f"<li>{label}{meta}</li>"
    kids = "".join(_tree_html(c, depth + 1) for c in node.children)
    open_attr = " open" if depth < 1 else ""
    return f"<li><details{open_attr}><summary>{label}{meta}</summary><ul>{kids}</ul></details></li>"


def _structure_html(result: AnalysisResult) -> str:
    st = result.structure
    if st is None:
        return ""
    cls_color = {"confirmed": "#166534", "strong": "#15803d", "weak": "#6b7280", "unknown": "#7c3aed"}
    sev_color = {"medium": "#c2410c", "low": "#a16207", "info": "#475569"}
    tree = f"<ul class='tree'>{_tree_html(st.tree)}</ul>" if st.tree else ""
    eps = _table(["File", "Line", "Kind", "Language", "Confidence", "Evidence"], [
        [f"<code>{_e(e.file)}</code>", _e(e.line or "-"), _e(e.kind), _e(e.language),
         _badge(e.confidence, CONF_COLOR.get(e.confidence, "#444")), f"<code>{_e(e.evidence)}</code>"] for e in st.entry_points])
    cfg = _table(["File", "Kind", "Size"], [[f"<code>{_e(c.path)}</code>", _e(c.kind), _size(c.size)] for c in st.config_files])
    env = _table(["Variable", "Status", "Referenced at", "Defined in (names only)"], [
        [f"<code>{_e(v.name)}</code>", _e(v.status),
         _e(", ".join(f"{u.file}:{u.line}" + (" (default)" if u.has_default else "") for u in v.references[:4])),
         _e(", ".join(v.defined_in))] for v in st.env_vars])
    dbc = _table(["Database", "Config key", "Form", "Location", "Confidence", "Evidence (redacted)"], [
        [_e(c.database), _e(c.key), _e(c.form), f"<code>{_e(c.file)}:{_e(c.line)}</code>",
         _badge(c.confidence, CONF_COLOR.get(c.confidence, "#444")), f"<code>{_e(redact(c.evidence))}</code> {_e(c.note)}"]
        for c in st.db_connections])
    ext = _table(["Package", "Language", "Imports", "Declared in manifest", "First seen"], [
        [_e(r["name"]), _e(r["language"]), r["count"],
         {True: "yes", False: "<b>no</b>", None: "unknown (no parsed manifest)"}[r["declared"]],
         f"<code>{_e(r['first_file'])}:{_e(r['first_line'])}</code>"] for r in st.import_summary.external_packages[:60]])
    fan = _table(["File", "Imported by (count)"], [[f"<code>{_e(r['file'])}</code>", r["count"]] for r in st.import_summary.most_depended_on])
    cyc = "".join(f"<li><code>{_e(' <-> '.join(c))}</code></li>" for c in st.import_summary.cycles)
    cyc = f"<ul>{cyc}</ul>" if cyc else "<p class='muted'>No circular imports found among resolved internal imports.</p>"
    conf = _table(["Subject", "Kind", "Evidence class", "Reasons"], [
        [_e(c.subject), _e(c.kind), _badge(c.evidence_class, cls_color.get(c.evidence_class, "#444")), _e("; ".join(c.reasons))]
        for c in st.confidence])
    warns = _table(["Severity", "Code", "Message", "Location"], [
        [_badge(w.severity, sev_color.get(w.severity, "#444")), _e(w.code), _e(w.message),
         f"<code>{_e(w.file)}{':' + str(w.line) if w.line else ''}</code>" if w.file else ""] for w in st.warnings])
    deps = _table(["Ecosystem", "Manifests", "Dependencies", "Without version constraint"], [
        [_e(k), v["manifests"], v["dependencies"], v["unpinned"]] for k, v in sorted(st.dependency_summary.items())])
    unused = ", ".join(f"{u['name']} ({u['file']}:{u['line'] or '-'})" for u in st.import_summary.unused_declared[:30])
    lims = "<ul>" + "".join(f"<li>{_e(x)}</li>" for x in st.limitations) + "</ul>"
    isum = st.import_summary
    return f"""
<h2>Project structure (Phase 2)</h2>
<div class="scroll">{tree}</div>
<h2>Entry points</h2>{eps}
<h2>Configuration files</h2>{cfg}
<h2>Environment variables</h2><p class="muted">Names only - values are never recorded.</p>{env}
<h2>Database connection configuration</h2><p class="muted">Presence of a driver is not treated as proof a database is configured.</p>{dbc}
<h2>Imports and module relationships</h2>
<p>{isum.edge_count:,} import statements in {isum.files_with_imports:,} files; {isum.internal_edges:,} resolve to files inside the archive.</p>
<h3>External packages</h3>{ext}
<h3>Most depended-on internal files</h3>{fan}
<h3>Circular imports</h3>{cyc}
<h3>Declared but not imported (weak indicator only)</h3><p>{_e(unused) or "<span class='muted'>None.</span>"}</p>
<h2>Dependency inventory</h2>{deps}
<h2>Detection confidence</h2><p class="muted">confirmed = independent evidence types agree; strong = one solid evidence type; weak = mention/driver only; unknown = no information.</p>{conf}
<h2>Configuration warnings</h2>{warns}
<h2>Phase 2 limitations</h2>{lims}
"""


def _kv_table(d, headers=("Item", "Count")) -> str:
    return _table(list(headers), [[_e(k), _e(v)] for k, v in sorted((d or {}).items())])


def _phases_html(result: AnalysisResult) -> str:
    """Sections for phases 3-8. Every dynamic value is escaped; free text is redacted."""
    out = []
    sa = result.static_analysis
    if sa is not None:
        runs = _table(["Analyzer", "Status", "Version", "Exit code", "Command", "Note"], [
            [_e(r.get("analyzer")), _e(r.get("status")), _e(r.get("version")), _e(r.get("exit_code", "")),
             f"<code>{_e(redact(' '.join(map(str, r.get('command', [])))))}</code>", _e(r.get("note", ""))]
            for r in sa.get("tool_runs", [])])
        tools = _table(["Tool", "Found at"], [[_e(k), _e(v or "NOT INSTALLED (skipped, nothing installed)")]
                                              for k, v in sorted((sa.get("tools") or {}).items())])
        notes = "".join(f"<li>{_e(n)}</li>" for n in sa.get("notes", []))
        out.append(f"<h2>Language-specific static analysis (Phase 3)</h2><p class='muted'>Findings from these tools are in the "
                   f"Findings table with source <code>phase3:&lt;tool&gt;</code>. Parser/compiler/lint/heuristic kinds are "
                   f"never mixed up.</p><h3>Tool runs</h3>{runs}<h3>Tool availability</h3>{tools}"
                   + (f"<ul>{notes}</ul>" if notes else ""))
    ex = result.execution
    if ex is not None or result.execution_results:
        iso = (ex or {}).get("isolation", {})
        state = (f"Container runtime available ({_e(iso.get('backend'))})." if iso.get("available")
                 else f"<b>Execution unavailable - nothing was run.</b> {_e(iso.get('reason', ''))}")
        cands = _table(["Id", "Needs network", "Command", "Source", "Risks"], [
            [_e(c.get("id")), "YES" if c.get("needs_network") else "no", f"<code>{_e(' '.join(c.get('argv', [])))}</code>",
             _e(f"{c.get('source_file', '')}:{c.get('source_line') or ''}"), _e("; ".join(c.get("risks", [])))]
            for c in (ex or {}).get("candidates", [])])
        runs = _table(["Candidate", "Status", "Exit", "Seconds", "Network", "Image", "Notes"], [
            [_e(r.get("candidate_id")), _e(r.get("status")), _e(r.get("exit_code")), _e(r.get("duration_s")),
             _e("enabled" if r.get("network_enabled") else "disabled"), _e(r.get("image")), _e("; ".join(r.get("notes", [])))]
            for r in result.execution_results])
        out.append(f"<h2>Build and test execution (Phase 4)</h2><p>{state}</p><p class='muted'>Commands are discovered, "
                   f"never run automatically. Each run needed explicit approval and happened only in a disposable container.</p>"
                   f"<h3>Discovered commands</h3>{cands}<h3>Approved runs</h3>{runs}")
    db = result.database_analysis
    if db is not None:
        dbs = _table(["Database", "Status", "Rationale"], [[_e(d.get("display")), _e(d.get("status")), _e(d.get("rationale"))]
                                                           for d in db.get("databases", [])])
        con = _table(["Database", "Level", "Kind", "Location", "Scope", "Redacted value"], [
            [_e(c.get("display")), _e(c.get("level")), _e(c.get("kind")), f"<code>{_e(c.get('file'))}:{_e(c.get('line'))}</code>",
             _e(c.get("scope")), f"<code>{_e(redact(str(c.get('redacted', ''))))}</code>"] for c in db.get("connections", [])])
        orm = _table(["ORM", "Entity", "Table", "Location"], [[_e(o.get("orm")), _e(o.get("name")), _e(o.get("table")),
                     f"<code>{_e(o.get('file'))}:{_e(o.get('line'))}</code>"] for o in db.get("orm_entities", [])[:100]])
        mig = _table(["Tool", "File", "Version"], [[_e(m.get("tool")), f"<code>{_e(m.get('file'))}</code>", _e(m.get("version"))]
                                                   for m in db.get("migrations", [])[:100]])
        sch = _table(["Kind", "Name", "Location"], [[_e(s.get("kind")), _e(s.get("name")), f"<code>{_e(s.get('file'))}:{_e(s.get('line'))}</code>"]
                                                    for s in db.get("schema_objects", [])[:200]])
        lim = "".join(f"<li>{_e(x)}</li>" for x in db.get("limitations", []))
        out.append(f"<h2>Database analysis (Phase 5)</h2><p class='muted'>confirmed = explicit configuration found; it does not mean "
                   f"the database is reachable. No connection was attempted. Secrets are masked.</p>{dbs}<h3>Connection configuration</h3>{con}"
                   f"<h3>ORM entities</h3>{orm}<h3>Migrations</h3>{mig}<h3>Schema objects (DDL)</h3>{sch}<ul>{lim}</ul>")
    cm = result.code_map
    if cm is not None:
        sm = cm.get("summary", {})
        hs = _table(["Function", "Lines", "Complexity", "Location"], [[_e(h["name"]), _e(h["lines"]), _e(h["complexity"]),
                    f"<code>{_e(h['file'])}:{_e(h['line'])}</code>"] for h in cm.get("hotspots", [])[:30]])
        mods = _table(["File", "Language", "Lines", "Symbols"], [[f"<code>{_e(m['file'])}</code>", _e(m["language"]), _e(m["lines"]), _e(m["symbols"])]
                      for m in cm.get("modules", [])[:200]])
        lim = "".join(f"<li>{_e(x)}</li>" for x in cm.get("limitations", []))
        out.append(f"<h2>Code map (functions, classes, calls)</h2><p>{_e(sm.get('symbols'))} symbols, {_e(sm.get('calls'))} calls "
                   f"({_e(sm.get('resolved_calls'))} resolved by unique name) in {_e(sm.get('files'))} files.</p>"
                   f"<h3>Complexity hotspots</h3>{hs}<h3>Modules</h3>{mods}<ul>{lim}</ul>")
    ov = result.findings_overview
    if ov is not None:
        out.append(f"<h2>Findings overview (Phase 6)</h2><p class='muted'>{_e(ov.get('note', ''))}</p><div class='scroll'>"
                   f"<h3>By severity</h3>{_kv_table(ov.get('by_severity'))}<h3>By category</h3>{_kv_table(ov.get('by_category'))}"
                   f"<h3>By source</h3>{_kv_table(ov.get('by_source'))}</div>")
    if result.ai_notes:
        rows = "".join(f"<details><summary>{_e(n.get('task'))} - {_e(n.get('provider'))}/{_e(n.get('model'))} "
                       f"(sent: {_e(', '.join(n.get('sent_items', [])))})</summary><pre>{_e(redact(str(n.get('text', ''))))}</pre></details>"
                       for n in result.ai_notes)
        out.append(f"<h2>AI-assisted notes (Phase 7) - UNVERIFIED</h2><p><b>AI output is a hypothesis, not a finding. It has not been "
                   f"verified by any test or tool.</b></p>{rows}")
    if result.patches:
        rows = _table(["Patch", "File", "Source", "Status", "Verification level", "Claim"], [
            [_e(x["patch"].get("id")), f"<code>{_e(x['patch'].get('file'))}</code>", _e(x["patch"].get("source")),
             _e(x["patch"].get("status")), _e(x["verification"].get("level")), _e(x["verification"].get("claim"))]
            for x in result.patches])
        out.append(f"<h2>Human-approved fixes (Phase 8)</h2>{rows}")
    return "\n".join(out)


def to_html(result: AnalysisResult) -> str:
    a = result.archive
    sev_counts: Dict[str, int] = {}
    for f in result.findings:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1
    cards = "".join(f"<div class='card'><b>{_e(v)}</b><span>{_e(k)}</span></div>" for k, v in [
        ("Files", f"{a.file_count:,}"), ("Uncompressed", _size(a.total_uncompressed)),
        ("Text files scanned", f"{a.scanned_text_files:,}"), ("Findings", f"{len(result.findings):,}"),
        ("Skipped entries", f"{a.skipped_total:,}"), ("Duration", f"{result.duration_seconds}s")])
    sev = " ".join(_badge(f"{k}: {v}", SEV_COLOR[k]) for k, v in sev_counts.items()) or "<span class='muted'>No findings.</span>"
    langs = _table(["Language", "Files", "Size", "% of bytes", "Confidence", "Evidence"], [
        [_e(l.language), l.files, _size(l.bytes), l.percent_of_bytes,
         _badge(l.confidence, CONF_COLOR.get(l.confidence, "#444")), _e("; ".join(l.evidence))] for l in result.languages])
    manifests = _table(["File", "Ecosystem", "Kind", "Dependencies"], [
        [f"<code>{_e(m.path)}</code>", _e(m.ecosystem), _e(m.kind),
         _e(", ".join(f"{d.name} {d.spec}".strip() + (f" (line {d.line})" if d.line else "") for d in m.dependencies[:40])
            + (f" ... +{len(m.dependencies) - 40} more" if len(m.dependencies) > 40 else "") + (f" [{m.parse_error}]" if m.parse_error else ""))]
        for m in result.manifests])
    findings = _table(["ID", "Severity", "Category", "Title", "Location", "Evidence", "Confidence / verification", "Remediation"], [
        [_e(f.id), _badge(f.severity, SEV_COLOR.get(f.severity, "#444")), _e(f.category), _e(f.title),
         f"<code>{_e(f.file)}{':' + str(f.line) if f.line else ''}</code>", f"<code>{_e(redact(f.evidence))}</code>",
         _e(f"{f.confidence} / {f.verification} ({f.source})"), _e(f.remediation)] for f in result.findings])
    skipped = _table(["Entry", "Reason"], [[f"<code>{_e(s['name'])}</code>", _e(s["reason"])] for s in a.skipped_entries])
    warns = "<ul>" + "".join(f"<li>{_e(w)}</li>" for w in result.warnings) + "</ul>" if result.warnings else "<p class='muted'>None.</p>"
    lims = "<ul>" + "".join(f"<li>{_e(x)}</li>" for x in result.limitations) + "</ul>"
    env = _e(", ".join(f"{k}: {v}" for k, v in result.environment.items()))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Analysis report - {_e(result.archive_name)}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1f2937;background:#f8fafc}}
h1{{margin:0 0 4px}} h2{{margin-top:28px;border-bottom:2px solid #e2e8f0;padding-bottom:4px}}
.muted{{color:#6b7280}} .cards{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:10px 16px;min-width:120px}}
.card b{{display:block;font-size:1.3em}} .card span{{color:#6b7280;font-size:.85em}}
.scroll{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;background:#fff;font-size:.9em}}
th,td{{border:1px solid #e2e8f0;padding:6px 8px;text-align:left;vertical-align:top}} th{{background:#f1f5f9}}
.badge{{color:#fff;border-radius:10px;padding:1px 8px;font-size:.8em;white-space:nowrap}}
ul.tree{{list-style:none;padding-left:14px;font-family:Consolas,monospace;font-size:.85em}}ul.tree ul{{list-style:none;padding-left:16px}}
code{{background:#f1f5f9;padding:1px 4px;border-radius:3px;word-break:break-all}}
</style></head><body>
<h1>AI Software Engineering Agent - Analysis Report</h1>
<p class="muted">Archive: <b>{_e(result.archive_name)}</b> | Analyzed: {_e(result.analyzed_at)} | Tool v{_e(result.tool_version)} | {env}</p>
<div class="cards">{cards}</div>
<p>{sev}</p>
<h2>Languages</h2>{langs}
<h2>Frameworks and runtimes</h2>{_detections(result.frameworks)}
<h2>Database indicators</h2>{_detections(result.databases)}
<h2>Dependency manifests</h2>{manifests}
{_structure_html(result)}
{_phases_html(result)}
<h2>Findings</h2>{findings}
<h2>Skipped archive entries</h2>{skipped}
<h2>Warnings</h2>{warns}
<h2>Known limitations</h2>{lims}
</body></html>"""


def _safe_stem(name: str) -> str:
    stem = re.sub(r"\.zip$", "", name, flags=re.I)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:60] or "archive"


def write_reports(result: AnalysisResult, out_dir, delivery: bool = True) -> Dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = f"{_safe_stem(result.archive_name)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    paths = {"json": out / f"{base}.json", "html": out / f"{base}.html"}
    paths["json"].write_text(to_json(result), encoding="utf-8")
    paths["html"].write_text(to_html(result), encoding="utf-8")
    if delivery:                       # Phase 10: summary + section reports in <base>_reports/ (never breaks the main reports)
        try:
            from core.delivery_packager import write_delivery_reports
            ddir = out / f"{base}_reports"
            extra = write_delivery_reports(result, ddir)
            paths["reports_dir"] = ddir
            paths["summary_md"] = Path(extra["analysis_summary.md"])
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"Phase 10 section reports failed: {type(exc).__name__}: {str(exc)[:100]}")
    return paths
