"""Batch grading: discover submissions, analyse each (isolated failures), score against a rubric, compare for similarity."""
from __future__ import annotations

import csv
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.archive_manager import ArchiveError, ArchiveReader, is_text_candidate
from core.analysis_options import AnalysisOptions
from core.analysis_orchestrator import AnalysisCancelled, analyze_archive
from core.input_adapter import TAR_SUFFIXES, prepared_input
from education.rubric import Evaluation, Rubric, default_rubric, evaluate
from education.similarity import PairResult, SimilarityOptions, compare_submissions
from security.archive_validation import ArchiveLimits, safe_display
from security.secret_scanner import redact

MAX_SUBMISSIONS = 500
ProgressCb = Callable[[float, str], None]


@dataclass
class StudentGrade:
    student_id: str
    source_name: str
    status: str = "graded"                         # graded | error
    error: str = ""
    evaluations: List[Dict[str, Any]] = field(default_factory=list)
    auto_points: float = 0.0
    auto_max: float = 0.0
    manual_points: float = 0.0
    manual_scored: int = 0
    manual_count: int = 0
    total_possible: float = 0.0
    files: int = 0
    findings: int = 0
    serious_findings: int = 0
    languages: List[str] = field(default_factory=list)
    top_findings: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    similarity_max: float = 0.0
    similarity_level: str = ""
    similar_to: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.status == "graded" and self.manual_scored >= self.manual_count

    @property
    def earned(self) -> float:
        return round(self.auto_points + self.manual_points, 2)

    @property
    def percent(self) -> Optional[float]:
        if self.status != "graded":
            return None
        return round(100 * self.earned / self.total_possible, 1) if self.total_possible else None

    def to_dict(self) -> Dict[str, Any]:
        return {"student_id": self.student_id, "source": self.source_name, "status": self.status, "error": self.error,
                "earned": self.earned, "possible": self.total_possible, "percent": self.percent, "complete": self.complete,
                "auto_points": self.auto_points, "auto_max": self.auto_max, "manual_points": self.manual_points,
                "criteria": self.evaluations, "files": self.files, "findings": self.findings, "serious_findings": self.serious_findings,
                "languages": self.languages, "top_findings": self.top_findings, "warnings": self.warnings,
                "similarity_max": round(self.similarity_max, 3), "similarity_level": self.similarity_level, "similar_to": self.similar_to}


