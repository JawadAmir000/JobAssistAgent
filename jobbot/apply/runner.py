"""Drives one application: browser lifecycle, adapter dispatch, status bookkeeping, pause/resume.

OWNER-THREAD CONTRACT — the one rule in this module:

    The thread that calls sync_playwright().start() owns every Playwright object for that application, and
    is the ONLY thread that may touch them.

Playwright's sync API is greenlet-based and thread-bound: a call from any other thread raises
greenlet.error("cannot switch to a different thread") instead of reaching the browser. Adapters swallow
probe failures, so such a call does not look like an error — it looks like an empty page, and the adapter
reports "form not found" on a form that is sitting right there.

Resume design: when an adapter raises NeedsHuman the browser stays open, the live objects are parked in
`_LIVE[app_id]`, and the owner thread STAYS ALIVE in `_serve`, waiting on `live["queue"]`. The web thread
that receives the user's answer never touches the page; `resume_application` only puts a command on that
queue. The owner then teaches the resolver the answer and re-runs `adapter.apply(ctx)` on the SAME page;
adapters are idempotent (they skip already-filled controls) so the re-run just continues.
If the server restarted (`_LIVE` empty) the answer is cached and the application starts over from scratch,
with the resuming thread becoming the new owner.
"""
from __future__ import annotations

import json
import os
import logging
import queue
import re
import threading
from datetime import datetime
from typing import Any, Callable

from jobbot import answers as store
from jobbot import config, db
from jobbot.apply import common
from jobbot.apply.base import AlreadyApplied, ApplyContext, ApplyError, NeedsHuman, get_adapter_for
from jobbot.apply.resolver import Resolver, normalize_question

log = logging.getLogger(__name__)

_LIVE: dict[int, dict[str, Any]] = {}
# Guards check-then-act sequences on _LIVE only (claiming a pause, evicting an entry, publishing a new one).
# Never held across a Playwright call, a thread join or a DB write.
_LOCK = threading.Lock()
# Held only around importlib.reload, so two applications starting at once cannot interleave re-imports of
# the same modules. Never taken with _LOCK: the reload does not touch _LIVE.
_RELOAD_LOCK = threading.Lock()

PARK_POLL_S = 5.0     # how often a parked owner re-checks that its _LIVE entry still exists
EVICT_JOIN_S = 10.0   # how long _evict waits for a foreign owner thread to close its own browser


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "unknown"


def _fact_values(facts: dict) -> set[str]:
    """Every scalar in facts.yaml, lowercased. A form value that matches one was filled from the facts file,
    not typed by the user, so it teaches the answers cache nothing."""
    out: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
        elif node is not None and not isinstance(node, bool):
            text = str(node).strip().lower()
            if text:
                out.add(text)

    walk(facts)
    return out


