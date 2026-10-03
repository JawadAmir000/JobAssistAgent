"""Apply all: work through the jobs on screen one at a time, retrying failures, skipping what cannot be done.

Each application still runs on a thread of its own, exactly as a click on Apply does — run_application blocks
for the whole life of the application, pauses included, so the queue cannot run it inline. The queue only
starts one, watches its row, and decides what happens next:

  submitted / manual  -> next job
  needs_you           -> leave it parked (its card in Applications can still be answered) and move on
  failed              -> Retry on the same window, up to MAX_ATTEMPTS, then skip
  still running after ATTEMPT_TIMEOUT_S -> skip it and move on; it keeps running on its own thread

One batch at a time. The state lives in this process only; a restart ends the batch.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from jobbot import db

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3            # first run + 2 retries
ATTEMPT_TIMEOUT_S = 15 * 60
POLL_S = 2.0
DONE = ("submitted", "manual", "needs_you", "failed")

_LOCK = threading.Lock()
_STATE: dict = {"running": False, "stop": False, "items": [], "started_at": "", "finished_at": ""}


def snapshot() -> dict:
    with _LOCK:
        return {**_STATE, "items": [dict(i) for i in _STATE["items"]]}


def start(jobs: list) -> bool:
    """Queue these job rows. False when a batch is already running."""
    with _LOCK:
        if _STATE["running"]:
            return False
        _STATE.update(running=True, stop=False, finished_at="",
                      started_at=datetime.now().strftime("%H:%M:%S"),
                      items=[{"job_id": j["id"], "company": j["company"], "title": j["title"],
                              "app_id": None, "status": "queued", "attempts": 0, "note": ""} for j in jobs])
    threading.Thread(target=_run, name="apply-all", daemon=True).start()
    return True


def stop() -> None:
    with _LOCK:
        _STATE["stop"] = True


def _set(item: dict, **fields) -> None:
    with _LOCK:
        item.update(fields)


def _spawn(fn, *args) -> None:
    from jobbot.web.app import _safe_run
    threading.Thread(target=_safe_run, args=(fn, *args), daemon=True).start()


def _wait(app_id: int) -> str:
    """Block until the application leaves 'running'; returns its status, or 'timeout'."""
    deadline = time.monotonic() + ATTEMPT_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(POLL_S)
        a = db.get_application(app_id)
        if a is None:
            return "failed"
        if a["status"] in DONE:
            return a["status"]
        if _STATE["stop"]:
            return "stopped"
    return "timeout"


def _apply_one(item: dict) -> None:
    from jobbot.apply import resume_application, run_application

    existing = db.get_application_for_job(item["job_id"])
    if existing is not None and existing["status"] in ("submitted", "running", "pending", "needs_you"):
        _set(item, status="skipped", app_id=existing["id"], note=f"already {existing['status'].replace('_', ' ')}")
        return

    app_id = db.create_application(item["job_id"])
    _set(item, app_id=app_id)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _set(item, status="running", attempts=attempt)
        db.update_application(app_id, status="running", step="starting" if attempt == 1 else "retrying")
        if attempt == 1:
            _spawn(run_application, app_id)
        else:
            # Same as the Retry button: re-runs the (reloaded) adapter on the window it already has open,
            # or starts fresh on this row when that window is gone.
            _spawn(resume_application, app_id, "")
        result = _wait(app_id)
        reason = (db.get_application(app_id)["reason"] or "")[:200]
        if result == "submitted":
            _set(item, status="submitted", note=reason)
            return
        if result == "manual":
            _set(item, status="skipped", note=reason or "no adapter for this ATS")
            return
        if result == "needs_you":
            _set(item, status="needs_you", note=reason or "waiting for your answer")
            return
        if result in ("timeout", "stopped"):
            _set(item, status="skipped", note="still running after 15 min" if result == "timeout" else "stopped")
            return
        log.info("apply-all: app %s failed on attempt %d: %s", app_id, attempt, reason)
        _set(item, note=reason)
    _set(item, status="skipped", note=f"failed {MAX_ATTEMPTS}× — {item['note']}")


def _run() -> None:
    try:
        for item in _STATE["items"]:
            if _STATE["stop"]:
                if item["status"] == "queued":
                    _set(item, status="skipped", note="stopped")
                continue
            try:
                _apply_one(item)
            except Exception as e:  # noqa: BLE001 — one bad job never ends the batch
                log.exception("apply-all: %s crashed", item["job_id"])
                _set(item, status="skipped", note=f"error: {e}"[:200])
    finally:
        with _LOCK:
            _STATE.update(running=False, finished_at=datetime.now().strftime("%H:%M:%S"))
