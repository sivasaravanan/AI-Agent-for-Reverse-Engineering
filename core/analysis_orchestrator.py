"""Coordinates a full static analysis of one ZIP archive. No project code is run."""
from __future__ import annotations

import logging
import platform
import posixpath
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from core import __version__
from core.archive_manager import (
    ARCHIVE_EXTENSIONS, ArchiveError, ArchiveReader, is_text_candidate,
)
from core.analysis_options import AnalysisOptions
from core.database_detector import detect_databases
from core.dependency_analyzer import identify_manifest, parse_dependencies
from core.framework_detector import detect_frameworks
from core.language_detector import detect_languages
from core.static_analysis import analyze_text_file
from core.structure_analysis import analyze_structure
from models.analysis_result import AnalysisResult, ArchiveSummary, Manifest
from models.finding import SEVERITY_RANK, Finding
from security.archive_validation import ArchiveLimits, safe_display

log = logging.getLogger("ase.orchestrator")
ProgressCb = Callable[[float, str], None]
MAX_FINDINGS_PER_RULE = 100
MAX_SKIPPED_LISTED = 200

LIMITATIONS = [
    "Static analysis only: no project code, script, build or test was executed.",
    "Heuristic findings (security patterns) are unverified and may be false positives or misses.",
    "Python syntax is always parsed; other languages are checked by external tools only when those tools are installed (see the static-analysis tool list).",
    "Database and framework detection is evidence-based, not proof of runtime behaviour.",
    "Vendored/generated folders (node_modules, bin, obj, build, dist, venv, ...) are excluded from analysis.",
]


class AnalysisCancelled(Exception):
    pass


def analyze_archive(path, limits: Optional[ArchiveLimits] = None, progress: Optional[ProgressCb] = None,
                    cancel: Optional[threading.Event] = None, options: Optional[AnalysisOptions] = None) -> AnalysisResult:
    """Analyze a ZIP, a tar archive (.tar/.tar.gz/.tgz/...) or a folder. Phases 1-2 always run; the optional
    phases follow ``options``. No project code is executed."""
    from core.input_adapter import prepared_input
    limits = limits or ArchiveLimits()
    with prepared_input(path, limits) as prep:
        result = _analyze_zip(prep.zip_path, limits, progress, cancel, options or AnalysisOptions())
    if prep.source_kind != "zip":
        result.archive_name = safe_display(prep.display_name)
        result.input_kind = prep.source_kind
        result.warnings.insert(0, f"Input was a {prep.source_kind}; it was safely converted to a temporary ZIP (symlinks, unsafe names and oversized files skipped).")
        result.warnings.extend(prep.notes[:200])
    return result