def make_seen(resolver: Resolver, answered_here: set[str], fact_values: set[str], app_id: int = 0,
              window_touched: Callable[[], bool] = lambda: False):
    """Build the ctx.seen callback: report a filled control, and learn it when the user filled it.

    A control that was already filled counts as the last answer given, so the conditional follow-up after it
    ("if yes, explain") still knows whether its condition was met.

    It is also the only record of an answer the user typed into the window by hand: jobbot was never asked
    the question, but the answer is sitting in the field. Cache it, so the next application does not stop to
    ask something already answered once.

    What is deliberately not learned, because none of it is the candidate answering anything:
      - a control still showing what the markup put in it (`default`/`placeholder`). This is the big one:
        an untouched country list resting on its first option taught the cache that this candidate's
        citizenship is Afghanistan, and a pre-ticked consent box taught it "Notification: Yes".
      - a value that is a placeholder in its own right ("Attach", "Select one", "-").
      - a field belonging to one row of a repeating section — an employer, a degree, a language. The answer
        is about that row, not about the candidate, so replaying it fills the next form with the wrong job.
      - a value that just echoes its own label.
      - anything jobbot itself answered on this form (`answered_here` — its own LLM text would be replayed
        at the next employer), anything the cache already covers, and any value that is simply a fact from
        facts.yaml (name, email, phone), which needs no cache entry and only invites a fuzzy mis-hit later.
    """
    learned_facts: dict[str, str] = {}      # identity fact -> the label that already rewrote it here

    def seen(value: str, label: str = "", *, kind: str = "", options: list | None = None,
             default: bool = False, placeholder: bool = False) -> None:
        if not value:
            return
        resolver.previous_answer = str(value)
        if not label:
            return
        if default or placeholder:
            log.debug("not learning %r: the form put %r there, not the user", label, str(value)[:40])
            return
        # A value that is nothing but punctuation ("-", "--", "n/a") normalises to the empty string, which
        # is the clearest possible sign that nobody answered anything.
        if not normalize_question(str(value)):
            return
        if normalize_question(str(value)) in store.PLACEHOLDER_VALUES:
            return
        if normalize_question(label) in store.ROW_FIELD_LABELS:
            log.debug("not learning %r: it belongs to one row, not to the candidate", label)
            return
        if normalize_question(str(value)) == normalize_question(label):
            return
        if str(value).strip().lower() in fact_values:
            return
        if normalize_question(label) in answered_here:
            return
        # An identity field the user corrected in the window (a LinkedIn URL the form would not take, a
        # different email). answers.json is the wrong home for it: every adapter fills these from
        # facts.yaml and never looks at the cache, so a correction left in the cache would be replaced by
        # the rejected value on the very next form. Write it where it is read from instead.
        key = _identity_fact_key(label)
        if key:
            if not window_touched():
                # Nobody has been near this window yet. On the first pass of a freshly opened form every
                # filled control was filled by the page — a tenant's own default, or an ATS profile echoing
                # a previous application — and none of it is the candidate correcting anything. The window
                # is only handed to the user when the run parks, so until it has parked once, "already
                # filled" cannot mean "filled by hand".
                #
                # Every corruption of facts.yaml so far came through here on an untouched window: "🇧🇩 +880"
                # into identity.location (122), "+880" into identity.phone (129) and into identity.country
                # (131), and — with those three all guarded by value — "United Arab Emirates" into
                # identity.country (136), read off the Oracle tenant's own default country. Guarding the
                # shape of the value only ever catches the last kind of wrong value; this catches the
                # reason they are all wrong. The answers cache is left alone: a bad entry there spoils one
                # question, and this file is what every adapter fills from.
                log.info("app %s: not writing %r to facts.yaml %s from %r: nobody has touched this window "
                         "yet, so the form put it there", app_id, str(value)[:60], key, label)
                return
            why = _not_a_correction(key, label, str(value).strip(),
                                    resolver.fact_str(key), learned_facts)
            if why:
                log.info("app %s: not writing %r to facts.yaml %s from %r: %s",
                         app_id, str(value)[:60], key, label, why)
                return
            if config.set_fact(key, str(value).strip()):
                log.info("app %s: %r corrected in the window -> facts.yaml %s = %r",
                         app_id, label, key, str(value)[:80])
                resolver.facts = config.load_facts()
                fact_values.add(str(value).strip().lower())
                learned_facts[key] = label
                return
        if resolver.knows(label):
            return
        log.info("app %s: learning %r from the form -> %r (typed)", app_id, label, str(value)[:60])
        resolver.learn(label, str(value), source="typed", kind=kind, options=list(options or []))
    return seen


def _not_a_correction(key: str, label: str, value: str, held: str, learned: dict[str, str]) -> str:
    """Why `value` in an identity field is the form's own doing rather than the candidate's — '' when it
    may be theirs and is safe to write to facts.yaml.

    Needed because this one learn-back is unlike every other: the answers cache is per-question and a bad
    entry there spoils one question, but facts.yaml is the source every adapter fills from, so a wrong
    value written here silently replaces a true fact for every application afterwards, and nothing
    downstream has anything left to compare it against. Two went this way in one evening --

      - application 122 read "🇧🇩 +880" out of a box labelled "Country dialing code" and stored it as
        identity.location, because "Country" falls past the anchored country rule into the location one;
      - application 129 read "+880" out of Oracle's phone widget -- the prefix the widget seeds itself,
        with no number behind it -- and stored it as identity.phone, so every application after it filled
        "+880" as the phone number until Cloudflare rejected it as too short (application 130);
      - application 131 read "+880" out of a picker labelled plainly "Country" -- an intl-tel-input or
        Oracle country-code control's accessible name is just that -- and stored it as identity.country.
        That one closed a loop: select_dial_country searches a picker's rows for the country's *name*, so
        with "+880" in the fact it could no longer drive the very kind of control it had learned from, and
        application 135 sent the whole number to a box beside a picker stuck on +971.

    So a value has to look like the fact it is replacing before it is allowed to replace it.
    """
    if store.is_about_the_field(label):
        return "the label asks about the shape of the field, not for the detail itself"
    if key == "identity.phone" and common.dial_code_only(value):
        return "a dial code is not a phone number"
    if key in ("identity.country", "identity.location") and common.is_dial_code(value):
        # Keyed on the value, because the label gives nothing away: the control that corrupted
        # identity.country was labelled "Country" and nothing else, which is_about_the_field passes and
        # should pass. `is_dial_code` rather than `dial_code_only` because a postcode is four digits too,
        # and "Dhaka 1207" is a location a candidate might really have typed.
        return "a dial code is not a country or a location"
    if key in learned and learned[key] != label:
        # Two labels writing one fact in a single pass is a control split in two ("Address Line 1" and
        # "Address Line 2" both land on identity.location), not the candidate changing their mind twice.
        return f"{key} was already taken from {learned[key]!r} on this form"
    if held and len(value) < len(held) and held.lower().startswith(value.lower()):
        return f"it is the start of the {key} already held ({held[:40]!r}) — a widget trimming our own value"
    return ""


