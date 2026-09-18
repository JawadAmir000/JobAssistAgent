"""Relevance scoring: cheap rules first, LLM only for the ambiguous middle.

    rule_score(job) -> (score 1..5, reason)
    score_job(job, facts) -> (score, reason)         # may call the LLM when rules land on 3
    score_all(jobs, facts, log) -> dict[job_id, (score, reason)]
"""
from __future__ import annotations

import re
from typing import Callable

from jobbot.models import Job

_LLM_KEYWORDS = ("llm", "large language model", "gpt", "claude", "openai", "anthropic", "agent",
                 "agentic", "rag", "retrieval augmented", "generative ai", "genai", "langchain", "prompt")
_BAD_TITLE = ("manager", "director", "intern", "internship", "recruiter", "recruiting", "sales",
              "account executive", "marketing", "head of", "vp ", "vice president", "chief")
_RESTRICT = ("security clearance", "clearance required", "active clearance", "ts/sci", "us citizen only",
             "u.s. citizens only", "must be a us citizen", "must be a u.s. citizen", "must be located in",
             "must reside in", "must be based in")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().replace("-", " ")).strip()


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def rule_score(job: Job) -> tuple[int, str]:
    t = _norm(job.title)
    d = _norm(job.description)
    is_fde = "forward deployed" in t or _has_word(t, "fde")

    hit = next((b for b in _BAD_TITLE if (_has_word(t, b) if " " not in b else b in t)), None)
    if hit and not is_fde:
        return 1, f"title looks like '{hit.strip()}' role"
    if ("staff" in t.split() or "principal" in t) and not is_fde:
        return 1, "staff/principal level"

    if is_fde:
        score, reason = 5, "forward deployed title"
    elif "applied ai" in t or "ai engineer" in t or "agentic" in t or "llm engineer" in t:
        score, reason = 4, "applied AI / AI engineer title"
    elif ("solutions engineer" in t or "solution engineer" in t or "customer engineer" in t
          or "implementation engineer" in t or "deployment engineer" in t) and _has_ai(t, d):
        score, reason = 4, "solutions/customer engineer with AI focus"
    elif "software engineer" in t or "software developer" in t or "full stack" in t or "backend" in t:
        if any(k in d for k in _LLM_KEYWORDS) or _has_ai(t, ""):
            score, reason = 3, "software engineer with LLM/agent keywords"
        else:
            score, reason = 2, "generic software engineer"
    elif "solutions engineer" in t or "customer engineer" in t or "machine learning" in t or _has_ai(t, ""):
        score, reason = 3, "adjacent title"
    else:
        score, reason = 2, "no strong title signal"

    if any(r in d for r in _RESTRICT) and not job.remote:
        if score > 2:
            score, reason = 2, reason + "; location/clearance restriction"
    return score, reason


def _has_ai(title: str, desc: str) -> bool:
    return (_has_word(title, "ai") or "artificial intelligence" in title or _has_word(title, "ml")
            or "machine learning" in title or _has_word(desc, "ai") or "llm" in desc)


# ---------- LLM refinement ----------
def _llm_prompt(job: Job, facts: dict) -> str:
    roles = ", ".join(map(str, (facts.get("target_roles") or [])[:6]))
    skills = ", ".join(map(str, (facts.get("skills") or [])[:12]))
    desc = re.sub(r"\s+", " ", job.description or "")[:800]
    return (
        "Rate how well this job fits the candidate on a 1-5 scale (5 = ideal forward-deployed / applied AI "
        "engineer role; 1 = irrelevant). Penalise roles needing US citizenship/clearance or that are not "
        "individual-contributor engineering.\n"
        f"Candidate targets: {roles}\nCandidate skills: {skills}\n\n"
        f"Job title: {job.title}\nCompany: {job.company}\nLocation: {job.location or 'n/a'}"
        f"{' (remote)' if job.remote else ''}\nDescription: {desc}\n\n"
        "Reply exactly as:\nSCORE: <1-5>\nREASON: <one short sentence>"
    )


def _parse_llm(text: str) -> tuple[int, str] | None:
    if not text:
        return None
    m = re.search(r"SCORE\s*[:=]\s*([1-5])", text, re.I) or re.search(r"\b([1-5])\b", text)
    if not m:
        return None
    score = int(m.group(1))
    r = re.search(r"REASON\s*[:=]\s*(.+)", text, re.I | re.S)
    reason = (r.group(1).strip().splitlines()[0] if r else "").strip()[:200]
    return score, reason or "llm"


def llm_score(job: Job, facts: dict) -> tuple[int, str] | None:
    from jobbot.llm import complete
    text = complete(_llm_prompt(job, facts), purpose="score", job_id=job.id, max_tokens=60)
    return _parse_llm(text)


def score_job(job: Job, facts: dict, use_llm: bool = True) -> tuple[int, str]:
    score, reason = rule_score(job)
    if score == 3 and use_llm:
        try:
            res = llm_score(job, facts)
            if res:
                return res[0], f"llm: {res[1]}"
        except Exception:  # noqa: BLE001  (RuntimeError when not configured, network, parse)
            pass
    return score, reason


def score_all(jobs: list[Job], facts: dict, log: Callable[[str], None] | None = None,
              use_llm: bool = True) -> dict[str, tuple[int, str]]:
    log = log or (lambda _s: None)
    out: dict[str, tuple[int, str]] = {}
    llm_ok = use_llm
    for j in jobs:
        score, reason = rule_score(j)
        if score == 3 and llm_ok:
            try:
                res = llm_score(j, facts)
                if res:
                    score, reason = res[0], f"llm: {res[1]}"
            except RuntimeError as e:
                llm_ok = False
                log(f"scorer: LLM unavailable, using rules only ({e})")
            except Exception as e:  # noqa: BLE001
                log(f"scorer: LLM error on {j.id}: {type(e).__name__}: {e}")
        out[j.id] = (score, reason)
    return out
