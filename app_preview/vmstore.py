"""Machines and snapshots for the VirtualBox-style windows (standard library only, no Tk).

A *machine* is a saved way of running a program in App Preview: project, mode, start command and the settings (screen size, limits,
install step ...). A *snapshot* is what VirtualBox would call one for a disposable container run: the screen picture, the console
text, the machine's settings and the start command at that moment, arranged in a tree (a snapshot taken while another one is
current becomes its child). The container's memory and disk are NOT stored - runs are disposable - so "restore" puts the settings
and the recorded picture back and the next Start runs the project fresh.

Layout under ``<base>`` (default ``<app>/data/vm``)::

    machines.json                       list of machines
    <machine id>/snapshots.json         snapshot tree + current pointer
    <machine id>/<snapshot id>/shot.png, console.txt
    <machine id>/last.png, last.log     the last picture / console of the last run (Manager preview, Log tab)

Ids are hex strings checked before they are used in a path, so a damaged or edited JSON file can never reach outside ``<base>``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAX_SNAPSHOTS = 200
MAX_AUTO_SNAPSHOTS = 10
MAX_PNG = 12 * 1024 * 1024
MAX_CONSOLE = 64 * 1024
MODES = ("run", "debug", "trace", "test")
KINDS = ("console", "web", "gui", "test")
BACKENDS = ("auto", "docker", "podman", "nerdctl", "finch", "custom")
VIEWS = ("fit", "50", "100", "200")
_ID = re.compile(r"^[0-9a-f]{6,32}$")
_SIZE = re.compile(r"^\s*(\d{3,4})\s*[xX×*]\s*(\d{3,4})\s*$")

DEFAULT_SETTINGS: Dict = {
    "screen": "800x600", "fps": 2.0, "view": "fit",
    "timeout_s": 900, "max_workspace_mb": 500, "memory_mb": 1536, "cpus": 1.0, "pids": 256,
    "install": False, "link_output": True, "stop_entry": True,
    "backend": "auto", "custom_exe": "", "image": "", "auto_snapshot_end": False,
}


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def clean_settings(d: Optional[Dict]) -> Dict:
    """Every key present and inside the range the panel accepts; unknown keys are dropped."""
    d = dict(d or {})
    out = dict(DEFAULT_SETTINGS)
    m = _SIZE.match(str(d.get("screen", "")))
    if m and 320 <= int(m.group(1)) <= 3840 and 240 <= int(m.group(2)) <= 2160:
        out["screen"] = f"{int(m.group(1))}x{int(m.group(2))}"
    out["fps"] = _clamp(d.get("fps"), 0.2, 10.0, out["fps"])
    out["view"] = d.get("view") if d.get("view") in VIEWS else "fit"
    out["timeout_s"] = int(_clamp(d.get("timeout_s"), 10, 86400, out["timeout_s"]))
    out["max_workspace_mb"] = int(_clamp(d.get("max_workspace_mb"), 10, 4096, out["max_workspace_mb"]))
    out["memory_mb"] = int(_clamp(d.get("memory_mb"), 128, 65536, out["memory_mb"]))
    out["cpus"] = round(_clamp(d.get("cpus"), 0.25, 32, out["cpus"]), 2)
    out["pids"] = int(_clamp(d.get("pids"), 16, 8192, out["pids"]))
    for k in ("install", "link_output", "stop_entry", "auto_snapshot_end"):
        out[k] = bool(d.get(k, out[k]))
    out["backend"] = d.get("backend") if d.get("backend") in BACKENDS else "auto"
    out["custom_exe"] = str(d.get("custom_exe") or "")[:300]
    out["image"] = str(d.get("image") or "")[:200]
    return out


def default_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "vm"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _ok_id(x) -> bool:
    return isinstance(x, str) and bool(_ID.match(x))


def _pick(cls, d: Dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (d or {}).items() if k in names})


@dataclass
class Machine:
    id: str
    name: str
    description: str = ""
    source: str = ""                                # project folder / ZIP
    mode: str = "run"
    entry: Dict = field(default_factory=dict)       # app_preview Entry as a dict (title, argv, kind, file, ...)
    settings: Dict = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    created: float = 0.0
    last_started: float = 0.0
    last_status: str = ""                           # completed | failed | cancelled | timeout | running | ""
    runs: int = 0

    @property
    def argv(self) -> List[str]:
        return [str(a) for a in (self.entry.get("argv") or [])]

    @property
    def command(self) -> str:
        return " ".join(self.argv) or str(self.entry.get("title") or "")

    @property
    def kind(self) -> str:
        k = str(self.entry.get("kind") or "console")
        return k if k in KINDS else "console"

    def key(self) -> str:
        return hashlib.sha1(json.dumps([self.source, self.argv, self.mode]).encode("utf-8")).hexdigest()


@dataclass
class Snapshot:
    id: str
    parent: str = ""
    name: str = ""
    description: str = ""
    taken: float = 0.0
    state: str = ""                                 # running | paused | ended | idle
    status: str = ""
    screen: str = ""                                # "WxH" of the picture
    has_png: bool = False
    has_console: bool = False
    auto: bool = False
    mode: str = "run"
    source: str = ""
    entry: Dict = field(default_factory=dict)
    settings: Dict = field(default_factory=dict)
    info: Dict = field(default_factory=dict)        # the Details list at that moment (State, Image, Ports ...)


def png_size(data: bytes) -> str:
    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return f"{int.from_bytes(data[16:20], 'big')}x{int.from_bytes(data[20:24], 'big')}"
    return ""


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class VMStore:
    def __init__(self, base: Optional[Path] = None) -> None:
        self.base = Path(base) if base else default_dir()
        self._lock = threading.RLock()
        self.machines: Dict[str, Machine] = {}
        self._snaps: Dict[str, Dict] = {}            # machine id -> {"current": id, "items": [Snapshot]}
        self.load()

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        with self._lock:
            self.machines, self._snaps = {}, {}
            try:
                raw = json.loads((self.base / "machines.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            for d in raw.get("machines", []) if isinstance(raw, dict) else []:
                if not isinstance(d, dict) or not _ok_id(d.get("id")):
                    continue
                m = _pick(Machine, d)
                m.settings = clean_settings(m.settings)
                m.mode = m.mode if m.mode in MODES else "run"
                if not isinstance(m.entry, dict):
                    m.entry = {}
                self.machines[m.id] = m

    def save(self) -> None:
        with self._lock:
            blob = json.dumps({"version": 1, "machines": [asdict(m) for m in self.machines.values()]}, indent=1)
            _atomic_write(self.base / "machines.json", blob.encode("utf-8"))

    def _mdir(self, mid: str) -> Path:
        if not _ok_id(mid):
            raise ValueError("bad machine id")
        return self.base / mid

    def _sdir(self, mid: str, sid: str) -> Path:
        if not _ok_id(sid):
            raise ValueError("bad snapshot id")
        return self._mdir(mid) / sid

    def _load_snaps(self, mid: str) -> Dict:
        with self._lock:
            if mid in self._snaps:
                return self._snaps[mid]
            data = {"current": "", "items": []}
            try:
                raw = json.loads((self._mdir(mid) / "snapshots.json").read_text(encoding="utf-8"))
                items = [_pick(Snapshot, d) for d in raw.get("items", []) if isinstance(d, dict) and _ok_id(d.get("id"))]
                ids = {s.id for s in items}
                for s in items:
                    if s.parent not in ids:
                        s.parent = ""
                data = {"current": raw.get("current") if raw.get("current") in ids else "", "items": items}
            except (OSError, ValueError, TypeError):
                pass
            self._snaps[mid] = data
            return data

    def _save_snaps(self, mid: str) -> None:
        d = self._load_snaps(mid)
        blob = json.dumps({"version": 1, "current": d["current"], "items": [asdict(s) for s in d["items"]]}, indent=1)
        _atomic_write(self._mdir(mid) / "snapshots.json", blob.encode("utf-8"))

    # ------------------------------------------------------------------ machines
    def sorted_machines(self) -> List[Machine]:
        return sorted(self.machines.values(), key=lambda m: m.name.lower())

    def new_machine(self, name: str, source: str = "", mode: str = "run", entry: Optional[Dict] = None,
                    settings: Optional[Dict] = None, description: str = "") -> Machine:
        with self._lock:
            m = Machine(_new_id(), self.unique_name(name or "New machine"), description, source, mode if mode in MODES else "run",
                        dict(entry or {}), clean_settings(settings), time.time())
            self.machines[m.id] = m
            self.save()
            return m

    def unique_name(self, name: str, ignore: str = "") -> str:
        name = (name or "").strip()[:80] or "Machine"
        taken = {m.name.lower() for m in self.machines.values() if m.id != ignore}
        if name.lower() not in taken:
            return name
        n = 2
        while f"{name} ({n})".lower() in taken:
            n += 1
        return f"{name} ({n})"

    def find_machine(self, source: str, argv: List[str], mode: str) -> Optional[Machine]:
        probe = Machine("000000", "", source=source, mode=mode, entry={"argv": list(argv)})
        return next((m for m in self.machines.values() if m.key() == probe.key()), None)

    def update(self, m: Machine) -> None:
        with self._lock:
            m.name = self.unique_name(m.name, ignore=m.id)
            m.settings = clean_settings(m.settings)
            m.mode = m.mode if m.mode in MODES else "run"
            self.machines[m.id] = m
            self.save()

    def clone(self, mid: str, name: str = "") -> Machine:
        src = self.machines[mid]
        return self.new_machine(name or src.name + " Clone", src.source, src.mode, json.loads(json.dumps(src.entry)),
                                dict(src.settings), src.description)

    def remove(self, mid: str, delete_files: bool = True) -> None:
        with self._lock:
            self.machines.pop(mid, None)
            self._snaps.pop(mid, None)
            self.save()
            if delete_files and _ok_id(mid):
                shutil.rmtree(self.base / mid, ignore_errors=True)

    def record_start(self, m: Machine) -> None:
        m.last_started, m.last_status, m.runs = time.time(), "running", m.runs + 1
        self.save()

    def record_end(self, m: Machine, status: str, png: bytes = b"", console: str = "") -> None:
        m.last_status = status or "ended"
        self.save()
        try:
            if png and len(png) <= MAX_PNG:
                _atomic_write(self._mdir(m.id) / "last.png", png)
            if console:
                _atomic_write(self._mdir(m.id) / "last.log", console[-MAX_CONSOLE * 2:].encode("utf-8", "replace"))
        except OSError:
            pass

    def last_png(self, mid: str) -> bytes:
        try:
            return (self._mdir(mid) / "last.png").read_bytes()
        except (OSError, ValueError):
            return b""

    def last_log(self, mid: str) -> str:
        try:
            return (self._mdir(mid) / "last.log").read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return ""

    # ------------------------------------------------------------------ snapshots
    def snapshots(self, mid: str) -> List[Snapshot]:
        return list(self._load_snaps(mid)["items"])

    def get(self, mid: str, sid: str) -> Optional[Snapshot]:
        return next((s for s in self._load_snaps(mid)["items"] if s.id == sid), None)

    def current(self, mid: str) -> str:
        return self._load_snaps(mid)["current"]

    def set_current(self, mid: str, sid: str) -> None:
        with self._lock:
            d = self._load_snaps(mid)
            d["current"] = sid if self.get(mid, sid) else ""
            self._save_snaps(mid)

    def next_name(self, mid: str) -> str:
        nums = [int(m.group(1)) for s in self.snapshots(mid) for m in [re.match(r"^Snapshot (\d+)$", s.name)] if m]
        return f"Snapshot {max(nums, default=0) + 1}"

    def take(self, mid: str, name: str = "", description: str = "", png: bytes = b"", console: str = "", state: str = "",
             status: str = "", info: Optional[Dict] = None, auto: bool = False) -> Snapshot:
        """New snapshot as a child of the current one; it becomes the current one. Raises ValueError when the limit is reached."""
        with self._lock:
            m = self.machines[mid]
            d = self._load_snaps(mid)
            if auto:                                   # automatic snapshots are pruned (oldest first) so they never fill the disk
                autos = [s for s in d["items"] if s.auto]
                for old in autos[:max(0, len(autos) - MAX_AUTO_SNAPSHOTS + 1)]:
                    self.delete(mid, old.id)
            if len(d["items"]) >= MAX_SNAPSHOTS:
                raise ValueError(f"A machine keeps at most {MAX_SNAPSHOTS} snapshots; delete some first.")
            png = png if png and len(png) <= MAX_PNG else b""
            console = (console or "")[-MAX_CONSOLE:]
            s = Snapshot(_new_id(), d["current"], (name or "").strip()[:80] or self.next_name(mid), (description or "")[:2000], time.time(),
                         state, status[:200], png_size(png), bool(png), bool(console), auto, m.mode, m.source,
                         json.loads(json.dumps(m.entry)), dict(m.settings), {str(k)[:40]: str(v)[:300] for k, v in (info or {}).items()})
            sdir = self._sdir(mid, s.id)
            if png:
                _atomic_write(sdir / "shot.png", png)
            if console:
                _atomic_write(sdir / "console.txt", console.encode("utf-8", "replace"))
            d["items"].append(s)
            d["current"] = s.id
            self._save_snaps(mid)
            return s

    def edit(self, mid: str, sid: str, name: Optional[str] = None, description: Optional[str] = None) -> None:
        with self._lock:
            s = self.get(mid, sid)
            if s is None:
                return
            if name is not None and name.strip():
                s.name = name.strip()[:80]
            if description is not None:
                s.description = description[:2000]
            self._save_snaps(mid)

    def delete(self, mid: str, sid: str) -> bool:
        """Remove one snapshot; its children move up to its parent (as in VirtualBox). The current pointer follows."""
        with self._lock:
            d = self._load_snaps(mid)
            s = self.get(mid, sid)
            if s is None:
                return False
            for c in d["items"]:
                if c.parent == sid:
                    c.parent = s.parent
            d["items"] = [x for x in d["items"] if x.id != sid]
            if d["current"] == sid:
                d["current"] = s.parent
            shutil.rmtree(self._sdir(mid, sid), ignore_errors=True)
            self._save_snaps(mid)
            return True

    def delete_all(self, mid: str) -> None:
        with self._lock:
            for s in self.snapshots(mid):
                shutil.rmtree(self._sdir(mid, s.id), ignore_errors=True)
            self._snaps[mid] = {"current": "", "items": []}
            self._save_snaps(mid)

    def png(self, mid: str, sid: str) -> bytes:
        try:
            return (self._sdir(mid, sid) / "shot.png").read_bytes()
        except (OSError, ValueError):
            return b""

    def console(self, mid: str, sid: str) -> str:
        try:
            return (self._sdir(mid, sid) / "console.txt").read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return ""

    def export_png(self, mid: str, sid: str, dest) -> Path:
        data = self.png(mid, sid)
        if not data:
            raise OSError("This snapshot has no picture.")
        dest = Path(dest)
        dest.write_bytes(data)
        return dest

    def tree(self, mid: str) -> List[Tuple[int, Snapshot]]:
        """Pre-order (depth, snapshot): children sorted by time, so the chain reads top to bottom."""
        items = self.snapshots(mid)
        kids: Dict[str, List[Snapshot]] = {}
        for s in items:
            kids.setdefault(s.parent, []).append(s)
        out: List[Tuple[int, Snapshot]] = []

        def walk(pid: str, depth: int, seen: set) -> None:
            for s in sorted(kids.get(pid, []), key=lambda x: x.taken):
                if s.id in seen:
                    continue
                seen.add(s.id)
                out.append((depth, s))
                walk(s.id, depth + 1, seen)
        walk("", 0, set())
        return out

    def disk_usage(self, mid: str) -> int:
        total = 0
        try:
            for p in self._mdir(mid).rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
        except (OSError, ValueError):
            pass
        return total


# ---------------------------------------------------------------------------------------------------------------- text helpers
STATE_NAMES = {"running": "Running", "paused": "Paused", "starting": "Starting", "off": "Powered Off", "aborted": "Aborted"}


def machine_state(m: Machine, live_state: str = "") -> str:
    """'Running' / 'Paused' / 'Starting' for the machine that is live in the panel, else Powered Off or Aborted (last run failed)."""
    if live_state in ("running", "paused", "starting"):
        return STATE_NAMES[live_state]
    return STATE_NAMES["aborted"] if m.last_status in ("failed", "timeout") else STATE_NAMES["off"]


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"
