"""What jobbot has learned about getting through pages, so a problem solved once stays solved.

Two memories, both written only from outcomes — a page that actually moved on, or an application that went
in — never from what the model merely said:

  * **site steps**: on this site, at this step (observe.signature), the page was a `kind` and pressing
    `action` moved it on. The second visit to a step replays it without asking the model anything. A step
    whose replay stops working is counted against, and once it has failed more than it has worked it is no
    longer trusted and the planner looks at the page afresh.
  * **lessons**: a page that reads like *this* is a `kind`, on whatever site it turns up. This is what makes
    a fix travel. JOIN's "We've sent you a secure login link — Check your email" is learned as an emailed
    link page once, and the next board that words its magic-link page the same way is recognised from
    memory, deterministically, before any model call.

What a kind means, and what to do about it, is planner.py's business. This module only remembers.

Contract:
    recall(snap) -> dict | None      # {"kind", "action", "source": "site"|"lesson", "score"}
    remember(snap, kind, action, ok) # after the outcome is known
    lessons(limit) -> list[dict]     # for reading what has been learned
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone

from jobbot import db
from jobbot.apply import observe

log = logging.getLogger(__name__)

LESSON_MIN_WORDS = 3
LESSON_MATCH = 0.75       # share of a lesson's words that must appear on the page for it to apply
LESSON_WORDS = 24         # a lesson keeps its most distinctive words, not a whole page of copy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playbook_steps (
    host TEXT NOT NULL,
    sig TEXT NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',
    ok_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    example_url TEXT DEFAULT '',
    updated TEXT,
    PRIMARY KEY (sig, kind, action)
);
CREATE TABLE IF NOT EXISTS playbook_lessons (
    phrase TEXT NOT NULL,
    kind TEXT NOT NULL,
    ok_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    example_url TEXT DEFAULT '',
    updated TEXT,
    PRIMARY KEY (phrase, kind)
);
"""
_ready = False
_lock = threading.Lock()

# Words that say nothing about what kind of page it is. Kept out of lessons so "the", "your" and a company
# name shared by two unrelated pages cannot make them look alike.
_STOP = frozenset("""a an the and or of to in on for with at by from your you we our us is are be been this that
it its as if will can may please here there all any more about into out up so do not no yes # me my i""".split())


def _ensure() -> None:
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        with db.connect() as conn:
            conn.executescript(_SCHEMA)
        _ready = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z#]+", (text or "").lower()) if w not in _STOP and len(w) > 1]


def _phrase(snap: dict) -> str:
    """A lesson's key: the page's distinctive words, in order, deduplicated."""
    seen: list[str] = []
    for w in _words(observe.lesson_text(snap)):
        if w not in seen:
            seen.append(w)
        if len(seen) >= LESSON_WORDS:
            break
    return " ".join(seen)


def recall(snap: dict) -> dict | None:
    """What memory says this page is and what moved it on, or None. This site's own step first; failing
    that, the best lesson learned anywhere whose words this page carries."""
    try:
        _ensure()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT kind, action, ok_count, fail_count FROM playbook_steps WHERE sig = ? "
                "AND ok_count > fail_count ORDER BY ok_count - fail_count DESC, updated DESC LIMIT 1",
                (observe.signature(snap),)).fetchone()
            if row:
                return {"kind": row["kind"], "action": row["action"], "source": "site",
                        "score": row["ok_count"] - row["fail_count"]}
            lessons = conn.execute(
                "SELECT phrase, kind, ok_count, fail_count FROM playbook_lessons WHERE ok_count > fail_count"
            ).fetchall()
    except Exception as e:  # noqa: BLE001 - memory is an optimisation; a broken one must not stop a run
        log.warning("playbook: recall failed: %s", e)
        return None
    page = set(_words(observe.lesson_text(snap)))
    best, best_score = None, 0.0
    for les in lessons:
        words = les["phrase"].split()
        if len(words) < LESSON_MIN_WORDS:
            continue
        score = sum(w in page for w in words) / len(words)
        if score >= LESSON_MATCH and score > best_score:
            best, best_score = les, score
    if best is None:
        return None
    return {"kind": best["kind"], "action": "", "source": "lesson", "score": round(best_score, 2),
            "phrase": best["phrase"]}


def remember(snap: dict, kind: str, action: str = "", ok: bool = True, lesson: bool = True) -> None:
    """Record how a page turned out. `ok` is the outcome, not the plan: did the page move on (or the
    application go in) after `kind` was acted on?

    A lesson is only written for a success. A failure is counted against a lesson that already exists, so
    one that has started misleading stops being trusted, but a failure alone never creates one.
    """
    if not kind:
        return
    try:
        _ensure()
        sig, now, url = observe.signature(snap), _now(), (snap.get("url") or "")[:300]
        col = "ok_count" if ok else "fail_count"
        with db.connect() as conn:
            conn.execute(
                f"INSERT INTO playbook_steps (host, sig, kind, action, {col}, example_url, updated) "
                f"VALUES (?, ?, ?, ?, 1, ?, ?) ON CONFLICT (sig, kind, action) DO UPDATE SET "
                f"{col} = {col} + 1, updated = excluded.updated",
                (snap.get("host", ""), sig, kind, action or "", url, now))
            phrase = _phrase(snap)
            if lesson and len(phrase.split()) >= LESSON_MIN_WORDS:
                if ok:
                    conn.execute(
                        "INSERT INTO playbook_lessons (phrase, kind, ok_count, example_url, updated) "
                        "VALUES (?, ?, 1, ?, ?) ON CONFLICT (phrase, kind) DO UPDATE SET "
                        "ok_count = ok_count + 1, updated = excluded.updated",
                        (phrase, kind, url, now))
                else:
                    conn.execute("UPDATE playbook_lessons SET fail_count = fail_count + 1, updated = ? "
                                 "WHERE phrase = ? AND kind = ?", (now, phrase, kind))
        log.info("playbook: %s %r on %s%s", "learned" if ok else "counted a miss for", kind,
                 snap.get("host", ""), f" (pressed {action!r})" if action else "")
    except Exception as e:  # noqa: BLE001
        log.warning("playbook: could not record %r: %s", kind, e)


def forget_action(snap: dict, kind: str, action: str) -> None:
    """A replayed step that did not work: count it against so it stops being replayed."""
    remember(snap, kind, action, ok=False, lesson=False)


def lessons(limit: int = 100) -> list[dict]:
    try:
        _ensure()
        with db.connect() as conn:
            rows = conn.execute("SELECT * FROM playbook_lessons ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        log.warning("playbook: could not list lessons: %s", e)
        return []
