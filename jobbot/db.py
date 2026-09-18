"""SQLite persistence. Single-process app; a fresh connection per call is fine.

Contract:
    init() -> None                                   # create tables (idempotent)
    connect() -> sqlite3.Connection                  # row_factory = sqlite3.Row
    get_setting / set_setting
    upsert_jobs(jobs: list[Job]) -> int              # returns number of NEW jobs
    list_jobs(min_score=None, query=None, limit=200, locations=None, max_age_days=None)
    get_job(job_id) -> sqlite3.Row | None
    update_job_score(job_id, score, reason) -> None
    create_application(job_id) -> int                # returns application id
    update_application(app_id, **fields) -> None
    get_application(app_id) / get_application_for_job(job_id) / list_applications(limit)
    record_llm_call(call: LLMCall) -> int
    cost_for_job(job_id) -> CostSummary
    cost_total(since_iso=None) -> CostSummary
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

from jobbot.config import DB_PATH
from jobbot.models import Job, LLMCall, CostSummary

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    ats TEXT NOT NULL DEFAULT 'other',
    source TEXT NOT NULL DEFAULT 'ats_board',
    location TEXT DEFAULT '',
    remote INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    posted_at TEXT,
    external_id TEXT DEFAULT '',
    score INTEGER,
    score_reason TEXT DEFAULT '',
    found_at TEXT NOT NULL,
    hidden INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_company_title ON jobs(company, title);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL DEFAULT 'pending',
    step TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    screenshot TEXT DEFAULT '',
    pending_question TEXT DEFAULT '',
    pending_options TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_job ON applications(job_id);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    job_id TEXT,
    duration_ms INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_job ON llm_calls(job_id);
CREATE TABLE IF NOT EXISTS cover_letters (
    job_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Every blocker a run has hit, counted. The same employer stops jobbot for the same reason on the next
-- run as on this one, and the log is where that pattern was previously visible only to whoever read it: a
-- blocker seen four times is a thing to fix in an adapter, not to keep answering by hand.
CREATE TABLE IF NOT EXISTS issues (
    signature TEXT PRIMARY KEY,
    ats TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    resolution TEXT NOT NULL DEFAULT '',
    seen_count INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    locations TEXT DEFAULT '',
    found INTEGER DEFAULT 0,
    new INTEGER DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    log TEXT DEFAULT '',
    hours_old INTEGER,
    has_snapshot INTEGER NOT NULL DEFAULT 0,
    warning TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS search_jobs (
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    PRIMARY KEY (search_id, job_id)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        _add_column_if_missing(c, "searches", "hours_old", "INTEGER")
        _add_column_if_missing(c, "searches", "has_snapshot", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(c, "searches", "warning", "TEXT DEFAULT ''")


def _add_column_if_missing(c: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add one literal, application-owned column to an older SQLite database."""
    columns = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# ---------- settings ----------