def _identity_fact_key(label: str) -> str:
    """The `identity.*` fact this form label names, '' when it names none.

    Restricted to the identity block on purpose: those are the fields the adapters fill from facts.yaml
    without asking, so they are the only ones a correction in the window cannot otherwise reach.
    """
    try:
        from jobbot.apply.generic import GenericFormAdapter
        key = GenericFormAdapter._identity_key(label) or ""
    except Exception as e:  # noqa: BLE001
        log.debug("identity key lookup failed for %r: %s", label, e)
        return ""
    return key if key.startswith("identity.") else ""


def _llm_enabled() -> bool:
    try:
        from jobbot.llm import get_provider
        ok, _ = get_provider().is_configured()
        return bool(ok)
    except Exception:
        return False


def _session_path(ats: str, company: str, shared: bool = False):
    """Where this application's cookies live.

    Per (ats, company) normally: each employer's Greenhouse/Lever/Ashby board is its own site with its own
    cookies, and a captcha clearance earned on one says nothing about another. Adapters that sign into a
    single account covering every employer (LinkedIn -> needs_account) get one shared file instead; keying
    those per company asks the user for the same login once per employer and never reuses it.
    """
    name = f"{ats}.json" if shared else f"{ats}-{_slug(company)}.json"
    return config.SESSIONS_DIR / name


def _save_state(live: dict | None) -> None:
    """Persist cookies for this (ats, company). Also called when we pause for the user, so that a login they
    perform in the window (LinkedIn, say) survives into the next run instead of being asked for again."""
    if not live or live.get("context") is None:
        return
    try:
        live["context"].storage_state(path=str(live["session_path"]))
    except Exception as e:
        log.debug("storage_state save failed: %s", e)


def _owned_here(live: dict) -> bool:
    """True when this thread may touch `live`'s Playwright objects (see the owner-thread contract above).

    An entry with no owner recorded is treated as ours: hand-built entries in tests have none.
    """
    owner = live.get("owner")
    return owner is None or getattr(owner, "ident", None) == threading.get_ident()


def _warn_if_foreign(app_id: int, live: dict, what: str) -> None:
    """Log-only guard. Never raises: the callers are all on 'never raises' paths, and a loud log line plus a
    safe page_alive() failure is more useful than an exception thrown from a daemon thread."""
    if not _owned_here(live):
        log.error("app %s: %s ran on thread %r but the browser belongs to %r — Playwright calls will fail",
                  app_id, what, threading.current_thread().name, getattr(live.get("owner"), "name", "?"))


def _close(app_id: int, save_state: bool = False) -> None:
    live = _LIVE.pop(app_id, None)
    if not live:
        return
    _warn_if_foreign(app_id, live, "_close")
    try:
        if save_state:
            _save_state(live)
        for key in ("context", "browser"):
            obj = live.get(key)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        pw = live.get("playwright")
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
    except Exception as e:  # pragma: no cover
        log.debug("close failed: %s", e)


def _evict(app_id: int) -> None:
    """Drop a live entry from ANY thread.

    Only the owner can actually close a browser, so: our own entry is closed here; a foreign owner that is
    still alive is asked to close through its queue; a dead owner's handles are unreachable from anywhere and
    are dropped with a warning (its Chromium survives until the process exits). App ids are fresh per Apply
    click, so this is an edge case, not the normal path.
    """
    with _LOCK:
        live = _LIVE.get(app_id)
    if live is None:
        return
    if _owned_here(live):
        _close(app_id, save_state=True)
        return
    owner = live.get("owner")
    if owner is not None and owner.is_alive() and live.get("queue") is not None:
        live["queue"].put(("close",))
        owner.join(EVICT_JOIN_S)      # the lock is NOT held: the owner needs to pop the entry itself
    with _LOCK:
        if _LIVE.pop(app_id, None) is not None:
            log.warning("app %s: dropped browser handles owned by %r (%s); its Chromium may stay open",
                        app_id, getattr(owner, "name", "?"),
                        "still alive" if owner is not None and owner.is_alive() else "dead")


