"""Institution-aware configuration for the prerequisite installer.

Precedence (later wins): built-in defaults < user file (data/prerequisites.json) < shipped policy (config/institution_policy.json)
< machine policy (%ProgramData%\\AISoftwareEngineeringAgent\\policy.json, /etc/aisea/policy.json). Keys that appear in a policy
file are *locked*: the UI shows them read-only. Invalid values are ignored with a warning; loading never raises.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.runtime_paths import data_root, machine_policy_path, resource_root

MODES = ("ask", "auto", "off")
RUNTIMES = ("auto", "podman", "docker")

DEFAULTS: Dict[str, Any] = {
    "mode": "ask",                  # ask = dialog at start-up | auto = install pre-selected items without a dialog | off = never prompt
    "startup_check": True,
    "snooze_days": 7,
    "prefer_runtime": "auto",       # auto = Podman first (free for organisations); Docker Desktop needs a licence for larger organisations
    "prefer_user_scope": True,      # per-user installs that need no administrator rights, when the product supports them
    "allow_downloads": True,
    "allow_installs": True,
    "allow_winget": True,
    "require_verification": True,   # refuse installers that are neither hash-checked nor validly signed by the expected publisher
    "proxy": "",                    # http://host:port  (credentials in the URL are stored as plain text - prefer env/system proxy)
    "no_proxy": "",
    "ca_bundle": "",                # PEM file with your institution's root CA (TLS inspection)
    "offline_dir": "",              # folder with pre-downloaded installers (+ SHA256SUMS)
    "mirror_base_url": "",          # internal mirror for installer downloads (Artifactory/Nexus/file share over https)
    "pip_index_url": "",
    "npm_registry": "",
    "registry_mirror": "",          # container registry prefix for sandbox images, e.g. artifactory.example.edu/docker
    "allowed_hosts": [],
    "blocked": [],                  # prerequisite ids that must never be offered
    "timeout_seconds": 60,
    "max_download_mb": 2048,
}
_BOOL = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
_STR = {"proxy", "no_proxy", "ca_bundle", "offline_dir", "mirror_base_url", "pip_index_url", "npm_registry", "registry_mirror"}
_LISTS = {"allowed_hosts", "blocked"}
_INT = {"snooze_days": (0, 365), "timeout_seconds": (5, 600), "max_download_mb": (1, 20480)}


@dataclass
class PrereqConfig:
    values: Dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULTS)))
    locked: Set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["values"][name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_dict(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self.values))


def _merge(values: Dict[str, Any], data: Any, origin: str, warnings: List[str]) -> Set[str]:
    applied: Set[str] = set()
    if not isinstance(data, dict):
        warnings.append(f"{origin}: not a JSON object; ignored.")
        return applied
    for key, val in data.items():
        if key not in DEFAULTS:
            continue
        if key in _BOOL and isinstance(val, bool):
            values[key] = val
        elif key in _STR and isinstance(val, str) and "\x00" not in val and len(val) < 1000:
            values[key] = val.strip()
        elif key in _LISTS and isinstance(val, list) and all(isinstance(x, str) for x in val):
            values[key] = [x.strip() for x in val if x.strip()]
        elif key in _INT and isinstance(val, int) and not isinstance(val, bool):
            lo, hi = _INT[key]
            values[key] = max(lo, min(hi, val))
        elif key == "mode" and val in MODES:
            values[key] = val
        elif key == "prefer_runtime" and val in RUNTIMES:
            values[key] = val
        else:
            warnings.append(f"{origin}: invalid value for '{key}'; ignored.")
            continue
        applied.add(key)
    return applied


def _read(path: Path, warnings: List[str]) -> Optional[Any]:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        warnings.append(f"{path}: unreadable ({type(exc).__name__}); ignored.")
        return None


def user_config_path(root: Optional[Path] = None) -> Path:
    return (root or data_root()) / "data" / "prerequisites.json"


def load_config(user_path: Optional[Path] = None, shipped_path: Optional[Path] = None,
                machine_path: Optional[Path] = None) -> PrereqConfig:
    cfg = PrereqConfig()
    for label, path in (("user settings", user_path or user_config_path()),
                        ("institution policy", shipped_path or resource_root() / "config" / "institution_policy.json"),
                        ("machine policy", machine_path or machine_policy_path())):
        data = _read(Path(path), cfg.warnings)
        if data is None:
            continue
        applied = _merge(cfg.values, data, label, cfg.warnings)
        cfg.sources.append(f"{label}: {path}")
        if label != "user settings":
            cfg.locked |= applied
    return cfg


def save_user_config(updates: Dict[str, Any], user_path: Optional[Path] = None, cfg: Optional[PrereqConfig] = None) -> List[str]:
    """Persist ``updates`` (unlocked keys only) to the user file. Returns warnings (e.g. ignored locked keys)."""
    path = Path(user_path or user_config_path())
    warnings: List[str] = []
    locked = (cfg.locked if cfg else set())
    current: Dict[str, Any] = {}
    existing = _read(path, warnings)
    if isinstance(existing, dict):
        current = {k: v for k, v in existing.items() if k in DEFAULTS}
    probe: Dict[str, Any] = {}
    for k, v in updates.items():
        if k in locked:
            warnings.append(f"'{k}' is set by your institution's policy and cannot be changed here.")
            continue
        probe[k] = v
    ok_values = dict(DEFAULTS)
    applied = _merge(ok_values, probe, "update", warnings)
    for k in applied:
        current[k] = ok_values[k]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return warnings
