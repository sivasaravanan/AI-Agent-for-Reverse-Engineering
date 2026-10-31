"""Model router (Phase 7): provider selection, fallback models, request/token limits, consent enforcement."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from providers.base_provider import (BaseProvider, GenerationResult, ProviderAuthError, ProviderError,
                                     safe_error)
from security.consent_policy import ConsentGrant, ConsentLedger, PayloadPreview, authorize

log = logging.getLogger("ase.router")


class RouterError(Exception):
    pass


class RequestLimitExceeded(RouterError):
    pass


class NoProviderAvailable(RouterError):
    pass


@dataclass
class RouterLimits:
    max_requests: int = 50              # model calls per session (each fallback attempt counts)
    max_total_tokens: int = 200_000     # sum of prompt+completion tokens (estimated when the API reports none)
    max_prompt_chars: int = 120_000


@dataclass
class RoutedResult:
    result: GenerationResult
    skipped: List[str] = field(default_factory=list)   # providers/models skipped and why (secret-safe)


class ModelRouter:
    def __init__(self, providers: Optional[Sequence[BaseProvider]] = None, limits: Optional[RouterLimits] = None,
                 task_routes: Optional[Dict[str, List[str]]] = None, ledger: Optional[ConsentLedger] = None):
        self.providers: List[BaseProvider] = list(providers or [])
        self.limits = limits or RouterLimits()
        self.task_routes = dict(task_routes or {})
        self.ledger = ledger or ConsentLedger()
        self.requests_made = 0
        self.tokens_used = 0
        self._lock = threading.Lock()

    # -- selection (no network)
    def ordered(self, task: str = "") -> List[BaseProvider]:
        enabled = [p for p in self.providers if p.config.enabled]
        route = self.task_routes.get(task) or self.task_routes.get("default")
        if route:
            by = {p.name: p for p in enabled}
            return [by[n] for n in route if n in by]
        return enabled

    def select(self, task: str = "") -> Optional[BaseProvider]:
        """First enabled provider that is usable without further setup, else None (AI is optional, not an error)."""
        for p in self.ordered(task):
            if p.config.model and (p.is_local or p.api_key() or p.kind == "ollama"):
                return p
        return None

    def external_hosts(self, task: str = "") -> List[str]:
        return sorted({p.config.host for p in self.ordered(task) if not p.is_local and p.config.model})

    def check_all(self) -> Dict[str, tuple]:
        return {p.name: p.check() for p in self.providers if p.config.enabled}

    # -- limits
    @staticmethod
    def _estimate_tokens(*texts: str) -> int:
        return sum(len(t) for t in texts) // 4 + 1

    def _reserve(self, prompt_len: int) -> None:
        with self._lock:
            if self.requests_made >= self.limits.max_requests:
                raise RequestLimitExceeded(f"Request limit reached ({self.limits.max_requests} per session).")
            if self.tokens_used >= self.limits.max_total_tokens:
                raise RequestLimitExceeded(f"Token budget used up ({self.limits.max_total_tokens}).")
            if prompt_len > self.limits.max_prompt_chars:
                raise RequestLimitExceeded(f"Prompt is too large ({prompt_len:,} characters; limit "
                                           f"{self.limits.max_prompt_chars:,}). Select fewer or smaller files.")
            self.requests_made += 1

    def generate(self, task: str, prompt: str, system: str = "", preview: Optional[PayloadPreview] = None,
                 grant: Optional[ConsentGrant] = None, max_tokens: Optional[int] = None) -> RoutedResult:
        """Try each provider (and its fallback models) in order. Consent is enforced PER provider."""
        preview = preview or PayloadPreview()
        skipped: List[str] = []
        chain = self.ordered(task)
        if not chain:
            raise NoProviderAvailable("No AI provider is enabled. The application works without AI; enable one in AI settings.")
        for prov in chain:
            ok, why = authorize(prov.is_local, prov.config.host, preview, grant)
            if not ok:
                skipped.append(f"{prov.name}: {why}")
                continue
            models = [m for m in [prov.config.model, *prov.config.fallback_models] if m]
            if not models:
                skipped.append(f"{prov.name}: no model selected.")
                continue
            for idx, mdl in enumerate(models):
                self._reserve(len(prompt) + len(system))
                try:
                    res = prov.complete(prompt, system, mdl, max_tokens)
                except ProviderAuthError as exc:
                    skipped.append(str(exc))
                    break                                  # a different model will not fix bad credentials
                except ProviderError as exc:
                    skipped.append(f"{prov.name}/{mdl}: {safe_error(exc)}")
                    continue
                used = ((res.prompt_tokens or 0) + (res.completion_tokens or 0)) or self._estimate_tokens(prompt, system, res.text)
                with self._lock:
                    self.tokens_used += used
                res.fallback_used = idx > 0 or prov is not chain[0]
                res.errors = list(skipped)
                if preview.items:
                    self.ledger.record(provider=prov.name, host=prov.config.host, model=mdl, task=task,
                                       local=prov.is_local, preview=preview)
                return RoutedResult(result=res, skipped=skipped)
        raise NoProviderAvailable("No provider could complete the request: " + " | ".join(skipped)[:600])
