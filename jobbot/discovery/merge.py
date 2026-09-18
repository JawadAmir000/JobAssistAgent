"""Cross-source dedupe.

    dedupe(jobs) -> list[Job]   keyed on (company_norm, title_norm); ats_board beats jobspy,
                                then the longer description wins.
"""
from __future__ import annotations

import re

from jobbot.models import Job

_SUFFIX_RE = re.compile(r"\b(inc|incorporated|ltd|limited|llc|corp|corporation|co|plc|pty|gmbh|ag|sa)\b\.?")
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")

_SOURCE_RANK = {"ats_board": 0, "jobspy": 1}


def norm_company(name: str) -> str:
    s = (name or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _SUFFIX_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def norm_title(title: str) -> str:
    s = (title or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _better(a: Job, b: Job) -> Job:
    ra, rb = _SOURCE_RANK.get(a.source, 9), _SOURCE_RANK.get(b.source, 9)
    if ra != rb:
        return a if ra < rb else b
    return a if len(a.description or "") >= len(b.description or "") else b


def dedupe(jobs: list[Job]) -> list[Job]:
    best: dict[tuple[str, str], Job] = {}
    order: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for j in jobs:
        if j.id in seen_ids:
            continue
        seen_ids.add(j.id)
        key = (norm_company(j.company), norm_title(j.title))
        if key in best:
            best[key] = _better(best[key], j)
        else:
            best[key] = j
            order.append(key)
    return [best[k] for k in order]
