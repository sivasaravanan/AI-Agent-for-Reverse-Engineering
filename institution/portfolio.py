"""Portfolio (batch) analysis: many ZIP archives -> one consolidated, comparable result.

Typical users: a university grading student projects, a company auditing vendor deliveries, a government
body reviewing contractor submissions. Each archive is analysed with the normal read-only pipeline
(``analyze_archive``); this module only adds orchestration, acceptance profiles, near-duplicate detection
and consolidated JSON / CSV / HTML output. No archive content is extracted or executed.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from core import __version__
from core.analysis_options import AnalysisOptions
from core.analysis_orchestrator import AnalysisCancelled, analyze_archive
from core.archive_manager import ArchiveError, ArchiveReader
from core.report_generator import write_reports
from security.archive_validation import ArchiveLimits, safe_display

log = logging.getLogger("ase.portfolio")
ProgressCb = Callable[[float, str], None]

MAX_ARCHIVES = 2000
MIN_FINGERPRINT_BYTES = 64          # tiny files (empty __init__.py, one-line stubs) say nothing about similarity
SECRET_RULES = {"sec-hardcoded-secret", "sec-credentials-in-url"}
SYNTAX_CATEGORY = "syntax"

# Acceptance profiles. A value of None disables that check. All values can be overridden from a JSON file.
PROFILES: Dict[str, Dict[str, Any]] = {
    "general": {"description": "Balanced default for any institution.",
                "max_critical": 0, "max_high": 5, "max_secrets": 0, "max_syntax_errors": 0,
                "similarity_review_at": 0.80},
    "education": {"description": "Student work: tolerate rough edges, flag copied submissions strongly.",
                  "max_critical": 0, "max_high": 15, "max_secrets": 0, "max_syntax_errors": 3,
                  "similarity_review_at": 0.70},
    "enterprise": {"description": "Internal/vendor deliveries: strict on security findings.",
                   "max_critical": 0, "max_high": 2, "max_secrets": 0, "max_syntax_errors": 0,
                   "similarity_review_at": 0.90},
    "government": {"description": "Contractor submissions: zero tolerance for critical/high and secrets.",
                   "max_critical": 0, "max_high": 0, "max_secrets": 0, "max_syntax_errors": 0,
                   "similarity_review_at": 0.90},
}
_NUMERIC_KEYS = {"max_critical", "max_high", "max_secrets", "max_syntax_errors"}
RISK_WEIGHTS = {"critical": 25, "high": 10, "medium": 3, "low": 1, "info": 0}


# ----------------------------------------------------------------------------- data
@dataclass
class ProjectRow:
    name: str                          # path relative to the chosen folder (display-safe)
    status: str = "pending"            # ok | rejected | error | cancelled | pending
    verdict: str = "n/a"               # PASS | REVIEW | FAIL | n/a
    reasons: List[str] = field(default_factory=list)
    sha256: str = ""
    size_bytes: int = 0
    files: int = 0
    languages: List[str] = field(default_factory=list)
    frameworks: List[str] = field(default_factory=list)
    databases: List[str] = field(default_factory=list)
    severity_counts: Dict[str, int] = field(default_factory=dict)
    secrets: int = 0
    syntax_errors: int = 0
    risk_score: int = 0                # 0-100, see RISK_WEIGHTS; higher = more to review
    top_rules: List[Tuple[str, int]] = field(default_factory=list)
    similar_to: List[Dict[str, Any]] = field(default_factory=list)
    duration_seconds: float = 0.0
    warnings: int = 0                  # analysis warnings (e.g. optional external tools not installed); informational only
    report_html: str = ""
    report_json: str = ""
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PortfolioResult:
    tool_version: str = __version__
    created_at: str = ""
    source: str = ""
    profile: str = "general"
    profile_values: Dict[str, Any] = field(default_factory=dict)
    rows: List[ProjectRow] = field(default_factory=list)
    totals: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    cancelled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": "1.0", **asdict(self)}


# ----------------------------------------------------------------------------- profiles
def load_profiles(path: Optional[Path] = None) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Built-in profiles, optionally extended/overridden by a JSON file. Never raises; bad values are ignored."""
    profiles = {k: dict(v) for k, v in PROFILES.items()}
    warnings: List[str] = []
    if path is None or not Path(path).is_file():
        return profiles, warnings
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return profiles, [f"Profile file ignored ({type(exc).__name__})."]
    items = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return profiles, ["Profile file ignored: expected an object with a 'profiles' key."]
    for name, vals in items.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_\- ]{1,40}", name) or not isinstance(vals, dict):
            warnings.append(f"Profile {str(name)[:40]!r} ignored (invalid name or body).")
            continue
        merged = dict(profiles.get(name, profiles["general"]))
        for k, v in vals.items():
            if k in _NUMERIC_KEYS:
                if v is None or (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1_000_000):
                    merged[k] = v
                else:
                    warnings.append(f"Profile {name}: {k} must be a whole number >= 0 or null; ignored.")
            elif k == "similarity_review_at":
                if v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 1):
                    merged[k] = v
                else:
                    warnings.append(f"Profile {name}: similarity_review_at must be between 0 and 1 or null; ignored.")
            elif k == "description" and isinstance(v, str):
                merged[k] = v[:200]
        profiles[name] = merged
    return profiles, warnings


