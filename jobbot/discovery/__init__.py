"""Job discovery orchestration.

    run_search(query, locations=None, hours_old=None, sources=("ats", "jobspy")) -> int   (search id)
    list_companies() -> list[dict]
    add_company(name, ats, slug) -> None

run_search never raises for source failures: every source error is logged into searches.log and the
row finishes with status "done" (or "error" only if something outside the sources blows up).
"""
from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import yaml

from jobbot import config, db
from jobbot.models import Job

__all__ = ["run_search", "list_companies", "add_company"]

_LOG_FLUSH_EVERY = 3


# ---------- companies.yaml ----------
def list_companies() -> list[dict]:
    path = config.COMPANIES_PATH
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    out = []
    for c in data.get("companies") or []:
        if not isinstance(c, dict) or not c.get("slug") or not c.get("ats"):
            continue
        out.append({
            "name": str(c.get("name") or c["slug"]),
            "ats": str(c["ats"]).lower().strip(),
            "slug": str(c["slug"]).strip(),
            "verified": bool(c.get("verified", True)),
        })
    return out


def add_company(name: str, ats: str, slug: str, verified: bool = False) -> None:
    ats = ats.lower().strip()
    if ats not in ("greenhouse", "lever", "ashby"):
        raise ValueError(f"ats must be greenhouse|lever|ashby, got {ats!r}")
    path = config.COMPANIES_PATH
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    data = data or {}
    companies = data.setdefault("companies", []) or []
    for c in companies:
        if isinstance(c, dict) and c.get("ats") == ats and c.get("slug") == slug:
            c["name"] = name
            c["verified"] = verified
            break
    else:
        companies.append({"name": name, "ats": ats, "slug": slug.strip(), "verified": verified})
    data["companies"] = companies
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


# ---------- search ----------
class _SearchLog:
    """Thread-safe log buffer that flushes to searches.log every few lines."""

    def __init__(self, search_id: int):
        self.search_id = search_id
        self.lines: list[str] = []
        self._lock = threading.Lock()
        self._pending = 0

    def __call__(self, msg: str) -> None:
        line = f"{datetime.utcnow().strftime('%H:%M:%S')} {msg}"
        with self._lock:
            self.lines.append(line)
            self._pending += 1
            flush = self._pending >= _LOG_FLUSH_EVERY
            if flush:
                self._pending = 0
        if flush:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            text = "\n".join(self.lines)
        try:
            db.update_search(self.search_id, log=text)
        except Exception:  # noqa: BLE001
            pass

    def text(self) -> str:
        with self._lock:
            return "\n".join(self.lines)


def _parse_locations(locations: list[str] | str | None) -> list[str]:
    if locations is None:
        raw = config.get_setting(config.SETTING_LOCATIONS) or ""
        locations = raw
    if isinstance(locations, str):
        locations = [x.strip() for x in locations.split(",")]
    return [x for x in locations if x]


def _run_ats(query: str, log) -> list[Job]:
    from jobbot.discovery.ats_boards import fetch_all_boards
    companies = list_companies()
    log(f"ats: polling {len(companies)} company boards")
    jobs = fetch_all_boards(companies, query, log)
    log(f"ats: {len(jobs)} matching postings")
    return jobs


def _run_jobspy(query: str, locations: list[str], hours_old: int, log,
                problems: list[str] | None = None) -> list[Job]:
    from jobbot.discovery.jobspy_source import fetch_jobspy
    log(f"jobspy: searching {len(locations)} locations (hours_old={hours_old})")
    jobs = fetch_jobspy(query, locations, hours_old, log, problems)
    log(f"jobspy: {len(jobs)} results")
    return jobs


