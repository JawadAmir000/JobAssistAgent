"""LLM provider layer.

Public API (the only thing other layers import):
    from jobbot.llm import complete, get_provider
    complete(prompt, *, purpose, job_id=None, system=None, max_tokens=400) -> str

Every call is recorded in llm_calls with tokens + cost so the UI can show burn per job.
"""
from jobbot.llm.base import Provider, LLMResult, complete, get_provider  # noqa: F401