def get_setting(key: str) -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------- cover letters ----------
def get_cover_letter(job_id: str) -> str | None:
    """The letter already written for this job, so a resume or retry sends the same one."""
    with connect() as c:
        row = c.execute("SELECT text FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()
        return row["text"] if row else None


def set_cover_letter(job_id: str, text: str) -> None:
    from datetime import datetime
    with connect() as c:
        c.execute("INSERT INTO cover_letters(job_id,text,created_at) VALUES(?,?,?) "
                  "ON CONFLICT(job_id) DO UPDATE SET text=excluded.text, created_at=excluded.created_at",
                  (job_id, text, datetime.utcnow().isoformat(timespec="seconds")))


# ---------- jobs ----------
def upsert_jobs(jobs: list[Job]) -> int:
    new = 0
    with connect() as c:
        for j in jobs:
            exists = c.execute("SELECT 1 FROM jobs WHERE id=?", (j.id,)).fetchone()
            r = j.to_row()
            if exists:
                c.execute(
                    "UPDATE jobs SET url=:url, location=:location, remote=:remote, posted_at=:posted_at, "
                    "description=CASE WHEN length(:description)>length(description) THEN :description ELSE description END "
                    "WHERE id=:id", r)
            else:
                cols = ",".join(r.keys())
                ph = ",".join(":" + k for k in r.keys())
                c.execute(f"INSERT INTO jobs({cols}) VALUES({ph})", r)
                new += 1
    return new


JOB_LIST_COLUMNS = (
    "j.id, j.company, j.title, j.url, j.ats, j.source, j.location, j.remote, j.posted_at, "
    "j.external_id, j.score, j.score_reason, j.found_at, j.hidden"
)


def list_jobs(min_score: int | None = None, query: str | None = None, limit: int = 200,
              include_hidden: bool = False, locations: list[str] | None = None,
              max_age_days: int | None = None, search_id: int | None = None) -> list[sqlite3.Row]:
    """Jobs for the Jobs tab. `locations` and `max_age_days` mirror the search form: the table keeps
    every posting ever found, so without them the list shows the whole history rather than the search."""
    sql = f"SELECT {JOB_LIST_COLUMNS}, a.status AS app_status, a.id AS app_id FROM jobs j "
    args: list = []
    if search_id is not None:
        sql += "JOIN search_jobs sj ON sj.job_id=j.id AND sj.search_id=? "
        args.append(search_id)
    sql += ("LEFT JOIN applications a ON a.id = (SELECT id FROM applications WHERE job_id=j.id "
            "ORDER BY id DESC LIMIT 1) WHERE 1=1")
    if not include_hidden:
        sql += " AND j.hidden=0"
    if min_score is not None:
        sql += " AND (j.score IS NULL OR j.score>=?)"
        args.append(min_score)
    if query:
        sql += " AND (j.title LIKE ? OR j.company LIKE ?)"
        args += [f"%{query}%", f"%{query}%"]
    if max_age_days and search_id is None:
        cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
        # posted_at is the truth when the board gives one; found_at is the fallback for boards that don't.
        sql += " AND COALESCE(NULLIF(substr(j.posted_at,1,10),''), substr(j.found_at,1,10)) >= ?"
        args.append(cutoff)
    if search_id is not None:
        sql += " ORDER BY COALESCE(NULLIF(j.posted_at,''), j.found_at) DESC, COALESCE(j.score,0) DESC"
    else:
        sql += " ORDER BY COALESCE(j.score,0) DESC, j.posted_at DESC, j.found_at DESC"
    if search_id is not None or not locations:
        sql += " LIMIT ?"
        args.append(limit)
    with connect() as c:
        rows = c.execute(sql, args).fetchall()
    if search_id is not None or not locations:
        return rows
    from jobbot.discovery.location import matches
    return [r for r in rows if matches(r["location"], bool(r["remote"]), locations)][:limit]


def get_job(job_id: str) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def update_job_score(job_id: str, score: int, reason: str = "") -> None:
    with connect() as c:
        c.execute("UPDATE jobs SET score=?, score_reason=? WHERE id=?", (score, reason, job_id))


def set_job_hidden(job_id: str, hidden: bool) -> None:
    with connect() as c:
        c.execute("UPDATE jobs SET hidden=? WHERE id=?", (int(hidden), job_id))


# ---------- applications ----------
def create_application(job_id: str) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO applications(job_id, status, started_at) VALUES(?, 'pending', ?)",
                        (job_id, datetime.utcnow().isoformat(timespec="seconds")))
        return cur.lastrowid


def update_application(app_id: int, **fields) -> None:
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE applications SET {sets} WHERE id=?", (*fields.values(), app_id))


_ISSUE_NOISE = re.compile(r"\d+|https?://\S+|'[^']*'|\"[^\"]*\"")


def issue_signature(ats: str, reason: str) -> str:
    """One key per kind of blocker, not per occurrence.

    The numbers, urls and quoted field names inside a reason are what make two reports of the same problem
    look different ("would not move past 'My Information'" vs "past 'My Experience'"), so they come out
    before the reason is used as a key.
    """
    core = _ISSUE_NOISE.sub("", (reason or "").lower())
    core = re.sub(r"\s+", " ", core).strip()[:160]
    return f"{(ats or '').lower()}|{core}"


