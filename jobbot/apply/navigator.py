"""The last resort before an application is handed back: ask the model which control moves it on.

Every rule in generic.py is a phrase somebody wrote down after watching a board break — "Apply", "Postuler",
"I'm interested", "Continue to next step". That list can only ever describe boards that have already been
seen, and the long tail is long: the issues table has "could not find an application form" nine times and
"found neither a Submit nor a Next button" alongside it, on Dayforce, SuccessFactors, a bespoke careers
site and a board whose opener said "Start your journey". None of those is a hard page. Each is a page whose
button is worded in a way no list happened to contain.

So when the deterministic walker runs out of phrases, the page is described to the model and it picks the
control to press. What makes that safe is that it never writes a selector and never invents an action: the
page is scanned for controls that are really there, they are numbered, and the reply is one of those
numbers or nothing. The worst a bad answer can do is press a button a person could have pressed.

The guard rails, in order of how much they matter:

  * only controls. Fields are filled by the walker from facts.yaml and the answer cache, where the answers
    are true; the model is never asked what to type into an application.
  * a fixed deny list (below) applied before the model sees anything, so "Delete", "Withdraw", "Sign out"
    and "Apply with LinkedIn" are not on the menu at all — a third-party login especially, which hands an
    employer's board a session it was never meant to have.
  * a budget per application. Three presses: enough to get past an opener, a cookie-shaped interstitial and
    a step, and few enough that a page which loops cannot run up a bill or a browser full of tabs.
  * it is only ever reached after the deterministic path has failed, so a board jobbot knows is still
    driven by rules that were written for it, and this costs nothing on the boards that already work.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from jobbot.apply import common as c

log = logging.getLogger(__name__)

MAX_PRESSES = 3          # per application; see the budget note above
MAX_CONTROLS = 40        # what the model is shown. More is noise, and a page with more is a job list.
PAGE_TEXT_CHARS = 1200
SETTLE_MS = 2500

# Never offered to the model. Undoing an application, leaving the site, or handing a third party a login are
# not "the next step" under any wording, and no reply should be able to reach them.
UNSAFE_RE = re.compile(
    r"\b(?:delete|discard|cancel|clear|reset|remove|withdraw|unsubscribe|report|block|flag"
    r"|sign\s*out|log\s*out|logout|sign\s*in\s+with|register\s+with|print|download|share|follow"
    r"|previous|back|close|dismiss|not\s+now|skip|decline|reject|deny|refuse|save\s+(?:job|for|this)"
    r"|another|different|similar|more\s+jobs|all\s+jobs|search"
    # "Refer a friend" / "Recommander un(e) ami(e)" sits next to the Apply button on a SmartRecruiters
    # posting and opens a different flow entirely — a real control on a real page, seen while probing
    # ServiceNow's board, and one no reply should be able to reach.
    r"|refer|recommend|recommander)\b", re.I)
# The same third-party rule the opener search uses: "Apply with LinkedIn" opens somebody else's login.
THIRD_PARTY_RE = re.compile(r"linked\s*in|indeed|google|facebook|apple\b|seek\b|xing|microsoft|dropbox|okta",
                            re.I)

_MARK = "data-jobbot-nav"

_CONTROLS_JS = "(args) => {" + c.DEEP_JS + r"""
    const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'
               && s.opacity !== '0'; };
    for (const el of deepAll('[' + args.mark + ']')) el.removeAttribute(args.mark);
    const out = [], seen = new Set();
    const sel = 'button, a[href], [role=button], [role=link], input[type=submit], input[type=button], summary';
    for (const el of deepAll(sel)) {
        if (!vis(el) || el.disabled) continue;
        // A mailto:/tel: link leaves the browser entirely. ServiceNow's posting carries a recruiter's
        // address as an ordinary link, and it has no business on a menu of ways to move an application on.
        const href = (el.getAttribute('href') || '').trim().toLowerCase();
        if (href.startsWith('mailto:') || href.startsWith('tel:') || href.startsWith('javascript:void')) continue;
        let name = (deepText(el) || deepAttr(el, 'aria-label') || el.value || el.title || '').trim();
        name = name.replace(/\s+/g, ' ').slice(0, 80);
        if (!name) continue;
        const key = name.toLowerCase();
        if (seen.has(key)) continue;            // the same control drawn twice (mobile + desktop headers)
        seen.add(key);
        el.setAttribute(args.mark, String(out.length));
        out.push({i: out.length, name: name, tag: el.tagName.toLowerCase()});
        if (out.length >= args.limit) break;
    }
    return out;
}"""


def controls(page: Any, limit: int = MAX_CONTROLS) -> list[dict]:
    """Every pressable control on the page that is safe to offer, marked so it can be pressed again.

    Marking rather than remembering a selector: boards name nothing stably, and a control found by its
    position in a list has moved by the time the model has answered.
    """
    try:
        found = page.evaluate(_CONTROLS_JS, {"mark": _MARK, "limit": limit}) or []
    except Exception as e:  # noqa: BLE001 - a page that cannot be scanned is one to give up on politely
        log.debug("navigator: could not scan the page: %s", e)
        return []
    return [ctl for ctl in found
            if not UNSAFE_RE.search(ctl.get("name", "")) and not THIRD_PARTY_RE.search(ctl.get("name", ""))]


def press_next(ctx: Any, goal: str) -> str:
    """Press whatever the model says moves the application towards `goal`. The name pressed, or ''.

    Never raises: this runs where the alternative is already a failure, so anything going wrong here should
    leave the caller free to report the failure it was going to report anyway.
    """
    page = ctx.page
    spent = int(ctx.extra.get("nav_presses", 0))
    if spent >= MAX_PRESSES:
        log.info("navigator: budget of %d presses already spent on this application", MAX_PRESSES)
        return ""
    options = controls(page)
    if not options:
        return ""
    choice = _ask_model(ctx, goal, options)
    if choice is None:
        log.info("navigator: the model found nothing on this page that moves the application on")
        return ""
    name = options[choice]["name"] if choice < len(options) else ""
    try:
        target = page.locator(f'[{_MARK}="{options[choice]["i"]}"]')
        if not c.is_visible_now(target.first):
            return ""
        ctx.extra["nav_presses"] = spent + 1
        log.info("navigator: pressing %r to %s", name, goal)
        ctx.step(f"Trying '{name[:40]}'")
        target.first.scroll_into_view_if_needed(timeout=c.SHORT)
        target.first.click(timeout=c.MEDIUM)
        page.wait_for_timeout(SETTLE_MS)
        return name
    except Exception as e:  # noqa: BLE001
        log.info("navigator: %r would not press (%s)", name, str(e)[:100])
        return ""


def _ask_model(ctx: Any, goal: str, options: list[dict]) -> int | None:
    """The index the model picked, or None. Validated against the list it was shown."""
    try:
        from jobbot.llm import complete
    except Exception as e:  # pragma: no cover
        log.debug("navigator: no llm available: %s", e)
        return None
    menu = "\n".join(f'{o["i"]}. {o["name"]} ({o["tag"]})' for o in options)
    prompt = (
        "You are driving a job application in a web browser for a candidate. The automation knows how to "
        "fill forms but cannot tell which control on this page moves the application forward, because the "
        "wording is not one it knows.\n\n"
        f"Goal: {goal}\n"
        f"Page: {_title(ctx.page)} — {(getattr(ctx.page, 'url', '') or '')[:160]}\n"
        f"Job: {ctx.job.get('company', '')} — {ctx.job.get('title', '')}\n\n"
        f"What the page says:\n{_page_text(ctx.page)}\n\n"
        f"Controls on the page:\n{menu}\n\n"
        "Reply with the number of the one control that best serves the goal. Reply NONE if none of them "
        "does — for example if this is a job listing with no application on it, a sign-in wall, an error "
        "page, or a control that would start a different application. Reply with the number alone, nothing "
        "else."
    )
    try:
        reply = complete(prompt, purpose="navigate", job_id=ctx.job.get("id"), max_tokens=8)
    except Exception as e:  # noqa: BLE001
        log.info("navigator: model unavailable (%s)", str(e)[:120])
        return None
    m = re.search(r"\d+", reply or "")
    if not m:
        return None
    idx = int(m.group(0))
    picked = next((n for n, o in enumerate(options) if o["i"] == idx), None)
    if picked is None:
        log.info("navigator: the model answered %r, which is not one of the controls offered", reply[:40])
    return picked


def _title(page: Any) -> str:
    try:
        return c.clean(page.title())[:80]
    except Exception:  # noqa: BLE001
        return ""


def _page_text(page: Any) -> str:
    try:
        return c.clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))[:PAGE_TEXT_CHARS]
    except Exception:  # noqa: BLE001
        return ""
