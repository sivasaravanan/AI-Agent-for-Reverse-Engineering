"""CLI:
  python -m plugins.build_execution check
  python -m plugins.build_execution candidates <zip|folder>
  python -m plugins.build_execution run <zip|folder> --candidate node:npm-test [--network] [--timeout 300]
"""
import argparse
import json
import shlex
import sys

from . import (Approval, Limits, Options, build_plan, container_command, custom_candidate, discover_candidates,
               isolation_status, run_plan)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build_execution")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    c = sub.add_parser("candidates"); c.add_argument("source")
    r = sub.add_parser("run")
    r.add_argument("source")
    r.add_argument("--candidate")
    r.add_argument("--custom", help="custom command line, e.g. --custom 'python -m unittest'")
    r.add_argument("--ecosystem", default="python")
    r.add_argument("--image", default="")
    r.add_argument("--network", action="store_true", help="enable container networking (needs your approval)")
    r.add_argument("--timeout", type=int, default=300)
    r.add_argument("--memory-mb", type=int, default=1024)
    r.add_argument("--json")
    r.add_argument("--yes-reviewed", action="store_true",
                   help="skip the interactive question AFTER you have reviewed the printed plan yourself")
    a = ap.parse_args(argv)

    if a.cmd == "check":
        s = isolation_status()
        print(json.dumps(s, indent=2))
        return 0 if s["available"] else 1
    if a.cmd == "candidates":
        from .run_targets import discover_all
        cands, notes = discover_all(a.source)
        for x in cands:
            print(f"{x.id:24s} network={'YES' if x.needs_network else 'no ':3s} {x.display}")
            for k in x.risks:
                print(f"{'':26s}risk: {k}")
        for n in notes:
            print("note:", n)
        return 0

    from .run_targets import discover_all
    cands, _ = discover_all(a.source)
    if a.custom:
        cand = custom_candidate(shlex.split(a.custom), a.ecosystem, a.network)
    else:
        cand = next((x for x in cands if x.id == a.candidate), None)
        if cand is None:
            print("Unknown candidate. Run the 'candidates' command first.", file=sys.stderr)
            return 2
    opts = Options()
    lim = Limits(timeout_s=a.timeout, memory_mb=a.memory_mb, network=a.network)
    plan = build_plan(a.source, cand, opts, image=a.image, limits=lim)
    print("=== EXECUTION PLAN - review carefully ===")
    print("Command (inside container):", cand.display)
    print("Image:", plan.image, "| network:", "ENABLED" if plan.limits.network else "disabled",
          "| timeout:", plan.limits.timeout_s, "s | memory:", plan.limits.memory_mb, "MB")
    for k in cand.risks:
        print("  risk:", k)
    print("Container command:", " ".join(container_command("<runtime>", plan, "<scratch-copy>", "<name>")))
    if not a.yes_reviewed and input("Type YES to approve and run: ").strip() != "YES":
        print("Not approved. Nothing was executed.")
        return 3
    res = run_plan(plan, Approval.for_plan(plan), opts, progress=lambda m: print(m, file=sys.stderr))
    print(f"status={res.status} exit={res.exit_code} duration={res.duration_s}s")
    for n in res.notes:
        print("note:", n)
    print(res.stdout[-4000:])
    if res.stderr:
        print("--- stderr ---\n" + res.stderr[-2000:])
    if a.json:
        res.write_json(a.json)
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
