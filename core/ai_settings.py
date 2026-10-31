"""Load/save AI settings (Phase 7) and build a ModelRouter. API keys are never read from or written to the file."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from providers.base_provider import ProviderConfig
from providers.model_router import ModelRouter, RouterLimits
from providers.ollama_provider import OllamaProvider
from providers.openai_compatible_provider import OpenAICompatibleProvider
from providers.openrouter_provider import OpenRouterProvider
from security.consent_policy import ConsentLedger

from core.runtime_paths import data_root as _data_root, resource_root as _resource_root   # noqa: E402  (same folders as before when run from source)

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = _resource_root() / "config" / "settings.json"          # shipped defaults (shared with Phase 9 settings)
USER_AI_SETTINGS_PATH = _data_root() / "data" / "ai_settings.json"         # user changes; never contains API keys
PROVIDER_CLASSES = {"ollama": OllamaProvider, "openai_compatible": OpenAICompatibleProvider, "openrouter": OpenRouterProvider}
_SECRETISH = re.compile(r"(?i)(api[_-]?key$|secret|token|password|authorization)")
_ALLOWED = set(ProviderConfig().to_dict())

DEFAULT_PROVIDERS = [
    {"name": "ollama", "kind": "ollama", "enabled": True, "base_url": "http://localhost:11434", "model": "",
     "fallback_models": [], "timeout_seconds": 120, "max_retries": 1, "max_tokens": 1024, "api_key_env": ""},
    {"name": "openai_compatible", "kind": "openai_compatible", "enabled": False, "base_url": "https://api.openai.com/v1",
     "model": "", "fallback_models": [], "timeout_seconds": 60, "max_retries": 2, "max_tokens": 1024,
     "api_key_env": "OPENAI_API_KEY"},
    {"name": "openrouter", "kind": "openrouter", "enabled": False, "base_url": "https://openrouter.ai/api/v1",
     "model": "", "fallback_models": [], "timeout_seconds": 60, "max_retries": 2, "max_tokens": 1024,
     "api_key_env": "OPENROUTER_API_KEY"}]

DEFAULTS: Dict[str, Any] = {"version": 1, "ai": {
    "enabled": False,
    "auto_detect_local": True,          # find Ollama / LM Studio and configure them (localhost only)
    "auto_start_local": True,           # start an INSTALLED but stopped local server
    "fetch_cloud_models": True,         # refresh the cloud model catalogue in the background (lists only, no project data)
    "limits": {"max_requests": 50, "max_total_tokens": 200000, "max_prompt_chars": 120000,
               "max_item_bytes": 24000, "max_total_bytes": 80000},
    "task_routes": {"default": ["ollama", "openai_compatible", "openrouter"]},
    "providers": []}}


def _read(p: Path) -> Dict[str, Any]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    """Explicit ``path``: only that file. No path: shipped defaults, then the user's overrides (data/ai_settings.json)."""
    if path is not None:
        data = _read(Path(path))
    else:
        data = _read(DEFAULT_SETTINGS_PATH)
        user = _read(USER_AI_SETTINGS_PATH)
        if isinstance(user.get("ai"), dict):
            data = {**data, "ai": user["ai"]}
    merged = copy.deepcopy(DEFAULTS)
    ai = data.get("ai") if isinstance(data.get("ai"), dict) else {}
    merged["ai"]["enabled"] = bool(ai.get("enabled", False))
    for flag in ("auto_detect_local", "auto_start_local", "fetch_cloud_models"):
        merged["ai"][flag] = bool(ai.get(flag, True))
    if isinstance(ai.get("limits"), dict):
        merged["ai"]["limits"].update({k: int(v) for k, v in ai["limits"].items() if isinstance(v, (int, float))})
    if isinstance(ai.get("task_routes"), dict):
        merged["ai"]["task_routes"] = {str(k): [str(x) for x in v] for k, v in ai["task_routes"].items() if isinstance(v, list)}
    provs = [sanitize_provider(x) for x in ai.get("providers", []) if isinstance(x, dict)]
    merged["ai"]["providers"] = provs or [copy.deepcopy(d) for d in DEFAULT_PROVIDERS]   # missing/empty file -> defaults
    return merged


def sanitize_provider(d: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only known, non-secret fields. A literal key placed in the file is dropped, not kept."""
    out = {k: v for k, v in d.items() if k in _ALLOWED and not (_SECRETISH.search(k) and k != "api_key_env")}
    env = str(out.get("api_key_env", ""))
    if env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", env):
        out["api_key_env"] = ""               # looks like a pasted key, not an env var name
    for k in ("timeout_seconds", "max_retries", "max_tokens"):
        if k in out:
            out[k] = max(0, int(out[k]))
    if isinstance(out.get("fallback_models"), list):
        out["fallback_models"] = [str(m) for m in out["fallback_models"] if str(m).strip()]
    return out


def save_settings(settings: Dict[str, Any], path: Optional[Path] = None) -> Path:
    p = Path(path or USER_AI_SETTINGS_PATH)
    clean = copy.deepcopy(settings)
    clean["ai"]["providers"] = [sanitize_provider(x) for x in clean["ai"].get("providers", [])]
    clean["note"] = "API keys are never stored here. 'api_key_env' is the NAME of an environment variable."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return p


def build_router(settings: Optional[Dict[str, Any]] = None, transport=None, ledger_path: Optional[Path] = None,
                 sleep=None) -> ModelRouter:
    s = (settings or load_settings())["ai"]
    kw: Dict[str, Any] = {}
    if transport is not None:
        kw["transport"] = transport
    if sleep is not None:
        kw["sleep"] = sleep
    providers = []
    if s.get("enabled"):
        for d in s["providers"]:
            cls = PROVIDER_CLASSES.get(d.get("kind", ""))
            if cls is None:
                continue
            fields = {k: v for k, v in d.items() if k in _ALLOWED}
            providers.append(cls(ProviderConfig(**fields), **kw))
    lim = s["limits"]
    return ModelRouter(providers, RouterLimits(lim["max_requests"], lim["max_total_tokens"], lim["max_prompt_chars"]),
                       s.get("task_routes"), ConsentLedger(ledger_path))
