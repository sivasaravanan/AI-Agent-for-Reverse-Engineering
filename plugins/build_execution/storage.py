"""Storage settings and clean-up for container runs (standard library only).

Settings (``data/storage.json``): where the disposable project copy lives (``scratch_dir``; default the system temp folder), whether it is
deleted after every run (``auto_clean``, default on) or kept for inspection (``keep_workspace``), the largest project copied in
(``max_workspace_mb``) and the size of the container's /tmp (``tmp_mb``). ``Limits`` reads its two size defaults from here, so a
changed value is part of the plan the user approves.

Clean-up only touches things this application created: scratch folders ``be_ws_*`` / ``be_pv_*``, containers named ``be-<hex>`` /
``be-pv-<hex>``, and (on request) the GUI preview image. Pruning dangling images and the build cache is global to the container
runtime, so it is a separate, explicitly confirmed option. Nothing here changes Docker's own storage location.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

DEFAULTS = {"scratch_dir": "", "auto_clean": True, "keep_workspace": False, "max_workspace_mb": 500, "tmp_mb": 1024,
            # added (Quick run / clean-up): see plugins/build_execution/quickprep.py, quickrun.py and cleanup.py
            "trim_large": False,        # a project over the limit is copied trimmed (big files / folders left out) instead of refused
            "max_file_mb": 25,          # largest single data file copied in when a project is trimmed or narrowed to one script
            "quick_narrow_mb": 100,     # a script run in a project bigger than this copies only the script and what it imports (fast)
            "clear_after_run": True,    # after every run also remove every leftover copy / stopped container this application made
            "forget_after_run": False,  # after every run forget the selected project (the toolbar selection), nothing of yours is touched
            "auto_pull": False,         # Quick run pulls a missing container image without asking (the pull itself is a download)
            "lean_copy": True}          # a script run copies ONLY what the script needs (its imports, files it names, your includes), never the whole project
RANGES = {"max_workspace_mb": (10, 4096), "tmp_mb": (64, 8192), "max_file_mb": (1, 2048), "quick_narrow_mb": (10, 4096)}
BOOLS = ("auto_clean", "keep_workspace", "trim_large", "clear_after_run", "forget_after_run", "auto_pull", "lean_copy")
SCRATCH_PREFIXES = ("be_ws_", "be_pv_")
CONTAINER_RE = re.compile(r"^be-(?:pv-)?[0-9a-f]{8,12}$")
GUI_IMAGE = "ase-preview-gui:latest"


def _data_dir() -> Path:
    try:
        from core.runtime_paths import data_root
        return Path(data_root()) / "data"
    except Exception:  # noqa: BLE001 - the plugin also works outside the application
        return Path(os.environ.get("AISEA_HOME") or Path.home() / ".ase_agent") / "data"


def settings_path() -> Path:
    return _data_dir() / "storage.json"


def load() -> Dict:
    out = dict(DEFAULTS)
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(raw, dict):
        out.update(sanitize(raw))
    return out


def sanitize(raw: Dict) -> Dict:
    clean: Dict = {}
    if isinstance(raw.get("scratch_dir"), str):
        clean["scratch_dir"] = raw["scratch_dir"].strip()
    for k in BOOLS:
        if isinstance(raw.get(k), bool):
            clean[k] = raw[k]
    for k, (lo, hi) in RANGES.items():
        try:
            clean[k] = max(lo, min(hi, int(raw[k])))
        except (KeyError, TypeError, ValueError):
            pass
    return clean


def save(**changes) -> Dict:
    """Validate and store the changed settings. Raises ValueError (readable) for an unusable scratch folder."""
    cur = load()
    new = sanitize(changes)
    if new.get("scratch_dir"):
        ok, msg = validate_scratch_dir(new["scratch_dir"])
        if not ok:
            raise ValueError(msg)
    cur.update(new)
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


def default(name: str):
    """Current stored value of a size setting (used as the default of ``Limits``)."""
    return load().get(name, DEFAULTS[name])


def validate_scratch_dir(path: str) -> Tuple[bool, str]:
    p = Path(path).expanduser()
    if not p.is_absolute():
        return False, "The scratch folder must be an absolute path."
    if "," in str(p):
        return False, "The scratch folder path must not contain a comma (it is mounted into the container)."
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = tempfile.mkdtemp(prefix="be_probe_", dir=str(p))
        shutil.rmtree(probe, ignore_errors=True)
    except OSError as exc:
        return False, f"Cannot write to that folder: {exc}"
    return True, ""


def scratch_root() -> Optional[str]:
    """The configured scratch folder if it is usable, else None (= system temp folder)."""
    d = load()["scratch_dir"]
    if d and validate_scratch_dir(d)[0]:
        return str(Path(d).expanduser())
    return None
def temp_root() -> Optional[str]:
    """Folder for EVERY temporary copy this application makes (analysis copies too, not only container runs): the configured scratch
    folder, or None = the system temp folder. Used as ``dir=`` of tempfile.mkdtemp by the other modules."""
    return scratch_root()


def new_scratch(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir=scratch_root()))


def finish_scratch(scratch: Optional[Path]) -> Tuple[bool, str]:
    """After a run: delete the disposable copy unless the user chose to keep it. Returns (removed, note)."""
    if scratch is None:
        return True, ""
    s = load()
    if s["keep_workspace"] or not s["auto_clean"]:
        return False, f"The disposable project copy was kept at {scratch} (Storage settings); use Storage & clean-up to remove it."
    shutil.rmtree(scratch, ignore_errors=True)
    return (not scratch.exists()), ""


# ------------------------------------------------------------------ scratch folders
def _du(p: Path) -> int:
    total = 0
    for d, _dirs, files in os.walk(p, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    return total


def scratch_inventory() -> List[Dict]:
    roots = {Path(tempfile.gettempdir())}
    if scratch_root():
        roots.add(Path(scratch_root()))
    out = []
    for r in roots:
        try:
            kids = list(r.iterdir())
        except OSError:
            continue
        for k in kids:
            if k.is_dir() and not k.is_symlink() and k.name.startswith(SCRATCH_PREFIXES):
                try:
                    out.append({"path": str(k), "bytes": _du(k), "age_s": max(0, time.time() - k.stat().st_mtime)})
                except OSError:
                    pass
    return sorted(out, key=lambda x: -x["bytes"])


def remove_scratch(paths: Optional[List[str]] = None, min_age_s: float = 0) -> List[str]:
    """Delete leftover scratch folders (only names this application creates). Returns the removed paths."""
    wanted = {str(Path(p)) for p in paths} if paths is not None else None
    removed = []
    for item in scratch_inventory():
        if wanted is not None and item["path"] not in wanted:
            continue
        if item["age_s"] < min_age_s:
            continue
        shutil.rmtree(item["path"], ignore_errors=True)
        if not Path(item["path"]).exists():
            removed.append(item["path"])
    return removed


def sweep_old_scratch(max_age_s: float = 3600) -> List[str]:
    """Start-up housekeeping: leftovers of a crashed run (older than an hour, so a running one is never touched)."""
    if load()["keep_workspace"]:
        return []
    return remove_scratch(None, min_age_s=max_age_s)


# ------------------------------------------------------------------ container runtime side
def _run(exe: str, args: List[str], timeout: float = 60) -> Tuple[Optional[int], str, str]:
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        p = subprocess.run([exe, *args], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)


def runtime_inventory(exe: str, run: Callable = _run) -> Dict:
    """What this application left in the container runtime, plus the runtime's storage overview. Read-only."""
    inv: Dict = {"containers": [], "gui_image": None, "dangling": 0, "df": "", "root": ""}
    rc, out, _ = run(exe, ["ps", "-a", "--filter", "name=be-", "--format", "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}"])
    for line in (out if rc == 0 else "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and CONTAINER_RE.match(parts[1]):
            inv["containers"].append({"id": parts[0], "name": parts[1], "state": parts[2], "status": parts[3] if len(parts) > 3 else ""})
    rc, out, _ = run(exe, ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}"])
    for line in (out if rc == 0 else "").splitlines():
        if line.split("\t")[0] == GUI_IMAGE:
            inv["gui_image"] = {"tag": GUI_IMAGE, "size": line.split("\t")[1] if "\t" in line else ""}
    rc, out, _ = run(exe, ["image", "ls", "--filter", "dangling=true", "-q"])
    inv["dangling"] = len([x for x in (out if rc == 0 else "").splitlines() if x.strip()])
    rc, out, err = run(exe, ["system", "df"])
    inv["df"] = (out if rc == 0 else err).strip()[:2000]
    for fmt in ("{{.DockerRootDir}}", "{{.Store.GraphRoot}}"):
        rc, out, _ = run(exe, ["info", "--format", fmt])
        if rc == 0 and out.strip() and "<no value>" not in out and "{{" not in out:
            inv["root"] = out.strip().splitlines()[0]
            break
    return inv


def clean_runtime(exe: str, *, containers: bool = True, include_running: bool = False, gui_image: bool = False,
                  dangling_images: bool = False, build_cache: bool = False, run: Callable = _run) -> List[str]:
    """Remove what was asked for. Containers: only our own names; running ones only with ``include_running``. Returns a report."""
    report: List[str] = []
    inv = runtime_inventory(exe, run)
    if containers:
        n = 0
        for c in inv["containers"]:
            if c["state"] == "running" and not include_running:
                report.append(f"kept running container {c['name']}")
                continue
            if not CONTAINER_RE.match(c["name"]):
                continue
            rc, _o, err = run(exe, ["rm", "-f", c["name"]], 60)
            n += rc == 0
            if rc != 0:
                report.append(f"could not remove {c['name']}: {err.strip()[:120]}")
        report.append(f"removed {n} container(s)")
    if gui_image:
        rc, _o, err = run(exe, ["image", "rm", GUI_IMAGE], 120)
        report.append(f"removed image {GUI_IMAGE}" if rc == 0 else f"could not remove {GUI_IMAGE}: {err.strip()[:160]}")
    if dangling_images:
        rc, out, err = run(exe, ["image", "prune", "-f"], 300)
        report.append("pruned dangling images" + (f": {out.strip().splitlines()[-1]}" if rc == 0 and out.strip() else "") if rc == 0
                      else f"could not prune images: {err.strip()[:160]}")
    if build_cache:
        rc, out, err = run(exe, ["builder", "prune", "-f"], 300)
        report.append("pruned the build cache" if rc == 0 else f"could not prune the build cache: {err.strip()[:160]}")
    return report


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"
