"""Claude Code CLI backend (uses the user's Claude subscription via `claude -p`).

Invocation:  claude -p --output-format json --max-turns 1 [--system-prompt S] [--model M]
The prompt is fed on stdin. The CLI prints one JSON object with "result", "usage",
"total_cost_usd" and "modelUsage".
"""
from __future__ import annotations

import json
import shutil
import subprocess

from jobbot.llm.base import LLMResult, Provider, register

TIMEOUT_S = 180
NOT_FOUND_MSG = "claude CLI not found on PATH — install Claude Code and run `claude login`"


def _to_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _extract_json(raw: str) -> dict:
    """Parse the CLI's JSON output, tolerating leading noise on stdout."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        if start < 0:
            return {"result": raw}
        try:
            data = json.loads(raw[start:])
        except json.JSONDecodeError:
            return {"result": raw}
    if isinstance(data, list):  # stream-style array: take the result event if present
        for ev in reversed(data):
            if isinstance(ev, dict) and ev.get("type") == "result":
                return ev
        return data[-1] if data and isinstance(data[-1], dict) else {}
    return data if isinstance(data, dict) else {"result": str(data)}


def _parse(raw: str, requested_model: str = "") -> LLMResult:
    data = _extract_json(raw)
    text = data.get("result")
    if not isinstance(text, str):
        text = "" if text is None else str(text)

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    estimated = False
    if usage and ("input_tokens" in usage or "output_tokens" in usage):
        in_tok = (_to_int(usage.get("input_tokens"))
                  + _to_int(usage.get("cache_read_input_tokens"))
                  + _to_int(usage.get("cache_creation_input_tokens")))
        out_tok = _to_int(usage.get("output_tokens"))
    else:
        in_tok = 0
        out_tok = len(text) // 4
        estimated = True

    cost_raw = data.get("total_cost_usd", data.get("cost_usd"))
    try:
        cost_usd = float(cost_raw) if cost_raw is not None else 0.0
    except (TypeError, ValueError):
        cost_usd = 0.0

    model = ""
    mu = data.get("modelUsage")
    if isinstance(mu, dict) and mu:
        # pick the model that produced the most output tokens (the "main" model)
        model = max(mu, key=lambda k: _to_int((mu.get(k) or {}).get("outputTokens")) if isinstance(mu.get(k), dict) else 0)
    model = model or data.get("model") or requested_model or "claude-code"

    return LLMResult(text=text, input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost_usd,
                     model=str(model), estimated=estimated)


@register
class ClaudeCodeProvider(Provider):
    name = "claude_code"
    default_model = ""  # let the CLI pick

    def is_configured(self) -> tuple[bool, str]:
        if shutil.which("claude") is None:
            return False, NOT_FOUND_MSG
        return True, "ok"

    def run(self, prompt: str, system: str | None, max_tokens: int, model: str) -> LLMResult:
        cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "1"]
        if system:
            cmd += ["--system-prompt", system]
        if model:
            cmd += ["--model", model]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude CLI timed out after {TIMEOUT_S}s") from e
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
        res = _parse(proc.stdout, model)
        data = _extract_json(proc.stdout)
        if data.get("is_error") and not res.text:
            raise RuntimeError(f"claude CLI error: {data.get('subtype') or proc.stderr.strip()[:500]}")
        return res
