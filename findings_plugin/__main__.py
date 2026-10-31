"""CLI: python -m findings_plugin --log run.log [--json out.json] [--html out.html] [--ui] [--demo]"""
from __future__ import annotations

import argparse
import sys

from . import FindingsPlugin, Finding, Kind, Severity, Confidence, make_ai_finding


def _demo(plugin: FindingsPlugin) -> None:
    plugin.add([
        Finding("[DEMO] Possible hard-coded credential", Severity.HIGH, "security", Kind.HEURISTIC,
                "demo/config.py", 12, evidence="DB_PASS = '***'", source="demo-sample", confidence=Confidence.WEAK,
                rule_id="demo-secret"),
        Finding("[DEMO] TODO left in code", Severity.LOW, "maintenance", Kind.HEURISTIC, "demo/config.py", 15,
                source="demo-sample", rule_id="demo-todo"),
        make_ai_finding("[DEMO] Hypothesis: connection string may point at wrong server",
                        explanation="Illustrative AI-style hypothesis row.", source="demo-sample",
                        file_path="demo/config.py", line=12),
    ])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="findings_plugin")
    ap.add_argument("--log", help="log / test output file to analyze")
    ap.add_argument("--json"), ap.add_argument("--html")
    ap.add_argument("--ui", action="store_true", help="open the findings window")
    ap.add_argument("--demo", action="store_true", help="load clearly labelled demo rows (UI preview only)")
    a = ap.parse_args(argv)
    if not (a.log or a.ui or a.demo):
        ap.print_help()
        return 0
    plugin = FindingsPlugin()
    if a.log:
        res = plugin.analyze_log_file(a.log)
        print(f"{res.line_count} lines, {len(res.findings)} finding(s)")
        for n in res.notes:
            print("note:", n)
        for f in plugin.store.query():
            print(f"[{f.severity.value:8}] {f.id} {f.location or '-'}  {f.title}")
    if a.demo:
        _demo(plugin)
    if a.json:
        plugin.export_json(a.json)
    if a.html:
        plugin.export_html(a.html)
    if a.ui:
        import tkinter as tk
        root = tk.Tk()
        root.title("Findings")
        root.geometry("1000x700")
        plugin.mount(root)
        root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
