"""Shared dataclasses. These are the types passed between layers."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Literal

ATS = Literal["greenhouse", "lever", "ashby", "workday", "smartrecruiters", "linkedin", "indeed", "other"]
Source = Literal["ats_board", "jobspy"]
ApplyStatus = Literal["pending", "running", "needs_you", "submitted", "failed", "skipped", "manual"]


@dataclass
class Job:
    id: str                      # stable: f"{ats}:{external_id}" or sha1 of url
    company: str
    title: str
    url: str                     # listing / apply page URL
    ats: str = "other"
    source: str = "ats_board"
    location: str = ""
    remote: bool = False
    description: str = ""        # plain text, may be truncated to ~8k chars
    posted_at: str | None = None # ISO date
    external_id: str = ""
    score: int | None = None     # 1..5, None = unscored
    score_reason: str = ""
    found_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds"))

    def to_row(self) -> dict:
        d = asdict(self)
        d["remote"] = int(self.remote)
        return d


@dataclass
class Application:
    job_id: str
    status: str = "pending"
    step: str = ""               # human-readable current step, e.g. "filling identity"
    reason: str = ""             # why needs_you / failed
    screenshot: str = ""         # path under data/screenshots
    pending_question: str = ""   # question awaiting the user's answer (needs_you)
    pending_options: str = ""    # JSON list of options if a select/radio
    id: int | None = None
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds"))
    finished_at: str | None = None


@dataclass
class LLMCall:
    provider: str
    model: str
    purpose: str                 # "score" | "answer" | "cover_letter" | "repair"
    input_tokens: int
    output_tokens: int
    cost_usd: float
    job_id: str | None = None
    duration_ms: int = 0
    id: int | None = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds"))


@dataclass
class CostSummary:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