def has_live_browser(app_id: int) -> bool:
    """True when this application still owns an open window that a retry can re-run on."""
    with _LOCK:
        live = _LIVE.get(app_id)
    owner = live.get("owner") if live is not None else None
    return live is not None and live.get("parked") and (owner is None or owner.is_alive())


# Chromium as Playwright ships it announces itself: it runs with --enable-automation, reports
# navigator.webdriver = true, and is a build no ordinary visitor has. Cloudflare's managed challenge reads
# that and escalates from a token it grants silently to a checkbox somebody has to tick by hand — which is
# how a Workable submit ended up parked on "Verify you are human" with the button stuck on "Submitting...".
# The user's own Chrome, launched without the automation switch, is the same browser they would have applied
# in themselves, and the challenge usually passes without ever being shown.
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
_AUTOMATION_DEFAULTS = ["--enable-automation"]


def _launch_browser(pw: Any, headless: bool):
    """The user's installed Chrome where there is one, else the Chromium Playwright ships with.

    The fallback matters: a machine without Chrome must still be able to apply, so a missing channel costs
    a line in the log rather than the application.
    """
    try:
        browser = pw.chromium.launch(headless=headless, channel="chrome",
                                     args=_LAUNCH_ARGS, ignore_default_args=_AUTOMATION_DEFAULTS)
        log.info("browser: using the installed Chrome")
        return browser
    except Exception as e:  # noqa: BLE001 - no Chrome on this machine, or it would not start
        log.info("browser: Chrome unavailable (%s); using bundled Chromium", str(e)[:120])
    return pw.chromium.launch(headless=headless, args=_LAUNCH_ARGS,
                              ignore_default_args=_AUTOMATION_DEFAULTS)


def _screenshot_fn(app_id: int, page_ref: dict):
    def screenshot(label: str) -> str:
        """Returns the filename only if the file was actually written — callers store it on the application,
        and a name for a file that never landed renders a broken 'screenshot' link in the UI."""
        fname = f"{app_id}-{_slug(label)}.png"
        try:
            page = page_ref.get("page")
            if page is None:
                return ""
            page.screenshot(path=str(config.SCREENSHOTS_DIR / fname), full_page=False)
            db.update_application(app_id, screenshot=fname)
        except Exception as e:
            log.debug("screenshot failed: %s", e)
            return ""
        return fname
    return screenshot


def _page_change_handler(live: dict, page_ref: dict):
    """Keep every runner-owned reference pointed at the page the adapter is currently driving."""
    def switch(page) -> None:
        page.set_default_timeout(8000)
        page_ref["page"] = page
        live["page"] = page
    return switch


def _ensure_page_tracking(app_id: int, live: dict) -> None:
    """Add page-change tracking to contexts created before this code was hot-reloaded."""
    ctx = live.get("ctx")
    if ctx is None or not hasattr(ctx, "page"):
        return
    page_ref = live.get("page_ref")
    if page_ref is None:
        page_ref = {"page": live.get("page")}
        live["page_ref"] = page_ref
        screenshot = _screenshot_fn(app_id, page_ref)
        live["screenshot"] = screenshot
        ctx.screenshot = screenshot
    ctx.on_page_change = _page_change_handler(live, page_ref)


def _shot_field(shot: str) -> dict:
    """Only overwrite the stored screenshot when a new one was really captured — a failed capture must not
    wipe the earlier 'needs you' shot, which is usually the one worth looking at."""
    return {"screenshot": shot} if shot else {}


def _note_issue(app_id: int, live: dict | None, reason: str) -> str:
    """Record the blocker and say how often it has happened, for the message the user actually reads.

    A reason seen once is this application's problem; the same reason seen six times is the adapter's, and
    that distinction was previously buried in a log nobody reads mid-run.
    """
    try:
        job = getattr((live or {}).get("ctx"), "job", None) or {}
        seen = db.record_issue(job.get("ats", ""), job.get("company", ""), reason)
    except Exception as e:  # noqa: BLE001
        log.debug("app %s: issue not recorded: %s", app_id, e)
        return reason
    if seen > 1:
        log.info("app %s: this blocker has now happened %d times", app_id, seen)
        return f"{reason} (seen {seen} times)"
    return reason


def _finish_needs_human(app_id: int, e: NeedsHuman, screenshot) -> None:
    live = _LIVE.get(app_id)
    _save_state(live)
    if live is not None and live.get("page") is not None:
        # Surface the window: what is being asked for (a captcha, a login) can only be done in it.
        try:
            live["page"].bring_to_front()
        except Exception:
            pass
    if live is not None:
        # Set before the DB write, so anyone who sees needs_you in the DB also sees a claimable pause.
        live["parked"] = True
    db.update_application(
        app_id, status="needs_you", reason=_note_issue(app_id, live, str(e.reason))[:500],
        # real_options, not e.options: offering the list's own "Select One" back to the candidate is how
        # it came to be stored as the answer to "Phone Device Type" (see answers.real_options).
        pending_question=e.question or "", pending_options=json.dumps(store.real_options(e.options)),
        step="Waiting for you", **_shot_field(screenshot("needs-you")),
    )


