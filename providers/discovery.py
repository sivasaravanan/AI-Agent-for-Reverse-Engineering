"""Local AI auto-configuration (Ollama, LM Studio) and a live model catalogue for cloud providers.

Nothing here sends project data anywhere. Local detection talks to localhost only. Catalogue requests ask a provider for its
public model LIST; where the provider needs an API key to list models, the key (from the environment variable or the
session-only value) is sent to that provider's own host and nowhere else. Keys are never stored or logged.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from providers.base_provider import ProviderError, Transport, default_transport, safe_error

from core.runtime_paths import data_root as _data_root

CATALOG_PATH = _data_root() / "data" / "model_catalog.json"
CACHE_MAX_AGE_S = 24 * 3600

# ------------------------------------------------------------------ local servers
LOCAL_SERVERS = {
    "ollama": {"name": "ollama", "kind": "ollama", "label": "Ollama", "base_url": "http://localhost:11434", "probe": "/api/tags",
               "binary": "ollama", "start": ["ollama", "serve"], "env_url": "OLLAMA_HOST", "api_key_env": ""},
    "lmstudio": {"name": "lmstudio", "kind": "openai_compatible", "label": "LM Studio", "base_url": "http://localhost:1234/v1",
                 "probe": "/models", "binary": "lms", "start": ["lms", "server", "start"], "env_url": "", "api_key_env": ""},
}


@dataclass
class LocalServer:
    key: str
    label: str
    base_url: str
    running: bool = False
    installed: bool = False
    models: List[str] = field(default_factory=list)
    started_by_us: bool = False
    note: str = ""


def _url_from_env(spec: Dict[str, Any]) -> str:
    raw = os.environ.get(spec.get("env_url") or "", "").strip() if spec.get("env_url") else ""
    if not raw:
        return spec["base_url"]
    if "://" not in raw:
        raw = "http://" + raw
    if raw.count(":") < 2:                      # scheme only + host, no port
        raw += ":11434"
    host = raw.split("://", 1)[1].split(":")[0].split("/")[0]
    if host in ("0.0.0.0", ""):
        raw = raw.replace(host, "localhost", 1) if host else raw
    return raw.rstrip("/")


def _probe(spec: Dict[str, Any], url: str, transport: Transport, timeout: float = 2.0) -> Tuple[bool, List[str]]:
    try:
        status, body = transport("GET", url.rstrip("/") + spec["probe"], {"Accept": "application/json"}, None, timeout)
    except ProviderError:
        return False, []
    except Exception:  # noqa: BLE001 - detection must never raise
        return False, []
    if status != 200:
        return False, []
    try:
        data = json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError:
        return True, []
    if spec["kind"] == "ollama":
        return True, sorted(m.get("name", "") for m in data.get("models", []) if isinstance(m, dict) and m.get("name"))
    return True, sorted(m.get("id", "") for m in data.get("data", []) if isinstance(m, dict) and m.get("id"))


def detect_local(transport: Optional[Transport] = None, auto_start: bool = False, which: Callable[[str], Optional[str]] = shutil.which,
                 popen: Callable[..., Any] = subprocess.Popen, wait_s: float = 12.0, sleep: Callable[[float], None] = time.sleep,
                 ) -> List[LocalServer]:
    """Find Ollama and LM Studio on this computer. With ``auto_start`` a stopped but INSTALLED server is started (local only)."""
    transport = transport or default_transport
    found: List[LocalServer] = []
    for key, spec in LOCAL_SERVERS.items():
        url = _url_from_env(spec)
        exe = which(spec["binary"])
        srv = LocalServer(key=key, label=spec["label"], base_url=url, installed=bool(exe))
        srv.running, srv.models = _probe(spec, url, transport)
        if not srv.running and auto_start and exe:
            try:
                kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
                if os.name == "nt":
                    kw["creationflags"] = 0x08000000 | 0x00000008          # no window, detached
                else:
                    kw["start_new_session"] = True
                popen([exe, *spec["start"][1:]], **kw)
                srv.started_by_us = True
                waited = 0.0
                while waited < wait_s and not srv.running:
                    sleep(0.5)
                    waited += 0.5
                    srv.running, srv.models = _probe(spec, url, transport)
            except OSError as exc:
                srv.note = f"Could not start {srv.label}: {type(exc).__name__}"
        if srv.running and not srv.models:
            srv.note = (srv.note + " " if srv.note else "") + (
                f"{srv.label} is running but has no model yet. " + ("Run: ollama pull llama3.2" if key == "ollama"
                                                                     else "Download a model in LM Studio and load it."))
        elif not srv.running and not srv.note:
            srv.note = (f"{srv.label} is installed but not running." if srv.installed else f"{srv.label} was not found on this computer.")
        found.append(srv)
    return found


def apply_local(settings: Dict[str, Any], servers: List[LocalServer]) -> List[str]:
    """Configure the local providers in ``settings`` (in place) for every running server; returns a human-readable log.

    Adds/updates the provider entry, selects the first model when none is chosen (or the chosen one is gone), puts local
    providers first in the default route. The master 'AI enabled' switch is never touched.
    """
    ai = settings.setdefault("ai", {})
    provs: List[Dict[str, Any]] = ai.setdefault("providers", [])
    log: List[str] = []
    for srv in servers:
        spec = LOCAL_SERVERS[srv.key]
        entry = next((p for p in provs if p.get("name") == spec["name"]), None)
        if not srv.running:
            continue
        if entry is None:
            entry = {"name": spec["name"], "kind": spec["kind"], "enabled": True, "base_url": srv.base_url, "model": "",
                     "fallback_models": [], "timeout_seconds": 180, "max_retries": 1, "max_tokens": 1024, "api_key_env": ""}
            provs.append(entry)
            log.append(f"{srv.label}: added.")
        entry["enabled"] = True
        if not entry.get("base_url"):
            entry["base_url"] = srv.base_url
        if srv.models:
            if entry.get("model") not in srv.models:
                entry["model"] = srv.models[0]
                log.append(f"{srv.label}: model set to {entry['model']}.")
            entry["fallback_models"] = [m for m in srv.models if m != entry["model"]][:3]
        log.append(f"{srv.label}: running with {len(srv.models)} model(s).")
    routes = ai.setdefault("task_routes", {})
    default = [str(x) for x in routes.get("default", [])]
    local_names = [LOCAL_SERVERS[s.key]["name"] for s in servers if s.running]
    routes["default"] = local_names + [n for n in default if n not in local_names]
    return log


# ------------------------------------------------------------------ cloud catalogue
@dataclass
class CloudSpec:
    key: str
    label: str
    base_url: str
    api_key_env: str
    auth: str = "bearer"                 # bearer | x-api-key | query
    kind: str = "openai_compatible"
    models_path: str = "/models"
    public_list: bool = False            # model list needs no key
    free_tier: bool = False              # the provider has a free plan (limits apply) - not the same as per-model free pricing
    extra_headers: Dict[str, str] = field(default_factory=dict)


CLOUD_PROVIDERS: List[CloudSpec] = [
    CloudSpec("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", kind="openrouter", public_list=True, free_tier=True),
    CloudSpec("openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    CloudSpec("anthropic", "Anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY", auth="x-api-key",
              extra_headers={"anthropic-version": "2023-06-01"}),
    CloudSpec("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", free_tier=True),
    CloudSpec("groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", free_tier=True),
    CloudSpec("mistral", "Mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", free_tier=True),
    CloudSpec("together", "Together AI", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    CloudSpec("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    CloudSpec("xai", "xAI", "https://api.x.ai/v1", "XAI_API_KEY"),
    CloudSpec("fireworks", "Fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
    CloudSpec("cerebras", "Cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", free_tier=True),
]
SPEC_BY_KEY = {c.key: c for c in CLOUD_PROVIDERS}


@dataclass
class ModelInfo:
    provider: str
    id: str
    name: str = ""
    cost: str = "unknown"                # free | paid | unknown  (known per model only where the provider publishes prices)
    prompt_price: Optional[float] = None  # USD per 1M input tokens when published
    completion_price: Optional[float] = None
    context: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _price_per_m(v: Any) -> Optional[float]:
    try:
        return round(float(v) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def parse_models(spec: CloudSpec, data: Dict[str, Any]) -> List[ModelInfo]:
    out: List[ModelInfo] = []
    rows = data.get("data") if isinstance(data.get("data"), list) else data.get("models", [])
    for m in rows if isinstance(rows, list) else []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or m.get("name") or "")
        if mid.startswith("models/"):
            mid = mid[len("models/"):]
        if not mid:
            continue
        info = ModelInfo(provider=spec.key, id=mid, name=str(m.get("name") or m.get("display_name") or mid))
        pr = m.get("pricing")
        if isinstance(pr, dict):
            info.prompt_price, info.completion_price = _price_per_m(pr.get("prompt")), _price_per_m(pr.get("completion"))
            if info.prompt_price is not None and info.completion_price is not None:
                info.cost = "free" if info.prompt_price == 0 and info.completion_price == 0 else "paid"
        elif mid.endswith(":free"):
            info.cost = "free"
        ctx = m.get("context_length") or m.get("context_window") or m.get("inputTokenLimit")
        info.context = int(ctx) if isinstance(ctx, (int, float)) else None
        out.append(info)
    return sorted(out, key=lambda x: x.id.lower())


def fetch_models(spec: CloudSpec, api_key: str = "", transport: Optional[Transport] = None, timeout: float = 20.0) -> List[ModelInfo]:
    """One provider's live model list. Raises ProviderError (secret-safe message) when it cannot be fetched."""
    transport = transport or default_transport
    if not spec.public_list and not api_key:
        raise ProviderError(f"{spec.label}: set {spec.api_key_env} (or enter a session key) to list its models.")
    headers = {"Accept": "application/json", "User-Agent": "ASE-Agent/0.9", **spec.extra_headers}
    url = spec.base_url.rstrip("/") + spec.models_path
    if api_key:
        if spec.auth == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        elif spec.auth == "x-api-key":
            headers["x-api-key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"
    status, body = transport("GET", url, headers, None, timeout)
    if status in (401, 403):
        raise ProviderError(f"{spec.label}: the key was rejected (HTTP {status}).")
    if status >= 400:
        raise ProviderError(f"{spec.label}: HTTP {status} {safe_error(body.decode('utf-8', 'replace'), (api_key,))[:120]}")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ProviderError(f"{spec.label}: the answer was not JSON.") from exc
    return parse_models(spec, data if isinstance(data, dict) else {"data": data})


class ModelCatalog:
    """Live model lists of all cloud providers with an on-disk cache (used when offline). Holds no keys."""

    def __init__(self, path: Optional[Path] = None, transport: Optional[Transport] = None,
                 key_for: Optional[Callable[[CloudSpec], str]] = None, clock: Callable[[], float] = time.time):
        self.path = Path(path or CATALOG_PATH)
        self.transport = transport
        self.key_for = key_for or (lambda spec: os.environ.get(spec.api_key_env, "").strip())
        self.clock = clock
        self.models: Dict[str, List[ModelInfo]] = {}
        self.errors: Dict[str, str] = {}
        self.fetched_at: Dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for k, rows in (data.get("models") or {}).items():
                self.models[k] = [ModelInfo(**r) for r in rows if isinstance(r, dict)]
            self.fetched_at = {k: float(v) for k, v in (data.get("fetched_at") or {}).items()}
        except (OSError, ValueError, TypeError):
            self.models, self.fetched_at = {}, {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"models": {k: [m.to_dict() for m in v] for k, v in self.models.items()},
                                             "fetched_at": self.fetched_at}), encoding="utf-8")
        except OSError:
            pass

    def refresh(self, keys: Optional[List[str]] = None, force: bool = True) -> Dict[str, str]:
        """Fetch the chosen providers (default: all). A failure keeps that provider's cached list. Returns {key: message}."""
        report: Dict[str, str] = {}
        for spec in CLOUD_PROVIDERS:
            if keys is not None and spec.key not in keys:
                continue
            if not force and self.clock() - self.fetched_at.get(spec.key, 0) < CACHE_MAX_AGE_S and self.models.get(spec.key):
                report[spec.key] = f"cached ({len(self.models[spec.key])})"
                continue
            key = self.key_for(spec)
            try:
                got = fetch_models(spec, key, self.transport)
            except ProviderError as exc:
                self.errors[spec.key] = str(exc)
                report[spec.key] = str(exc) + (" Showing the last saved list." if self.models.get(spec.key) else "")
                continue
            except Exception as exc:  # noqa: BLE001
                self.errors[spec.key] = f"{spec.label}: {type(exc).__name__}"
                report[spec.key] = self.errors[spec.key]
                continue
            self.models[spec.key] = got
            self.fetched_at[spec.key] = self.clock()
            self.errors.pop(spec.key, None)
            report[spec.key] = f"{len(got)} model(s)"
        self.save()
        return report

    def search(self, provider: str = "", cost: str = "all", text: str = "") -> List[ModelInfo]:
        text = text.strip().lower()
        out: List[ModelInfo] = []
        for key, rows in self.models.items():
            if provider and key != provider:
                continue
            for m in rows:
                if cost in ("free", "paid") and m.cost != cost:
                    continue
                if text and text not in m.id.lower() and text not in m.name.lower():
                    continue
                out.append(m)
        return out


def apply_cloud_choice(settings: Dict[str, Any], spec: CloudSpec, model_id: str, enable: bool = True) -> Dict[str, Any]:
    """Add (or update) the provider entry for a catalogue pick and select the model. Returns the entry. Master switch untouched."""
    ai = settings.setdefault("ai", {})
    provs: List[Dict[str, Any]] = ai.setdefault("providers", [])
    entry = next((p for p in provs if p.get("name") == spec.key), None)
    if entry is None:
        entry = {"name": spec.key, "kind": spec.kind, "enabled": enable, "base_url": spec.base_url, "model": model_id,
                 "fallback_models": [], "timeout_seconds": 60, "max_retries": 2, "max_tokens": 1024,
                 "api_key_env": spec.api_key_env}
        if spec.extra_headers:
            entry["extra_headers"] = dict(spec.extra_headers)
        provs.append(entry)
    entry.update(model=model_id, enabled=enable)
    routes = ai.setdefault("task_routes", {})
    default = [str(x) for x in routes.get("default", [])]
    if spec.key not in default:
        default.append(spec.key)
    routes["default"] = default
    return entry
