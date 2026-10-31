"""Start-up behaviour: when to check, when to ask, how to remember "not now" / "don't ask again" - without nagging."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from core.runtime_paths import data_root
from prerequisites.catalog import BY_ID, CONTAINER_RUNTIMES
from prerequisites.detect import INSTALLED, MISSING, READY, Status, container_ready


def state_path(root: Optional[Path] = None) -> Path:
    return (root or data_root()) / "data" / "prerequisites_state.json"


def load_state(path: Optional[Path] = None) -> Dict:
    try:
        return json.loads(Path(path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: Dict, path: Optional[Path] = None) -> None:
    p = Path(path or state_path())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def snooze(days: int, path: Optional[Path] = None, now: Optional[float] = None) -> None:
    st = load_state(path)
    st["snooze_until"] = (now or time.time()) + days * 86400
    save_state(st, path)


def dont_ask_again(path: Optional[Path] = None) -> None:
    st = load_state(path)
    st["dont_ask"] = True
    save_state(st, path)


def remember_license(pid: str, path: Optional[Path] = None) -> None:
    st = load_state(path)
    st.setdefault("license_accepted", [])
    if pid not in st["license_accepted"]:
        st["license_accepted"].append(pid)
    save_state(st, path)


@dataclass
class Decision:
    action: str                       # prompt | auto | skip
    reason: str
    offer: List[str] = field(default_factory=list)       # prerequisite ids to pre-select
    recommended_runtime: str = ""


def recommended_runtime(cfg) -> str:
    return cfg.prefer_runtime if cfg.prefer_runtime in CONTAINER_RUNTIMES else "podman"


def decide(statuses: List[Status], cfg, state: Optional[Dict] = None, now: Optional[float] = None, env_skip: bool = False) -> Decision:
    state = state or {}
    if env_skip or os.environ.get("AISEA_NO_PREREQ_CHECK") == "1":
        return Decision("skip", "disabled by command line/environment")
    if cfg.mode == "off" or not cfg.startup_check:
        return Decision("skip", "start-up check disabled in settings/policy")
    ready, _backend = container_ready(statuses)
    if ready:
        return Decision("skip", "a container runtime is ready")
    if state.get("dont_ask"):
        return Decision("skip", "user chose not to be asked again")
    if (state.get("snooze_until") or 0) > (now or time.time()):
        return Decision("skip", "reminder snoozed")
    if not cfg.allow_installs and not cfg.allow_downloads and not cfg.offline_dir:
        return Decision("skip", "policy forbids installing and no offline folder is set")
    installed = next((s for s in statuses if s.id in CONTAINER_RUNTIMES and s.state == INSTALLED), None)
    rec = installed.id if installed else recommended_runtime(cfg)
    if rec in cfg.blocked:
        rec = next((r for r in CONTAINER_RUNTIMES if r not in cfg.blocked), "")
    if not rec:
        return Decision("skip", "all container runtimes are blocked by policy")
    action = "prompt"
    if cfg.mode == "auto" and not BY_ID[rec].license_ack and not installed:
        action = "auto"          # never auto for licence-gated products unless accepted earlier
    if cfg.mode == "auto" and BY_ID[rec].license_ack and rec in state.get("license_accepted", []):
        action = "auto"
    return Decision(action, f"no ready container runtime ({'installed but not running' if installed else 'none installed'})", [rec], rec)