def _finish_failed(app_id: int, e: Exception, screenshot) -> None:
    """Record the failure and park, leaving the window open.

    The form on screen is usually most of the way filled and the fix is often something only a person can do
    in it. Closing here would throw that away and make Retry start over on an empty form, so the browser
    stays and Retry re-runs the adapter on it. With no live browser (setup failed before there was one)
    there is nothing to park and the entry is dropped as before.
    """
    live = _LIVE.get(app_id)
    _save_state(live)
    if live is not None:
        live["parked"] = True     # set before the DB write: whoever sees "failed" also sees a claimable pause
    db.update_application(app_id, status="failed", reason=_note_issue(app_id, live, str(e))[:500],
                          finished_at=_now(), pending_question="", pending_options="",
                          **_shot_field(screenshot("failed")))
    if live is None:
        _close(app_id)


def _finish_submitted(app_id: int, screenshot, reason: str = "") -> None:
    # Capture the confirmation page before the browser goes: it is the only proof of what was submitted.
    db.update_application(app_id, status="submitted", reason=reason[:500], step="Submitted",
                          finished_at=_now(), pending_question="", pending_options="",
                          **_shot_field(screenshot("submitted")))
    _close(app_id, save_state=True)


# A vendor adapter that never got as far as a form. The page is still on screen and nothing has been
# submitted, so the generic walker — which finds fields by their visible label rather than by this vendor's
# ids — is worth one try before the application is given up on. Greenhouse, Lever and Ashby each rewrote
# their markup at some point and each produced exactly this: a dead adapter on a form a human (and the
# generic walker) could see perfectly well.
_FORM_NOT_FOUND_RE = re.compile(
    r"\bform\b[^.]{0,40}\bnot found\b|\bno\b[^.]{0,40}\bform\b[^.]{0,40}\b(?:appeared|found)\b"
    r"|\bno form fields\b|\bpage did not load\b|\bsubmit button not found\b"
    r"|\bnot currently automated\b", re.I)


def _generic_fallback(ctx: Any, err: Exception):
    """The generic walker to retry `err` with, or None when a retry would be wrong.

    Never after a generic walk (ctx.extra carries the flag it sets, through a LinkedIn hand-off as well),
    never on a dead page, and only for a failure that says the adapter never reached the form — anything
    later than that may have submitted something, and a second walker must not risk sending it twice.
    """
    if ctx is None or ctx.extra.get("generic_walked") or not _FORM_NOT_FOUND_RE.search(str(err)):
        return None
    if not common.page_alive(getattr(ctx, "page", None)):
        return None
    from jobbot.apply.base import get_adapter_for as _get       # re-imported: adapters hot-reload
    return _get("generic")


def _drive(app_id: int, live: dict) -> None:
    """Run adapter.apply on the live context and translate the outcome into application status."""
    adapter, ctx, screenshot = live["adapter"], live["ctx"], live["screenshot"]
    try:
        try:
            adapter.apply(ctx)
        except ApplyError as e:
            fallback = _generic_fallback(ctx, e)
            if fallback is None:
                raise
            log.info("app %s: %s — trying the generic walker on the same page", app_id, e)
            ctx.step("Trying the generic form walker")
            try:
                fallback.apply(ctx)
            except AlreadyApplied:
                raise       # the walker got far enough to read the employer's own notice; that is the news
            except ApplyError as second:
                # Report what the adapter that knows this board said; the walker's own "no form here" is
                # the more generic of the two and usually the less informative.
                log.info("app %s: the generic walker also stopped: %s", app_id, second)
                raise e from second
    except NeedsHuman as e:
        log.info("app %s needs human: %s", app_id, e.reason)
        _finish_needs_human(app_id, e, screenshot)
        return
    except AlreadyApplied as e:
        # The application is with the employer already. Recording it as a failure put a card in the failed
        # pile whose Retry button could only ever fetch the same notice back.
        log.info("app %s: already applied — %s", app_id, e)
        _finish_submitted(app_id, screenshot, reason=f"already applied — {e}")
        return
    except ApplyError as e:
        log.warning("app %s failed: %s", app_id, e)
        _finish_failed(app_id, e, screenshot)
        return
    except Exception as e:
        log.exception("app %s crashed", app_id)
        _finish_failed(app_id, e, screenshot)
        return
    _finish_submitted(app_id, screenshot)


