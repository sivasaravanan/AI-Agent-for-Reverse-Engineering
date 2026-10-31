"""Build a run trace (timeline + diagnostics + hints) from an ExecutionResult, and read source lines for each diagnostic."""
from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.archive_manager import ArchiveError, ArchiveReader
from run_insights.hints import hints_for
from run_insights.parsers import Diagnostic, parse_output
from security.archive_validation import ArchiveLimits

MAX_LINE_CHARS = 300
MAX_FOLDER_FILES = 5000
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "bin", "obj", "target", "dist", "build"}


@dataclass
class RunTrace:
    status: str
    exit_code: Optional[int]
    summary: str
    events: List[Dict[str, str]] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)
    hints: List[Dict[str, str]] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": "1.0", "status": self.status, "exit_code": self.exit_code, "summary": self.summary,
                "events": self.events, "diagnostics": [d.to_dict() for d in self.diagnostics], "hints": self.hints,
                "stats": self.stats, "note": "Hints are heuristics, not verified diagnoses."}


def build_trace(result) -> RunTrace:
    """``result`` is a plugins.build_execution ExecutionResult (or any object/dict with the same fields)."""
    g = (lambda k, d=None: result.get(k, d)) if isinstance(result, dict) else (lambda k, d=None: getattr(result, k, d))
    stdout, stderr = g("stdout", "") or "", g("stderr", "") or ""
    status, code = g("status", "unknown"), g("exit_code")
    diags = parse_output(stdout, stderr)
    argv = " ".join(str(a) for a in (g("argv", []) or []))
    ev: List[Dict[str, str]] = []

    def add(label: str, detail: str = "") -> None:
        ev.append({"label": label, "detail": detail})

    add("Plan", f"{g('candidate_id', '') or 'custom'}: {argv}".strip(": "))
    add("Sandbox", f"{g('backend', '') or 'runtime'} image {g('image', '') or '?'}; network "
                   f"{'ON' if g('network_enabled') else 'off'}; limits {json.dumps(g('limits', {}) or {})}")
    if g("started"):
        add("Started", str(g("started")))
    for n in g("notes", []) or []:
        add("Note", str(n))
    out_lines, err_lines = len(stdout.splitlines()), len(stderr.splitlines())
    add("Output", f"{out_lines} line(s) on stdout, {err_lines} on stderr" + (" (truncated)" if g("output_truncated") else ""))
    first = next((d for d in diags if d.severity == "error"), None)
    if first:
        add("First error", f"{first.tool} {first.code} at {first.location or 'unknown location'}: {first.message[:160]}")
    add("Finished", f"{status}, exit code {code}, {g('duration_s', 0)} s")
    errors = sum(1 for d in diags if d.severity == "error")
    warns = len(diags) - errors
    summary = (f"{status.upper()} (exit {code}) in {g('duration_s', 0)} s - {errors} error(s), {warns} warning(s) located"
               if diags else f"{status.upper()} (exit {code}) in {g('duration_s', 0)} s - no located errors")
    hints = hints_for(status, code, stdout + "\n" + stderr, bool(g("network_enabled")), diags)
    return RunTrace(status, code, summary, ev, diags, hints,
                    {"stdout_lines": out_lines, "stderr_lines": err_lines, "errors": errors, "warnings": warns,
                     "duration_s": g("duration_s", 0)})


# ---------------------------------------------------------------------------- source lookup (read-only, bounded)
def _suffix_pick(names: List[str], rel: str) -> Optional[str]:
    rel = rel.strip("/")
    if not rel:
        return None
    exact = [n for n in names if n == rel]
    if exact:
        return exact[0]
    suff = [n for n in names if n.endswith("/" + rel)]
    if not suff and "/" not in rel:
        suff = [n for n in names if n.rsplit("/", 1)[-1] == rel]
    if not suff:
        parts = rel.split("/")
        for k in range(1, len(parts)):                      # a host path kept some leading folders: drop them one by one
            tail = "/".join(parts[k:])
            suff = [n for n in names if n == tail or n.endswith("/" + tail)]
            if suff:
                break
    return min(suff, key=len) if suff else None


def _window(text: str, line: int, radius: int) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    lo, hi = max(1, line - radius), min(len(lines), line + radius)
    return [{"n": n, "text": lines[n - 1][:MAX_LINE_CHARS], "target": n == line} for n in range(lo, hi + 1)]