def record_issue(ats: str, company: str, reason: str) -> int:
    """Note that a run stopped for this reason. Returns how many times it has now happened.

    Never raises: bookkeeping about a failure must not become a second failure.
    """
    if not (reason or "").strip():
        return 0
    now = datetime.utcnow().isoformat(timespec="seconds")
    signature = issue_signature(ats, reason)
    try:
        with connect() as c:
            c.execute(
                """INSERT INTO issues (signature, ats, company, reason, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(signature) DO UPDATE SET
                       seen_count = seen_count + 1, last_seen = excluded.last_seen,
                       company = excluded.company, reason = excluded.reason""",
                (signature, ats or "", company or "", reason[:500], now, now))
            row = c.execute("SELECT seen_count FROM issues WHERE signature=?", (signature,)).fetchone()
            return int(row[0]) if row else 1
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).debug("could not record the issue: %s", e)
        return 0


def known_issues(limit: int = 20) -> list[sqlite3.Row]:
    """The blockers that keep happening, worst first."""
    with connect() as c:
        return c.execute(
            "SELECT * FROM issues ORDER BY seen_count DESC, last_seen DESC LIMIT ?", (limit,)).fetchall()


def get_application(app_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()


def get_application_for_job(job_id: str) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()


def list_applications(limit: int = 500) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT a.*, j.company, j.title, j.url, j.ats FROM applications a JOIN jobs j ON j.id=a.job_id "
            "ORDER BY a.id DESC LIMIT ?", (limit,)).fetchall()


# ---------- llm calls / cost ----------
def record_llm_call(call: LLMCall) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO llm_calls(provider,model,purpose,input_tokens,output_tokens,cost_usd,job_id,duration_ms,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (call.provider, call.model, call.purpose, call.input_tokens, call.output_tokens, call.cost_usd,
             call.job_id, call.duration_ms, call.created_at))
        return cur.lastrowid


def _summary(row) -> CostSummary:
    return CostSummary(calls=row[0] or 0, input_tokens=row[1] or 0, output_tokens=row[2] or 0, cost_usd=row[3] or 0.0)


def cost_for_job(job_id: str) -> CostSummary:
    with connect() as c:
        row = c.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) FROM llm_calls WHERE job_id=?", (job_id,)).fetchone()
        return _summary(row)


def cost_total(since_iso: str | None = None) -> CostSummary:
    with connect() as c:
        if since_iso:
            row = c.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) FROM llm_calls WHERE created_at>=?", (since_iso,)).fetchone()
        else:
            row = c.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) FROM llm_calls").fetchone()
        return _summary(row)


def list_llm_calls(limit: int = 200) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ---------- searches ----------
def create_search(query: str, locations: str, hours_old: int | None = None) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO searches(query, locations, hours_old, has_snapshot, started_at) VALUES(?,?,?,?,?)",
            (query, locations, hours_old, 1, datetime.utcnow().isoformat(timespec="seconds")))
        return cur.lastrowid


def record_search_jobs(search_id: int, job_ids: list[str]) -> None:
    with connect() as c:
        c.executemany("INSERT OR IGNORE INTO search_jobs(search_id, job_id) VALUES(?,?)",
                      ((search_id, job_id) for job_id in job_ids))


def count_search_jobs(search_id: int, include_hidden: bool = False) -> int:
    sql = "SELECT COUNT(*) FROM search_jobs sj JOIN jobs j ON j.id=sj.job_id WHERE sj.search_id=?"
    if not include_hidden:
        sql += " AND j.hidden=0"
    with connect() as c:
        return int(c.execute(sql, (search_id,)).fetchone()[0])


def latest_snapshot_search() -> sqlite3.Row | None:
    with connect() as c:
        return c.execute(
            "SELECT * FROM searches WHERE has_snapshot=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()


def update_search(search_id: int, **fields) -> None:
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE searches SET {sets} WHERE id=?", (*fields.values(), search_id))


def get_search(search_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone()
