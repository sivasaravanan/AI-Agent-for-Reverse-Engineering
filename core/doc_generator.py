"""Deterministic technical documentation (master prompt item 13) generated from the analysis - no AI, nothing invented.

Sections without data say so. Everything is derived from detected facts; unknowns are marked TODO.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from models.analysis_result import AnalysisResult
from security.secret_scanner import redact


def _md(s: Any) -> str:
    return redact(str(s if s is not None else "")).replace("|", "\\|").replace("\n", " ")


def _table(headers: List[str], rows: List[List[Any]], empty: str) -> str:
    if not rows:
        return f"_{empty}_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_md(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def generate_documentation(r: AnalysisResult, project_name: str = "") -> str:
    name = project_name or Path(r.archive_name).stem or "project"
    st, cm = r.structure, r.code_map or {}
    o: List[str] = [f"# {name} - technical documentation", "",
                    f"_Generated from static analysis on {r.analyzed_at} by AI Software Engineering Agent {r.tool_version}. "
                    "Facts below are detected, not verified at runtime. Items marked TODO need a human._", "",
                    "## 1. Overview", ""]
    o.append(f"- Files: {r.archive.file_count:,} (text files scanned: {r.archive.scanned_text_files:,})")
    o.append("- Languages: " + (", ".join(f"{l.language} ({l.files} files, {l.confidence})" for l in r.languages) or "none detected"))
    o.append("- Frameworks: " + (", ".join(f"{d.name} ({d.confidence})" for d in r.frameworks) or "none detected"))
    o.append("- Databases: " + (", ".join(f"{d.name} ({d.confidence})" for d in r.databases) or "none detected"))
    o += ["- Purpose: TODO (cannot be determined from static analysis)", "", "## 2. How to run", ""]
    eps = st.entry_points if st else []
    o.append(_table(["Entry point", "Kind", "Language", "Confidence"], [[f"{e.file}:{e.line or '-'}", e.kind, e.language, e.confidence] for e in eps],
                    "No entry points detected."))
    ex = (r.execution or {}).get("candidates", [])
    o.append("Commands discovered from manifests (not executed):\n")
    o.append(_table(["Id", "Command", "Needs network"], [[c["id"], " ".join(c["argv"]), "yes" if c["needs_network"] else "no"] for c in ex],
                    "No build/test commands were discovered."))
    o += ["## 3. Dependencies", ""]
    rows = [[d.name, d.spec, m.ecosystem, f"{d.file}:{d.line or '-'}"] for m in r.manifests for d in m.dependencies]
    o.append(_table(["Package", "Version", "Ecosystem", "Declared in"], rows[:300], "No dependencies declared."))
    if len(rows) > 300:
        o.append(f"_{len(rows) - 300} more not shown._\n")
    o += ["## 4. Configuration and environment", ""]
    if st:
        o.append(_table(["Variable", "Status", "First reference"], [[v.name, v.status, f"{v.references[0].file}:{v.references[0].line}" if v.references else ""] for v in st.env_vars],
                        "No environment variables referenced."))
        o.append(_table(["Config file", "Kind"], [[c.path, c.kind] for c in st.config_files], "No configuration files found."))
    o += ["## 5. Data layer", ""]
    db = r.database_analysis or {}
    o.append(_table(["Database", "Evidence level", "Rationale"], [[d.get("display"), d.get("status"), d.get("rationale")] for d in db.get("databases", [])],
                    "No database evidence."))
    o.append(_table(["ORM", "Entity", "Table", "Location"], [[e["orm"], e["name"], e.get("table"), f"{e['file']}:{e['line']}"] for e in db.get("orm_entities", [])[:100]],
                    "No ORM entities detected."))
    o.append(_table(["Kind", "Name", "Location"], [[s["kind"], s["name"], f"{s['file']}:{s['line']}"] for s in db.get("schema_objects", [])[:150]],
                    "No schema objects (DDL) detected."))
    o += ["## 6. Code structure", ""]
    by_file: Dict[str, List[dict]] = defaultdict(list)
    for s in cm.get("symbols", []):
        by_file[s["file"]].append(s)
    if not by_file:
        o.append("_No code map available._\n")
    for f in sorted(by_file)[:150]:
        o.append(f"### `{f}`\n")
        for s in by_file[f][:80]:
            params = f"({', '.join(s['params'])})" if s["method"] == "ast" and s["kind"] in ("function", "method") else ""
            doc = "" if s["doc"] or s["method"] != "ast" else " - TODO: undocumented"
            o.append(f"- {s['kind']} `{s['qualname']}{params}` (line {s['line']}){doc}")
        o.append("")
    if st:
        o += ["### Module relationships", "",
              _table(["Most depended-on file", "Imported by"], [[x["file"], x["count"]] for x in st.import_summary.most_depended_on], "None."),
              "Circular imports: " + (", ".join(" <-> ".join(c) for c in st.import_summary.cycles) or "none found among resolved imports."), ""]
    if cm.get("hotspots"):
        o += ["### Complexity hotspots", "", _table(["Function", "Lines", "Complexity", "Location"],
              [[h["name"], h["lines"], h["complexity"], f"{h['file']}:{h['line']}"] for h in cm["hotspots"][:20]], ""), ""]
    o += ["## 7. Known issues (detected)", ""]
    top = [f for f in r.findings if f.severity in ("critical", "high", "medium")][:60]
    o.append(_table(["ID", "Severity", "Location", "Title", "Verification"], [[f.id, f.severity, f"{f.file}:{f.line or '-'}", f.title, f.verification] for f in top],
                    "No medium-or-higher findings."))
    o += ["## 8. Limitations of this document", ""] + [f"- {x}" for x in [*r.limitations, *cm.get("limitations", [])]]
    return "\n".join(o) + "\n"


def write_documentation(r: AnalysisResult, out_dir, project_name: str = "") -> Path:
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    stem = "".join(c if c.isalnum() or c in "._-" else "_" for c in (project_name or Path(r.archive_name).stem or "project"))[:60]
    path = p / f"{stem}_documentation.md"
    path.write_text(generate_documentation(r, project_name), encoding="utf-8")
    return path