@dataclass
class BatchResult:
    rubric: Rubric
    students: List[StudentGrade] = field(default_factory=list)
    pairs: List[PairResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    out_dir: Optional[Path] = None
    files: Dict[str, str] = field(default_factory=dict)

    def by_id(self, sid: str) -> Optional[StudentGrade]:
        return next((s for s in self.students if s.student_id == sid), None)


# ------------------------------------------------------------------ discovery
def _student_id(name: str) -> str:
    stem = name
    for suf in sorted(TAR_SUFFIXES + (".zip",), key=len, reverse=True):
        if stem.lower().endswith(suf):
            stem = stem[: -len(suf)]
            break
    stem = re.sub(r"[^A-Za-z0-9_.\-]+", "_", stem).strip("._") or "student"
    return stem[:60]


def discover_submissions(folder: Any, limit: int = MAX_SUBMISSIONS) -> Tuple[List[Tuple[str, Path]], List[str]]:
    """Each ZIP/tar file or sub-folder directly inside ``folder`` is one submission. Returns ([(id, path)], notes)."""
    root = Path(folder)
    notes: List[str] = []
    if not root.is_dir():
        raise ArchiveError("The submissions folder was not found.")
    out: List[Tuple[str, Path]] = []
    used: Dict[str, int] = {}
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if p.name.startswith(".") or p.is_symlink():
            if p.is_symlink():
                notes.append(f"Skipped symbolic link {safe_display(p.name)}.")
            continue
        ok = p.is_dir() or p.name.lower().endswith((".zip",) + TAR_SUFFIXES)
        if not ok:
            if not p.name.lower().endswith((".csv", ".json", ".md", ".txt")):
                notes.append(f"Ignored {safe_display(p.name)} (not a ZIP/tar/folder).")
            continue
        sid = _student_id(p.name)
        used[sid] = used.get(sid, 0) + 1
        if used[sid] > 1:
            sid = f"{sid}_{used[sid]}"
            notes.append(f"Duplicate student id; {safe_display(p.name)} was named {sid}.")
        out.append((sid, p))
    if len(out) > limit:
        notes.append(f"Only the first {limit} of {len(out)} submissions were graded.")
        out = out[:limit]
    return out, notes


def load_manual_scores(path: Any) -> Dict[str, Dict[str, Tuple[float, str]]]:
    """CSV with columns student_id,criterion_id,points[,comment]. Returns {student: {criterion: (points, comment)}}."""
    out: Dict[str, Dict[str, Tuple[float, str]]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.DictReader(fh), 2):
            try:
                sid, cid = (row.get("student_id") or "").strip(), (row.get("criterion_id") or "").strip()
                pts = float(row.get("points") or "")
            except ValueError:
                raise ValueError(f"manual scores line {i}: points must be a number")
            if not sid or not cid:
                raise ValueError(f"manual scores line {i}: student_id and criterion_id are required")
            out.setdefault(sid, {})[cid] = (pts, (row.get("comment") or "")[:300])
    return out


# ------------------------------------------------------------------ grading
def _read_texts(path: Path, limits: ArchiveLimits) -> Tuple[List[str], Dict[str, str]]:
    names: List[str] = []
    texts: Dict[str, str] = {}
    with prepared_input(path, limits) as prep:
        with ArchiveReader(prep.zip_path, limits) as reader:
            usable = [e for e in reader.entries if not e.is_dir and not e.skip_reason and not e.vendored
                      and not e.name.startswith("__MACOSX/")]
            names = [e.name for e in usable]
            cands = [e for e in usable if is_text_candidate(e.name) and e.size <= limits.max_text_file_size][:limits.max_scanned_files]
            for e in cands:
                try:
                    t = reader.read_text(e)
                except ArchiveError:
                    continue
                if t is not None:
                    texts[e.name] = t
    return names, texts


def grade_one(sid: str, path: Path, rubric: Rubric, limits: ArchiveLimits,
              manual: Optional[Dict[str, Tuple[float, str]]] = None) -> Tuple[StudentGrade, Dict[str, str]]:
    g = StudentGrade(sid, safe_display(path.name))
    try:
        opts = AnalysisOptions(static_plugins=True, database_analysis=False, discover_execution=False, findings_overview=False,
                               code_map=True, tool_timeout_s=60)
        res = analyze_archive(path, limits, options=opts)
        names, texts = _read_texts(path, limits)
    except (ArchiveError, AnalysisCancelled) as exc:
        g.status, g.error = "error", redact(str(exc))
        return g, {}
    except Exception as exc:  # noqa: BLE001 - one bad submission must never stop the batch
        g.status, g.error = "error", f"Unexpected error: {type(exc).__name__}"
        return g, {}
    d = res.to_dict()
    evs: List[Evaluation] = evaluate(rubric, d, names, texts)
    manual = manual or {}
    for ev in evs:
        row: Dict[str, Any] = {"id": ev.criterion, "title": ev.title, "max": ev.max_points, "points": ev.points,
                               "status": ev.status, "evidence": ev.evidence, "manual": ev.status == "manual"}
        if ev.status == "manual":
            g.manual_count += 1
            if ev.criterion in manual:
                pts, comment = manual[ev.criterion]
                pts = max(0.0, min(float(pts), ev.max_points))
                row.update(points=pts, status="pass" if pts >= ev.max_points else ("partial" if pts > 0 else "fail"),
                           evidence="Instructor: " + (redact(comment) or "graded"))
                g.manual_points += pts
                g.manual_scored += 1
                g.total_possible += ev.max_points
        else:
            g.auto_points += ev.points or 0.0
            g.auto_max += ev.max_points
            g.total_possible += ev.max_points
        g.evaluations.append(row)
    g.manual_points = round(g.manual_points, 2)
    g.auto_points = round(g.auto_points, 2)
    g.total_possible = round(g.total_possible, 2)
    if not g.complete:       # provisional: percent is of the points that can be scored so far
        pass
    g.files = d["archive"]["file_count"]
    g.findings = len(d["findings"])
    g.serious_findings = sum(1 for f in d["findings"] if f["severity"] in ("critical", "high"))
    g.languages = [l["language"] for l in d["languages"]][:5]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    top = sorted(d["findings"], key=lambda f: order.get(f["severity"], 9))[:15]
    g.top_findings = [{"severity": f["severity"], "title": redact(f["title"]), "file": safe_display(f.get("file") or ""),
                       "line": f.get("line"), "evidence": redact((f.get("evidence") or "")[:160]),
                       "remediation": redact((f.get("remediation") or "")[:200])} for f in top]
    g.warnings = [redact(w) for w in d["warnings"][:10]]
    return g, texts


def grade_batch(folder: Any, rubric: Optional[Rubric] = None, out_dir: Any = None, progress: Optional[ProgressCb] = None,
                cancel: Optional[threading.Event] = None, similarity: Optional[SimilarityOptions] = None,
                template: Any = None, manual_scores: Any = None, limits: Optional[ArchiveLimits] = None) -> BatchResult:
    rubric = rubric or default_rubric()
    limits = limits or ArchiveLimits()
    subs, notes = discover_submissions(folder)
    if not subs:
        raise ArchiveError("No submissions found (expected ZIP/tar files or folders).")
    manual: Dict[str, Dict[str, Tuple[float, str]]] = {}
    if manual_scores:
        manual = load_manual_scores(manual_scores)
        unknown = sorted(set(manual) - {s for s, _ in subs})
        if unknown:
            notes.append("Manual scores for unknown students ignored: " + ", ".join(unknown[:10]))
    batch = BatchResult(rubric, notes=notes)
    sources: Dict[str, Dict[str, str]] = {}
    for i, (sid, path) in enumerate(subs):
        if cancel is not None and cancel.is_set():
            batch.notes.append(f"Cancelled after {i} of {len(subs)} submissions.")
            break
        if progress:
            progress(i / len(subs), f"Grading {sid} ({i + 1}/{len(subs)})")
        g, texts = grade_one(sid, path, rubric, limits, manual.get(sid))
        batch.students.append(g)
        if texts:
            sources[sid] = texts
    if progress:
        progress(0.95, "Comparing submissions")
    tpl = None
    if template:
        try:
            _names, tpl = _read_texts(Path(template), limits)
        except ArchiveError as exc:
            batch.notes.append(f"Starter template ignored: {redact(str(exc))}")
    if len(sources) >= 2:
        batch.pairs, sim_notes = compare_submissions(sources, similarity, tpl)
        batch.notes.extend(sim_notes)
        rank = {"high": 2, "review": 1, "low": 0}
        for p in batch.pairs:
            for me, other in ((p.a, p.b), (p.b, p.a)):
                s = batch.by_id(me)
                if s and p.level != "low":
                    s.similar_to.append(other)
                    if p.score > s.similarity_max:
                        s.similarity_max = p.score
                    if rank[p.level] > rank.get(s.similarity_level, -1):
                        s.similarity_level = p.level
    if out_dir:
        from education.reports import write_batch_reports
        batch.out_dir = Path(out_dir)
        batch.files = write_batch_reports(batch, batch.out_dir)
    if progress:
        progress(1.0, "Done")
    return batch