def _analyze_zip(path, limits: ArchiveLimits, progress: Optional[ProgressCb], cancel: Optional[threading.Event],
                 options: AnalysisOptions) -> AnalysisResult:
    t0 = time.monotonic()

    def step(frac: float, msg: str) -> None:
        if cancel is not None and cancel.is_set():
            raise AnalysisCancelled()
        if progress:
            progress(frac, msg)

    result = AnalysisResult(
        tool_version=__version__, analyzed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        limitations=list(LIMITATIONS),
        environment={"python": sys.version.split()[0], "platform": platform.platform()},
    )
    step(0.02, "Opening and validating archive...")
    with ArchiveReader(path, limits) as reader:
        result.archive_name = safe_display(reader.path.name)
        result.warnings.extend(reader.warnings)
        entries = reader.entries
        files = [e for e in entries if not e.is_dir]
        summary = ArchiveSummary(
            file_count=len(files), dir_count=len(entries) - len(files),
            total_uncompressed=sum(e.size for e in files), total_compressed=sum(e.compressed_size for e in files),
            vendored_file_count=sum(1 for e in files if e.vendored and not e.skip_reason),
        )
        findings: List[Finding] = []
        skipped = [e for e in entries if e.skip_reason]
        summary.skipped_total = len(skipped)
        for e in skipped[:MAX_SKIPPED_LISTED]:
            summary.skipped_entries.append({"name": safe_display(e.raw_name), "reason": e.skip_reason or ""})
        for e in skipped:
            reason = e.skip_reason or ""
            sev = "high" if ("traversal" in reason or "absolute" in reason or "drive" in reason) else "medium"
            findings.append(Finding(
                rule_id="archive-unsafe-entry", severity=sev, category="security",
                title="Unsafe or unreadable archive entry skipped", file=safe_display(e.raw_name),
                evidence=reason, explanation="This entry was not read. Nothing from the archive is extracted to disk.",
                remediation="Review how the archive was created; do not extract it with tools that trust member names.",
                source="archive-validator", confidence="confirmed", verification="verified-by-archive-metadata"))
        nested = sum(1 for e in files if e.name.lower().endswith(ARCHIVE_EXTENSIONS) and not e.vendored)
        if nested:
            result.warnings.append(f"{nested} nested archive(s) were found and not expanded.")

        usable = [e for e in files if not e.skip_reason and not e.vendored]
        step(0.08, f"Reading text files ({len(usable):,} candidate files)...")
        candidates = [e for e in usable if is_text_candidate(e.name)]
        too_big = [e for e in candidates if e.size > limits.max_text_file_size]
        candidates = [e for e in candidates if e.size <= limits.max_text_file_size]
        if too_big:
            result.warnings.append(f"{len(too_big)} text file(s) larger than the scan limit were not scanned.")
        if len(candidates) > limits.max_scanned_files:
            result.warnings.append(
                f"Only the first {limits.max_scanned_files:,} of {len(candidates):,} text files were scanned.")
            candidates = candidates[: limits.max_scanned_files]
        texts: Dict[str, str] = {}
        binary = 0
        for i, e in enumerate(candidates):
            if i % 25 == 0:
                step(0.08 + 0.42 * i / max(1, len(candidates)), f"Reading {safe_display(e.name, 60)}")
            try:
                text = reader.read_text(e)
            except ArchiveError as exc:
                result.warnings.append(str(exc))
                continue
            if text is None:
                binary += 1
            else:
                texts[e.name] = text
        summary.scanned_text_files = len(texts)
        summary.unscanned_files = len(usable) - len(texts)
        result.archive = summary

    step(0.52, "Detecting languages...")
    result.languages = detect_languages(entries)

    step(0.58, "Identifying dependency manifests...")
    for e in usable:
        ident = identify_manifest(e.name)
        if not ident:
            continue
        mf = Manifest(path=e.name, ecosystem=ident[0], kind=ident[1])
        if e.name in texts:
            try:
                mf.dependencies = parse_dependencies(e.name, texts[e.name])
            except ValueError as exc:
                mf.parse_error = f"could not parse: {str(exc)[:120]}"
                result.warnings.append(f"{e.name}: {mf.parse_error}")
        result.manifests.append(mf)
    result.manifests.sort(key=lambda m: m.path)

    step(0.66, "Detecting frameworks...")
    result.frameworks = detect_frameworks(result.manifests, texts, [e.name for e in usable])
    step(0.72, "Detecting database clues...")
    result.databases = detect_databases(result.manifests, texts)

    step(0.76, "Running static checks...")
    names = sorted(texts)
    for i, name in enumerate(names):
        if i % 25 == 0:
            step(0.76 + 0.20 * i / max(1, len(names)), f"Checking {safe_display(name, 60)}")
        findings.extend(analyze_text_file(name, texts[name]))

    step(0.965, "Analyzing project structure...")
    result.structure = analyze_structure(entries, texts, result.manifests, result.languages,
                                         result.frameworks, result.databases)

    # ---- Phase 5: database analysis (reads the text already in memory; never connects to anything)
    if options.database_analysis:
        step(0.966, "Analyzing database artifacts...")
        _run_database_phase(result, texts, findings)
    # ---- Phase 3: language-specific static analysis (external tools only if installed; never installs)
    if options.static_plugins:
        step(0.972, "Running language analyzers (Ruff, ESLint, tsc, javac, ...)...")
        _run_static_phase(result, path, findings, options, cancel, lambda m: step(0.972, m), texts)
    if options.code_map:
        step(0.980, "Building function/class map...")
        _run_code_map(result, texts)
    # ---- Phase 4: discover (not run) build/test commands and report isolation status
    if options.discover_execution:
        step(0.985, "Discovering build/test commands (nothing is executed)...")
        _discover_execution(result, path)

    # cap noisy rules, sort, number
    counts = Counter()
    kept: List[Finding] = []
    for f in sorted(findings, key=lambda f: (SEVERITY_RANK.get(f.severity, 9), f.file or "", f.line or 0)):
        counts[f.rule_id] += 1
        if counts[f.rule_id] <= MAX_FINDINGS_PER_RULE:
            kept.append(f)
    for rule, n in counts.items():
        if n > MAX_FINDINGS_PER_RULE:
            result.warnings.append(f"Rule '{rule}' matched {n} times; only the first {MAX_FINDINGS_PER_RULE} are listed.")
    for i, f in enumerate(kept, 1):
        f.id = f"F-{i:04d}"
    result.findings = kept
    if options.findings_overview:
        _findings_overview(result)
    result.duration_seconds = round(time.monotonic() - t0, 3)
    step(1.0, "Analysis complete.")
    log.info("Analyzed %s: %d files, %d findings", result.archive_name, summary.file_count, len(kept))
    return result


