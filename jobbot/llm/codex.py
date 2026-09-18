"""OpenAI Codex CLI backend (subscription-billed, so cost_usd is 0).

Invocation:  codex exec --json --skip-git-repo-check -s read-only -
The prompt is fed on stdin ("-"), cwd is a scratch temp dir so the agent cannot touch the repo.
stdout is a JSONL event stream; we keep the last assistant message text and any usage event.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile

from jobbot.llm.base import LLMResult, Provider, register

TIMEOUT_S = 180
NOT_FOUND_MSG = "codex CLI not found on PATH — install OpenAI Codex CLI and run `codex login`"
SANDBOX_FLAGS = ["-s", "read-only"]


def _to_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _find_usage(obj) -> dict | None:
    """Recursively find a dict that looks like a token-usage block."""
    if isinstance(obj, dict):
        if "input_tokens" in obj or "output_tokens" in obj:
            return obj
        for key in ("total_token_usage", "usage", "info", "last_token_usage"):
            if key in obj:
                found = _find_usage(obj[key])
                if found:
                    return found
        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = _find_usage(v)
                if found:
                    return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_usage(v)
            if found:
                return found
    return None


def _assistant_text(ev: dict) -> str | None:
    """Return assistant text from an event if it carries one, else None."""
    # New-style: {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
    item = ev.get("item")
    if isinstance(item, dict):
        if item.get("type") == "agent_message" or item.get("role") == "assistant":
            t = item.get("text") or item.get("content") or item.get("message")
            if isinstance(t, str):
                return t
    # Old-style: {"msg":{"type":"agent_message","message":"..."}}
    msg = ev.get("msg")
    if isinstance(msg, dict) and "message" in msg.get("type", "") and msg.get("type") != "user_message":
        t = msg.get("message") or msg.get("text")
        if isinstance(t, str):
            return t
    # Generic: {"type":"...message...","role":"assistant","text"/"content":...}
    etype = str(ev.get("type", ""))
    role = ev.get("role") or (msg.get("role") if isinstance(msg, dict) else None)
    if "message" in etype and role == "assistant":
        t = ev.get("text") or ev.get("content") or ev.get("message")
        if isinstance(t, str):
            return t
        if isinstance(t, list):  # content blocks
            parts = [b.get("text") for b in t if isinstance(b, dict) and isinstance(b.get("text"), str)]
            if parts:
                return "".join(parts)
    return None


def _parse(raw: str, requested_model: str = "") -> LLMResult:
    text = ""
    usage: dict | None = None
    model = ""
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        t = _assistant_text(ev)
        if t is not None:
            text = t
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else {}
        etype = f"{ev.get('type', '')} {msg.get('type', '')}"
        if "usage" in ev or "token_count" in etype or "turn.completed" in etype:
            u = _find_usage(ev)
            if u:
                usage = u
        m = ev.get("model") or msg.get("model")
        if isinstance(m, str) and m:
            model = m

    estimated = False
    if usage:
        in_tok = _to_int(usage.get("input_tokens")) + _to_int(usage.get("cache_read_input_tokens"))
        out_tok = _to_int(usage.get("output_tokens"))
    else:
        in_tok = 0
        out_tok = len(text) // 4
        estimated = True
    return LLMResult(text=text, input_tokens=in_tok, output_tokens=out_tok, cost_usd=0.0,
                     model=model or requested_model or "codex", estimated=estimated)


def _unknown_flag(stderr: str) -> bool:
    s = (stderr or "").lower()
    return ("unexpected argument" in s or "unrecognized" in s or "unknown option" in s
            or "found argument" in s) and ("-s" in s or "sandbox" in s or "read-only" in s)


@register
class CodexProvider(Provider):
    name = "codex"
    default_model = ""  # let the CLI pick

    def is_configured(self) -> tuple[bool, str]:
        if shutil.which("codex") is None:
            return False, NOT_FOUND_MSG
        return True, "ok"

    def run(self, prompt: str, system: str | None, max_tokens: int, model: str) -> LLMResult:
        full = f"Instructions:\n{system}\n\nTask:\n{prompt}" if system else prompt
        base = ["codex", "exec", "--json", "--skip-git-repo-check"]
        if model:
            base += ["-m", model]
        with tempfile.TemporaryDirectory(prefix="jobbot-codex-") as cwd:
            proc = self._exec(base + SANDBOX_FLAGS + ["-"], full, cwd)
            if proc.returncode != 0 and not proc.stdout.strip() and _unknown_flag(proc.stderr):
                proc = self._exec(base + ["-"], full, cwd)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(f"codex CLI failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
        res = _parse(proc.stdout, model)
        if not res.text and proc.returncode != 0:
            raise RuntimeError(f"codex CLI returned no message (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
        return res

    @staticmethod
    def _exec(cmd: list[str], stdin: str, cwd: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"codex CLI timed out after {TIMEOUT_S}s") from e
