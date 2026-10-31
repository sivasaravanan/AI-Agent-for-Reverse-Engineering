"""Phase 9: validated application settings (defaults file + per-user overrides).

``config/settings.json`` holds the shipped defaults; user changes are saved separately (default
``data/user_settings.json``) so the install folder can stay read-only. Invalid values are clamped or
ignored with a warning - a damaged settings file never prevents the app from starting.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from security.archive_validation import ArchiveLimits

# key -> (type, min, max)
LIMIT_BOUNDS: Dict[str, Tuple[type, float, float]] = {
    "max_files": (int, 1, 100_000),
    "max_total_uncompressed_mb": (int, 1, 10_240),
    "max_member_mb": (int, 1, 1_024),
    "max_text_file_kb": (int, 16, 16_384),
    "max_compression_ratio": (int, 2, 10_000),
    "max_scanned_files": (int, 1, 50_000),
}
DEFAULT_LIMITS = {"max_files": 20_000, "max_total_uncompressed_mb": 1024, "max_member_mb": 100,
                  "max_text_file_kb": 1024, "max_compression_ratio": 200, "max_scanned_files": 5000}


@dataclass
class Settings:
    output_dir: str = "reports"
    history_enabled: bool = True
    max_history: int = 200
    show_dashboard_after_analysis: bool = True
    limits: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LIMITS))

    def to_dict(self) -> Dict[str, Any]:
        return {"version": 1, **asdict(self)}


def _int(value: Any, lo: float, hi: float, default: int, name: str, warnings: List[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append(f"Setting '{name}' must be a number; using {default}.")
        return default
    clamped = int(min(max(value, lo), hi))
    if clamped != int(value):
        warnings.append(f"Setting '{name}' was out of range ({lo:g}-{hi:g}); using {clamped}.")
    return clamped


def validate(data: Any, base: Optional[Settings] = None) -> Tuple[Settings, List[str]]:
    """Merge ``data`` over ``base`` (or defaults). Unknown keys are ignored. Never raises."""
    s = Settings(**{**asdict(base or Settings())}) if base else Settings()
    s.limits = dict(s.limits)
    warnings: List[str] = []
    if not isinstance(data, dict):
        return s, ["Settings were not a JSON object; defaults are used."] if data is not None else []
    if "output_dir" in data:
        v = data["output_dir"]
        if isinstance(v, str) and v.strip() and "\x00" not in v and len(v) < 500:
            s.output_dir = v.strip()
        else:
            warnings.append("Setting 'output_dir' is invalid; keeping the previous value.")
    for key in ("history_enabled", "show_dashboard_after_analysis"):
        if key in data:
            if isinstance(data[key], bool):
                setattr(s, key, data[key])
            else:
                warnings.append(f"Setting '{key}' must be true or false; keeping the previous value.")
    if "max_history" in data:
        s.max_history = _int(data["max_history"], 1, 5000, s.max_history, "max_history", warnings)
    lim = data.get("limits")
    if isinstance(lim, dict):
        for key, (_t, lo, hi) in LIMIT_BOUNDS.items():
            if key in lim:
                s.limits[key] = _int(lim[key], lo, hi, s.limits[key], f"limits.{key}", warnings)
    elif lim is not None:
        warnings.append("Setting 'limits' must be an object; keeping the previous values.")
    return s, warnings


def load_settings(defaults_path: str | Path | None = None, user_path: str | Path | None = None
                  ) -> Tuple[Settings, List[str]]:
    """Defaults file first, then the user's overrides. Missing/corrupt files only produce warnings."""
    settings, warnings = Settings(), []
    for label, path in (("defaults", defaults_path), ("user settings", user_path)):
        if not path:
            continue
        p = Path(path)
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.append(f"The {label} file could not be read ({type(exc).__name__}); ignored.")
            continue
        settings, w = validate(data, settings)
        warnings += [f"{label}: {x}" for x in w]
    return settings, warnings


def save_settings(settings: Settings, user_path: str | Path) -> None:
    p = Path(user_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(p)


def to_limits(settings: Settings) -> ArchiveLimits:
    l = settings.limits
    return ArchiveLimits(
        max_files=l["max_files"], max_total_uncompressed=l["max_total_uncompressed_mb"] * 1024 ** 2,
        max_member_size=l["max_member_mb"] * 1024 ** 2, max_text_file_size=l["max_text_file_kb"] * 1024,
        max_compression_ratio=float(l["max_compression_ratio"]), max_scanned_files=l["max_scanned_files"])


def resolve_output_dir(settings: Settings, base: str | Path) -> Path:
    p = Path(settings.output_dir)
    return p if p.is_absolute() else Path(base) / p
