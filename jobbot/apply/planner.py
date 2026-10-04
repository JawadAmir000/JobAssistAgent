"""What to do on a page the walker cannot place: work out what kind of page it is, act on that, and remember.

The walker's rules describe pages somebody has already watched a board produce. Every new board brings a page
none of them fits — a "check your email for a link" that is worded differently, a sign-in shown as a modal, a
"Review & send" whose button is called "Looks good" — and each used to end the run with "found neither a
Submit nor a Next button" or "could not find an application form". Those are not hard pages. They are pages
of a kind jobbot already knows how to handle, that it failed to *recognise*.

So when the walker is stuck it calls unstick(), which:

  1. looks (observe.snapshot);
  2. classifies, cheapest first: the page's own signals (a code box, a captcha, a thank-you), then memory —
     this site's step, then a lesson learned on any site (playbook.recall) — and only then the model;
  3. acts through the handler for that kind, the same code the walker uses when its own rules fire;
  4. checks the result on the page itself and writes it to the playbook: a step that moved on is replayed
     next time without the model, and a page kind recognised once is recognised on the next site that
     words it the same way.

The model never types an answer and never writes a selector. It names a kind, and when a control has to be
pressed, the number of one of the controls actually on the page (navigator's menu, with its deny list), so
the worst a bad reply can do is press what a person could have pressed. Its verdict is only remembered once
the page has confirmed it by moving on.

Contract:
    unstick(ctx, goal, fill) -> "submitted" | "moved" | ""   # "" = nothing it knows to do; raises for
                                                             # terminal kinds and for pauses (NeedsHuman)
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from jobbot.apply import account, common as c, navigator, observe, playbook
from jobbot.apply.base import AlreadyApplied, ApplyContext, ApplyError, NeedsHuman

log = logging.getLogger(__name__)

MAX_MODEL_CALLS = 6      # per application run; a page that loops cannot run up a bill
MAX_PRESSES = 6
SETTLE_MS = 3000
LINK_LOOKBACK_S = 900

# What a page can be. The handlers below are keyed on these, and so is the playbook.
KINDS = {
    "email_link": "the site emailed the candidate a link (sign-in / magic / verify link) and waits for it to be opened",
    "verification_code": "the site emailed or texted a one-time code and shows a box to type it into",
    "account": "a sign-in or create-account page standing in front of the application",
    "sso": "the candidate's own Google / Microsoft / Apple / GitHub / Okta sign-in page (not the employer's)",
    "captcha": "a captcha or human-verification challenge",
    "confirmation": "the application has been sent; a thank-you / received / submitted page",
    "already_applied": "the site says this candidate has already applied to this job",
    "closed": "the job is closed, expired, or no longer accepting applications",
    "press": "the application continues by pressing one of the listed controls (apply, next, continue, "
             "review, submit, confirm...)",
    "form_step": "a step with questions still unanswered that must be filled by hand before moving on",
    "not_application": "not part of an application: a job list, a company homepage, an error or 404 page",
}
# Kinds whose handling does not depend on which button a site happens to draw, so what is learned on one site
# holds on the next. A "press" is always tied to its own site's control, so it is never a cross-site lesson.
PORTABLE = {"email_link", "verification_code", "account", "sso", "captcha", "confirmation", "already_applied", "closed",
            "not_application"}


def unstick(ctx: ApplyContext, goal: str, fill: Callable[[], None] | None = None) -> str:
    """Get the application past the current page. See the module docstring for the outcomes."""
    page = ctx.page
    if recover_network_error(page):
        return "moved"
    before = _settled_snapshot(page)
    verdict = _classify(ctx, before, goal)
    if not verdict:
        return ""
    kind, press, source = verdict["kind"], verdict.get("press"), verdict["source"]
    log.info("planner: page is %r (from %s)%s — %s", kind, source,
             f", press {press!r}" if press else "", verdict.get("reason", "")[:160])
    ctx.step(f"Recognised this page as: {kind.replace('_', ' ')}")
    try:
        outcome = _act(ctx, before, verdict, fill)
    except (NeedsHuman, ApplyError) as e:
        # A pause or a terminal answer. The verdict is kept for a portable kind the page itself confirmed,
        # because "this is a captcha / closed posting" is right whatever the user does next.
        if kind in PORTABLE and source == "signal":
            playbook.remember(before, kind, ok=True)
        raise e
    ok = outcome in ("submitted", "moved")
    if source != "signal" or ok:
        playbook.remember(before, kind, press or "", ok=ok, lesson=kind in PORTABLE)
    return outcome


NETWORK_RETRIES = 3


def recover_network_error(page) -> bool:
    """Reload past Chrome's own network-error page; True when a real page came back.

    A dropped connection is not a verdict on the job. EY's careers site answered one press with
    ERR_HTTP2_PROTOCOL_ERROR, the model read chrome-error://chromewebdata/ as "not part of an application",
    and the run failed (application 299). Reloading the error page re-requests the URL that failed; when
    that keeps failing, going back returns to the page whose press led here, so it can be pressed again.
    """
    def broken() -> bool:
        try:
            return (page.url or "").startswith("chrome-error://")
        except Exception:  # noqa: BLE001
            return False
    if not broken():
        return False
    for attempt in range(NETWORK_RETRIES):
        log.info("planner: network error page; reloading (attempt %d)", attempt + 1)
        try:
            page.wait_for_timeout(2000 * (attempt + 1))
            page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception as e:  # noqa: BLE001
            log.debug("planner: reload failed: %s", e)
        if not broken():
            return True
    try:
        page.go_back(wait_until="domcontentloaded", timeout=30000)
    except Exception:  # noqa: BLE001
        pass
    return not broken()


_LOADING_RE = re.compile(r"^\W*(?:loading|please wait|one moment|just a moment)\b|\bloading\s*(?:\.{3}|…)", re.I)
SETTLE_LIMIT_MS = 20000


def _still_loading(snap: dict) -> bool:
    """A page drawn before its content: nothing to fill or press, and either next to no text or a spinner's
    own words. SuccessFactors shows "Loading..." for several seconds after Apply, and judged in that state it
    was called a confirmation once and "not an application" once (applications 215 and 218)."""
    if snap.get("fields"):
        return False
    text = (snap.get("text") or "").strip()
    if _LOADING_RE.search(text[:400]):
        return True     # a header link ("Employee Login") around a spinner is still a spinner
    return len(text) < 200 and len(snap.get("controls") or []) <= 3


def _settled_snapshot(page: Any) -> dict:
    snap = observe.snapshot(page)
    waited = 0
    while _still_loading(snap) and waited < SETTLE_LIMIT_MS:
        try:
            page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001 - closed: judge what we have
            break
        waited += 1500
        snap = observe.snapshot(page)
    if waited:
        log.info("planner: waited %.1fs for the page to finish loading", waited / 1000)
    return snap


# ---------- classify ----------
def _classify(ctx: ApplyContext, snap: dict, goal: str) -> dict | None:
    sig = snap.get("signals") or {}
    # 1. The page's own signals. Deterministic and already trusted by the walker.
    for name, kind in (("confirmation", "confirmation"), ("already_applied", "already_applied"),
                       ("captcha", "captcha"), ("bot_block", "captcha"), ("sso", "sso"), ("email_link", "email_link"),
                       ("verification_code", "verification_code"), ("account_gate", "account")):
        if sig.get(name):
            return {"kind": kind, "source": "signal", "reason": str(sig[name])[:160]}
    # 2. Memory. A site step carries the control that moved it on last time, which must still be on the page.
    mem = playbook.recall(snap)
    if mem:
        if mem["kind"] == "press":
            name = mem.get("action", "")
            if any(ctl["name"].lower() == name.lower() for ctl in snap["controls"]):
                return {"kind": "press", "press": name, "source": "memory", "reason": "pressed here before"}
            log.info("planner: remembered control %r is not on this page any more", name)
        elif mem["kind"] in ("confirmation", "already_applied"):
            # Never from memory alone: a page wrongly learned as a thank-you (Avanade's pre-apply redirect
            # modal, 338/350) then "confirmed" every later visit without looking (371/372). The live signals
            # above already said no, so fall through to the model.
            log.info("planner: memory says %r here but the page's own signals do not; not trusting it", mem["kind"])
        elif mem["kind"] in KINDS:
            return _apply_over_verdict(snap, {"kind": mem["kind"], "source": "memory",
                    "reason": f"learned {mem['source']}" + (f" ({mem['phrase'][:60]})" if mem.get('phrase') else "")})
    # 3. The model.
    return _apply_over_verdict(snap, _ask_model(ctx, snap, goal))


def _apply_over_verdict(snap: dict, verdict: dict | None) -> dict | None:
    # A job page with its Apply button below the fold reads as a description and nothing else, and the model
    # called Lenovo's "not an application" with an "Apply" link in the very menu it was shown (application
    # 336). A plain Apply control outranks that verdict: pressing it is how every application starts.
    if verdict and verdict["kind"] in ("not_application", "form_step") and not snap.get("fields"):
        apply_ctl = next((ctl["name"] for ctl in snap.get("controls") or [] if _APPLY_CONTROL_RE.match(ctl["name"])), None)
        if apply_ctl:
            log.info("planner: model said %r but the page offers %r; pressing it", verdict["kind"], apply_ctl)
            return {"kind": "press", "press": apply_ctl, "source": "rule", "reason": "an Apply control is on the page"}
    return verdict


_APPLY_CONTROL_RE = re.compile(r"^\s*(?:apply|apply now|apply online|apply for (?:this|the) (?:job|position|role)|"
                               r"start (?:your )?application|i'?m interested|bewerben|jetzt bewerben|postuler|"
                               r"postuler maintenant|candidatar(?:-se)?|aplicar|solliciteer)\s*[!.›»→]*\s*$", re.I)


def _ask_model(ctx: ApplyContext, snap: dict, goal: str) -> dict | None:
    spent = int(ctx.extra.get("planner_calls", 0))
    if spent >= MAX_MODEL_CALLS:
        log.info("planner: model budget of %d calls spent on this application", MAX_MODEL_CALLS)
        return None
    ctx.extra["planner_calls"] = spent + 1
    try:
        from jobbot.llm import complete
    except Exception as e:  # noqa: BLE001
        log.debug("planner: no llm: %s", e)
        return None
    menu = "\n".join(f'{o["i"]}. {o["name"]} ({o["tag"]})' for o in snap["controls"]) or "(none)"
    kinds = "\n".join(f"- {k}: {v}" for k, v in KINDS.items())
    prompt = (
        "You are the eyes of a browser automation that applies to jobs for a candidate. It filled what it "
        "could on this page and is now stuck: none of its rules recognise the page. Decide what kind of page "
        "this is.\n\n"
        f"Goal: {goal}\n"
        f"Job: {ctx.job.get('company', '')} — {ctx.job.get('title', '')}\n\n"
        f"{observe.describe(snap)}\n\n"
        f"Controls on the page:\n{menu}\n\n"
        f"Kinds:\n{kinds}\n\n"
        "Reply with JSON only, on one line: "
        '{"kind": "<one kind>", "press": <control number or null>, "reason": "<under 20 words>"}\n'
        "Give `press` only for kind \"press\" (the one control that moves the application forward), or for "
        "\"verification_code\" / \"email_link\" when a control must be pressed to send the code or link. "
        "Never pick a control that signs in with another site, withdraws, deletes or leaves the application."
    )
    try:
        reply = complete(prompt, purpose="plan", job_id=ctx.job.get("id"), max_tokens=120)
    except Exception as e:  # noqa: BLE001
        log.info("planner: model unavailable (%s)", str(e)[:120])
        return None
    m = re.search(r"\{.*\}", reply or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    kind = str(data.get("kind") or "").strip()
    if kind not in KINDS:
        log.info("planner: model reply not understood: %r", (reply or "")[:120])
        return None
    press = None
    if data.get("press") is not None:
        try:
            idx = int(data["press"])
            press = next((o["name"] for o in snap["controls"] if o["i"] == idx), None)
        except (TypeError, ValueError):
            press = None
    return {"kind": kind, "press": press, "source": "model", "reason": str(data.get("reason") or "")}


# ---------- act ----------
def _act(ctx: ApplyContext, before: dict, verdict: dict, fill: Callable[[], None] | None) -> str:
    page, kind, press = ctx.page, verdict["kind"], verdict.get("press")
    reason = verdict.get("reason", "")

    if kind == "confirmation":
        return "submitted"
    if kind == "already_applied":
        raise AlreadyApplied(str((before.get("signals") or {}).get("already_applied") or reason)[:300]
                             or "The employer says you have already applied to this job.")
    if kind == "closed":
        raise _verdict(kind, f"This posting is not accepting applications: {reason}")
    if kind == "not_application":
        # The posting's own page judged before it had drawn (Avanade: "site navigation only; job content and
        # apply button not loaded", application 377). One reload and a second look before giving up on it.
        if not ctx.extra.get("na_reloaded") and re.search(r"not (?:yet )?loaded|failed to load|still loading|"
                                                          r"navigation only|no (?:job )?content", reason, re.I):
            ctx.extra["na_reloaded"] = True
            log.info("planner: %r looks like a page that had not loaded; reloading once", reason[:80])
            try:
                page.reload(wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)
            except Exception as e:  # noqa: BLE001
                log.debug("planner: reload failed: %s", e)
            return "moved"
        raise _verdict(kind, f"This page is not part of an application ({reason}) at {page.url}")
    if kind == "captcha":
        c.detect_captcha(page)          # raises the usual pause when a widget is really there
        raise NeedsHuman(c.CAPTCHA_MSG)

    if kind == "sso":
        raise NeedsHuman(c.SSO_MSG.format(provider=c.sso_provider(page.url) or "that"))

    if kind == "email_link":
        if press:
            _press(ctx, before, press)   # "Send me a link": the mail is only sent once this is pressed
        since = ctx.extra.get("advanced_at") or (datetime.now(timezone.utc) - timedelta(seconds=LINK_LOOKBACK_S))
        c.follow_login_link(ctx, since - timedelta(seconds=c.VERIFY_CLOCK_SKEW_S))
        return "moved"

    if kind == "verification_code":
        if press:
            _press(ctx, before, press)
        prompt = c.verification_prompt(page)
        if not prompt:
            if c.code_boxes(page):
                prompt = {"length": 0, "to": ""}
            else:
                raise NeedsHuman("This step wants a code the site sent you, and jobbot cannot find where to "
                                 "type it. Enter it in the open window, then click Continue.")
        if not prompt.get("length"):
            prompt["length"] = c._code_length(c.code_boxes(page)) if c.code_boxes(page) else 6
        since = ctx.extra.get("advanced_at") or (datetime.now(timezone.utc) - timedelta(seconds=LINK_LOOKBACK_S))
        c.handle_verification(ctx, prompt, since - timedelta(seconds=c.VERIFY_CLOCK_SKEW_S))
        return _press_forward(ctx, before)

    if kind == "account":
        if account.pass_gate(ctx, fill or (lambda: None)):
            return "moved"
        raise NeedsHuman("This employer wants you to sign in or create an account here, in a shape jobbot "
                         "does not recognise yet. Do it in the open window, then click Continue — the "
                         "application carries on from there.")

    if kind == "form_step":
        # The one kind whose definition is "questions still unanswered that must be filled", and the only
        # one that used to answer that by asking the user to fill them. The filler is already in hand — the
        # same `fill` the account gate is given — so use it before giving the form back. Application 293
        # stood on Shopee's application form, open and completely blank, was told by the model in as many
        # words that it was a form step with name, email, education and experience still empty, and handed
        # the whole thing to the user because this branch only ever pressed a button.
        #
        # Once per application: a step that is still a form step after being filled is a step jobbot cannot
        # fill, and filling it again would only spend the budget twice over.
        # Once per pass (90 s), not per application: a retry carries ctx.extra over, and a fix made during the pause
        # would otherwise never get to fill the step it was made for (Rippling, application 385).
        if fill is not None and time.time() - float(ctx.extra.get("planner_filled_at") or 0) > 90:
            ctx.extra["planner_filled_at"] = time.time()
            log.info("planner: filling this form step before asking the user to")
            try:
                fill()
            except (NeedsHuman, AlreadyApplied, ApplyError):
                raise
            except Exception as e:  # noqa: BLE001 - one control that would not take a value is not the page
                log.info("planner: filling this step raised %s", str(e)[:120])
            # "moved" whether or not the page's shape changed: filling a form rarely changes its signature,
            # and what the caller needs to know is that the form was reached and worked on, so that it looks
            # at the page again rather than treating it as a dead end.
            return "moved"
        # A step whose one gap is a consent tick drawn in a shape the fill pass did not see: UKG/UltiPro's
        # "Almost there!" signup page (application 318) had its names and phone filled and Create account
        # disabled behind an unticked privacy box, and was handed back to the user over that alone.
        if c.tick_consent_boxes(page):
            return "moved"
        if press and _press(ctx, before, press):
            return "moved"
        empty = _required_empty(page)
        if empty:
            log.info("planner: required and still empty on this step: %s", empty)
        raise NeedsHuman(f"This step has something jobbot could not fill ({reason or 'an unusual control'})"
                         + (f" — still empty: {', '.join(empty[:5])}" if empty else "")
                         + ". Complete it in the open window, then click Continue.")

    if kind == "press":
        if not press:
            return ""
        if _press(ctx, before, press):
            if c.confirmation_showing(ctx.page):
                return "submitted"
            return "moved"
        playbook.forget_action(before, "press", press)
        return ""
    return ""


def _required_empty(page) -> list[str]:
    """Labels of visible required controls that hold nothing, for the message that hands a step back."""
    try:
        return page.evaluate("""() => [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select')]
            .filter(e => (e.required || e.getAttribute('aria-required') === 'true') && e.offsetParent !== null)
            .filter(e => (e.type === 'checkbox' || e.type === 'radio')
                         ? !document.querySelector(`input[name="${CSS.escape(e.name)}"]:checked`)
                         : e.type === 'file' ? !(e.files && e.files.length) : !String(e.value || '').trim())
            .map(e => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.name || e.type || '').trim().slice(0, 60))
            .filter((v, i, a) => v && a.indexOf(v) === i)""") or []
    except Exception:  # noqa: BLE001
        return []


def _verdict(kind: str, message: str) -> ApplyError:
    """A terminal answer about the page, tagged so the runner reports it over a vendor adapter's guess."""
    err = ApplyError(message[:400])
    err.verdict = kind
    return err


