"""OpenAI-compatible chat-completions provider (OpenAI, LM Studio, vLLM, llama.cpp server, Azure-style gateways...)."""
from __future__ import annotations

from typing import Dict, List, Optional

from providers.base_provider import (BaseProvider, GenerationResult, ProviderAuthError, ProviderConfig,
                                     ProviderResponseError, ProviderError)

DEFAULT_URL = "https://api.openai.com/v1"


class OpenAICompatibleProvider(BaseProvider):
    kind = "openai_compatible"
    requires_key_when_remote = True

    def __init__(self, config: Optional[ProviderConfig] = None, **kw):
        cfg = config or ProviderConfig()
        cfg.kind = cfg.kind or self.kind
        cfg.base_url = cfg.base_url or DEFAULT_URL
        cfg.name = cfg.name or self.kind
        super().__init__(cfg, **kw)

    def _headers(self) -> Dict[str, str]:
        h = super()._headers()
        key = self.api_key()
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _need_key(self) -> None:
        if self.requires_key_when_remote and not self.is_local and not self.api_key():
            raise ProviderAuthError(f"{self.name}: no API key. Set the environment variable "
                                    f"'{self.config.api_key_env or 'ASE_API_KEY'}' or enter a session key.")

    def list_models(self) -> List[str]:
        self._need_key()
        data, _ = self._request("GET", "/models", timeout=min(15, self.config.timeout_seconds), retries=0)
        return [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]

    def is_available(self) -> bool:
        try:
            self.list_models()
            return True
        except ProviderError:
            return False

    def complete(self, prompt: str, system: str = "", model: str = "", max_tokens: Optional[int] = None) -> GenerationResult:
        self._need_key()
        mdl = model or self.config.model
        if not mdl:
            raise ProviderResponseError(f"{self.name}: no model selected.")
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        payload = {"model": mdl, "messages": msgs, "max_tokens": int(max_tokens or self.config.max_tokens),
                   "temperature": 0.2, "stream": False}
        data, attempts = self._request("POST", "/chat/completions", payload)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(f"{self.name}: unexpected response shape.") from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderResponseError(f"{self.name}: empty response.")
        usage = data.get("usage") or {}
        fin = str(choice.get("finish_reason") or "")
        return GenerationResult(text=text, provider=self.name, model=mdl, attempts=attempts,
                                prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                                finish_reason=fin, truncated=fin == "length")
