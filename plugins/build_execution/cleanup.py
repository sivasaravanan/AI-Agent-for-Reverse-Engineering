"""Clean-up of everything this application copies or starts for a run (standard library only). New module, additive.

What counts as "ours" (nothing else is ever touched):
  * project copies and temporary analysis copies in the scratch / system temp folder, by their prefixes
    (``be_ws_`` / ``be_pv_`` container runs, ``ase_src_`` / ``ase_input_`` / ``ase_an_`` / ``ase_php_`` / ``sa_plugin_`` analysis);
  * containers named ``be-<hex>`` / ``be-pv-<hex>`` (already removed by ``--rm``; this catches the ones a crash or kill left behind).
Your project folder / archive / file is NEVER modified or deleted: only copies made from it are removed.

Two entry points:
  ``after_run(exe)``         called automatically at the end of every run (setting "Clear after every run", default on). Copies younger
                             than ``MIN_AGE_S`` are kept, so an analysis that is still running is never pulled from under its feet.
  ``clear_everything(exe)``  the "Clear now" button: everything of ours, any age; running containers only with ``include_running``.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import storage

APP_TEMP_PREFIXES = ("be_ws_", "be_pv_", "ase_src_", "ase_input_", "ase_an_", "ase_php_", "sa_plugin_")
MIN_AGE_S = 120


def _roots() -> List[Path]:
    roots = [Path(tempfile.gettempdir())]
    sr = storage.scratch_root()
    if sr and Path(sr) not in roots:
        roots.append(Path(sr))
    return roots


def inventory(all_temp: bool = True) -> List[Dict]:
    """Our leftover copies: [{'path', 'bytes', 'age_s', 'kind'}] largest first. ``all_temp=False`` = container-run copies only."""
    prefixes = APP_TEMP_PREFIXES if all_temp else storage.SCRATCH_PREFIXES
    out: List[Dict] = []
    for r in _roots():
        try:
            kids = list(r.iterdir())
        except OSError:
            continue
        for k in kids:
            if k.is_dir() and not k.is_symlink() and k.name.startswith(prefixes):
                try:
                    out.append({"path": str(k), "bytes": storage._du(k), "age_s": max(0.0, time.time() - k.stat().st_mtime),
                                "kind": "container run copy" if k.name.startswith(storage.SCRATCH_PREFIXES) else "analysis copy"})
                except OSError:
                    pass
    return sorted(out, key=lambda x: -x["bytes"])


def remove(paths: Optional[List[str]] = None, min_age_s: float = 0.0, all_temp: bool = True) -> List[str]:
    """Delete our leftover copies. Returns the removed paths."""
    wanted = {str(Path(p)) for p in paths} if paths is not None else None
    gone: List[str] = []
    for item in inventory(all_temp):
        if wanted is not None and item["path"] not in wanted:
            continue
        if item["age_s"] < min_age_s:
            continue
        shutil.rmtree(item["path"], ignore_errors=True)
        if not Path(item["path"]).exists():
            gone.append(item["path"])
    return gone


def after_run(exe: Optional[str] = None, run: Callable = storage._run) -> List[str]:
    """Automatic clean-up at the end of a run. Never raises; returns a short report (empty when the setting is off or nothing was there)."""
    try:
        cfg = storage.load()
        if not cfg["clear_after_run"] or cfg["keep_workspace"] or not cfg["auto_clean"]:
            return []
        report: List[str] = []
        gone = remove(None, min_age_s=MIN_AGE_S)
        if gone:
            report.append(f"cleared {len(gone)} leftover copy/copies of the project")
        if exe:
            rep = storage.clean_runtime(exe, containers=True, include_running=False, run=run)
            report += [r for r in rep if not r.startswith("removed 0 ")]
        return report
    except Exception:  # noqa: BLE001 - housekeeping never disturbs a run
        return []


def clear_everything(exe: Optional[str] = None, include_running: bool = False, run: Callable = storage._run) -> List[str]:
    """The 'Clear now' button: every copy and container of ours, any age. Returns a report."""
    report: List[str] = []
    before = sum(i["bytes"] for i in inventory())
    gone = remove(None, min_age_s=0)
    report.append(f"removed {len(gone)} project copy/copies ({storage.human(before)})")
    if exe:
        report += storage.clean_runtime(exe, containers=True, include_running=include_running, run=run)
    else:
        report.append("no container runtime found: only local copies were cleared")
    return report


def has_leftovers() -> bool:
    """Quick check (no sizes): is there any copy of ours lying around?"""
    for r in _roots():
        try:
            if any(k.is_dir() and k.name.startswith(APP_TEMP_PREFIXES) for k in r.iterdir()):
                return True
        except OSError:
            continue
    return False


def fresh_start(exe: Optional[str] = None, run: Callable = storage._run) -> List[str]:
    """Before a run and when the application closes: delete every leftover copy and stopped container of ours (your own files are never
    touched). Nothing is removed while another run is active, so a live run never loses its copy."""
    from .events import BUS
    if BUS.active():
        return ["left alone: another run is still active"]
    return clear_everything(exe, include_running=False, run=run)