def _refresh_adapter(app_id: int, live: dict) -> None:
    """Pick up adapter code edited since this application paused, without touching its browser.

    The reason a paused run keeps its window open is that the form in it is most of the way filled — the
    account signed into, the CV uploaded, the answers given. Restarting the server to load a fix destroys
    exactly that, so the fix is loaded in place instead and the retry continues on the same page.

    facts.yaml is re-read for the same reason. The window is kept open precisely so the user can fix what
    the form rejected before pressing Retry, and the fix for a rejected name, phone or URL belongs in
    facts.yaml — but the resolver read that file once, when the application started, so without this the
    retry replays the very value the form just refused. (Application 130 was rejected on a phone number
    that facts.yaml had been made to hold; correcting the file and pressing Retry changed nothing.)

    Never raises: running the retry with the code already in memory is worse than running it with the fix,
    but it is far better than losing the window.
    """
    _ensure_page_tracking(app_id, live)
    try:
        resolver = live.get("resolver")
        if resolver is not None:
            resolver.facts = config.load_facts()
    except Exception as e:  # noqa: BLE001
        log.warning("app %s: could not re-read facts.yaml (%s); retrying with the facts already loaded",
                    app_id, e)
    try:
        from jobbot.apply.base import get_adapter_for, reload_adapters
        names = reload_adapters()
        ats = (live["ctx"].job.get("ats") if live.get("ctx") is not None else "") or "other"
        adapter = get_adapter_for(ats)
        if adapter is not None:
            live["adapter"] = adapter
        log.info("app %s: reloaded %d adapter modules, retrying on the open window", app_id, len(names))
    except Exception as e:  # noqa: BLE001
        log.warning("app %s: could not reload adapters (%s); retrying with the code already loaded",
                    app_id, e)


def _serve(app_id: int, live: dict) -> None:
    """Own this application until it is finished: drive the adapter, then stay parked on the queue.

    This is what keeps the owner thread alive across a pause. While parked the thread is blocked in
    `queue.get`, so the Playwright objects it created stay usable — every resume runs here, on this thread,
    never on the web thread that received the answer. Returns once the _LIVE entry is gone (submitted,
    failed, closed or evicted). If the user never answers, this blocks forever and the browser window stays
    open; the thread is a daemon, so it dies with the process. That is the same bargain as before.
    """
    _warn_if_foreign(app_id, live, "_serve")
    _drive(app_id, live)
    q = live["queue"]
    while _LIVE.get(app_id) is live:    # _finish_failed / _finish_submitted pop the entry via _close
        try:
            cmd = q.get(timeout=PARK_POLL_S)
        except queue.Empty:
            continue                    # loop back to re-check the entry: an _evict may have dropped us
        try:
            if cmd[0] == "close":
                _close(app_id, save_state=True)
                return
            if cmd[0] != "resume":
                log.error("app %s: unknown command %r", app_id, cmd)
                continue
            _, question, answer = cmd
            # The user has now had this window in front of them: whatever they typed into it is
            # theirs, and from here on a filled control may be learned back into facts.yaml.
            live["human_touched"] = True
            if question and answer:
                # Learned here rather than in the dispatcher so the resolver's dict is only ever touched by
                # the thread that reads it.
                live["resolver"].learn(question, answer)
            if live.get("ctx") is None or not common.page_alive(live.get("page")):
                # The parked browser died while we waited (machine slept, user closed the window — a pause
                # can last hours). The answer is cached, so a fresh run simply replays it.
                log.info("app %s: parked browser is gone, starting fresh", app_id)
                _close(app_id)          # on the owner thread, so this really does close it
                # No window left to protect, so the fix the user made while we waited is loaded here just
                # as _refresh_adapter would have loaded it onto a window that survived.
                _reload_before_a_fresh_start(app_id)
                _run_application(app_id)  # new Playwright on THIS thread; it runs its own _serve
                return
            try:
                live["page"].bring_to_front()
            except Exception:
                pass
            # Whatever the user just did in the window may have earned a cookie worth keeping — solving a
            # captcha issues a clearance cookie that spares the next application on the same board. Bank it
            # now rather than only on the next pause, so a later failure cannot lose it.
            _save_state(live)
            _refresh_adapter(app_id, live)   # a fix made while we waited lands on this same page
            _drive(app_id, live)        # NeedsHuman -> entry stays, we park again; otherwise the loop ends
        except Exception as e:          # belt and braces: this runs on a daemon thread with no caller
            log.exception("app %s: resume crashed", app_id)
            _finish_failed(app_id, e, live["screenshot"])
            return


