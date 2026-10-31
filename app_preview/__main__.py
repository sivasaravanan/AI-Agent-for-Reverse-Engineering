"""Command line: list what can be started, or run one entry in a container and stream its output.

    python -m app_preview PROJECT.zip --list
    python -m app_preview PROJECT.zip --entry python:main.py --mode trace [--install] [--backend podman] [--yes]
Without --yes the exact plan is printed and you must type 'yes'.
"""
from __future__ import annotations

import argparse
import sys

from plugins.build_execution.events import BUS

from . import PreviewOptions, approve, discover_entries, make_plan, start, status
from .container import container_command


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app_preview")
    ap.add_argument("source", nargs="?", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--entry", default="")
    ap.add_argument("--mode", default="run", choices=("run", "debug", "trace", "test"))
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--image", default="")
    ap.add_argument("--install", action="store_true", help="install requirements/npm packages inside the container first (network)")
    ap.add_argument("--yes", action="store_true", help="approve the printed plan without asking")
    ap.add_argument("--build-gui-image", action="store_true", help="build the GUI preview image (needs network; asks first)")
    a = ap.parse_args(argv)
    if a.build_gui_image:
        return _build_image(a)
    if not a.source:
        ap.error("a ZIP or folder is required (or use --build-gui-image)")
    entries, notes = discover_entries(a.source)
    if a.list or not a.entry:
        for e in entries:
            print(f"{e.id:40} {e.kind:8} {e.title}")
        for n in notes:
            print("note:", n)
        return 0
    entry = next((e for e in entries if e.id == a.entry), None)
    if entry is None:
        print("Unknown entry id; use --list.", file=sys.stderr)
        return 2
    opts = PreviewOptions(backend=a.backend)
    plan = make_plan(a.source, entry, a.mode, opts, image=a.image, install=a.install)
    st = status(opts)
    print(f"Command : {' '.join(plan.argv)}\nImage   : {plan.image} (never pulled)\nNetwork : {'ENABLED' if plan.limits.network else 'disabled'}"
          f"\nPorts   : {plan.ports or '-'}\nRuntime : {st['backend'] or 'none'} {'ready' if st['available'] else '- ' + st['reason']}")
    for r in plan.risks:
        print("Risk    :", r)
    print("Container command line:\n  " + " ".join(container_command("<runtime>", plan, "<scratch-copy>", "<helper>", "<name>")))
    if not st["available"]:
        return 3
    if not a.yes and input("Type 'yes' to approve and run: ").strip().lower() != "yes":
        print("Not approved. Nothing was run.")
        return 1

    def show(ev):
        if ev["type"] in ("out", "err"):
            (sys.stdout if ev["type"] == "out" else sys.stderr).write(ev["text"])
        elif ev["type"] == "phase":
            print("▸", ev["text"])
    BUS.subscribe(show)
    s = start(plan, approve(plan), opts)
    try:
        res = s.wait()
    except KeyboardInterrupt:
        s.stop()
        res = s.wait(30)
    print(f"\n[{res.status}] exit={res.exit_code} {res.duration_s}s", *res.notes, sep="\n  ")
    return 0 if res and res.ok else 1


def _build_image(a) -> int:
    from .imagebuilder import build_gui_image, make_build_plan
    opts = PreviewOptions(backend=a.backend)
    try:
        plan = make_build_plan(opts, tag=a.image)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 3
    print("Build command :", plan.command_line, f"\nDockerfile    : sha256 {plan.dockerfile_sha256[:16]}...")
    for r in plan.risks:
        print("Risk          :", r)
    if not a.yes and input("Type 'yes' to approve and build: ").strip().lower() != "yes":
        print("Not approved. Nothing was built.")
        return 1
    res = build_gui_image(plan, approve(plan), progress=print)
    print(*res["notes"], sep="\n")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