def source_context(source, rel_file: str, line: int, radius: int = 4, limits: Optional[ArchiveLimits] = None) -> Dict[str, Any]:
    """{'path', 'lines': [{'n','text','target'}], 'note'} for a diagnostic location. Never executes or extracts anything."""
    res: Dict[str, Any] = {"path": "", "lines": [], "note": ""}
    if not source or not rel_file or line <= 0:
        res["note"] = "No source location."
        return res
    src = Path(str(source))
    try:
        if src.is_file():
            with ArchiveReader(src, limits or ArchiveLimits()) as r:
                ok = {e.name: e for e in r.entries if not e.is_dir and not e.skip_reason}
                name = _suffix_pick(list(ok), rel_file)
                if not name:
                    res["note"] = f"{rel_file} is not in the project."
                    return res
                text = r.read_text(ok[name])
        elif src.is_dir():
            root = src.resolve()
            names: List[str] = []
            for dp, dn, fn in os.walk(root, followlinks=False):
                dn[:] = [d for d in dn if d not in SKIP_DIRS]
                for f in fn:
                    names.append(str(Path(dp, f).relative_to(root)).replace("\\", "/"))
                if len(names) > MAX_FOLDER_FILES:
                    break
            name = _suffix_pick(names, rel_file)
            if not name:
                res["note"] = f"{rel_file} is not in the project."
                return res
            full = (root / name).resolve()
            if root not in full.parents or not full.is_file() or full.stat().st_size > 2_000_000:
                res["note"] = "File not readable (outside the project or too large)."
                return res
            text = full.read_text(encoding="utf-8", errors="replace")
        else:
            res["note"] = "Source not found."
            return res
    except (ArchiveError, OSError) as exc:
        res["note"] = f"Could not read the source: {type(exc).__name__}"
        return res
    if text is None:
        res["note"] = "Binary file."
        return res
    res["path"] = name
    res["lines"] = _window(text, line, radius)
    if not res["lines"]:
        res["note"] = f"{name} has fewer than {line} lines."
    return res


# ---------------------------------------------------------------------------- rendering
def render_text(t: RunTrace) -> str:
    out = [t.summary, "", "TIMELINE"]
    out += [f"  - {e['label']}: {e['detail']}" for e in t.events]
    out += ["", "DIAGNOSTICS"] + ([f"  [{d.severity}] {d.tool} {d.code} {d.location}: {d.message}" for d in t.diagnostics]
                                  or ["  (none located)"])
    for d in t.diagnostics:
        if d.frames:
            out.append(f"  call trace for {d.code or d.tool} at {d.location}:")
            out += [f"      {f}:{l} in {fn}" for f, l, fn in d.frames]
    out += ["", "HINTS (heuristics)"] + ([f"  [{h['level']}] {h['text']}" for h in t.hints] or ["  (none)"])
    return "\n".join(out)


def render_html(t: RunTrace, source=None) -> str:
    e = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    rows = "".join(f"<tr><td>{e(d.severity)}</td><td>{e(d.tool)}</td><td>{e(d.code)}</td><td>{e(d.location)}</td>"
                   f"<td>{e(d.message)}</td></tr>" for d in t.diagnostics)
    snippets = ""
    for d in t.diagnostics[:20]:
        if d.file and d.line and source:
            c = source_context(source, d.file, d.line)
            if c["lines"]:
                body = "".join(f"<div class='{'hit' if x['target'] else ''}'>{x['n']:>5}  {e(x['text'])}</div>" for x in c["lines"])
                snippets += f"<h4>{e(c['path'])}:{d.line}</h4><pre>{body}</pre>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><title>Run trace</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px}}table{{border-collapse:collapse;font-size:13px}}
td,th{{border:1px solid #ddd;padding:3px 8px;text-align:left}}pre{{background:#f6f8fa;padding:8px;overflow:auto}}
.hit{{background:#fde8e8}}</style></head><body><h1>Run trace</h1><p><b>{e(t.summary)}</b></p>
<h2>Timeline</h2><ul>{''.join(f"<li><b>{e(x['label'])}</b>: {e(x['detail'])}</li>" for x in t.events)}</ul>
<h2>Diagnostics</h2><table><tr><th>Severity</th><th>Tool</th><th>Code</th><th>Location</th><th>Message</th></tr>{rows}</table>
{snippets}<h2>Hints (heuristics, not verified)</h2><ul>{''.join(f"<li>{e(h['text'])}</li>" for h in t.hints)}</ul></body></html>"""
