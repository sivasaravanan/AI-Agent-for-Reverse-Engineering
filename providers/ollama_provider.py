"""Ollama (local) provider: http://localhost:11434 by default."""
from __future__ import annotations

from typing import List, Optional

from providers.base_provider import (BaseProvider, GenerationResult, ProviderConfig, ProviderError,
                                     ProviderResponseError)

DEFAULT_URL = "http://localhost:11434"


class OllamaProvider(BaseProvider):
    kind = "ollama"

    def __init__(self, config: Optional[ProviderConfig] = None, **kw):
        cfg = config or ProviderConfig()
        cfg.kind = "ollama"
        cfg.base_url = cfg.base_url or DEFAULT_URL
        cfg.name = cfg.name or "ollama"
        super().__init__(cfg, **kw)

    def list_models(self) -> List[str]:
        data, _ = self._request("GET", "/api/tags", timeout=min(10, self.config.timeout_seconds), retries=0)
        return [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]

    def is_available(self) -> bool:
        try:
            self.list_models()
            return True
        except ProviderError:
            return False

    def complete(self, prompt: str, system: str = "", model: str = "", max_tokens: Optional[int] = None) -> GenerationResult:
        mdl = model or self.config.model
        if not mdl:
            raise ProviderResponseError("ollama: no model selected.")
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        payload = {"model": mdl, "messages": msgs, "stream": False,
                   "options": {"num_predict": int(max_tokens or self.config.max_tokens)}}
        data, attempts = self._request("POST", "/api/chat", payload)
        text = ((data.get("message") or {}).get("content")) or data.get("response") or ""
        if not isinstance(text, str) or not text.strip():
            raise ProviderResponseError("ollama: empty response.")
        return GenerationResult(text=text, provider=self.name, model=mdl, attempts=attempts,
                                prompt_tokens=data.get("prompt_eval_count"), completion_tokens=data.get("eval_count"),
                                finish_reason=str(data.get("done_reason") or ""),
                                truncated=data.get("done_reason") == "length")