def _press_forward(ctx: ApplyContext, before: dict) -> str:
    """After typing a code: the page usually moves by itself, else its forward control is the model's pick."""
    page = ctx.page
    page.wait_for_timeout(1500)
    if _changed(before, observe.snapshot(page)):
        return "moved"
    verdict = _ask_model(ctx, observe.snapshot(page), "submit the code that was just typed in")
    if verdict and verdict.get("press") and _press(ctx, before, verdict["press"]):
        return "moved"
    return ""


def _press(ctx: ApplyContext, before: dict, name: str) -> bool:
    """Press the control called `name` and say whether the page moved on."""
    page = ctx.page
    spent = int(ctx.extra.get("planner_presses", 0))
    if spent >= MAX_PRESSES:
        log.info("planner: press budget of %d spent on this application", MAX_PRESSES)
        return False
    # Marked afresh: the snapshot's marks may belong to a render that has since been replaced.
    options = navigator.controls(page)
    target = next((o for o in options if o["name"].lower() == name.lower()), None)
    if target is None:
        log.info("planner: %r is not on the page to press", name)
        return False
    ctx.extra["planner_presses"] = spent + 1
    try:
        el = page.locator(f'[{navigator._MARK}="{target["i"]}"]').first
        ctx.step(f"Pressing '{name[:40]}'")
        el.scroll_into_view_if_needed(timeout=c.SHORT)
        el.click(timeout=c.MEDIUM)
        page.wait_for_timeout(SETTLE_MS)
    except Exception as e:  # noqa: BLE001
        log.info("planner: %r would not press (%s)", name, str(e)[:100])
        try:
            if c.follow_control_href(page, el, page.url):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False
    after = observe.snapshot(ctx.page)
    if not _changed(before, after):
        try:
            if c.follow_control_href(page, el, page.url):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


def _changed(before: dict, after: dict) -> bool:
    """Whether the page is a different page now: another step, another URL, or a different set of controls."""
    if observe.signature(before) != observe.signature(after) or before.get("url") != after.get("url"):
        return True
    names = lambda s: {o["name"] for o in s.get("controls", [])}  # noqa: E731
    return names(before) != names(after) or abs(before.get("fields", 0) - after.get("fields", 0)) > 0
