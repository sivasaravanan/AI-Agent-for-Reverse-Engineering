"""Phase 2 coordinator: combines tree, entry points, imports, configuration, confidence and warnings."""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Mapping, Sequence

from core.architecture_analyzer import MAX_EDGES_KEPT, analyze_imports
from core.archive_manager import ArchiveEntry
from core.config_discovery import find_db_connections, find_env_vars
from core.dependency_analyzer import identify_manifest
from core.project_inventory import build_tree, find_entry_points, inventory_config_files
from models.analysis_result import CONFIDENCE_RANK, Detection, LanguageStat, Manifest
from models.project_info import ConfidenceIndicator, StructureResult, StructureWarning

PHASE2_LIMITATIONS = [
    "Structure analysis is static: imports are read from source text/AST and never executed or resolved by a package manager.",
    "Import resolution inside the archive is heuristic (suffix matching); ambiguous names may resolve to the wrong file.",
    "Third-party import vs declared-dependency checks are only made for Python and JavaScript/TypeScript, and package/import name mismatches can cause false positives.",
    "Environment-variable and connection-configuration discovery finds literal names/strings only; values supplied at runtime are unknown.",
    "Entry points marked 'weak' come from file-name conventions only.",
]
ECOSYSTEM_LANG = {"Python": "python", "JavaScript": "javascript", "TypeScript": "javascript", "C#": "dotnet",
                  "VB.NET": "dotnet", "Java": "java", "Go": "go", "PHP": "php"}
_SRC_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".cs", ".vb", ".java", ".go", ".php")
FW_IMPORT_ALIAS = {"Next.js": "next", "Angular": "@angular/core", "Spring Boot": "org.springframework", "Laravel": "illuminate",
                   "ASP.NET Core": "microsoft.aspnetcore", "Entity Framework Core": "microsoft.entityframeworkcore",
                   "Gin": "github.com/gin-gonic/gin", "Tkinter": "tkinter", "xUnit": "xunit", "pytest": "pytest",
                   "Windows Forms": "system.windows.forms", "WPF": "system.windows"}


def _source_levels(d: Detection) -> set:
    """Evidence levels excluding manifest lines: a declared package is not proof of use."""
    return {ev.level for ev in d.evidence if not identify_manifest(ev.file)}


def _norm(s: str) -> str:
    return re.sub(r"[-_.]+", "-", s.lower())


def analyze_structure(entries: Sequence[ArchiveEntry], texts: Mapping[str, str], manifests: Sequence[Manifest],
                      languages: Sequence[LanguageStat], frameworks: Sequence[Detection],
                      databases: Sequence[Detection]) -> StructureResult:
    res = StructureResult(limitations=list(PHASE2_LIMITATIONS))
    usable = [e for e in entries if not e.is_dir and not e.skip_reason and not e.vendored]
    res.tree = build_tree(entries)
    res.entry_points = find_entry_points(texts, [e.name for e in usable])
    res.config_files = inventory_config_files(entries)
    edges, summary = analyze_imports(texts, manifests)
    res.import_summary = summary
    res.imports = edges[:MAX_EDGES_KEPT]
    res.imports_truncated = max(0, len(edges) - MAX_EDGES_KEPT)
    res.env_vars = find_env_vars(texts)
    res.db_connections = find_db_connections(texts, res.env_vars)
    _dependency_summary(res, manifests)
    res.confidence = _confidence(languages, frameworks, databases, manifests, res)
    res.warnings = _warnings(languages, databases, manifests, res)
    return res


# --------------------------------------------------------------------------- dependencies
def _dependency_summary(res: StructureResult, manifests: Sequence[Manifest]) -> None:
    for m in manifests:
        d = res.dependency_summary.setdefault(m.ecosystem, {"manifests": 0, "dependencies": 0, "unpinned": 0})
        d["manifests"] += 1
        for dep in m.dependencies:
            d["dependencies"] += 1
            spec = dep.spec.strip()
            unpinned = (not spec) or spec in ("*", "latest", "x") or (m.ecosystem == "python" and not re.search(r"[=~!]", spec))
            if m.ecosystem == "python" and spec.startswith(">=") and "," not in spec:
                unpinned = False  # a lower bound is a constraint, though a loose one
            if unpinned:
                d["unpinned"] += 1
                if len(res.unpinned_dependencies) < 100:
                    res.unpinned_dependencies.append({"name": dep.name, "file": dep.file, "line": dep.line,
                                                      "ecosystem": m.ecosystem})


