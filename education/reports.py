"""Instructor summary, per-student reports (no other student's name) and CSV exports."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Dict, List

from security.secret_scanner import redact

_CSS = ("body{font-family:system-ui,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#222}"
        "table{border-collapse:collapse;width:100%;margin:12px 0}th,td{border:1px solid #ccc;padding:6px 8px;text-align:left;font-size:14px}"
        "th{background:#f0f0f0}.pass{color:#1a7f37}.partial{color:#9a6700}.fail{color:#cf222e}.manual{color:#555}"
        ".note{background:#fff8e1;border:1px solid #e6d28a;padding:8px 12px;margin:12px 0}")


def _e(s) -> str:
    return html.escape(redact(str(s if s is not None else "")))


def csv_safe(v):
    """Neutralise spreadsheet formula injection in student-controlled text."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _write_csv(path: Path, header: List[str], rows: List[List]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow([csv_safe(c) for c in r])


def _page(title: str, body: str) -> str:
    return f"<!doctype html><html lang='en'><meta charset='utf-8'><title>{_e(title)}</title><style>{_CSS}</style><body>{body}</body></html>"


def student_report_html(batch, s) -> str:
    rows = "".join(
        f"<tr><td>{_e(c['title'])}</td><td class='{c['status']}'>{_e(c['status'])}</td>"
        f"<td>{'pending' if c['points'] is None else _e(c['points'])} / {_e(c['max'])}</td><td>{_e(c['evidence'])}</td></tr>"
        for c in s.evaluations)
    fnd = "".join(f"<tr><td>{_e(f['severity'])}</td><td>{_e(f['title'])}</td><td>{_e(f['file'])}:{_e(f['line'] or '')}</td>"
                  f"<td>{_e(f['remediation'])}</td></tr>" for f in s.top_findings) or "<tr><td colspan=4>No findings.</td></tr>"
    status = "Final" if s.complete else "Provisional - some criteria still await the instructor"
    if s.status == "error":
        body = f"<h1>{_e(s.student_id)}</h1><p class='fail'>This submission could not be analysed: {_e(s.error)}</p>"
        return _page(f"Report {s.student_id}", body)
    body = (f"<h1>{_e(s.student_id)} - feedback report</h1><p><b>{_e(s.earned)} / {_e(s.total_possible)}</b> "
            f"({_e(s.percent)}%) - {_e(status)}</p>"
            f"<p>Rubric: {_e(batch.rubric.name)}. Files: {_e(s.files)}; languages: {_e(', '.join(s.languages) or 'none')}.</p>"
            f"<div class='note'>Scores come from static checks only; your code was not run. Findings are heuristic and may include false positives.</div>"
            f"<h2>Criteria</h2><table><tr><th>Criterion</th><th>Result</th><th>Points</th><th>Evidence</th></tr>{rows}</table>"
            f"<h2>Top findings</h2><table><tr><th>Severity</th><th>Issue</th><th>Where</th><th>How to fix</th></tr>{fnd}</table>")
    return _page(f"Report {s.student_id}", body)


def summary_html(batch) -> str:
    rows = "".join(
        f"<tr><td><a href='students/{_e(s.student_id)}.html'>{_e(s.student_id)}</a></td><td>{_e(s.status)}</td>"
        f"<td>{_e(s.earned) if s.status == 'graded' else ''}</td><td>{_e(s.percent if s.percent is not None else '')}</td>"
        f"<td>{'yes' if s.complete else 'no'}</td><td>{_e(s.serious_findings)}</td>"
        f"<td>{_e(s.similarity_level)} {_e(round(s.similarity_max, 2) if s.similarity_level else '')}</td></tr>"
        for s in batch.students)
    pairs = "".join(f"<tr><td>{_e(p.a)}</td><td>{_e(p.b)}</td><td>{p.score:.2f}</td><td>{p.containment:.2f}</td><td>{_e(p.level)}</td>"
                    f"<td>{_e('; '.join(f['file_a'] + ' ~ ' + f['file_b'] for f in p.files[:3]))}</td></tr>"
                    for p in batch.pairs if p.level != "low") or "<tr><td colspan=6>No pairs above the review threshold.</td></tr>"
    notes = "".join(f"<li>{_e(n)}</li>" for n in batch.notes)
    body = (f"<h1>Batch grading summary</h1><p>Rubric: {_e(batch.rubric.name)} ({_e(batch.rubric.total)} points; "
            f"{_e(batch.rubric.auto_total)} automatic, {_e(batch.rubric.manual_total)} manual). Students: {len(batch.students)}.</p>"
            f"<table><tr><th>Student</th><th>Status</th><th>Points</th><th>%</th><th>Final</th><th>High+ findings</th><th>Similarity</th></tr>{rows}</table>"
            f"<h2>Similarity review (instructor only)</h2><table><tr><th>A</th><th>B</th><th>Jaccard</th><th>Containment</th><th>Level</th><th>Files</th></tr>{pairs}</table>"
            f"<h2>Notes</h2><ul>{notes}</ul>")
    return _page("Batch grading summary", body)


def write_batch_reports(batch, out_dir: Path) -> Dict[str, str]:
    out = Path(out_dir)
    (out / "students").mkdir(parents=True, exist_ok=True)
    files: Dict[str, str] = {}
    for s in batch.students:
        (out / "students" / f"{s.student_id}.html").write_text(student_report_html(batch, s), encoding="utf-8")
    (out / "summary.html").write_text(summary_html(batch), encoding="utf-8")
    files["summary_html"] = str(out / "summary.html")
    crit_ids = [c.id for c in batch.rubric.criteria]
    header = ["student_id", "status", "earned", "possible", "percent", "final"] + crit_ids + ["serious_findings", "similarity_level", "similarity_max", "error"]
    rows = []
    for s in batch.students:
        by = {c["id"]: c for c in s.evaluations}
        rows.append([s.student_id, s.status, s.earned if s.status == "graded" else "", s.total_possible, s.percent if s.percent is not None else "",
                     "yes" if s.complete else "no"] + [("" if by.get(i) is None or by[i]["points"] is None else by[i]["points"]) for i in crit_ids]
                    + [s.serious_findings, s.similarity_level, round(s.similarity_max, 3) if s.similarity_level else "", s.error])
    _write_csv(out / "grades.csv", header, rows)
    files["grades_csv"] = str(out / "grades.csv")
    _write_csv(out / "similarity.csv", ["student_a", "student_b", "jaccard", "containment", "shared_shingles", "level", "top_files"],
               [[p.a, p.b, round(p.score, 3), round(p.containment, 3), p.shared, p.level,
                 "; ".join(f"{f['file_a']} ~ {f['file_b']}" for f in p.files[:3])] for p in batch.pairs])
    files["similarity_csv"] = str(out / "similarity.csv")
    # a template the instructor can fill and feed back with --manual-scores
    _write_csv(out / "manual_scores_template.csv", ["student_id", "criterion_id", "points", "comment"],
               [[s.student_id, c.id, "", ""] for s in batch.students if s.status == "graded" for c in batch.rubric.criteria if c.manual])
    files["manual_template"] = str(out / "manual_scores_template.csv")
    (out / "grades.json").write_text(json.dumps({"rubric": batch.rubric.to_dict(), "students": [s.to_dict() for s in batch.students],
                                                 "pairs": [p.to_dict() for p in batch.pairs], "notes": batch.notes}, indent=2), encoding="utf-8")
    files["json"] = str(out / "grades.json")
    return files
