"""Turn an Entry + mode into a reviewable PreviewPlan (the exact command, image, ports, limits and risks)."""
from __future__ import annotations

import hashlib
import shlex
import socket
from pathlib import Path
from typing import List, Optional

from plugins.build_execution.discovery import DEFAULT_IMAGES
from plugins.build_execution.models import Limits

from .models import DISPLAY_NUM, Entry, PreviewOptions, PreviewPlan

HELPER = "/ase"                                    # read-only mount of app_preview/runtime inside the container


def free_port(preferred: int = 0, span: int = 60) -> int:
    """A TCP port that is free on 127.0.0.1 right now (preferred first, then the next ones, then any)."""
    candidates = list(range(preferred, preferred + span)) if preferred else []
    for p in candidates + [0]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", p))
                return s.getsockname()[1]
        except OSError:
            continue
    raise OSError("No free local port.")


def _source_id(src: Path) -> str:
    if src.is_file():
        h = hashlib.sha256()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return "zip:" + h.hexdigest()
    return "dir:" + str(src.resolve())


def make_plan(source, entry: Entry, mode: str, options: Optional[PreviewOptions] = None, *, image: str = "",
              install: bool = False, breakpoints: Optional[List[str]] = None, host_port: int = 0,
              link_output: bool = True) -> PreviewPlan:
    options = options or PreviewOptions()
    if mode not in ("run", "debug", "trace", "test"):
        raise ValueError(f"Unknown mode '{mode}'.")
    if mode == "test" and entry.kind != "test":
        raise ValueError("Test mode needs a test command.")
    if mode in ("debug", "trace") and not entry.traceable:
        raise ValueError(f"{mode.title()} works for Python scripts and 'python -m module' commands. "
                         "For other languages use Run or Test; located errors still appear in the code view.")
    lim = Limits(**vars(options.limits))
    env = dict(entry.env)
    risks = list(entry.risks) + ["The project's own code runs inside the container and its output is displayed here"]
    notes = list(entry.notes)
    argv = list(entry.argv)
    display = entry.kind == "gui"
    auto_gui_note = ""
    if (not display and mode in ("run", "debug", "trace") and entry.kind == "console" and entry.ecosystem == "python"
            and getattr(options, "autotune", False)):
        from .discovery import entry_uses_gui          # a console-looking start option whose code opens a window
        if entry_uses_gui(source, entry):
            display = True
            auto_gui_note = "This program opens a window (found through its imports), so it runs on the virtual screen (needs the preview GUI image)."
    if mode == "test" and entry.ecosystem == "python" and getattr(options, "autotune", False):
        from .discovery import project_uses_gui          # GUI test suites (tkinter widgets) need a virtual screen, too
        if project_uses_gui(source):
            display = True
            gui_test_note = "The project uses a GUI toolkit, so the tests run on the virtual screen (needs the preview GUI image)."
        else:
            gui_test_note = ""
    else:
        gui_test_note = ""
    helper = False
    interactive = False
    ports: List[tuple] = []
    bps = [b for b in (breakpoints or []) if _valid_bp(b)]

    if entry.ecosystem == "python":
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    helper_mode = mode if mode in ("debug", "trace") else ("out" if mode == "run" and link_output and entry.traceable else "")
    if helper_mode:
        helper, interactive = True, mode == "debug"
        wrapper = ["python", f"{HELPER}/ase_tracer.py", "--mode", helper_mode, "--root", "/work", "--max-events", str(options.max_trace_events)]
        for b in bps:
            wrapper += ["--break", b]
        argv = wrapper + ["--"] + argv[1:]
        notes.append(f"{helper_mode.title()} mode uses a small read-only helper mounted at {HELPER} (app_preview/runtime/ase_tracer.py)"
                     + (" so the output can be shown next to the code that printed it." if helper_mode == "out" else "."))
        if mode == "debug":
            notes.append("In debug mode the program gets an empty stdin; stdin carries the debugger commands.")
    if gui_test_note:
        notes.append(gui_test_note)
    if auto_gui_note:
        notes.append(auto_gui_note)
    if display:
        helper = True
        argv = ["sh", f"{HELPER}/ase_gui_entry.sh"] + argv
        env["DISPLAY"] = DISPLAY_NUM
        env["ASE_SCREEN"] = options.screen + "x24"
        notes.append("The app draws on a virtual screen inside the container; this panel shows snapshots of it.")
    if entry.port and entry.kind == "web":
        host = host_port or free_port(entry.port)
        ports = [(host, entry.port)]
        lim = Limits(**{**vars(lim), "network": True})
        risks.append("A published port needs a network: the network is ENABLED for this run, so the project code can reach it. "
                     f"The port is bound to 127.0.0.1:{host} on this computer only")
    if install and entry.install:
        lim = Limits(**{**vars(lim), "network": True})
        line = " ".join(shlex.quote(a) for a in entry.install) + ' && exec "$@"'
        argv = ["sh", "-c", line, "ase"] + argv
        risks.append("Dependencies are downloaded from the internet and their install scripts run inside the container")
        notes.append("Install step: " + " ".join(entry.install))
    elif entry.needs_network and not lim.network and entry.kind != "test":
        notes.append("This command usually needs dependencies that are not in the image; with the network off it may fail.")
    if entry.needs_network and mode == "test":
        lim = Limits(**{**vars(lim), "network": True})
    if mode == "test":
        lim = Limits(**{**vars(lim), "timeout_s": min(lim.timeout_s, 600)})

    if install and entry.install and getattr(options, "autotune", False):     # large requirements: disk install + fitting limits
        from plugins.build_execution.autotune import apply_preview_install
        lim, argv, env, tuned = apply_preview_install(source, lim, argv, env)
        notes += tuned

    try:
        from plugins.build_execution import storage as _st
        from plugins.build_execution.sandbox import entry_script as _es
        if _es(argv_for_note := list(entry.argv)) and _st.load().get("lean_copy", True):
            notes.append("Lean copy: only this script, the files it imports or names, and anything you add with Include... are copied into the "
                         "container - not the whole folder / archive.")
    except Exception:  # noqa: BLE001 - a note only
        pass
    eco = entry.ecosystem if entry.ecosystem != "static" else "python"
    img = image or (options.gui_image if display else options.images.get(eco) or DEFAULT_IMAGES.get(eco, ""))
    if not img:
        raise ValueError(f"No container image configured for '{entry.ecosystem}'. Enter an image name.")
    src = Path(source)
    return PreviewPlan(entry=entry, mode=mode, image=img, limits=lim, source=str(src), source_id=_source_id(src), argv=argv,
                       env=env, ports=ports, display=display, interactive=interactive, mount_helper=helper,
                       breakpoints=bps, install=bool(install and entry.install), risks=risks, notes=notes)


def _valid_bp(b: str) -> bool:
    f, _, l = b.rpartition(":")
    return bool(f) and l.isdigit() and not f.startswith("/") and ".." not in f.split("/") and "\x00" not in f
