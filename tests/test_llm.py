"""Offline tests for the LLM provider layer (no network, no CLIs required)."""
from __future__ import annotations

import json

import pytest

from jobbot import db
from jobbot.llm import anthropic_api, base, claude_code, codex, prices  # noqa: F401
from jobbot.llm.base import LLMResult, Provider, available_providers, complete, register


def test_registry_loads_all_backends():
    names = set(available_providers())
    assert {"anthropic", "claude_code", "codex"} <= names
    assert available_providers()["anthropic"].default_model == "claude-haiku-4-5"
    assert available_providers()["claude_code"].default_model == ""
    assert available_providers()["codex"].default_model == ""


# ---------- prices ----------
def test_prices_cost_known_model():
    usd, est = prices.cost("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert usd == pytest.approx(1.00 + 5.00)
    assert est is False
    usd, est = prices.cost("claude-sonnet-4-5", 1000, 500)
    assert usd == pytest.approx(1000 * 3 / 1e6 + 500 * 15 / 1e6)
    assert est is False


def test_prices_cost_dated_suffix_and_unknown():
    usd, est = prices.cost("claude-opus-4-1-20250805", 100, 10)
    assert est is False
    assert usd == pytest.approx(100 * 15 / 1e6 + 10 * 75 / 1e6)
    usd, est = prices.cost("some-unknown-model", 1_000_000, 0)
    assert est is True
    assert usd == pytest.approx(1.00)  # haiku fallback


# ---------- claude_code parser ----------
CLAUDE_JSON = {
    "type": "result", "subtype": "success", "is_error": False,
    "result": "Hello from Claude",
    "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7},
    "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 115, "outputTokens": 7}},
}


def test_claude_code_parse_full():
    res = claude_code._parse(json.dumps(CLAUDE_JSON))
    assert res.text == "Hello from Claude"
    assert res.input_tokens == 115
    assert res.output_tokens == 7
    assert res.cost_usd == pytest.approx(0.0123)
    assert res.model == "claude-haiku-4-5-20251001"
    assert res.estimated is False


def test_claude_code_parse_missing_usage_estimates():
    res = claude_code._parse(json.dumps({"result": "x" * 40}))
    assert res.text == "x" * 40
    assert res.estimated is True
    assert res.output_tokens == 10
    assert res.cost_usd == 0.0
    assert res.model == "claude-code"


def test_claude_code_parse_tolerates_noise_and_garbage():
    res = claude_code._parse("some warning line\n" + json.dumps({"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 2}}))
    assert res.text == "ok" and res.input_tokens == 1 and res.output_tokens == 2
    res = claude_code._parse("plain text output")
    assert res.text == "plain text output" and res.estimated


# ---------- codex parser ----------
CODEX_JSONL_NEW = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i1", "type": "reasoning", "text": "thinking..."}},
    {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "First draft"}},
    {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "Final answer"}},
    {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 20}},
])

CODEX_JSONL_OLD = "\n".join(json.dumps(e) for e in [
    {"id": "0", "msg": {"type": "session_configured", "model": "gpt-5-codex"}},
    {"id": "1", "msg": {"type": "agent_message", "message": "Old style answer"}},
    {"id": "2", "msg": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 30, "output_tokens": 9}}}},
])


def test_codex_parse_new_style():
    res = codex._parse(CODEX_JSONL_NEW)
    assert res.text == "Final answer"
    assert res.input_tokens == 50 and res.output_tokens == 20
    assert res.cost_usd == 0.0
    assert res.estimated is False
    assert res.model == "codex"


def test_codex_parse_old_style():
    res = codex._parse(CODEX_JSONL_OLD)
    assert res.text == "Old style answer"
    assert res.input_tokens == 30 and res.output_tokens == 9
    assert res.model == "gpt-5-codex"
    assert res.estimated is False


def test_codex_parse_no_usage_estimates():
    raw = "not json\n" + json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "abcdefgh"}})
    res = codex._parse(raw)
    assert res.text == "abcdefgh"
    assert res.estimated is True and res.output_tokens == 2 and res.input_tokens == 0


# ---------- complete() records llm_calls ----------
def test_complete_records_llm_call(monkeypatch):
    db.init()
    available_providers()  # make sure real backends are loaded before we add the fake

    calls: list = []

    @register
    class FakeProvider(Provider):
        name = "fake"
        default_model = "fake-model"

        def is_configured(self):
            return True, "ok"

        def run(self, prompt, system, max_tokens, model):
            calls.append((prompt, system, max_tokens, model))
            return LLMResult(text="fake reply", input_tokens=11, output_tokens=3, cost_usd=0.5, model=model)

    try:
        before = db.cost_total().calls
        out = complete("hi", purpose="test", job_id="job-x", system="sys", provider="fake")
        assert out == "fake reply"
        assert calls and calls[0][0] == "hi" and calls[0][1] == "sys" and calls[0][3] == "fake-model"
        assert db.cost_total().calls == before + 1
        row = db.list_llm_calls(limit=1)[0]
        assert row["provider"] == "fake"
        assert row["model"] == "fake-model"
        assert row["purpose"] == "test"
        assert row["job_id"] == "job-x"
        assert row["input_tokens"] == 11 and row["output_tokens"] == 3
        assert row["cost_usd"] == pytest.approx(0.5)
        summary = db.cost_for_job("job-x")
        assert summary.calls >= 1 and summary.cost_usd >= 0.5
    finally:
        base._REGISTRY.pop("fake", None)


def test_complete_raises_when_not_configured():
    @register
    class Unconfigured(Provider):
        name = "unconfigured"

        def is_configured(self):
            return False, "nope"

    try:
        with pytest.raises(RuntimeError, match="nope"):
            complete("hi", purpose="test", provider="unconfigured")
    finally:
        base._REGISTRY.pop("unconfigured", None)