# --------------------------------------------------------------------------- confidence indicators
def _confidence(languages, frameworks, databases, manifests, res: StructureResult) -> List[ConfidenceIndicator]:
    out: List[ConfidenceIndicator] = []
    ext_pkgs = {r["name"].lower() for r in res.import_summary.external_packages}
    ext_pkgs |= {_norm(r["name"]) for r in res.import_summary.external_packages}
    langs_with_entry = {e.language for e in res.entry_points if e.confidence != "weak"}
    lang_with_imports = set(res.import_summary.by_language)
    for l in languages:
        reasons = list(l.evidence)
        has_marker = any(r.startswith("marker file") for r in reasons)
        has_use = l.language in lang_with_imports or any(l.language in e for e in langs_with_entry) \
            or (l.language in ("JavaScript", "TypeScript") and "JavaScript/TypeScript" in langs_with_entry)
        if has_marker and l.files >= 1 and has_use:
            cls = "confirmed"
            reasons.append("source imports/entry point found in addition to manifest and files")
        elif (has_marker and l.files >= 1) or l.files >= 3:
            cls = "strong"
        else:
            cls = "weak"
            reasons.append("few files and no project marker")
        out.append(ConfidenceIndicator(l.language, "language", cls, reasons))
    for d in frameworks:
        declared = imported = marker = False
        reasons = []
        for ev in d.evidence:
            if identify_manifest(ev.file):
                declared = True
                reasons.append(f"declared in {ev.file}:{ev.line or '-'}")
            elif re.match(r"\s*(from|import|const|var|let)\b|require\(", ev.text):
                imported = True
                reasons.append(f"imported in {ev.file}:{ev.line or '-'}")
            else:
                marker = True
                reasons.append(f"marker in {ev.file}:{ev.line or '-'}")
        alias = FW_IMPORT_ALIAS.get(d.name, d.name).lower()
        if not imported and d.kind != "runtime" and any(alias == p or _norm(alias) == p or p.startswith(alias) for p in ext_pkgs):
            imported = True
            reasons.append("import graph contains this package")
        if d.kind == "runtime":
            cls = "strong" if declared else "weak"
        elif declared and imported:
            cls = "confirmed"
        elif declared:
            cls = "strong"
            reasons.append("declared, but no usage found in scanned source")
        elif imported:
            cls = "strong"
            reasons.append("used in source but not declared in a parsed manifest")
        else:
            cls = "weak"
        out.append(ConfidenceIndicator(d.name, "framework", cls, reasons[:6]))
    for d in databases:
        levels = _source_levels(d)
        cfg = [c for c in res.db_connections if c.database == d.name and c.confidence == "strong"]
        reasons = [f"{ev.level or d.confidence}: {ev.file}:{ev.line or '-'}" for ev in d.evidence[:4]]
        if d.kind == "schema":
            cls = "strong"
            reasons.append("schema/DDL present; database engine not identified")
        elif (cfg or "strong" in levels) and "moderate" in levels:
            cls = "confirmed"
            reasons.append("connection configuration AND driver usage in code")
        elif cfg or "strong" in levels:
            cls = "strong"
            reasons.append("connection configuration found; no driver usage seen in scanned code")
        else:
            cls = "weak"
            reasons.append("driver/mention only - not proof the database is configured or used")
        out.append(ConfidenceIndicator(d.name, "database", cls, reasons))
    if not databases:
        unknown_env = [c for c in res.db_connections if c.form == "env-reference"]
        out.append(ConfidenceIndicator("database", "database", "unknown", [
            "no database indicators found" if not unknown_env else
            "database settings are read from environment variables; the target database is unknown"]))
    order = {"confirmed": 0, "strong": 1, "weak": 2, "unknown": 3}
    out.sort(key=lambda c: (c.kind, order[c.evidence_class], c.subject))
    return out


