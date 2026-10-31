"""CLI:  python -m plugins.static_analysis <project.zip|folder> [--json out.json] [--html out.html]"""
import argparse
import sys

from . import ALL_LANGUAGES, ArchiveRejected, Options, analyze, available_tools


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="static_analysis")
    ap.add_argument("source", nargs="?")
    ap.add_argument("--json"), ap.add_argument("--html")
    ap.add_argument("--languages", default=",".join(ALL_LANGUAGES))
    ap.add_argument("--approve", action="append", default=[], help="approve a build tool, e.g. --approve dotnet")
    ap.add_argument("--sql-dialect", default="ansi")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--no-pyright", action="store_true")
    ap.add_argument("--list-tools", action="store_true")
    a = ap.parse_args(argv)
    if a.list_tools:
        for k, v in available_tools().items():
            print(f"{k:10s} {v or 'NOT FOUND'}")
        return 0
    if not a.source:
        ap.error("source is required")
    opts = Options(languages={x.strip() for x in a.languages.split(",") if x.strip()},
                   approved_tools=set(a.approve), sql_dialect=a.sql_dialect, timeout_s=a.timeout,
                   use_pyright=not a.no_pyright)
    try:
        rep = analyze(a.source, opts, progress=lambda m: print(m, file=sys.stderr))
    except (ArchiveRejected, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if a.json:
        rep.write_json(a.json)
    if a.html:
        rep.write_html(a.html)
    s = rep.summary()
    print(f"{s['total']} findings  {s['by_severity']}  {s['by_kind']}")
    for t in rep.tool_runs:
        print(f"  [{t.status:11s}] {t.analyzer} {t.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