# ----------------------------------------------------------------------------- optional phases (never raise)
def _run_database_phase(result: AnalysisResult, texts: Dict[str, str], findings: List[Finding]) -> None:
    try:
        from core.database_analyzer import analyze_database_artifacts, summarize, to_main_findings
        db = analyze_database_artifacts(texts)
        result.database_analysis = summarize(db)
        findings.extend(to_main_findings(db))
    except Exception as exc:  # noqa: BLE001 - an optional phase must not break the main analysis
        log.exception("Database analysis failed")
        result.warnings.append(f"Database analysis (Phase 5) failed: {type(exc).__name__}: {str(exc)[:120]}")


def _run_static_phase(result: AnalysisResult, path, findings: List[Finding], options: AnalysisOptions, cancel,
                      progress, texts: Optional[Dict[str, str]] = None) -> None:
    from analyzers import run_static_analysis
    from plugins.static_analysis import Options as SAOptions
    sa = SAOptions(timeout_s=options.tool_timeout_s, approved_tools=set(options.approved_tools), sql_dialect=options.sql_dialect)
    out = run_static_analysis(path, sa, progress=progress, cancel=cancel, skip_duplicates_of=findings)
    result.static_analysis = out.to_dict()
    result.static_analysis["findings"] = len(out.findings)    # findings themselves live in result.findings
    if out.error:
        result.warnings.append(out.error)
    unavailable = [r["analyzer"] for r in out.tool_runs if r.get("status") == "unavailable"]
    if unavailable:
        result.warnings.append("External analyzers not installed (skipped, nothing was installed): " +
                               ", ".join(sorted(set(unavailable))))
    findings.extend(out.findings)
    if texts:                                   # HTML / CSS / PHP / Go built-in analyzers (Stage A)
        from analyzers.builtin_analyzers import run_builtin_analyzers
        bf, runs = run_builtin_analyzers(texts)
        findings.extend(bf)
        result.static_analysis["tool_runs"].extend(runs)
        result.static_analysis["findings"] += len(bf)
        for r in runs:
            if r["status"] == "unavailable":
                result.warnings.append(f"{r['analyzer']}: {r['note']}")


def _run_code_map(result: AnalysisResult, texts: Dict[str, str]) -> None:
    try:
        from core.code_map import build_code_map
        result.code_map = build_code_map(texts).to_dict()
    except Exception as exc:  # noqa: BLE001
        log.exception("Code map failed")
        result.warnings.append(f"Code map failed: {type(exc).__name__}: {str(exc)[:120]}")


def _discover_execution(result: AnalysisResult, path) -> None:
    try:
        from plugins.build_execution import discover_candidates
        from services.environment_service import detect_isolation
        cands, notes = discover_candidates(str(path))
        iso = detect_isolation()
        result.execution = {
            "isolation": {"available": iso.available, "backend": iso.backend, "reason": iso.reason},
            "candidates": [c.to_dict() for c in cands], "notes": list(notes),
            "policy": "Nothing is executed during analysis. Each command needs explicit approval and a container runtime."}
    except Exception as exc:  # noqa: BLE001
        log.exception("Execution discovery failed")
        result.warnings.append(f"Build/test discovery (Phase 4) failed: {type(exc).__name__}: {str(exc)[:120]}")


def _findings_overview(result: AnalysisResult) -> None:
    try:
        by_sev, by_cat, by_src = Counter(), Counter(), Counter()
        for f in result.findings:
            by_sev[f.severity] += 1
            by_cat[f.category] += 1
            by_src[f.source.split(":")[0] if f.source else "unknown"] += 1
        result.findings_overview = {"total": len(result.findings), "by_severity": dict(by_sev),
                                    "by_category": dict(by_cat), "by_source": dict(by_src),
                                    "note": "Detected findings only; AI-generated notes are listed separately and are never verified."}
    except Exception:  # noqa: BLE001
        log.exception("Findings overview failed")
