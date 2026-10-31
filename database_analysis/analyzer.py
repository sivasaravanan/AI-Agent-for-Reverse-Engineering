"""Orchestrator: walks a file provider, runs detectors, aggregates the result.
Pure static analysis - no project code is executed and no network/database connection is made."""
from __future__ import annotations

import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Iterable, List, Mapping, Optional, Union

from . import __version__
from . import signatures as sig
from .collector import Collector
from .context import (DATA_FILE_EXTS, LOCKFILES, FileContext, build_context, classify, decode_text, manifest_kind)
from .detectors import (detect_config_keys, detect_connections, detect_credential_literals, detect_env_refs,
                        detect_imports, detect_injection, detect_mentions, detect_providers, detect_services)
from .manifests import parse_manifest
from .models import (CONFIRMED, LEVEL_RANK, SCOPE_EXAMPLE, SCOPE_PROD, SCOPE_TEST, SEVERITY_RANK, STRONG, WEAK,
                     DatabaseAnalysisResult, DatabaseDetection, Evidence)
from .options import AnalysisOptions
from .orm import detect_migration, detect_orm
from .sources import ArchiveLimits, DirectoryProvider, FileProvider, MemoryProvider, ZipProvider
from .sqlscan import detect_sql

# A custom detector receives (FileContext, Collector). Use collector.add(...) / .connection(...) / .finding(...)
Detector = Callable[[FileContext, Collector], None]

LIMITATIONS = [
    "Static text analysis only: no project code is executed and no database connection is ever attempted.",
    "'confirmed' means explicit configuration was found - not that the database is reachable, that credentials "
    "work, or that the code path is exercised at runtime.",
    "Comments are skipped using simple per-language rules; string literals and docstrings are not distinguished "
    "from code, so a connection string inside a docstring can still be reported.",
    "Files larger than the configured size limit, binary files (except SQLite file headers), and third-party "
    "folders (node_modules, venv, vendor, bin/obj, ...) are not scanned.",
    "Dialect detection from SQL keywords is heuristic; ORM entity discovery covers common patterns only.",
    "SQLite/SQL Server data files are detected by name/header but their contents are never opened or parsed.",
]


def _is_data_file(ext: str) -> bool:
    return ext in DATA_FILE_EXTS


