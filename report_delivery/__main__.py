"""python -m report_delivery reports input.json --out reports/
   python -m report_delivery package <project_root> --out delivery.zip [--run-tests]"""
from __future__ import annotations

import argparse
import json
import sys

from .packager import PackageOptions, PackagingError
from .plugin import run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="report_delivery")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reports")
    r.add_argument("input_json"), r.add_argument("--out", required=True), r.add_argument("--no-probe", action="store_true")
    p = sub.add_parser("package")
    p.add_argument("root"), p.add_argument("--out", required=True), p.add_argument("--run-tests", action="store_true")
    p.add_argument("--reports-dir"), p.add_argument("--version", default="1.0.0"), p.add_argument("--fail-on-secret", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "reports":
            with open(a.input_json, encoding="utf-8") as fh:
                data = json.load(fh)
            res = run({"action": "reports", "input": data, "out_dir": a.out, "probe_tools": not a.no_probe})
            for name, path in res["reports"].items():
                print(f"wrote {path}")
        else:
            opts = PackageOptions(version=a.version, fail_on_secret=a.fail_on_secret)
            res = run({"action": "package", "root": a.root, "out_zip": a.out, "run_tests": a.run_tests,
                       "reports_dir": a.reports_dir, "options": opts})
            pk = res["package"]
            print(f"wrote {pk['zip']} ({pk['file_count']} files)")
            print("excluded:", len(pk["excluded"]), " generated:", ", ".join(pk["generated"]) or "none")
            print("missing checklist items:", ", ".join(pk["missing"]) or "none")
        return 0
    except (PackagingError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