def _reload_before_a_fresh_start(app_id: int) -> None:
    """Re-import the adapter modules before building a new browser for this application.

    `_refresh_adapter` does this for a retry that keeps its parked window. Everything else — a brand-new
    application, and a retry whose window died while it waited — came through here and ran whatever the
    server imported when it started, which may be hours or days old. That is not a theoretical gap: a fix
    for "could not find an application form" (application 118, Deloitte NZ on SmartRecruiters) was verified
    against the live page, and Retry reproduced the identical blocker because the parked browser had gone,
    this path was taken, and the server was still executing the generic.py it had imported that morning.

    Safe here in a way it would not be mid-run: this is the moment before the application has a browser, a
    context or an adapter of its own. Never raises — starting with stale code beats not starting.
    """
    with _RELOAD_LOCK:
        try:
            from jobbot.apply.base import reload_adapters
            log.info("app %s: reloaded %d adapter modules", app_id, len(reload_adapters()))
        except Exception as e:  # noqa: BLE001
            log.warning("app %s: could not reload adapters (%s); starting with the code already loaded",
                        app_id, e)


def run_application(app_id: int) -> None:
    """Blocking for the whole life of the application, pauses included. Never raises.

    The caller's thread becomes the owner of this application's browser (see the contract at the top), so it
    must be a thread that can sit idle: the web app gives each one its own.
    """
    try:
        _reload_before_a_fresh_start(app_id)
        _run_application(app_id)
    except Exception as e:  # last-resort guard
        log.exception("run_application(%s) escaped: %s", app_id, e)
        try:
            db.update_application(app_id, status="failed", reason=str(e)[:500], finished_at=_now())
        except Exception:
            pass
        _close(app_id)


def _run_application(app_id: int) -> None:
    _evict(app_id)
    app = db.get_application(app_id)
    if app is None:
        log.error("application %s not found", app_id)
        return
    job_row = db.get_job(app["job_id"])
    if job_row is None:
        db.update_application(app_id, status="failed", reason="Job not found", finished_at=_now())
        return
    job = dict(job_row)

    cv_path = config.get_setting(config.SETTING_CV_PATH) or ""
    if not cv_path or not os.path.exists(cv_path):
        db.update_application(app_id, status="failed", reason="Upload a CV in Settings first", finished_at=_now())
        return

    adapter = get_adapter_for(job.get("ats") or "other")
    if adapter is None:
        db.update_application(app_id, status="manual", reason=f"No adapter for {job.get('ats')}", finished_at=_now())
        return

    facts = config.load_facts()
    # Records carry where each remembered answer came from, which is what decides whether it is allowed to
    # settle a given question; the flat map is the same answers as plain text, for the fuzzy scans.
    records = config.load_answer_records()
    answers = store.text_view(records)
    # Re-imported rather than used from the module-level binding, for the same reason get_adapter_for is
    # (see _drive): reload_adapters() rebuilds jobbot.apply.resolver, but runner is deliberately NOT
    # reloadable, so the name bound here at import time still points at the class object from before the
    # reload. Every edit to the resolver was therefore loaded and then ignored — application 146 paused to
    # ask "What is highest level of education you have completed?" with the rule that answers it already
    # written, saved and reloaded.
    from jobbot.apply.resolver import Resolver as _Resolver
    resolver = _Resolver(facts, answers, job, llm_enabled=_llm_enabled(),
                         cv_text=config.load_cv_text(cv_path), records=records)
    db.update_application(app_id, status="running", step="Launching browser", reason="",
                          pending_question="", pending_options="")

    # Questions jobbot answered itself on this form, and every value facts.yaml already carries: between
    # them they separate "the user typed this" from "we put it there", which is what seen() learns on.
    answered_here: set[str] = set()
    fact_values = _fact_values(facts)

    page_ref: dict = {}
    screenshot = _screenshot_fn(app_id, page_ref)
    headless = (config.get_setting(config.SETTING_HEADLESS) or "0") == "1"
    session_path = _session_path(job.get("ats", "other"), job.get("company", ""),
                                 shared=getattr(adapter, "needs_account", False))

    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
    except Exception as e:
        db.update_application(app_id, status="failed", reason=f"Playwright unavailable: {e}"[:500], finished_at=_now())
        return

    me = threading.current_thread()
    if me is not threading.main_thread():
        me.name = f"app-{app_id}"       # so "[app-19]" in the log names the owner of every line
    live: dict[str, Any] = {"playwright": pw, "session_path": session_path, "screenshot": screenshot,
                            "adapter": adapter, "resolver": resolver,
                            "owner": me,              # only this thread may touch the Playwright objects
                            "queue": queue.Queue(),   # ("resume", question, answer) | ("close",)
                            "parked": False}          # True while waiting for the user; claimed on resume
    with _LOCK:
        _LIVE[app_id] = live
    try:
        browser = _launch_browser(pw, headless)
        live["browser"] = browser
        # An empty grant denies every permission outright, so the site is never asked and Chrome never
        # draws the bubble. That bubble is browser chrome, not page content: nothing on the page can
        # dismiss it, Playwright cannot click it, it covers the top-left of the viewport and it swallows
        # real clicks aimed at whatever is under it. Oracle Recruiting asks for location on the apply
        # page (seen on Westpac's careers site), and a job application has no use for the answer.
        kwargs: dict = {"viewport": {"width": 1280, "height": 900}, "permissions": []}
        if session_path.exists():
            kwargs["storage_state"] = str(session_path)
        context = browser.new_context(**kwargs)
        live["context"] = context
        page = context.new_page()
        page.set_default_timeout(8000)
        page_ref["page"] = page
        live["page_ref"] = page_ref
        live["page"] = page

        def step(text: str) -> None:
            log.info("app %s: %s", app_id, text)
            try:
                db.update_application(app_id, step=text[:200])
            except Exception:
                pass

        def answer(question: str, options: list[str] | None = None, kind: str = "text") -> str:
            answered_here.add(normalize_question(question))
            return resolver.answer(question, options, kind)

        # facts.yaml is only written from a window the user has actually had in front of them —
        # see make_seen. `live` is the dict that survives a park, so this reads the flag live
        # rather than capturing its value at startup, when it is always False.
        seen = make_seen(resolver, answered_here, fact_values, app_id,
                         window_touched=lambda: bool(live.get("human_touched")))

        ctx = ApplyContext(job=job, page=page, facts=facts, cv_path=cv_path, step=step, answer=answer,
                           screenshot=screenshot, seen=seen,
                           on_page_change=_page_change_handler(live, page_ref))
        live["ctx"] = ctx

        step("Opening job page")
        page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1000)
    except Exception as e:
        log.exception("app %s: browser setup failed", app_id)
        _finish_failed(app_id, e, screenshot)
        return

    _serve(app_id, live)


