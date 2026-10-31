"""OpenRouter provider (OpenAI-compatible API with its own base URL and optional attribution headers)."""
from __future__ import annotations

from typing import Optional

from providers.base_provider import ProviderConfig
from providers.openai_compatible_provider import OpenAICompatibleProvider

DEFAULT_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAICompatibleProvider):
    kind = "openrouter"

    def __init__(self, config: Optional[ProviderConfig] = None, **kw):
        cfg = config or ProviderConfig()
        cfg.kind = "openrouter"
        cfg.base_url = cfg.base_url or DEFAULT_URL
        cfg.name = cfg.name or "openrouter"
        cfg.api_key_env = cfg.api_key_env or "OPENROUTER_API_KEY"
        cfg.extra_headers.setdefault("X-Title", "AI Software Engineering Agent")
        super().__init__(cfg, **kw)
