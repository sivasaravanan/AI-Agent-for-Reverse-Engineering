"""python -m database_analysis <project.zip|folder> [--json out.json] [--html out.html] [--md out.md]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import render_html, render_json, render_markdown, run
from .sources import ArchiveError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="database_analysis", description="Static database analysis (nothing is executed).")
    ap.add_argument("source", help="ZIP archive or project folder")
    ap.add_argument("--json"), ap.add_argument("--html"), ap.add_argument("--md")
    a = ap.parse_args(argv)
    try:
        res = run(a.source)
    except ArchiveError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for path, fn in ((a.json, render_json), (a.html, render_html), (a.md, render_markdown)):
        if path:
            Path(path).write_text(fn(res), encoding="utf-8")
            print(f"wrote {path}")
    print(f"Databases detected: {len(res.databases)}  findings: {len(res.findings)}  files scanned: {res.stats.get('files_scanned', 0)}")
    for d in res.databases:
        print(f"  - {d.display}: {d.status} ({d.rationale})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
