"""Public job-board APIs for Greenhouse, Lever and Ashby.

    greenhouse(slug) / lever(slug) / ashby(slug) -> list[Job]      (network)
    _parse_greenhouse(payload, slug) / _parse_lever / _parse_ashby  (pure, tested)
    fetch_all_boards(companies, title_filter, log) -> list[Job]
    title_matches(title, query) -> bool
"""
from __future__ import annotations

import html as _html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Iterable

import httpx

from jobbot.models import Job

TIMEOUT = 15.0
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}
MAX_DESC = 8000

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|section)[^>]*>", re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n\s*\n+")


def html_to_text(s: str | None) -> str:
    """Strip HTML to plain text (unescapes entities, keeps rough paragraph breaks)."""
    if not s:
        return ""
    s = _html.unescape(s)          # Greenhouse returns entity-escaped HTML
    s = _BLOCK_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = _NL_RE.sub("\n\n", s)
    return s.strip()


def _truncate(s: str) -> str:
    return s[:MAX_DESC]


def _is_remote(*parts: str | None) -> bool:
    return any(p and "remote" in p.lower() for p in parts)


def _ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except Exception:
        return None


def _get_json(url: str) -> dict | list:
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.json()


# ---------- greenhouse ----------
def _parse_greenhouse(payload: dict, slug: str, company: str | None = None) -> list[Job]:
    company = company or slug
    out: list[Job] = []
    for j in payload.get("jobs") or []:
        ext = str(j.get("id") or "")
        title = (j.get("title") or "").strip()
        url = j.get("absolute_url") or ""
        if not (ext and title and url):
            continue
        loc = ((j.get("location") or {}).get("name") or "").strip()
        updated = (j.get("updated_at") or j.get("first_published") or "")[:10] or None
        out.append(Job(
            id=f"greenhouse:{slug}:{ext}", company=company, title=title, url=url,
            ats="greenhouse", source="ats_board", location=loc,
            remote=_is_remote(loc, title), description=_truncate(html_to_text(j.get("content"))),
            posted_at=updated, external_id=ext,
        ))
    return out


def greenhouse(slug: str, company: str | None = None) -> list[Job]:
    payload = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return _parse_greenhouse(payload, slug, company)


# ---------- lever ----------
def _parse_lever(payload: list, slug: str, company: str | None = None) -> list[Job]:
    company = company or slug
    out: list[Job] = []
    for j in payload or []:
        ext = str(j.get("id") or "")
        title = (j.get("text") or "").strip()
        url = j.get("hostedUrl") or ""
        if not (ext and title and url):
            continue
        cats = j.get("categories") or {}
        loc = (cats.get("location") or "").strip()
        all_locs = j.get("allLocations") or []
        if all_locs and isinstance(all_locs, list):
            loc = ", ".join(dict.fromkeys([loc, *[str(x) for x in all_locs]])) if loc else ", ".join(map(str, all_locs))
        wt = cats.get("workplaceType") or j.get("workplaceType") or ""
        desc = j.get("descriptionPlain") or html_to_text(j.get("description"))
        out.append(Job(
            id=f"lever:{slug}:{ext}", company=company, title=title, url=url,
            ats="lever", source="ats_board", location=loc,
            remote=_is_remote(loc, title, wt), description=_truncate(desc),
            posted_at=_ms_to_iso(j.get("createdAt")), external_id=ext,
        ))
    return out


def lever(slug: str, company: str | None = None) -> list[Job]:
    payload = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return _parse_lever(payload, slug, company)


# ---------- ashby ----------
def _parse_ashby(payload: dict, slug: str, company: str | None = None) -> list[Job]:
    company = company or slug
    out: list[Job] = []
    for j in payload.get("jobs") or []:
        ext = str(j.get("id") or "")
        title = (j.get("title") or "").strip()
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        if not (ext and title and url):
            continue
        loc = (j.get("location") or "").strip()
        desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
        remote = bool(j.get("isRemote")) or _is_remote(loc, title)
        out.append(Job(
            id=f"ashby:{slug}:{ext}", company=company, title=title, url=url,
            ats="ashby", source="ats_board", location=loc, remote=remote,
            description=_truncate(desc), posted_at=(j.get("publishedAt") or "")[:10] or None,
            external_id=ext,
        ))
    return out


def ashby(slug: str, company: str | None = None) -> list[Job]:
    payload = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return _parse_ashby(payload, slug, company)


FETCHERS: dict[str, Callable[[str, str | None], list[Job]]] = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
}


# ---------- title matching ----------
STOPWORDS = {"and", "the", "for", "with", "engineer", "engineering", "senior", "junior", "lead",
             "role", "roles", "jobs", "job", "remote", "team", "staff", "principal", "level"}

SYNONYMS: dict[str, tuple[str, ...]] = {
    "forward deployed": ("forward deployed", "forward-deployed", "fde", "applied ai", "solutions engineer",
                         "solution engineer", "customer engineer", "deployment engineer", "ai engineer",
                         "implementation engineer", "deployed engineer", "field engineer"),
    "fde": ("forward deployed", "forward-deployed", "fde"),
    "applied ai": ("applied ai", "ai engineer", "forward deployed", "llm engineer", "genai", "generative ai"),
    "ai engineer": ("ai engineer", "applied ai", "llm engineer", "machine learning engineer", "agentic"),
    "solutions engineer": ("solutions engineer", "solution engineer", "customer engineer",
                           "sales engineer", "implementation engineer"),
    "customer engineer": ("customer engineer", "solutions engineer", "solution engineer"),
}


def _clean(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())


def expand_query(query: str) -> set[str]:
    """Phrases that count as a match for `query`."""
    q = _clean(query).strip()
    q = re.sub(r"\s+", " ", q)
    phrases = {q} if q else set()
    for key, syns in SYNONYMS.items():
        if key in q:
            phrases.update(syns)
    return phrases


def significant_words(query: str) -> set[str]:
    return {w for w in _clean(query).split() if len(w) > 3 and w not in STOPWORDS}


def title_matches(title: str, query: str) -> bool:
    if not query or not query.strip():
        return True
    t = " " + re.sub(r"\s+", " ", _clean(title)).strip() + " "
    for phrase in expand_query(query):
        if phrase and f" {phrase} " in t:
            return True
    words = set(t.split())
    return any(w in words for w in significant_words(query))


# ---------- bulk fetch ----------
def fetch_all_boards(companies: Iterable[dict], title_filter: str, log: Callable[[str], None],
                     workers: int = 8) -> list[Job]:
    """Fetch every company board concurrently; failures are logged and skipped."""
    companies = [c for c in companies if c.get("slug") and c.get("ats") in FETCHERS]
    results: list[Job] = []

    def one(c: dict) -> tuple[dict, list[Job]]:
        fetch = FETCHERS[c["ats"]]
        return c, fetch(c["slug"], c.get("name") or c["slug"])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, c): c for c in companies}
        for fut in as_completed(futs):
            c = futs[fut]
            name = c.get("name") or c.get("slug")
            try:
                _, jobs = fut.result()
            except httpx.HTTPStatusError as e:
                log(f"{name}: HTTP {e.response.status_code} ({c['ats']}/{c['slug']})")
                continue
            except Exception as e:  # noqa: BLE001
                log(f"{name}: {type(e).__name__}: {e}")
                continue
            kept = [j for j in jobs if title_matches(j.title, title_filter)]
            log(f"{name}: {len(jobs)} postings, {len(kept)} match")
            results.extend(kept)
    return results
