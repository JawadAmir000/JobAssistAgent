"""Anthropic Messages API backend (pay-per-token, exact usage from the response)."""
from __future__ import annotations

from jobbot import config
from jobbot.llm.base import LLMResult, Provider, register
from jobbot.llm.prices import cost

KEY_NAME = "ANTHROPIC_API_KEY"


@register
class AnthropicProvider(Provider):
    name = "anthropic"
    default_model = "claude-haiku-4-5"

    def is_configured(self) -> tuple[bool, str]:
        if not config.get_secret(KEY_NAME):
            return False, f"{KEY_NAME} not set (Settings → Secrets, or env var)"
        return True, "ok"

    def run(self, prompt: str, system: str | None, max_tokens: int, model: str) -> LLMResult:
        import anthropic

        client = anthropic.Anthropic(api_key=config.get_secret(KEY_NAME))
        model = model or self.default_model
        kwargs = dict(model=model, max_tokens=max_tokens,
                      messages=[{"role": "user", "content": prompt}])
        if system:
            kwargs["system"] = system
        resp = client.messages.create(**kwargs)

        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        usage = getattr(resp, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        used_model = getattr(resp, "model", None) or model
        usd, estimated = cost(used_model, in_tok, out_tok)
        return LLMResult(text=text, input_tokens=in_tok, output_tokens=out_tok, cost_usd=usd,
                         model=used_model, estimated=estimated)