class DatabaseAnalyzer:
    def __init__(self, options: Optional[AnalysisOptions] = None,
                 extra_detectors: Optional[Iterable[Detector]] = None):
        self.opts = options or AnalysisOptions()
        self.extra_detectors: List[Detector] = list(extra_detectors or [])

    # ----------------------------------------------------------- public entry points
    def analyze_zip(self, zip_path: Union[str, os.PathLike], limits: Optional[ArchiveLimits] = None) -> DatabaseAnalysisResult:
        with ZipProvider(zip_path, limits or ArchiveLimits()) as p:
            return self.analyze(p)

    def analyze_directory(self, root: Union[str, os.PathLike]) -> DatabaseAnalysisResult:
        with DirectoryProvider(root) as p:
            return self.analyze(p)

    def analyze_files(self, files: Mapping[str, Union[str, bytes]]) -> DatabaseAnalysisResult:
        with MemoryProvider(files) as p:
            return self.analyze(p)

    # ----------------------------------------------------------- core
    def analyze(self, provider: FileProvider) -> DatabaseAnalysisResult:
        t0 = time.perf_counter()
        col = Collector(self.opts)
        files = sorted(provider.list_files(), key=lambda f: f.path)
        stats: Counter = Counter()
        cancelled = False
        total = len(files)
        for idx, fi in enumerate(files):
            if self.opts.cancel and self.opts.cancel():
                cancelled = True
                break
            if self.opts.progress:
                self.opts.progress(idx, total, fi.path)
            stats["files_total"] += 1
            self._scan_one(provider, fi, col, stats)
        if self.opts.progress and not cancelled:
            self.opts.progress(total, total, "")
        result = self._finalize(col, provider, stats, time.perf_counter() - t0)
        result.cancelled = cancelled
        return result

    def _scan_one(self, provider: FileProvider, fi, col: Collector, stats: Counter) -> None:
        path = fi.path
        parts = path.lower().split("/")
        base = parts[-1]
        if any(p in self.opts.skip_dirs for p in parts[:-1]):
            col.skipped["third-party/build directory"] += 1
            return
        if base in LOCKFILES or base.endswith((".lock", ".min.js", ".map")):
            col.skipped["lock/minified file"] += 1
            return
        ext = os.path.splitext(base)[1]
        if _is_data_file(ext):
            self._data_file(provider, fi, col, ext)
            return
        category, lang = classify(path)
        if category == "other":
            stats["files_not_relevant"] += 1
            return
        if fi.size > self.opts.max_file_bytes:
            col.skipped["file too large"] += 1
            return
        text = decode_text(provider.read(path, self.opts.max_file_bytes))
        if text is None:
            col.skipped["binary or unreadable file"] += 1
            return
        stats["files_scanned"] += 1
        stats["bytes_scanned"] += len(text)
        stats[f"category_{category}"] += 1
        ctx = build_context(path, text, category, lang)
        self._run_detectors(ctx, col)

    def _run_detectors(self, ctx: FileContext, col: Collector) -> None:
        steps = []
        if ctx.category == "manifest":
            steps.append(self._manifest)
        if ctx.category != "docs":
            steps += [detect_imports, detect_connections, detect_providers, detect_config_keys, detect_services, detect_env_refs]
        steps += [detect_sql, detect_orm, detect_injection, detect_mentions]
        for step in steps:
            self._safe(step, ctx, col)
        if ctx.category in ("source", "config", "sql", "manifest", "prisma"):
            self._safe(lambda c, k: detect_migration(c, k), ctx, col)
        for det in self.extra_detectors:
            self._safe(det, ctx, col)
        self._safe(detect_credential_literals, ctx, col)

    @staticmethod
    def _safe(fn, ctx: FileContext, col: Collector) -> None:
        # One malformed file or detector must never abort a whole analysis.
        try:
            fn(ctx, col)
        except Exception as exc:  # noqa: BLE001
            col.skipped[f"detector error ({getattr(fn, '__name__', 'custom')}: {exc.__class__.__name__})"] += 1

    @staticmethod
    def _manifest(ctx: FileContext, col: Collector) -> None:
        for name, ver, line, eco, dev in parse_manifest(ctx):
            rule = sig.match_dependency(name)
            if rule:
                col.dependency(ctx, name, ver, rule[1], rule[0], line, eco, dev)

    def _data_file(self, provider: FileProvider, fi, col: Collector, ext: str) -> None:
        ctx = FileContext(fi.path, fi.path.lower().rsplit("/", 1)[-1], ext, "datafile", None,
                          "production-or-unknown", "", [], [], [])
        from .context import detect_scope
        ctx.scope = detect_scope(fi.path)
        head = provider.read(fi.path, 16)
        if ext in (".sqlite", ".sqlite3", ".db", ".db3", ".s3db"):
            if head.startswith(b"SQLite format 3\x00"):
                col.add("sqlite", CONFIRMED, "data_file", ctx, None,
                        f"SQLite database file included in archive ({fi.size} bytes; contents not opened)")
                col.finding("DB-DATA-001", "medium", "SQLite database file included in the project", fi.path, None,
                            fi.path,
                            "A real SQLite database file is part of the archive. It may contain production or "
                            "personal data. The file was NOT opened or parsed by this analysis.",
                            "Remove data files from source control/archives (or replace with a schema-only seed) "
                            "and add them to .gitignore.", "high", "sqlite", "database-data")
            else:
                col.skipped["non-SQLite .db file (not identified)"] += 1
        elif ext == ".mdf":
            if len(head) >= 4:
                col.add("sqlserver", STRONG, "data_file", ctx, None,
                        f"file with SQL Server data-file extension (.mdf, {fi.size} bytes; not verified, not opened)")
        elif ext == ".bson":
            col.add("mongodb", WEAK, "data_file", ctx, None, "BSON file (MongoDB export/dump format)")

    # ----------------------------------------------------------- aggregation
    def _finalize(self, col: Collector, provider: FileProvider, stats: Counter, elapsed: float) -> DatabaseAnalysisResult:
        for path, reason in provider.skipped:
            col.skipped[f"archive entry skipped: {reason}"] += 1
        detections: List[DatabaseDetection] = []
        for db, evs in col.evidence.items():
            top = max(LEVEL_RANK[e.level] for e in evs)
            status = next(k for k, v in LEVEL_RANK.items() if v == top)
            kinds = col.counts[db]
            scopes = sorted({e.scope for e in evs})
            notes: List[str] = []
            non_weak_kinds = set(kinds) - {"dependency", "documentation"}
            if not non_weak_kinds:
                notes.append("Evidence is limited to dependency declarations and/or documentation mentions; no "
                             "import, connection configuration or schema evidence was found, so the database "
                             "is not treated as confirmed or in use.")
            only_test = all(e.scope == SCOPE_TEST for e in evs if e.level != WEAK) and any(e.level != WEAK for e in evs)
            if only_test:
                notes.append("All non-weak evidence comes from test files; production use is not established.")
            if status == CONFIRMED:
                notes.append("Configuration found; reachability and credentials were not verified (no live "
                             "connection is attempted).")
            if db == "unknown-sql":
                notes.append("A generic driver/API was detected without a connection string naming the database type.")
            rationale = ", ".join(f"{k} x{v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))
            detections.append(DatabaseDetection(db, sig.DB_DISPLAY.get(db, db), status, rationale, dict(kinds),
                                                evs, scopes, notes))
        detections.sort(key=lambda d: (-LEVEL_RANK[d.status], d.db == "unknown-sql", d.db))
        self._cross_findings(col, detections)
        findings = sorted(col.findings, key=lambda f: (-SEVERITY_RANK.get(f.severity, 0), f.file or "", f.line or 0, f.rule))
        for i, f in enumerate(findings, 1):
            f.id = f"DBA-{i:04d}"
        stats_out = dict(stats)
        stats_out["elapsed_ms"] = int(elapsed * 1000)
        stats_out["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return DatabaseAnalysisResult(
            plugin_version=__version__, databases=detections, dependencies=col.dependencies,
            connections=col.connections, schema_objects=col.schema, orm_entities=col.orm,
            migrations=col.migrations, sql_scripts=col.scripts, env_references=sorted(col.envs, key=lambda e: (e.name, e.file)),
            findings=findings, stats=stats_out, skipped=dict(col.skipped), limitations=list(LIMITATIONS))

    def _cross_findings(self, col: Collector, detections: List[DatabaseDetection]) -> None:
        configured = {c.db for c in col.connections} | {
            e.db for evs in col.evidence.values() for e in evs if e.kind in ("provider", "config_key", "service")}
        defined_env = {e.name for e in col.envs if e.kind == "definition"}
        referenced_env = sorted({e.name for e in col.envs if e.kind == "reference"})
        for d in detections:
            if d.db == "unknown-sql":
                continue
            first = lambda kind: next((e for e in d.evidence if e.kind == kind), None)  # noqa: E731
            if d.evidence_counts.get("import") and d.db not in configured:
                e = first("import")
                env_hint = (f" Environment variables referenced by the code: {', '.join(referenced_env[:6])}."
                            if referenced_env else "")
                col.finding(
                    "DB-CFG-001", "info", f"{d.display}: driver used but no connection configuration found",
                    e.file if e else None, e.line if e else None, e.snippet if e else "",
                    "The code imports a database driver but no connection string, provider setting or config "
                    "keys were found in the scanned files. Configuration may be supplied at deployment (environment "
                    "variables, secret store) or be missing." + env_hint,
                    "Document the required settings (e.g. in .env.example) and verify them in the target environment.",
                    "medium", d.db, "database-config")
            if set(d.evidence_counts) <= {"dependency", "documentation"} and d.evidence_counts.get("dependency"):
                e = first("dependency")
                col.finding(
                    "DB-CFG-002", "low", f"{d.display}: dependency declared but no usage found",
                    e.file if e else None, e.line if e else None, e.detail if e else "",
                    "The package is listed as a dependency but no import, connection setting or schema evidence "
                    "was found. It may be unused or used only through code that was not scanned.",
                    "Remove the dependency if unused, or confirm where it is used.", "medium", d.db, "database-config")
        concrete = [d for d in detections if d.db != "unknown-sql" and d.status in (CONFIRMED, STRONG)
                    and (set(d.evidence_counts) - {"dependency", "documentation"})]
        if len(concrete) > 1:
            names = ", ".join(d.display for d in concrete)
            col.finding("DB-CFG-003", "info", "Multiple database technologies detected", None, None, names,
                        f"Evidence for several databases was found: {names}. This is normal for polyglot "
                        "systems but worth confirming (e.g. SQLite in tests, PostgreSQL in production).",
                        "Confirm which database each environment uses.", "medium", None, "database-config")
        for c in col.connections:
            if c.placeholder and c.scope != SCOPE_EXAMPLE:
                col.finding("DB-CFG-004", "info", "Connection settings contain placeholder values", c.file, c.line,
                            c.redacted, "The connection uses template/placeholder values (e.g. <host>, ${DB_HOST}); "
                            "real values must be provided at deployment.",
                            "Make sure the deployment supplies these values.", "medium", c.db, "database-config")
        prod_db = {d.db for d in concrete if any(e.scope == SCOPE_PROD and e.kind in ("connection", "provider", "config_key", "service") for e in d.evidence)}
        for db, per_file in col.sql_dialect_dbs.items():
            if prod_db and db not in prod_db and db not in {d.db for d in concrete if d.evidence_counts.get("import")}:
                f0 = sorted(per_file)[0]
                col.finding("DB-CFG-005", "low", f"SQL written for {sig.DB_DISPLAY.get(db, db)} but configured database differs",
                            f0, None, f0, f"Dialect-specific SQL for {sig.DB_DISPLAY.get(db, db)} was found, while the "
                            f"configured database(s) are: {', '.join(sorted(sig.DB_DISPLAY.get(x, x) for x in prod_db))}.",
                            "Check that scripts match the target database dialect.", "low", db, "database-config")
        for env in referenced_env:
            if env not in defined_env:
                ref = next(e for e in col.envs if e.name == env and e.kind == "reference")
                col.finding("DB-CFG-006", "info", f"Environment variable {env} is referenced but not defined", ref.file,
                            ref.line, env, "The variable is read by the code/config but no definition was found in the "
                            "project files (.env, compose, properties). It must be provided by the runtime environment.",
                            "Add it to .env.example or deployment documentation.", "medium", None, "database-config")


# ---------------------------------------------------------------- convenience functions
def analyze_zip(zip_path, options: Optional[AnalysisOptions] = None, extra_detectors=None) -> DatabaseAnalysisResult:
    return DatabaseAnalyzer(options, extra_detectors).analyze_zip(zip_path)


def analyze_directory(root, options: Optional[AnalysisOptions] = None, extra_detectors=None) -> DatabaseAnalysisResult:
    return DatabaseAnalyzer(options, extra_detectors).analyze_directory(root)


def analyze_files(files: Mapping[str, Union[str, bytes]], options: Optional[AnalysisOptions] = None,
                  extra_detectors=None) -> DatabaseAnalysisResult:
    return DatabaseAnalyzer(options, extra_detectors).analyze_files(files)
