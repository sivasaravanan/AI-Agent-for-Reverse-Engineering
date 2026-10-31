"""Let Build & Test run programs that open a window: GUI image + virtual screen (Xvfb) around the planned command.

The plain python image has no Tk and no screen ("ImportError: libtk8.6.so ..."). For a program that uses a GUI toolkit this module
wraps the command so it starts on a virtual screen inside the preview GUI image. A program that opens a window runs until it is
closed, so the wrapper treats "still running after a few seconds" as "started without errors" and stops it (smoke run); tests run
to the end. Detection only parses text; nothing is imported or executed on the host. The wrapped command is shown in the approval.
"""
from __future__ import annotations

import dataclasses
import re
from .discovery import SourceView
from .models import Candidate

GUI_IMAGE = "ase-preview-gui:latest"                # built once from app_preview/images/Dockerfile.gui, never pulled
SMOKE_SECONDS = 30
GUI_IMPORTS = re.compile(r"^\s*(?:from|import)\s+(tkinter|customtkinter|ttkbootstrap|PyQt5|PyQt6|PySide2|PySide6|wx|kivy|pygame|pyglet|arcade|turtle)\b", re.M)
_PY_CALL = re.compile(r"""\bpython3?\s+((?:-[A-Za-z]+\s+)*)["']?([\w./\\-]+)["']?""")

SCRIPT = r'''SCREEN="${ASE_SCREEN:-1024x768x24}"; DISPLAY=:99; export DISPLAY
if ! command -v Xvfb >/dev/null 2>&1; then
  echo "[gui] Xvfb is not installed in this image. Build the preview GUI image (app_preview/images/Dockerfile.gui)." >&2; exit 127
fi
Xvfb "$DISPLAY" -screen 0 "$SCREEN" -nolisten tcp >/tmp/xvfb.log 2>&1 &
XPID=$!
i=0
while [ ! -S /tmp/.X11-unix/X99 ] && [ $i -lt 100 ]; do i=$((i+1)); sleep 0.1; done
if [ "$ASE_SMOKE" = "1" ]; then
  "$@" &
  PID=$!
  n=0
  while kill -0 "$PID" 2>/dev/null && [ "$n" -lt "$ASE_SMOKE_TICKS" ]; do n=$((n+1)); sleep 0.1; done
  if kill -0 "$PID" 2>/dev/null; then
    echo "[gui] The program is still running after $((ASE_SMOKE_TICKS/10)) s on the virtual screen: it started without errors. Stopped by the agent."
    kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; CODE=0
  else
    wait "$PID"; CODE=$?
  fi
else
  "$@"; CODE=$?
fi
kill "$XPID" 2>/dev/null
exit $CODE
'''


def _local_gui(src: SourceView, text: str) -> bool:
    names = src.names
    tops = {(n[:-3] if "/" not in n and n.endswith(".py") else n.split("/")[0]) for n in names}
    for m in sorted(set(re.findall(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", text, re.M)) & tops)[:8]:
        for f in [n for n in names if n.endswith(".py") and (n == m + ".py" or n.startswith(m + "/"))][:80]:
            if GUI_IMPORTS.search(src.read(f, 200_000) or ""):
                return True
    return False


def _targets(argv: list, source_file: str) -> list:
    """Python files / modules a command starts: ('file', path) or ('module', name)."""
    out = []
    if source_file.endswith(".py"):
        out.append(("file", source_file))
    cmds = [" ".join(argv)] if argv and argv[0] == "sh" and len(argv) > 2 and argv[1] == "-c" else []
    if argv and argv[0] in ("python", "python3"):
        cmds.append(" ".join(argv))
    for c in cmds:
        for flags, tgt in _PY_CALL.findall(c):
            if tgt.endswith(".py"):
                out.append(("file", tgt))
            elif "m" in flags.replace("-", ""):
                out.append(("module", tgt.split(".")[0]))
    return out


def uses_gui(source, argv: list, source_file: str = "", project_wide: bool = False) -> bool:
    try:
        src = SourceView(source)
    except Exception:  # noqa: BLE001 - bad source: the normal path reports it
        return False
    try:
        if project_wide:
            return any(GUI_IMPORTS.search(src.read(n, 200_000) or "") for n in [n for n in src.names if n.endswith(".py")][:300])
        for kind, tgt in _targets(argv, source_file):
            if kind == "file":
                tgt = tgt[2:] if tgt.startswith("./") else tgt
                text = src.read(tgt, 2_000_000) or ""
            else:
                text = f"import {tgt}\n"
            if text and (GUI_IMPORTS.search(text) or _local_gui(src, text)):
                return True
        return False
    finally:
        src.close()


def apply_gui_screen(candidate: Candidate, source, image: str = "") -> tuple:
    """Return (candidate, image, notes). Unchanged unless the command starts a Python program (or test suite) that uses a GUI toolkit."""
    if candidate.ecosystem != "python" or not candidate.argv or candidate.argv[0] == "xvfb-run":
        return candidate, image, []
    joined = " ".join(candidate.argv)
    is_test = bool(re.search(r"pytest|unittest|\btest\b", f"{candidate.id} {joined}", re.I))
    if "compileall" in joined or not uses_gui(source, candidate.argv, candidate.source_file or "", project_wide=is_test):
        return candidate, image, []
    env = dict(candidate.env)
    env["ASE_SMOKE"] = "0" if is_test else "1"
    env["ASE_SMOKE_TICKS"] = str(SMOKE_SECONDS * 10)
    notes = ["This program uses a GUI toolkit: it runs on a virtual screen in the preview GUI image (" + GUI_IMAGE + ", build it once)."]
    notes.append("The tests run to the end on the virtual screen." if is_test else
                 f"A program that opens a window runs until it is closed, so it is stopped after {SMOKE_SECONDS} s; "
                 "still running by then counts as 'started without errors'. Use App Preview to see and use the window.")
    cand = dataclasses.replace(candidate, argv=["sh", "-c", SCRIPT, "ase"] + list(candidate.argv), env=env,
                               notes=list(candidate.notes) + notes)
    return cand, (image or GUI_IMAGE), notes
