"""python -m prerequisites <command>

  check [--json]                    what is installed (detect first, always)
  diagnose [--network] [--json]     institutional hurdles (admin rights, proxy, virtualization, policy ...)
  plan ID [ID...]                   show exactly what installing would do (changes nothing)
  install ID [ID...] --yes          install after you reviewed the plan; Docker also needs --accept-license docker
  start podman|docker               start the runtime and wait until it answers
  pull-images --yes                 pull the Phase 4 sandbox images (honours registry_mirror)
  it-request [ID...] [--out FILE]   text for your IT desk
  offline-manifest [ID...]          what to download elsewhere for an air-gapped machine
  network-test                      reachability through the configured proxy/CA
  config show | config set KEY VALUE
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import List, Optional

from core.runtime_paths import data_root
from prerequisites import (BY_ID, CATALOG, CONTAINER_RUNTIMES, Approval, Env, InstallPlan, PlanRefusal, build_it_request, build_plan,
                           container_ready, detect_all, diagnose, expand_dependencies, load_config, offline_manifest, run_plan,
                           save_user_config, start_runtime, pull_sandbox_images)
from prerequisites import network, tools_env
from prerequisites.config import DEFAULTS, user_config_path


def _ids(raw: List[str]) -> List[str]:
    out = []
    for r in raw:
        if r not in BY_ID:
            raise SystemExit(f"Unknown prerequisite '{r}'. Known: {', '.join(BY_ID)}")
        out.append(r)
    return out


def main(argv: Optional[List[str]] = None, env: Optional[Env] = None, cfg=None) -> int:
    ap = argparse.ArgumentParser(prog="prerequisites", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("--json", action="store_true")
    d = sub.add_parser("diagnose"); d.add_argument("--network", action="store_true"); d.add_argument("--json", action="store_true")
    pl = sub.add_parser("plan"); pl.add_argument("ids", nargs="+")
    ins = sub.add_parser("install"); ins.add_argument("ids", nargs="+"); ins.add_argument("--yes", action="store_true")
    ins.add_argument("--accept-license", action="append", default=[], metavar="ID"); ins.add_argument("--scope", choices=("user", "machine"))
    st = sub.add_parser("start"); st.add_argument("runtime", choices=CONTAINER_RUNTIMES)
    pi = sub.add_parser("pull-images"); pi.add_argument("--yes", action="store_true")
    it = sub.add_parser("it-request"); it.add_argument("ids", nargs="*"); it.add_argument("--out"); it.add_argument("--contact", default="")
    om = sub.add_parser("offline-manifest"); om.add_argument("ids", nargs="*")
    sub.add_parser("network-test")
    cf = sub.add_parser("config"); cf.add_argument("action", choices=("show", "set")); cf.add_argument("key", nargs="?"); cf.add_argument("value", nargs="?")
    a = ap.parse_args(argv)

    cfg = cfg or load_config()
    env = env or Env.real(interactive=sys.stdin.isatty())
    tools_env.activate(env.root, env.platform)
    for w in cfg.warnings:
        print("Config:", w, file=sys.stderr)

    if a.cmd == "check":
        sts = detect_all(env)
        if a.json:
            print(json.dumps([s.to_dict() for s in sts], indent=2))
        else:
            for s in sts:
                print(f"{s.name:32} {s.state:12} {s.version[:40]:40} {s.detail[:80]}")
            ready, backend = container_ready(sts)
            print("\nContainer runtime:", f"READY ({backend})" if ready else "NOT ready - builds/tests will be skipped (static analysis still works).")
        return 0
    if a.cmd == "diagnose":
        conn = network.check_connectivity(cfg) if a.network else None
        dg = diagnose(env, cfg, connectivity=conn, statuses=detect_all(env))
        if a.json:
            print(json.dumps(dg.to_dict(), indent=2))
        else:
            print("System:", json.dumps(dg.system))
            for h in dg.hurdles:
                print(f"\n[{h.severity.upper()}] {h.title}\n  {h.detail}\n  -> {h.fix}")
            if not dg.hurdles:
                print("No hurdles detected.")
        return 0
    if a.cmd in ("plan", "install"):
        ids = expand_dependencies(_ids(a.ids), env)
        plans = [build_plan(i, env, cfg, scope=getattr(a, "scope", None)) for i in ids]
        for p in plans:
            print(p.describe() if isinstance(p, InstallPlan) else f"Cannot install {p.prereq_id}: {p.reason}\n  {p.advice} {p.manual_url}")
            print()
        if a.cmd == "plan":
            return 0
        if not a.yes:
            print("Nothing was installed. Review the plan above, then repeat with --yes to approve exactly these steps.", file=sys.stderr)
            return 3
        rc = 0
        for p in plans:
            if isinstance(p, PlanRefusal):
                rc = 1
                continue
            lic = p.prereq_id in a.accept_license
            res = run_plan(p, Approval.for_plan(p, license_accepted=lic), env, cfg, progress=print)
            print(f"==> {p.name}: {res.status} {res.detail}\n    {res.advice}")
            rc = rc or (0 if res.ok else 1)
        return rc
    if a.cmd == "start":
        s = start_runtime(a.runtime, env, progress=print)
        print(s.name, s.state, s.detail)
        return 0 if s.state == "ready" else 1
    if a.cmd == "pull-images":
        if not a.yes:
            print("Pulling images downloads several GB. Repeat with --yes to approve.", file=sys.stderr)
            return 3
        ok, backend = container_ready(detect_all(env, CONTAINER_RUNTIMES))
        if not ok:
            print("No ready container runtime.", file=sys.stderr)
            return 1
        res = pull_sandbox_images(env, cfg, backend, progress=print)
        for r in res:
            print(r.prereq_id, r.status, r.detail)
        return 0 if all(r.ok for r in res) else 1
    if a.cmd == "it-request":
        ids = _ids(a.ids) if a.ids else [i for i in ("wsl2", "podman") if i in BY_ID and (i != "wsl2" or env.platform == "windows")]
        text = build_it_request(ids, env, cfg, a.contact)
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
            print("wrote", a.out)
        else:
            print(text)
        return 0
    if a.cmd == "offline-manifest":
        ids = _ids(a.ids) if a.ids else ["podman", "docker"]
        print(json.dumps(offline_manifest(ids, env, cfg), indent=2))
        return 0
    if a.cmd == "network-test":
        bad = 0
        for r in network.check_connectivity(cfg):
            print(f"{r['host']:32} {'OK ' if r['ok'] == 'yes' else 'FAIL'} {r['detail'][:60]} {r['advice']}")
            bad += r["ok"] != "yes"
        return 1 if bad else 0
    if a.cmd == "config":
        if a.action == "show":
            print(json.dumps({"values": cfg.to_dict(), "locked_by_policy": sorted(cfg.locked), "sources": cfg.sources}, indent=2))
            return 0
        if not a.key or a.key not in DEFAULTS or a.value is None:
            print(f"Usage: config set KEY VALUE. Keys: {', '.join(DEFAULTS)}", file=sys.stderr)
            return 2
        default = DEFAULTS[a.key]
        val = (a.value.lower() in ("1", "true", "yes", "on")) if isinstance(default, bool) else int(a.value) if isinstance(default, int) \
            else [x for x in a.value.split(",") if x] if isinstance(default, list) else a.value
        for w in save_user_config({a.key: val}, user_config_path(env.root), cfg):
            print(w, file=sys.stderr)
        print("saved", user_config_path(env.root))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