def run_search(query: str, locations: list[str] | str | None = None, hours_old: int | None = None,
               sources: tuple[str, ...] = ("ats", "jobspy"), search_id: int | None = None) -> int:
    from jobbot.discovery.location import filter_jobs
    from jobbot.discovery.merge import dedupe
    from jobbot.discovery.scorer import score_all

    db.init()
    locs = _parse_locations(locations)
    if hours_old is None:
        try:
            hours_old = int(config.get_setting(config.SETTING_HOURS_OLD) or 72)
        except ValueError:
            hours_old = 72
    if search_id is None:
        search_id = db.create_search(query, ", ".join(locs), hours_old)
    else:
        db.update_search(search_id, locations=", ".join(locs), hours_old=hours_old, status="running")
    log = _SearchLog(search_id)
    problems: list[str] = []
    log(f"search {search_id}: {query!r} in {locs or ['(any)']}, sources={list(sources)}")

    try:
        collected: list[Job] = []

        def guarded(name, fn, *args):
            try:
                return fn(*args)
            except Exception as e:  # noqa: BLE001
                log(f"{name}: source failed: {type(e).__name__}: {e}")
                problems.append(f"{name} failed: {type(e).__name__}: {e}")
                return []

        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = []
            if "ats" in sources:
                futs.append(ex.submit(guarded, "ats", _run_ats, query, log))
            if "jobspy" in sources:
                futs.append(ex.submit(guarded, "jobspy", _run_jobspy, query, locs, hours_old, log, problems))
            for f in futs:
                collected.extend(f.result())

        merged = dedupe(collected)
        log(f"merge: {len(collected)} raw -> {len(merged)} unique")

        # ATS boards return a company's whole board and jobspy happily answers "Canada" with jobs
        # elsewhere, so the search criteria are enforced here — before anything is stored or scored.
        max_age_days = max(1, round(hours_old / 24)) if hours_old else None
        merged, dropped = filter_jobs(merged, locs, max_age_days)
        if dropped["location"] or dropped["stale"]:
            log(f"filter: kept {len(merged)} in {locs or ['(any)']} within {hours_old}h "
                f"({dropped['location']} out of area, {dropped['stale']} too old)")

        if not merged:
            # A green "found 0" reads as "no such jobs exist". Say which of the two it actually was:
            # nothing came back, or everything that came back was filtered out.
            if not collected:
                log("note: every source came back empty — check the errors above before trusting this")
            else:
                log(f"note: all {len(collected)} postings were filtered out. The ATS boards return each "
                    f"company's whole board, so {locs or ['(any)']} has to come from JobSpy — widen the "
                    f"locations or the date window, or re-run if JobSpy was blocked above.")

        new = db.upsert_jobs(merged)
        db.record_search_jobs(search_id, [j.id for j in merged])
        log(f"db: {new} new jobs, {len(merged) - new} already known")
        db.update_search(search_id, found=len(merged), new=new)
        log.flush()

        # score only rows whose score is NULL
        ids = [j.id for j in merged]
        unscored = _unscored(ids)
        to_score = [j for j in merged if j.id in unscored]
        if to_score:
            facts = config.load_facts()
            log(f"score: scoring {len(to_score)} jobs")
            results = score_all(to_score, facts, log)
            for jid, (score, reason) in results.items():
                db.update_job_score(jid, score, reason)
            counts = {}
            for s, _ in results.values():
                counts[s] = counts.get(s, 0) + 1
            log("score: " + ", ".join(f"{k}★×{v}" for k, v in sorted(counts.items(), reverse=True)))
        else:
            log("score: nothing new to score")

        log("done")
        warning = "; ".join(dict.fromkeys(problems))[:500]
        db.update_search(search_id, status="done",
                         finished_at=datetime.utcnow().isoformat(timespec="seconds"), log=log.text(),
                         warning=warning)
    except Exception as e:  # noqa: BLE001
        problems.append(f"search failed: {type(e).__name__}: {e}")
        log(f"error: {type(e).__name__}: {e}")
        log(traceback.format_exc().strip().splitlines()[-1])
        db.update_search(search_id, status="error",
                         finished_at=datetime.utcnow().isoformat(timespec="seconds"), log=log.text(),
                         warning="; ".join(dict.fromkeys(problems))[:500])
    return search_id


def _unscored(ids: list[str]) -> set[str]:
    if not ids:
        return set()
    out: set[str] = set()
    with db.connect() as c:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            rows = c.execute(f"SELECT id FROM jobs WHERE score IS NULL AND id IN ({ph})", chunk).fetchall()
            out.update(r["id"] for r in rows)
    return out
