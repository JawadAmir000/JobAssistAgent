"""python-jobspy wrapper (Indeed / LinkedIn / Google).

    fetch_jobspy(query, locations, hours_old, log) -> list[Job]
    _rows_to_jobs(rows: list[dict]) -> list[Job]          (pure, tested)
"""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from typing import Any, Callable

from jobbot.models import Job
# country_for lives in discovery.location, which the ingestion filter uses too, so a search and the
# list it produces agree on what "Canada" means.
from jobbot.discovery.location import country_for  # noqa: F401  (re-exported)

try:
    from jobbot.apply.base import detect_ats
except Exception:  # noqa: BLE001  jobbot.apply/__init__ may import adapters not yet present
    from urllib.parse import urlparse as _urlparse

    def detect_ats(url: str) -> str:  # mirror of jobbot.apply.base.detect_ats
        host = (_urlparse(url).netloc or "").lower()
        for needle, ats in (("greenhouse.io", "greenhouse"), ("lever.co", "lever"), ("ashbyhq.com", "ashby"),
                            ("myworkdayjobs.com", "workday"), ("workday", "workday"),
                            ("smartrecruiters.com", "smartrecruiters"), ("linkedin.com", "linkedin"),
                            ("indeed.com", "indeed")):
            if needle in host:
                return ats
        return "other"

SITES = ["indeed", "linkedin", "google"]
RESULTS_WANTED = 25
LOCATION_BUDGET_S = 240  # covers all of SITES scraped one after another
MAX_DESC = 8000

def _clean(v: Any) -> str:
    """NaN/None-safe string."""
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    try:
        import pandas as pd  # noqa: WPS433
        if v is pd.NaT or (hasattr(pd, "isna") and not isinstance(v, (list, dict, str)) and pd.isna(v)):
            return ""
    except Exception:
        pass
    s = str(v).strip()
    return "" if s.lower() in ("nan", "nat", "none") else s


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = _clean(v).lower()
    return s in ("true", "1", "yes")


def _row_to_job(row: dict) -> Job | None:
    job_url = _clean(row.get("job_url"))
    if not job_url:
        return None
    url = _clean(row.get("job_url_direct")) or job_url
    title = _clean(row.get("title"))
    company = _clean(row.get("company")) or _clean(row.get("company_name")) or "Unknown"
    if not title:
        return None
    loc = _clean(row.get("location"))
    title_l, loc_l = title.lower(), loc.lower()
    remote = _bool(row.get("is_remote")) or "remote" in loc_l or "remote" in title_l
    desc = _clean(row.get("description"))[:MAX_DESC]
    posted = _clean(row.get("date_posted"))[:10] or None
    return Job(
        id="jobspy:" + hashlib.sha1(job_url.encode("utf-8")).hexdigest()[:16],
        company=company, title=title, url=url, ats=detect_ats(url), source="jobspy",
        location=loc, remote=remote, description=desc, posted_at=posted,
        external_id=_clean(row.get("id")),
    )


def _rows_to_jobs(rows: list[dict]) -> list[Job]:
    out: list[Job] = []
    for r in rows:
        j = _row_to_job(r)
        if j:
            out.append(j)
    return out


def _site_kwargs(query: str, location: str, hours_old: int, site: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        site_name=[site], search_term=query, results_wanted=RESULTS_WANTED,
        hours_old=hours_old, description_format="markdown", verbose=0,
    )
    if location.strip().lower() == "remote":
        kwargs.update(location="", is_remote=True, country_indeed="worldwide")
    else:
        kwargs.update(location=location, country_indeed=country_for(location))
    kwargs["google_search_term"] = f"{query} jobs in {location or 'remote'} since last week"
    return kwargs


def _scrape_one(scrape_jobs: Callable, query: str, location: str, hours_old: int,
                log: Callable[[str], None] = lambda _m: None,
                failed: list[str] | None = None) -> list[dict]:
    """One site at a time, because jobspy scrapes them in a pool and re-raises the first failure —
    asking for all three in one call means Google's 429 throws away the Indeed and LinkedIn results
    that had already come back. Sites that blow up are appended to `failed` so the caller can say so."""
    rows: list[dict] = []
    for site in SITES:
        try:
            df = scrape_jobs(**_site_kwargs(query, location, hours_old, site))
        except Exception as e:  # noqa: BLE001 — one blocked site must not cost us the others
            log(f"jobspy[{location}/{site}]: {_short(e)}")
            if failed is not None:
                failed.append(site)
            continue
        if df is None or len(df) == 0:
            continue
        rows.extend(df.to_dict(orient="records"))
    return rows


def _short(e: Exception, limit: int = 160) -> str:
    """Scraper errors carry whole retry URLs; keep the log readable."""
    msg = " ".join(str(e).split())
    return f"{type(e).__name__}: {msg[:limit]}{'…' if len(msg) > limit else ''}"


def fetch_jobspy(query: str, locations: list[str], hours_old: int, log: Callable[[str], None],
                 problems: list[str] | None = None) -> list[Job]:
    try:
        from jobspy import scrape_jobs
    except Exception as e:  # noqa: BLE001
        message = f"jobspy import failed: {_short(e)}"
        log(f"{message}; skipping")
        if problems is not None:
            problems.append(message)
        return []

    results: list[Job] = []
    failed: list[str] = []          # site names that errored, so a blocked scraper is not read as "no jobs"
    attempts = len(locations) * len(SITES)
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        for loc in locations:
            fut = ex.submit(_scrape_one, scrape_jobs, query, loc, hours_old, log, failed)
            try:
                rows = fut.result(timeout=LOCATION_BUDGET_S)
            except FutTimeout:
                message = f"jobspy[{loc}] timed out after {LOCATION_BUDGET_S}s"
                log(message)
                if problems is not None:
                    problems.append(message)
                ex.shutdown(wait=False, cancel_futures=True)
                ex = ThreadPoolExecutor(max_workers=1)  # fresh worker for the next location
                continue
            except Exception as e:  # noqa: BLE001
                message = f"jobspy[{loc}] failed: {_short(e)}"
                log(message)
                if problems is not None:
                    problems.append(message)
                continue
            jobs = _rows_to_jobs(rows)
            log(f"jobspy[{loc}]: {len(jobs)} results")
            results.extend(jobs)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if failed:
        by_site = ", ".join(f"{n}×{s}" for s, n in sorted(Counter(failed).items()))
        message = (f"jobspy: {len(failed)}/{attempts} site attempts failed ({by_site}) — "
                   f"blocked or rate-limited, so these results are incomplete")
        log(message)
        if problems is not None:
            problems.append(message)
    return results