def resume_application(app_id: int, answer: str) -> None:
    """After the user answered the pending question (or solved a captcha). Never raises.

    Runs on the web thread and therefore NEVER touches a Playwright object: when the owner thread is alive
    the work is handed to it through its queue. Only when there is no owner left (the server restarted, say)
    does this thread start a fresh run and become the owner itself.
    """
    try:
        _resume_application(app_id, answer)
    except Exception as e:
        log.exception("resume_application(%s) escaped: %s", app_id, e)
        try:
            db.update_application(app_id, status="failed", reason=str(e)[:500], finished_at=_now())
        except Exception:
            pass
        _evict(app_id)      # not _close: the browser may belong to another thread


def _resume_application(app_id: int, answer: str) -> None:
    app = db.get_application(app_id)
    if app is None:
        log.error("application %s not found", app_id)
        return
    question = app["pending_question"] or ""
    answer = str(answer or "").strip()

    with _LOCK:
        # Check-and-claim has to be atomic: two clicks on "Answer & continue" would otherwise queue two
        # answers, and the second would be replayed against whatever question came next.
        live = _LIVE.get(app_id)
        owner = live.get("owner") if live is not None else None
        alive = owner is not None and owner.is_alive()
        claimed = False
        if live is not None and alive and live.get("parked"):
            live["parked"] = False
            claimed = True
        if live is not None and not alive:
            _LIVE.pop(app_id, None)   # the owner is gone; its handles cannot be closed from here

    if live is not None and alive:
        if not claimed:
            log.warning("app %s: answer ignored, the application is not waiting for one", app_id)
            return
        db.update_application(app_id, status="running", reason="", pending_question="", pending_options="",
                              step="Resuming")
        live["queue"].put(("resume", question, answer))
        return

    # No usable owner: cache the answer here so the fresh run finds it, then run it on this thread.
    if live is not None:
        log.warning("app %s: owner thread is gone, starting fresh (its browser may still be open)", app_id)
    else:
        log.info("app %s: no live browser, starting fresh", app_id)
    if question and answer:
        from jobbot.apply.resolver import Resolver as _Resolver   # re-imported: the resolver hot-reloads
        _Resolver({}, {}, {}, False).learn(question, answer, source="human")
    db.update_application(app_id, status="running", reason="", pending_question="", pending_options="",
                          step="Resuming")
    _reload_before_a_fresh_start(app_id)
    _run_application(app_id)
