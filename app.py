"""AI Software Engineering Agent - entry point.

GUI:  python app.py
CLI:  python app.py --cli project.zip [--out reports]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import __version__  # noqa: E402
from core.runtime_paths import data_root, ensure_dirs, resource_root, seed_config  # noqa: E402
from services.log_service import configure_logging  # noqa: E402


def run_cli(zip_path: str, out_dir: str, docs: bool = False) -> int:
    from core.analysis_orchestrator import analyze_archive
    from core.archive_manager import ArchiveError
    from core.report_generator import write_reports
    try:
        result = analyze_archive(zip_path, progress=lambda f, m: print(f"[{f:4.0%}] {m}"))
    except ArchiveError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    paths = write_reports(result, out_dir)
    if docs:
        from core.doc_generator import write_documentation
        print(f"Documentation: {write_documentation(result, out_dir)}")
    print(f"Files: {result.archive.file_count}  Findings: {len(result.findings)}")
    print("Languages:", ", ".join(f"{l.language} ({l.confidence})" for l in result.languages) or "none")
    print("Databases:", ", ".join(f"{d.name} ({d.confidence})" for d in result.databases) or "none")
    print(f"HTML report: {paths['html']}\nJSON report: {paths['json']}")
    return 0


def run_grade(folder: str, out_dir: str, rubric: str = None, template: str = None, manual: str = None) -> int:
    from core.archive_manager import ArchiveError
    from education import RubricError, grade_batch, load_rubric
    try:
        rb = load_rubric(rubric) if rubric else None
        batch = grade_batch(folder, rb, Path(out_dir) / "grading", progress=lambda f, m: print(f"[{f:4.0%}] {m}"),
                            template=template, manual_scores=manual)
    except (ArchiveError, RubricError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    ok = sum(1 for s in batch.students if s.status == "graded")
    flagged = sum(1 for p in batch.pairs if p.level != "low")
    print(f"Graded: {ok}/{len(batch.students)}  Similarity pairs to review: {flagged}")
    for k, v in batch.files.items():
        print(f"{k}: {v}")
    return 0


def run_batch(folder: str, out_dir: str, profile: str, recursive: bool, profiles_file=None) -> int:
    """Institution portfolio mode: analyse every ZIP in a folder and write one consolidated report."""
    from institution.portfolio import analyze_portfolio, load_profiles, write_portfolio_reports
    profiles, warns = load_profiles(Path(profiles_file) if profiles_file else ROOT / "config" / "institution_profiles.json")
    for w in warns:
        print("Profiles:", w, file=sys.stderr)
    if profile not in profiles:
        print(f"ERROR: unknown profile '{profile}'. Available: {', '.join(sorted(profiles))}", file=sys.stderr)
        return 2
    res = analyze_portfolio(folder, out_dir, profile=profile, recursive=recursive, profiles=profiles,
                            progress=lambda f, m: print(f"[{f:4.0%}] {m}"))
    paths = write_portfolio_reports(res, out_dir)
    t = res.totals
    print(f"Archives: {t.get('archives', 0)}  Analysed: {t.get('analysed', 0)}  Verdicts: {t.get('verdicts', {})}")
    for n in res.notes:
        print("Note:", n)
    print(f"Portfolio HTML: {paths['html']}\nPortfolio CSV: {paths['csv']}\nPortfolio JSON: {paths['json']}")
    if not t.get("archives"):
        return 2
    return 1 if t.get("verdicts", {}).get("FAIL") else 0


def run_setup(install: bool, assume_yes: bool) -> int:
    """Check prerequisites; with --setup-install offer to install the missing ones (asks first)."""
    from setup_doctor import apply_known_paths, run_actions, run_checks, summarize
    apply_known_paths()
    items = run_checks()
    for i in items:
        print(f"[{i.status:8}] {i.title}: {i.detail}")
    s = summarize(items)
    if not install:
        print("Required prerequisites: " + ("all present" if s["ok"] else "missing -> " + ", ".join(s["missing_required"])))
        return 0 if s["ok"] else 1
    acts, seen = [], set()
    for i in items:
        if i.fixable and i.required and i.status == "missing":
            for a in i.actions:
                if a.id not in seen:
                    seen.add(a.id)
                    acts.append(a)
    if not acts:
        print("Nothing to install automatically.")
        return 0 if s["ok"] else 1
    print("\nThese commands will be run:")
    for a in acts:
        print("  " + a.display)
    if not assume_yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Cancelled; nothing was installed.")
        return 1
    results = run_actions(acts, progress=print)
    return 0 if results and all(r.ok for r in results) else 1


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "--prereq":      # python app.py --prereq check|diagnose|plan|install|it-request|network-test|config ... (docs/PREREQUISITES.md)
        from prerequisites.__main__ import main as prereq_main
        return prereq_main(raw[1:])
    parser = argparse.ArgumentParser(description="AI Software Engineering Agent",
                                     epilog="Institution prerequisites: python app.py --prereq check | diagnose | plan ID | install ID --yes | it-request | network-test")
    parser.add_argument("--cli", metavar="ZIP", help="analyze a ZIP, .tar.gz or folder without opening the GUI")
    parser.add_argument("--docs", action="store_true", help="with --cli: also write generated technical documentation (Markdown)")
    parser.add_argument("--grade", metavar="DIR", help="batch-grade a folder of student ZIP/tar/folder submissions")
    parser.add_argument("--rubric", metavar="JSON", help="with --grade: rubric file (default: built-in static rubric)")
    parser.add_argument("--template", metavar="ZIP", help="with --grade: starter code excluded from similarity checks")
    parser.add_argument("--manual-scores", metavar="CSV", help="with --grade: student_id,criterion_id,points,comment")
    parser.add_argument("--ci", metavar="SRC", help="CI gate: analyse SRC, apply --policy, write SARIF + SBOM; exit 0 pass / 1 fail / 2 error")
    parser.add_argument("--portfolio", metavar="DIR", help="scan every project (zip/tar/folder) in DIR with one policy; CSV/JSON/HTML + SBOMs")
    parser.add_argument("--policy", metavar="JSON", help="with --ci/--portfolio: policy file (default: no critical/high findings, no secrets)")
    parser.add_argument("--audit-log", metavar="FILE", help="append tamper-evident audit entries here (default: <out>/audit.jsonl for --ci/--portfolio)")
    parser.add_argument("--verify-audit", metavar="FILE", help="verify an audit log hash chain and exit")
    parser.add_argument("--batch", metavar="FOLDER", help="analyze every ZIP in FOLDER and write one portfolio report (exit code 1 if any project FAILs its profile)")
    parser.add_argument("--recursive", action="store_true", help="with --batch: also search sub-folders")
    parser.add_argument("--profile", default="general", help="with --batch: acceptance profile (general, education, enterprise, government, or your own)")
    parser.add_argument("--profiles-file", default=None, help="with --batch: JSON file that adds or overrides profiles")
    parser.add_argument("--setup-check", action="store_true", help="check prerequisites (container runtime, images, optional tools) and exit")
    parser.add_argument("--setup-install", action="store_true", help="check prerequisites and offer to install the missing required ones (asks first)")
    parser.add_argument("--assume-yes", action="store_true", help="with --setup-install: do not ask; use only after reviewing the printed commands")
    parser.add_argument("--out", default=None, help="report output folder (default: from settings, normally ./reports)")
    parser.add_argument("--deliver", metavar="OUT_DIR", help="build the secret-scanned delivery package of this application and exit")
    parser.add_argument("--run-tests", action="store_true", help="with --deliver: run this app's own tests and store the result as evidence")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    DATA = data_root()           # = the project folder (also in an EXE) unless that folder is not writable; then a per-user folder
    if DATA != resource_root():
        seed_config()
    ensure_dirs(DATA)
    configure_logging(DATA / "logs")
    from core.settings import load_settings, resolve_output_dir
    settings, setting_warnings = load_settings(DATA / "config" / "settings.json", DATA / "data" / "user_settings.json")
    for w in setting_warnings:
        print("Settings:", w, file=sys.stderr)
    out_dir = args.out or str(resolve_output_dir(settings, DATA))
    if args.deliver:
        from core.delivery_packager import build_package
        try:
            zip_path = build_package(ROOT, args.deliver, run_tests=args.run_tests)
        except Exception as exc:  # noqa: BLE001 - packaging errors never contain secret values
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Delivery package: {zip_path}")
        return 0
    if args.verify_audit:
        from enterprise import AuditLog
        v = AuditLog(args.verify_audit).verify()
        print(("OK: " if v["ok"] else "TAMPERED: ") + v["message"], f"({v['entries']} valid entries)")
        return 0 if v["ok"] else 1
    if args.ci or args.portfolio:
        from enterprise import AuditLog, PolicyError, load_policy, run_ci, scan_portfolio
        from core.archive_manager import ArchiveError
        audit = AuditLog(args.audit_log or Path(out_dir) / "audit.jsonl")
        if args.ci:
            return run_ci(args.ci, Path(out_dir) / "ci", args.policy, audit)
        try:
            res = scan_portfolio(args.portfolio, Path(out_dir) / "portfolio", load_policy(args.policy) if args.policy else None, audit,
                                 progress=lambda f, m: print(f"[{f:4.0%}] {m}"))
        except (PolicyError, ArchiveError, ValueError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Projects: {len(res.projects)}  not passing: {res.failed}")
        for k, v in res.files.items():
            print(f"{k}: {v}")
        return 1 if res.failed else 0
    if args.setup_check or args.setup_install:
        return run_setup(args.setup_install, args.assume_yes)
    if args.batch:
        return run_batch(args.batch, out_dir, args.profile, args.recursive, args.profiles_file)
    if args.grade:
        return run_grade(args.grade, out_dir, args.rubric, args.template, args.manual_scores)
    if args.cli:
        return run_cli(args.cli, out_dir, args.docs)
    try:
        import tkinter as tk
    except ImportError:
        print("Tkinter is not available in this Python installation. Re-run the Python installer "
              "and enable 'tcl/tk and IDLE', or use --cli.", file=sys.stderr)
        return 1
    from ui.main_window import MainWindow
    root = tk.Tk()
    MainWindow(root, out_dir, settings=settings, base_dir=DATA)
    try:                                 # check prerequisites in the background; asks before installing anything (policy can switch it off)
        from ui.setup_dialog import schedule_startup_check
        schedule_startup_check(root)
    except Exception:  # noqa: BLE001 - never prevents the application from starting
        pass
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
