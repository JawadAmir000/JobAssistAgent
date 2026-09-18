"""Provider interface + dispatcher. Backends live in anthropic_api.py, claude_code.py, codex.py.

CONTRACT — implement in each backend:

    class XProvider(Provider):
        name = "anthropic" | "claude_code" | "codex"
        default_model = "..."
        def is_configured(self) -> tuple[bool, str]      # (ok, human message e.g. "API key missing")
        def run(self, prompt, system, max_tokens, model) -> LLMResult

LLMResult must carry real token counts. If a backend cannot report exact tokens (Codex),
estimate with len(text)//4 and set estimated=True so the UI can mark it "~".
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from jobbot import config, db
from jobbot.models import LLMCall


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str
    estimated: bool = False


class Provider:
    name: str = "base"
    default_model: str = ""

    def is_configured(self) -> tuple[bool, str]:
        raise NotImplementedError

    def run(self, prompt: str, system: str | None, max_tokens: int, model: str) -> LLMResult:
        raise NotImplementedError


_REGISTRY: dict[str, type[Provider]] = {}
_LOADED = False


def register(cls: type[Provider]) -> type[Provider]:
    _REGISTRY[cls.name] = cls
    return cls


def available_providers() -> dict[str, type[Provider]]:
    _ensure_loaded()
    return dict(_REGISTRY)


def _ensure_loaded() -> None:
    global _LOADED
    if not _LOADED:
        _LOADED = True
        from jobbot.llm import anthropic_api, claude_code, codex  # noqa: F401


def get_provider(name: str | None = None) -> Provider:
    _ensure_loaded()
    name = name or config.get_setting(config.SETTING_PROVIDER) or "anthropic"
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"unknown provider {name!r}; known: {sorted(_REGISTRY)}")
    return cls()


def complete(prompt: str, *, purpose: str, job_id: str | None = None, system: str | None = None,
             max_tokens: int = 400, provider: str | None = None, model: str | None = None) -> str:
    p = get_provider(provider)
    ok, msg = p.is_configured()
    if not ok:
        raise RuntimeError(f"LLM provider '{p.name}' not configured: {msg}")
    model = model or (config.get_setting(config.SETTING_MODEL) or "") or p.default_model
    t0 = time.time()
    res = p.run(prompt, system, max_tokens, model)
    db.record_llm_call(LLMCall(
        provider=p.name, model=res.model, purpose=purpose,
        input_tokens=res.input_tokens, output_tokens=res.output_tokens, cost_usd=res.cost_usd,
        job_id=job_id, duration_ms=int((time.time() - t0) * 1000),
    ))
    return res.text