# --------------------------------------------------------------------------- warnings
def _warnings(languages, databases, manifests, res: StructureResult) -> List[StructureWarning]:
    w: List[StructureWarning] = []
    add = lambda code, sev, msg, f="", l=None: w.append(StructureWarning(code, sev, msg, f, l))  # noqa: E731
    ecos = {m.ecosystem for m in manifests if m.kind == "manifest"}
    for l in languages:
        eco = ECOSYSTEM_LANG.get(l.language)
        if eco and l.files >= 3 and eco not in ecos:
            add("missing-manifest", "low", f"{l.files} {l.language} files but no dependency manifest was found; "
                "dependencies cannot be determined.")
    if languages and not res.entry_points:
        add("no-entry-point", "info", "No entry point was identified; how the project starts is unknown.")
    strong_eps = [e for e in res.entry_points if e.confidence == "strong"]
    if len(strong_eps) > 5:
        add("many-entry-points", "info", f"{len(strong_eps)} strong entry points found; the primary one is ambiguous.",
            strong_eps[0].file, strong_eps[0].line)
    # databases
    strong_db = {c.database for c in res.db_connections if c.confidence == "strong"}
    for d in databases:
        if d.kind == "schema":
            continue
        if d.name not in strong_db and d.confidence in ("moderate", "weak"):
            ev = d.evidence[0] if d.evidence else None
            add("db-driver-without-config", "info",
                f"{d.name}: driver or mention found but no connection configuration was identified.",
                ev.file if ev else "", ev.line if ev else None)
        elif d.name in strong_db and d.confidence == "strong" and "moderate" not in _source_levels(d):
            c = next(c for c in res.db_connections if c.database == d.name and c.confidence == "strong")
            add("db-config-without-driver", "low",
                f"{d.name}: connection configuration found but no driver usage was seen in scanned code.", c.file, c.line)
    if len(strong_db) > 1:
        add("multiple-databases", "info", "Connection configuration for several databases was found "
            f"({', '.join(sorted(strong_db))}); confirm which is primary.")
    # environment variables
    undefined = [v for v in res.env_vars if v.status == "undefined-in-archive"]
    for v in undefined[:25]:
        u = v.references[0]
        add("env-undefined", "info", f"Environment variable {v.name} is read but not defined anywhere in the archive.",
            u.file, u.line)
    if len(undefined) > 25:
        add("env-undefined", "info", f"{len(undefined) - 25} more undefined environment variables not listed.")
    for v in res.env_vars:
        if v.status == "template-only":
            add("env-template-only", "info", f"{v.name} appears only in an example/template env file.", v.defined_in[0])
    for c in res.db_connections:
        if c.form == "env-reference":
            v = next((x for x in res.env_vars if x.name == c.key), None)
            if v and v.status == "undefined-in-archive":
                add("db-env-unresolved", "low", f"Database setting {c.key} comes from the environment and is not defined "
                    "in the archive; the target database is unknown.", c.file, c.line)
    if res.config_files and not any(c.kind == "environment file" for c in res.config_files) and res.env_vars \
            and any(v.status == "undefined-in-archive" for v in res.env_vars):
        add("no-env-file", "info", "Environment variables are used but no .env or example env file was included.")
    # imports
    for r in res.import_summary.external_packages:
        if r["declared"] is False:
            add("undeclared-import", "low", f"{r['language']} package '{r['name']}' is imported ({r['count']}x) but not "
                "found in a parsed manifest (name mismatches are possible).", r["first_file"], r["first_line"])
    for cyc in res.import_summary.cycles:
        add("import-cycle", "low", "Circular import between: " + ", ".join(cyc[:6]), cyc[0])
    if res.unpinned_dependencies:
        first = res.unpinned_dependencies[0]
        add("unpinned-dependencies", "info", f"{len(res.unpinned_dependencies)} dependencies have no version constraint "
            f"(e.g. {first['name']}).", first["file"], first["line"])
    for m in manifests:
        if m.parse_error:
            add("manifest-parse-error", "low", f"{m.path}: {m.parse_error}", m.path)
    sev = {"medium": 0, "low": 1, "info": 2}
    w.sort(key=lambda x: (sev.get(x.severity, 3), x.code, x.file))
    return w
