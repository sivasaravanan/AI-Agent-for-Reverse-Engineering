"""HTML/JSON export helpers so Phase 1/10 report generators can embed findings."""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Iterable, Optional

from .models import Finding, Kind, Severity
from .store import sort_findings

_CSS = ("body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}"
        "h1,h2{margin:.6em 0 .3em}.f{border:1px solid #ccc;border-left-width:6px;border-radius:4px;margin:8px 0;padding:6px 10px}"
        ".critical{border-left-color:#8b0000}.high{border-left-color:#d9534f}.medium{border-left-color:#f0ad4e}"
        ".low{border-left-color:#5bc0de}.info{border-left-color:#999}"
        "pre{background:#f6f6f6;padding:6px;overflow:auto;white-space:pre-wrap}"
        ".b{display:inline-block;background:#eee;border-radius:3px;padding:0 6px;margin-right:4px;font-size:12px}"
        ".ai{background:#fff3cd}.note{background:#fff3cd;padding:6px 10px;border-radius:4px}")


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def render_html_section(findings: Iterable[Finding], heading: str = "Findings") -> str:
    items = sort_findings(findings)
    parts = [f"<section><h2>{_e(heading)} ({len(items)})</h2>"]
    if any(f.kind is Kind.AI_HYPOTHESIS for f in items):
        parts.append('<p class="note">Items marked <b>AI hypothesis</b> are unverified suggestions, '
                     "not detected faults.</p>")
    for sev in sorted(Severity, key=lambda s: -s.rank):
        group = [f for f in items if f.severity is sev]
        if not group:
            continue
        parts.append(f"<h3>{_e(sev.value.title())} ({len(group)})</h3>")
        for f in group:
            ai = " ai" if f.kind is Kind.AI_HYPOTHESIS else ""
            parts.append(f'<div class="f {sev.value}"><b>{_e(f.title)}</b><br>'
                         f'<span class="b">{_e(f.id)}</span><span class="b">{_e(f.category)}</span>'
                         f'<span class="b{ai}">{_e(f.kind.value)}</span>'
                         f'<span class="b">confidence: {_e(f.confidence.value)}</span>'
                         f'<span class="b">verification: {_e(f.verification.value)}</span>')
            if f.location:
                parts.append(f"<br>Location: <code>{_e(f.location)}</code>")
            for label, val in (("Evidence", f.evidence), ("Explanation", f.explanation), ("Remediation", f.remediation)):
                if val:
                    parts.append(f"<br><i>{label}</i><pre>{_e(val)}</pre>" if label == "Evidence"
                                 else f"<br><i>{label}:</i> {_e(val)}")
            if f.debug_steps:
                parts.append("<br><i>Suggested debugging steps</i><ol>" +
                             "".join(f"<li>{_e(s)}</li>" for s in f.debug_steps) + "</ol>")
            parts.append(f"<br><small>Source: {_e(f.source)}</small></div>")
    parts.append("</section>")
    return "\n".join(parts)


def render_html_page(findings: Iterable[Finding], title: str = "Findings Report", meta: Optional[dict] = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meta_html = "".join(f"<li>{_e(k)}: {_e(v)}</li>" for k, v in (meta or {}).items())
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(title)}</title><style>{_CSS}</style></head>"
            f"<body><h1>{_e(title)}</h1><p>Generated {stamp}</p><ul>{meta_html}</ul>"
            f"{render_html_section(findings)}</body></html>")


def findings_to_json_report(findings: Iterable[Finding], meta: Optional[dict] = None) -> str:
    items = sort_findings(findings)
    return json.dumps({"schema": "findings/1", "meta": meta or {}, "count": len(items),
                       "findings": [f.to_dict() for f in items]}, indent=2, ensure_ascii=False)