def risk_score(counts: Dict[str, int]) -> int:
    return min(100, sum(RISK_WEIGHTS.get(sev, 0) * n for sev, n in counts.items()))


def evaluate(row: ProjectRow, profile: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Pure decision: verdict + human-readable reasons. FAIL beats REVIEW beats PASS."""
    if row.status == "rejected":
        return "FAIL", [f"Archive rejected by safety validation: {row.message}"]
    if row.status != "ok":
        return "n/a", [row.message or "Not analysed."]
    fail: List[str] = []
    review: List[str] = []

    def over(limit_key: str, value: int, label: str) -> None:
        limit = profile.get(limit_key)
        if limit is not None and value > limit:
            fail.append(f"{label}: {value} (allowed {limit})")

    over("max_critical", row.severity_counts.get("critical", 0), "Critical findings")
    over("max_high", row.severity_counts.get("high", 0), "High findings")
    over("max_secrets", row.secrets, "Hard-coded secrets")
    over("max_syntax_errors", row.syntax_errors, "Syntax errors")
    thr = profile.get("similarity_review_at")
    if thr is not None:
        for s in row.similar_to:
            if s.get("similarity", 0) >= thr:
                review.append(f"{int(round(s['similarity'] * 100))}% identical content to {s['name']}")
    if fail:
        return "FAIL", fail + review
    if review:
        return "REVIEW", review
    return "PASS", ["Within the thresholds of this profile."]


# ----------------------------------------------------------------------------- discovery and fingerprints
def discover_archives(folder, recursive: bool = False, limit: int = MAX_ARCHIVES) -> Tuple[List[Path], List[str]]:
    """List ``*.zip`` files (regular files only; symlinks and junctions are not followed). Sorted for stable output."""
    root = Path(folder)
    notes: List[str] = []
    if not root.is_dir():
        return [], [f"Folder not found: {safe_display(str(root))}"]
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
        for fn in sorted(filenames):
            if fn.lower().endswith(".zip"):
                p = Path(dirpath) / fn
                if p.is_file() and not p.is_symlink():
                    found.append(p)
        if not recursive:
            break
    found.sort(key=lambda p: str(p).lower())
    if len(found) > limit:
        notes.append(f"Only the first {limit:,} of {len(found):,} archives are analysed.")
        found = found[:limit]
    return found, notes


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def content_fingerprint(path: Path, limits: Optional[ArchiveLimits] = None) -> frozenset:
    """Set of (CRC32, size) for meaningful, non-vendored members, read from ZIP metadata only (no content read)."""
    fp = set()
    with ArchiveReader(path, limits or ArchiveLimits()) as reader:
        for e in reader.entries:
            if e.is_dir or e.skip_reason or e.vendored or e.info is None or e.size < MIN_FINGERPRINT_BYTES:
                continue
            fp.add((e.info.CRC, e.size))
    return frozenset(fp)


def similarity(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ----------------------------------------------------------------------------- main entry
def analyze_portfolio(source, out_dir, profile: str = "general", recursive: bool = False,
                      limits: Optional[ArchiveLimits] = None, options: Optional[AnalysisOptions] = None,
                      progress: Optional[ProgressCb] = None, cancel: Optional[threading.Event] = None,
                      write_item_reports: bool = True, profiles: Optional[Dict[str, Dict[str, Any]]] = None,
                      archives: Optional[Sequence[Path]] = None, row_done: Optional[Callable[[ProjectRow], None]] = None,
                      ) -> PortfolioResult:
    """Analyse every ZIP in ``source`` (a folder) or the explicit ``archives`` list. One bad archive never stops the batch."""
    profiles = profiles or PROFILES
    if profile not in profiles:
        raise ValueError(f"Unknown profile {profile!r}. Available: {', '.join(sorted(profiles))}")
    prof = profiles[profile]
    limits = limits or ArchiveLimits()
    options = options or AnalysisOptions()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res = PortfolioResult(created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          source=safe_display(str(source)), profile=profile, profile_values=dict(prof))
    if archives is None:
        paths, notes = discover_archives(source, recursive)
        res.notes.extend(notes)
    else:
        paths = [Path(p) for p in archives][:MAX_ARCHIVES]
    base = Path(source) if Path(source).is_dir() else None
    if not paths:
        res.notes.append("No ZIP archives were found.")
    item_dir = out / "projects"
    fingerprints: Dict[int, frozenset] = {}
    n = max(1, len(paths))
    t0 = time.monotonic()
    used_stems: Dict[str, int] = {}

    for i, p in enumerate(paths):
        if cancel is not None and cancel.is_set():
            res.cancelled = True
            res.notes.append(f"Cancelled after {i} of {len(paths)} archives.")
            break
        try:
            rel = str(p.relative_to(base)) if base else p.name
        except ValueError:
            rel = p.name
        row = ProjectRow(name=safe_display(rel.replace("\\", "/")))

        def sub(frac: float, msg: str, _i=i) -> None:
            if progress:
                progress((_i + min(max(frac, 0.0), 1.0)) / n, f"[{_i + 1}/{len(paths)}] {msg}")

        sub(0.0, f"Starting {row.name}")
        try:
            row.size_bytes = p.stat().st_size
            row.sha256 = file_sha256(p)
            result = analyze_archive(p, limits=limits, progress=sub, cancel=cancel, options=options)
            _fill_row(row, result)
            try:
                fingerprints[i] = content_fingerprint(p, limits)
            except ArchiveError:
                pass
            if write_item_reports:
                stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(rel).with_suffix("").as_posix())[:60] or "project"
                used_stems[stem] = used_stems.get(stem, 0) + 1
                folder = item_dir / (stem if used_stems[stem] == 1 else f"{stem}_{used_stems[stem]}")
                paths_out = write_reports(result, folder, delivery=False)
                row.report_html = str(paths_out["html"].relative_to(out)).replace("\\", "/")
                row.report_json = str(paths_out["json"].relative_to(out)).replace("\\", "/")
            row.status = "ok"
        except AnalysisCancelled:
            res.cancelled = True
            res.notes.append(f"Cancelled while analysing {row.name}.")
            break
        except ArchiveError as exc:
            row.status, row.message = "rejected", safe_display(str(exc), 300)
        except Exception as exc:  # noqa: BLE001 - one broken archive must not stop the batch
            log.exception("Portfolio item failed: %s", row.name)
            row.status, row.message = "error", f"{type(exc).__name__}: {safe_display(str(exc), 200)}"
        res.rows.append(row)
        sub(1.0, f"Finished {row.name}")

    _link_similar(res.rows, paths, fingerprints, prof)
    for row in res.rows:
        row.verdict, row.reasons = evaluate(row, prof)
        if row_done:
            row_done(row)
    res.totals = _totals(res.rows, time.monotonic() - t0)
    if progress:
        progress(1.0, "Portfolio analysis complete.")
    return res


def _fill_row(row: ProjectRow, r) -> None:
    row.files = r.archive.file_count
    row.languages = [l.language for l in r.languages[:5]]
    row.frameworks = [d.name for d in r.frameworks[:6]]
    row.databases = [d.name for d in r.databases[:4]]
    counts: Dict[str, int] = {}
    rules: Dict[str, int] = {}
    for f in r.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
        rules[f.rule_id] = rules.get(f.rule_id, 0) + 1
        if f.rule_id in SECRET_RULES:
            row.secrets += 1
        if f.category == SYNTAX_CATEGORY and f.severity in ("high", "critical", "medium"):
            row.syntax_errors += 1
    row.severity_counts = counts
    row.risk_score = risk_score(counts)
    row.top_rules = sorted(rules.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    row.duration_seconds = r.duration_seconds
    row.warnings = len(r.warnings)


def _link_similar(rows: List[ProjectRow], paths: Sequence[Path], fps: Dict[int, frozenset], prof: Dict[str, Any]) -> None:
    """Pairwise similarity over ZIP metadata. Identical archive bytes count as 100%."""
    idx = [i for i, r in enumerate(rows) if r.status == "ok"]
    floor = 0.5
    for a_pos, a in enumerate(idx):
        for b in idx[a_pos + 1:]:
            if rows[a].sha256 and rows[a].sha256 == rows[b].sha256:
                s = 1.0
            else:
                s = similarity(fps.get(a, frozenset()), fps.get(b, frozenset()))
            if s >= floor:
                rows[a].similar_to.append({"name": rows[b].name, "similarity": round(s, 3)})
                rows[b].similar_to.append({"name": rows[a].name, "similarity": round(s, 3)})
    for r in rows:
        r.similar_to.sort(key=lambda d: -d["similarity"])
        del r.similar_to[10:]


def _totals(rows: List[ProjectRow], seconds: float) -> Dict[str, Any]:
    verdicts: Dict[str, int] = {}
    sev: Dict[str, int] = {}
    for r in rows:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
        for k, v in r.severity_counts.items():
            sev[k] = sev.get(k, 0) + v
    return {"archives": len(rows), "analysed": sum(1 for r in rows if r.status == "ok"),
            "rejected": sum(1 for r in rows if r.status == "rejected"), "errors": sum(1 for r in rows if r.status == "error"),
            "verdicts": verdicts, "findings_by_severity": sev, "seconds": round(seconds, 2)}


# ----------------------------------------------------------------------------- output
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> str:
    """Neutralise spreadsheet formula injection from untrusted names."""
    s = str(value if value is not None else "")
    return "'" + s if s.startswith(_FORMULA_START) else s


CSV_COLUMNS = ["project", "verdict", "status", "risk_score", "files", "critical", "high", "medium", "low", "secrets",
               "syntax_errors", "warnings", "languages", "frameworks", "databases", "most_similar", "similarity_pct", "reasons", "sha256"]


def to_csv_rows(res: PortfolioResult) -> List[List[str]]:
    rows = []
    for r in res.rows:
        top = r.similar_to[0] if r.similar_to else None
        rows.append([_csv_safe(x) for x in [
            r.name, r.verdict, r.status, r.risk_score, r.files,
            r.severity_counts.get("critical", 0), r.severity_counts.get("high", 0),
            r.severity_counts.get("medium", 0), r.severity_counts.get("low", 0), r.secrets, r.syntax_errors, r.warnings,
            "; ".join(r.languages), "; ".join(r.frameworks), "; ".join(r.databases),
            top["name"] if top else "", int(round(top["similarity"] * 100)) if top else "",
            " | ".join(r.reasons), r.sha256]])
    return rows


_VERDICT_COLOR = {"PASS": "#15803d", "REVIEW": "#b45309", "FAIL": "#b91c1c", "n/a": "#6b7280"}


def _e(v: Any) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def to_html(res: PortfolioResult) -> str:
    t = res.totals
    head = "".join(f"<th>{_e(h)}</th>" for h in ["Project", "Verdict", "Risk", "Files", "Crit", "High", "Med", "Secrets",
                                                  "Languages", "Most similar", "Why"])
    body = []
    for r in res.rows:
        top = r.similar_to[0] if r.similar_to else None
        link = f"<a href='{_e(r.report_html)}'>{_e(r.name)}</a>" if r.report_html else _e(r.name)
        body.append("<tr>" + "".join(f"<td>{c}</td>" for c in [
            link, f"<span class='b' style='background:{_VERDICT_COLOR.get(r.verdict, '#444')}'>{_e(r.verdict)}</span>",
            r.risk_score, r.files, r.severity_counts.get("critical", 0), r.severity_counts.get("high", 0),
            r.severity_counts.get("medium", 0), r.secrets, _e(", ".join(r.languages)),
            f"{_e(top['name'])} ({int(round(top['similarity'] * 100))}%)" if top else "",
            _e("; ".join(r.reasons))]) + "</tr>")
    verdicts = ", ".join(f"{_e(k)}: {v}" for k, v in sorted(t.get("verdicts", {}).items()))
    notes = "".join(f"<li>{_e(n)}</li>" for n in res.notes)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Portfolio report</title><style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#111}} table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}} th{{background:#f3f4f6}}
.b{{color:#fff;padding:1px 8px;border-radius:10px;font-size:12px}} .muted{{color:#666}}</style></head><body>
<h1>Portfolio report</h1>
<p class="muted">Source: {_e(res.source)} &middot; Profile: <b>{_e(res.profile)}</b> &middot; {_e(res.created_at)} &middot; tool {_e(res.tool_version)}</p>
<p>{_e(t.get('archives', 0))} archive(s), {_e(t.get('analysed', 0))} analysed, {_e(t.get('rejected', 0))} rejected, {_e(t.get('errors', 0))} error(s). Verdicts &mdash; {verdicts or 'none'}.</p>
{'<ul>' + notes + '</ul>' if notes else ''}
<div style="overflow-x:auto"><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>
<h2>How to read this</h2><ul>
<li>Static analysis only: no project code was run. Findings are heuristics unless a report marks them as verified.</li>
<li>Risk = 25&times;critical + 10&times;high + 3&times;medium + 1&times;low, capped at 100. It orders review work; it is not a quality grade.</li>
<li>Similarity compares ZIP file checksums (CRC32 + size) of non-vendored files; it finds copied content, not copied ideas.</li>
<li>Verdicts apply the thresholds of the <b>{_e(res.profile)}</b> profile and are advice for a human reviewer.</li></ul>
</body></html>"""


def write_portfolio_reports(res: PortfolioResult, out_dir) -> Dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    paths = {"json": out / f"portfolio_{stamp}.json", "csv": out / f"portfolio_{stamp}.csv",
             "html": out / f"portfolio_{stamp}.html"}
    paths["json"].write_text(json.dumps(res.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    with open(paths["csv"], "w", newline="", encoding="utf-8-sig") as fh:     # BOM so Excel reads UTF-8
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        w.writerows(to_csv_rows(res))
    paths["html"].write_text(to_html(res), encoding="utf-8")
    return paths
