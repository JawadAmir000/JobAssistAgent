"""Shared Playwright helpers for adapters. All helpers are defensive: an absent optional field never raises."""
from __future__ import annotations

import base64
import html
import logging
import random
import os
import pathlib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qsl, urlparse

from jobbot.apply.base import AlreadyApplied, ApplyContext, ApplyError, NeedsHuman

# jobbot.mail holds only functions and constants, so it is safe to swap under a running server, and this
# module is reloaded on every Retry: reloading it from here is what lets a fix to the code reader (the
# "follow" OTP, application 225) land without a restart that would close every parked window.
# base._RELOADABLE lists it too, which takes over once the server has been restarted.
try:
    import importlib as _importlib
    import sys as _sys
    if "jobbot.mail" in _sys.modules:
        _importlib.reload(_sys.modules["jobbot.mail"])
except Exception:  # noqa: BLE001 - an unreloadable mail module keeps the version already loaded
    pass

log = logging.getLogger(__name__)

SHORT = 1500      # ms - "is it there?" probes
MEDIUM = 5000     # ms - waits that should normally succeed
CONFIRM_TIMEOUT_S = 20
VERIFY_CLOCK_SKEW_S = 120   # how far before the submit click to look for the code mail (clocks disagree)
SUBMIT_SETTLE_MS = 1500     # let a successful submit navigate before judging what the page shows
VERIFY_GRACE_S = 3.0        # the code step must still be there after this to count; see wait_for_confirmation
CAPTCHA_GRACE_S = 4.0       # a challenge must still be up after this to count; see wait_for_confirmation
CAPTCHA_CONFIRM_S = 3.0     # and must survive a re-measure this far apart; see captcha_frame_showing
SUBMIT_INFLIGHT_MAX_S = 75  # how long a form that still says "Submitting..." is given before giving up

CAPTCHA_MSG = ("Captcha on this form. Solve it in the Chromium window that is already open (it has been "
               "brought to the front), then click Continue here. Solving it once per company is usually "
               "enough — the clearance cookie is reused for that board's later applications.")
# A tick-box sitting at the foot of the form (JazzHR's applytojob.com): nothing to clear before filling, and
# every form needs its own tick, so the clearance-cookie advice above does not apply.
CHECKBOX_MSG = ("The form is filled in; only its \"I'm not a robot\" tick is left. Tick it in the Chromium "
                "window (it has been brought to the front), then click Continue here — jobbot sends it from "
                "there. Do it within two minutes: the tick expires.")
# Greenhouse will not accept a submission until a code it mails to the candidate is typed back into the form.
VERIFY_MSG = ("The form wants the verification code emailed to {to}. Paste the code here, or set an app "
              "password in Settings so it is fetched automatically next time.")
VERIFY_QUESTION = "Verification code from the email (one-time)"
# A submit that produced neither a confirmation nor a complaint. The form may well have gone through, so
# jobbot must never press Submit again on its own: a second send is a duplicate application at a real
# employer, which is worse than a missed one. The window is left on the page for the candidate to read.
UNCERTAIN_MSG = ("jobbot filled this form and pressed Submit, but the site showed neither a confirmation "
                 "nor an error, so whether it went through cannot be told from the page. Look at the "
                 "window that is already open. If the application was sent, click 'Mark applied'. If the "
                 "form is still sitting there, click Continue and jobbot will look again — it will not "
                 "submit a second time on its own.")
UNCERTAIN_AGAIN_MSG = ("jobbot still cannot confirm this application went through, and it will not send the "
                       "form a second time by itself. Finish or check it in the open window, then click "
                       "'Mark applied' if it is in, or Retry to start the application over.")
GOTO_MIN_TEXT = 200   # characters of body text that make a page "shown" though it never finished loading


def goto(page: Any, url: str, *, timeout: int = 30000) -> None:
    """page.goto(url, wait_until="domcontentloaded") that tolerates a page which renders but never fires
    DOMContentLoaded. Macquarie's Avature portal shows the whole job within seconds, yet a handful of its
    deferred script bundles hang for minutes, so the event never comes and goto() timed out on a page that
    was on screen (application 486, 'Page.goto: Timeout 30000ms exceeded' four times). On a timeout, a page
    that has reached the URL and has real text on it is used as it is; anything else is still a timeout."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return
    except Exception as e:  # noqa: BLE001
        if "Timeout" not in str(e):
            raise
        try:
            here = page.url or ""
            text = len(page.evaluate("document.body ? document.body.innerText : ''") or "")
        except Exception:  # noqa: BLE001
            raise e from None
        if here.startswith("http") and text >= GOTO_MIN_TEXT:
            log.info("goto: %s never finished loading, but it is shown (%d chars); carrying on",
                     here[:120], text)
            return
        raise


CONFIRM_TEXTS = (
    "thank you for applying", "thanks for applying", "application has been submitted", "application submitted",
    "your application was submitted", "we have received your application", "we've received your application",
    "thank you for your application", "thank you for your interest", "application received",
    "successfully submitted", "your application has been received",
    # the shorter forms the newer boards use
    "thanks for your application", "you have applied", "you've applied", "application sent",
    "application complete", "we got your application", "we've got your application", "your application is in",
    "you're all set", "you are all set", "application was sent", "has been sent to",
    # an adverb in the middle: Rippling's "You have successfully applied to Forward Deployed Engineer"
    # matched none of the above, so its confirmation page was taken for a bounced form (application 175)
    "successfully applied", "applied successfully", "application was successful", "application is complete",
    # a noun in the middle: Taleo's "Thank you for your job application" under "Process completed"
    # (Cognizant, application 305) — sent, and reported as unconfirmed
    "thank you for your job application", "thanks for your job application", "thank you for submitting your",
    "your job application has been", "process completed",
)
# Phrases a page also uses BEFORE anything is sent: Avanade's "Thank you for your interest in Avanade - you will
# now be redirected to our application page" was read as a confirmation and two applications that were never
# made were recorded as already sent (338, 350); OutSystems' form says "thank you for your interest" above its
# fields (341). They confirm only right after a submit press, never from a page judged on its own.
WEAK_CONFIRM_TEXTS = frozenset({"thank you for your interest", "you're all set", "you are all set", "has been sent to",
                                "process completed",
                                # "…requirements of the role you have applied to" in Accenture's AI-screening
                                # notice, on a form not yet sent (application 382)
                                "you have applied", "you've applied"})
STRONG_CONFIRM_TEXTS = tuple(t for t in CONFIRM_TEXTS if t not in WEAK_CONFIRM_TEXTS)
FORM_GONE_GRACE_S = 4.0     # a form that vanished after Submit must stay gone this long to count as sent
CONFIRM_URL_HINTS = ("confirmation", "thanks", "thank-you", "thankyou", "submitted", "success", "applied")
# The hints above, as words of the URL's path and query -- never its host. "career5.successfactors.eu"
# carries "success" in the host, so every SuccessFactors page read as a thank-you page and Capgemini was
# recorded as applied from a "Loading..." screen (application 215). Word-bounded for the same reason:
# "/successfactors/", "?unapplied=" and "/applied-ai-engineer" are not confirmations either.
_CONFIRM_URL_RE = re.compile(
    # Not after a hyphenated word either: Coveo's job lives under /customer-management/technical-success/
    # (application 373), and "customer-success" teams are everywhere.
    r"(?<![a-z0-9])(?<!technical-)(?<!customer-)(?<!client-)(?<!partner-)(?<!student-)(?:confirm(?:ation|ed)?|thanks|thank-?you|submitted|success(?:ful(?:ly)?)?|applied)"
    r"(?![a-z0-9]|-(?:ai|ml|data|science|scientist|engineer|research))", re.I)


def confirm_url(url: str) -> bool:
    """True when the URL itself says the application went in (see _CONFIRM_URL_RE)."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    return bool(_CONFIRM_URL_RE.search(f"{parts.path}?{parts.query}#{parts.fragment}"))

_WS = re.compile(r"\s+")


class FrameView:
    """A Frame that answers like a Page, so the walkers can fill a form inside an iframe in place.

    iCIMS keeps the whole candidate flow in an iframe of its own site and frame-busts any attempt to open
    that URL top-level, so hopping to the frame's src (the Greenhouse-embed trick) lands back on the framed
    page. Locators, evaluate, url and the waits go to the frame; the keyboard, mouse, screenshots and
    liveness checks belong to the page that owns it.
    """
    def __init__(self, frame: Any):
        self._frame = frame
        self.page = frame.page

    def __getattr__(self, name: str) -> Any:
        if name in ("keyboard", "mouse", "screenshot", "bring_to_front", "context", "set_default_timeout",
                    "expect_navigation", "wait_for_load_state", "reload"):
            return getattr(self.page, name)
        return getattr(self._frame, name)

    def is_closed(self) -> bool:
        try:
            return self.page.is_closed() or self._frame.is_detached()
        except Exception:  # noqa: BLE001
            return True

    @property
    def frames(self):
        return self.page.frames


# Spliced into every page script below that asks a page what controls, forms or buttons it holds.
#
# `document.querySelectorAll` stops dead at a shadow root, and boards have started building applications out
# of web components. SmartRecruiters' one-click apply (jobs.smartrecruiters.com/oneclick-ui/…) is the case
# that forced this: the whole form is <spl-*> custom elements with open shadow roots, and its light DOM holds
# no input, no button and no <form> at all — 1772 shadow roots and `document.querySelectorAll('input')`
# returning zero on a page showing first name, last name, email, confirm email and a phone box. Every probe
# went blind at once, so scope detection counted no controls and the run ended on "jobbot could not find an
# application form" (application 118, Deloitte NZ). The walker itself never had the problem: Playwright's
# selector engine pierces open shadow roots, so the locators that fill the fields found all six.
#
# `deepAll` therefore queries every open root rather than the document alone, and the containment helpers
# walk the composed tree (`parentNode || host`), because a control inside a component has no `body`, `main`
# or `form` ancestor of its own however plainly it sits inside one.
DEEP_JS = r"""
    const _deepRoots = (() => {
        const roots = [document];
        for (let i = 0; i < roots.length; i++)
            for (const el of roots[i].querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
        return roots;
    })();
    const deepAll = sel => {
        const out = [];
        for (const r of _deepRoots) for (const el of r.querySelectorAll(sel)) out.push(el);
        return out;
    };
    const deepContains = (anc, node) => {
        for (let n = node; n; n = n.parentNode || n.host) if (n === anc) return true;
        return false;
    };
    const deepClosest = (el, sel) => {
        for (let n = el; n; n = n.parentNode || n.host) if (n.matches && n.matches(sel)) return n;
        return null;
    };
    const deepIn = (el, sel) => deepAll(sel).filter(n => n !== el && deepContains(el, n));
    // What a control built as a web component actually reads as on screen. <spl-button>Next</spl-button>
    // renders a <button> inside its shadow root whose own innerText is empty: the word "Next" stays in the
    // light DOM and is pulled in through a <slot>, so the button that the scans below find is nameless and
    // the host that carries the name is not a button. Falling back up the host chain names it either way.
    const deepText = el => {
        const txt = n => ((n && (n.innerText || n.textContent)) || '').replace(/\s+/g, ' ').trim();
        let t = txt(el);
        for (let host = el.getRootNode().host; !t && host; host = host.getRootNode().host) t = txt(host);
        return t;
    };
    const deepAttr = (el, name) => {
        for (let n = el; n; n = n.getRootNode().host) {
            const v = n.getAttribute && n.getAttribute(name);
            if (v) return v;
        }
        return '';
    };
"""

# ---------- dropdowns built out of <div>s ----------
# A select the walker could not see at all. Shopee's careers site (application 274) draws every list as
#
#     <div class="shopee-select">                        <- the widget
#       <div class="shopee-selector" tabindex="0">       <- what a person clicks
#         <div class="shopee-selector__inner placeholder">Education Level</div>
#       </div>
#       <div class="shopee-popper" style="display:none">  <- the list, with its own search box
#         <input class="shopee-input__input">            <- hidden until the list is open
#         <div class="shopee-option">Bachelor's Degree</div> ...
#
# No <select>, no role=combobox, and the one <input> is invisible while the list is closed. Every walker
# here is keyed on input / select / textarea / [role=combobox], so the page's nine dropdowns — Education
# Level, School, Course of Study, Current Location, the phone's country code, "How did you know about this
# role?" — were not on the form as far as jobbot was concerned. The resume parse filled some; the rest
# stayed empty, and the submit bounced on all of them. Worse, field_errors attributes a complaint to the
# nearest *fillable* control, so "Education Level: please select an option" was pinned on the CGPA text
# box beside it, which was then "repaired" and its (correct, human-given) answer marked rejected.
#
# Element UI / Element Plus (el-select), iView (ivu-select), Vuetify, Semantic UI, Bootstrap-select, Select2
# and Chosen are all built the same way, so this is keyed on the shape rather than on any vendor's class
# names: a small, visible box whose class says select / dropdown / picker / combobox, holding at most one
# text input and exactly one thing to click, and no native control that the walker already drives. The
# widget's *root* is stamped `data-jobbot-widget` and from then on is a control like any other: it has a
# label, a value, is required or not, is named in a validation message, and is driven through
# open_widget / popup_options / choose_combobox below.
WIDGET_ATTR = "data-jobbot-widget"
POPUP_ATTR = "data-jobbot-popup"
OPTION_ATTR = "data-jobbot-opt"
FACE_ATTR = "data-jobbot-face"
WIDGET_JS = r"""
    const W_ROOT_SEL = '[class*="select" i]:not(select):not(option):not(optgroup):not(label), [class*="dropdown" i],'
                     + ' [class*="combobox" i], [class*="autocomplete" i], [class*="cascader" i], [class*="picker" i]';
    const W_NATIVE = 'select, textarea, [role=combobox], [role=listbox], input[list], input[type=checkbox],'
                   + ' input[type=radio], input[type=file], input[type=submit], button[type=submit]';
    const W_FACE = '[tabindex]:not([tabindex="-1"]), [role=button], button, [class*="selector" i], [class*="control" i],'
                 + ' [class*="trigger" i], [class*="selection" i], [class*="toggle" i], [class*="inner" i]';
    const W_POPUP = '[class*="popper" i], [class*="dropdown" i], [class*="popup" i], [class*="menu" i],'
                  + ' [class*="options" i], [class*="panel" i], [role=listbox], [role=menu]';
    const W_OPT = '[role=option], [role=menuitem], [role=treeitem], li, [class*="option" i], [class*="item" i]';
    const W_ICON = '[class*="arrow" i], [class*="caret" i], [class*="chevron" i], [class*="suffix" i],'
                 + ' [class*="indicator" i], svg, i';
    const wVis = el => { try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; } catch (e) { return false; } };
    const wClean = s => (s || '').replace(/\s+/g, ' ').trim();
    const wText = el => wClean(el.innerText || el.textContent);
    const wPrompt = t => /^\s*(?:-+\s*)?(?:please\s+)?(?:select|choose|pick|search)(?:\s+(?:one|an?\s+option|an?\s+answer|an?\s+item|here))?\s*(?:\.{3}|…)?\s*(?:-+)?\s*$/i.test(t || '');
    const wPopupsIn = root => [...root.querySelectorAll(W_POPUP)];
    const wCls = n => ((n && n.className && n.className.toString) ? n.className.toString() : '') + ' ' + ((n && n.id) || '');
    // What the widget shows while closed: its text outside the popup, read from the visible text nodes.
    const wFaceText = root => {
        const pops = wPopupsIn(root);
        const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let out = '';
        for (let n = w.nextNode(); n; n = w.nextNode()) {
            const p = n.parentElement;
            if (!p || pops.some(x => x.contains(n)) || !wVis(p)) continue;
            out += ' ' + n.textContent;
        }
        // A select2 / react-select clear button is a bare "×" beside the value: "US Dollar ×" was learned as
        // the answer to Lenovo's currency question (application 336).
        return wClean(wClean(out).replace(/(^|\s)[×✕✖⨯](?=\s|$)/g, ' '));
    };
    // Site chrome is never part of an application: AMD's iCIMS footer drew its social links and menus as lists
    // that the walker "answered" and learned from ('Benefits' -> 'Job Categories', application 421).
    const wChrome = el => !el.closest('form') && !!el.closest(
        'nav, footer, header, [role=navigation], [role=contentinfo], [role=banner], [role=menubar], [role=menu], [class*=footer i], [id*=footer i], [class*=navbar i], [class*=site-header i], [class*=global-header i], [class*=social i], [class*=chat i], [id*=chat i], [aria-label*=chat i], [class*=concierge i], [class*=intercom i], [id*=intercom i], [class*=drift i], [id*=drift i], [class*=livechat i], [class*=messenger i], [role=log]');
    const wIsRoot = el => {
        if (!wVis(el) || wChrome(el) || el.closest("[class*=chat i], [id*=chat i], [aria-label*=chat i], [class*=concierge i], [class*=intercom i], [id*=intercom i], [class*=drift i], [id*=drift i], [class*=livechat i], [class*=messenger i], [role=log]")) return false;
        const r = el.getBoundingClientRect();
        if (r.height > 120 || r.width < 40) return false;          // a control's size, never a section's
        if (el.querySelector(W_NATIVE)) return false;               // wraps something the walker already drives
        // A part of a control the walker already drives: react-select's indicator box (clear "×" + arrow)
        // sits beside its role=combobox input, and taken for a dropdown of its own it was asked the
        // question, chosen into, then read back empty — the value is drawn in the sibling, so "No" was
        // reported as not chosen while the box showed it (Smartcat/Greenhouse, application 356).
        const par = el.parentElement;
        if (par && [...par.querySelectorAll('input[role=combobox], [role=combobox] input')].some(i => !el.contains(i))
            && !el.querySelector('input:not([type=hidden])')) return false;
        const inputs = [...el.querySelectorAll('input:not([type=hidden])')];
        if (inputs.length > 1) return false;
        const pops = wPopupsIn(el);
        const hasIcon = !!el.querySelector(W_ICON);
        // An ordinary text box inside a wrapper that merely says "select" in its class: visible, typeable,
        // no list of its own and no arrow beside it. A filterable el-select has the arrow.
        if (inputs.length === 1 && wVis(inputs[0]) && !inputs[0].readOnly && !pops.length && !hasIcon) return false;
        const faces = [...el.querySelectorAll(W_FACE)].filter(f => wVis(f) && !pops.some(p => p.contains(f))
                                                                && !f.matches('svg, i, [class*="icon" i]'));
        // What a person could click and read: an arrow icon whose class happens to say "selector" is not
        // a second face (Shopee's shopee-selector__suffix-icon).
        const leaf = faces.filter(f => !faces.some(g => g !== f && f.contains(g))
                                       && (wText(f) || f.matches('[tabindex], button, [role=button]')));
        if (leaf.length > 1) return false;                          // a row holding two dropdowns
        if (!leaf.length && !inputs.length && !hasIcon) return false;
        return wFaceText(el).length <= 160;
    };
    const wKind = root => {
        const around = wCls(root) + ' ' + wCls(root.parentElement) + ' ' + wCls(root.parentElement && root.parentElement.parentElement);
        if (/date|month|calendar|\btime/i.test(around) || root.querySelector('[class*="calendar" i], [class*="month-table" i], [class*="date" i]')) return 'date';
        if (/multi/i.test(wCls(root)) || root.querySelector('[class*="tag" i], [class*="chip" i], [class*="multi" i]')) return 'multiselect';
        return 'select';
    };
    const wFace = root => {
        const pops = wPopupsIn(root);
        const faces = [...root.querySelectorAll(W_FACE)].filter(f => wVis(f) && !pops.some(p => p.contains(f)));
        const outer = faces.filter(f => !faces.some(g => g !== f && g.contains(f)));
        return outer[0] || root;                                    // the outermost face carries the click handler
    };
    const wPlaceholderText = root => {
        const pops = wPopupsIn(root);
        return wClean([...root.querySelectorAll('[class*="placeholder" i]')]
            .filter(e => !pops.some(p => p.contains(e)) && wVis(e)).map(wText).join(' '));
    };
    const wValue = root => {
        const pops = wPopupsIn(root);
        const notPop = e => !pops.some(p => p.contains(e)) && wVis(e);
        const tagEls = [...root.querySelectorAll('[class*="tag" i], [class*="chip" i], [class*="multi-value" i], [class*="selection-item" i]')]
            .filter(notPop);
        // the leaf-most: a tag's container is named "tags" and would read every tag a second time
        const tags = tagEls.filter(t => !tagEls.some(u => u !== t && t.contains(u))).map(wText).filter(Boolean);
        if (tags.length) return tags.join(', ');
        const inp = root.querySelector('input:not([type=hidden])');
        if (inp && notPop(inp) && wClean(inp.value)) return wClean(inp.value);
        let t = wFaceText(root);
        const ph = wPlaceholderText(root);
        if (ph) t = wClean(t.replace(ph, ' '));
        return (!t || wPrompt(t)) ? '' : t;
    };
    const widgetRoots = scope => {
        const tops = (!scope || scope === 'body') ? [document.body] : deepAll(scope);
        const out = [];
        let n = document.querySelectorAll('[data-jobbot-widget]').length;
        for (const top of tops) {
            if (!top) continue;
            for (const el of top.querySelectorAll(W_ROOT_SEL)) {
                const stamped = el.closest('[data-jobbot-widget]');
                if (stamped && stamped !== el) continue;             // inside a widget already found
                if (stamped === el) {
                    // Re-judged, not trusted: a stamp left by an older rule (or a page that changed under it)
                    // kept a react-select's indicator box a "widget" after the rule excluding it landed.
                    if (!wIsRoot(el)) { el.removeAttribute('data-jobbot-widget'); continue; }
                    if (wVis(el)) out.push(el);
                    continue;
                }
                if (!wIsRoot(el)) continue;
                el.setAttribute('data-jobbot-widget', 'w' + (n++));
                out.push(el);
            }
        }
        return out;
    };
    const wMarkSeenPopups = () => {
        for (const p of document.querySelectorAll(W_POPUP)) {
            if (wVis(p)) p.setAttribute('data-jobbot-popup-seen', '1'); else p.removeAttribute('data-jobbot-popup-seen');
        }
    };
    // The list a widget opened: inside the widget when it is drawn there (Shopee), else the list that
    // appeared on the page since the click (Element UI appends its dropdown to <body>).
    const wPopupOf = root => {
        const inside = wPopupsIn(root).filter(wVis);
        if (inside.length) return inside[0];
        const positioned = p => ['absolute', 'fixed'].includes(getComputedStyle(p).position);
        const all = [...document.querySelectorAll(W_POPUP)].filter(p => wVis(p) && !root.contains(p) && !p.contains(root) && positioned(p));
        const outer = ps => ps.filter(p => !ps.some(q => q !== p && q.contains(p)));
        // Only a list that appeared since the click: the site's own header menu is a visible, positioned
        // thing full of <li>s, and taken as "the list" it had no rows worth reading (application 274).
        const fresh = outer(all.filter(p => !p.hasAttribute('data-jobbot-popup-seen')));
        const withOpts = ps => ps.filter(p => p.querySelector(W_OPT));
        return withOpts(fresh)[0] || fresh[0] || null;
    };
    const wOptions = popup => {
        const all = [...popup.querySelectorAll(W_OPT)].filter(o => wVis(o) && wClean(o.innerText));
        return all.filter(o => !all.some(x => x !== o && o.contains(x)));     // the leaf-most: one per row
    };
"""


def clean(s: str | None) -> str:
    return _WS.sub(" ", (s or "").replace("\xa0", " ")).strip()


def visible(loc: Any, timeout: int = SHORT) -> bool:
    try:
        loc.first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def is_visible_now(el: Any) -> bool:
    try:
        return bool(el.is_visible())
    except Exception:
        return False


def is_enabled_now(el: Any) -> bool:
    """True when this control can be pressed right now. A gate's own button is routinely disabled until the
    boxes above it are filled, and a click on one costs the full actionability wait and then raises."""
    try:
        return bool(el.is_enabled())
    except Exception:
        return False


def page_alive(page: Any) -> bool:
    """True when the page can still run script.

    `page.is_closed()` alone is not enough: when Chromium dies under Playwright (laptop sleep, the user
    quitting the browser hours into a pause) the Page object can keep reporting open while every call raises
    "Target page, context or browser has been closed". Only an actual round-trip proves the page is there.
    """
    try:
        if page.is_closed():
            return False
        page.evaluate("() => 1")
        return True
    except Exception as e:
        msg = str(e).lower()
        if type(e).__module__.split(".")[0] == "greenlet" or "greenlet" in msg or "different thread" in msg:
            # Playwright's sync objects are bound to the thread that created them; a call from any other
            # thread raises greenlet.error instead of talking to the browser. That is a programming error
            # (see the owner-thread contract in runner.py), never a live page. Failing open here is what
            # made a resume report "form not found": every probe raised, page.url returned its cached value,
            # and the adapter concluded the form had vanished. Fail safe instead: report dead, run fresh.
            log.error("page_alive: Playwright object used from the wrong thread: %s", e)
            return False
        if any(k in msg for k in ("closed", "target", "crashed", "disconnected", "not connected")):
            return False
        return True  # a transient error (execution context swapped mid-navigation) is not a dead page


def require_open(page: Any) -> None:
    """Raise a specific error when the window/tab is gone.

    Adapters probe with helpers that swallow exceptions, so a dead page makes every lookup fail and the
    adapter reports whatever its last check was ("form not found") instead of the real cause. Call this
    before concluding that something is missing.
    """
    if not page_alive(page):
        raise ApplyError("Browser window was closed — click Retry to start over")


def current_value(el: Any) -> str:
    """Current value of an input/textarea/select/contenteditable, '' if none/unknown."""
    try:
        tag = (el.evaluate("e => e.tagName") or "").lower()
        if tag in ("input", "textarea", "select"):
            typ = (el.get_attribute("type") or "").lower() if tag == "input" else ""
            if typ in ("checkbox", "radio"):
                return "on" if el.is_checked() else ""
            if typ == "file":
                return el.evaluate("e => e.files && e.files.length ? 'file' : ''") or ""
            return clean(el.input_value())
        return clean(el.evaluate("e => e.value || e.textContent || ''"))
    except Exception:
        return ""


# A plain number, which is all an <input type=number> will hold.
NUMBER_VALUE_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")


def is_number_box(el: Any) -> bool:
    """True when the browser will only keep a number in this control.

    The bug this exists for (application 114, Aviato Consulting): "Salary Expectations (day rate/annual)"
    and "Notice period (in week)" are <input type=number>, and the answers for them are prose — "Negotiable"
    from the salary rule, "None" from work.notice_period. A number box does not reject text, it swallows it:
    typing "Negotiable" leaves the box showing "e" (the exponent character is the only letter it takes),
    .value reads back as '' so the field looks EMPTY to us, fill() refuses the same string outright, and the
    browser's own validation stops the submit with "Please enter a number." Retry replayed it for ever.

    inputmode=numeric is deliberately not here: that is a keyboard hint on a text box, which holds
    "+880 177 161 4053" quite happily, and treating it as a number box would mangle every phone field.
    """
    try:
        return (el.get_attribute("type") or "").lower() in ("number", "range")
    except Exception:
        return False


# A question whose only honest answer is a figure. LinkedIn's Easy Apply draws these as plain type=text
# boxes and then rejects anything but digits with "Invalid input" -- "10+" for "Approximately how many
# AI-powered applications have you built?" stopped application 178. Asked as a number, the resolver turns
# "10+" / "6 years" into the bare figure, or asks the user when there is none.
_COUNT_QUESTION_RE = re.compile(
    r"^\s*(?:approximately|roughly|about|in total,?)?\s*how\s+many\b"
    r"|^\s*(?:total\s+)?(?:number|no\.?)\s+of\b"
    r"|^\s*years\s+of\b", re.I)
_PLAIN_NUMBER_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*$")


_TODAY_DATE_RE = re.compile(r"(?:today s|todays|today|current|signature|signing|submission)\s+date|date signed"
                            r"|date(?:\s+(?:of\s+)?(?:signature|signing|today))")


def normalize_label(label: str) -> str:
    from jobbot.answers import normalize_question
    return normalize_question(strip_required(label or ""))


def asks_for_count(label: str) -> bool:
    return bool(_COUNT_QUESTION_RE.search(clean(label or "")))


# LinkedIn draws a salary box the same way: type=text, digits only, "Invalid input" for anything else. The
# salary rule's "Negotiable" is what it bounced on for "Salary Expectations in AED" (application 220,
# spiderSilk) and "Expected Salary in THB (per month)" (application 278, Xponential). A pay question that
# names a currency or a period is such a box. LinkedIn only: elsewhere a salary text box takes prose, and a
# box that wants a figure says so with type=number.
_PAY_QUESTION_RE = re.compile(r"salary|compensation|\bpay\b|\brate\b|wage|remuneration|\bctc\b", re.I)
_PAY_UNIT_RE = re.compile(
    r"\b(?:THB|USD|AUD|NZD|AED|SGD|EUR|GBP|INR|BDT|CAD|MYR|PHP|IDR|JPY|HKD|SAR|QAR|CHF|SEK|DKK|NOK|PLN|ZAR)\b"
    r"|[$\u20ac\u00a3\u0e3f\u20b9]|\bper\s+(?:month|annum|year|hour|day|week)\b|\bmonthly\b|\bannual(?:ly)?\b", re.I)


def asks_for_figure(ctx: Any, label: str) -> bool:
    """A text box whose only honest answer is a number: a count on any board, a priced salary on LinkedIn."""
    if asks_for_count(label):
        return True
    text = clean(label or "")
    return ((ctx.job.get("ats") or "") == "linkedin" and bool(_PAY_QUESTION_RE.search(text))
            and bool(_PAY_UNIT_RE.search(text)))


def has_bad_input(el: Any) -> bool:
    """True when the control is holding something its type cannot represent — the stray "e" a text answer
    leaves in a number box. Nothing else notices it: .value reads '' and the box looks untouched."""
    try:
        return bool(el.evaluate("e => !!(e.validity && e.validity.badInput)"))
    except Exception:
        return False


def clear_bad_input(el: Any) -> bool:
    """Empty a control the browser marks as badInput, so a bounced form is not bounced by it again."""
    if not has_bad_input(el):
        return False
    try:
        el.fill("", timeout=SHORT)
        log.info("cleared a value the field could not hold (it read back as empty and blocked the submit)")
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("could not clear bad input: %s", e)
        return False


# Typing, rather than setting values outright. Ashby answered a form filled and submitted in 27 seconds with
# "Your application submission was flagged as possible spam" — and it was right to be suspicious: fill() sets
# a value without a single keystroke event behind it. The application itself is genuine, so the fix is to
# fill it at a human pace rather than to look like something it is not.
TYPE_DELAY_MS = (35, 75)    # per keystroke
FIELD_PAUSE_MS = (120, 380)  # between one field and the next
TYPE_MAX_CHARS = 240        # past this (a cover letter) typing costs minutes; set it and move on


def _human_typing() -> bool:
    from jobbot import config
    return (config.get_setting(config.SETTING_HUMAN_TYPING) or "1") == "1"


def _type_value(el: Any, value: str) -> None:
    """Enter `value` the way a person would, when that is affordable.

    Falls back to setting the value outright for anything too long to type (a cover letter would take
    minutes) and for controls that have no typing API at all, so a board whose widget only accepts fill()
    still gets filled.
    """
    typer = getattr(el, "press_sequentially", None) or getattr(el, "type", None)
    if not _human_typing() or len(value) > TYPE_MAX_CHARS or typer is None:
        el.fill(value, timeout=MEDIUM)
        return
    try:
        el.fill("", timeout=SHORT)      # clear first: typing appends
        typer(value, delay=random.randint(*TYPE_DELAY_MS), timeout=MEDIUM)
    except Exception as e:  # noqa: BLE001 - a control that will not take keystrokes still takes a value
        log.debug("typing %d chars failed (%s); setting the value instead", len(value), e)
        el.fill(value, timeout=MEDIUM)
        return
    # Typing needs the field to keep focus, and a page that re-renders mid-word swallows the keystrokes
    # without raising anything at all: iCIMS took a blank email this way and answered "the format of the
    # email address is not valid". A typed value that did not land is set outright instead.
    if clean(current_value(el)) != clean(value):
        log.info("typed value did not land in the field; setting it directly")
        el.fill(value, timeout=MEDIUM)
        return
    time.sleep(random.randint(*FIELD_PAUSE_MS) / 1000)


def fill_if_empty(el: Any, value: str, *, clear: bool = False) -> bool:
    """Fill an input/textarea only when it is empty (idempotent). Returns True if a value was typed."""
    if value is None or value == "":
        return False
    try:
        if not is_visible_now(el):
            return False
        if is_number_box(el) and not NUMBER_VALUE_RE.match(value.strip()):
            # Typing it would keep only the stray "e" out of it and leave the form unsubmittable, with a
            # field that reads back as empty. Better to leave it visibly blank and let the caller ask.
            log.warning("not typing %r into a number field: it can only hold a number", value[:60])
            return False
        cur = current_value(el)
        if cur and not clear:
            return False
        try:
            el.click(timeout=SHORT)
        except Exception as e:  # noqa: BLE001
            # Something drawn over the box takes the click — TalentMate's floating labels sit on top of its
            # email boxes, so both stayed empty through three signups (application 307). Focus is what the
            # click was for; typing works the same once the box has it.
            log.debug("fill_if_empty: click intercepted (%s); focusing instead", str(e).splitlines()[0][:80])
            el.focus(timeout=SHORT)
        _type_value(el, value)
        return True
    except Exception as e:
        log.debug("fill_if_empty failed: %s", e)
        return False


def upload_resume(page: Any, cv_path: str, file_input: Any | None = None) -> bool:
    """Set the CV on a file input if none is attached yet. Picks the first visible-or-hidden file input near 'Resume/CV'."""
    if not cv_path:
        return False
    # Already on the page under its own name: a board that renders an upload as a list row and leaves its
    # file input empty afterwards (Workday's Resume/CV dropzone) looks exactly like an untouched form to the
    # loop below, so every re-walk of the step attached one more copy -- OCBC's application was carrying
    # three identical CVs, one per pass. The same check already existed as a fallback *after* the loop, for
    # a board that removes the input entirely; it has to run before the loop to stop the second upload.
    if file_input is not None:
        # The caller named the field, so judge that field alone: Ashby's "Autofill from resume" box shows the
        # file name too, and the page-wide look below took that for the Resume field and left the real one
        # empty — "Required fields still empty: Resume" (Brain Co., application 312).
        if _field_holds_file(file_input, cv_path):
            log.info("resume: %s is already in this field; not uploading it again", os.path.basename(str(cv_path)))
            return True
    elif file_listed(page, cv_path):
        log.info("resume: %s is already listed on this page; not uploading it again",
                 os.path.basename(str(cv_path)))
        return True
    candidates = []
    if file_input is not None:
        candidates.append(file_input)
    try:
        inputs = page.locator("input[type=file]")
        n = inputs.count()
        for i in range(n):
            el = inputs.nth(i)
            ctx_text = ""
            try:
                ctx_text = clean(el.evaluate(
                    "e => { const c = e.closest('div,fieldset,section,label,form'); "
                    "return c ? (c.innerText || c.textContent || '').slice(0, 400) : ''; }")).lower()
            except Exception:
                pass
            # The label beside the input names it when nothing around the input does: Gem's two dropzones
            # both read "Click to upload or drag and drop here" inside, and only the <span> above each
            # says which is the CV and which the cover letter.
            ctx_text = (get_label_for(el) + " " + ctx_text).lower()
            if "cover" in ctx_text and "resume" not in ctx_text and "cv" not in ctx_text.split():
                continue
            if not _accepts_document(el):
                continue
            candidates.append(el)
    except Exception:
        pass
    attached = False
    for el in candidates:
        try:
            if current_value(el) == "file":
                attached = True
            elif not attached or _wants_cv(el):
                # The first input takes the CV. So does any later one that names the CV: SmartRecruiters
                # puts an "autofill from your file" dropzone at the top of the form and the actual CV
                # dropzone further down, and a form with the first filled and the second empty goes in
                # without a CV attached.
                el.set_input_files(cv_path, timeout=MEDIUM)
                page.wait_for_timeout(1200)
                attached = True
                try:
                    held = el.evaluate("e => [e.id || e.name, e.disabled, e.files ? e.files.length : -1].join('/')")
                except Exception:  # noqa: BLE001 - detached by the board's own re-render
                    held = "detached"
                log.info("resume: set %s on a file input (id/disabled/files: %s)", os.path.basename(str(cv_path)), held)
            else:
                continue
            if not _wants_cv(el) and attached:
                # nothing names a CV beside this one; a second file input here is "other documents"
                pass
        except Exception as e:
            log.info("resume: a file input refused the CV: %s", str(e).splitlines()[0][:160])
    if attached:
        return True
    if not candidates and _upload_through_chooser(page, cv_path):
        return True
    # Greenhouse swaps the file input for a filename chip once a file is chosen, so on an idempotent re-run
    # there is no input left to find. The CV is attached; don't report that as a failure.
    try:
        name = os.path.basename(cv_path)
        if name and page.get_by_text(name, exact=False).count():
            return True
    except Exception:
        pass
    return False


_JOB_ID_PARAMS = ("jobid", "job_id", "jobreqid", "reqid", "requisitionid", "record", "jid", "gh_jid")


def other_job(job_url: str, url: str) -> bool:
    """True when `url` is a different posting on the same site as `job_url`, told by a job-id query parameter
    both carry with different values. Macquarie's Avature portal (application 487): a retry resumed on a
    window the portal had moved to a page about other jobs, pressed the first 'Apply' there, and set out to
    make the account and apply for "Employment Screening Administrator | Manila" (jobId=24498) instead of
    the AI Engineer role (jobId=22923)."""
    try:
        a, b = urlparse(job_url or ""), urlparse(url or "")
        if not a.netloc or a.netloc.lower() != b.netloc.lower():
            return False
        qa = {k.lower(): v for k, v in parse_qsl(a.query)}
        qb = {k.lower(): v for k, v in parse_qsl(b.query)}
    except Exception:  # noqa: BLE001
        return False
    return any(k in qa and k in qb and qa[k] != qb[k] for k in _JOB_ID_PARAMS)


_SESSION_EXPIRED_RE = re.compile(r"(?:your\s+)?session\s+(?:has\s+)?(?:expired|timed\s*out|ended)", re.I)


def session_expired(page: Any) -> bool:
    """True when a visible dialog says the site's session has ended (Avature: "Your session has expired")."""
    try:
        return bool(page.evaluate(r"""(src) => {
            const re = new RegExp(src, 'i');
            const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
            // Any short visible heading or line saying it -- Avature draws its notice in a plain div overlay.
            return [...document.querySelectorAll('h1, h2, h3, h4, p, strong, [role=dialog], [role=alertdialog], dialog')]
                .some(d => (d.innerText || '').length < 200 && re.test(d.innerText || '') && vis(d));
        }""", _SESSION_EXPIRED_RE.pattern))
    except Exception:  # noqa: BLE001
        return False


def control_href(el: Any) -> str:
    """The absolute URL a control carries (href or a data-* link), '' when none."""
    try:
        return el.evaluate("""e => { const v = e.getAttribute('href') || e.getAttribute('data-href')
                                   || e.getAttribute('data-url') || e.getAttribute('data-link') || '';
                                   if (!v || /^(#|javascript:)/i.test(v)) return '';
                                   try { return new URL(v, location.href).href; } catch (_) { return ''; } }""") or ""
    except Exception:  # noqa: BLE001
        return ""


def follow_control_href(page: Any, el: Any, url_before: str, job_url: str = "") -> bool:
    """Open the URL a non-link control carries when pressing it went nowhere.

    TalentMate's "Apply Job" is a <button href="…/candidate/job-applications/apply/…"> wired to a modal
    that never opens: a button's href means nothing to the browser, so three presses and the planner's
    fourth all stayed on the posting (application 307, seen 12 times). The URL is the site's own, on
    the control the walk already chose to press, so following it is that press finished by hand.
    """
    href = control_href(el)
    if not href or href == url_before or not href.startswith("http"):
        return False
    if other_job(job_url, href):
        log.info("not opening %s: it is a different job from %s", href[:120], job_url[:120])
        return False
    log.info("the press went nowhere; opening the URL the control carries: %s", href[:120])
    try:
        goto(page, href, timeout=30000)
        page.wait_for_timeout(1500)
    except Exception as e:  # noqa: BLE001
        log.info("that URL would not open: %s", str(e).splitlines()[0][:120])
        return False
    return page.url != url_before


def click_reachable(el: Any) -> bool:
    """False when another element covers this control's centre, so a click would land on that instead.

    Taraki's application opens as a modal over the posting, and the posting's own "Apply" button, still
    visible behind the modal's backdrop, was taken as the form's submit: the click waited five seconds on the
    backdrop and crashed the run (application 308). Off-screen controls are given the benefit of the doubt —
    the caller scrolls before it clicks — and so is anything this cannot measure.
    """
    try:
        return bool(el.evaluate("""e => {
            const r = e.getBoundingClientRect();
            const x = r.left + r.width / 2, y = r.top + r.height / 2;
            if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return true;
            const hit = document.elementFromPoint(x, y);
            return !hit || e === hit || e.contains(hit) || hit.contains(e);
        }"""))
    except Exception:  # noqa: BLE001
        return True


_DROPZONE_RE = re.compile(r"drop\s+(?:your\s+)?files?|click\s+to\s+(?:browse|upload)|browse\s+files?|choose\s+(?:a\s+)?file"
                          r"|upload\s+(?:a\s+|your\s+)?(?:file|resume|cv|r[ée]sum[ée])|attach\s+(?:a\s+|your\s+)?(?:file|resume|cv)"
                          r"|^\s*(?:upload|attach|browse)\s*$", re.I)


def _upload_through_chooser(page: Any, cv_path: str) -> bool:
    """Attach the CV where the page has no file input at all until its drop zone is clicked.

    Airtable's form (Musly Club, application 311) asks "Please upload your resume" above a "Drop files here or
    click to browse" box, and creates its file input only inside the click — so upload_resume found nothing
    and the application went in without a CV. Clicking the zone that sits under CV wording, with Playwright
    holding the file chooser it opens, is the same thing a person does.
    """
    try:
        zones = page.get_by_text(_DROPZONE_RE)
        for i in range(min(zones.count(), 6)):
            zone = zones.nth(i)
            # The question above the zone ("Please upload your resume") is skipped, not the zone — unless it
            # is itself a button: Eightfold's profile builder offers exactly "Upload your resume" as one
            # (GlobalFoundries, application 402), and skipping it left the step for the user.
            if not is_visible_now(zone) or (
                    re.match(r"^\s*(?:please\s+)?upload\s+your\s+(?:resume|cv)\b", zone.inner_text() or "", re.I)
                    and not zone.evaluate("e => !!e.closest('button, a, [role=button]')")):
                continue
            near = zone.evaluate("""e => { let p = e; for (let i = 0; i < 15 && p; i++, p = p.parentElement) {
                    const t = (p.innerText || '').slice(0, 400); if (/\\b(cv|resume|résumé|curriculum)\\b/i.test(t)) return t; }
                    return ''; }""")
            if not near or re.search(r"\bcover\s+letter\b", near, re.I) and not re.search(r"\b(?:cv|resume)\b", near, re.I):
                continue
            before = page.locator("input[type=file]").count()
            try:
                with page.expect_file_chooser(timeout=SHORT) as fc:
                    zone.click(timeout=SHORT)
                fc.value.set_files(cv_path)
            except Exception:  # noqa: BLE001 - no native picker: the click drew the site's own upload panel
                # Airtable's click opens a panel ("Local Files, Link, Webcam, Google Drive…") and adds the
                # file inputs then; its own "Upload 1 file" button sends what was chosen.
                inputs = page.locator("input[type=file]")
                if inputs.count() <= before and not inputs.count():
                    continue
                inputs.first.set_input_files(cv_path, timeout=MEDIUM)
                page.wait_for_timeout(2000)
                send = page.get_by_role("button", name=re.compile(r"^\s*upload\b", re.I))
                if send.count() and is_visible_now(send.first):
                    send.first.click(timeout=MEDIUM)
            page.wait_for_timeout(3000)
            log.info("resume: attached %s through the page's own upload control", os.path.basename(str(cv_path)))
            return True
    except Exception as e:  # noqa: BLE001
        log.info("resume: the upload zone did not open a file picker: %s", str(e).splitlines()[0][:120])
    return False


def _field_holds_file(file_input: Any, cv_path: str) -> bool:
    """True when this file input holds a file, or its own field (not the page) shows the CV's name."""
    name = os.path.basename(str(cv_path or ""))
    try:
        # What the field shows wins over what the input holds: in application 312 Ashby's input carried a
        # file while its field still offered "Upload File", so the app had never taken it.
        return bool(file_input.evaluate("""(e, n) => {
            const f = e.closest('.ashby-application-form-field-entry, fieldset, [class*=field i], li');
            if (f && (f.innerText || '').trim()) return !!(n && f.innerText.includes(n));
            return !!(e.files && e.files.length); }""", name))
    except Exception:  # noqa: BLE001
        return False


def cv_input_emptied(page: Any) -> bool:
    """True when a file input that names the CV is on the page, enabled, and holds no file."""
    try:
        inputs = page.locator("input[type=file]")
        for i in range(min(inputs.count(), 8)):
            el = inputs.nth(i)
            if not _wants_cv(el) or not _accepts_document(el):
                continue
            if el.evaluate("e => !e.disabled && !!e.files && e.files.length === 0"):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


# Image suffixes an avatar dropzone lists in `accept`. A CV is never one of these.
_IMAGE_ONLY_RE = re.compile(r"^(?:image/[\w.+-]+|\.(?:jpe?g|png|gif|bmp|webp|heic|heif|tiff?|svg|avif))$", re.I)


def _accepts_document(el: Any) -> bool:
    """False for a file input that takes images only — a profile-photo slot, not a CV one.

    Workable puts an optional "Photo" dropzone above the required "Resume" one, so the first file input on
    the page is the wrong one. A PDF set there is not refused out loud: the widget redraws as though the
    file took, no upload request is made at all, and the form is left holding an attachment that has a name
    and no URL. Submit then does nothing whatsoever — no navigation, no banner, nothing `form_errors` can
    see — and the run ends 20 seconds later on "Submit not confirmed". An input that states no `accept`,
    or lists any non-image type, still takes the CV: only an all-images list is disqualifying.
    """
    try:
        accept = el.get_attribute("accept") or ""
    except Exception:  # noqa: BLE001
        return True
    types = [t.strip() for t in accept.split(",") if t.strip()]
    if not types:
        return True     # no restriction stated: anything goes
    return not all(_IMAGE_ONLY_RE.match(t) for t in types)


_CV_CONTEXT_RE = re.compile(r"\b(?:cv|resume|r[ée]sum[ée]|curriculum|lebenslauf|currículum)\b", re.I)


def _wants_cv(el: Any) -> bool:
    """True when the text around a file input names the CV (heading, label or button)."""
    try:
        return bool(_CV_CONTEXT_RE.search(get_label_for(el) + " " + (el.evaluate(
            "e => { const c = e.closest('section,fieldset,div,label,form'); "
            "const h = c && c.previousElementSibling; "
            "return ((c ? c.innerText : '') + ' ' + (h ? h.innerText : '') + ' ' + (e.getAttribute('aria-label') || '') "
            "+ ' ' + (e.name || '') + ' ' + (e.id || '')).slice(0, 600); }") or "")))
    except Exception:  # noqa: BLE001
        return False


# "Autofill with Resume", "Autofill from resume", "Fill application with CV". The CV must be named in the
# button for this to fire: "Autofill with LinkedIn" is a different offer entirely (it opens an OAuth dance),
# and taking it by accident would hand an employer's board a login it was never meant to have.
AUTOFILL_NAMES = re.compile(r"(?:auto\s*-?\s*fill|fill)\b[^.]{0,24}\b(?:resume|cv)\b", re.I)


def autofill_from_resume(page: Any, cv_path: str) -> bool:
    """Take a form's own "autofill with resume" offer: press it, then give it the CV.

    Worth preferring wherever it exists. The CV is the fullest record of the user's history there is, and a
    board that parses it fills the employment and education blocks — pages of dates and titles that are
    otherwise typed one control at a time or, where facts.yaml has no key for them, asked about. What it
    fills is not trusted blindly: the adapters re-read every control afterwards, fill what is still empty
    from facts.yaml, and ask about whatever is left.
    """
    if not cv_path:
        return False
    try:
        btn = page.get_by_role("button", name=AUTOFILL_NAMES)
        if not visible(btn, SHORT):
            btn = page.get_by_role("link", name=AUTOFILL_NAMES)
            if not visible(btn, SHORT):
                return False
        btn.first.click(timeout=MEDIUM)
        page.wait_for_timeout(1200)
    except Exception as e:  # noqa: BLE001 - an autofill we cannot take is a slower path, not a failure
        log.debug("autofill offer did not open: %s", e)
        return False
    if not upload_resume(page, cv_path):
        return False
    page.wait_for_timeout(2000)     # the board parses the file and re-renders the fields it filled
    log.info("autofilled the form from the CV")
    return True


def drop_file(page: Any, target: Any, path: str) -> bool:
    """Drop a file onto a dropzone, as a person dragging it from the desktop would.

    The last resort for an upload area with no <input type=file> to set and no button that opens a file
    chooser — Workday's "Drop files here or Select files" is exactly that, and it is the one attachment the
    whole application exists for. The file is read into the page as a Blob and handed to the zone in a real
    DataTransfer, so the app's own drop handler runs and its state updates the way it would for a person.
    """
    try:
        raw = pathlib.Path(path).read_bytes()
    except OSError as e:
        log.warning("cannot read the CV at %s: %s", path, e)
        return False
    payload = {"data": base64.b64encode(raw).decode(), "name": os.path.basename(path),
               "type": "application/pdf" if path.lower().endswith(".pdf") else "application/octet-stream"}
    try:
        handle = target.element_handle(timeout=MEDIUM) if hasattr(target, "element_handle") else target
        if handle is None:
            return False
        ok = handle.evaluate(
            """async (el, p) => {
                const res = await fetch('data:' + p.type + ';base64,' + p.data);
                const file = new File([await res.blob()], p.name, {type: p.type});
                const dt = new DataTransfer();
                dt.items.add(file);
                for (const kind of ['dragenter', 'dragover', 'drop']) {
                    el.dispatchEvent(new DragEvent(kind, {bubbles: true, cancelable: true, dataTransfer: dt}));
                }
                return true;
            }""", payload)
        page.wait_for_timeout(2000)
        return bool(ok)
    except Exception as e:  # noqa: BLE001
        log.debug("drop_file failed: %s", e)
        return False


COOKIE_BUTTONS = ("Deny", "Reject all", "Reject", "Decline", "Only necessary", "Necessary only",
                  "Accept all", "Accept")


# True when the button's nearest banner/dialog talks about cookies and says nothing destructive.
_COOKIE_CONTEXT_JS = r"""e => {
    let n = e;
    for (let i = 0; i < 8 && n; i++, n = n.parentElement) {
        const t = (n.innerText || '').slice(0, 4000);
        if (/\b(delete|permanently|withdraw|close (your|my) account)\b/i.test(t)) return false;
        if (/cookie/i.test(t)) return true;
        if (n.matches && n.matches('[role=dialog], dialog, [aria-modal=true]')) return false;
    }
    return false;
}"""


def _cancel_destructive_dialog(page: Any) -> None:
    """Press Cancel/No on an open dialog that warns of deleting or withdrawing anything. Never the action."""
    try:
        dlg = page.locator("[role=dialog], dialog, [aria-modal=true], .ui-dialog, .modal").filter(
            has_text=re.compile(r"permanently delete|delete all data|delete your account", re.I))
        for i in range(min(dlg.count(), 3)):
            d = dlg.nth(i)
            # The warning itself, not a privacy notice that merely mentions deletion rights further down:
            # this guard closed EY's Privacy Notice before it could be acknowledged (application 288).
            if not d.is_visible() or len(c_text(d)) > 600:
                continue
            btn = d.get_by_role("button", name=re.compile(r"^\s*(cancel|no|close|keep)\b", re.I))
            if btn.count():
                btn.first.click(timeout=SHORT)
                page.wait_for_timeout(500)
                log.warning("closed a destructive dialog (%r) with Cancel", c_text(d)[:80])
    except Exception as e:  # noqa: BLE001
        log.debug("destructive dialog check: %s", e)


def c_text(el: Any) -> str:
    try:
        return clean(el.inner_text() or "")
    except Exception:  # noqa: BLE001
        return ""


def dismiss_cookie_banner(page: Any) -> bool:
    """Close a cookie consent bar. These are fixed to the bottom of the page and sit over the submit button
    (Palantir's Lever board is one), so leaving one up can make the final click land on the banner instead.
    Refusal options are tried before acceptance.

    Only inside something that is about cookies. EY's SuccessFactors signup opens a "Privacy Notice" dialog
    with Acknowledge / Decline, and pressing that Decline (taken for a cookie refusal) opened "Delete All
    Data -- this action will permanently delete your account" (application 288). A button whose nearest
    banner does not mention cookies is somebody else's question, and one near "delete" is never pressed.
    """
    _cancel_destructive_dialog(page)
    for label in COOKIE_BUTTONS:
        # Starts-with, not exact: EY's SuccessFactors wall says "Reject All Cookies", and an exact match on
        # "Reject" left it standing over the Apply button — which the run then reported as "no application
        # form on this page". Refusals are still tried before acceptance, so the loosening cannot turn a
        # decline into a consent.
        pattern = re.compile(rf"^\s*{re.escape(label)}\b", re.I)
        for role in ("button", "link"):
            try:
                btn = page.get_by_role(role, name=pattern)
                if not visible(btn, 600):
                    continue
                if not btn.first.evaluate(_COOKIE_CONTEXT_JS):
                    log.info("not pressing %r: what it sits in is not a cookie banner", label)
                    continue
                btn.first.click(timeout=SHORT)
                page.wait_for_timeout(500)
                log.info("dismissed a cookie banner with %r", label)
                return True
            except Exception:
                continue
    return False


def inline_checkbox_only(page: Any) -> bool:
    """The only challenge on screen is a tick-box sitting inside a form that still has fields to fill.

    Regression (WELL Health on JazzHR): applytojob.com puts a reCAPTCHA v2 "I'm not a robot" box under its
    one-page form. The walker checked for a captcha before filling, so the pause handed the user an empty
    form, twelve times over. A tick-box blocks the send, not the filling, and is ticked last; a painted
    puzzle frame (bframe / frame=challenge) or a challenge page with no form is still a captcha up front.
    """
    try:
        frames = list(_owning_page(page).frames)
    except Exception:  # noqa: BLE001
        return False
    saw_box = False
    for fr in frames[1:]:
        try:
            url = fr.url or ""
            if not _CAPTCHA_FRAME_RE.search(url) or _PASSIVE_FRAME_RE.search(url):
                continue
            el = fr.frame_element()
            if not _frame_element_painted(el):
                continue
            box = el.bounding_box()
        except Exception:  # noqa: BLE001
            continue
        if not box or box.get("width", 0) <= 30 or box.get("height", 0) <= 30:
            continue
        if not _CHECKBOX_FRAME_RE.search(url):
            return False        # a puzzle, a Turnstile or an interstitial: a real challenge
        saw_box = True
    if not saw_box:
        return False
    try:
        fields = int(page.evaluate(
            """() => [...document.querySelectorAll('input, textarea, select')].filter(el => {
                    if (/^(hidden|submit|button|image|reset)$/i.test(el.type || '')) return false;
                    if (/captcha/i.test((el.name || '') + ' ' + (el.id || ''))) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
                }).length"""))
    except Exception:  # noqa: BLE001
        return False
    return fields >= 3


def detect_captcha(page: Any, raise_: bool = True, allow_inline: bool = False) -> bool:
    """A visible reCAPTCHA / hCaptcha / Turnstile widget (or challenge iframe) on the page.

    Also catches the older kind that ships no recognisable widget at all — Zoho Recruit renders a plain
    image and a text box labelled "Type below image text". Nothing in the markup says captcha, so the prompt
    wording is the only signal, and missing it means asking the user to read out an image through a form
    question instead of pausing so they can just type it in the window.

    Regression (application 101, Databricks on Greenhouse): every Greenhouse form carries reCAPTCHA
    Enterprise in invisible mode, whose badge is a 256x60 anchor frame with an empty token box until the
    submit. The frame measure added for Turnstile read that as a standing challenge and paused the run
    before a field was filled. The badge is told apart by its own URL (size=invisible), not by the page's
    wording — "verify" is ordinary form copy, and the badge's own text sits in a cross-origin frame that
    innerText cannot see, so keying on prose got it wrong in both directions.

    allow_inline: called before the form is filled — a tick-box inside the form is left for the post-fill
    check (see inline_checkbox_only), which pauses with CHECKBOX_MSG instead.
    """
    found = False
    try:
        found = bool(page.evaluate(
            "() => {" + DEEP_JS +
            r""" const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                    return r.width > 30 && r.height > 30 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
                const hit = s => /recaptcha|hcaptcha|turnstile|arkose|funcaptcha|geetest|datadome|captcha-delivery|perimeterx|px-captcha|awswaf|challenges\.cloudflare/i.test(s || '');
                for (const f of deepAll('iframe')) {
                    if ((hit(f.src) || hit(f.title) || hit(f.className)) && vis(f)) {
                        // The invisible reCAPTCHA badge is not blocking. Keyed on the frame's own URL
                        // (mirrors _PASSIVE_FRAME_RE) and on Google's badge container, never on page prose.
                        // A size=normal|compact anchor (the tick-box) or a visible bframe (the image
                        // challenge) does not match either test, falls through, and is reported.
                        if (/recaptcha\/(api2|enterprise)\/anchor\?[^#]*\bsize=invisible\b/i.test(f.src)
                            || deepClosest(f, '.grecaptcha-badge')) continue;
                        return true;
                    }
                }
                for (const d of deepAll('.h-captcha, .cf-turnstile, .g-recaptcha:not([data-size=invisible])')) {
                    if (vis(d)) return true;
                }
                // Image challenges that name themselves in prose rather than in a known class or src.
                // Their own visibility floor: the widget test wants a box bigger than 30x30, but this text
                // is often a single-line <label> about 20px tall, which that floor rejects.
                const visText = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                    return r.width > 20 && r.height > 8 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
                const prompts = /click on the shape|select all (images|squares)|pick the (odd|different)|verify you are (a )?human|i'?m not a robot|solve the (puzzle|challenge)|type (the |below )?image text|enter the (captcha|characters|text) (shown|above|below|in the image)|captcha code|slide (right|the slider) to|drag the (piece|slider)|verification required|secure your access/i;
                for (const el of deepAll('div,section,form,p,h1,h2,h3,span,label,legend')) {
                    const t = (el.innerText || '').trim();
                    if (t && t.length < 200 && prompts.test(t) && visText(el)) return true;
                }
                // ...and the ones that only say it in the box itself: Zoho Recruit's challenge names
                // itself in a placeholder, which no scan of element text can ever see.
                for (const el of deepAll('input[placeholder], input[aria-label]')) {
                    const t = (el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('aria-label') || '');
                    if (prompts.test(t) && visText(el)) return true;
                }
                return false;
            }"""))
    except Exception:
        found = False
    if found and _captcha_frame_box(page, 30) and captcha_token_present(page):
        # The widget already passed. Turnstile and reCAPTCHA both keep their box on screen afterwards and
        # only swap in a tick, so every check above still fires on a form that is working. Gated on a
        # vendor frame actually being there, so that a token left by some other widget on the page cannot
        # wave through a challenge that has no token of its own — Zoho's image box being the one that
        # matters, since it is found by its prompt wording alone.
        found = False
    elif not found:
        found = captcha_frame_showing(page)
    inline = found and inline_checkbox_only(page)
    if inline and allow_inline:
        log.info("captcha: a tick-box inside the form; left until the form is filled")
        return False
    if found and raise_:
        raise NeedsHuman(CHECKBOX_MSG if inline else CAPTCHA_MSG)
    return found


# The page a bot-detection vendor serves instead of the site. DataDome's says "Access is temporarily
# restricted" with no puzzle to solve; the run can only wait it out.
_BOT_BLOCK_RE = re.compile(r"access is temporarily restricted|unusual activity from your (?:device|network)"
                           r"|automated \(bot\) activity|request blocked|access denied.{0,80}(?:bot|automated)", re.I)
BOT_BLOCK_MSG = ("The site is refusing automated access from this network for now (its bot-protection page is "
                 "showing). Wait a few minutes, load the job in the browser window, then click Continue.")


def detect_bot_block(page: Any, raise_: bool = True) -> bool:
    """A vendor's "we detected unusual activity" page in place of the site."""
    try:
        text = clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))
    except Exception:  # noqa: BLE001
        return False
    if len(text) > 4000 or not _BOT_BLOCK_RE.search(text):
        return False
    if detect_captcha(page, raise_=False):
        return False        # a puzzle is on offer: that is a captcha, handled as one
    if raise_:
        raise NeedsHuman(BOT_BLOCK_MSG)
    return True


def _typeable_count(page: Any) -> int:
    """Visible fillable controls on the page, -1 when the page cannot be asked."""
    try:
        return int(page.evaluate(
            "() => {" + DEEP_JS +
            """ const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                return deepAll('input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select,'
                               + ' button[aria-haspopup=listbox], [role=combobox]')
                    .filter(vis).length; }"""))
    except Exception:  # noqa: BLE001
        return -1


def _button_named_visible(page: Any, names: tuple[str, ...]) -> bool:
    """True when a visible button carries one of `names` — the form's own Submit/Next is still on screen."""
    if not names:
        return False
    try:
        return bool(page.evaluate(
            "(names) => {" + DEEP_JS +
            """ const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const want = new Set(names.map(norm));
                // Links too: Eightfold's "Apply Now" is an <a> drawn as a button, and with it unseen the form-gone
                // rule took a profile builder closing over the job page for a sent application (402).
                for (const b of deepAll('button, input[type=submit], input[type=button], [role=button], a[href], [role=link]')) {
                    if (!vis(b)) continue;
                    if (want.has(norm(deepText(b) || b.value)) || want.has(norm(deepAttr(b, 'aria-label')))) return true;
                }
                return false; }""", list(names)))
    except Exception:  # noqa: BLE001
        return True     # unknown: assume the form is still there


# A question the site puts between Submit and the submission: Stikeman Elliott's "You've made changes to your
# documents. Would you like to save these changes to your presence? Yes / No" (application 429) held the submit
# open until the window closed on it. Answered with its affirmative, once per dialog, unless the dialog is about
# leaving, discarding or withdrawing.
_SUBMIT_DIALOG_JS = r"""() => {
    const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
        return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const boxes = [...document.querySelectorAll('[role=dialog], [role=alertdialog], dialog[open], .modal.show, .modal.in, '
        + '.ui-dialog, [class*=modal i][class*=open i], [class*=dialog i][class*=open i], [aria-modal=true]')].filter(vis);
    for (const d of boxes) {
        const text = (d.innerText || '').replace(/\s+/g, ' ').trim();
        if (!text || text.length > 600) continue;
        if (/\b(leave|discard|delete|withdraw|remove|lose|unsaved|cancel (?:your|this) application|sign out|log out)\b/i.test(text)) continue;
        if (!/\b(sure|confirm|save (?:these |the |your )?changes|would you like|do you want|proceed|submit)\b/i.test(text)) continue;
        const btns = [...d.querySelectorAll('button, input[type=button], input[type=submit], a[role=button]')].filter(vis);
        const yes = btns.find(b => /^\s*(yes|ok|okay|confirm|submit|continue|proceed|save|yes, (?:submit|continue|save))\b/i
            .test((b.innerText || b.value || '').trim()));
        if (yes) { yes.setAttribute('data-jobbot-dialog-yes', '1'); return text.slice(0, 160); }
    }
    return '';
}"""


def answer_submit_dialog(page: Any, answered: set[str]) -> bool:
    """Press the affirmative of a confirm/save dialog raised by a submit. True when one was pressed."""
    try:
        text = page.evaluate(_SUBMIT_DIALOG_JS) or ""
        if not text or text in answered:
            return False
        answered.add(text)
        btn = page.locator("[data-jobbot-dialog-yes]").first
        label = clean(btn.inner_text() or btn.get_attribute("value") or "")
        btn.click(timeout=MEDIUM)
        log.info("submit raised a dialog (%r); pressed %r", text[:100], label)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("submit dialog: %s", e)
        return False


def wait_for_confirmation(page: Any, timeout_s: int = CONFIRM_TIMEOUT_S, names: tuple[str, ...] = ()) -> bool:
    """Wait for a URL change to a confirmation path or a 'thank you' text. Raises ApplyError if neither shows.

    A third signal, weaker than the two above and so given a grace period: the form itself is gone. Boards
    word their confirmation page in a hundred ways, and one whose wording is not on the list produced
    "Submit not confirmed" for an application the employer had already received — and a Retry would have
    sent it twice. A page whose fillable controls all disappeared after the click, with no error, no
    challenge, no refusal and none of the form's own buttons (`names`) left on screen, has taken the
    submission.
    """
    deadline = time.time() + timeout_s
    start_url = ""
    try:
        start_url = page.url
    except Exception:
        pass
    fields_before = _typeable_count(page)
    # Confirmation phrases already on the form do not confirm anything: OutSystems' Workday form says "thank you
    # for your interest" above its fields, and a submit the form refused (phone "isn't recognized") was recorded
    # as sent 1.5 s after the click (application 341). Only a phrase that appears after the press counts.
    try:
        body_before = clean(page.evaluate("() => (document.body && document.body.innerText) || ''")).lower()
    except Exception:  # noqa: BLE001
        body_before = ""
    stale_phrases = {t for t in CONFIRM_TEXTS if t in body_before}
    gone_since: float | None = None
    # A submit that works navigates, but not instantly: judged in the same millisecond, the old page is still
    # painted. That raced a successful submission into "the code step is still up", which re-submitted onto a
    # thank-you page and reported "Submit button not found" for an application that had gone through.
    try:
        page.wait_for_timeout(SUBMIT_SETTLE_MS)
    except Exception:
        pass
    verify_since: float | None = None
    captcha_since: float | None = None
    inflight_deadline = time.time() + SUBMIT_INFLIGHT_MAX_S
    answered_dialogs: set[str] = set()
    while time.time() < deadline:
        try:
            url = page.url.lower()
            if url != start_url.lower() and confirm_url(url):
                return True
            if answer_submit_dialog(page, answered_dialogs):
                page.wait_for_timeout(SUBMIT_SETTLE_MS)
                continue
            body = clean(page.evaluate("() => (document.body && document.body.innerText) || ''")).lower()
            if any(t in body for t in CONFIRM_TEXTS if t not in stale_phrases):
                if not form_errors(page):
                    return True
            elif stale_phrases and any(t in body for t in stale_phrases) and _typeable_count(page) == 0 \
                    and not form_errors(page):
                return True     # the form went and its old thank-you text is all that is left: a real one
            # An emailed-code step is not a failure and will never turn into a confirmation on its own, so
            # stop waiting once it is really there — but only once it has persisted, never on first sight,
            # which is indistinguishable from the last frame of a page that is already on its way out.
            prompt = verification_prompt(page)
            if prompt:
                verify_since = verify_since if verify_since is not None else time.time()
                if time.time() - verify_since >= VERIFY_GRACE_S:
                    raise VerificationRequired(prompt)
            else:
                verify_since = None
            # A challenge on the submit. Given a moment first: a managed Turnstile often ticks itself
            # within a second or two, and pausing on first sight would hand back every application that
            # was going through on its own.
            if detect_captcha(page, raise_=False):
                captcha_since = captcha_since if captcha_since is not None else time.time()
                if time.time() - captcha_since >= CAPTCHA_GRACE_S:
                    raise NeedsHuman(CAPTCHA_MSG)
            else:
                captcha_since = None
            # The form's own button still says "Submitting..." — the click landed and the site is working.
            # Keep waiting rather than calling a submission that is still in progress a failure.
            if time.time() > deadline - 2 and time.time() < inflight_deadline and submit_in_flight(page):
                deadline = min(time.time() + 5, inflight_deadline)
            errors = form_errors(page)
            if errors:
                # The form bounced. Saying which field it rejected beats "Submit not confirmed", which sent
                # us hunting through the whole flow for what was really one empty required input.
                raise ApplyError("Form rejected: " + "; ".join(errors[:5]))
            # "You have already applied for this job" is not a refusal to work around: the application is
            # with the employer. Reported as a plain failure it put a card in the failed pile whose Retry
            # could only ever fetch the same notice back, so it is told apart from the quota and closed-
            # posting refusals below, which are failures.
            applied = already_applied_message(page)
            if applied:
                raise AlreadyApplied(applied)
            blocked = submit_blocked_message(page)
            if blocked:
                # The site refused outright (application quota, closed posting). Refilling and
                # resubmitting cannot help, so stop here and quote it.
                raise ApplyError(blocked[:400])
            # The form went away and nothing complained: see the docstring.
            if (fields_before >= 3 and _typeable_count(page) == 0 and not submit_in_flight(page)
                    and not _button_named_visible(page, names) and not detect_bot_block(page, raise_=False)):
                gone_since = gone_since if gone_since is not None else time.time()
                if time.time() - gone_since >= FORM_GONE_GRACE_S:
                    log.info("submit confirmed by the form disappearing (no confirmation text recognised) on %s",
                             (page.url or "")[:100])
                    return True
            else:
                gone_since = None
        except (NeedsHuman, ApplyError):
            raise
        except Exception:
            pass
        page.wait_for_timeout(700)

    errors = form_errors(page)
    if errors:
        raise ApplyError("Form rejected: " + "; ".join(errors[:5]))
    applied = already_applied_message(page)
    if applied:
        raise AlreadyApplied(applied)
    blocked = submit_blocked_message(page)
    if blocked:
        raise ApplyError(blocked[:400])
    raise ApplyError("Submit not confirmed — no confirmation page and no error message on the form")


# Challenge frames, matched on the vendor URL. Cloudflare's own host covers Turnstile and the interstitial.
_CAPTCHA_FRAME_RE = re.compile(
    r"challenges\.cloudflare\.com|/turnstile/|recaptcha/api2|recaptcha/enterprise|hcaptcha\.com"
    r"|arkoselabs|funcaptcha|geetest|captcha-delivery\.com|perimeterx|awswaf", re.I)

# A vendor frame that never asks anything of the user, so its size proves nothing. reCAPTCHA v3, v2-invisible
# and Enterprise score-based all load their anchor frame with size=invisible: that frame is the 256x60
# "protected by reCAPTCHA" badge, parked in a fixed div hanging off the right edge of the window. The tick-box
# is the same URL with size=normal|compact and the image challenge is a separate .../bframe frame, so neither
# matches — the bframe stays in, because it *is* the puzzle; it is told apart from the dormant copy that sits
# on every v2 page by whether it is painted (`_frame_element_painted`). Deliberately reCAPTCHA-only: hCaptcha's invisible checkbox frame renders 0x0 and is already under
# the size floor, while its frame=challenge popup can also carry size=invisible, so a vendor-agnostic rule
# would wave a real puzzle through. Mirrored by the regex literal inside detect_captcha's page script; keep
# the two in step.
_PASSIVE_FRAME_RE = re.compile(r"recaptcha/(?:api2|enterprise)/anchor\?[^#]*\bsize=invisible\b", re.I)

# The tick-box itself, as opposed to the puzzle it may open: reCAPTCHA's visible anchor (the invisible one is
# passive, above) and hCaptcha's frame=checkbox. Their puzzles are other frames (.../bframe, frame=challenge).
_CHECKBOX_FRAME_RE = re.compile(r"recaptcha/(?:api2|enterprise)/anchor\?|hcaptcha\.com/.*[#&?]frame=checkbox", re.I)


def _owning_page(page: Any) -> Any:
    """The Page behind a Page or a FrameView (a Frame carries the page it belongs to as `.page`)."""
    return getattr(page, "page", None) or page


def captcha_token_present(page: Any) -> bool:
    """True when a challenge on this page has already handed its token to the form.

    This is what tells a challenge apart from a widget that has finished. Turnstile does not disappear when
    it passes — it keeps its 300x65 box and shows a green tick — so size alone called every solved widget a
    blocker, which would have paused a run on every Cloudflare-protected form that was working perfectly.
    The hidden response input stays in the light DOM even when the widget itself is in a closed shadow
    root, so it is readable where the widget is not.
    """
    try:
        return bool(page.evaluate(
            """() => {
                for (const n of ['cf-turnstile-response', 'g-recaptcha-response', 'h-captcha-response']) {
                    for (const el of document.getElementsByName(n)) {
                        if ((el.value || '').length > 20) return true;
                    }
                }
                return false;
            }"""))
    except Exception:  # noqa: BLE001
        return False


def _frame_element_painted(el: Any) -> bool:
    """Playwright's own visibility test, which bounding_box() is not.

    Regression (application 122, LG Electronics on Dayforce): bounding_box() is pure geometry — it happily
    measures an element that is painted nowhere. reCAPTCHA v2-invisible parks its image challenge (the
    .../bframe frame) on the page from load, inside a wrapper carrying `visibility: hidden; opacity: 0;
    top: -10000px`, and only unhides it if Google decides to ask. The iframe inside that wrapper is
    `position: fixed` at 100% x 100%, so it escapes the wrapper's offset and measures the *whole viewport*,
    which read as a 1470x722 challenge standing there with no token — the run paused on a form that had
    never been asked anything. is_visible() honours the `visibility: hidden` the iframe inherits, which is
    the same test the in-page scan in detect_captcha already applies, so the two paths now agree.

    An object that is not a Playwright element handle falls back to the measurement, so that a missing
    method can only ever cost the extra check, never blind the scan to a challenge that is really up.
    """
    try:
        return bool(el.is_visible())
    except Exception:  # noqa: BLE001
        return True


def _captcha_frame_box(page: Any, min_px: int) -> bool:
    """One measurement: is a vendor challenge frame on screen right now? The reCAPTCHA badge is not one."""
    try:
        frames = list(_owning_page(page).frames)
    except Exception:  # noqa: BLE001
        return False
    for fr in frames[1:]:       # [0] is the main frame, which has no frame element
        try:
            url = fr.url or ""
            if not _CAPTCHA_FRAME_RE.search(url) or _PASSIVE_FRAME_RE.search(url):
                continue
            el = fr.frame_element()
            if not _frame_element_painted(el):
                continue
            box = el.bounding_box()
        except Exception:  # noqa: BLE001 - a frame detaching mid-scan is not a challenge
            continue
        if box and box.get("width", 0) > min_px and box.get("height", 0) > min_px:
            return True
    return False


def captcha_frame_showing(page: Any, min_px: int = 30, confirm_s: float = CAPTCHA_CONFIRM_S) -> bool:
    """A vendor challenge frame that is really on screen, found through Playwright instead of the DOM.

    The DOM scan in detect_captcha cannot see a Cloudflare Turnstile at all. Turnstile renders its widget
    into a *closed* shadow root, so `document.querySelectorAll('iframe')` returns an empty list, and the
    "Verify you are human" prompt sits inside a cross-origin frame, so `document.body.innerText` never
    carries it either — both halves of the scan are blind at once, and the class hook (`.cf-turnstile`)
    only exists on forms that render the widget implicitly, which Workable's does not. Playwright's frame
    list is not blind: it enumerates frames below a closed shadow root, and frame_element() measures them.

    Size is the first test — an invisible Turnstile renders 0x0, though the invisible reCAPTCHA badge does
    not, so it is excluded by URL (`_PASSIVE_FRAME_RE`) before anything is measured — and a token already
    handed to the form is the second, because a widget that has passed keeps its box and only shows a tick.
    Nor does one sighting
    count: a *managed* Turnstile draws its box, spins and ticks itself within a second or two, so the frame
    is measured again after a pause and only a challenge still standing then is treated as blocking. All
    three together keep this from pausing runs that were going through by themselves, which is the only way
    a captcha check earns its place in an unattended run.
    """
    if not _captcha_frame_box(page, min_px) or captcha_token_present(page):
        return False
    try:
        page.wait_for_timeout(int(confirm_s * 1000))
    except Exception:  # noqa: BLE001
        time.sleep(confirm_s)
    return _captcha_frame_box(page, min_px) and not captcha_token_present(page)


# Mirrored by the regex literal inside submit_in_flight's page script; keep the two in step.
_INFLIGHT_RE = re.compile(r"submitting|sending|uploading|please wait|processing|in progress", re.I)


def submit_in_flight(page: Any) -> bool:
    """True while the form's own submit control still says it is working.

    Workable disables its button and relabels it "Submitting..." the moment the click lands, and holds it
    there until its captcha hands back a token. That is a submission still in progress, not a form that
    ignored the click, and calling it at the 20s mark reported "Submit not confirmed" for an application
    that had not finished being sent.
    """
    try:
        return bool(page.evaluate(
            "() => {" + DEEP_JS +
            """ const vis = el => { const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
                for (const el of deepAll("button, input[type=submit]")) {
                    if (!vis(el)) continue;
                    const busy = el.disabled || el.getAttribute('aria-busy') === 'true'
                              || el.getAttribute('aria-disabled') === 'true';
                    if (!busy) continue;
                    const t = ((deepText(el) || el.value || '') + ' ' + deepAttr(el, 'aria-label'))
                        .replace(/\s+/g, ' ').trim();
                    if (/submitting|sending|uploading|please wait|processing|in progress/i.test(t)) return true;
                }
                return false;
            }"""))
    except Exception:  # noqa: BLE001
        return False


# "You've already applied for this job." Boards say this in a dozen shapes and the apostrophe is as often
# curly as straight, so both are allowed everywhere one can appear. Kept wide on the phrasing and narrow on
# the subject: "already applied" alone also appears in privacy blurb and in adverts for other roles.
_THIS_JOB = r"th(?:is|e)\s+(?:job|position|role|opening|vacancy|posting)"
_ALREADY_APPLIED_RE = re.compile(
    # "You've already applied." / "...already applied for this job." Anchored on either the full stop or a
    # qualifier naming this job, so prose that merely contains the words ("...applications you have already
    # applied elsewhere") cannot mark a job as submitted that was never sent.
    rf"you\s*(?:['\u2019]ve|\s+have)?\s*already\s+applied\s*(?:[.!]|$|(?:for|to)\s+{_THIS_JOB})"
    rf"|you\s+applied\s+(?:for|to)\s+{_THIS_JOB}\s+on\b"
    r"|already\s+submitted\s+an\s+application"
    r"|duplicate\s+application", re.I)


def already_applied_message(page: Any) -> str:
    """The employer's own "you have applied to this already" notice, '' if there is none.

    Worth its own check rather than folding into submit_blocked_message: this one is not a refusal to fix
    but a statement that the work is done, and the two deserve opposite outcomes on the card.
    """
    try:
        text = clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))
    except Exception:  # noqa: BLE001
        return ""
    m = _ALREADY_APPLIED_RE.search(text or "")
    if not m:
        return ""
    start = max(0, text.rfind(".", 0, m.start()) + 1)
    chunk = text[start:m.end() + 120].strip()
    return (re.split(r"(?<=\.)\s(?=[A-Z])", chunk)[0] or chunk)[:200]


def raise_if_already_applied(page: Any) -> None:
    """Stop the run when the employer says the application is already in."""
    notice = already_applied_message(page)
    if notice:
        raise AlreadyApplied(notice)


# A page-level refusal: the submit went through and the site said no. Distinct from a field validation error
# (which refill can fix) and from a silent non-confirmation (which tells the user nothing).
_SUBMIT_BLOCKED_RE = re.compile(
    r"could\s*n[o']?t submit|can\s*not submit|cannot submit|unable to submit|we\s+could\s*n[o']?t"
    r"|maximum number of applications|application limit|reached the (?:maximum|limit)"
    r"|already applied|duplicate application|no longer accepting|applications are closed"
    r"|this (?:job|position|role) is (?:closed|no longer)", re.I)


def submit_blocked_message(page: Any) -> str:
    """The site's own explanation for refusing the submission, '' if there is none.

    Reported verbatim: "Submit not confirmed" sent the user hunting through a form that was filled correctly
    and submitted, when the page already said "You have reached the maximum number of applications".
    """
    try:
        raw = page.evaluate("() => (document.body && document.body.innerText) || ''") or ""
    except Exception:
        return ""
    # Line by line, because a refusal is a heading with its explanation on the next line. Cut at the last
    # full stop instead, a page whose text had none before the refusal returned everything above it: Ashby's
    # "We couldn't submit your application" (application 168, Doppel) came back as the job's title, location
    # and pay band, with the reason — "flagged as possible spam" — cut off the end.
    lines = [clean(ln) for ln in raw.splitlines() if clean(ln)]
    for i, line in enumerate(lines):
        if _SUBMIT_BLOCKED_RE.search(line):
            nxt = lines[i + 1] if i + 1 < len(lines) and len(line) < 120 else ""
            return f"{line} {nxt}".strip()[:300]
    return ""


# Announcements that share the markup of an error without being one. Boards put upload confirmations and
# save notices in the same [role=alert] live region as their validation messages, so "…successfully
# uploaded" came back as a form error — and a step that had just done exactly what was asked was reported
# as stuck on it.
# Anchored: a field's own message can carry the tally after it ("Enter a maximum of 50 characters. (1 out of 7
# issues)" plus the toast's text), and that one must stay attached to its field.
_SUMMARY_BANNER_RE = re.compile(r"^\W*(?:you\s+have\s+\d+\s+(?:issues?|errors?|problems?)\b|there\s+(?:are|is)\s+\d+\s+"
                                r"(?:issues?|errors?|problems?)\b|please\s+(?:correct|fix)\s+the\s+(?:errors?|issues?)\b)", re.I)
# ...and the hint printed under a multi-select ("Select a maximum of 3 locations", Nationwide, application 314):
# it is there whether or not anything is wrong, and read as a complaint it kept every refill pass busy.
_NOT_AN_ERROR_RE = re.compile(r"success|uploaded|saved\b|complete[ds]?\b|thank you|no errors"
                              r"|^\W*\w?\s*select\s+(?:a\s+maximum\s+of|up\s+to)\s+\d+\b", re.I)


def form_errors(page: Any) -> list[str]:
    """Per-field validation messages, from the page's own markup and from the browser's own validation.

    The second half matters as much as the first. When a required field is empty the browser refuses to
    submit and draws "Please fill out this field." itself — that bubble is browser chrome, not DOM, so no
    selector finds it. Nothing navigates and no banner appears, and the run ends on "Submit not confirmed",
    which describes the symptom and hides the cause: one empty field the adapter failed to fill.
    """
    try:
        found = page.evaluate(
            "() => {" + DEEP_JS +
            """ const vis = el => { const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
                const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
                const out = [];

                // (1) messages the page rendered itself
                // A single-page app announces each new page's title in a [role=alert] live region (Next.js's
                // route announcer, application 175's "Apply - Forward Deployed Engineer"). That is navigation,
                // not a complaint, and read as one it turned a confirmed submit into a "repair" and a pause.
                const announcer = el => !!deepClosest(el, 'next-route-announcer, [id*=announcer i], [class*=announcer i], [data-testid*=announcer i]') || clean(el.innerText || el.textContent) === clean(document.title);
                const sel = "[aria-invalid=true], .field-error, .error, [class*='error' i], [role=alert]";
                for (const el of deepAll(sel)) {
                    if (!vis(el) || announcer(el)) continue;
                    const t = clean(el.innerText || el.textContent);
                    if (t && t.length < 160) out.push(t);
                }

                // (1b) messages tied to a control by aria-describedby / aria-errormessage. LinkedIn's
                // native-<dialog> Easy Apply draws "This field is required" / "Invalid input" as a bare red
                // <p> inside the node the control points at -- no role, no aria-invalid, hashed classes --
                // so the pass above saw nothing and the step paused as "gave no reason" (application 178).
                // That node also holds a "0/20" character counter, so only red or error-worded text counts.
                const red = el => { const m = getComputedStyle(el).color.match(/\\d+/g) || [];
                    return m.length >= 3 && +m[0] > 150 && +m[1] < 100 && +m[2] < 100; };
                const errWords = /required|invalid|must|please (?:enter|select|choose|provide)|enter an? |not valid|too (?:long|short)/i;
                for (const c of deepAll('[aria-describedby], [aria-errormessage]')) {
                    const ids = ((c.getAttribute('aria-describedby') || '') + ' ' + (c.getAttribute('aria-errormessage') || '')).split(/\\s+/).filter(Boolean);
                    const q = clean(c.getAttribute('aria-label') || (c.labels && c.labels[0] && c.labels[0].innerText) || '');
                    for (const id of ids) {
                        const box = (c.getRootNode().getElementById ? c.getRootNode() : document).getElementById(id);
                        if (!box || !vis(box)) continue;
                        for (const leaf of [box, ...box.querySelectorAll('*')]) {
                            if (leaf.children.length) continue;     // leaves only, the box itself included
                            const t = clean(leaf.innerText || leaf.textContent);
                            if (!t || t.length >= 160 || !vis(leaf) || !(red(leaf) || errWords.test(t))) continue;
                            out.push(q ? q.replace(/\\s*\\*+\\s*$/, '') + ': ' + t : t);
                        }
                    }
                }

                // (2) the browser's own constraint validation, which renders outside the DOM
                const labelFor = e => {
                    if (e.labels && e.labels.length) return clean(e.labels[0].innerText);
                    const al = e.getAttribute('aria-label'); if (al) return clean(al);
                    const wrap = deepClosest(e, 'li,div,fieldset,label');
                    if (wrap) { const t = clean(wrap.innerText); if (t) return t.slice(0, 80); }
                    return clean(e.getAttribute('name') || e.getAttribute('id'));
                };
                for (const e of deepAll('input, select, textarea')) {
                    if (e.disabled || e.type === 'hidden') continue;
                    // Visible, or drawn by a visible label (custom widgets hide the real input behind one).
                    // Without this the pass also reads the steps a single-page wizard keeps mounted out of
                    // sight, so a clean submit came back as "Form rejected" naming a field on another page.
                    if (!vis(e) && !(e.labels && e.labels.length && vis(e.labels[0]))) continue;
                    if (typeof e.checkValidity !== 'function' || e.checkValidity()) continue;
                    const name = labelFor(e) || 'a required field';
                    out.push(name.replace(/\\s*\\*+\\s*$/, '') + ': ' + (e.validationMessage || 'is required'));
                }
                return [...new Set(out)];
            }""")
    except Exception:
        return []
    return [clean(t) for t in (found or [])
            if clean(t) and not _NOT_AN_ERROR_RE.search(t)]


# ---------- field-level rejection diagnosis ----------
# `form_errors` says the form bounced; this says which control it bounced on. Without it a rejection is a
# sentence and the only possible response is to fill the same form the same way again — which is what
# happened seven times running to one C3 AI application ("Please select a school, degree, and field of
# study from the suggestions") and seven times to one Presight application ("There are 4 issues that need
# your attention", whose four the run never read). A retry that cannot name a field cannot change one.
_ERR_STAMP = "data-jobbot-err"

_FIELD_ERRORS_JS = """
    const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
    const vis = el => { try { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
      } catch (err) { return false; } };
    const NOT_ERR = /success|uploaded|saved\\b|complete[ds]?\\b|thank you|no errors/i;
    // Dropdowns built out of divs are controls here too (WIDGET_JS): a complaint about one used to land on
    // the nearest text box instead -- Shopee's "Education Level: please select" on the CGPA box beside it.
    widgetRoots('body');
    const CTRL = "input,select,textarea,[role=combobox],[contenteditable='true'],[data-jobbot-widget]";
    const fillable = e => {
        if (!e || !e.matches || !e.matches(CTRL)) return false;
        // A chat widget is never the application, form of its own or not: Personio's "AI Chat Concierge"
        // box was typed into as a question (application 460).
        if (e.closest("[class*=chat i], [id*=chat i], [aria-label*=chat i], [class*=concierge i], [class*=intercom i], [id*=intercom i], [class*=drift i], [id*=drift i], [class*=livechat i], [class*=messenger i], [role=log]")) return false;
        if (!e.closest('form') && e.closest('nav, footer, header, [role=navigation], [role=contentinfo], [role=banner], [role=menubar], [role=menu], [class*=footer i], [id*=footer i], [class*=navbar i], [class*=site-header i], [class*=global-header i], [class*=social i], [class*=chat i], [id*=chat i], [aria-label*=chat i], [class*=concierge i], [class*=intercom i], [id*=intercom i], [class*=drift i], [id*=drift i], [class*=livechat i], [class*=messenger i], [role=log]'))
            return false;                                         // site chrome: a menu, a language picker, a footer
        if (e.hasAttribute('data-jobbot-widget')) return vis(e);
        if (e.closest('[data-jobbot-widget]')) return false;     // a widget's own search box: the widget is the control
        const t = (e.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'reset', 'image'].indexOf(t) >= 0) return false;
        if (e.disabled) return false;
        return vis(e) || !!(e.labels && e.labels.length && vis(e.labels[0]));
    };
    // Stamps from the previous attempt are cleared first: the numbering restarts each time, so a leftover
    // "0" on a control that is no longer in error would be the one Python then reached for.
    for (const old of deepAll('[data-jobbot-err]')) old.removeAttribute('data-jobbot-err');
    let n = 0;
    const stamp = e => {
        if (!e.hasAttribute('data-jobbot-err')) e.setAttribute('data-jobbot-err', String(n++));
        return e.getAttribute('data-jobbot-err');
    };
    const real = t => /[\\p{L}\\p{N}]/u.test(t || '');
    // Text as it reads on screen, <slot> content included. A web-component form (SmartRecruiters' spl-*
    // fields, application 172) passes its question into the label through a slot, so the label's own text
    // is the required star alone and the question itself is only reachable through the slot.
    const slotText = n => {
        if (!n) return '';
        if (n.nodeType === 3) return n.textContent;
        if (n.nodeType !== 1 || ['STYLE', 'SCRIPT', 'TEMPLATE'].includes(n.tagName)) return '';
        if (n.tagName === 'SLOT') {
            const a = n.assignedNodes({flatten: true});
            return (a.length ? a : [...n.childNodes]).map(slotText).join(' ');
        }
        return [...(n.shadowRoot ? n.shadowRoot.childNodes : n.childNodes)].map(slotText).join(' ');
    };
    const tx = n => { const a = clean(n.innerText || n.textContent); return real(a) ? a : clean(slotText(n)); };
    const labelFor = e => {
        if (e.labels && e.labels.length) { const t = tx(e.labels[0]); if (real(t)) return t; }
        const by = e.getAttribute('aria-labelledby');
        if (by) { const root = e.getRootNode();
            for (const id of by.split(/\\s+/)) {
                const nd = root.getElementById ? root.getElementById(id) : document.getElementById(id);
                if (nd) { const t = tx(nd); if (real(t)) return t; } } }
        const al = e.getAttribute('aria-label'); if (real(al)) return clean(al);
        let p = e.parentElement, hops = 0;
        while (p && hops++ < 5) {
            const lab = p.querySelector('label, legend, [class*="label" i]');
            if (lab) { const t = tx(lab); if (real(t)) return t.slice(0, 120); }
            p = p.parentElement;
        }
        return clean(e.getAttribute('placeholder') || e.getAttribute('name') || e.getAttribute('id'));
    };
    const kindOf = e => {
        if (e.hasAttribute('data-jobbot-widget')) return wKind(e) === 'date' ? 'date' : 'combobox';
        const tag = e.tagName.toLowerCase();
        if (tag === 'select') return 'select';
        if (tag === 'textarea') return 'textarea';
        if (e.getAttribute('role') === 'combobox' || e.getAttribute('aria-autocomplete')) return 'combobox';
        const t = (e.getAttribute('type') || 'text').toLowerCase();
        if (t === 'checkbox' || t === 'radio' || t === 'file') return t;
        if (t === 'number' || e.getAttribute('inputmode') === 'numeric') return 'number';
        return 'text';
    };
    const valueOf = e => {
        if (e.hasAttribute('data-jobbot-widget')) return wValue(e);
        const tag = e.tagName.toLowerCase();
        if (tag === 'select') return clean(e.selectedIndex >= 0 && e.options[e.selectedIndex]
                                           ? e.options[e.selectedIndex].textContent : '');
        const t = (e.getAttribute('type') || '').toLowerCase();
        if (t === 'checkbox' || t === 'radio') return e.checked ? 'checked' : '';
        return clean(e.value || e.getAttribute('value') || e.textContent);
    };
    // Which controls a message is about. It is rarely attached to one in any way a selector follows, so:
    // the aria reference if there is one, else the nearest wrapper holding exactly one field. A wrapper
    // holding several is a section rather than a field — but a message often names the fields it means
    // ("Please select a school, degree, and field of study from the suggestions"), and matching those
    // names against the labels in the section is what lets one complaint repair all three boxes.
    const ownersOf = (node, msg) => {
        const id = node.getAttribute && node.getAttribute('id');
        if (id) {
            for (const e of deepAll('[aria-describedby],[aria-errormessage]')) {
                const ref = (e.getAttribute('aria-describedby') || '') + ' '
                          + (e.getAttribute('aria-errormessage') || '');
                if (ref.split(/\\s+/).indexOf(id) >= 0 && fillable(e)) return [e];
            }
        }
        const low = clean(msg).toLowerCase();
        // A field's whole block flagged as erroneous (".shopee-form-item--error", ".has-error", ".is-invalid"
        // on the wrapper) names the controls inside it. Walking *up* from such a block is what pinned one
        // field's complaint on its neighbour (application 274): the block's own control was a div-built
        // dropdown nothing counted, so the climb went on until it met the next text box.
        const own = Array.prototype.filter.call(node.querySelectorAll(CTRL), fillable);
        if (own.length) {
            if (own.length <= 3) {
                const bad = own.filter(e => e.getAttribute('aria-invalid') === 'true');
                return bad.length ? bad : own;
            }
            const named = own.filter(e => {
                const l = labelFor(e).toLowerCase().replace(/[*:]/g, '').trim();
                return l.length > 2 && low.indexOf(l) >= 0;
            });
            return named.length ? named : [];
        }
        let p = node.parentElement, hops = 0;
        while (p && hops++ < 5) {
            const inside = Array.prototype.filter.call(p.querySelectorAll(CTRL), fillable);
            if (inside.length === 1) return inside;
            if (inside.length > 1) {
                const bad = inside.filter(e => e.getAttribute('aria-invalid') === 'true');
                if (bad.length) return bad;
                const named = inside.filter(e => {
                    const l = labelFor(e).toLowerCase().replace(/[*:]/g, '').trim();
                    return l.length > 2 && low.indexOf(l) >= 0;
                });
                return named.length ? named : [];
            }
            p = p.parentElement;
        }
        return [];
    };
    const out = [];
    const add = (els, msg) => {
        const m = clean(msg);
        if (!m || m.length > 200 || NOT_ERR.test(m)) return;
        if (!els || !els.length) {
            if (!out.some(o => o.id === null && o.message === m))
                out.push({id: null, label: '', kind: '', value: '', required: false, message: m});
            return;
        }
        for (const el of els) {
            const sid = stamp(el);
            if (out.some(o => o.id === sid)) continue;
            out.push({id: sid, label: labelFor(el), kind: kindOf(el), value: valueOf(el),
                      required: !!(el.required || el.getAttribute('aria-required') === 'true'), message: m});
        }
    };
    // Boards spell an error container a dozen ways: .error, .field-error, .err, .is-invalid,
    // .validation-message, .help-block, an aria live region. Missing one costs the whole diagnosis, and
    // the NOT_ERR filter plus the 200-character cap already keep ordinary prose out.
    const ERR_SEL = "[aria-invalid=true], [role=alert], [aria-live=assertive], [aria-live=polite],"
                  + " [class*='err' i], [class*='invalid' i], [class*='validation' i], .help-block,"
                  + " [class*='warning' i], [id*='error' i], [id*='err' i]";
    // A page title read out by a route announcer is navigation, not a complaint (see form_errors).
    const announcer = el => !!deepClosest(el, 'next-route-announcer, [id*=announcer i], [class*=announcer i], [data-testid*=announcer i]') || clean(el.innerText || el.textContent) === clean(document.title);
    // What a flagged block is complaining about. For a block that holds the field itself (label, control
    // and message together) that is the marked line inside it, never the block's whole text: "Education
    // Level* Education Level Please select an option" is three things run together, of which only the last
    // is the complaint.
    const ERR_LEAF = /error|invalid|message|explain|help|hint|feedback|warning|validation/i;
    const errText = el => {
        const inside = Array.prototype.filter.call(el.querySelectorAll(CTRL), fillable);
        if (!inside.length) return el.innerText || el.textContent;
        const leaves = Array.prototype.filter.call(el.querySelectorAll('*'), n => vis(n) && !n.matches(CTRL)
            && !n.querySelector(CTRL) && ERR_LEAF.test((n.className || '').toString() + ' ' + (n.id || ''))
            && clean(n.innerText || n.textContent));
        const leaf = leaves.filter(n => !leaves.some(m => m !== n && n.contains(m)))[0];
        if (leaf) return leaf.innerText || leaf.textContent;
        let t = clean(el.innerText || el.textContent);
        for (const c of inside) { const l = clean(labelFor(c)); if (l) t = t.split(l).join(' '); }
        return clean(t);
    };
    for (const el of deepAll(ERR_SEL)) {
        if (!vis(el) || announcer(el)) continue;
        if (fillable(el)) {
            // An input flagged invalid is picked up on its own below; a div-built dropdown carrying the
            // error class on its root has no other way in.
            if (el.hasAttribute('data-jobbot-widget')) add([el], 'the form marked this field invalid');
            continue;
        }
        const txt = errText(el);
        add(ownersOf(el, txt), txt);
    }
    // An aria-errormessage reference is unambiguous, whatever the container is called.
    for (const e of deepAll('[aria-errormessage]')) {
        if (!fillable(e)) continue;
        const root = e.getRootNode();
        for (const id of (e.getAttribute('aria-errormessage') || '').split(/\\s+/)) {
            const nd = root.getElementById ? root.getElementById(id) : document.getElementById(id);
            if (nd && vis(nd)) add([e], nd.innerText || nd.textContent);
        }
    }
    for (const e of deepAll('[aria-invalid=true]')) {
        if (fillable(e)) add([e], 'the form marked this field invalid');
    }
    for (const e of deepAll('input, select, textarea')) {
        if (!fillable(e)) continue;
        if (typeof e.checkValidity !== 'function' || e.checkValidity()) continue;
        add([e], e.validationMessage || 'is required');
    }
    return out;
}"""


def field_errors(page: Any) -> list[dict]:
    """Every validation message the page is showing, with the control it is about where that can be told.

    Each entry: `id` (the stamp written on the control, None when the message names no single field),
    `label`, `kind`, `value` (what the control holds now), `required`, `message`. The stamp is how Python
    reaches the same control again — `page.locator('[data-jobbot-err="3"]')` — without having to re-derive
    it from a label that may not be unique.
    """
    try:
        found = page.evaluate("() => {" + DEEP_JS + WIDGET_JS + _FIELD_ERRORS_JS)
    except Exception as e:  # noqa: BLE001 - diagnosis must never become the failure
        log.debug("field_errors failed: %s", e)
        return []
    out: list[dict] = []
    log.debug("field_errors raw: %s", [(f.get("id"), str(f.get("label"))[:40], str(f.get("message"))[:80])
                                       for f in (found or []) if isinstance(f, dict)][:12])
    for f in found or []:
        if not isinstance(f, dict):
            continue
        msg = clean(str(f.get("message") or ""))
        if not msg or _NOT_AN_ERROR_RE.search(msg):
            continue
        if f.get("id") is not None and _SUMMARY_BANNER_RE.search(msg):
            # The page's tally, not this field's complaint: Oracle pins its toast "You have 8 issues that need
            # to be fixed" to every field it marked invalid (Nationwide, application 314). The field IS one of
            # the issues — keep it, with its own inline message when the wrapper shows one.
            own = _inline_message(page, f.get("id"))
            msg = own or "This field was marked invalid."
        out.append({"id": f.get("id"), "label": clean(str(f.get("label") or "")),
                    "kind": str(f.get("kind") or ""), "value": clean(str(f.get("value") or "")),
                    "required": bool(f.get("required")), "message": msg})
    return out


_INLINE_ERR_RE = re.compile(r"\b(?:enter|required|invalid|maximum|minimum|must|select|choose|provide|format)\b", re.I)


def _inline_message(page: Any, stamp: Any) -> str:
    """The validation line drawn inside a stamped field's own wrapper, '' when there is none."""
    try:
        text = page.locator(f'[{_ERR_STAMP}="{stamp}"]').first.evaluate("""e => {
            let p = e.parentElement;
            for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
                if (p.querySelectorAll('input, textarea, select, [role=combobox]').length > 2) break;
                const t = p.innerText || ''; if (t.split('\\n').length > 1) return t; }
            return ''; }""") or ""
    except Exception:  # noqa: BLE001
        return ""
    for line in (clean(x) for x in str(text).splitlines()):
        if line and _INLINE_ERR_RE.search(line) and not _SUMMARY_BANNER_RE.search(line) and len(line) < 200:
            return line
    return ""


def _error_shape(msg: str) -> str:
    """A rejection message with the parts that vary between attempts taken out, so two attempts can be
    compared. "1 out of 4 issues" and "2 out of 4 issues" are the same complaint."""
    return re.sub(r"\d+", "#", (msg or "").strip().lower())[:80]


def rejection_signature(fields: list[dict], errors: list[str] | None = None) -> str:
    """What this rejection *is*, as one comparable string.

    The retry budget is spent on distinct diagnoses rather than on attempts: pressing Submit again is only
    worth doing when something about the complaint or the form has changed. An identical signature twice
    means the last repair achieved nothing, and one more identical attempt is not going to either.

    Falls back to the loose messages when nothing could be attributed to a control, so a form whose only
    complaint is "There are 4 issues that need your attention" is still comparable between attempts.
    """
    parts = sorted({f"{(f.get('label') or '')[:60].strip().lower()}|{_error_shape(f.get('message') or '')}"
                    for f in fields or []})
    if not parts:
        parts = sorted({_error_shape(m) for m in (errors or []) if m})
    return " ; ".join(parts)


def _note_rejected_answer(label: str, value: str, why: str) -> None:
    """Mark a remembered answer the form has just refused, so it is not replayed on the next application.

    Only when the cache is what put the value there: a value that came from facts.yaml is the candidate's
    own and is not jobbot's to overrule, and one the page prefilled was never an answer at all.
    """
    value = (value or "").strip()
    if not value:
        return
    try:
        from jobbot import answers as store
        from jobbot import config
        key = store.normalize_question(label)
        if not key:
            return
        records = config.load_answer_records()
        rec = records.get(key)
        if rec is None or (rec.answer or "").strip() != value or not rec.usable:
            return
        if rec.source == "rule" or store.normalize_question(why) == key:
            # A rule's value is facts.yaml's, which the form cannot overrule from here. And a "complaint"
            # that is only the field's own label is not a complaint: Greenhouse's newer board flags every
            # field on a bounced submit, so a correct "Jawad" in Preferred First Name was marked rejected
            # (application 182) because the textarea below it was empty.
            return
        if rec.trust >= store.TRUST["typed"]:
            # The candidate's own answer, given in the UI or typed into the window, is not jobbot's to
            # throw away on the strength of a validation message -- least of all one that may be about a
            # different control: Shopee's "Education Level: please select an option" was attributed to the
            # CGPA box beside it, and the CGPA the candidate had just supplied was marked rejected
            # (application 274). The complaint is noted on the record and the answer stays in use.
            rec.note = f"a form complained near it: {why[:120]}"
            config.save_answer_records({key: rec})
            log.info("answers.json: %r kept (your own answer) despite the form's complaint %r", key[:60], why[:60])
            return
        rec.confidence = "rejected"
        rec.note = f"the form refused it: {why[:120]}"
        config.save_answer_records({key: rec})
        log.info("answers.json: %r marked rejected — the form refused %r (%s)", key[:60], value[:40], why[:60])
    except Exception as e:  # noqa: BLE001
        log.debug("could not mark %r rejected: %s", label, e)


_URL_ERROR_RE = re.compile(r"\burl\b|\blink\b|linkedin|github|website|profile", re.I)


def _clear_control(el: Any) -> None:
    for attempt in (lambda: el.fill("", timeout=SHORT),
                    lambda: el.evaluate("e => { e.value = ''; "
                                        "e.dispatchEvent(new Event('input', {bubbles: true})); "
                                        "e.dispatchEvent(new Event('change', {bubbles: true})); }")):
        try:
            attempt()
            return
        except Exception:  # noqa: BLE001
            continue


def fit_phone_pattern(el: Any, phone: str, national: str = "") -> str:
    """Write the number in the first shape the box's own validity accepts; '' when none does."""
    digits = re.sub(r"\D", "", phone or "")
    nat = re.sub(r"\D", "", national or "")
    if not digits:
        return ""
    code = digits[: len(digits) - len(nat.lstrip("0"))] if nat and digits.endswith(nat.lstrip("0")) else ""
    rest = nat.lstrip("0") if nat else digits
    shapes = [f"+{digits}", digits, f"00{digits}", nat, rest,
              f"+{code} {rest}" if code else "", f"+{code}-{rest}" if code else "",
              f"+{code} {rest[:4]} {rest[4:]}" if code else "", f"({code}) {rest}" if code else ""]
    for shape in dict.fromkeys(x for x in shapes if x):
        try:
            ok = el.evaluate("""(e, v) => { const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                set.call(e, v); e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true}));
                return e.checkValidity(); }""", shape)
        except Exception:  # noqa: BLE001
            return ""
        if ok:
            return shape
    return ""


_EXPAND_GROUPS_JS = r"""(scope) => {
    // A required question drawn as a bare header until clicked: Personio's "Preferred Work Location*" renders its
    // Amsterdam ... Remote checkboxes only after a click on the header, with no ARIA saying so (application 460).
    // The shape: a row whose text ends in the required "*", holding no control at all, among sibling rows that do.
    const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const CTRL = 'input:not([type=hidden]), select, textarea, button, [role=combobox], [role=listbox], [contenteditable=true]';
    const root = (scope && scope !== 'body' && document.querySelector(scope)) || document.body;
    const marked = [];
    for (const el of root.querySelectorAll('div, li')) {
        if (!vis(el) || el.querySelector(CTRL) || el.hasAttribute('data-jobbot-expanded')) continue;
        const full = (el.innerText || '').trim();
        const t = full.split('\n')[0].trim();          // a validation line can sit under the header once refused
        if (full.length > 220 || t.length < 3 || t.length > 120 || !/\*\s*$/.test(t)) continue;
        if (/resume|\bcv\b|upload|attach|\bfile|document|photo|cover letter/i.test(t)) continue;   // a click there opens a file picker
        const par = el.parentElement;
        if (!par || ![...par.children].some(x => x !== el && x.querySelector(CTRL))) continue;
        el.setAttribute('data-jobbot-expand', String(marked.length));
        marked.push(t.slice(0, 60));
    }
    return marked;
}"""


def expand_hidden_choice_groups(page: Any, scope: str = "body") -> int:
    """Click open every collapsed choice list on the page, so its boxes can be answered. Returns how many."""
    try:
        heads = page.evaluate(_EXPAND_GROUPS_JS, scope) or []
    except Exception as e:  # noqa: BLE001
        log.debug("expand groups: %s", e)
        return 0
    opened = 0
    for i, name in enumerate(heads[:8]):
        try:
            h = page.locator(f"[data-jobbot-expand='{i}']").first
            h.click(timeout=SHORT)
            h.evaluate("e => e.setAttribute('data-jobbot-expanded', '1')")
            page.wait_for_timeout(300)
            opened += 1
            log.info("opened the collapsed choice list %r", name)
        except Exception as e:  # noqa: BLE001
            log.debug("expand %r: %s", name, e)
    return opened


def _repair_one(ctx: ApplyContext, el: Any, f: dict) -> bool:
    """Act on one field the form rejected. True when the control now holds something different.

    The return value is the whole point: it is what tells the caller whether pressing Submit again could
    possibly produce a different result. Never let a NeedsHuman out of here quietly — a field the form
    insists on and jobbot cannot answer is exactly the question the candidate should be asked, with the
    site's own complaint attached to it.
    """
    page = ctx.page
    label, kind, before, msg = f["label"], f["kind"], f["value"], f["message"]

    # A phone box with its own pattern: "Please match the requested format." (Personio, application 460)
    # refused +8801771614053 twice. The same number in each common shape, keeping the one the box accepts.
    if kind == "text" and re.search(r"phone|mobile|tel", label, re.I) and \
            re.search(r"format|pattern|valid", msg, re.I):
        shaped = fit_phone_pattern(el, ctx.fact("identity.phone"), ctx.fact("identity.phone_national"))
        if shaped and shaped != before:
            log.info("%r refused %r; its pattern takes %r", label[:60], before, shaped)
            return True

    # "Enter a maximum of 50 characters." — the answer is right and too long (Nationwide's Oracle form held
    # "N/A - I have not worked at Nationwide or Virgin Money", application 314). Keep its first clause, and
    # failing that cut at a word boundary; a box that wants less than that is asked about below as usual.
    cap = re.search(r"\b(?:maximum|max\.?|at\s+most|no\s+more\s+than|up\s+to)\s+(?:of\s+)?(\d{1,5})\s+characters?\b", msg, re.I)
    if cap and kind in ("text", "textarea") and before:
        limit = int(cap.group(1))
        full = current_value(el) or before
        try:
            # What the rules say now, first: the box may hold an answer from before a rule was fixed (a
            # citizenship paragraph in "type of visa you hold", application 314).
            fresh = clean(ctx.answer(label, None, kind) or "")
        except NeedsHuman:
            fresh = ""
        if fresh and fresh != full:
            full = fresh
        if len(full) > limit or full != (current_value(el) or before):
            short = full if len(full) <= limit else re.split(r"\s+[-–—:;]\s+|[.;]\s", full, maxsplit=1)[0].strip()
            if not short or len(short) > limit:
                # Never cut prose mid-sentence ("I am a citizen of" is not an answer): ask for a short one.
                raise NeedsHuman(f"'{label[:80]}' takes at most {limit} characters. Give a short answer here.",
                                 question=label, kind=kind)
            if short and short != (current_value(el) or before):
                log.info("%r allows %d characters; shortening the answer to %r", label[:60], limit, short)
                fill_if_empty(el, short, clear=True)
                return clean(current_value(el)) == clean(short)

    # Asked through ctx.answer rather than through _ask: _ask lets an unanswerable question go by when the
    # control looks optional, and a control the form has just named in a validation message is not optional
    # whatever its markup says. This is the other half of the C3 AI failure — the field-of-study box read as
    # optional, the question was skipped, and the form then refused the submit over the empty box.
    if kind == "checkbox":
        if before == "checked":
            return False
        # One box of a group is not a question of its own. Greenhouse marks every box of a required group
        # "Please check this box if you want to proceed" while the group is unsatisfied, and asking each box
        # by its own label ("Spain?") ticked Spain for a candidate in Bangladesh (Smartcat, application 356).
        # A group already holding a tick is not what the form is refusing; one holding none is answered as
        # the group, by the walker, not box by box here.
        try:
            siblings = el.evaluate("""e => { const n = e.getAttribute('name');
                const g = n ? [...document.querySelectorAll('input[type=checkbox]')].filter(x => x.name === n) : [];
                return {count: g.length, ticked: g.filter(x => x.checked).length}; }""")
        except Exception:  # noqa: BLE001
            siblings = {"count": 1, "ticked": 0}
        if siblings.get("count", 1) > 1:
            log.info("not ticking %r on its own: it is one box of a %d-box group (%d ticked)",
                     label[:60], siblings["count"], siblings.get("ticked", 0))
            return False
        # A consent gate the form will not go in without ("You need to agree to the terms and conditions").
        ans = ctx.answer(label, ["Yes", "No"], "checkbox")
        if not ans or not re.match(r"^\s*(?:y|true|agree|accept|i )", ans, re.I):
            return False
        return bool(tick(el))

    if kind == "file":
        # An empty upload the form insists on, other than the cover letter's: the CV goes in it. UKG/UltiPro
        # parses the CV from an "Upload Resume" box at the top and then refuses the submit over an empty
        # "Documents" box further down (application 318); upload_resume had already seen the CV's name on
        # the page and left every other input alone.
        if before == "file" or not ctx.cv_path or not _accepts_document(el):
            return False
        words = (label + " " + label_context(el)).lower()
        if "cover" in words and not _CV_CONTEXT_RE.search(words):
            return False
        try:
            el.set_input_files(ctx.cv_path, timeout=MEDIUM)
            page.wait_for_timeout(1500)
        except Exception as e:  # noqa: BLE001
            log.info("repair %r: the upload refused the CV (%s)", label[:50], str(e)[:100])
            return False
        log.info("repair %r: attached %s to the upload the form insists on", label[:50],
                 os.path.basename(str(ctx.cv_path)))
        return True

    if kind in ("radio", "date"):
        return False        # the group container is not knowable from here; the broad refill handles these

    if kind == "select":
        opts = select_options(el)
        ans = ctx.answer(label, opts, kind)
        if not ans or ans == before:
            return False
        return bool(choose_select(el, ans, opts))

    if kind == "combobox":
        opts = combobox_options(page, el)
        ans = ctx.answer(label, opts or None, kind)
        if not ans or ans == before:
            return False
        return bool(choose_combobox(page, el, ans, _answer_alternatives(ctx, label), allow_other=_other_ok(label)))

    # text / textarea / number
    if before and not has_suggestions(el):
        _note_rejected_answer(label, before, msg)

    # A URL the validator judged on its shape. The next shape is a real change; the same one is not.
    if before and (_URL_ERROR_RE.search(label) or _URL_ERROR_RE.search(msg)) and "://" in before:
        for variant in url_variants(before):
            if variant != before:
                _clear_control(el)
                if fill_if_empty(el, variant):
                    log.info("repair %r: the form refused %r, trying %r", label[:50], before[:50], variant[:50])
                    return True
        return False

    ans = ctx.answer(label, None, "number" if kind == "number" else kind)
    if ans == "" and kind in ("text", "textarea"):
        ans = _NOT_APPLICABLE     # a skipped "If yes, …" box the form refused as empty; see _ask
        log.info("repair %r: the form insists on a follow-up that does not apply; writing %r", label[:50], ans)
    if not ans:
        return False
    if has_suggestions(el):
        # The box only takes what its own list offers, which is why typing the true answer at it failed.
        # facts.yaml's declared near-misses are tried before "Other".
        before_commit = current_value(el)
        fill_from_suggestions(page, el, ans, _answer_alternatives(ctx, label))
        return current_value(el) != before_commit
    if ans == before:
        return False        # the same answer again cannot produce a different outcome
    _clear_control(el)
    return bool(fill_if_empty(el, ans))


def repair_fields(ctx: ApplyContext, fields: list[dict]) -> list[str]:
    """Act on each rejected field. Returns a line per control that now holds something different.

    An empty list is the signal to stop: nothing about the form changed, so submitting it again would
    reproduce the rejection exactly, which is how one application came to be refused seven times.
    """
    changed: list[str] = []
    for f in fields or []:
        if f.get("id") is None:
            continue
        sel = f'[{_ERR_STAMP}="{f["id"]}"]'
        try:
            el = ctx.page.locator(sel).first
            if not is_visible_now(el):
                continue
        except Exception as e:  # noqa: BLE001
            log.debug("repair: %s not reachable: %s", sel, e)
            continue
        if not re.search(r"\w", f.get("label") or "") or is_prompt_value(f["label"]) or is_furniture_label(f["label"]):
            # field_errors names a control the quick way, which takes a list's own "Select" for its name;
            # the full reader finds the question written above it (application 175).
            f["label"] = get_label_for(el) or f["label"]
        if f["kind"] in ("combobox", "select") and is_dial_control(el):
            continue        # a phone's country code: the identity refill sets it from the number itself
        log.info("repairing %r (%s, holds %r): %s",
                 f["label"][:60], f["kind"], f["value"][:40], f["message"][:90])
        if f["kind"] == "text" and is_prompt_value(f.get("value") or ""):
            # A "text" box holding a list's own prompt is a dropdown jobbot did not recognise: log its shape.
            try:
                log.info("prompt-holding control: %s", re.sub(r"\s+", " ", el.evaluate(
                    "e => { let n = e; for (let i = 0; i < 3 && n.parentElement; i++) n = n.parentElement;"
                    " return n.outerHTML.replace(/ (style|d)=\"[^\"]*\"/g, '').slice(0, 1800); }")))
            except Exception:  # noqa: BLE001
                pass
        if not re.search(r"\w", f.get("label") or ""):
            _log_unnamed_field(el)
        try:
            if _repair_one(ctx, el, f):
                changed.append(f"{f['label'][:60] or 'a field'} — {f['message'][:80]}")
        except NeedsHuman:
            raise           # the candidate is the fix for this one; ask with the site's complaint attached
        except ApplyError as e:
            log.info("repair of %r did not take (%s)", f["label"][:50], e)
        except Exception as e:  # noqa: BLE001
            log.debug("repair of %r failed: %s", f["label"][:50], e)
    return changed


def _log_unnamed_field(el: Any) -> None:
    """A rejected control nothing names. Logged with its surroundings (shape only, values stripped), because
    the page is usually one that refuses a second browser, and this line is the only way to see it."""
    try:
        shape = el.evaluate("""e => { let n = e; for (let i = 0; i < 4 && n.parentElement; i++) n = n.parentElement;
            return n.outerHTML.replace(/ (class|style)="[^"]*"/g, '').replace(/ value="[^"]*"/g, ' value=…').slice(0, 2500); }""")
        log.warning("repair: unnamed rejected control; its surroundings were %s", re.sub(r"\s+", " ", shape))
    except Exception as e:  # noqa: BLE001
        log.debug("repair: could not describe an unnamed control: %s", e)


def rejection_summary(fields: list[dict], errors: list[str]) -> str:
    """What to tell the candidate when jobbot has run out of ways to get a form past its own validation."""
    named = [f"{f['label'][:60]}: {f['message'][:90]}" for f in (fields or []) if f.get("id") is not None]
    loose = [m[:90] for m in (errors or []) if not any(m[:90] in n for n in named)]
    return "; ".join((named + loose)[:5]) or "; ".join((errors or [])[:5])


# ---------- identity URLs ----------
# A form validator judges a URL on its shape, not on where it points, and they disagree about which shape
# is valid. Gem refuses "https://linkedin.com/in/jawad-amir" and takes "https://www.linkedin.com/in/…";
# others refuse the scheme, or the trailing slash. So a URL fact is written in the shape most validators
# accept, and a form that rejects it is given the next shape rather than the same one again.
_WWW_HOSTS = ("linkedin.com", "facebook.com", "instagram.com")      # canonical with www
_BARE_HOSTS = ("github.com", "gitlab.com", "medium.com", "x.com", "twitter.com")   # canonical without


def canonical_url(value: str) -> str:
    """`value` in the shape most form validators accept: https, and www exactly where the host wants it."""
    raw = clean(value)
    if not raw or " " in raw:
        return raw
    rest = re.sub(r"^[a-z][\w+.-]*://", "", raw, flags=re.I)
    host = rest.split("/")[0].lower()
    bare = host[4:] if host.startswith("www.") else host
    if any(bare == h or bare.endswith("." + h) for h in _WWW_HOSTS):
        rest = "www." + bare + rest[len(host):]
    elif any(bare == h or bare.endswith("." + h) for h in _BARE_HOSTS):
        rest = bare + rest[len(host):]
    return "https://" + rest


def url_variants(value: str) -> list[str]:
    """Every shape of one URL worth trying, canonical first, no duplicates."""
    canon = canonical_url(value)
    if not canon:
        return []
    rest = canon[len("https://"):]
    host = rest.split("/")[0]
    other = rest[4:] if host.startswith("www.") else "www." + rest
    out = [canon, "https://" + other, canon.rstrip("/") + "/", rest, clean(value)]
    seen: list[str] = []
    for v in out:
        if v and v not in seen:
            seen.append(v)
    return seen


# Words too common to tell one field from another in an error message.
_ERROR_STOPWORDS = frozenset({"your", "the", "please", "enter", "valid", "this", "field", "number", "address",
                              "name", "required", "must", "value", "input", "url", "link"})


def error_for_field(errors: list[str], label: str) -> str:
    """The validation message that is about `label`, '' when none of them is.

    Matched on a distinctive word the two share ("LinkedIn URL" against "Please enter a valid LinkedIn
    URL."), because the message is rarely attached to the control in any way a selector can follow.
    """
    words = {w for w in re.split(r"\W+", (label or "").lower())
             if len(w) > 3 and w not in _ERROR_STOPWORDS}
    if not words:
        return ""
    for e in errors or []:
        low = (e or "").lower()
        if any(w in low for w in words):
            return e
    return ""


def get_label_for(el: Any) -> str:
    """Best-effort label text for a form control.

    The ancestor scan goes seven levels because widget libraries bury the input that deep. Zoho Recruit is
    the worst seen: it renders `<label for="">First Name *</label>` beside a control wrapped in four nested
    divs and two custom elements, so the label is real and readable but reachable only by walking up past
    all of them. Stopping earlier fell through to `e.name` and asked the user about "rec-form_842019…".
    The nearest ancestor holding a label still wins, so the extra depth only applies where nothing closer
    has one.

    The ancestor scan takes the nearest label-ish element that has text in it, preferring the one above the
    control. Widget libraries plant empty ones: JobAdder's phone box (application 113, Fusion5) sits beside
    a select2 country dropdown whose `<label for="s2id_autogen2" class="select2-offscreen"></label>` is the
    first label in the wrapper, so the scan stopped on it and returned "" three levels below the real
    `<label>Mobile</label>`. An unnamed control is dropped from the walk entirely, which left a required
    field empty and bounced the form on every submit and every retry. The same form stacks Phone and Mobile
    in one wrapper, which is why the label above the control beats the first one in the markup.

    Last of all, the text that simply sits beside the control. Gem's board (jobs.gem.com) has no <label>
    anywhere: each field is `<span>First name *</span>` next to a div holding the input, with no id, name,
    placeholder or aria attribute on the input at all. Every one of the searches above came back empty, the
    walker took the whole form for nine unlabelled boxes, and filled none of them. So when nothing names the
    control, the nearest short text block that precedes it inside a wrapper holding only this control is
    its label — bounded to a wrapper with a single control so a shared row cannot lend its text to the
    wrong field, and to 200 characters so a paragraph of instructions is not mistaken for a question.
    """
    try:
        txt = clean(el.evaluate(_LABEL_JS))
        if _GENERIC_LABEL_RE.match(txt):
            # "Category" alone says nothing, and its cached answer came from a job-category list on another
            # form ("Data & Analytics" offered to Nationwide's disability category, application 314). The
            # control's own name usually does say: GB-STANDARD-ORA_DISABILITY_CATEGORY-STANDARD.
            hint = _name_hint(el, txt)
            if hint:
                return f"{txt} ({hint})"
            # The box's own prefix, not its question: Taraki draws "PKR [ … ] / month" inside the field
            # group, the scan stopped on "PKR", and both required salary boxes were left blank as optional
            # questions nobody could answer (application 308). The heading above the group is the label.
            above = clean(el.evaluate(_LABEL_ABOVE_UNIT_JS))
            if above:
                return above
        return txt
    except Exception:
        return ""


_GENERIC_LABEL_RE = re.compile(r"^\W*(?:category|type|details?|other|please\s+specify|specify|description|value|"
                               r"select|option|choice|status|level)\W*$", re.I)
_NAME_NOISE = {"gb", "us", "uk", "standard", "ora", "std", "field", "input", "select", "value", "id", "the", "a"}


def _name_hint(el: Any, label: str) -> str:
    """Readable words from a control's name/id that say more than its generic label, '' when none do."""
    try:
        raw = el.evaluate("e => (e.getAttribute('name') || '') + ' ' + (e.id || '')") or ""
    except Exception:  # noqa: BLE001
        return ""
    words: list[str] = []
    for w in re.findall(r"[A-Za-z]{3,}", re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)):
        lw = w.lower()
        if lw not in _NAME_NOISE and lw not in words:
            words.append(lw)
    if not words or (len(words) == 1 and words[0] == clean(label).lower()):
        return ""
    return " ".join(words[:4])


# A currency code or symbol, or a per-period suffix: decoration drawn inside a field group.
_UNIT_LABEL_RE = re.compile(r"^\W*(?:(?-i:[A-Z]{3})|[$€£¥₹৳₨]|/\s*(?:month|year|hour|day|annum)|per\s+(?:month|year|hour|annum)"
                            r"|%|days?|months?|years?)\W*$", re.I)

_LABEL_ABOVE_UNIT_JS = """e => {
    const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
    const code = t => /^\\W*[A-Z]{3}\\W*$/.test(t);
    const unit = t => code(t) || /^\\W*(?:[$€£¥₹৳₨]|\\/\\s*(?:month|year|hour|day|annum)|per\\s+(?:month|year|hour|annum)|%|days?|months?|years?)\\W*$/i.test(t);
    let p = e.parentElement;
    for (let i = 0; i < 7 && p; i++, p = p.parentElement) {
        if (p.querySelectorAll('input:not([type=hidden]), textarea, select').length > 1) break;
        const kids = [...p.children];
        const mine = kids.findIndex(k => k === e || k.contains(e));
        for (let j = mine - 1; j >= 0; j--) {
            const t = clean(kids[j].innerText);
            if (t && t.length <= 200 && /[\\p{L}]/u.test(t) && !unit(t)) return t;
        }
    }
    return '';
}"""


_LABEL_JS = """e => {
    const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
    // A required star on its own is not a label. SmartRecruiters (Deloitte NZ, application 172) gives its
    // privacy-consent box a <label> holding only "*" and puts the sentence in a sibling; stopping on the
    // star turned a consent tick into an "Empty question" pause.
    const real = t => /[\\p{L}\\p{N}]/u.test(t || '');
    // Text as it reads on screen, <slot> content included. A web-component form (SmartRecruiters' spl-*
    // fields, application 172) passes its question into the label through a slot, so the label's own text
    // is the required star alone and the question itself is only reachable through the slot.
    const slotText = n => {
        if (!n) return '';
        if (n.nodeType === 3) return n.textContent;
        if (n.nodeType !== 1 || ['STYLE', 'SCRIPT', 'TEMPLATE'].includes(n.tagName)) return '';
        if (n.tagName === 'SLOT') {
            const a = n.assignedNodes({flatten: true});
            return (a.length ? a : [...n.childNodes]).map(slotText).join(' ');
        }
        return [...(n.shadowRoot ? n.shadowRoot.childNodes : n.childNodes)].map(slotText).join(' ');
    };
    const tx = n => { const a = clean(n.innerText || n.textContent); return real(a) ? a : clean(slotText(n)); };
    // What a control shows while empty ("Select", "Search", "Choose…") names nothing.
    const prompt = t => /^\\s*(?:-+\\s*)?(?:please\\s+)?(?:select|search|choose|pick|type to search|start typing)\\b[\\s\\w]{0,20}?(?:\\.{3}|…)?\\s*(?:-+)?\\s*$/i.test(t || '')
                        && !/\\?/.test(t || '');
    // A dropdown built out of divs (data-jobbot-widget, see WIDGET_JS) is a field like any other: without
    // it here, the text drawn on one ("Course Start Month") read as the question of the picker after it.
    const CTRL = 'input:not([type=hidden]), textarea, select, button, [role=combobox], [role=radiogroup], '
               + '[role=radio], [role=checkbox], [role=listbox], [role=switch], [contenteditable=true], [data-jobbot-widget]';
    const before = start => {
        const own = n => { let x = n; while (x.parentElement && !x.parentElement.contains(start)) x = x.parentElement; return x; };
        const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
        w.currentNode = start;
        for (let n = w.previousNode(), seen = 0; n && seen < 400; n = w.previousNode(), seen++) {
            if (n.nodeType === 1) {
                // A field between here and the text above it: that text is the other field's question.
                if (!n.contains(start) && n.matches(CTRL) && vis(n)) return '';
                continue;
            }
            const host = n.parentElement;
            if (!host || start.contains(host) || !real(n.textContent)) continue;
            if (host.closest('script, style, noscript, [aria-hidden=true]')) continue;
            const branch = own(host);
            if (branch.matches(CTRL) || branch.querySelector(CTRL)) return '';   // the previous field's own text
            // The <label> itself when the text sits in one: Shopee writes the required star in a sibling
            // <i> of the span holding the words, so the span alone reads "First Name" and the field looked
            // optional (application 274).
            const blk = host.closest('label, legend') || host.closest('p, h1, h2, h3, h4, h5, h6, li, dt, div, span') || host;
            if (!vis(blk)) continue;
            const t = tx(blk);
            if (!real(t) || prompt(t)) continue;
            if (t.length > 600) return '';          // a page of instructions, not a question
            return t;
        }
        return '';
    };
    const vis = n => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    // An empty label names nothing, so each of the searches below passes over one and keeps looking.
    for (const l of (e.labels || [])) { const t = tx(l); if (real(t)) return t; }
    const al = e.getAttribute('aria-labelledby');
    const root = e.getRootNode();
    const byId = id => (root.getElementById ? root.getElementById(id) : document.getElementById(id));
    if (al) { const t = al.split(/\\s+/).map(id => { const n = byId(id); return n ? (n.innerText || n.textContent) : ''; }).join(' '); if (real(clean(t))) return clean(t); }
    // A tick box whose words are only its description: EY's signup draws "Receive new job posting
    // notifications" in an aria-describedby span, and the scan below lent it "Email Address:" from the row
    // above, so the opt-in was asked as a contact question (application 288).
    if (e.type === 'checkbox' || e.getAttribute('role') === 'checkbox') {
        const ad = e.getAttribute('aria-describedby');
        if (ad) { const t = ad.split(/\\s+/).map(id => { const n = byId(id); return n ? (n.innerText || n.textContent) : ''; }).join(' '); if (real(clean(t))) return clean(t); }
    }
    // A radio whose aria-label is shared with the other options of its group is labelled with the group's
    // *question*, not with its own option. LinkedIn's native-<dialog> Easy Apply does this: every radio of
    // "Have you built…?" carries that question as aria-label, beside an empty <label> and a <p>Yes</p>, so
    // both options read as the question and "Yes" could never be picked (application 178). The option's own
    // words are the text beside it, found by the tick-box scan further down.
    const aria = e.getAttribute('aria-label');
    const sharedAria = (e.type === 'radio' || e.type === 'checkbox') && e.name && real(aria) && root.querySelectorAll
        && [...root.querySelectorAll(`input[name="${CSS.escape(e.name)}"]`)].some(o => o !== e && o.getAttribute('aria-label') === aria);
    if (real(aria) && !prompt(aria) && !sharedAria) return clean(aria);
    const id = e.id; if (id) { for (const l of root.querySelectorAll(`label[for="${CSS.escape(id)}"]`)) { const t = tx(l); if (real(t)) return t; } }
    const wrap = e.closest('label'); if (wrap) { const t = tx(wrap); if (real(t)) return t; }
    const fs = e.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) { const t = tx(lg); if (real(t)) return t; } }
    // The question written before the control. Many boards put it in a plain <p> above the field and tie it to
    // nothing: Rippling's "Are you legally eligible to work…" sits over a combobox whose only name is its own
    // "Select" prompt, and its essay question over a textarea with no name at all, so the ancestor scan
    // below climbed to the cover-letter drop zone and the model answered "Drop or select (.doc/.pdf)" with a
    // file name (application 175). Read as a person reads a form: the nearest text after the previous field
    // and before this one. Text inside another field's own wrapper is that field's, never this one's, which
    // keeps a floating label (drawn after its input) from naming the field that follows it.
    if (!(e.type === 'checkbox' || e.type === 'radio')) { const q = before(e); if (q) return q; }
    const ph = e.getAttribute('placeholder'); if (real(ph) && !prompt(ph)) return clean(ph);
    // A tick box is named by the sentence beside it, on either side. Looked for inside the wrappers that
    // hold this control alone, before the ancestor scan below can climb to a neighbouring field's label.
    if (e.type === 'checkbox' || e.type === 'radio') {
        let w = e.parentElement;
        for (let i = 0; i < 4 && w; i++, w = w.parentElement) {
            if (w.querySelectorAll('input:not([type=hidden]), textarea, select, [role=combobox]').length > 1) break;
            for (const k of w.children) {
                if (k === e || k.contains(e) || k.querySelector('input, textarea, select, button')) continue;
                const t = tx(k);
                if (real(t) && t.length <= 400 && vis(k)) return t;
            }
        }
    }
    let p = e.parentElement; for (let i = 0; i < 7 && p; i++, p = p.parentElement) {
        // Every candidate at this level, not just the first: an empty one is furniture, and stopping on it
        // hid the real label three levels up. The nearest one ABOVE the control wins, so a wrapper holding
        // two fields lends each its own label rather than the first in the markup (see get_label_for).
        let above = '', below = '';
        for (const l of p.querySelectorAll('label, legend, .label, [class*="label" i], h3, h4, h5')) {
            if (l.contains(e) || !vis(l)) continue;
            // Another field's own words: the header labels inside the month picker before this one
            // ("2020 – 2029") named the picker after it (application 274).
            const owner = l.closest(CTRL);
            if (owner && owner !== e && !owner.contains(e)) continue;
            const t = tx(l);
            if (!real(t)) continue;
            if (l.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING) above = t;
            else if (!below) below = t;
        }
        if (above || below) return above || below;
    }
    // Nothing names it: the text beside it does (see get_label_for).
    p = e.parentElement;
    for (let i = 0; i < 7 && p; i++, p = p.parentElement) {
        if (p.querySelectorAll('input:not([type=hidden]), textarea, select, [role=combobox]').length > 1) break;
        const kids = [...p.children];
        const mine = kids.findIndex(k => k === e || k.contains(e));
        for (let j = mine - 1; j >= 0; j--) {
            const k = kids[j];
            if (k.querySelector('input, textarea, select, button')) continue;
            const t = tx(k);
            if (real(t) && t.length <= 200 && vis(k)) return t;
        }
    }
    return clean(e.name || '');
}"""


def same_value(a: str, b: str) -> str:
    """True when two field values are the same answer — case, spacing and a trailing slash aside."""
    norm = lambda s: clean(s).rstrip("/").lower()   # noqa: E731
    return norm(a) == norm(b)


# What a widget prints on itself: an upload zone's "Drop or select (.doc / .docx / .pdf)", a search box's
# "Search", a résumé picker's "Select resume Forward-Deployed-AI-Engineer.pdf". Taken for questions, each was
# answered and cached — the model duly put a file name into an essay box (application 175, Nutrient).
FURNITURE_LABEL_RE = re.compile(
    r"^\s*(?:drop|drag|choose|select|browse|upload|attach)\b.{0,60}\b(?:files?|here|resume|résumé|cv|pdf|docx?)\b"
    r"|^\s*(?:search|select|choose)\s*(?:\.{3}|…)?\s*$", re.I)
FILENAME_RE = re.compile(r"^[^\n/\\]{1,150}\.(?:pdf|docx?|rtf|txt|odt|pages)$", re.I)


def is_furniture_label(label: str) -> bool:
    """True for words a control prints on itself rather than a question it asks."""
    return bool(FURNITURE_LABEL_RE.search(clean(label)))


def looks_like_filename(value: str) -> bool:
    return bool(FILENAME_RE.match(clean(value)))


def strip_required(label: str) -> str:
    return clean(re.sub(r"(\*|\(required\)|\(optional\)|required|optional)\s*$", "", label, flags=re.I))


IDENTITY_WORDS = ("first name", "last name", "full name", "email", "phone", "resume", "cv", "cover letter",
                  "linkedin", "github", "portfolio", "website", "name")


def is_identity_label(label: str) -> bool:
    l = label.lower()
    return any(w == l or l.startswith(w) or l.endswith(w) for w in IDENTITY_WORDS)


def select_options(el: Any) -> list[str]:
    try:
        return [clean(o) for o in el.evaluate(
            "e => Array.from(e.options).filter(o => o.value !== '' && !o.disabled).map(o => o.textContent)") if clean(o)]
    except Exception:
        return []


def same_option(a: str, b: str) -> bool:
    """Whether two option labels mean the same choice.

    The resolver maps an answer onto the options with punctuation and case stripped, and the code that then
    has to click the thing compared the strings outright — so an answer of "Mr" against an option printed
    "Mr." matched upstream and missed here, and the application failed on "Could not choose 'Mr' for
    'Prefix'". One comparison, used by everything that picks an option.
    """
    from jobbot.answers import normalize_question
    return normalize_question(a) == normalize_question(b) and bool(normalize_question(a))


def is_materialize_select(el: Any) -> bool:
    """A native <select> Materialize CSS hides behind its own read-only "select-dropdown" text box."""
    try:
        return bool(el.evaluate("e => e.tagName === 'SELECT' && !!e.closest('.select-wrapper')"))
    except Exception:  # noqa: BLE001
        return False


def is_materialize_face(el: Any) -> bool:
    """Materialize's read-only text box that only displays its hidden <select>'s choice."""
    try:
        return bool(el.evaluate("e => e.tagName === 'INPUT' && e.classList.contains('select-dropdown')"
                                " && !!e.closest('.select-wrapper')"))
    except Exception:  # noqa: BLE001
        return False


_BACKING_SELECT_JS = r"""(e) => {
    // A dropdown drawn over a hidden native <select>: select2, chosen, bootstrap-select, nice-select, tom-select.
    // The <select> is the real field the form submits; the facade is only how it is shown.
    const hidden = s => s.tagName === 'SELECT' && (s.classList.contains('select2-hidden-accessible')
        || s.getAttribute('aria-hidden') === 'true' || getComputedStyle(s).display === 'none'
        || s.offsetWidth < 3 || s.offsetHeight < 3);
    const ids = (e.getAttribute('aria-controls') || '') + ' ' + (e.getAttribute('aria-owns') || '') + ' '
              + ((e.querySelector('[id^=select2-][id$=-container]') || {}).id || '') + ' ' + (e.id || '');
    const m = ids.match(/select2-(.+?)-(?:container|results)/);
    if (m) { const s = document.getElementById(m[1]); if (s && s.tagName === 'SELECT') { s.setAttribute('data-jobbot-native', '1'); return true; } }
    const facade = e.closest('.select2-container, .chosen-container, .bootstrap-select, .nice-select, .ts-wrapper, .selectize-control') || e;
    for (let n = facade, i = 0; n && i < 3; n = n.parentElement, i++) {
        const sib = [n.previousElementSibling, n.nextElementSibling];
        for (const c of sib) if (c && hidden(c)) { c.setAttribute('data-jobbot-native', '1'); return true; }
        const inside = [...(n.parentElement ? n.parentElement.querySelectorAll(':scope > select') : [])].filter(hidden);
        if (inside.length === 1) { inside[0].setAttribute('data-jobbot-native', '1'); return true; }
    }
    return false;
}"""


def backing_select(el: Any) -> Any:
    """The hidden native <select> a drawn dropdown stands in for, or None. Amazon's select2 "How did you hear
    about this role?" (application 414) would not open for the widget walker; its <select> takes the answer
    directly, with the full list readable without opening anything."""
    try:
        el.page.evaluate("() => document.querySelectorAll('[data-jobbot-native]').forEach(s => s.removeAttribute('data-jobbot-native'))")
        if not el.evaluate(_BACKING_SELECT_JS):
            return None
        nat = el.page.locator("select[data-jobbot-native]").first
        return nat if nat.count() and len(select_options(nat)) >= 2 else None
    except Exception as e:  # noqa: BLE001
        log.debug("backing_select: %s", e)
        return None


def set_hidden_select(el: Any, label: str) -> bool:
    """Choose `label` in a hidden native <select> the page draws its own way (Materialize: Coveo's French form,
    application 378), firing the events its script listens for and updating the box it displays."""
    try:
        return bool(el.evaluate("""(e, want) => {
            const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const o = [...e.options].find(x => norm(x.textContent) === norm(want)) ||
                      [...e.options].find(x => norm(x.textContent).startsWith(norm(want)));
            if (!o) return false;
            e.value = o.value; o.selected = true;
            e.dispatchEvent(new Event('input', {bubbles: true}));
            e.dispatchEvent(new Event('change', {bubbles: true}));
            // select2 / chosen redraw on their own jQuery events
            if (window.jQuery) { try { jQuery(e).trigger('change').trigger('chosen:updated'); } catch (err) {} }
            const face = e.closest('.select-wrapper') && e.closest('.select-wrapper').querySelector('input.select-dropdown');
            if (face) face.value = o.textContent.trim();
            return e.value === o.value; }""", label))
    except Exception as e:  # noqa: BLE001
        log.debug("set_hidden_select(%r): %s", label, e)
        return False


def choose_select(el: Any, answer: str, options: list[str], *, force: bool = False) -> bool:
    """select_option by visible label, falling back to a case- and punctuation-insensitive match.

    An already-chosen select is left alone, because on every other field that choice is somebody's answer.
    `force` is for the one control that is not: a phone number's country-code picker, which the form itself
    pre-answers on our behalf and gets wrong — Oracle's UAE tenant rests it on +971 whoever is applying. On
    that one control "already chosen" means "not yet corrected", and without `force` the correction would
    report success having done nothing at all (application 135).
    """
    try:
        cur = clean(el.evaluate("e => e.selectedIndex > 0 ? e.options[e.selectedIndex].textContent : ''"))
        if cur and not force:
            return True
        for o in options:
            if o.lower() == answer.lower() or same_option(o, answer):
                el.select_option(label=o, timeout=MEDIUM)
                return True
        el.select_option(label=answer, timeout=MEDIUM)
        return True
    except Exception as e:
        log.debug("choose_select failed: %s", e)
        return False


# ---------- controls the page draws itself ----------
# The accessible pattern behind most custom tick boxes: a real <input type=checkbox> is kept in the markup
# for screen readers and the keyboard, sized to nothing (0x0, opacity:0, clip:rect(0,0,0,0)), and a styled
# <span> inside its <label> is what a person sees and clicks. Playwright calls such an input invisible --
# correctly -- and the field walker skipped it, which is how Oracle Recruiting's "I agree with the terms and
# conditions" went unticked on application 127 (Westpac): the step bounced with "You need to agree to the
# terms and conditions.", the one refill did exactly the same thing, and every Retry reproduced it.
#
# The discriminator against a honeypot is the label. A trap is aria-hidden, keyboard-skipped or named for
# what it is; a control a person is meant to tick has visible label text standing in for it.
_DRAWN_BY_LABEL_JS = """e => {
    const r = e.getBoundingClientRect();
    if (r.width > 2 && r.height > 2) return '';                  // on screen in its own right
    if ((e.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return '';
    if (e.getAttribute('tabindex') === '-1') return '';          // deliberately unreachable: a trap
    let lbl = null;
    for (const l of (e.labels || [])) {
        const b = l.getBoundingClientRect(), s = getComputedStyle(l);
        if (b.width > 3 && b.height > 3 && s.visibility !== 'hidden' && s.display !== 'none'
                && s.opacity !== '0') { lbl = l; break; }
    }
    if (!lbl) {
        // No <label>: the control may be drawn by an ARIA widget wrapped round it instead. Rippling draws
        // each radio as <div role="radio"> over a zero-sized native input, so every Yes/No on its form was
        // skipped as invisible (application 175).
        const w = e.parentElement && e.parentElement.closest('[role=radio], [role=checkbox], [role=switch]');
        if (w) { const b = w.getBoundingClientRect(); if (b.width > 3 && b.height > 3) lbl = w; }
    }
    if (!lbl) return '';
    // An empty but visible <label> is still what draws the control: LinkedIn's native-<dialog> Easy Apply
    // styles <label for=radio></label> as the circle and prints "Yes" in a sibling <p>. Returning its
    // (empty) text made every such radio read as not drawn at all, and each Yes/No on the step was
    // skipped as invisible (application 178). Callers only ask whether one exists.
    return (lbl.innerText || lbl.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200) || '(drawn)';
}"""


def drawn_by_label(el: Any) -> str:
    """Text of the visible <label> standing in for a control the page has sized to nothing, '' if none."""
    try:
        return clean(el.evaluate(_DRAWN_BY_LABEL_JS))
    except Exception:  # noqa: BLE001 - a control we cannot measure is not one to go clicking labels for
        return ""


# Where on a <label> it is safe to click. A consent label almost always carries a link -- "I agree with the
# [terms and conditions]", "I have read the [privacy policy]" -- and Playwright clicks an element's CENTRE.
# On a short label the link sits right under that centre, so the click opens the policy instead of ticking
# the box (see _label_point).
_LABEL_POINT_JS = r"""l => {
    const lb = l.getBoundingClientRect();
    if (lb.width < 4 || lb.height < 4) return null;
    const blockers = [...l.querySelectorAll('a[href], button, [role=link], [role=button], input, select, textarea')]
        .map(n => n.getBoundingClientRect())
        .filter(b => b.width > 0 && b.height > 0);
    if (!blockers.length) return null;                       // nothing in the way: centre is fine
    const y = lb.top + lb.height / 2;
    const clear = x => !blockers.some(b =>
        x >= b.left - 2 && x <= b.right + 2 && y >= b.top - 2 && y <= b.bottom + 2);
    for (let x = lb.left + 3; x < lb.right - 2; x += 4)
        if (clear(x)) return {x: x - lb.left, y: y - lb.top};
    return null;                                             // a link covers the whole label
}"""


def _label_point(label: Any) -> dict | None:
    """Offset within `label` that is clear of any link or button inside it, None when the centre will do.

    The bug this exists for (applications 127 and 128, Westpac on Oracle Recruiting): the consent label
    read "I agree with the terms and conditions", 271px wide, with the "terms and conditions" link running
    from x=553 to x=682. Playwright clicks the centre -- x=561 -- which is inside the link. So every pass
    opened Oracle's Terms dialog, left the box unticked, and the form came back "You need to agree to the
    terms and conditions." Worse, the dialog it opened is modal and does not dismiss cleanly, so the Next
    press after it had nothing to land on. Two applications died on it, three times over.

    Returning None means the label has nothing clickable inside it and the ordinary centre click is right.
    """
    try:
        return label.evaluate(_LABEL_POINT_JS)
    except Exception as e:  # noqa: BLE001 - a label we cannot measure is clicked the ordinary way
        log.debug("label point: %s", e)
        return None


def is_checked_now(el: Any) -> bool:
    try:
        return bool(el.is_checked())
    except Exception:  # noqa: BLE001
        return False


def tick(el: Any) -> bool:
    """Tick a checkbox or radio however the page has drawn it. True when it ended up ticked.

    Four ways, the cheapest and most faithful first: it may be ticked already; Playwright's own check() when
    the input is on screen; a real click on its <label>, which is what a person clicks when the input itself
    is invisible; and last a scripted click. The label click earns its place beyond Oracle -- a styled box
    whose <span> sits over the input swallows a click aimed at the input, and check() then fails
    actionability against an element nothing can reach.
    """
    if is_checked_now(el):
        return True
    if is_visible_now(el):
        try:
            el.check(timeout=MEDIUM)
        except Exception as e:  # noqa: BLE001 - covered by its own label, or moved as we reached for it
            log.debug("tick: check() refused (%s)", str(e)[:100])
        if is_checked_now(el):
            return True
    try:
        label = el.locator("xpath=ancestor::label[1]")
        if label.count() and is_visible_now(label.first):
            # Aimed, not centred: a consent label's centre is usually inside the policy link it carries.
            point = _label_point(label.first)
            if point:
                log.debug("tick: clicking the label at +%.0f,%.0f to miss the link inside it",
                          point["x"], point["y"])
                label.first.click(timeout=MEDIUM, position=point)
            else:
                label.first.click(timeout=MEDIUM)
    except Exception as e:  # noqa: BLE001
        log.debug("tick: label click refused (%s)", str(e)[:100])
    if is_checked_now(el):
        return True
    try:
        # An ARIA widget drawn round a hidden input (Rippling's <div role="radio">) is what takes the click;
        # a scripted click on the input underneath does not reach the framework's own state.
        widget = el.locator("xpath=ancestor::*[@role='radio' or @role='checkbox' or @role='switch'][1]")
        if widget.count() and is_visible_now(widget.first):
            widget.first.click(timeout=MEDIUM)
            if is_checked_now(el) or (widget.first.get_attribute("aria-checked") or "") == "true":
                return True
    except Exception as e:  # noqa: BLE001
        log.debug("tick: widget click refused (%s)", str(e)[:100])
    try:
        # Last resort, and the only one that reaches a label rendered outside the input's ancestry:
        # activating the <label> in script toggles the control it names exactly as a click would.
        el.evaluate("e => { const l = (e.labels && e.labels[0]) || e.closest('label');"
                    "        if (l) l.click();"
                    "        if (!e.checked) e.click(); }")
    except Exception as e:  # noqa: BLE001
        log.debug("tick: scripted click refused (%s)", str(e)[:100])
    return is_checked_now(el)


def reassert_choice(container: Any) -> bool:
    """Re-select the ticked radio in `container` with real clicks, so a form whose state never heard about
    the tick hears about it now: a different option first, then the chosen one. True when the chosen one
    ends up ticked again."""
    try:
        radios = container.locator("input[type=radio]")
        n = radios.count()
        chosen = next((i for i in range(n) if radios.nth(i).is_checked()), None)
        if chosen is None or n < 2:
            return False
        other = 0 if chosen != 0 else 1

        def click(i: int) -> None:
            el = radios.nth(i)
            label = el.locator("xpath=ancestor::label[1]")
            if not label.count():
                el_id = el.get_attribute("id")
                label = container.locator(f"label[for='{el_id}']") if el_id else label
            if not label.count():
                label = el.locator("xpath=ancestor::*[.//label][1]//label")
            (label.first if label.count() else el).click(timeout=MEDIUM)

        click(other)
        container.page.wait_for_timeout(150) if hasattr(container, "page") else None
        click(chosen)
        ok = radios.nth(chosen).is_checked()
        log.info("re-selected the ticked option so the form registers it (%s)", "held" if ok else "did not hold")
        return ok
    except Exception as e:  # noqa: BLE001
        log.debug("reassert_choice: %s", e)
        return False


def check_choice(container: Any, answer: str) -> bool:
    """See _check_one; a multi-select answer ("a | b | c", resolver.MULTI_SEP) ticks each of its parts."""
    from jobbot.apply.resolver import MULTI_SEP
    parts = [p for p in (answer or "").split(MULTI_SEP) if p.strip()] if MULTI_SEP in (answer or "") else [answer]
    if len(parts) == 1:
        return _check_one(container, parts[0])
    results = [_check_one(container, p) for p in parts]
    return any(results)


def _check_one(container: Any, answer: str) -> bool:
    """Tick the radio/checkbox inside `container` whose label equals `answer` (case-insensitive).

    Returns what `tick` returned, not merely whether a matching control was found. Reporting success on a
    tick that did not land is how a consent box goes out unticked with nothing in the log to say so: the
    caller raises "Could not tick ..." precisely so the run stops instead of submitting a form the board
    will reject, and swallowing the result disarmed it.
    """
    try:
        inputs = container.locator("input[type=radio], input[type=checkbox]")
        n = inputs.count()
        for i in range(n):
            inp = inputs.nth(i)
            label = get_label_for(inp)
            value = clean(inp.get_attribute("value") or "")
            if (label.lower() == answer.lower() or value.lower() == answer.lower()
                    or same_option(label, answer) or same_option(value, answer)):
                return tick(inp)
    except Exception as e:
        log.debug("check_choice failed: %s", e)
    return False


def choice_options(container: Any) -> list[str]:
    opts = []
    try:
        inputs = container.locator("input[type=radio], input[type=checkbox]")
        for i in range(inputs.count()):
            l = get_label_for(inputs.nth(i)) or clean(inputs.nth(i).get_attribute("value") or "")
            if l:
                opts.append(l)
    except Exception:
        pass
    return opts


def checked_choice_label(container: Any) -> str:
    """Label of the ticked radio/checkbox in `container`, '' if none. Used to report an existing answer."""
    try:
        inputs = container.locator("input[type=radio]:checked, input[type=checkbox]:checked")
        if inputs.count():
            return get_label_for(inputs.first) or clean(inputs.first.get_attribute("value") or "")
    except Exception:
        pass
    return ""


def choice_checked(container: Any) -> bool:
    try:
        return container.locator("input[type=radio]:checked, input[type=checkbox]:checked").count() > 0
    except Exception:
        return False


# Where a combobox draws the choice that was made, and where it draws the prompt when none was. Keyed on
# the vendor's own class names: react-select writes a single-value/multi-value node, Ant Design (Dayforce,
# and every board built on antd) writes a selection-item. Shared by combobox_value and
# combobox_is_placeholder so the two can never disagree about what a widget is showing.
_COMBO_JS = r"""
    const COMBO_VALUE_SEL = '[class*="single-value"], [class*="singleValue"], [class*="multi-value"],'
        + ' [class*="multiValue"], [class*="selection-item"], [class*="selectionItem"]';
    const COMBO_PLACEHOLDER_SEL = '[class*="placeholder"], [class*="Placeholder"]';
    const COMBO_CONTROL_SEL = 'input, select, textarea, [role="combobox"], [role="listbox"], [role="spinbutton"]';
    // How far out this widget reaches, found by where the *next* control begins rather than by naming the
    // wrapper's class. Naming it went wrong in both directions: the nearest ancestor whose class contains
    // "select" is the input's own wrapper, where the value never is, and `[class*="control"]` — which is
    // tried first — lands on Ant's `ant-form-item-control-input-content`, four levels out. An ancestor
    // holding a second control is a layout wrapper rather than this widget, and reading a value out of one
    // would report the neighbouring field's answer: two react-selects side by side in a <form> would make
    // the untouched one, showing its placeholder, read back as whatever its neighbour had been set to.
    const comboScope = e => {
        let scope = e.parentElement || e;
        for (let n = scope, hops = 0; n && n !== document.body && hops < 8; n = n.parentElement, hops++) {
            let others = 0;
            for (const ctl of n.querySelectorAll(COMBO_CONTROL_SEL)) if (ctl !== e) others++;
            if (others) break;
            scope = n;
        }
        return scope;
    };
    const comboFind = (e, sel) => {
        const scope = comboScope(e);
        const hit = scope && scope.querySelector(sel);
        return hit && (hit.textContent || '').trim() ? hit : null;
    };
"""


_OPTION_TEXT_JS = """e => {
    const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
    const flat = n => {
        if (!n) return '';
        if (n.nodeType === 3) return n.textContent;
        if (n.nodeType !== 1 || ['STYLE', 'SCRIPT', 'TEMPLATE'].includes(n.tagName)) return '';
        if (n.tagName === 'SLOT') {
            const a = n.assignedNodes({flatten: true});
            return (a.length ? a : [...n.childNodes]).map(flat).join(' ');
        }
        return [...(n.shadowRoot ? n.shadowRoot.childNodes : n.childNodes)].map(flat).join(' ');
    };
    // The same words drawn twice (once in the shadow tree, once through a slot, as SmartRecruiters' options
    // are) read back as "A A". One copy is what the option says.
    const once = t => { const n = t.length, m = (n - 1) / 2;
        return (n % 2 === 1 && t[m] === ' ' && t.slice(0, m) === t.slice(m + 1)) ? t.slice(0, m) : t; };
    let t = clean(e.innerText) || clean(flat(e));
    for (let h = e.getRootNode().host; !t && h; h = h.getRootNode().host) t = clean(h.innerText) || clean(flat(h));
    return once(t || clean(e.getAttribute('aria-label') || e.getAttribute('label') || e.getAttribute('value') || ''));
}"""


def option_text(item: Any) -> str:
    """What a list option reads as on screen. Not inner_text() alone: SmartRecruiters draws each option as a
    web component whose words arrive through a <slot>, so every option read as '' and no answer could ever
    be matched to one (application 172, "Could not choose '$110,001 - $120,000 / year'", seen five times)."""
    try:
        return clean(item.evaluate(_OPTION_TEXT_JS))
    except Exception:  # noqa: BLE001
        try:
            return clean(item.inner_text())
        except Exception:  # noqa: BLE001
            return ""


def file_listed(page: Any, path: Any) -> bool:
    """True when the page already shows a file of this name as uploaded."""
    name = pathlib.Path(str(path or "")).name
    if not name:
        return False
    try:
        # Shadow roots included: a web-component board draws its list of uploads inside one.
        return bool(page.evaluate("n => {" + DEEP_JS + """
            return _deepRoots.some(r => ((r === document ? (document.body && document.body.innerText)
                                                          : r.textContent) || '').includes(n)); }""", name))
    except Exception:  # noqa: BLE001
        return False


def _open_combobox(combo: Any) -> None:
    """Click a combobox's input to open its list. When the widget draws its chosen value (or its prompt) in a
    layer over the input, Playwright waits on that layer for ever; a forced click lands on the layer, which
    is what a person clicks and what opens the list. Sea's career site (Coral select) covers every input
    this way: each open timed out, its 160 countries read as no options at all, and a 'Current Location'
    already set to "United Arab Emirates" in the window was asked about again with an empty text box
    (application 300)."""
    try:
        combo.click(timeout=SHORT)
    except Exception as e:  # noqa: BLE001
        log.debug("combobox click fell back to a forced click: %s", str(e)[:80])
        combo.click(timeout=SHORT, force=True)


def combobox_options(page: Any, combo: Any, limit: int = 60) -> list[str]:
    """Open a react-select / aria combobox and read its option texts, then close it.

    `limit` is a runaway guard, not a statement about how many options a control has. The default is sized
    for the yes/no and seniority lists that make up almost every dropdown on an application form, and a
    country list runs to ~240 — so a caller that must see every option has to raise it. Nothing does yet:
    the dial-code picker, the one control known to need the whole list, reads its rows through _dial_rows
    so it can click one without closing the list first.
    """
    if is_widget(combo):
        return widget_options(page, combo)
    opts: list[str] = []
    try:
        _open_combobox(combo)
        page.wait_for_timeout(400)
        listbox = page.locator("[role=listbox]:visible, [role=option]:visible")
        items = page.locator("[role=option]:visible")
        if not items.count():
            # Some lists draw their rows only on a key: Oracle's disability "Category" showed nothing on a
            # click, so its options were never known and the answer could not be matched (application 314).
            try:
                combo.focus(timeout=SHORT)
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(700)
            except Exception:  # noqa: BLE001
                pass
            items = page.locator("[role=option]:visible")
        for i in range(min(items.count(), limit)):
            t = option_text(items.nth(i))
            if t:
                opts.append(t)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        _ = listbox
    except Exception:
        pass
    return opts


_SCROLL_OPTIONS_JS = """async limit => {
    const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
    const shown = () => Array.from(document.querySelectorAll('[role=option]'))
        .filter(o => o.getClientRects().length && getComputedStyle(o).visibility !== 'hidden');
    const first = shown()[0];
    let box = first && first.parentElement;
    while (box && box !== document.body && !(box.scrollHeight > box.clientHeight + 4
           && /auto|scroll/.test(getComputedStyle(box).overflowY))) box = box.parentElement;
    const out = [];
    const take = () => { for (const o of shown()) { const t = clean(o.innerText || o.textContent);
                                                    if (t && !out.includes(t)) out.push(t); } };
    take();
    if (!box || box === document.body) return out;
    for (let i = 0; i < 80 && out.length < limit; i++) {
        const before = out.length, top = box.scrollTop;
        box.scrollTop = top + Math.max(box.clientHeight - 40, 40);
        await new Promise(r => setTimeout(r, 120));
        take();
        if (box.scrollTop === top && out.length === before) break;
    }
    box.scrollTop = 0;
    return out.slice(0, limit);
}"""


def combobox_all_options(page: Any, combo: Any, limit: int = 300) -> list[str]:
    """Every option of a combobox, for a question to the user — not for matching an answer.

    A virtualised list draws only the rows in view: Sea's 160 countries read as the first 30, without the
    "United Arab Emirates" the user needed to pick (application 300). The list is scrolled to the end and
    every row seen on the way is kept.
    """
    if is_widget(combo):
        return widget_options(page, combo)
    try:
        _open_combobox(combo)
        page.wait_for_timeout(400)
        opts = [clean(t) for t in (page.evaluate(_SCROLL_OPTIONS_JS, limit) or []) if clean(t)]
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        return opts or combobox_options(page, combo, limit=limit)
    except Exception as e:  # noqa: BLE001
        log.debug("combobox_all_options: %s", e)
        return combobox_options(page, combo, limit=limit)


def _grid_popup(combo: Any) -> str:
    """The id of the grid a combobox opens, '' when it opens a listbox. Oracle Recruiting Cloud's cx-select
    ("Country", WSP, application 433) says aria-haspopup="grid" and draws rows, not role=option items, so every
    option reader came back empty and "Bangladesh" was "not an option"."""
    try:
        if (combo.get_attribute("aria-haspopup") or "").lower() != "grid":
            return ""
        return combo.get_attribute("aria-controls") or ""
    except Exception:  # noqa: BLE001
        return ""


def _choose_from_grid(page: Any, combo: Any, answer: str, alternatives, seen: list[str]) -> bool:
    grid = _grid_popup(combo)
    rows_sel = (f"[id='{grid}'] [role=row], [id='{grid}'] [role=gridcell], [id='{grid}'] [role=option], "
                f"[id='{grid}'] li")
    for term in dict.fromkeys([answer, *alternatives]):
        if not term:
            continue
        try:
            combo.click(timeout=SHORT)
            combo.fill("", timeout=SHORT)
            combo.press_sequentially(str(term)[:40], delay=40, timeout=MEDIUM)
            page.wait_for_timeout(1200)
            rows = page.locator(rows_sel)
            texts = [clean(rows.nth(i).inner_text()) for i in range(min(rows.count(), 40))]
            seen.extend(t for t in texts if t and t not in seen)
            hit = next((i for i, t in enumerate(texts) if t and same_option(t, str(term))), None)
            if hit is None:
                starts = [i for i, t in enumerate(texts) if t.lower().startswith(str(term).lower())]
                hit = starts[0] if len(starts) == 1 else None
            if hit is None:
                continue
            rows.nth(hit).click(timeout=MEDIUM)
            page.wait_for_timeout(400)
            got = clean(current_value(combo))
            log.info("grid combobox: chose %r (reads back %r)", texts[hit], got)
            return bool(got)
        except Exception as e:  # noqa: BLE001
            log.debug("grid combobox %r: %s", term, e)
    return False


def choose_combobox(page: Any, combo: Any, answer: str, alternatives: tuple[str, ...] | list[str] = (),
                    *, allow_other: bool = False, seen: list[str] | None = None) -> bool:
    """Click the combobox, type the answer, pick the matching option.

    Exact match first; else the only option left after typing; else the first option that starts with
    the answer. A blind Enter used to take whatever react-select had highlighted, which on a list that did
    not filter ("Yes" typed into a list of countries) was the wrong answer written with no error. Returns
    True only when the control reads back a value afterwards.

    When the whole answer finds nothing, shorter searches and the `alternatives` facts.yaml allows are tried
    the way fill_from_suggestions does for a datalist box. Greenhouse's newer boards draw Discipline as a
    react-select that fetches its options per search, and "Computer Science & Engineering" returns an empty
    list there while "Computer Science" returns the option to take (application 182, Veeam). `allow_other`
    lets a list's own "Other" stand in for an answer it lacks — education lists only, where that is what the
    board expects; "Other" is never an acceptable stand-in for a pronoun or a visa answer. Every option text
    seen along the way is added to `seen`, so a caller that has to ask can offer the list's real choices.
    """
    if seen is None:
        seen = []
    if is_widget(combo):
        return choose_widget(page, combo, answer, alternatives, allow_other=allow_other, seen=seen)
    if _grid_popup(combo):
        return _choose_from_grid(page, combo, answer, alternatives, seen)
    try:
        _open_combobox(combo)
        page.wait_for_timeout(200)
        try:
            combo.fill("", timeout=SHORT)
        except Exception:
            pass
        page.keyboard.type(answer, delay=20)
        page.wait_for_timeout(600)
        wait_list_loaded(page)
        items = page.locator("[role=option]:visible")
        texts = [option_text(items.nth(i)) for i in range(min(items.count(), 60))]
        _note_seen(seen, texts)
        want = clean(answer).lower()
        pick = next((i for i, t in enumerate(texts) if t.lower() == want or same_option(t, answer)), None)
        if pick is None and len(texts) == 1 and want and want in texts[0].lower():
            pick = 0
        if pick is None:
            pick = next((i for i, t in enumerate(texts) if want and t.lower().startswith(want)), None)
        chosen = texts[pick] if pick is not None else answer
        if pick is not None:
            items.nth(pick).click(timeout=SHORT)
        else:
            page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        # A chip counts too, and has to be checked before the retry below: on a multi-select that second
        # click toggles the choice straight back off (application 314).
        held = combobox_value(combo)
        if pick is None and held and clean(held).lower() == clean(answer).lower():
            # Only our own typing, still sitting in the search box: nothing was picked. Oracle's lists kept
            # "Asian or Asian British - Bangladeshi" as typed text and the field stayed empty (application 314).
            held = ""
        if held or _chip_shows(combo, chosen):
            return True
        # Some widgets swallow the typed text and only take a click on the opened list
        _open_combobox(combo)
        page.wait_for_timeout(300)
        items = page.locator("[role=option]:visible")
        for i in range(min(items.count(), 60)):
            text = option_text(items.nth(i))
            _note_seen(seen, [text])
            if text.lower() == want or same_option(text, answer):
                items.nth(i).click(timeout=SHORT)
                page.wait_for_timeout(300)
                break
        else:
            page.keyboard.press("Escape")
        held = combobox_value(combo)
        if held and not (clean(held).lower() == clean(answer).lower() and not _option_clicked(seen, answer)):
            return True
        if _combobox_search(page, combo, answer, alternatives, allow_other, seen):
            return True
        _log_combo_miss(combo, answer, texts or seen)
        return False
    except Exception as e:
        log.debug("choose_combobox failed: %s", e)
        return False


def _option_clicked(seen: list[str], answer: str) -> bool:
    """Whether the answer was ever on the list as an option (so a box showing it may really hold it)."""
    want = clean(answer).lower()
    return any(clean(t).lower() == want for t in seen)


def _note_seen(seen: list[str], texts: list[str]) -> None:
    for t in texts:
        t = clean(t)
        if t and t not in seen and not is_prompt_value(t) and not _LIST_LOADING_RE.match(t) and len(seen) < 200:
            seen.append(t)


def _combobox_typed_options(page: Any, combo: Any, query: str) -> list[str]:
    """Clear the box, type `query`, and read what the list offers for it (fetched lists refresh per key)."""
    _open_combobox(combo)
    page.wait_for_timeout(150)
    try:
        combo.fill("", timeout=SHORT)
    except Exception:  # noqa: BLE001
        pass
    page.keyboard.type(query, delay=20)
    page.wait_for_timeout(900)
    wait_list_loaded(page)
    items = page.locator("[role=option]:visible")
    return [option_text(items.nth(i)) for i in range(min(items.count(), 60))]


def _click_combobox_option(page: Any, combo: Any, pick: str) -> bool:
    items = page.locator("[role=option]:visible")
    for i in range(min(items.count(), 60)):
        if option_text(items.nth(i)) == pick:
            items.nth(i).click(timeout=SHORT)
            page.wait_for_timeout(300)
            return bool(combobox_value(combo)) or _chip_shows(combo, pick)
    return False


def _chip_shows(combo: Any, pick: str) -> bool:
    """A multi-select keeps its input empty and draws the choice as a chip beside it: Nationwide's
    "Preferred Location" took "Head Office - Swindon" and was reported as not chosen (application 314)."""
    try:
        return bool(combo.evaluate("""(e, pick) => {
            let p = e.parentElement;
            for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
                const chips = p.querySelectorAll('[class*=chip i], [class*=tag i], [class*=multi-value i], [class*=selected i], [class*=token i], li');
                for (const c of chips) {
                    // An option in the open list is not a choice: Oracle's rows are <li>s holding the very
                    // text, and counting them reported three empty dropdowns as chosen (application 314).
                    if (c.contains(e) || c.getAttribute('role') === 'option' || c.closest('[role=listbox], [role=option]')) continue;
                    const r = c.getBoundingClientRect(); if (!(r.width > 0 && r.height > 0)) continue;
                    if ((c.innerText || '').trim().startsWith(pick)) return true;
                }
            }
            return false; }""", pick))
    except Exception:  # noqa: BLE001
        return False


def _combobox_search(page: Any, combo: Any, answer: str, alternatives: tuple[str, ...] | list[str],
                     allow_other: bool, seen: list[str]) -> bool:
    """The recovery fill_from_suggestions gives a datalist box, for a react-select that found nothing.

    Prefixes of the answer, then the facts.yaml alternatives (see _suggestion_queries for why only prefixes),
    each matched with _best_suggestion against the answer and then each alternative; last, the list's own
    "Other" when `allow_other`. A one-word answer has no shorter search, so without alternatives or "Other"
    there is nothing to try and the caller asks instead.
    """
    targets = [answer, *[a for a in alternatives if a]]
    queries = [q for q in _suggestion_queries(answer, alternatives) if clean(q).lower() != clean(answer).lower()]
    try:
        for query in queries[:10]:
            texts = _combobox_typed_options(page, combo, query)
            _note_seen(seen, texts)
            if not texts:
                continue
            for target in targets:
                pick = _best_suggestion(texts, target)
                if pick and _click_combobox_option(page, combo, pick):
                    log.info("combobox: typed %r, took %r for %r", query, pick, answer)
                    return True
        if allow_other:
            for query in ("Other", "Not listed"):
                texts = _combobox_typed_options(page, combo, query)
                _note_seen(seen, texts)
                pick = next((t for t in texts if _OTHER_RE.match(clean(t))), "")
                if pick and _click_combobox_option(page, combo, pick):
                    log.info("combobox: %r is not on this list — falling back to %r", answer, pick)
                    return True
    except Exception as e:  # noqa: BLE001
        log.debug("_combobox_search(%r): %s", answer, e)
    try:
        page.keyboard.press("Escape")
        combo.fill("", timeout=SHORT)
    except Exception:  # noqa: BLE001
        pass
    return False


def _log_combo_miss(combo: Any, answer: str, typed_options: list[str]) -> None:
    """Say what a list offered when the answer could not be chosen from it. A widget that draws its options
    somewhere the locators above cannot see shows up here as an empty list; one whose wording differs
    shows up with the options it really has."""
    try:
        shape = combo.evaluate("""e => { const h = e.getRootNode().host; const n = h || e.parentElement || e;
            return (n.outerHTML || '').replace(/ (class|style)="[^"]*"/g, '').slice(0, 1200); }""")
    except Exception:  # noqa: BLE001
        shape = ""
    log.warning("combobox: could not choose %r; options seen after typing: %s; control: %s",
                answer[:80], typed_options[:12], re.sub(r"\s+", " ", shape or "")[:1200])


# ---------- driving a dropdown built out of <div>s (see WIDGET_JS) ----------
_WIDGET_SCAN_JS = "(scope) => {" + DEEP_JS + WIDGET_JS + """
    return widgetRoots(scope).map(r => ({id: r.getAttribute('data-jobbot-widget'), kind: wKind(r), value: wValue(r),
                                         face: wFaceText(r), placeholder: wPlaceholderText(r)}));
}"""


def custom_widgets(page: Any, scope: str = "body") -> list[dict]:
    """Every dropdown built out of divs under `scope`, stamped so it can be reached again by `widget()`.

    Each entry: `id` (the stamp), `kind` ("select" / "multiselect" / "date"), `value` (what it shows, '' while
    it only shows its prompt), `face` (all its visible text) and `placeholder` (the prompt it draws while
    empty — "Course Start Month" — which is the half of the question its label does not carry).
    """
    try:
        return [w for w in (page.evaluate(_WIDGET_SCAN_JS, scope) or []) if isinstance(w, dict) and w.get("id")]
    except Exception as e:  # noqa: BLE001 - a page that cannot be scanned has no widgets to drive
        log.debug("custom_widgets: %s", e)
        return []


def widget(page: Any, wid: str) -> Any:
    return page.locator(f"[{WIDGET_ATTR}='{wid}']").first


def is_widget(el: Any) -> bool:
    try:
        return bool(el.evaluate(f"e => e.hasAttribute('{WIDGET_ATTR}')"))
    except Exception:  # noqa: BLE001
        return False


def in_widget(el: Any) -> bool:
    """True for a control that belongs to a widget: its search box, which the widget's own driver types into."""
    try:
        return bool(el.evaluate(f"e => !!e.closest('[{WIDGET_ATTR}]') && !e.hasAttribute('{WIDGET_ATTR}')"))
    except Exception:  # noqa: BLE001
        return False


def widget_value(root: Any) -> str:
    try:
        return clean(root.evaluate("e => {" + DEEP_JS + WIDGET_JS + " return wValue(e); }"))
    except Exception:  # noqa: BLE001
        return ""


def widget_kind(root: Any) -> str:
    try:
        return str(root.evaluate("e => {" + DEEP_JS + WIDGET_JS + " return wKind(e); }") or "select")
    except Exception:  # noqa: BLE001
        return "select"


def open_widget(page: Any, root: Any) -> bool:
    """Click the widget open and stamp the list it drew `data-jobbot-popup`. False when nothing opened."""
    try:
        root.evaluate("e => {" + DEEP_JS + WIDGET_JS + f"""
            for (const old of document.querySelectorAll('[{FACE_ATTR}]')) old.removeAttribute('{FACE_ATTR}');
            for (const old of document.querySelectorAll('[{POPUP_ATTR}]')) old.removeAttribute('{POPUP_ATTR}');
            wMarkSeenPopups();
            wFace(e).setAttribute('{FACE_ATTR}', '1'); }}""")
        face = page.locator(f"[{FACE_ATTR}]").first
        face.scroll_into_view_if_needed(timeout=SHORT)
        face.click(timeout=MEDIUM)
        # The list slides in: read too soon, a date picker's panel measured 0x0 and the page's header menu
        # was taken for the list instead. Poll until something has really opened.
        for _ in range(6):
            page.wait_for_timeout(300)
            found = root.evaluate("e => {" + DEEP_JS + WIDGET_JS + f"""
                const p = wPopupOf(e);
                if (!p) return false;
                p.setAttribute('{POPUP_ATTR}', '1');
                return true; }}""")
            if found:
                return True
        log.debug("open_widget: nothing opened under the widget")
        return False
    except Exception as e:  # noqa: BLE001
        log.debug("open_widget: %s", e)
        return False


def widget_popup(page: Any) -> Any:
    return page.locator(f"[{POPUP_ATTR}]").first


def widget_filter(page: Any, root: Any) -> Any | None:
    """The search box of an open widget — in its list (Shopee) or in its face (a filterable el-select)."""
    for scope in (widget_popup(page), root):
        try:
            box = scope.locator("input:not([type=hidden]):not([readonly])")
            for i in range(min(box.count(), 3)):
                if is_visible_now(box.nth(i)):
                    return box.nth(i)
        except Exception:  # noqa: BLE001
            continue
    return None


_LIST_LOADING_RE = re.compile(r"^\W*(?:searching|loading(?: more)?(?: results)?|please wait)\b", re.I)


def wait_list_loaded(page: Any, limit_ms: int = 8000) -> None:
    """Wait while an open list is still fetching for what was typed. A remote-search list (select2 with ajax,
    Lenovo's 160-currency "Salary Expectation - Currency") keeps the previous rows on screen under a
    "Searching…" row; read at a fixed 700 ms it offered Afghani…Lek and never the "US Dollar" just typed
    (application 336)."""
    waited = 0
    while waited < limit_ms:
        try:
            busy = page.evaluate("""() => !![...document.querySelectorAll(
                    '[role=option], .select2-results__option, li[class*=loading i], [class*=loading-results i]')]
                .find(o => o.offsetParent !== null && /^\\W*(searching|loading|please wait)/i.test((o.innerText || '').trim()))""")
        except Exception:  # noqa: BLE001
            return
        if not busy:
            return
        page.wait_for_timeout(300)
        waited += 300
    log.info("list still loading after %.1fs; reading it anyway", limit_ms / 1000)


def widget_type(page: Any, root: Any, text: str) -> bool:
    """Put `text` in the open widget's search box; failing one, type at the widget, which some forward."""
    box = widget_filter(page, root)
    try:
        if box is not None:
            box.click(timeout=SHORT)
            box.fill("", timeout=SHORT)
            if text:
                box.press_sequentially(text, delay=20, timeout=MEDIUM)
            page.wait_for_timeout(700)      # a remote list refetches on every keystroke
            wait_list_loaded(page)
            return True
        if text:
            page.keyboard.type(text, delay=20)
            page.wait_for_timeout(500)
            wait_list_loaded(page)
            return True
    except Exception as e:  # noqa: BLE001
        log.debug("widget_type(%r): %s", text[:30], e)
    return False


def popup_options(page: Any, limit: int = 120) -> tuple[Any, list[str]]:
    """(locator, texts) over the rows of whatever list is open: ARIA options where the page draws them, else
    the leaf rows of the popup a widget opened (see WIDGET_JS). The locator and the texts are index-aligned."""
    try:
        items = page.locator("[role=option]:visible")
        n = min(items.count(), limit)
        if n:
            return items, [option_text(items.nth(i)) for i in range(n)]
    except Exception:  # noqa: BLE001
        pass
    try:
        texts = page.evaluate("() => {" + DEEP_JS + WIDGET_JS + f"""
            for (const old of document.querySelectorAll('[{OPTION_ATTR}]')) old.removeAttribute('{OPTION_ATTR}');
            const popup = document.querySelector('[{POPUP_ATTR}]');
            if (!popup) return [];
            const rows = wOptions(popup).slice(0, {limit});
            rows.forEach((o, i) => o.setAttribute('{OPTION_ATTR}', String(i)));
            return rows.map(wText); }}""") or []
        return page.locator(f"[{OPTION_ATTR}]"), [clean(t) for t in texts]
    except Exception as e:  # noqa: BLE001
        log.debug("popup_options: %s", e)
        return page.locator(f"[{OPTION_ATTR}]"), []


def popup_is_complete(page: Any) -> bool:
    """True when the open list shows every option it has: no search box to narrow it and nothing to scroll.
    A virtualised list (Shopee renders 12 of 247 countries) or a searchable one is open-ended, and the
    rows on screen must not be handed to the resolver as the whole list."""
    try:
        return bool(page.evaluate("() => {" + DEEP_JS + WIDGET_JS + f"""
            const popup = document.querySelector('[{POPUP_ATTR}]');
            if (!popup) return false;
            if ([...popup.querySelectorAll('input:not([type=hidden])')].some(wVis)) return false;
            for (const n of [popup, ...popup.querySelectorAll('*')]) {{
                if (n.scrollHeight > n.clientHeight + 4 && getComputedStyle(n).overflowY !== 'visible' && n.querySelector(W_OPT)) return false;
            }}
            return true; }}"""))
    except Exception:  # noqa: BLE001
        return False


def close_popups(page: Any, root: Any | None = None) -> None:
    """Shut whatever list is open. Escape first; a multi-select that ignores it is closed by clicking its own
    face, and last by a click on the page's margin — an open list intercepts every later click (Shopee's
    Skills list swallowed the click meant for the question below it)."""
    def still_open() -> bool:
        try:
            return is_visible_now(widget_popup(page))
        except Exception:  # noqa: BLE001
            return False
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        if still_open() and root is not None:
            page.locator(f"[{FACE_ATTR}]").first.click(timeout=SHORT)
            page.wait_for_timeout(200)
        if still_open():
            page.mouse.click(2, 300)
            page.wait_for_timeout(200)
    except Exception as e:  # noqa: BLE001
        log.debug("close_popups: %s", e)


def _click_row(items: Any, texts: list[str], idx: int, page: Any) -> None:
    items.nth(idx).click(timeout=SHORT)
    page.wait_for_timeout(300)


def _pick_row(items: Any, texts: list[str], answer: str, page: Any, seen: list[str]) -> bool:
    """Click the row that is `answer`: exact, the same option differently punctuated, the one row left that
    contains it, or the first that starts with it. Never a blind Enter."""
    _note_seen(seen, texts)
    want = clean(answer).lower()
    if not want:
        return False
    pick = next((i for i, t in enumerate(texts) if t.lower() == want or same_option(t, answer)), None)
    if pick is None and len(texts) == 1 and want in texts[0].lower():
        pick = 0
    if pick is None:
        pick = next((i for i, t in enumerate(texts) if t.lower().startswith(want)), None)
    if pick is None:
        return False
    _click_row(items, texts, pick, page)
    return True


def _comma_parts(value: str) -> list[str]:
    """"Dhaka, Bangladesh" is also "Dhaka" and "Bangladesh": a list of countries holds the second."""
    parts = [clean(p) for p in (value or "").split(",")]
    return [p for p in parts if p and p.lower() != clean(value).lower()]


def choose_widget(page: Any, root: Any, answer: str, alternatives: tuple[str, ...] | list[str] = (), *,
                  allow_other: bool = False, seen: list[str] | None = None) -> bool:
    """choose_combobox for a div-built dropdown: open it, search its own box, click the row, read it back."""
    if seen is None:
        seen = []
    if not open_widget(page, root):
        log.info("widget: could not open the list for %r", answer[:40])
        return False
    try:
        targets = [answer, *[a for a in alternatives if a], *_comma_parts(answer)]
        # The list as it opens — the whole of a short one, the first page of a long one.
        items, texts = popup_options(page)
        if _pick_row(items, texts, answer, page, seen) and widget_value(root):
            return True
        filterable = widget_filter(page, root) is not None
        queries: list[str] = []
        for q in [answer, *_suggestion_queries(answer, alternatives), *_comma_parts(answer)]:
            if q and q not in queries:
                queries.append(q)
        for query in queries[:10] if filterable else []:
            if not widget_type(page, root, query):
                break
            items, texts = popup_options(page)
            _note_seen(seen, texts)
            if not texts:
                continue
            for target in targets:
                pick = _best_suggestion(texts, target)
                if pick and pick in texts:
                    _click_row(items, texts, texts.index(pick), page)
                    if widget_value(root):
                        log.info("widget: typed %r, took %r for %r", query, pick, answer)
                        return True
        if allow_other and filterable:
            for query in ("Other", "Not listed", "Not applicable"):
                if not widget_type(page, root, query):
                    break
                items, texts = popup_options(page)
                _note_seen(seen, texts)
                pick = next((t for t in texts if _OTHER_RE.match(clean(t))), "")
                if pick:
                    _click_row(items, texts, texts.index(pick), page)
                    if widget_value(root):
                        log.info("widget: %r is not on this list — falling back to %r", answer, pick)
                        return True
        # Nothing took. Show the caller the list's real rows rather than the empty result of a search.
        if filterable:
            widget_type(page, root, "")
            _, texts = popup_options(page)
            _note_seen(seen, texts)
        _log_combo_miss(root, answer, seen)
        return False
    except Exception as e:  # noqa: BLE001
        log.debug("choose_widget(%r): %s", answer, e)
        return False
    finally:
        close_popups(page, root)


def widget_options(page: Any, root: Any) -> list[str]:
    """The rows a div-built dropdown offers, when it shows all of them; [] for a searchable or scrolling list,
    whose first page must not be mistaken for the whole (the resolver would pick among twelve of 247)."""
    if not open_widget(page, root):
        return []
    try:
        if not popup_is_complete(page):
            return []
        _, texts = popup_options(page)
        return [t for t in texts if t and not is_prompt_value(t)]
    finally:
        close_popups(page, root)


# ---------- month pickers ----------
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_PICKER_CELL_JS = "() => {" + DEEP_JS + WIDGET_JS + f"""
    const popup = document.querySelector('[{POPUP_ATTR}]');
    if (!popup) return null;
    // Document-wide: the stamps left in the previous picker's (now hidden) panel were what a locator found
    // first, so the end-of-range panel's "Jan" click landed on the start panel's invisible one.
    for (const old of document.querySelectorAll('[data-jobbot-cell], [data-jobbot-head]')) {{
        old.removeAttribute('data-jobbot-cell'); old.removeAttribute('data-jobbot-head'); }}
    const cells = [...popup.querySelectorAll('td, [class*="col" i], [class*="cell" i], [class*="item" i], [role=gridcell], button')]
        .filter(c => wVis(c) && wClean(c.innerText).length <= 12 && !c.querySelector('td, [class*="col" i], [class*="cell" i]'));
    cells.forEach((c, i) => c.setAttribute('data-jobbot-cell', String(i)));
    const heads = [...popup.querySelectorAll('[class*="header" i] *, [class*="title" i], [role=heading]')]
        .filter(h => wVis(h) && !h.querySelector('*') || (h.children.length <= 2 && wVis(h)));
    heads.forEach((h, i) => h.setAttribute('data-jobbot-head', String(i)));
    const nav = sel => {{ const n = popup.querySelector(sel); return n && wVis(n); }};
    return {{cells: cells.map(wText), heads: heads.map(wText),
            prev: nav('[class*="prev" i], [aria-label*="previous" i]'), next: nav('[class*="next" i], [aria-label*="next" i]')}};
}}"""


def _month_index(text: str) -> int | None:
    t = clean(text).lower()[:3]
    return _MONTHS.index(t) + 1 if t in _MONTHS else None


def pick_month(page: Any, root: Any, year: int, month: int) -> bool:
    """Choose `month`/`year` in a div-built month picker: a year label to click, a table of years, a table
    of months (Shopee's Course Period; Element UI's month picker is the same shape). True when the widget
    reads back a value afterwards."""
    if not open_widget(page, root):
        return False
    try:
        label_tried = False
        for step in range(40):
            info = page.evaluate(_PICKER_CELL_JS)
            if not info:
                log.debug("pick_month: no open picker at step %d", step)
                return False
            cells, heads = info["cells"], info["heads"]
            years = [i for i, t in enumerate(cells) if re.fullmatch(r"\d{4}", clean(t))]
            months = [i for i, t in enumerate(cells) if _month_index(t)]
            head_year = next((int(m.group(0)) for h in heads for m in [re.search(r"\b(19|20)\d{2}\b", h)] if m), None)
            log.debug("pick_month step %d: heads=%s years=%s months=%d head_year=%s", step,
                      [h for h in heads if h], [cells[i] for i in years], len(months), head_year)
            if not years and not months and step < 4:
                # The first picker opened on a page draws its panel a moment after the popup itself
                # (Shopee's Course Period: an empty popper at first read, the month table on the next).
                page.wait_for_timeout(400)
                continue
            if years and not months:
                # A table of years: click ours, else page to the decade that holds it.
                hit = next((i for i in years if int(cells[i]) == year), None)
                if hit is not None:
                    _click_picker(page, page.locator(f"[data-jobbot-cell='{hit}']").first)
                    continue
                shown = [int(cells[i]) for i in years]
                arrow = "prev" if year < min(shown) else "next"
                if not info.get(arrow):
                    return False
                target = _visible_arrow(widget_popup(page).locator(
                    f"[class*='{arrow}' i], [aria-label*='{'previous' if arrow == 'prev' else 'next'}' i]"), prefer_last=False)
                if target is None:
                    return False
                _click_picker(page, target)
                continue
            if months:
                if head_year is not None and head_year != year:
                    label = next((i for i, h in enumerate(heads) if re.fullmatch(r"\s*(19|20)\d{2}\s*", h)), None)
                    if label is not None and not label_tried:
                        # The year in the header opens a table of years on most pickers.
                        label_tried = True
                        _click_picker(page, page.locator(f"[data-jobbot-head='{label}']").first)
                        continue
                    # Not here it does not (the end-of-range panel on Shopee's Course Period takes no click
                    # on its year): page a year at a time with the arrow nearest the header's labels.
                    arrow = "prev" if year < head_year else "next"
                    if not info.get(arrow):
                        return False
                    arrows = widget_popup(page).locator(
                        f"[class*='{arrow}' i], [aria-label*='{'previous' if arrow == 'prev' else 'next'}' i]")
                    target = _visible_arrow(arrows, prefer_last=(arrow == "prev"))
                    if target is None:
                        return False
                    _click_picker(page, target)
                    continue
                hit = next((i for i in months if _month_index(cells[i]) == month), None)
                if hit is None:
                    return False
                _click_picker(page, page.locator(f"[data-jobbot-cell='{hit}']").first)
                page.wait_for_timeout(200)
                got = widget_value(root)
                log.debug("pick_month: clicked %r, widget now shows %r", cells[hit], got)
                return bool(got)
            log.debug("pick_month: neither months nor years on screen (cells=%s)", cells[:8])
            return False
        return False
    except Exception as e:  # noqa: BLE001
        log.debug("pick_month: %s", e)
        return False
    finally:
        close_popups(page, root)


def _visible_arrow(arrows: Any, prefer_last: bool) -> Any | None:
    """The arrow a person could press: a range picker's end panel keeps one of its two drawn and hides the
    other, and a click aimed at the hidden one waits for ever."""
    try:
        shown = [arrows.nth(i) for i in range(min(arrows.count(), 6)) if is_visible_now(arrows.nth(i))]
    except Exception:  # noqa: BLE001
        return None
    if not shown:
        return None
    return shown[-1] if prefer_last else shown[0]


def _click_picker(page: Any, el: Any) -> None:
    """Click a calendar cell or header. A real click first; when the previous picker's popup is still fading
    out over it (Playwright then waits for the overlay for ever), the element's own click handler."""
    try:
        el.click(timeout=SHORT)
    except Exception as e:  # noqa: BLE001
        log.debug("picker click fell back to a scripted click: %s", str(e)[:80])
        el.evaluate("e => e.click()")
    page.wait_for_timeout(350)


def fill_date_widget(page: Any, root: Any, answer: str) -> bool:
    """Put a date answer ("January 2015", "2019-01", "Immediately") into a div-built date picker."""
    from dateutil import parser as dateparser
    text = clean(answer)
    if not text:
        return False
    today = datetime.now().date()
    try:
        when = _target_date(text, today) if _SOON_RE.search(text) or _IN_RE.search(text) else \
            dateparser.parse(text, fuzzy=True, default=datetime(today.year, 1, 1)).date()
    except (ValueError, OverflowError):
        return False
    if when is None:
        return False
    return pick_month(page, root, when.year, when.month)


# ---------- consent boxes drawn without an input ----------
# "By proceeding, I confirm that I have carefully read and agree to the Terms of Service and Privacy Policy"
# beside a small <div> that is the tick box — no <input>, no role, and on Shopee not even inside the <form>.
# Nothing above can see it, and the submit is refused until it is ticked. Found by its sentence, which is the
# one thing every consent line has; ticked by clicking the icon-sized element drawn before the sentence.
_CONSENT_SCAN_JS = "() => {" + DEEP_JS + WIDGET_JS + r"""
    const RE = /\bI\s+(?:confirm|agree|accept|acknowledge|consent|certify|declare|understand|have\s+read)\b[^.]{0,200}\b(?:terms|privacy|policy|conditions|consent|notice|agreement|accurate|true)\b/i;
    for (const old of document.querySelectorAll('[data-jobbot-consent]')) old.removeAttribute('data-jobbot-consent');
    const out = [];
    let n = 0;
    for (const el of deepAll('span, p, label, div, li')) {
        if (!wVis(el) || el.children.length > 6) continue;
        const t = wText(el);
        if (t.length > 400 || !RE.test(t)) continue;
        if ([...el.children].some(k => RE.test(wText(k)))) continue;                     // take the innermost block
        const block = el.parentElement || el;
        if (block.querySelector('input, [role=checkbox], [role=switch], [role=radio]')) continue;  // a real control draws this one
        const small = e => { const r = e.getBoundingClientRect(); return r.width >= 8 && r.width <= 40 && r.height >= 8 && r.height <= 40; };
        const box = [...block.children].find(k => k !== el && !k.contains(el) && !k.querySelector('a') && small(k)
                                                  && (k.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
        if (!box) continue;      // a sentence with nothing to tick is prose; clicking it could follow its policy link
        box.setAttribute('data-jobbot-consent', String(n));
        out.push({id: String(n), text: t.slice(0, 160), box: true,
                  state: (box.className || '').toString() + '|' + box.innerHTML.length});
        n++;
    }
    return out;
}"""


def tick_consent_clauses(ctx: ApplyContext) -> int:
    """Tick every consent line drawn without a checkbox input. Returns how many were ticked.

    Idempotent across the re-passes a submit makes: what the box looked like untouched and ticked is kept
    on ctx.extra, so a second pass neither unticks it nor clicks a box that is already on."""
    page = ctx.page
    try:
        found = page.evaluate(_CONSENT_SCAN_JS) or []
    except Exception as e:  # noqa: BLE001
        log.debug("consent scan: %s", e)
        return 0
    states: dict = ctx.extra.setdefault("consent_states", {})
    ticked = 0
    for f in found:
        key = f["text"][:80]
        el = page.locator(f"[data-jobbot-consent='{f['id']}']").first
        known = states.get(key)
        if known and f["state"] == known.get("ticked"):
            continue        # on, from an earlier pass
        if re.search(r"\b(?:checked|active|selected|is-on|ticked)\b", f["state"].split("|")[0], re.I):
            continue        # the page says it is on
        try:
            el.scroll_into_view_if_needed(timeout=SHORT)
            el.click(timeout=MEDIUM)
            page.wait_for_timeout(300)
            after = el.evaluate("e => (e.className || '').toString() + '|' + e.innerHTML.length")
        except Exception as e:  # noqa: BLE001
            log.info("consent: could not click %r (%s)", f["text"][:60], str(e)[:80])
            continue
        if after == f["state"]:
            log.info("consent: clicked %r but nothing changed; leaving it", f["text"][:60])
            continue
        states[key] = {"untouched": f["state"], "ticked": after}
        ticked += 1
        log.info("consent: ticked %r (drawn without a checkbox)", f["text"][:80])
    return ticked


# ---------- consent boxes the walk could not see ----------
# UKG Pro Recruiting (UltiPro, application 318, MNP) asks for name, phone and "By checking this box, I have
# read and agree to the Consent and Privacy Policy" on its "Almost there!" page after signup, with Create
# account disabled until the box is ticked. The walker filled the three text boxes and never touched the
# tick: a box drawn by a styled sibling over a zero-sized input with no <label> (or a bare role=checkbox
# <div>) is invisible to every scoped walk, and tick_consent_clauses above skips a line that has a control
# of its own. So the run stopped on a page whose only gap was one consent tick.
#
# This finds such a box by the shape every consent has -- an unticked checkbox, in any drawing, whose own
# words agree to terms / privacy / a policy -- anywhere on the page, and ticks it. A hidden input with nothing
# visible drawing it is a honeypot and is left alone.
_CONSENT_BOX_JS = "() => {" + DEEP_JS + WIDGET_JS + r"""
    const AGREE = /\b(?:agree|accept|acknowledge|consent|have\s+read|certify|confirm)\b/i;
    const SUBJECT = /\b(?:terms|privacy|policy|policies|conditions|consent|notice|agreement|statement)\b/i;
    const small = e => { const r = e.getBoundingClientRect(); return r.width >= 8 && r.width <= 48 && r.height >= 8 && r.height <= 48; };
    for (const old of document.querySelectorAll('[data-jobbot-consentbox]')) old.removeAttribute('data-jobbot-consentbox');
    const out = [];
    let n = 0;
    for (const el of deepAll('input[type=checkbox], [role=checkbox], [role=switch]')) {
        const native = el.tagName === 'INPUT';
        if (native ? el.checked : (el.getAttribute('aria-checked') || '') === 'true') continue;
        if (el.disabled || (el.getAttribute('aria-disabled') || '') === 'true') continue;
        if (!native && el.querySelector('input[type=checkbox]')) continue;    // its input is the one to tick
        // What a person sees and clicks: the control itself, its <label>, an ARIA wrapper, or a small styled
        // box beside it or round it.
        let face = wVis(el) ? el : null;
        if (!face && native) {
            if ((el.getAttribute('aria-hidden') || '') === 'true' && !(el.labels || []).length) continue;
            face = [...(el.labels || [])].find(wVis)
                || (el.parentElement && [...el.parentElement.children].find(k => k !== el && wVis(k) && small(k)))
                || (() => { for (let a = el.parentElement, i = 0; a && i < 3; a = a.parentElement, i++)
                                if (wVis(a) && (small(a) || a.matches('label, [role=checkbox], [role=switch]'))) return a;
                            return null; })();
        }
        if (!face) continue;
        // Its words: its own label or aria naming, else the nearest wrapper that holds a sentence.
        let words = [...(el.labels || [])].map(wText).join(' ') || el.getAttribute('aria-label') || '';
        const by = el.getAttribute('aria-labelledby');
        if (!words && by) words = by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(wText).join(' ');
        if (!words) for (let a = el.parentElement, i = 0; a && i < 4; a = a.parentElement, i++) {
            const t = wText(a);
            if (t.length >= 10) { words = t.length <= 400 ? t : ''; break; }
        }
        if (!AGREE.test(words) || !SUBJECT.test(words)) continue;
        el.setAttribute('data-jobbot-consentbox', String(n));
        out.push({id: String(n), text: words.slice(0, 160), native, drawn: face !== el});
        n++;
    }
    return out;
}"""


def tick_consent_boxes(page: Any) -> int:
    """Tick every unticked consent checkbox on the page, however it is drawn. Returns how many were ticked.

    Consent is given, not asked about (see the consent policy): a form that will not go on without it offers
    no choice the user has any reason to decline here."""
    try:
        found = page.evaluate(_CONSENT_BOX_JS) or []
    except Exception as e:  # noqa: BLE001
        log.debug("consent boxes: %s", e)
        return 0
    ticked = 0
    for f in found:
        el = page.locator(f"[data-jobbot-consentbox='{f['id']}']").first
        try:
            if f["native"]:
                ok = tick(el)
            else:
                el.scroll_into_view_if_needed(timeout=SHORT)
                el.click(timeout=MEDIUM)
                page.wait_for_timeout(300)
                if (el.get_attribute("aria-checked") or "") != "true":
                    el.focus(timeout=SHORT)
                    page.keyboard.press("Space")
                    page.wait_for_timeout(300)
                ok = (el.get_attribute("aria-checked") or "") == "true"
        except Exception as e:  # noqa: BLE001
            log.info("consent: could not tick %r (%s)", f["text"][:60], str(e)[:80])
            continue
        if ok:
            ticked += 1
            log.info("consent: ticked %r%s", f["text"][:80], " (drawn by a styled box)" if f["drawn"] else "")
        else:
            log.info("consent: %r would not tick", f["text"][:60])
    return ticked


# The words a list control shows while nothing is chosen. Rippling's eligibility question drew "Select" in a
# <p>, which read back as the answer, so the question was reported as answered and never asked (application
# 175); its Apply button stayed disabled over it.
PROMPT_VALUE_RE = re.compile(
    r"^\s*(?:-+\s*)?(?:please\s+)?(?:select|choose|pick|search)"
    r"(?:\s+(?:one|an?\s+option|an?\s+answer|an?\s+item|here))?\s*(?:\.{3}|…)?\s*(?:-+)?\s*$"
    # The same prompt in the languages boards are written in: Coveo's French form showed "Veuillez
    # sélectionner" in three required lists, read as answers, so they went out empty (application 378).
    r"|^\s*(?:-+\s*)?(?:veuillez\s+)?(?:s[ée]lectionner|choisir|choisissez|s[ée]lectionnez)(?:\s+une?\s+\w+)?\s*(?:\.{3}|…)?\s*(?:-+)?\s*$"
    r"|^\s*(?:-+\s*)?(?:bitte\s+)?(?:w[äa]hlen|ausw[äa]hlen)(?:\s+sie)?\s*(?:\.{3}|…)?\s*(?:-+)?\s*$"
    r"|^\s*(?:-+\s*)?(?:por\s+favor\s+)?(?:seleccione|selecciona|seleccionar|elija|selecione)(?:\s+una?\s+\w+)?\s*(?:\.{3}|…)?\s*(?:-+)?\s*$"
    r"|^\s*(?:-+\s*)?(?:seleziona|scegli|selecteer|kies)\s*(?:\.{3}|…)?\s*(?:-+)?\s*$", re.I)


def is_prompt_value(text: str) -> bool:
    return bool(PROMPT_VALUE_RE.match(clean(text)))


def combobox_value(combo: Any) -> str:
    """What a combobox holds, '' while it only shows its prompt. See _combobox_shown for the reading."""
    if is_widget(combo):
        return widget_value(combo)
    shown = _combobox_shown(combo)
    if not shown or is_prompt_value(shown):
        return ""
    try:
        own = [clean(combo.get_attribute(a) or "") for a in ("aria-label", "placeholder")]
    except Exception:  # noqa: BLE001
        own = []
    return "" if clean(shown) in [o for o in own if o] else shown


# Coral (Sea's career site) shows the choice as the bare text of [data-coral-select-selected-content], drawn over
# the search input; the input itself holds only what is being typed, so "Dubai" typed into a list of countries
# read back as the chosen value though nothing had been picked. While the input has focus the node is taken
# away altogether, and then nothing on the control is a committed value. The prompt sits in the same node
# inside a child element, so only the node's own text is the value. null when the control is not a Coral select.
_CORAL_VALUE_JS = """
    if (!e.closest('[data-coral-text-field-wrapper]') || e.getAttribute('aria-haspopup') !== 'listbox') return null;
    const coral = comboScope(e).querySelector('[data-coral-select-selected-content]');
    if (!coral) return '';
    return Array.from(coral.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join('');
"""


def _combobox_shown(combo: Any) -> str:
    """Current value shown by a combobox (the vendor's value node, or the input text).

    react-select renders the chosen value as a sibling of the input's own wrapper:

        div.select__control > div.select__value-container > [div.select__single-value] + div.select__input-container > input

    The old lookup took the nearest ancestor whose class contained "select" — the input-container — and
    searched inside it, where the value never is. Every answered Greenhouse dropdown therefore read back as
    empty: the walker re-asked it on each pass, and one the user had chosen by hand in the window was
    asked about again after Continue. Walk out to whichever ancestor holds the value instead.

    Regression (application 122, LG Electronics on Dayforce): Dayforce's form is Ant Design, which shows the
    choice in a `span.ant-select-selection-item` and blanks its own search input afterwards. No selector
    here matched that span, so a Prefix that had just been set to "Mr" — visible on the form, sitting in the
    control — read back as empty and choose_combobox reported "Could not choose 'Mr' for 'Prefix'", failing
    an application whose field was correctly filled.
    """
    try:
        coral = combo.evaluate("e => {" + _COMBO_JS + _CORAL_VALUE_JS + "}")
        if coral is not None:
            return clean(coral)
        v = current_value(combo)
        if v:
            return v
        return clean(combo.evaluate(
            "e => {" + _COMBO_JS +
            """ const sv = comboFind(e, COMBO_VALUE_SEL);
                if (sv) return sv.textContent;
                // aria-style comboboxes name their choice on the control itself
                const t = e.getAttribute('aria-valuetext') || e.getAttribute('data-value') || '';
                if (t) return t;
                // A select built as a web component keeps its choice on the host element, not on the
                // trigger the [role=combobox] search finds: SmartRecruiters' phone country picker is a
                // <button role="combobox"> inside the shadow root of <spl-select value="NZ">, and every
                // search above it stops at that root. Read back as empty, a picker the page had already
                // set to the job's own country was asked about as though it were unanswered.
                // The immediate host only — the component further out owns a different field.
                const host = e.getRootNode().host;
                if (host) return (host.value != null ? String(host.value) : '') || host.getAttribute('value') || '';
                return '';
            }"""))
    except Exception:
        return ""


def select_is_default(el: Any) -> bool:
    """True when a <select> still shows what the markup chose, rather than what a person chose.

    `defaultSelected` is the exact signal: the option carrying it is the one the HTML marked, so a form
    re-rendered with the candidate's previous choice reads as chosen and an untouched list does not. The
    fallback covers the lists that mark nothing at all — an unfilled country dropdown resting on its first
    real option, which is how "Afghanistan" was learned as this candidate's citizenship.
    """
    try:
        return bool(el.evaluate(
            """e => {
                if (e.selectedIndex < 0) return true;
                const o = e.options[e.selectedIndex];
                if (o && o.defaultSelected) return true;
                const marked = Array.from(e.options).some(x => x.defaultSelected);
                return !marked && e.selectedIndex <= 0;
            }"""))
    except Exception:
        return False


def combobox_is_placeholder(combo: Any) -> bool:
    """True when a combobox is showing its placeholder rather than a chosen value.

    Unlike <select>, combobox_value() has no index to check: it reads whatever the widget renders, and a
    react-select drawing "Select..." or a locale list drawing its default looks exactly like an answer.
    """
    if is_widget(combo):
        return not widget_value(combo)
    shown = _combobox_shown(combo)
    if shown and not combobox_value(combo):
        return True
    try:
        return bool(combo.evaluate(
            "e => {" + _COMBO_JS +
            """ if (comboFind(e, COMBO_VALUE_SEL)) return false;
                if (comboFind(e, COMBO_PLACEHOLDER_SEL)) return true;
                // an aria combobox that names a value only through its default attributes
                return !e.value && !!(e.getAttribute('aria-valuetext') || e.getAttribute('data-value'));
            }"""))
    except Exception:
        return False


def choice_is_default(el: Any) -> bool:
    """True when the ticked radio/checkbox was ticked by the markup, not by a person.

    Takes either the group container or the input itself: a lone consent box has no group around it, and
    `locator` only ever looks at descendants, so searching inside the input would find nothing and report
    every pre-ticked box as a real answer.
    """
    if el is None:
        return False
    try:
        if el.evaluate("e => (e.tagName || '').toLowerCase() === 'input' && "
                       "['checkbox', 'radio'].includes((e.type || '').toLowerCase())"):
            return bool(el.evaluate("e => e.checked && e.defaultChecked"))
        boxes = el.locator("input[type=radio]:checked, input[type=checkbox]:checked")
        if not boxes.count():
            return False
        return bool(boxes.first.evaluate("e => e.defaultChecked"))
    except Exception:
        return False


def text_is_default(el: Any) -> bool:
    """True when a text control still holds the value the markup gave it."""
    try:
        return bool(el.evaluate("e => e.value !== undefined && e.value === e.defaultValue"))
    except Exception:
        return False


def click_submit(page: Any, names: tuple[str, ...]) -> None:
    for name in names:
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I))
            if visible(btn, SHORT):
                btn.first.scroll_into_view_if_needed(timeout=SHORT)
                btn.first.click(timeout=MEDIUM)
                return
        except Exception:
            continue
    for name in names:
        try:
            btn = page.locator(f"button:has-text('{name}'), input[type=submit][value*='{name}' i]")
            if visible(btn, SHORT):
                btn.first.click(timeout=MEDIUM)
                return
        except Exception:
            continue
    if spam_flagged(page):
        raise NeedsHuman(SPAM_FLAG_MSG)
    raise ApplyError("Submit button not found")


# A submit turned down by the board's own bot score, worded as a spam verdict: Ashby's "Your application
# submission was flagged as possible spam. If you believe this was a mistake, please submit your application
# again" (EvenUp, application 419). The form is filled and the Submit button gone, which used to read as
# "Submit button not found" and a failed application. Nothing here tries to get round the check.
_SPAM_FLAG_RE = re.compile(r"flagged as (?:possible |potential |likely )?spam|(?:suspected|possible) (?:spam|bot)\b"
                           r"|automated (?:submission|traffic) (?:was )?detected|looks like (?:spam|a bot)", re.I)
SPAM_FLAG_MSG = ("The site's bot check flagged the submission as spam (every field is filled). Press Submit "
                 "yourself in the open window, then click 'Mark applied' once it confirms.")


# Pages reached from a form's own links where nothing should ever be submitted: accommodation requests, contact
# forms, privacy/terms/FAQ/help pages (Siemens, application 438, sent its accommodation form).
NOT_APPLICATION_URL_RE = re.compile(r"accommodation|/contact(?:-us)?(?:/|\b|$)|/privacy|privacy-(?:policy|notice|statement)"
                                    r"|/terms(?:-of|/|\b)|/faq|/help(?:/|\b)|/support/|/newsletter|/subscribe|preference",
                                    re.I)


def spam_flagged(page: Any) -> bool:
    try:
        return bool(_SPAM_FLAG_RE.search(clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))))
    except Exception:  # noqa: BLE001
        return False


# The emailed-code boxes live inside the same <form> as the screening questions, so every adapter's control
# loop walks straight into them. They are not questions: handle_verification fills them after the submit, and
# asking the user for "Security code" as if it were one is how a resume ends up stuck asking twice.
_VERIFY_LABEL_RE = re.compile(r"\b(?:security|verification|confirmation|one[\s-]?time|access)\s*code\b"
                              r"|\bone[\s-]?time\s*(?:password|passcode|pin)\b|\botp\b", re.I)


def is_verification_control(el: Any, label: str = "") -> bool:
    """True for an input that belongs to the emailed-code step rather than to the application's questions."""
    if label and _VERIFY_LABEL_RE.search(label):
        return True
    try:
        attrs = el.evaluate(
            "e => [e.getAttribute('autocomplete'), e.getAttribute('maxlength'), e.getAttribute('name'), "
            "e.getAttribute('id'), e.getAttribute('inputmode')].join(' ')") or ""
    except Exception:
        return False
    if "one-time-code" in attrs.lower():
        return True
    if _VERIFY_LABEL_RE.search(attrs.replace("_", " ").replace("-", " ")):
        return True
    return False


# ---------- phone numbers ----------
# Controls that hold a phone number's country code apart from the number itself. One list for both the
# reading and the driving, because the asymmetry between them was the bug: the old read selector found
# Oracle's country-code control (it reported "+971" three passes running) while the old write selector —
# buttons and intl-tel-input flags only — did not, so the picker could be read and never corrected.
# Application 135 (Presight, Oracle) therefore sent "8801771614053" to the box beside a picker resting on
# +971 and the form answered "Enter a valid number." until the run failed.
DIAL_CONTROL_SEL = (
    "select, [role=combobox][aria-label*='country' i], [role=combobox][aria-label*='dial' i], "
    # Oracle Recruiting Cloud names this control nowhere a selector could see it: no aria-label (it points
    # at a <span> through aria-labelledby), no country in any class — just
    # id="country-codes-dropdownphoneNumber" and the matching aria-controls. The id is the only honest
    # handle on it, and without these three lines application 135 could read "+971" off the page and never
    # find anything to change (see dial_code_on_page).
    "[id*='country-code' i], [id*='countrycode' i], [aria-controls*='country-code' i], "
    "[role=combobox][aria-labelledby*='country' i], "
    "[data-automation-id*='countryPhoneCode' i], .iti__selected-flag, .iti__selected-country, "
    "[class*='country-code' i], [class*='countrycode' i], [class*='dial' i], "
    "button[aria-label*='country' i], button[aria-label*='pays' i], "
    "button[aria-label*='dial' i], button[aria-label*='indicatif' i]")
# Read-only additions, and the reason the two lists are not one: Workday shows the code it holds in a pill,
# and clicking that pill DELETES the choice — it is something to read and never something to drive. An
# <option> is not a control either.
_DIAL_SHOWN_SEL = (DIAL_CONTROL_SEL
                   + ", [data-automation-id='selectedItem'], [class*='country' i] option:checked")
# Where a picker draws its choices. Oracle's country-code widget declares aria-haspopup="grid", so its
# rows are gridcells and none of them carry role=option — a scan for options alone finds an open list with
# nothing in it.
DIAL_ROW_SEL = ("[role=option]:visible, [role=menuitem]:visible, [role=row]:visible, "
                "[role=gridcell]:visible, li:visible")
# A country list runs to ~240 rows. The 60 an ordinary combobox stops at is a cap sized for a yes/no list,
# and Bangladesh sits well past it.
DIAL_OPTIONS = 400


def is_dial_control(el: Any) -> bool:
    """True when this control picks a phone number's country code rather than holding the number.

    Oracle labels both halves of its phone widget "Phone Number" — the <span> reading "Country code" sits
    inside the composite and is not the accessible name — so the label-driven walker matched the country
    code combobox as the phone field and typed "+8801771614053" into it (applications 135-138). Nothing in
    the label can tell them apart; the id can.
    """
    try:
        return bool(el.evaluate(
            """e => {
                const id = (e.id || '') + ' ' + (e.getAttribute('aria-controls') || '');
                if (/country.?code|dial.?code|phone.?prefix/i.test(id)) return true;
                const ref = e.getAttribute('aria-labelledby') || '';
                if (/country.?code/i.test(ref)) return true;
                const lbl = ref.split(/\\s+/).map(x => document.getElementById(x))
                              .filter(Boolean).map(x => x.textContent || '').join(' ');
                if (/\\b(country|dial(l?ing)?|area)\\s*code\\b/i.test(lbl)) return true;
                // Named nowhere at all: Shopee's picker is a div showing "Select" with no id, no aria and
                // no telling class, beside a box whose placeholder says "Contact Number" (application 274).
                // A list control that stands immediately before the one phone box in its row is the
                // phone's country code -- unless its own prompt says it is a *type* of phone.
                const isList = e.hasAttribute('data-jobbot-widget') || e.tagName === 'SELECT'
                            || e.getAttribute('role') === 'combobox';
                if (!isList) return false;
                const own = (e.getAttribute('placeholder') || '') + ' ' + (e.getAttribute('aria-label') || '')
                          + ' ' + (e.getAttribute('name') || '') + ' ' + (e.id || '');
                if (/\\b(?:type|kind|method|preferred|device)\\b/i.test(own)) return false;
                const vis = x => { const r = x.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
                const CTRL = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]),'
                           + ' textarea, select, [role=combobox], [data-jobbot-widget]';
                for (let w = e.parentElement, i = 0; w && i < 4; w = w.parentElement, i++) {
                    const others = [...w.querySelectorAll(CTRL)]
                        .filter(x => x !== e && !e.contains(x) && !x.contains(e) && vis(x));
                    if (!others.length) continue;
                    if (others.length > 1) return false;
                    const nb = others[0];
                    if (!(e.compareDocumentPosition(nb) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
                    if (nb.tagName !== 'INPUT') return false;
                    const about = [nb.getAttribute('placeholder'), nb.getAttribute('aria-label'), nb.getAttribute('name'),
                                   nb.id, nb.labels && nb.labels[0] && nb.labels[0].innerText, w.innerText]
                        .filter(Boolean).join(' ');
                    return nb.type === 'tel' || /\\b(?:phone|mobile|telephone|cell|contact\\s*(?:number|no\\b)|tel)\\b/i.test(about);
                }
                return false;
            }""")) or _shows_dial_code(el)
    except Exception:  # noqa: BLE001
        return False


_DIAL_SHOWN_RE = re.compile(r"[^\d+]{0,40}\(?\+\s?\d{1,4}\)?[^\d]{0,40}")


def _shows_dial_code(el: Any) -> bool:
    """A list control whose current choice reads like "+1 US" or "Bangladesh (+880)". Rippling's picker is
    named only "Search" and sits under the "Phone number" label, so neither its id nor its name says what it
    is; what it shows does (application 175 learned "+1 US" as the answer to "Search")."""
    try:
        role = (el.get_attribute("role") or "").lower()
        tag = (el.evaluate("e => e.tagName") or "").lower()
        if role != "combobox" and tag != "select":
            return False
        shown = combobox_value(el) if role == "combobox" else current_value(el)
        return bool(shown) and bool(_DIAL_SHOWN_RE.fullmatch(clean(shown)))
    except Exception:  # noqa: BLE001
        return False


_TWO_DIGIT_DIAL = frozenset("20 27 30 31 32 33 34 36 39 40 41 43 44 45 46 47 48 49 51 52 53 54 55 56 57 58 "
                            "60 61 62 63 64 65 66 81 82 84 86 90 91 92 93 94 95 98".split())


def dial_in(text: str) -> str:
    """The dial code written into `text` ("Bangladesh (+880)" -> "880", "+ 880" -> "880"); '' when none.

    One place knows how a form writes a dial code, because three of them used to guess at it separately.
    """
    m = re.search(r"\+\s*(\d{1,4})", text or "")
    if not m:
        return ""
    code = m.group(1)
    # Only a code some country really has. Employment Hero's Country box was left showing "+8" (the start of a
    # number typed at it), read as a dial picker already on our +880, and the phone went out as "801771614053"
    # (application 413). No country's code is 8, 2 or 3 alone; a one- or two-digit code must be a real one.
    if len(code) == 1 and code not in "17":
        return ""
    if len(code) == 2 and code not in _TWO_DIGIT_DIAL:
        return ""
    return code


def dial_code_on_page(page: Any) -> str:
    """The country dial code a separate control on this form already holds ("Bangladesh (+880)" -> "880").

    Only Workday-style forms split the number in two. When they do, the international number facts.yaml
    carries is not a valid answer for the Phone Number box beside it: Workday rejects "+8801XXXXXXXXX" with
    "Enter a valid format for Phone Number" and stops the step.

    A <select> is read through `selectedOptions`, not `innerText`: the text of a <select> element is the
    text of every option inside it, so reading it whole reports the first country in the list rather than
    the chosen one — a bare "+1" from Afghanistan-to-Zimbabwe order, whatever the form is actually holding.
    """
    try:
        held = page.evaluate("() => {" + DEEP_JS + """
            const sel = %r;
            for (const el of deepAll(sel)) {
                // innerText of a hidden node is its whole textContent, so a picker's closed country list
                // (intl-tel-input keeps every row in the DOM, each with a .iti__dial-code span) read as
                // holding its first row, Afghanistan's +93 (application 182, Veeam). A <select> is exempt:
                // a native one is often hidden behind the widget that drives it and still holds the value.
                if (el.tagName !== 'SELECT' && !(el.getClientRects().length
                        && getComputedStyle(el).visibility !== 'hidden')) continue;
                const shown = el.tagName === 'SELECT'
                    ? ((el.selectedOptions && el.selectedOptions[0]) ? el.selectedOptions[0].textContent : '')
                    : (el.innerText || el.value || '');
                const m = (shown || '').match(/\\+\\s*(\\d{1,4})/);
                if (m) return m[1];
            }
            return ''; }""" % _DIAL_SHOWN_SEL) or ""
        if held:
            return held
        # A div-built picker showing its code on its face ("+65" on Shopee's). Only the pickers that stand
        # beside a phone box are read, so a dropdown that happens to show "+1" for some other reason is not.
        for w in custom_widgets(page):
            code = dial_in(w.get("value") or "")
            if code and is_dial_control(widget(page, w["id"])):
                return code
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _dial_from_options(options: list[str], digits: str) -> tuple[str, str]:
    """The longest dial code among `options` that begins `digits`, and the option text carrying it.

    Longest wins because the codes nest: a list offering both +88 and +880 must send a Bangladeshi number
    as +880, and one offering +1 and +1246 must not send a Barbadian number as +1. Taking the first match
    would pick whichever happened to come first in the list, which is alphabetical by country.

    This is why jobbot carries no dial-code table. The split is only ever needed on a form that keeps the
    code in a control of its own, and such a control already enumerates every code it will accept — so the
    page is a better authority than any list shipped in here, and a code the form does not offer can never
    be chosen.
    """
    best, best_text = "", ""
    for text in options:
        code = dial_in(text)
        if code and digits.startswith(code) and len(code) > len(best):
            best, best_text = code, text
    return best, best_text


def _dial_rows(page: Any, combo: Any) -> Any:
    """Open `combo`'s list and return a locator over its rows, scoped to the popup it owns.

    Scoped through aria-controls, because a page-wide sweep for rows picks up whatever else is drawing a
    list: on Oracle's first step it returned the Title choices — "Doctor", "Miss", "Mr." — while the
    country list sat open and unread two elements away.
    """
    try:
        combo.click(timeout=SHORT)
        page.wait_for_timeout(500)
        owned = combo.evaluate("e => e.getAttribute('aria-controls') || ''")
        if owned:
            rows = page.locator(f"#{owned} > *, #{owned} [role=option], #{owned} [role=row], "
                                f"#{owned} li, #{owned} [class*='item' i]")
            if rows.count():
                return rows
    except Exception:  # noqa: BLE001
        pass
    return page.locator(DIAL_ROW_SEL)


def _clear_combo(page: Any, combo: Any) -> None:
    """Empty a combobox that is holding a value, whatever it is built out of.

    `fill("")` is not enough on a widget that owns its own input: Oracle's country-code box kept "+971" and
    the search text landed on the end of it, so the list was filtered for "+971Bangladesh" and matched
    nothing (application 139). Three ways, cheapest first — the widget's own clear button, then select-all
    and Delete on the focused input, then fill.
    """
    try:
        reset = combo.locator(
            "xpath=./ancestor::*[self::div or self::span][position()<=3]"
            "//button[contains(translate(@aria-label,'RCV','rcv'),'remove value')"
            " or contains(translate(@aria-label,'CLR','clr'),'clear')]").first
        if is_visible_now(reset):
            reset.click(timeout=SHORT)
            page.wait_for_timeout(200)
            if not current_value(combo):
                return
    except Exception:  # noqa: BLE001
        pass
    for keys in ("Control+a", "Meta+a"):
        try:
            combo.click(timeout=SHORT)
            combo.press(keys, timeout=SHORT)
            combo.press("Delete", timeout=SHORT)
            if not current_value(combo):
                return
        except Exception:  # noqa: BLE001
            pass
    try:
        combo.fill("", timeout=SHORT)
    except Exception:  # noqa: BLE001
        pass


def _click_dial_option(page: Any, combo: Any, code: str) -> bool:
    """Type "+<code>" into an open picker and click the row that comes back holding exactly that code.

    Typing is what narrows a 240-row list, and it is the only way through a virtualised one that renders
    twenty rows at a time. It is a second pass rather than the first, because a picker that filters on the
    country's *name* shows nothing at all for "+880" — the caller reads the unfiltered list first and only
    probes when that found nothing.
    """
    # Both spellings: a list searching its labels finds "+880" in "Bangladesh (+880)", but one whose
    # labels read "Bangladesh 880" answers only to the bare digits.
    for typed in ("+" + code, code):
        try:
            combo.click(timeout=SHORT)
            page.wait_for_timeout(300)
            _clear_combo(page, combo)
            page.keyboard.type(typed, delay=20)
            page.wait_for_timeout(600)
            rows = page.locator(DIAL_ROW_SEL)
            hit = next((i for i in range(min(rows.count(), DIAL_OPTIONS))
                        if dial_in(option_text(rows.nth(i))) == code), None)
            if hit is not None:
                rows.nth(hit).click(timeout=SHORT)
                page.wait_for_timeout(400)
                return True
            page.keyboard.press("Escape")
        except Exception as e:  # noqa: BLE001
            log.debug("_click_dial_option(%s) failed: %s", typed, e)
    return False


def _click_dial_by_name(page: Any, combo: Any, country: str, digits: str) -> str:
    """Open the picker, type the country's name, and click the row naming it whose code begins our number.
    The code taken, or ''."""
    name = clean(country).lower()
    if not name:
        return ""
    for typed in (country, country[:4]):
        try:
            combo.click(timeout=SHORT)
            page.wait_for_timeout(400)
            _clear_combo(page, combo)
            page.keyboard.type(typed, delay=60)
            page.wait_for_timeout(800)
            rows = page.locator(DIAL_ROW_SEL)
            for i in range(min(rows.count(), 400)):
                text = option_text(rows.nth(i))
                code = dial_in(text)
                if code and digits.startswith(code) and name in text.lower():
                    rows.nth(i).scroll_into_view_if_needed(timeout=SHORT)
                    rows.nth(i).click(timeout=SHORT)
                    page.wait_for_timeout(400)
                    return code
            page.keyboard.press("Escape")
        except Exception as e:  # noqa: BLE001
            log.debug("_click_dial_by_name(%s) failed: %s", typed, e)
    return ""


def set_dial_code(page: Any, phone: str, country: str = "") -> str:
    """Point the form's own country-code control at our number's country; the code it then holds, '' if not.

    Read the control's list, take the longest code that begins our digits, choose it, and the number box
    beside it gets the rest. Nothing here knows that +880 is Bangladesh, and nothing needs to.
    """
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return ""
    try:
        controls = page.locator(DIAL_CONTROL_SEL)
        count = min(controls.count(), 40)
    except Exception:  # noqa: BLE001
        return ""
    log.debug("set_dial_code: %d candidate controls for +%s", count, digits[:4])
    for i in range(count):
        el = controls.nth(i)
        try:
            if not is_visible_now(el) or is_prompt_control(el):
                continue    # a Workday prompt belongs to workday._prompts, which drives it its own way
            tag = (el.evaluate("e => e.tagName") or "").upper()
            if tag == "SELECT":
                options = select_options(el)
                # A <select> qualifies on its options alone, with no click: a country-of-residence select
                # lists names and no dial codes, so it never qualifies. That is the discriminator, rather
                # than a guess from the control's name — clicking every combobox on a form to find out what
                # it is would be worse than the bug being fixed.
                if sum(1 for o in options if dial_in(o)) < 3:
                    continue
                log.info("set_dial_code: <select> with %d options carrying a dial code", 
                         sum(1 for o in options if dial_in(o)))
                code, label = _dial_from_options(options, digits)
                if not code or not choose_select(el, label, options, force=True):
                    continue
            else:
                # Anything else is only driven when it is already showing a dial code — the very signal
                # dial_code_on_page reads — or was matched by a selector that names it.
                shown = current_value(el) or clean(el.inner_text() or "")
                if not dial_in(shown):
                    continue
                log.info("set_dial_code: driving a %s showing %r", tag.lower(), shown[:40])
                # Read the list as it opens and click the row, rather than searching it. These pickers
                # filter on the country's NAME: typing "+880" into Oracle's box returned "No results were
                # found." while the very row we wanted sat in the unfiltered list (application 141). The
                # list is the authority; the search box is not.
                rows = _dial_rows(page, el)
                texts = [option_text(rows.nth(i)) for i in range(min(rows.count(), DIAL_OPTIONS))]
                code, _ = _dial_from_options(texts, digits)
                if code:
                    hit = next(i for i, t in enumerate(texts) if dial_in(t) == code)
                    rows.nth(hit).click(timeout=SHORT)
                    page.wait_for_timeout(400)
                else:
                    # Virtualised: too few rows rendered to hold the answer. Typing is the only way to
                    # reach the rest, and a list that filters by code will answer it.
                    code = next((c for c in (digits[:4], digits[:3], digits[:2], digits[:1])
                                 if c and _click_dial_option(page, el, c)), "")
                if not code and country:
                    # A listbox that answers typing by jumping to the country NAME (Workday's "Country Phone
                    # Code" button: "+880" and "880" find nothing, "Bangladesh" scrolls to its row). It stayed
                    # on "United States of America (+1)" and the form refused the number (application 341).
                    code = _click_dial_by_name(page, el, country, digits)
                if not code:
                    continue
            shown = dial_code_on_page(page)
            if shown == code:
                log.info("country-code control set to +%s for the number in facts.yaml", code)
                return code
            # Never trust the click over the form: reporting a code the control is not holding is how this
            # would fail silently again, one cheerful log line instead of three identical ones.
            log.info("country-code control did not take +%s (it shows +%s) — sending the number whole",
                     code, shown or "?")
        except Exception as e:  # noqa: BLE001
            log.debug("set_dial_code on control %d failed: %s", i, e)
    # Pickers built out of divs, which no selector above names: told by where they stand (is_dial_control).
    for w in custom_widgets(page):
        root = widget(page, w["id"])
        try:
            if w.get("kind") == "date" or not is_dial_control(root):
                continue
            code = _set_dial_widget(page, root, digits, country)
            if code:
                log.info("country-code picker (div-built) set to +%s for the number in facts.yaml", code)
                return code
        except Exception as e:  # noqa: BLE001
            log.debug("set_dial_code on widget %s failed: %s", w.get("id"), e)
    return ""


def _set_dial_widget(page: Any, root: Any, digits: str, country: str = "") -> str:
    """Point a div-built country-code picker at our number's country. The code it then shows, '' if not.

    The list as it opens first (it may be the whole list), then its own search box with the code and with
    the country's name: Shopee renders twelve of 247 rows until something is typed (application 274).
    """
    if not open_widget(page, root):
        return ""
    try:
        items, texts = popup_options(page)
        code, label = _dial_from_options(texts, digits)
        if code:
            _click_row(items, texts, texts.index(label), page)
        elif widget_filter(page, root) is not None:
            queries = [f"+{digits[:n]}" for n in (4, 3, 2, 1) if digits[:n]] + [digits[:4], digits[:3]]
            if country and not is_dial_code(country):
                queries.append(country)
            for q in queries:
                if not widget_type(page, root, q):
                    break
                items, texts = popup_options(page)
                code, label = _dial_from_options(texts, digits)
                if code:
                    _click_row(items, texts, texts.index(label), page)
                    break
        if not code:
            return ""
        page.wait_for_timeout(300)
        shown = dial_in(widget_value(root))
        if shown != code:
            log.info("country-code picker did not take +%s (it shows +%s)", code, shown or "?")
            return ""
        return code
    finally:
        close_popups(page, root)


def dial_code_for_phone(page: Any, phone: str, country: str = "") -> str:
    """The dial code this form is holding separately for our number, after switching its control to ours.

    '' means "this form keeps no code of its own" — the ordinary case, and then the number goes in whole.
    A code that is NOT ours means the control would not take the change and is still resting on the
    tenant's own default; the caller sends the number whole and says so. Returning '' for that case too
    would throw away the one fact worth reporting, which is how application 136 refilled a box beside a
    picker stuck on +971 without a word about the picker.

    Safe to call from any adapter: a form with no such control is found out by reading, before anything is
    clicked.
    """
    held = dial_code_on_page(page)
    if not held:
        # Nothing shows a code yet. That is the ordinary single-box form -- and also a picker still on its
        # "Select" prompt (Shopee, application 274), which set_dial_code tells apart from an ordinary list
        # by its options and by where it stands, and drives; anything else it leaves alone. An empty picker
        # that names itself a country code (Oracle's, application 314) is driven by country after that.
        return set_dial_code(page, phone, country) or select_dial_country(page, country, phone) or ""
    if re.sub(r"\D", "", phone or "").startswith(held):
        return held     # already resting on our country — the Bangladeshi tenant of a Bangladeshi employer
    return set_dial_code(page, phone, country) or select_dial_country(page, country, phone) or held


def select_dial_country(page: Any, country: str, phone: str = "") -> str:
    """Switch a form's separate country-code picker to `country`, returning the dial code it then shows.

    SmartRecruiters (and the intl-tel-input widget many career sites use) defaults the picker to the job's
    country: a Montreal posting shows "+1", and a Bangladeshi number typed beside it is rejected as invalid.
    Best effort: a picker this cannot drive is left as it is and '' is returned, and the caller sends the
    number in full.

    Runs after `set_dial_code`, and only for the lists that answer to a country's name rather than to its
    code. The guard on the first line is not defensive tidying: facts.yaml held `identity.country: "+880"`
    after a learn-back, and `\\b\\+880\\b` cannot match "(+880)" because neither neighbour of the "+" is a
    word character — so this searched all 240 rows for a pattern none of them could hold, pressed Escape,
    and returned "" on every pass of application 135.
    """
    if not country or is_dial_code(country):
        return ""
    want = re.sub(r"[^\d]", "", phone or "")[:4]
    try:
        # The control that is actually showing a dial code, not merely the first thing on the page that
        # matched the selector — on an Oracle step that is some unrelated <select> twenty fields up.
        candidates = page.locator(DIAL_CONTROL_SEL)
        picker = next((c for c in (candidates.nth(i) for i in range(min(candidates.count(), 40)))
                       if is_visible_now(c) and dial_in(current_value(c) or clean(c.inner_text() or ""))),
                      None)
        if picker is None:
            # An empty picker is still the picker: after one wrong pick Oracle's showed no code at all, and
            # every later pass skipped it while the form kept saying "Enter a valid number" (application 314).
            named = page.locator("[id*='country-code' i], [id*='countrycode' i], [aria-controls*='country-code' i], "
                                 "[role=combobox][aria-label*='country code' i], [role=combobox][aria-label*='dial' i]")
            picker = next((c for c in (named.nth(i) for i in range(min(named.count(), 10)))
                           if is_visible_now(c) and not current_value(c)), None)
        if picker is None:
            return ""
        picker.click(timeout=SHORT)
        page.wait_for_timeout(500)
        # Searchable pickers take typing; the plain list is scanned for the country's name
        try:
            _clear_combo(page, picker)      # or the name lands after the code already in there
            page.keyboard.type(country, delay=20)
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass
        rows = page.locator(DIAL_ROW_SEL)
        n = min(rows.count(), DIAL_OPTIONS)
        pattern = re.compile(rf"\b{re.escape(country)}\b", re.I)
        for i in range(n):
            row = rows.nth(i)
            text = option_text(row)
            if pattern.search(text) and (not want or ("+" + want[:3] in text.replace(" ", "")) or "+" not in text):
                row.click(timeout=SHORT)
                page.wait_for_timeout(400)
                dial = dial_code_on_page(page)
                log.info("dial-code picker switched to %r -> +%s", country, dial or "?")
                if dial:
                    return dial
                break
        page.keyboard.press("Escape")
        # Searched by the code instead: Oracle's rows read "+880 (Bangladesh)", and the pick by name left
        # its box showing no code at all, so Nationwide refused the number (application 314).
        for k in (3, 2, 1):
            code = want[:k]
            if not code:
                continue
            picker.click(timeout=SHORT)
            page.wait_for_timeout(400)
            _clear_combo(page, picker)
            page.keyboard.type("+" + code, delay=30)
            page.wait_for_timeout(700)
            rows = page.locator(DIAL_ROW_SEL)
            for i in range(min(rows.count(), DIAL_OPTIONS)):
                text = option_text(rows.nth(i)).replace(" ", "")
                if text.startswith("+" + code) and not text[len(code) + 1:len(code) + 2].isdigit() and pattern.search(option_text(rows.nth(i))):
                    rows.nth(i).click(timeout=SHORT)
                    page.wait_for_timeout(400)
                    dial = dial_code_on_page(page)
                    log.info("dial-code picker: typed +%s, took %r -> +%s", code, option_text(rows.nth(i)) if rows.count() > i else "", dial or "?")
                    if dial:
                        return dial
            page.keyboard.press("Escape")
    except Exception as e:  # noqa: BLE001
        log.debug("select_dial_country failed: %s", e)
    return ""


def dial_code_only(value: str) -> str:
    """The country prefix, when `value` is nothing but one ("+880", "🇧🇩 +880"); '' when a number is in there.

    intl-tel-input and every widget built like it seed the box with the dial code of whatever country their
    picker is resting on. The box then looks filled — it reads back as a value and `fill_if_empty` leaves
    it alone — while holding no number at all. No country's dial code is longer than four digits and no
    real phone number is that short, so the two never overlap.

    Worth the helper because the miss is silent and permanent: application 129 read "+880" out of Oracle's
    phone widget, `seen` took it for the candidate correcting themselves in the window and wrote it into
    facts.yaml, and every application after it filled a three-digit phone number — application 130
    (Cloudflare, Greenhouse) came back "Phone number is too short".
    """
    digits = re.sub(r"\D", "", value or "")
    return digits if digits and len(digits) <= 4 else ""


def is_dial_code(value: str) -> bool:
    """True when `value` says a dial code and nothing else: "+880", "🇧🇩 +880", "00880".

    Deliberately not `dial_code_only`, which exists for a phone box and answers on the digit count alone.
    `dial_code_only("Dhaka 1207")` is "1207" — the right answer for "is this box holding a number or just a
    prefix", and the wrong one for "is this a country". Guarding identity.country and identity.location with
    the digit-count test would throw away every real postcode a candidate ever typed.

    A flag emoji is a Symbol rather than a letter, so "🇧🇩 +880" still qualifies while "Bangladesh (+880)"
    — a country naming its code, which is a country — does not.
    """
    text = (value or "").strip()
    if not text or re.search(r"[^\W\d_]", text):
        return False
    return bool(re.fullmatch(r"(?:\+|00)?\d{1,4}", re.sub(r"[^\d+]", "", text)))


def fill_phone(el: Any, value: str, *, page: Any = None, country: str = "") -> bool:
    """Write a phone number into a control that may already be holding a widget's country prefix.

    `fill_verified` cannot be used directly for a phone box. It refuses to touch a control that reads back
    non-empty, which is right for every other field and wrong for this one: an intl-tel-input box resting
    on "+880" reads as filled while holding no number, so the number is silently never typed and the form
    bounces on a three-character phone number. Every adapter that fills a phone box goes through here, so
    a board whose widget seeds its prefix is handled wherever it turns up rather than one vendor at a time.

    Pass `page` on a board with no control loop of its own and the number is split against whatever country
    code the form is holding separately, exactly as the generic walker does it. On the single-box forms
    those boards show today it costs one read and changes nothing, because `dial_code_for_phone` finds no
    such control and clicks nothing.
    """
    if not value:
        return False
    if page is not None:
        dial = dial_code_for_phone(page, value, country)
        if dial:
            value = national_phone(value, dial)
    if dial_code_only(current_value(el)):
        log.info("phone box holds only a dial code — writing %r over it", value)
        return fill_if_empty(el, value, clear=True)
    return fill_verified(el, value)


# ---------- suggestion lists (datalist autocompletes) ----------
def datalist_options(el: Any) -> list[str]:
    """What the <datalist> bound to this input is currently offering.

    Greenhouse writes its education boxes as `<input list="gh-edu-disciplines-...">` with a <datalist> that
    it refills from the server on every keystroke, and its submit refuses anything that is not one of those
    suggestions: "Please select a school, degree, and field of study from the suggestions." The box looks
    like a plain text field and typing the true answer into it is exactly what fails.
    """
    try:
        return [o for o in el.evaluate(
            """e => { const id = e.getAttribute('list'); if (!id) return [];
                      const dl = e.ownerDocument.getElementById(id);
                      return dl ? Array.from(dl.options).map(o => o.value || o.textContent) : []; }""") if o]
    except Exception:  # noqa: BLE001
        return []


def has_suggestions(el: Any) -> bool:
    """True for a control whose value has to come from a list it offers as you type."""
    try:
        return bool(el.evaluate("e => !!e.getAttribute('list')"))
    except Exception:  # noqa: BLE001
        return False


def _suggestion_queries(value: str, alternatives: tuple[str, ...] | list[str] = ()) -> list[str]:
    """What to type to make the list show the answer, most specific first.

    Only ever prefixes of the value, never an arbitrary substring, and that is the safety property: a
    search for "Engineering & Technology" offers "University of Engineering & Technology (UET) Lahore" —
    a different university in a different country — while every prefix of "Rajshahi University of ..."
    keeps the one distinctive word and so can only ever match the right institution or nothing.
    """
    out: list[str] = []
    for source in [value, *alternatives]:
        words = [w for w in (source or "").split() if w]
        for n in range(len(words), 0, -1):
            q = " ".join(words[:n]).strip(" ,&-")
            if q and q not in out:
                out.append(q)
    # Each part of a comma-separated answer on its own, broadest last: a list of countries answers
    # "Dhaka, Bangladesh" with "Bangladesh" (Shopee's Current Location, application 274).
    for part in _comma_parts(value):
        if part not in out:
            out.append(part)
    return out


def _best_suggestion(options: list[str], value: str) -> str:
    """The offered option that best answers `value`: exact, then either containing the other, then tokens."""
    from jobbot.answers import normalize_question     # imported here, as same_option does, to avoid a cycle
    want = normalize_question(value)
    for o in options:
        if normalize_question(o) == want:
            return o
    for o in options:
        n = normalize_question(o)
        if n and (n.startswith(want) or want.startswith(n)):
            return o
    # A fuzzy match has to keep the word that makes the answer this answer. Scored on tokens alone,
    # "Rajshahi University of Engineering & Technology (RUET)" comes out 84% the same as "University of
    # Engineering & Technology (UET) Lahore" — a different university, in a different country, and the one
    # word that distinguishes them is the one token_set_ratio is happiest to drop.
    head = next((w for w in want.split() if len(w) > 3), "")
    try:
        from rapidfuzz import fuzz
        # token_set_ratio scores every superset of the answer's words 100, so "Dhaka Chandragati, Rajshahi
        # Division, Bangladesh" tied "Dhaka, Dhaka Division, Bangladesh" for "Dhaka, Bangladesh" and won by
        # coming first (application 213, Meta). Ties go to the option that begins the way the answer does
        # (its first comma part, "Dhaka", exactly), then to the one with the fewest extra words.
        lead = normalize_question(value.split(",")[0])
        best, score = "", (0, 0, 0)
        for o in options:
            n = normalize_question(o)
            if head and head not in n:
                continue
            sc = (fuzz.token_set_ratio(n, want), int(normalize_question(o.split(",")[0]) == lead),
                  fuzz.token_sort_ratio(n, want))
            if sc > score:
                best, score = o, sc
        if score[0] >= 80:
            return best
    except Exception:  # noqa: BLE001
        pass
    return ""


# "Other", "Others", "Other (please specify)", "Others / Not Applicable" — a list's own escape hatch, whatever
# it hangs off the word.
_OTHER_RE = re.compile(r"^(?:other|others|not listed|n/?a|not applicable|none of the above)(?:\s*[/(,:-].*)?$", re.I)


def _commit_suggestion(page: Any, el: Any, pick: str) -> bool:
    """Put `pick` in the box as real keystrokes, and confirm the form stopped objecting to it.

    Two things matter and both were learned the hard way on C3 AI's Greenhouse form (applications 147-150):

    - It has to be TYPED. `fill()` sets the value in one assignment, and Greenhouse left its "Select a
      match from the suggestions, or this entry won't be included" line showing over a box that read
      "Computer Science" — correct text, not accepted. Keystrokes clear it. Dispatching input/change by
      hand does not: React ignores an event whose value its own tracker already holds, and this widget
      wants the trusted events a real keyboard produces.
    - It has to be EXACT. There is no picking a suggestion to finish the word off — ArrowDown and Enter do
      nothing to a native <datalist>, which Chrome draws outside the page where no synthetic key reaches
      it. Typing a few characters short and hoping the list completes them leaves the box holding a stem,
      and `same_value` is lenient enough to call "Bachelor's Degr" a match for "Bachelor's Degree", which
      is why that read as a success for two runs while the form kept refusing it.
    """
    try:
        el.click(timeout=SHORT)
        try:
            el.fill("", timeout=SHORT)
        except Exception:  # noqa: BLE001
            pass
        el.press_sequentially(pick, delay=30, timeout=MEDIUM)
        page.wait_for_timeout(900)          # the list is refetched on every keystroke
        el.evaluate("e => e.dispatchEvent(new Event('blur', {bubbles: true}))")
        page.wait_for_timeout(300)
        if clean(current_value(el)) == clean(pick):
            return True
        # Typing did not survive — this widget re-renders its row, and the keystrokes land on the node that
        # was replaced. Assigning the value still puts the right text in the box, which is worth doing even
        # when the form will not count it as chosen: what the box holds is then the answer this board does
        # accept ("Computer Science") rather than the one it rejects ("Computer Science & Engineering").
        el.fill(pick, timeout=SHORT)
        page.wait_for_timeout(250)
        # Compared exactly, not through same_value: a box holding most of the answer is the failure this
        # is here to catch, and same_value would wave it through.
        return clean(current_value(el)) == clean(pick)
    except Exception as e:  # noqa: BLE001
        log.debug("_commit_suggestion(%r): %s", pick, e)
        return False


def fill_from_suggestions(page: Any, el: Any, value: str,
                          alternatives: tuple[str, ...] | list[str] = ()) -> bool:
    """Type `value` and commit whichever suggestion the form is willing to accept for it.

    The form's own list is the authority, the same way it is for a phone number's country code. facts.yaml
    says "Computer Science & Engineering" and Greenhouse's discipline list has no such entry — it offers
    "Computer Science" — so the truth has to be mapped onto what this employer will take rather than typed
    at it and refused (application 145, C3 AI).

    `alternatives` are the near-misses facts.yaml has said are acceptable. When neither the value nor any
    of them is on the list, an "Other" the list offers is better than free text the submit will reject.
    """
    if not value:
        return False
    for query in _suggestion_queries(value, alternatives):
        try:
            el.fill(query, timeout=SHORT)
            page.wait_for_timeout(700)
            options = datalist_options(el)
            if not options:
                continue
            pick = _best_suggestion(options, value)
            if pick:
                took = _commit_suggestion(page, el, pick)
                log.info("suggestion list: typed %r, took %r for %r%s",
                         query, pick, value, "" if took else " (the box did not keep it)")
                if took:
                    return True
        except Exception as e:  # noqa: BLE001
            log.debug("fill_from_suggestions(%r): %s", query, e)
    # Nothing on the list is this candidate's answer. Say so out loud — an application that reads "OTHER"
    # for a real university is a compromise, not a success, and the log is where that is visible.
    for query in ("Other", "OTHER", "Not listed"):
        try:
            el.fill(query, timeout=SHORT)
            page.wait_for_timeout(600)
            pick = next((o for o in datalist_options(el) if _OTHER_RE.match(clean(o))), "")
            if pick and _commit_suggestion(page, el, pick):
                log.info("suggestion list: %r is not on this board's list — falling back to %r",
                         value, pick)
                return True
        except Exception:  # noqa: BLE001
            pass
    log.warning("suggestion list: nothing offered for %r and no 'Other' either; leaving it as typed", value)
    try:
        el.fill(value, timeout=SHORT)
    except Exception:  # noqa: BLE001
        pass
    return False


def same_phone(a: str, b: str) -> bool:
    """True when two phone strings are the same number written differently.

    A form that holds the dial code in its own control shows only the national part, and so does an ATS
    profile that stored the two apart — SuccessFactors' portal shows "1771614053" for a number saved as
    "+8801771614053". Neither is the candidate correcting anything, but both differ from the fact as plain
    strings, which is how the country code was stripped out of facts.yaml twice in one evening: the walker
    read the shorter value as a hand-made correction and learned it back over the real number.
    """
    x, y = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    if not x or not y:
        return False
    return x == y or (len(x) >= 6 and y.endswith(x)) or (len(y) >= 6 and x.endswith(y))


def national_phone(phone: str, dial: str) -> str:
    """`phone` without the dial code the form is holding separately. Unchanged when it does not start with
    it — a number we cannot split confidently is better sent whole than silently truncated."""
    digits = re.sub(r"[^\d+]", "", phone or "").lstrip("+")
    if not dial or not digits.startswith(dial):
        return phone
    rest = digits[len(dial):]
    return rest or phone


# ---------- vendor prompt widgets ----------
def is_prompt_control(el: Any) -> bool:
    """True for a Workday "prompt": a search box whose value can only be chosen from its popup tree.

    It carries no role and looks exactly like a text input, so a label-driven walker types the answer into
    it — which leaves the field looking filled and the application holding nothing. workday.py drives these;
    every other walker skips them.
    """
    try:
        return bool(el.evaluate(
            """e => !!(e.closest("[data-automation-id='multiselectInputContainer']")
                || e.closest("[data-automation-id='multiSelectContainer']")
                || (e.getAttribute('data-uxi-element-id') || '').startsWith('selectinput'))"""))
    except Exception:  # noqa: BLE001
        return False


# ---------- honeypots ----------
# Fields planted to catch anything that fills a form by label. Workday's says, in as many words, "Enter
# website. This input is for robots only, do not enter if you're human" — and the generic walker's website
# rule would have filled it in with the portfolio URL, throwing away an otherwise complete application.
_HONEYPOT_RE = re.compile(
    r"robots?\s+only|do\s+not\s+enter\s+if\s+you'?re\s+human|only\s+(?:enter|fill).{0,20}if\s+you'?re\s+a\s+robot"
    r"|leave\s+(?:this|it)\s+(?:field\s+)?(?:blank|empty)"
    # Oracle Recruiting ships one on every "apply with your email" step and labels it in as many words:
    # <input id="honey-pot-1" aria-hidden="true"> beside <label>honeypot</label>.
    r"|honey\s*-?\s*pot|bot[\s-]?(?:field|trap)", re.I)


_CHAT_WIDGET_JS = r"""e => {
    // A recruiting chatbot or live-chat panel docked beside the form. Its reply box is a textarea like any
    // other, and a walker scoped to <body> answered it: Presight's Oracle page got the candidate summary
    // posted into its "Presight Careers" assistant twice (application 221) -- a message sent on the
    // candidate's behalf that nobody asked for.
    const ph = (e.getAttribute('placeholder') || e.getAttribute('aria-label') || '').toLowerCase();
    if (/\b(?:write|type|send|enter)\s+(?:a|your)\s+(?:reply|message)\b|\bask (?:me|a question|anything)\b|^message\b/.test(ph))
        return true;
    for (let n = e.parentElement, i = 0; n && n !== document.body && i < 12; n = n.parentElement, i++) {
        const tag = (n.className && n.className.toString ? n.className.toString() : '') + ' ' + (n.id || '') + ' '
                  + (n.getAttribute('aria-label') || '') + ' ' + n.tagName;
        if (/\b(?:chat|chatbot|livechat|messenger|intercom|drift|zendesk|webchat|conversation|assistant-panel)\b|chat-?(?:widget|window|panel|container|box)/i.test(tag))
            return true;
    }
    return false;
}"""


def in_chat_widget(el: Any) -> bool:
    """True for a control inside a chat panel beside the form -- never an application question."""
    try:
        return bool(el.evaluate(_CHAT_WIDGET_JS))
    except Exception:  # noqa: BLE001
        return False


def is_honeypot(el: Any, label: str = "") -> bool:
    """True for a control no human would fill, whatever its label says it wants."""
    if label and _HONEYPOT_RE.search(label):
        return True
    try:
        if (el.get_attribute("aria-hidden") or "").lower() == "true":
            return True
        # A tick box the page draws with a styled label measures 0x0 too, and the rule below would bin it
        # as a trap. Its visible label is what tells the two apart -- see drawn_by_label.
        if drawn_by_label(el):
            return False
        box = el.bounding_box()
        if box and (box.get("width", 0) <= 1 or box.get("height", 0) <= 1):
            return True     # the one-pixel input trick
    except Exception:  # noqa: BLE001 - a control we cannot measure is judged on its label alone
        pass
    return bool(_HONEYPOT_RE.search(label_context(el)))


# ---------- account credentials ----------
# A password box is not a screening question. It belongs to a signup the run has to get through, and the one
# password jobbot uses for those lives in jobbot.credentials — so these are filled, never asked about.
_PASSWORD_LABEL_RE = re.compile(r"pass\s*word|pass\s*phrase|pass\s*code", re.I)
# ...except the kind that is a question: a one-time code mailed or texted to the candidate.
# "Confirmation" only as a code: "Password confirmation" is the retyped password, and taking it for a one-time
# code left Macquarie's signup confirmation to be filled with something else (application 488).
_ONE_TIME_PASSWORD_RE = re.compile(r"one[\s-]*time|verification|security|confirmation\s*(?:code|number|pin)\b"
                                   r"|\botp\b|2fa", re.I)


def is_password_control(el: Any, label: str = "") -> bool:
    """True for an account password box (including 'Verify New Password'), False for a one-time code."""
    if label and _ONE_TIME_PASSWORD_RE.search(label):
        return False
    try:
        if (el.get_attribute("type") or "").lower() == "password":
            return True
    except Exception:
        return False
    return bool(label and _PASSWORD_LABEL_RE.search(label))


# What a form says the longest password it takes is. Read from the rules it prints, never from the markup:
# SuccessFactors puts "Password must not be longer than 18 characters" beside a box whose maxlength attribute
# says 99, so a cap taken from the attribute would have let the 20-character managed password straight
# through to be refused — and the refusal names a rule that was on the screen the whole time.
_PASSWORD_WORD_RE = re.compile(r"pass\s?word|pass\s?phrase", re.I)
_PASSWORD_MAX_RES = (
    re.compile(r"(?:longer\s+than|exceeds?|at\s+most|maximum\s+(?:of\s+)?|max\.?\s+|up\s+to)\s*"
               r"(\d{1,3})\s*(?:characters?|chars?)", re.I),
    re.compile(r"(\d{1,3})\s*(?:characters?|chars?)\s*(?:or\s+(?:fewer|less)|max(?:imum)?)\b", re.I),
    re.compile(r"\b(?:between|from)\s+\d{1,3}\s*(?:and|to|[-\u2013])\s*(\d{1,3})\s*(?:characters?|chars?)", re.I),
)


def password_max_length(page: Any) -> int:
    """The longest password this form says it will take, 0 when it does not say.

    Matched a sentence at a time, and only in sentences that are about a password. The rules sit in a bullet
    list beside the box, next to other fields' own limits, and a page-wide search for "maximum 30 characters"
    finds whichever limit is printed first — which on a signup form is as likely to be the phone number's.
    """
    try:
        text = clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))
    except Exception:  # noqa: BLE001
        return 0
    best = 0
    for sentence in re.split(r"[.!?\u2022\n;]", text):
        if not _PASSWORD_WORD_RE.search(sentence):
            continue
        for rx in _PASSWORD_MAX_RES:
            m = rx.search(sentence)
            if not m:
                continue
            found = int(m.group(1))
            # A cap under 6 is a misread of some other number in the sentence, not a password rule any site
            # actually has; over 128 is a limit no managed password could ever run into.
            if 6 <= found <= 128:
                best = found if not best else min(best, found)
    if best:
        log.info("this form caps passwords at %d characters", best)
    return best


def named_button(page: Any, names: tuple[str, ...]):
    """The first visible, enabled control whose accessible name is exactly one of `names`, or None.

    Exact names only. A substring match on "Apply" finds "Apply with LinkedIn" and a substring match on
    "Sign in" finds "Sign in to save this job", and pressing either is a detour the run does not come back
    from. Buttons are looked at before links because a form's own sender is one.
    """
    for name in names:
        pattern = re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
        for finder in (lambda: page.get_by_role("button", name=pattern),
                       lambda: page.locator(f"input[type=submit][value='{name}' i], "
                                            f"input[type=button][value='{name}' i]"),
                       lambda: page.get_by_role("link", name=pattern)):
            try:
                loc = finder()
                for i in range(min(loc.count(), 4)):
                    el = loc.nth(i)
                    if is_visible_now(el) and el.is_enabled():
                        return el
            except Exception:  # noqa: BLE001 - one dead locator must not hide the rest
                continue
    return None


def fill_account_password(page: Any, password: str = "") -> bool:
    """Put jobbot's account password into every empty password box on the page.

    A signup form has two — the password and its confirmation — and they have to match, which is the whole
    reason this fills them together rather than treating each as its own field. Boxes that already hold
    something are left alone, so a re-run after a pause does not retype over a value the user entered.
    """
    try:
        boxes = page.locator("input[type=password]")
        count = boxes.count()
    except Exception as e:  # noqa: BLE001
        log.debug("password boxes not readable: %s", e)
        return False
    if not count:
        return False
    if not password:
        from jobbot import credentials
        password = credentials.account_password(password_max_length(page))
    filled = False
    for i in range(count):
        el = boxes.nth(i)
        try:
            if not is_visible_now(el) or current_value(el):
                continue
            if is_password_control(el, get_label_for(el)) and fill_verified(el, password):
                filled = True
        except Exception as e:  # noqa: BLE001
            log.debug("password box %d: %s", i, e)
    # A password and its confirmation must agree. Where one box already holds the managed password, a box
    # beside it holding anything else is not the user's to keep: Macquarie's "Password confirmation" was
    # taken for a one-time code, answered with an invented value cached from another signup, and left the
    # account about to be created with a confirmation matching nothing (application 488).
    try:
        visible = [boxes.nth(i) for i in range(count) if is_visible_now(boxes.nth(i))]
        values = [current_value(el) for el in visible]
        if password in values:
            for el, value in zip(visible, values):
                if value and value != password and is_password_control(el, get_label_for(el)):
                    log.info("a password box disagreed with the account password beside it; filling it again")
                    _clear_control(el)
                    filled = fill_verified(el, password) or filled
    except Exception as e:  # noqa: BLE001
        log.debug("password boxes re-check: %s", e)
    if filled:
        log.info("filled the account password from the keychain")
    return filled


_FIELD_OF_STUDY_RE = re.compile(r"field of (study|degree)|discipline|\bmajor\b|course of study"
                                r"|area of study|specialis|specializ", re.I)


def _answer_alternatives(ctx: ApplyContext, label: str) -> list[str]:
    """Near-miss answers facts.yaml has already said are acceptable when a board's list lacks the real one.

    Only the field of study has them today, and only because a degree subject is the one fact whose exact
    wording differs from board to board — "Computer Science & Engineering" on the certificate, "Computer
    Science" on Greenhouse's list. Naming the acceptable substitutes in facts.yaml keeps that decision the
    candidate's rather than a fuzzy match's.
    """
    if not _FIELD_OF_STUDY_RE.search(label or ""):
        return []
    alts = (ctx.facts.get("education") or {}).get("field_of_study_alternatives") or []
    return [str(a) for a in alts if str(a).strip()]


# Lists where the board's own "Other" is the expected answer when the real one is missing: a university
# Greenhouse has never heard of, a degree subject worded differently. Nowhere else — "Other" is not a stand-in
# for a pronoun, a salary band or a visa answer.
_OTHER_OK_LABEL_RE = re.compile(r"school|universit|institution|college|\bdegree\b|qualification", re.I)


def _other_ok(label: str) -> bool:
    return bool(_OTHER_OK_LABEL_RE.search(label or "") or _FIELD_OF_STUDY_RE.search(label or ""))


_JUNK_CHOICE_RE = re.compile(r"^[\s+\-().\d]+$")
_NUMERIC_LABEL_RE = re.compile(r"phone|mobile|code|dial|prefix|year|number|no\.|zip|post|age|salary|experience|"
                               r"how many|count|amount|rate|gpa|score|grade|month|day|hour|notice|\bnum", re.I)


def _choice_miss(el: Any, label: str, ans: str, options: list[str] | None, kind: str,
                 required_el: Any = None) -> None:
    """An answer the control would not take. Ask about it, or leave an optional field be — never fail.

    This used to be ApplyError, which ends the application as "failed" with nothing but Retry on its card:
    no question, nothing learned, and the same miss on the next form ("Could not choose 'Computer Science &
    Engineering' for 'Discipline'", seen 7 times, application 182). A question with the list's real options
    goes to the card instead; the pick is learned as the candidate's answer to this label and the run carries
    on in the same window. Modelled on the Workday list pick (workday.py), the one place that already did it.
    """
    try:
        held = clean(el.evaluate("e => e.tagName === 'SELECT' && e.selectedIndex >= 0 ? "
                                 "e.options[e.selectedIndex].textContent : ''") or "") or clean(current_value(el) or "")
        if held and ans and (same_option(held, ans) or held.lower() == str(ans).lower()):
            # The choice landed and the control re-rendered under the call that made it (GEI's MUI native
            # select showed "Bangladesh" while the run asked which country, application 443).
            log.info("%r already shows %r; not a miss", label[:60], held)
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        gone = el.count() == 0 or not el.first.is_visible()
    except Exception:  # noqa: BLE001 - a wrapper without count(): judged by is_required as before
        gone = False
    if gone:
        # The control went away while it was being answered: Employment Hero re-draws its profile editor, and
        # the stale "First Name" locator then read as a required list with no options (application 413).
        # A question about a control that is not there cannot be answered; the next pass re-reads the page.
        log.info("not asking about %r: its control is no longer on the page", label)
        return
    if not is_required(required_el if required_el is not None else el):
        log.info("leaving the optional %r blank: the form's list has nothing matching %r", label, ans)
        return
    try:
        # What the walker took for this question's list, kept in the log: a label pinned on the wrong control
        # ("First Name" on Employment Hero's Country dropdown, application 413) is only seen in the markup.
        log.info("choice miss on %r: control %s", label[:60], clean(el.evaluate("e => e.outerHTML") or "")[:400])
        (pathlib.Path(__file__).resolve().parents[2] / "data" / "screenshots" / "last-choice-miss.html").write_text(
            el.page.content())
    except Exception as e:  # noqa: BLE001
        log.info("choice miss on %r: could not read the control (%s: %s)", label[:60], type(el).__name__, e)
    # The card draws these as a <select>, so a country list goes whole: cut at 25, Sea's "United Arab
    # Emirates" was never on offer and the only way to answer was the browser window (application 300).
    offered = [o for o in (options or []) if clean(o) and not is_prompt_value(o)][:300]
    raise NeedsHuman(
        f"'{label}' has no option jobbot could match to {ans!r}"
        + (f" (it offers: {', '.join(offered[:8])})" if offered else "")
        + ". Pick the right one here, or choose it in the browser window and click Continue — "
          "it is remembered for next time.",
        question=label, options=offered or None, kind=kind)


_NOT_APPLICABLE = "N/A"


class _AlwaysRequired:
    """Stands in for a control the page has said is mandatory; is_required() reads it as required."""
    def evaluate(self, *_a, **_k):
        return True


def _refused_as_mandatory(ctx: ApplyContext, label: str) -> bool:
    """The page's last bounce named this field as mandatory (`The field "Zip/Postal Code" is mandatory.`)."""
    said = (ctx.extra or {}).get("refused_text") or ""
    key = re.sub(r"[\s.*:]+$", "", clean(label or "")).lower()[:60]
    return bool(said and key and len(key) >= 3 and key in said)


def _ask(ctx: ApplyContext, el: Any, label: str, options: list[str] | None, kind: str,
         required_el: Any = None):
    """The resolver's answer, or None when there is no answer and the form does not need one."""
    if _refused_as_mandatory(ctx, label):
        required_el = _AlwaysRequired()
    try:
        ans = ctx.answer(label, options, kind)
        if ans == "" and kind in ("text", "textarea") and is_required(required_el if required_el is not None else el):
            # The resolver leaves an "If yes, please describe…" box empty when the answer above it was No.
            # Some forms still star that box, and then refuse the whole submit over it (application 182,
            # Veeam). An empty required box is never accepted; "N/A" is the honest answer to it.
            log.info("%r does not apply but the form requires it; writing %r", label[:60], _NOT_APPLICABLE)
            return _NOT_APPLICABLE
        return ans
    except NeedsHuman:
        if is_required(required_el if required_el is not None else el):
            if options:
                # What the pause was asked about, for the log: a question offered with a list is a label
                # read off the page, and when that label is the wrong one (EY's create-account page
                # asked "Email Address:" with the options "Notification:" / "Hear more about career
                # opportunities", application 288) the markup is the only way to see why.
                try:
                    log.info("asking %r from a control: %s", label[:60], el.evaluate(
                        "e => { const p = e.parentElement, g = p && p.parentElement;"
                        " return ((g || p || e).outerHTML || '').replace(/\\s+/g, ' ').slice(0, 1200); }"))
                except Exception:  # noqa: BLE001
                    pass
            raise
        log.info("leaving the optional %r blank: nothing in facts.yaml or the CV answers it", label)
        return None


def is_required(el: Any) -> bool:
    """Whether the form insists on this control.

    It decides what a question jobbot cannot answer costs. A required field has to be asked about — the form
    will not go in without it. An optional one that nothing in facts.yaml or the CV answers (a postal code,
    which the resolver will never invent) is better left blank than turned into a pause: the application is
    complete without it, and stopping to ask makes the user finish a form they asked not to have to.

    Unknown counts as required: asking one question too many beats submitting a form with a hole in it.

    The star is read from wherever the label was found, not only from <label> elements: on a board whose
    labels are plain <span>s (Gem) the label search above came back empty and every required field read as
    optional, so a question jobbot could not answer was silently left blank on a form that then refused
    to submit.
    """
    try:
        verdict = el.evaluate("""e => {
            if (e.required || e.getAttribute('aria-required') === 'true') return true;
            if (e.getAttribute('aria-required') === 'false') return false;
            // A radio/checkbox group judged by its container: the `required` is on the inputs inside it
            // (Amazon's government-employment radios, application 414, read as optional and were left blank).
            if (!/^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName) && e.querySelector
                && e.querySelector('input[type=radio][required], input[type=checkbox][required], [role=radiogroup][aria-required=true], [role=radio][aria-required=true]'))
                return true;
            const star = t => /\*|\brequired\b|\bobligatoire\b|\bpflichtfeld\b|\bobligatorio\b/i.test(t || '');
            // the control's own label first: Greenhouse and most React boards put the * there, and the
            // nearest <div> around a react-select input is a wrapper with no label in it at all
            for (const l of (e.labels ? Array.from(e.labels) : [])) if (star(l.innerText)) return true;
            const by = e.getAttribute('aria-labelledby');
            const root = e.getRootNode();
            if (by) for (const id of by.split(/\s+/)) {
                const n = root.getElementById ? root.getElementById(id) : document.getElementById(id);
                if (n && star(n.innerText)) return true; }
            // A dropdown built out of divs is its own nearest <div>, and the label-ish things inside it
            // (a month picker's "October" header) are not its label: look outward from it, and only at
            // label elements it does not contain.
            const widget = e.hasAttribute('data-jobbot-widget');
            const from = widget ? e.parentElement : e;
            const wrap = from ? from.closest("[data-automation-id^='formField'], [class*='field-wrapper' i], [class*='form-field' i], .field, fieldset, li, div") : null;
            const shown = x => { const r = x.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            const foreign = l => { const w = l.closest('[data-jobbot-widget]'); return !!w && w !== e; };
            const lab = wrap ? Array.from(wrap.querySelectorAll('label, legend, [class*="label" i]'))
                .find(l => shown(l) && !foreign(l) && !(widget && e.contains(l))) || null : null;
            if (lab && star(lab.innerText)) return true;
            // A star on the group rather than on the box. Greenhouse marks its education block that way:
            // "Field of study" carries no mark of its own, so the box read as optional, the question the
            // resolver could not settle was skipped, and the submit was then refused over the empty box —
            // "Please select a school, degree, and field of study from the suggestions", seven times for
            // one C3 AI application. A group only speaks for its fields when it is small enough to be one
            // question, and only when the form is not marking fields individually: an unstarred box among
            // starred siblings really is optional.
            const grp = e.closest("fieldset, [role=group], [class*='question' i], [class*='section' i], [class*='education' i], [class*='group' i]");
            if (grp) {
                const ctrls = grp.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea");
                if (ctrls.length > 0 && ctrls.length <= 4) {
                    let individually = false;
                    for (const c of ctrls)
                        for (const l of (c.labels ? Array.from(c.labels) : []))
                            if (star(l.innerText)) individually = true;
                    if (!individually)
                        for (const h of grp.querySelectorAll("legend, h2, h3, h4, label, [class*='label' i], [class*='title' i]"))
                            if (star(h.innerText)) return true;
                }
            }
            if (lab) return false;
            return null;    // no label element anywhere near: judge on the text the label finder settles on
        }""")
    except Exception:  # noqa: BLE001
        return True
    if verdict is not None:
        return bool(verdict)
    return bool(_REQUIRED_MARK_RE.search(get_label_for(el)))


_REQUIRED_MARK_RE = re.compile(r"\*|\brequired\b|\bobligatoire\b|\bpflichtfeld\b|\bobligatorio\b", re.I)


# Workday refuses a free-text box that holds any of these, with "Contains illegal characters < > [ ] " { } \"
# and no hint of which box — OCBC's Work Experience step bounced on a Role Description carrying nothing worse
# than the straight quotes around a quoted sentence (application 295). The replacements keep the prose
# readable rather than deleting runs of it: a curly quote reads as a quote, a paren as a bracket.
_ILLEGAL_WORKDAY = {'"': "'", "<": "(", ">": ")", "[": "(", "]": ")", "{": "(", "}": ")", "\\": "/"}
_WORKDAY_HOST_RE = re.compile(r"myworkdayjobs\.com|workday\.com", re.I)


def scrub_illegal(page: Any, text: str) -> str:
    """Drop the characters this board will not accept in free text.

    Scoped to Workday by host rather than applied everywhere: the rule is Workday's own, and rewriting
    punctuation on a board that never objected to it would change answers for no reason.
    """
    if not text:
        return text
    try:
        if not _WORKDAY_HOST_RE.search(page.url or ""):
            return text
    except Exception:  # noqa: BLE001 - a frame with no url of its own
        return text
    out = "".join(_ILLEGAL_WORKDAY.get(ch, ch) for ch in str(text))
    if out != text:
        log.info("scrubbed characters Workday rejects from a %d-character answer", len(str(text)))
    return out


def answer_and_set(ctx: ApplyContext, el: Any, label: str, kind: str, options: list[str] | None = None,
                   container: Any | None = None) -> None:
    """Ask the resolver for `label` and write the answer into the control according to `kind`."""
    page = ctx.page
    if is_verification_control(el, label):
        log.debug("skipping verification control %r; it is filled after submit", label)
        return
    if kind == "date":
        # A div-built date / month picker (Shopee's "Course Period", application 274): asked like any other
        # question, and the answer clicked into its calendar rather than typed.
        existing = combobox_value(el)
        if existing:
            ctx.seen(existing, label, kind="text")
            return
        ans = _ask(ctx, el, label, None, "text")
        if ans is None:
            return
        if fill_date_widget(page, el, ans):
            log.info("date picker %r -> %r", label[:60], ans)
            return
        if is_required(el):
            raise NeedsHuman(f"'{label}' is a date picker jobbot could not set to {ans!r}. Pick it in the "
                             "browser window, then click Continue.", question=label, kind="text")
        log.info("leaving the optional date %r: the picker would not take %r", label[:60], ans)
        return
    if kind == "text" or kind == "textarea":
        existing = current_value(el)
        if existing and looks_like_filename(existing) and not re.search(r"file|resume|résumé|\bcv\b|attach", label, re.I):
            # A file name in a box that asks a question is left over from a misread label, not an answer.
            # Kept, `seen` would learn it as the candidate's reply to this question.
            log.info("clearing %r out of %r: a file name is not an answer to it", existing[:60], label[:60])
            try:
                el.fill("", timeout=SHORT)
            except Exception as e:  # noqa: BLE001
                log.debug("could not clear %r: %s", label[:40], e)
            existing = current_value(el)
        if existing and kind == "text" and _TODAY_DATE_RE.fullmatch(normalize_label(label)):
            # A signing date is today's, whatever an earlier run or the model left in the box: ServiceNow went
            # out dated 2026-02-01 on 2026-09-29 because the box already held it (application 222).
            from datetime import date
            today = date.today().isoformat()
            if existing.strip() != today:
                log.info("%r holds %r; writing today's date %s", label[:40], existing[:20], today)
                fill_if_empty(el, today, clear=True)
                return
        if existing and kind == "text" and asks_for_figure(ctx, label) and not _PLAIN_NUMBER_RE.match(existing):
            # "10+" in a box that asks "how many": LinkedIn answers it with "Invalid input" and nothing else,
            # and every retry read "10+" back as the answer already given (application 178).
            from jobbot.apply.resolver import as_number
            num = as_number(existing, label)
            if num is None:
                # "Negotiable" names no figure, so what is in the box is what the step keeps bouncing on.
                # Asked as a number the resolver ignores that prose and asks the user once for the figure.
                num = _ask(ctx, el, label, None, "number")
                if num is None:
                    return
            log.info("%r asks for a figure; rewriting %r as %r", label[:60], existing[:30], num)
            fill_if_empty(el, num, clear=True)
            return
        if existing and kind == "text" and _GPA_LABEL_RE.search(label):
            shaped = shape_gpa(ctx, el, existing)
            if shaped != existing:
                log.info("%r holds %r; the box's own example wants %r", label[:40], existing[:20], shaped)
                fill_if_empty(el, shaped, clear=True)
                return
        if existing and (cleaned := scrub_illegal(page, existing)) != existing:
            # Already in the box from an earlier pass, or written there by the board's own CV parse. The
            # branches above correct a value the form will not accept; this is one more of them, and it has
            # to run before the `seen` below or the step keeps bouncing on prose nothing will rewrite.
            log.info("%r holds characters Workday rejects; rewriting it", label[:40])
            fill_if_empty(el, cleaned, clear=True)
            return
        if existing:
            ctx.seen(existing, label, kind=kind, default=text_is_default(el) or _unchanged_identity(ctx, label, existing))
            return
        if kind == "textarea" and COVER_LABEL_RE.search(label or ""):
            letter = cover_letter_text(ctx)
            if letter:
                fill_if_empty(el, scrub_illegal(page, letter))
                return
        if kind == "text" and (is_number_box(el) or asks_for_figure(ctx, label)):
            # The control decides the shape of the answer: "Negotiable" is a fine answer to a salary box and
            # no answer at all to a salary *number* box. The resolver turns what it knows into a number where
            # it honestly can ("None" notice period -> 0 weeks) and asks the user where it cannot.
            kind = "number"
            clear_bad_input(el)     # a text answer from an earlier pass, still blocking the submit
        ans = _ask(ctx, el, label, None, kind)
        if ans is None:
            return
        if ans == "" and kind in ("text", "textarea") and current_value(el):
            from jobbot.apply.resolver import _CONDITIONAL_FOLLOWUP_RE, normalize_question as _nq
            if _CONDITIONAL_FOLLOWUP_RE.match(_nq(label)):
                # An "If yes, ..." box whose condition is not met, still holding what an earlier pass put
                # there (GEI's non-compete terms read as a sponsorship paragraph, application 443).
                try:
                    el.fill("", timeout=SHORT)
                    log.info("cleared %r: its 'if yes' condition is not met", label[:60])
                except Exception as e:  # noqa: BLE001
                    log.debug("clearing %r: %s", label[:60], e)
                return
        if kind == "text" and (fmt := typed_date_format(el)):
            # A text box that is really a date picker (react-datepicker, a type=date input, a "MM/DD/YYYY"
            # placeholder) throws away anything it cannot parse on blur: "Immediately" typed into OpenAI's
            # Ashby "When can you start a new role?" vanished and the form bounced three times on "Missing
            # entry for required field" (application 329). The answer becomes a date in the box's own format.
            if fill_typed_date(el, ans, fmt):
                log.info("date box %r -> %r (from %r)", label[:60], current_value(el), str(ans)[:40])
                return
            if is_required(el):
                raise NeedsHuman(f"'{label}' is a date box jobbot could not set from {ans!r}. Type the date in "
                                 "the browser window, then click Continue.", question=label, kind="text")
            return
        if kind == "text" and _GPA_LABEL_RE.search(label):
            ans = shape_gpa(ctx, el, ans)
        ans = scrub_illegal(page, ans)
        if has_suggestions(el):
            # A box that only accepts what its own list offers. Typing the true answer at it is what fails:
            # Greenhouse refused "Computer Science & Engineering" with "Please select a school, degree, and
            # field of study from the suggestions" (application 145, C3 AI).
            fill_from_suggestions(page, el, ans, _answer_alternatives(ctx, label))
            return
        if fill_if_empty(el, ans) or kind == "number":
            if (kind != "number" and ans and not clean(current_value(el))
                    and not NUMBER_VALUE_RE.match(str(ans).strip())):
                # Typed, and gone: a box with its own script that keeps digits only (Taraki's salary boxes,
                # application 308). "Negotiable" vanished from both and the step's Next stayed disabled.
                log.info("%r dropped %r; asking for a number", label, str(ans)[:40])
                num = _ask(ctx, el, label, None, "number")
                if num is not None:
                    fill_if_empty(el, num, clear=True)
            return
        # The control refused the answer, and the only control that does is a number box. Its `type` read
        # back as text a moment ago — one dropped attribute read on a slow page is all it takes — so the
        # answer came back as prose ("None" for a notice period) and the required field would have been
        # left empty, which is how a form bounces on "Please enter a number" with nothing visibly wrong.
        # Keyed on the refusal rather than on the pre-read, so it heals whatever the reason was.
        if is_number_box(el) and not current_value(el):
            log.info("%r took no text; asking again for a number", label)
            num = _ask(ctx, el, label, None, "number")
            if num is not None:
                fill_if_empty(el, num)
    elif kind == "select" and is_materialize_select(el):
        opts = [o for o in (options or select_options(el)) if not is_prompt_value(o)]
        held = clean(el.evaluate("e => e.selectedIndex >= 0 && e.value ? e.options[e.selectedIndex].textContent : ''") or "")
        if held and not is_prompt_value(held):
            ctx.seen(held, label, kind=kind, options=opts)
            return
        ans = _ask(ctx, el, label, opts, kind)
        if ans is None:
            return
        if not set_hidden_select(el, ans):
            _choice_miss(el, label, ans, opts, kind)
        else:
            log.info("hidden select %r -> %r", label[:60], ans)
    elif kind == "select" and re.fullmatch(r"\s*(?:day|month|year|dd|mm|yyyy)\s*\**\s*", label or "", re.I):
        _fill_date_parts(ctx, el, label)
    elif kind == "select":
        opts = options or select_options(el)
        existing = clean(el.evaluate("e => e.selectedIndex > 0 ? e.options[e.selectedIndex].textContent : ''"))
        if existing and (want := _policy_over_prefill(ctx, label, existing, opts, kind)):
            if choose_select(el, want, opts):
                return
        if existing:
            ctx.seen(existing, label, kind=kind, options=opts,
                     default=select_is_default(el) or _unchanged_identity(ctx, label, existing))
            return
        if len(opts) == 1:
            # "— Make a Selection — / Continue" (iCIMS's consent gate): one real choice is not a question,
            # and asking the user to pick the only option there is stops a run for nothing.
            log.info("select %r offers one option; taking %r", label, opts[0])
            if not choose_select(el, opts[0], opts):
                _choice_miss(el, label, opts[0], opts, kind)
            return
        ans = _ask(ctx, el, label, opts, kind)
        if ans is None:
            return
        if not choose_select(el, ans, opts):
            _choice_miss(el, label, ans, opts, kind)
    elif kind == "combobox" and (nat := backing_select(el)) is not None:
        opts = [o for o in select_options(nat) if not is_prompt_value(o)]
        held = clean(nat.evaluate("e => e.selectedIndex >= 0 && e.value ? e.options[e.selectedIndex].textContent : ''") or "")
        if held and not is_prompt_value(held):
            ctx.seen(held, label, kind="select", options=opts)
            return
        ans = _ask(ctx, el, label, opts, "select")
        if ans is None:
            return
        want = next((o for o in opts if o.lower() == ans.lower() or same_option(o, ans)), ans)
        if not set_hidden_select(nat, want):
            _choice_miss(el, label, ans, opts, "select")
        else:
            log.info("native select behind %r -> %r", label[:60], want)
    elif kind == "combobox":
        existing = combobox_value(el)
        if existing and _JUNK_CHOICE_RE.match(existing) and not _NUMERIC_LABEL_RE.search(label):
            # Digits where a name belongs: Employment Hero's Country held "+8", the start of a phone number
            # typed at it, and as "already answered" it was left that way (application 413). Chosen afresh.
            log.info("combobox %r holds %r, which is no answer to it; choosing again", label[:60], existing)
            existing = ""
        if existing:
            ctx.seen(existing, label, kind=kind, placeholder=combobox_is_placeholder(el),
                     default=_unchanged_identity(ctx, label, existing))
            return
        opts = options if options is not None else combobox_options(page, el)
        if options is None and len(opts) >= 60:
            # combobox_options stops at 60: this is a searchable list cut short, not the list. Mapped against
            # it, a school outside the first page (RUET on Greenhouse) read as "not an option" and the field
            # was dropped. With no list the resolver gives the true answer, and choose_combobox searches for it.
            opts = []
        ans = _ask(ctx, el, label, opts or None, kind)
        if ans is None:
            return
        seen: list[str] = list(opts or [])
        if not choose_combobox(page, el, ans, _answer_alternatives(ctx, label), allow_other=_other_ok(label),
                               seen=seen):
            if not seen or (options is None and len(opts) >= 60):
                # Typing the answer filtered the list to nothing, so `seen` is empty or a first page: the
                # question would reach the card as a bare text box. The whole list, for the user to pick from.
                seen = combobox_all_options(page, el) or seen
            digits = re.sub(r"\D", "", str(ans))
            if seen and len(digits) >= 8 and sum(1 for o in seen if dial_in(o)) >= max(3, len(seen) // 2):
                # A country-code picker labelled "Phone" (Kingspan, application 461: "Ireland+353", "Albania+355"
                # ...) was given the whole number and left on Ireland. Its options are dial codes: take the
                # longest one our number starts with, and search the list for that country.
                codes = sorted({dial_in(o) for o in seen if dial_in(o)} | {digits[:n] for n in (1, 2, 3)}, key=len, reverse=True)
                code = next((k for k in codes if digits.startswith(k) and any(dial_in(o) == k for o in seen + combobox_all_options(page, el))), "")
                pick = next((o for o in seen + combobox_all_options(page, el) if dial_in(o) == code), None) if code else None
                if pick and choose_combobox(page, el, clean(re.sub(r"\+\s*\d+", "", pick)) or pick, (pick,), seen=[]):
                    log.info("combobox %r is a dial-code list; chose %r for +%s", label[:60], pick, code)
                    return
            if seen and not opts and len(seen) < 60:
                # Answered blind: the list only drew its options once opened (Amazon's select2 "How did you
                # hear about this role?", application 414), so "LinkedIn" was given where the list says
                # "Job Posting". Asked again with the options in hand, the resolver maps to one of them.
                again = _ask(ctx, el, label, seen, kind)
                if again and again != ans and choose_combobox(page, el, again, _answer_alternatives(ctx, label),
                                                              allow_other=_other_ok(label), seen=[]):
                    log.info("combobox %r: %r was not offered; chose %r from the list", label[:60], ans, again)
                    return
            _choice_miss(el, label, ans, seen, kind)
    elif kind in ("radio", "checkbox"):
        cont = container if container is not None else el
        if choice_checked(cont) and (want := _policy_over_prefill(ctx, label, checked_choice_label(cont),
                                                                  options or choice_options(cont), kind)):
            if check_choice(cont, want):
                return
        if choice_checked(cont):
            ctx.seen(checked_choice_label(cont), label, kind=kind, options=options or choice_options(cont),
                     default=choice_is_default(cont))
            return
        if kind == "checkbox" and _tick_agreeable_boxes(cont):
            # The boxes' own words are opt-ins or consents, whatever the group around them is called: EY's
            # "Receive new job posting notifications" sits under "Email Address:" and was asked as a contact
            # question four times (application 288). Consent policy: every such box is ticked.
            return
        opts = options or choice_options(cont)
        ans = _ask(ctx, el, label, opts, kind, cont)
        if ans is None:
            return
        if not check_choice(cont, ans):
            _choice_miss(el, label, ans, opts, kind, cont)
    elif kind == "file":
        return  # the CV goes through upload_resume; the cover letter through fill_cover_letter


def _tick_agreeable_boxes(cont: Any) -> bool:
    """Tick every checkbox in `cont` (or `cont` itself) whose own label is an opt-in or a consent.
    True when at least one was ticked and none of the boxes is anything else."""
    from jobbot.apply.resolver import is_agreeable, normalize_question
    try:
        is_box = (cont.evaluate("e => e.type === 'checkbox' || e.getAttribute('role') === 'checkbox'"))
        boxes = [cont] if is_box else [cont.locator("input[type=checkbox]").nth(i)
                                       for i in range(min(cont.locator("input[type=checkbox]").count(), 8))]
        # A <label> may be only the row's heading ("Notification:") with the box's words in its
        # aria-describedby text, so both are read.
        labels = [clean(get_label_for(b) + " " + (b.evaluate(
            "e => (e.getAttribute('aria-describedby') || '').split(/\\s+/).map(i => { const n = i && document.getElementById(i);"
            " return n ? n.innerText : ''; }).join(' ')") or "")) for b in boxes]
        log.info("checkbox group: own labels %s", [l[:60] for l in labels])
        if not boxes or not all(l and is_agreeable(normalize_question(l)) for l in labels):
            return False
        done = 0
        for b, l in zip(boxes, labels):
            if tick(b):
                done += 1
                log.info("ticked %r (its own label is an opt-in)", l[:80])
        return done > 0
    except Exception as e:  # noqa: BLE001
        log.debug("agreeable boxes: %s", e)
        return False


_GPA_LABEL_RE = re.compile(r"\b(?:gpa|cgpa|grade\s+point)\b", re.I)


def shape_gpa(ctx: ApplyContext, el: Any, value: str) -> str:
    """"2.76" written the way the box's own example is written: "2.76/4.00" for a placeholder reading
    "0.00 / 0.00 (e.g., 3.80/4.00)" (Shopee, application 274). The scale is facts.yaml's where it says one,
    else the example's. A value that already carries its scale, or is not a bare number, is left alone."""
    v = clean(value)
    if not v or "/" in v or not re.fullmatch(r"\d+(?:\.\d+)?", v):
        return v
    try:
        ph = el.get_attribute("placeholder") or ""
    except Exception:  # noqa: BLE001
        ph = ""
    m = re.search(r"\d+(?:\.\d+)?\s*/\s*(\d+(?:\.\d+)?)", ph)
    if not m:
        return v
    scale = clean(str(ctx.fact("education.gpa_scale") or "")) or m.group(1)
    return f"{v}/{scale}"


def _unchanged_identity(ctx: ApplyContext, label: str, value: str) -> bool:
    """True when an identity field (country, location, email…) still holds exactly what it held the first
    time jobbot looked at it on this application -- the form's own value, whoever has had the window since.

    The learn-back writes a changed identity field into facts.yaml, and it only trusts a window a person has
    had in front of them. That is not enough: after one pause, Just Eat's Country dropdown -- which the form
    itself had set to "United Kingdom" from the job's location -- was taken for the candidate's correction
    and rewrote identity.country (application 224). A correction is a *change*; nothing changed here.
    """
    try:
        from jobbot.apply.runner import _identity_fact_key
        if not _identity_fact_key(label):
            return False
    except Exception:  # noqa: BLE001
        return False
    first = ctx.extra.setdefault("first_seen_identity", {})
    key = clean(label).lower()
    if key not in first:
        first[key] = clean(value)
        return True             # first sight: whatever is there, the page put it there
    return first[key] == clean(value)


def _policy_over_prefill(ctx: ApplyContext, label: str, existing: str, options: list[str], kind: str) -> str:
    """The option policy wants in place of a value the form arrived with, '' to leave the value alone.

    Only for questions whose answer is fixed by policy, never by the employer or the candidate's mood:
    "How did you hear about us?". Heidi's Ashby form came with "Heidi Website" already ticked, the walker
    took a filled control for an answered one, and the pre-tick was sent and then cached as the candidate's
    own typed answer (application 199) -- while the rule says the answer is always the job source.
    """
    from jobbot.apply.resolver import is_source_question
    if not existing or not is_source_question(label):
        return ""
    try:
        want = ctx.answer(label, options or None, kind)
    except NeedsHuman:
        return ""           # nothing on the list is true: leave the form's value, and do not learn it either
    if not want or same_option(want, existing):
        return ""
    log.info("%r arrived holding %r; policy answers %r", label[:60], existing[:40], want[:40])
    return want


# ---------- cover letter ----------
COVER_LABEL_RE = re.compile(r"cover\s*letter|motivation letter|letter of (?:interest|motivation)", re.I)


def cover_letter_text(ctx: ApplyContext) -> str:
    """The letter for this job, written once and reused. '' when there is no LLM or no CV."""
    cached = ctx.extra.get("cover_letter")
    if cached is not None:
        return cached
    try:
        from jobbot import config, cover
        text = cover.letter_for(ctx.job, config.load_cv_text(ctx.cv_path), ctx.facts)
    except Exception as e:  # noqa: BLE001 — a missing letter must never fail an application
        log.warning("cover letter unavailable: %s", e)
        text = ""
    ctx.extra["cover_letter"] = text
    return text


def fill_cover_letter(ctx: ApplyContext) -> bool:
    """Put the letter in whatever the form offers: a textarea, or a file input that wants a document.

    The letter is written only once a place to put it has been found. Writing it first cost a model call
    (and two or three seconds) on every single application, and most forms — LinkedIn's Easy Apply, Workday,
    the majority of Greenhouse boards — have nowhere to put a cover letter at all.
    """
    page = ctx.page
    # A textarea is the better target: it keeps the letter readable in the ATS rather than as an attachment.
    for sel in ("textarea[name*='cover' i]", "textarea[id*='cover' i]", "#cover_letter_text",
                "textarea[aria-label*='cover' i]", "textarea[placeholder*='cover' i]"):
        try:
            ta = page.locator(sel).first
            if not is_visible_now(ta):
                continue
            if current_value(ta):
                return True     # already filled; apply() is re-run after every pause
            text = cover_letter_text(ctx)
            if not text:
                return False
            ta.fill(text, timeout=MEDIUM)
            log.info("cover letter written into %s", sel)
            return True
        except Exception:
            continue
    return upload_cover_letter(ctx)


def upload_cover_letter(ctx: ApplyContext, text: str | None = None) -> bool:
    """Attach the letter on a cover-letter file input, if the form has one.

    As a PDF: every board takes one, where a .txt is refused by some (a refusal that shows up as a form
    error at submit, on an attachment that was optional to begin with). The input is recognised by its
    attributes, the text around it, or the label beside it — Gem names its dropzones only in a <span> above.
    """
    page = ctx.page
    try:
        inputs = page.locator("input[type=file]")
        for i in range(inputs.count()):
            el = inputs.nth(i)
            attrs = " ".join(filter(None, [
                el.get_attribute("name"), el.get_attribute("id"), el.get_attribute("aria-label"),
                el.get_attribute("accept"), label_context(el), get_label_for(el)]))
            if not COVER_LABEL_RE.search(attrs or ""):
                continue
            if el.evaluate("e => e.files && e.files.length ? 1 : 0"):
                return True         # already attached
            text = cover_letter_text(ctx) if text is None else text
            if not text:
                return False
            from jobbot import cover
            name = _slugish(ctx.fact("identity.full_name") or "cover-letter")
            path = cover.letter_file(text, name, el.get_attribute("accept") or "")
            el.set_input_files(str(path), timeout=MEDIUM)
            page.wait_for_timeout(600)
            log.info("cover letter attached as %s", path.name)
            return True
    except Exception as e:
        log.warning("could not attach the cover letter: %s", e)
    return False


def label_context(el: Any) -> str:
    """Nearby text for a control, used to tell a cover-letter input from a CV input."""
    try:
        return clean(el.evaluate(
            "e => { const w = e.closest('div,fieldset,label,li'); return w ? (w.innerText||'').slice(0,120) : ''; }"))
    except Exception:
        return ""


def _slugish(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "candidate"


# ---------- framework-aware filling ----------
def react_value(el: Any) -> str | None:
    """What React believes the control holds, or None for a node React is not (yet) attached to."""
    try:
        v = el.evaluate("""e => { const k = Object.keys(e).find(k => k.startsWith('__reactProps'));
            if (!k) return null; const p = e[k]; return p && p.value != null ? String(p.value) : null; }""")
        return None if v is None else str(v)
    except Exception:
        return None


def wait_for_react(page: Any, selector: str, timeout: int = 10000) -> bool:
    """Block until React has attached to `selector`.

    Ashby and the new Greenhouse boards server-render the form and hydrate it a moment later. Filling an input in
    that gap puts text in the DOM that the application's state never sees; on submit the form rejects the field
    as empty while every screenshot shows it filled. Waiting for the __reactProps handle closes the gap.
    """
    try:
        page.wait_for_function(
            "sel => { const e = document.querySelector(sel); "
            "return !!e && Object.keys(e).some(k => k.startsWith('__reactProps')); }",
            arg=selector, timeout=timeout)
        return True
    except Exception:
        return False


def fill_verified(el: Any, value: str) -> bool:
    """fill_if_empty, then confirm the framework registered it.

    If React tracks an empty value while the DOM shows ours (hydration race, a store reset after an autofill),
    clear and type once more. A different non-empty tracked value is a real one — left alone.
    """
    if value is None or value == "":
        return False
    typed = fill_if_empty(el, value)
    if not typed and not clean(current_value(el)):
        # Nothing went in and nothing was there. Saying True here told every caller the box was filled,
        # which is how TalentMate's signup was sent three times with both email boxes empty (application 307).
        return False
    tracked = react_value(el)
    if tracked is not None and clean(tracked) != clean(value) and not clean(tracked):
        try:
            el.click(timeout=SHORT)
            el.fill(value, timeout=MEDIUM)  # fill() clears first
        except Exception as e:
            log.debug("fill_verified refill failed: %s", e)
        tracked = react_value(el)
    return tracked is None or clean(tracked) == clean(value) or clean(current_value(el)) == clean(value)


# "A verification code was sent to you@example.com. To submit your application, enter the 8-character code."
_VERIFY_PROMPT_RE = re.compile(
    r"verification code (?:was |has been )?(?:sent|emailed)"
    # Oracle Recruiting puts the verb first ("We have sent a verification code to ..."), and several
    # boards name the code rather than its length ("Enter the verification code").
    r"|(?:sent|emailed|mailed) (?:you )?(?:a|an|the) (?:verification|security|confirmation|access|one[- ]time) code"
    r"|enter the (?:\d+[- ])?(?:character |digit )?code"
    r"|enter the (?:verification|security|confirmation|access|one[- ]time) code"
    r"|security code"
    r"|confirm (?:that )?you'?re (?:a )?human"
    r"|check your (?:email|inbox) for (?:a|the|your) code", re.I)
_VERIFY_LEN_RE = re.compile(r"(\d+)[-\s]*(?:character|digit)", re.I)
# "sent to you@example.com", and Oracle's "sent to this email address: you@example.com".
_VERIFY_TO_RE = re.compile(r"sent to[^\n@]{0,40}?([^\s,;:]+@[^\s,;:]+\.[A-Za-z]{2,})", re.I)
VERIFY_INPUTS = ("input[name*='security' i], input[name*='verification' i], input[name*='confirmation' i], "
                 "input[id*='security' i], input[id*='verification' i], input[autocomplete='one-time-code'], "
                 "input[name*='otp' i], input[id*='otp' i], input[class*='otp' i], "
                 "input[aria-label*='verification' i], input[aria-label*='one-time' i], "
                 # Lumen: six plain text boxes named only "Please enter OTP character 1…6" (application 225)
                 "input[aria-label*='otp' i], input[placeholder*='otp' i], input[aria-label*='passcode' i], "
                 "input[inputmode='numeric'][maxlength='1'], input[maxlength='1']")
# Oracle Recruiting's "Confirm Your Identity" step draws six round boxes that declare none of the above: no
# maxlength, no name worth matching, nothing but a numeric keyboard hint. Application 129 therefore walked
# the code page as one more step of the form, pressed what looked like its send button and timed out waiting
# for a confirmation that was six digits away. These match on the shape of the control alone, so they are
# only ever consulted after the page text has already said a code was emailed -- on an ordinary form a
# numeric box is a postcode or an area code, which is what the exclusions below keep out.
VERIFY_INPUTS_WEAK = ("input[aria-label*='digit' i], input[aria-label*='code' i], "
                      "input[placeholder*='code' i], input[inputmode='numeric'], input[type='tel']")
VERIFY_WEAK_EXCLUDE_RE = re.compile(
    r"post[\s_-]*code|postal|zip|area[\s_-]*code|country[\s_-]*code|dial|phone|mobile|promo|discount|"
    r"coupon|referral|salary|year", re.I)


class VerificationRequired(ApplyError):
    """The form is asking for an emailed one-time code. Not a failure: the submission is one code away."""
    def __init__(self, prompt: dict):
        super().__init__("Verification code required")
        self.prompt = prompt


def verification_prompt(page: Any) -> dict | None:
    """The emailed-code step, or None. Returns {"length": int, "to": str} when the form is asking for a code.

    Both signals are required — the words AND a set of code inputs — because "security code" also appears in
    unrelated prose, and a lone maxlength=1 input is a common date-field pattern.
    """
    try:
        text = clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))
    except Exception:
        return None
    if not text or not _VERIFY_PROMPT_RE.search(text):
        return None
    boxes = code_boxes(page)
    if not boxes:
        _log_no_code_boxes(page)
        return None
    m = _VERIFY_LEN_RE.search(text)
    length = int(m.group(1)) if m and 3 <= int(m.group(1)) <= 12 else 0
    if not length:
        # The form's own example says how long: Amazon's "Enter verification code (e.g. 123456)" (application
        # 414), whose single box otherwise read as an 8-character code.
        ex = re.search(r"(?:e\.g\.?|for example|example|like)[:\s]*\(?\s*([A-Za-z0-9]{4,10})\b", text, re.I)
        if ex and re.search(r"\d", ex.group(1)):
            length = len(ex.group(1))
    if not length:
        length = _code_length(boxes)
    to = ""
    m = _VERIFY_TO_RE.search(text)
    if m:
        to = m.group(1).rstrip(".")
    return {"length": length, "to": to}


def code_boxes(page: Any) -> list:
    """The visible controls that take an emailed one-time code, in document order. Empty when there are none.

    Several of them means one box per character, which is also how long the code is (_code_length). The
    strong shapes -- a control that names itself a verification, security or one-time code -- are taken
    first; only when none is on the page are the weak ones consulted, and those are filtered, because a
    numeric box on an ordinary form is a postcode as often as it is a code.
    """
    boxes = _visible_matches(page, VERIFY_INPUTS)
    if boxes:
        return boxes
    return [el for el in _visible_matches(page, VERIFY_INPUTS_WEAK) if not _names_another_kind_of_code(el)]


def _visible_matches(page: Any, selector: str, limit: int = 12) -> list:
    """Every visible element matching `selector`, up to `limit`.

    Deliberately not `.first`: visibility used to be read off the first match alone, so one hidden input
    whose name happened to hold "confirmation" could answer for the visible boxes underneath it and the
    whole code step went unrecognised.
    """
    found: list = []
    try:
        loc = page.locator(selector)
        for i in range(min(loc.count(), limit)):
            el = loc.nth(i)
            if is_visible_now(el):
                found.append(el)
    except Exception:  # noqa: BLE001
        pass
    return found


def _names_another_kind_of_code(el: Any) -> bool:
    """True for the numeric boxes that are not one-time codes: postcodes, area codes, promo codes."""
    for attr in ("aria-label", "placeholder", "name", "id"):
        try:
            if VERIFY_WEAK_EXCLUDE_RE.search(el.get_attribute(attr) or ""):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _log_no_code_boxes(page: Any) -> None:
    """The page says a code was emailed but nothing on it looks like somewhere to type one.

    Logged rather than guessed at: when a board draws its code step in a shape nothing here matches, this
    line says what its inputs actually look like, which is what application 129 cost a run to find out.
    Shapes and attribute names only, never a value -- the value would be the code itself.
    """
    try:
        shapes = page.evaluate(
            "() => Array.from(document.querySelectorAll('input'))"
            ".filter(e => e.offsetWidth > 0 || e.offsetHeight > 0).slice(0, 12)"
            ".map(e => [e.type, e.name, e.id, e.getAttribute('maxlength'), e.getAttribute('inputmode'),"
            " e.getAttribute('autocomplete'), e.getAttribute('aria-label')].map(v => v || '-').join('|'))")
    except Exception:  # noqa: BLE001
        return
    log.warning("verification: the page asks for an emailed code but no input on it matched; "
                "visible inputs were %s", shapes)


def _code_length(boxes: list) -> int:
    """How long the code is, read off the boxes when the page does not say it in words.

    The old rule was max(1, count of maxlength=1 boxes), which returns 1 for the ordinary case of a single
    input -- Oracle Recruiting's is one box with maxlength=6 -- and a length of 1 makes the mailbox search
    look for a one-character token, which matches nothing worth having. So: one box per character means
    that many characters, a single box means whatever maxlength it declares, and only then the default.
    """
    if len(boxes) > 1:
        return len(boxes)
    try:
        declared = int((boxes[0].get_attribute("maxlength") or "").strip() or 0)
        if 3 <= declared <= 12:
            return declared
    except Exception:  # noqa: BLE001
        pass
    return 8


def fill_verification_code(page: Any, code: str) -> bool:
    """Type `code` into the form, whether it is one input or one box per character."""
    code = (code or "").strip()
    if not code:
        return False
    try:
        boxes = code_boxes(page)
        if len(boxes) >= len(code) > 1:
            for box in boxes:
                box.fill("", timeout=SHORT)      # clear first: a retry must not append to what is there
            # Real keystrokes into the first box. One-box-per-character widgets (Eightfold, behind Lumen's
            # careers site) keep the code in their own state, fed by key events that also move the focus
            # along; a value set with fill() shows in the box and is still "Missing or Invalid OTP" on
            # submit (application 225).
            boxes[0].click(timeout=SHORT)
            page.keyboard.type(code, delay=60)
            page.wait_for_timeout(200)
            typed = "".join(current_value(b)[:1] for b in boxes[:len(code)])
            if typed != code:
                log.info("verification: keystrokes did not land box by box; typing each box on its own")
                for ch, box in zip(code, boxes):
                    box.click(timeout=SHORT)
                    box.fill("", timeout=SHORT)
                    box.press_sequentially(ch, delay=40)
            page.wait_for_timeout(300)
            return True
        if boxes:
            one = boxes[0]
            one.click(timeout=SHORT)
            one.fill(code, timeout=MEDIUM)
            page.wait_for_timeout(300)
            return True
    except Exception as e:
        log.warning("could not type the verification code: %s", e)
    return False


_SMS_2FA_RE = re.compile(
    r"(?:2|two)[- ]?(?:factor|step)\s+(?:authentication|verification)|\b2fa\b|\bmfa\b"
    r"|(?:text|sms)\s+(?:you\s+)?(?:a\s+)?(?:one[- ]time\s+)?(?:verification\s+)?code"
    r"|phone number for verification", re.I)


_OUTLINE_JS = r"""(needle) => {""" + DEEP_JS + r"""
    const want = needle.toLowerCase();
    for (const r of _deepRoots) for (const el of r.querySelectorAll('*')) {
        if (['SCRIPT', 'STYLE', 'TEMPLATE'].includes(el.tagName)) continue;
        const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ')
                    .replace(/\s+/g, ' ').trim().toLowerCase();
        if (!own || !own.includes(want)) continue;
        if (deepClosest(el, '[class*=error i], [role=alert], [aria-live]')) continue;   // the complaint, not the field
        let box = el;
        for (let i = 0; i < 2; i++) { const up = box.parentElement || (box.parentNode && box.parentNode.host);
                                      if (!up || up === document.body) break; box = up; }
        const d = (n, k) => { if (k > 8 || !n.tagName) return '';
            const a = ['type', 'role', 'name', 'aria-label', 'aria-pressed', 'aria-checked', 'slot', 'label']
                .map(x => n.getAttribute(x) ? x + '=' + String(n.getAttribute(x)).slice(0, 30) : '').filter(Boolean).join(' ');
            const cls = (n.getAttribute('class') || '').split(/\s+/).filter(c => c && c.length < 40).slice(0, 2).join('.');
            const t = [...n.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent.trim()).join(' ').slice(0, 40);
            const kids = n.shadowRoot ? [...n.shadowRoot.children, ...n.children] : [...n.children];
            return ' '.repeat(k) + n.tagName.toLowerCase() + (cls ? '.' + cls : '') + (n.shadowRoot ? '(#s)' : '')
                 + (a ? ' [' + a + ']' : '') + (t ? ' "' + t + '"' : '') + '\n' + kids.slice(0, 12).map(c => d(c, k + 1)).join(''); };
        return d(box, 0).slice(0, 3000);
    }
    return '';
}"""


_ILLEGAL_CHARS_RE = re.compile(r"(?:contains?|has)\s+(?:illegal|invalid|disallowed|unsupported)\s+characters?:?\s*(.+)$", re.I)


def strip_illegal_characters(page: Any, errors: list[str]) -> int:
    """Remove the characters a form names as illegal from the fields it names. Returns how many changed.

    Workday: "Role Description Contains illegal characters < > [ ] \" { } \\" -- the job description its own
    CV parse wrote in carried a quote, and every save bounced on it (application 251). The characters are
    read from the message itself, so another board's list works the same way."""
    changed = 0
    for err in errors or []:
        m = _ILLEGAL_CHARS_RE.search(err or "")
        if not m:
            continue
        bad = set(ch for ch in m.group(1) if not ch.isspace())
        if not bad:
            continue
        head = clean(err[:m.start()])
        head = re.sub(r"^(?:errors? found\s*)?(?:error\s*[-:]\s*)?", "", head, flags=re.I).strip(" -:")
        try:
            boxes = page.locator("textarea:visible, input[type=text]:visible")
            for i in range(min(boxes.count(), 80)):
                el = boxes.nth(i)
                val = current_value(el)
                if not val or not any(ch in bad for ch in val):
                    continue
                label = clean(get_label_for(el))
                if head and label and head.lower() not in label.lower() and label.lower() not in head.lower():
                    continue
                el.fill("".join(ch for ch in val if ch not in bad), timeout=MEDIUM)
                changed += 1
                log.info("removed the characters %s from %r, which the form refuses", "".join(sorted(bad)), label[:50])
        except Exception as e:  # noqa: BLE001
            log.debug("strip_illegal_characters: %s", e)
    return changed


def _silent_bot_check(page: Any, complaint: str) -> bool:
    """A generic "error processing your application" on a page carrying an invisible reCAPTCHA/hCaptcha."""
    if not re.search(r"error processing your application|something went wrong|please try again", complaint or "", re.I):
        return False
    try:
        return bool(page.locator(".grecaptcha-badge, iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
                                 "iframe[title*='reCAPTCHA' i]").count())
    except Exception:  # noqa: BLE001
        return False


def log_field_outline(page: Any, complaint: str) -> None:
    """Log the markup around the field a form keeps refusing, so the next unknown widget explains itself in
    the log instead of costing a probe and a rerun (applications 222 and 230 each needed one). Structure,
    attributes and short labels only -- never an input's value."""
    m = re.search(r"(?:required field:|field is required:|:)\s*(.{12,60}?)(?:;|$)", complaint or "")
    needle = clean(m.group(1) if m else complaint)[:40]
    if len(needle) < 8:
        return
    try:
        outline = page.evaluate(_OUTLINE_JS, needle)
    except Exception as e:  # noqa: BLE001
        log.debug("field outline: %s", e)
        return
    if outline:
        log.warning("field outline for the refused %r:\n%s", needle, outline)


def detect_sms_2fa(page: Any) -> None:
    """Stop, with the reason, on a step that texts a code to the candidate's phone. The mailbox reader cannot
    see an SMS, and the walker used to press Continue on "Set up 2-factor authentication" and report a
    submit it could not confirm (application 225, Lumen/Eightfold) -- sending the user to look for an
    application that was never sent."""
    try:
        text = clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))[:4000]
        if not text or not _SMS_2FA_RE.search(text):
            return
        if not page.locator("input[type=tel]:visible, input[name*=phone i]:visible, input[placeholder*=phone i]:visible, "
                            "input[autocomplete*=one-time-code]:visible").count():
            return
    except Exception:  # noqa: BLE001
        return
    raise NeedsHuman("This employer wants to text a verification code to your phone (two-factor sign-in), which "
                     "jobbot cannot read. Finish that step in the open window, then click Continue — the "
                     "application carries on from there.")


RESEND_NAMES = ("resend email", "resend code", "resend", "send a new code", "send new code", "resend the code",
                "send code again", "get a new code", "request a new code", "didn't get a code? resend")


# "Resend", "Send me a new code" (Amazon, application 414), "Didn't get it? Send again", "Request a new link":
# worded a dozen ways, so matched by shape rather than listed. Anchored to the start of the control's name so a
# sentence that merely mentions sending is not pressed.
RESEND_RE = re.compile(r"^\s*(?:didn'?t (?:get|receive) (?:it|a code|the (?:code|email|link))\??\s*)?"
                       r"(?:re-?send|send (?:me )?(?:a |the )?(?:new |another )?(?:code|link|email|one)(?: again)?"
                       r"|send (?:it )?again|(?:get|request) (?:a )?new (?:code|link)|send new (?:code|link))\b", re.I)


def resend_control(page: Any) -> Any:
    """The visible control that asks the site to mail the code or link again, or None."""
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role, name=RESEND_RE)
            for i in range(min(loc.count(), 4)):
                el = loc.nth(i)
                if is_visible_now(el) and el.is_enabled():
                    return el
        except Exception:  # noqa: BLE001
            continue
    return named_button(page, RESEND_NAMES) or _named_link_on(page, RESEND_NAMES)


def _named_link_on(page: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        try:
            loc = page.get_by_role("link", name=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I))
            for i in range(min(loc.count(), 3)):
                if is_visible_now(loc.nth(i)):
                    return loc.nth(i)
        except Exception:  # noqa: BLE001
            continue
    return None


def handle_verification(ctx: ApplyContext, prompt: dict, submitted_at) -> None:
    """Satisfy the emailed-code step: read the code from the mailbox, or ask the user for it once.

    Raises NeedsHuman when the code cannot be read, so the application parks with the browser open instead of
    failing — the form is filled and one code away from being submitted.
    """
    from jobbot import mail

    length, to = prompt.get("length") or 8, prompt.get("to") or ""
    ctx.step("Waiting for the verification code by email")
    hints = (ctx.job.get("company", ""), ctx.job.get("ats", ""))
    code = mail.fetch_code(submitted_at, length=length, hints=hints)
    used = ctx.extra.setdefault("codes_used", [])
    if code and code in used:
        # Typed already and the step is still here, so the form refused it — usually because it expired
        # (Amazon's last 3 minutes, application 414). Reading the same mail again would loop; ask for a new one.
        log.info("verification: the code in the mailbox was already tried; asking for a new one")
        code = None
    if not code and mail.is_configured()[0] and not ctx.extra.get("code_resent"):
        # The code this step is waiting for was mailed before the wait began -- on a Retry, or after a code
        # that was typed wrong -- so no new mail is coming by itself. Lumen's step offers "resend email";
        # pressing it once, then reading only mail newer than the press, is what a person would do
        # (application 225). Once per application: every press restarts the sender's cooldown.
        resend = resend_control(ctx.page)
        if resend is not None:
            from datetime import datetime, timezone
            ctx.extra["code_resent"] = True
            sent = datetime.now(timezone.utc)
            try:
                resend.click(timeout=MEDIUM)
                log.info("verification: no fresh code in the mailbox; pressed %r and waiting again",
                         clean(resend.inner_text() or "")[:40])
                ctx.step("Asked for a new verification code")
                code = mail.fetch_code(sent, length=length, hints=hints)
            except Exception as e:  # noqa: BLE001
                log.debug("verification: resend: %s", e)
    if not code:
        ok, why = mail.is_configured()
        reason = VERIFY_MSG.format(to=to or mail.mail_user() or "your inbox")
        if ok:
            reason = f"No code arrived for {to or mail.mail_user()}. Paste it here once it does."
        else:
            log.info("verification: mailbox not usable (%s)", why)
        # Asked through the resolver so a resume can read the answer back; never written to answers.json
        # (see Resolver.learn) because a one-time code must not be replayed on the next application.
        code = ctx.answer(VERIFY_QUESTION, None, "text")
    used.append(code)
    if not fill_verification_code(ctx.page, code):
        raise ApplyError("Could not type the verification code into the form")
    ctx.step("Verification code entered")


# An optional account upsell between sign-in and the form: Employment Hero's "Transition your profile to
# password login" offers "Set Up Password" or "Skip for now (3 logins remaining)" (application 413). Nothing on
# it belongs to the application, so the skip is pressed; a cookie banner over it is never the way on.
UPSELL_PAGE_RE = re.compile(
    r"set ?up (?:a |your )?(?:password|passkey)|password login|passkeys?\b|two[- ]factor|multi[- ]factor|\bmfa\b"
    r"|download (?:our|the) app|get the app|(?:enable|turn on) (?:push )?notifications|secure your account"
    r"|security upgrade|add (?:a )?(?:phone|recovery) (?:number|email)", re.I)
SKIP_CONTROL_RE = re.compile(r"^\s*(?:skip(?: for now| this step| this| it)?|not now|maybe later|remind me later"
                             r"|no,? thanks|later|i'?ll do (?:it|this) later|do (?:it|this) later)\b", re.I)
COOKIE_CONTROL_RE = re.compile(r"cookie|accept all|reject all|allow all|deny all|without accepting"
                               r"|(?:only|strictly) necessary|necessary only|manage (?:preferences|consent)", re.I)


# The sign-in-by-email step. JOIN asks for the candidate's email on its first page and then, instead of a
# form, shows "We've sent you a secure login link — Check your email": the application only continues once
# that link is opened, in this browser. There is no code to type and no button that moves it on, so the
# walker used to press nothing and fail with "found neither a Submit nor a Next button" (application 169).
_LOGIN_LINK_PROMPT_RE = re.compile(
    r"(?:sent|emailed|mailed) you an? (?:secure |magic |one[- ]time )?(?:log ?in|sign[- ]?in|magic|access) link"
    r"|check your (?:email|inbox) (?:for|to find) (?:a|the|your) (?:log ?in |sign[- ]?in |magic )?link"
    # Teamtailor, for an address it already knows: "Please click the verification link in the email to
    # complete your application" -- the application is held until that link is opened (application 240).
    r"|click (?:on )?the (?:verification|confirmation|activation) link in (?:the|your|this) e-?mail", re.I)
# The link to follow in that mail, as opposed to its imprint and terms links.
LOGIN_LINK_URL_RE = re.compile(r"log-?in|sign-?in|magic|verif|auth|token", re.I)
LOGIN_LINK_QUESTION = "Sign-in link from the email (one-time)"
LOGIN_LINK_WAIT_MS = 4000


def login_link_prompt(page: Any) -> bool:
    """True when the page is waiting for the candidate to open a sign-in link it emailed them."""
    try:
        text = clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))
    except Exception:  # noqa: BLE001
        return False
    return bool(text and _LOGIN_LINK_PROMPT_RE.search(text))


# "Didn't get the email? Send again" -- the press that mails a fresh link once the old one has expired.
LOGIN_RESEND_NAMES = RESEND_NAMES + ("send again", "resend link", "send link again", "send a new link",
                                     "resend sign-in link", "resend login link", "send me a new link")
_LINK_DEAD_RE = re.compile(r"link (?:has |is )?(?:expired|invalid|no longer valid|already (?:been )?used)"
                           r"|(?:expired|invalid) (?:sign[- ]?in |log ?in |magic )?link", re.I)


def _link_failed(page: Any) -> bool:
    """True when following the link left the candidate where they were: the wait page, or an "expired" one."""
    if login_link_prompt(page):
        return True
    try:
        text = clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))
    except Exception:  # noqa: BLE001
        return False
    return bool(_LINK_DEAD_RE.search(text or ""))


def follow_login_link(ctx: ApplyContext, sent_at) -> None:
    """Open the emailed sign-in link in the application's own window, which is what continues the form.

    Read from the mailbox like a code. When it cannot be, the user is asked to paste it once: opening it
    from the mail app would sign in their everyday browser, not the window jobbot is filling.

    These links are one-time and short-lived (Employment Hero's last 15 minutes), so on a Retry the one in
    the mailbox is usually spent. When the link does not sign in, the wait page is reopened, its "Send
    again" is pressed once, and only mail newer than that press is read (application 413).
    """
    from datetime import datetime, timezone
    from jobbot import mail

    page = ctx.page
    wait_url = page.url
    ctx.step("Opening the sign-in link from your mailbox")
    host = (urlparse(page.url).hostname or "").removeprefix("www.").split(".")[0]
    hints = (ctx.job.get("company", ""), ctx.job.get("ats", ""), host)
    used = ctx.extra.setdefault("login_links_used", [])
    link = mail.fetch_link(sent_at, match=LOGIN_LINK_URL_RE, label=mail.LOGIN_LABEL_RE, hints=hints)
    if link in used:
        link = None                     # spent by an earlier attempt; following it again only shows "expired"
    for attempt in range(2):
        if not link and attempt == 0 and used and mail.is_configured()[0]:
            link = "resend"             # a Retry whose only mailed link is spent: go straight to "Send again"
        if link and link != "resend":
            # JOIN's plain-text part carries "&amp;" between the query parameters, and a link followed with
            # those still in it drops the token and signs nobody in.
            link = html.unescape(link)
            if not link.lower().startswith("http"):
                raise ApplyError("The sign-in link from the email is not a web address")
            used.append(link)
            goto(page, link, timeout=30000)
            page.wait_for_timeout(LOGIN_LINK_WAIT_MS)
            log.info("followed the emailed sign-in link; now on %s", page.url[:100])
            if not _link_failed(page):
                ctx.step("Signed in from the email link")
                return
        if attempt or ctx.extra.get("login_link_resent") or not mail.is_configured()[0]:
            break
        # Back to the wait page and ask for a new link, as a person would.
        if page.url != wait_url:
            try:
                goto(page, wait_url, timeout=30000)
                page.wait_for_timeout(LOGIN_LINK_WAIT_MS)
            except Exception as e:  # noqa: BLE001
                log.debug("login link: back to the wait page: %s", e)
        resend = resend_control(page)
        if resend is None:
            log.info("login link: no 'Send again' on %s", page.url[:100])
            break
        ctx.extra["login_link_resent"] = True
        sent = datetime.now(timezone.utc) - timedelta(seconds=VERIFY_CLOCK_SKEW_S)
        try:
            resend.click(timeout=MEDIUM)
        except Exception as e:  # noqa: BLE001
            log.debug("login link: resend: %s", e)
            break
        log.info("login link: the mailed link was spent; pressed 'Send again' and waiting for a new one")
        ctx.step("Asked for a new sign-in link")
        link = mail.fetch_link(sent, match=LOGIN_LINK_URL_RE, label=mail.LOGIN_LABEL_RE, hints=hints)
    if not used:
        # No link could be read from the mailbox at all: ask once for it to be pasted.
        link = html.unescape((ctx.answer(LOGIN_LINK_QUESTION, None, "text") or "").strip())
        if not link.lower().startswith("http"):
            raise ApplyError("The sign-in link from the email is not a web address")
        used.append(link)
        goto(page, link, timeout=30000)
        page.wait_for_timeout(LOGIN_LINK_WAIT_MS)
        if not _link_failed(page):
            ctx.step("Signed in from the email link")
            return
    # Expired, or already spent. Following it again would loop to MAX_PAGES.
    raise NeedsHuman("The emailed sign-in link did not sign in (it may have expired). Press 'Resend link' "
                     "in the open window, then click Retry.")


# A date asked for with a calendar rather than a box. JOIN draws its "When are you available to start?" step as
# an always-open Ark UI date picker: a grid of day cells, each carrying its ISO date, with a month and a year
# react-select above it that only turn the page. Walked as ordinary controls, those two selects were taken for
# questions of their own, labelled by the month they showed, and the run failed on "Could not choose
# 'September' for 'September'" (application 169) while the date itself was never picked.
CALENDAR_SEL = ("[aria-roledescription=datepicker], [data-scope=date-picker][data-part=content], "
                "[role=application][aria-label*=calendar i]")
_CALENDAR_CELL_SEL = "[role=gridcell]"
_CALENDAR_NEXT_SEL = ("[data-part=next-trigger], button[aria-label*='next month' i], "
                      "[role=button][aria-label*='next month' i]")
_CALENDAR_MAX_MONTHS = 12
_SOON_RE = re.compile(r"\b(?:immediately|asap|as soon as possible|right away|now|none|no notice)\b|^\s*0\s*$", re.I)
_IN_RE = re.compile(r"(\d+)\s*(day|week|month)", re.I)


def in_calendar(el: Any) -> bool:
    """True for a control that belongs to a calendar widget: its month and year pickers are navigation."""
    try:
        return bool(el.evaluate("(e, sel) => !!e.closest(sel)", CALENDAR_SEL))
    except Exception:  # noqa: BLE001
        return False


def _calendar_label(cal: Any) -> str:
    """The question a calendar answers: the nearest heading or label written before it."""
    try:
        return clean(cal.evaluate("""e => {
            const q = 'h1,h2,h3,h4,legend,label';
            for (let n = e, d = 0; n && d < 8; n = n.parentElement, d++) {
                for (let s = n.previousElementSibling; s; s = s.previousElementSibling) {
                    const t = s.matches(q) ? s : s.querySelector(q);
                    if (t && t.innerText.trim()) return t.innerText.trim();
                }
            }
            return '';
        }"""))
    except Exception:  # noqa: BLE001
        return ""


_DATE_PLACEHOLDER_RE = re.compile(r"^\s*(?:mm|dd|yyyy|yy|m|d)(?:\s*[/.\-]\s*(?:mm|dd|yyyy|yy|m|d)){2}\s*$", re.I)


def typed_date_format(el: Any) -> str | None:
    """The strftime format a typed date box expects, or None when the box is not a date box.

    Recognised by shape, not by board: an <input type=date>, an input inside react-datepicker's wrapper
    (its default format is MM/dd/yyyy), or a placeholder spelling the format out ("DD/MM/YYYY")."""
    try:
        info = el.evaluate("""e => ({type: (e.type || '').toLowerCase(), ph: e.getAttribute('placeholder') || '',
                                     rdp: !!e.closest('.react-datepicker-wrapper, .react-datepicker__input-container')})""")
    except Exception:  # noqa: BLE001
        return None
    if info.get("type") == "date":
        return "%Y-%m-%d"
    ph = clean(info.get("ph") or "")
    if ph and _DATE_PLACEHOLDER_RE.match(ph):
        fmt = ph.lower()
        for tok, rep in (("yyyy", "%Y"), ("yy", "%y"), ("mm", "%m"), ("dd", "%d")):
            fmt = fmt.replace(tok, rep)
        fmt = re.sub(r"(?<!%)\bm\b", "%m", fmt)
        fmt = re.sub(r"(?<!%)\bd\b", "%d", fmt)
        return fmt.replace(" ", "")
    if info.get("rdp"):
        return "%m/%d/%Y"
    return None


def fill_typed_date(el: Any, answer: str, fmt: str) -> bool:
    """Type `answer` ("Immediately", "2 weeks", "1 Nov 2026") into a date box as a date in `fmt`, and commit
    it the way a person would (Enter, then leave the box). True when the box keeps the value."""
    from datetime import date
    target = _target_date(str(answer or ""), date.today())
    if target is None:
        return False
    value = target.strftime(fmt)
    try:
        if fmt == "%Y-%m-%d" and (el.get_attribute("type") or "").lower() == "date":
            el.fill(value, timeout=SHORT)
        else:
            try:
                el.click(timeout=SHORT)
            except Exception:  # noqa: BLE001
                el.focus(timeout=SHORT)
            el.fill("", timeout=SHORT)
            el.press_sequentially(value, delay=40, timeout=MEDIUM)
            el.press("Enter", timeout=SHORT)
            try:
                el.press("Escape", timeout=SHORT)
            except Exception:  # noqa: BLE001
                pass
            el.evaluate("e => e.blur()")
    except Exception as e:  # noqa: BLE001
        log.debug("fill_typed_date: %s", e)
        return False
    return bool(clean(current_value(el)))


_DATE_PARTS_JS = r"""(e) => {
    // The three selects of one date (Talemetry's MonthYearDaySelect, GEI application 443), and the question
    // they answer: the nearest text above the group that is not itself a part's label.
    const part = t => { t = (t || '').replace(/[*\s]+/g, ' ').trim().toLowerCase();
        return /^(day|dd)$/.test(t) ? 'day' : /^(month|mm)$/.test(t) ? 'month' : /^(year|yyyy)$/.test(t) ? 'year' : ''; };
    const lab = s => { const l = s.labels && s.labels[0]; if (l) return l.innerText;
        const f = s.closest('[class*=FormControl], .field, div'); const x = f && f.querySelector('label'); return x ? x.innerText : ''; };
    let group = e.parentElement;
    for (let i = 0; i < 6 && group; i++, group = group.parentElement) {
        const sels = [...group.querySelectorAll('select')].filter(s => part(lab(s)));
        if (sels.length >= 2) {
            sels.forEach(s => s.setAttribute('data-jobbot-datepart', part(lab(s))));
            let q = '';
            for (let n = group; n && !q; n = n.parentElement) {
                const cands = [...n.querySelectorAll('label, legend, p, h3, h4, span')]
                    .filter(x => !group.contains(x) && (x.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING));
                const t = cands.length ? (cands[cands.length - 1].innerText || '').trim() : '';
                if (t && !part(t)) q = t;
                if (n === document.body) break;
            }
            return q.slice(0, 200);
        }
    }
    return null;
}"""


def _fill_date_parts(ctx: ApplyContext, el: Any, label: str) -> None:
    """Answer a date drawn as Month / Day / Year selects as one question, and set each part."""
    from datetime import date
    page = ctx.page
    try:
        question = el.evaluate(_DATE_PARTS_JS)
    except Exception as e:  # noqa: BLE001
        log.debug("date parts: %s", e)
        question = None
    if question is None:
        _ask(ctx, el, label, select_options(el), "select")      # a lone "Year" list: an ordinary question
        return
    question = strip_required(clean(question)) or "Date"
    ans = _ask(ctx, el, question, None, "text")
    if not ans:
        return
    target = _target_date(str(ans), date.today())
    if target is None:
        raise NeedsHuman(f"'{question[:80]}' wants a date and jobbot could not turn {ans!r} into one. Set it in "
                         "the browser window, then click Continue.", question=question, kind="text")
    import calendar
    wants = {"day": [str(target.day), f"{target.day:02d}"],
             "month": [calendar.month_name[target.month], calendar.month_abbr[target.month], str(target.month),
                       f"{target.month:02d}"],
             "year": [str(target.year)]}
    for part, values in wants.items():
        box = page.locator(f"select[data-jobbot-datepart={part}]").first
        try:
            if not box.count():
                continue
            opts = select_options(box)
            hit = next((o for v in values for o in opts if clean(o).lower() == v.lower()), None)
            if hit is not None:
                box.select_option(label=hit, timeout=MEDIUM)
        except Exception as e:  # noqa: BLE001
            log.debug("date part %s: %s", part, e)
    log.info("date %r -> %s (from %r)", question[:60], target.isoformat(), str(ans)[:40])


def _target_date(answer: str, today):
    """The earliest date an answer allows: "Immediately" is today, "2 weeks" is a fortnight on, a date is
    that date. Never in the past, which no availability calendar offers."""
    from dateutil import parser as dateparser

    text = clean(answer)
    if not text or _SOON_RE.search(text):
        return today
    m = _IN_RE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return today + timedelta(days=n * {"day": 1, "week": 7, "month": 30}[unit])
    try:
        return max(today, dateparser.parse(text, fuzzy=True, default=datetime.combine(today, datetime.min.time())).date())
    except (ValueError, OverflowError):
        return None


def _cell_date(cell: Any):
    """The date a calendar cell stands for, from its ISO data-value or else its aria-label."""
    from dateutil import parser as dateparser

    try:
        info = cell.evaluate("""e => {
            const t = e.querySelector('[data-value],[aria-label]') || e;
            return [e.getAttribute('data-value') || t.getAttribute('data-value') || e.getAttribute('data-date') || '',
                    t.getAttribute('aria-label') || e.getAttribute('aria-label') || '',
                    !!(e.matches('[aria-disabled=true],[data-disabled],[data-unavailable]') ||
                       t.matches('[aria-disabled=true],[data-disabled],[data-unavailable]'))];
        }""")
    except Exception:  # noqa: BLE001
        return None
    value, aria, disabled = info
    if disabled:
        return None
    for raw in (value, re.sub(r"^\s*(?:choose|select)\s+", "", aria or "", flags=re.I)):
        if not raw:
            continue
        try:
            return dateparser.parse(raw, fuzzy=True).date()
        except (ValueError, OverflowError):
            continue
    return None


def fill_calendar(ctx: ApplyContext) -> bool:
    """Answer a calendar question by clicking the earliest day the answer allows. True when a day was picked.

    Asked like any other question, by the heading above the calendar, so "When are you available to start"
    comes from preferences.start_date. A calendar that already has a day selected is left alone.
    """
    page = ctx.page
    try:
        cals = [c_ for c_ in _visible_matches(page, CALENDAR_SEL, limit=4)
                if c_.locator(_CALENDAR_CELL_SEL).count()]
    except Exception:  # noqa: BLE001
        return False
    picked = False
    for cal in cals:
        if cal.locator("[role=gridcell][aria-selected=true], [role=gridcell] [data-selected]").count():
            continue
        label = _calendar_label(cal) or "Start date"
        ans = _ask(ctx, cal, label, None, "text")
        if ans is None:
            continue
        today = datetime.now().date()
        target = _target_date(str(ans), today)
        if target is None:
            raise ApplyError(f"Could not read {ans!r} as a date for {label!r}")
        for _ in range(_CALENDAR_MAX_MONTHS + 1):
            best, best_date = None, None
            cells = cal.locator(_CALENDAR_CELL_SEL)
            for i in range(min(cells.count(), 42)):
                cell = cells.nth(i)
                d = _cell_date(cell)
                if d is not None and d >= target and (best_date is None or d < best_date):
                    best, best_date = cell, d
            if best is not None:
                trigger = best.locator("[role=button], button").first
                (trigger if trigger.count() else best).click(timeout=SHORT)
                page.wait_for_timeout(400)
                log.info("calendar: picked %s for %r (answer %r)", best_date.isoformat(), label, ans)
                picked = True
                break
            nxt = cal.locator(_CALENDAR_NEXT_SEL).first
            if not nxt.count():
                break
            nxt.click(timeout=SHORT)
            page.wait_for_timeout(400)
        if best is None:
            raise ApplyError(f"The calendar for {label!r} offers no day on or after {target.isoformat()}")
    return picked


# Sign-in pages that belong to an identity provider, not to the employer. Some employers accept applications
# only from a signed-in account there — Google Careers sends Apply to accounts.google.com (application 176).
# jobbot never types anything into these: they are the candidate's own personal accounts, the providers refuse
# sign-ins from an automated browser and ask for a second factor, and jobbot's generated employer password
# typed at one is a failed login on the candidate's real account. The candidate signs in once in the window;
# the runner saves the session on Continue, so later applications to that employer go straight through.
_SSO_HOSTS = (
    (re.compile(r"(^|\.)accounts\.google\.com$"), "Google"),
    (re.compile(r"(^|\.)login\.(?:microsoftonline|live|microsoft)\.com$"), "Microsoft"),
    (re.compile(r"(^|\.)appleid\.apple\.com$"), "Apple"),
    (re.compile(r"(^|\.)github\.com$"), "GitHub"),
    (re.compile(r"(^|\.)okta(?:preview)?\.com$"), "Okta"),
)
_SSO_PATH = {"GitHub": re.compile(r"^/(?:login|session)"), }
SSO_MSG = ("This employer only takes applications from a signed-in {provider} account, and jobbot never signs "
           "in to your personal accounts for you. Sign in to {provider} once in the open window, then click "
           "Continue — the sign-in is saved, so later applications to this employer skip this step.")


def sso_provider(url: str) -> str:
    """The identity provider whose sign-in page `url` is ('Google', 'Microsoft', ...), '' for anything else."""
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    for pattern, name in _SSO_HOSTS:
        if pattern.search(host):
            path_rule = _SSO_PATH.get(name)
            if path_rule and not path_rule.search(parsed.path or ""):
                return ""
            return name
    return ""


def detect_sso(page: Any) -> None:
    """Pause, with the reason, when the application has been sent to an identity provider's sign-in."""
    provider = sso_provider(getattr(page, "url", "") or "")
    if provider:
        raise NeedsHuman(SSO_MSG.format(provider=provider))


def _confirmation_arrives(page: Any, wait_s: float = 5.0) -> bool:
    """Whether a confirmation shows up within `wait_s`. Asked before any rejection is acted on, because a
    single-page board swaps the form for its thank-you a moment after the click, and whatever was read in
    between is neither the form's verdict nor worth "repairing" a filled field over."""
    deadline = time.time() + wait_s
    while True:
        if confirmation_showing(page):
            log.info("the page confirmed the application after all")
            return True
        if time.time() >= deadline:
            return False
        try:
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001 - a closed page confirms nothing
            return False


def confirmation_showing(page: Any) -> bool:
    """True when the page is, right now, telling the candidate the application went in.

    The cheap half of wait_for_confirmation, for callers that only need to know what is on screen.
    """
    try:
        body = clean(page.evaluate("() => (document.body && document.body.innerText) || ''")).lower()
        if not confirm_url(page.url or "") and not any(t in body for t in STRONG_CONFIRM_TEXTS):
            return False
        # A page still holding an application form is not its thank-you page, whatever words it carries
        # somewhere: Coveo's job page (application 373) was "confirmed" with every field of its form on screen.
        return _typeable_count_all_frames(page) < 3
    except Exception:  # noqa: BLE001
        return False


def _typeable_count_all_frames(page: Any) -> int:
    """_typeable_count over the page and every frame in it: Coveo draws its form in an iframe, and the top
    page alone counted none of it (application 375)."""
    total = max(_typeable_count(page), 0)
    try:
        frames = list(getattr(page, "frames", []) or [])
    except Exception:  # noqa: BLE001
        frames = []
    main = getattr(page, "main_frame", None)
    for fr in frames:
        if fr is main:
            continue
        total += max(_typeable_count(fr), 0)
    return total


def _submit_key(url: str) -> str:
    """Which page a submit was pressed on: host and path, without the query a board rewrites per visit."""
    try:
        u = urlparse(url or "")
        return (u.netloc + u.path).lower()
    except Exception:  # noqa: BLE001
        return url or ""


def _step_name(page: Any) -> str:
    """Which step of a wizard is showing: the active progress-bar step, else the main heading."""
    try:
        return clean(page.evaluate("""() => {
            const a = document.querySelector("[data-automation-id='progressBarActiveStep'], [aria-current=step], "
                + "[class*=step][class*=active i], [class*=Step][class*=current i]");
            const h = document.querySelector('main h1, main h2, h1, h2');
            return ((a && a.innerText) || (h && h.innerText) || '').slice(0, 120); }""") or "")
    except Exception:  # noqa: BLE001
        return ""


def _guard_uncertain_submit(ctx: ApplyContext, page: Any) -> bool:
    """Stand between a form jobbot already sent once and a second press of its Submit button.

    A submit that produced no confirmation and no complaint may well have gone through — the form-gone
    grace period in wait_for_confirmation exists because boards word their thank-you pages in a hundred
    ways. Sending it again would put a duplicate application in front of a real employer, so from here on
    that decision is the candidate's. Returns True when the application turns out to be in already.
    """
    where = ctx.extra.get("submit_uncertain")
    if not where:
        return False
    pressed_on = where.get("url", "") if isinstance(where, dict) else ""
    if pressed_on and _submit_key(pressed_on) != _submit_key(getattr(page, "url", "") or ""):
        # The uncertain press was on another page -- a posting's own Apply button taken for a submit (EY
        # and BCG, applications 288 and 289) -- and this is the form it led to, which has not been sent.
        ctx.extra.pop("submit_uncertain", None)
        log.info("the uncertain submit was on %s, not this page; sending this form normally", pressed_on[:120])
        return False
    if answer_submit_dialog(page, set()):
        # The first press is still in flight behind a dialog of its own ("save these changes? Yes / No",
        # application 429). Answering it completes that submit; it does not send a second one.
        try:
            if wait_for_confirmation(page):
                ctx.extra.pop("submit_uncertain", None)
                log.info("the pending submit went through once its dialog was answered")
                return True
        except ApplyError as e:
            log.info("after answering the submit's dialog: %s", str(e)[:160])
    was_on = where.get("step", "") if isinstance(where, dict) else ""
    now_on = _step_name(page)
    if was_on and now_on and was_on != now_on and not confirmation_showing(page):
        # The press moved a wizard on to another step (Workday's disclosures -> Review, application 427):
        # it was a Next, not a submit, and this step's own Submit has never been pressed.
        ctx.extra.pop("submit_uncertain", None)
        log.info("the uncertain press was on step %r and the form is now on %r; it was not a submit", was_on, now_on)
        return False
    applied = already_applied_message(page)
    if applied:
        ctx.extra.pop("submit_uncertain", None)
        raise AlreadyApplied(applied)
    if confirmation_showing(page):
        ctx.extra.pop("submit_uncertain", None)
        log.info("the page now shows a confirmation for the submit that could not be read the first time")
        return True
    if form_errors(page):
        # The form is still on screen complaining about its own fields, so the earlier submit plainly did
        # not go anywhere. Safe to carry on and send it once it is fixed.
        ctx.extra.pop("submit_uncertain", None)
        log.info("the form is still showing validation errors, so the uncertain submit did not go through")
        return False
    raise NeedsHuman(UNCERTAIN_AGAIN_MSG)


SUBMIT_MAX_ATTEMPTS = 4


def _filled_count(page: Any) -> int:
    """How many visible text-like boxes hold a value: a before/after measure around a submit."""
    try:
        return int(page.evaluate("""() => [...document.querySelectorAll(
            'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]):not([type=submit]):not([type=button]), textarea')]
            .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (e.value || '').trim(); }).length"""))
    except Exception:  # noqa: BLE001
        return -1


def submit_and_confirm(ctx: ApplyContext, names: tuple[str, ...], refill: Callable[[], None] | None = None,
                       click: Callable[[], None] | None = None) -> None:
    """Click submit, read what the page did about it, and act on that.

    Four outcomes, told apart on purpose. A confirmation returns. An employer saying the application is
    already in raises AlreadyApplied, which is news and not a failure. A rejection is diagnosed field by
    field (`field_errors`), the named controls are repaired (`repair_fields`) and the form is sent again —
    but only ever after something about it has actually changed. A submit that produced neither a
    confirmation nor a complaint is never repeated: it pauses for the candidate to look at the window.

    The retry budget is spent on distinct diagnoses rather than on attempts. Before this, a rejection was
    answered by refilling the form with the identical values and pressing Submit again, so one C3 AI
    application was refused seven times over with the same sentence, and one Presight application seven
    times with "There are 4 issues that need your attention" — whose four the run never read.

    `click` is for boards whose submit control cannot be pressed by its name. Workday is the one: its real
    buttons are aria-hidden behind a transparent div that carries the accessible name and swallows the click,
    so the default clicker would press something inert and then wait out the confirmation timeout.
    """
    page = ctx.page
    click = click or (lambda: click_submit(page, names))
    if NOT_APPLICATION_URL_RE.search(getattr(page, "url", "") or ""):
        # Never a submit on a page that is not the application: a Siemens run wandered onto the "disability
        # accommodation" contact form and sent it (application 438). Whatever led here, nothing is pressed.
        raise NeedsHuman(f"jobbot ended up on {(page.url or '')[:120]}, which is not the application form, and "
                         "will not submit anything there. Go back to the job in the open window, then click Continue.")
    if _guard_uncertain_submit(ctx, page):
        return
    submitted_at = datetime.now(timezone.utc)
    seen_rejections: set[str] = set()
    for attempt in range(1, SUBMIT_MAX_ATTEMPTS + 1):
        # A resume can land straight back on the code step (apply() re-runs from the top), so deal with it
        # before clicking anything: submitting again without the code just reprints the same prompt.
        prompt = verification_prompt(page)
        if prompt:
            handle_verification(ctx, prompt, submitted_at - timedelta(seconds=VERIFY_CLOCK_SKEW_S))
            ctx.step("Submitting with the verification code")
        else:
            ctx.step("Submitting" if attempt == 1 else "Form bounced — fixing what it rejected")
            submitted_at = datetime.now(timezone.utc)
        if _human_typing():
            time.sleep(random.uniform(1.2, 2.6))    # a person looks the form over before sending it
        filled_before = _filled_count(page)
        click()
        ctx.step("Waiting for confirmation")
        try:
            wait_for_confirmation(page, names=names)
            ctx.extra.pop("submit_uncertain", None)
            return
        except VerificationRequired:
            if attempt >= SUBMIT_MAX_ATTEMPTS:
                raise ApplyError("The form kept asking for a verification code")
            continue    # round the loop: the top of it fills the code and submits again
        except (AlreadyApplied, NeedsHuman):
            raise       # the application is in, or only the candidate can move this on
        except ApplyError as e:
            message = str(e)
            if spam_flagged(page) or _SPAM_FLAG_RE.search(message):
                # Ashby's "flagged as possible spam" after the press (GiGi, application 424): a verdict on the
                # browser, not on the form. Parked for a person's own Submit; never re-sent from here.
                raise NeedsHuman(SPAM_FLAG_MSG) from e
            if _confirmation_arrives(page):
                # The page was still between the form and its thank-you when it was read: what looked
                # like a complaint was the form being torn down (application 175, Rippling).
                ctx.extra.pop("submit_uncertain", None)
                return
            filled_after = _filled_count(page)
            if message.startswith("Form rejected") and filled_before >= 3 and filled_after <= 1:
                # Every box that was filled is empty again: the form reset itself after taking the submission
                # (Myticas' Scout Genius form, application 439), and its "Please fill out this field" is the
                # empty form, not a refusal. Refilling and pressing again would send it twice.
                log.warning("the form cleared itself after the submit (%d filled before, %d after); "
                            "treating it as possibly sent", filled_before, filled_after)
                message = "Submit not confirmed: the form reset itself after the press"
            if message.startswith("Submit not confirmed"):
                try:
                    from jobbot import mail
                    ctx.step("Checking the mailbox for the employer's receipt")
                    receipt = mail.fetch_application_receipt(
                        submitted_at - timedelta(seconds=VERIFY_CLOCK_SKEW_S), ctx.job.get("company", ""))
                except Exception as e2:  # noqa: BLE001
                    receipt = None
                    log.debug("receipt check: %s", e2)
                if receipt:
                    ctx.extra.pop("submit_uncertain", None)
                    log.info("the page showed no confirmation, but the mailbox did: %r", receipt[:120])
                    return
                ctx.extra["submit_uncertain"] = {"url": (getattr(page, "url", "") or "")[:300],
                                                 "step": _step_name(page)}
                log.warning("submit could not be confirmed; not sending it again on our own")
                raise NeedsHuman(UNCERTAIN_MSG)
            if not message.startswith("Form rejected") or attempt >= SUBMIT_MAX_ATTEMPTS:
                raise
            fields = field_errors(page)
            loose = form_errors(page)
            signature = rejection_signature(fields, loose) or _error_shape(message)
            summary = rejection_summary(fields, loose) or message[len("Form rejected: "):]
            if signature in seen_rejections:
                # The same complaint after a repair means the repair achieved nothing, and a further
                # identical submit would achieve nothing either.
                log_field_outline(page, summary)
                if _silent_bot_check(page, summary):
                    # Greenhouse's "There was an error processing your application", with an invisible
                    # reCAPTCHA on the page and every field valid: the site's own bot score refused the
                    # submit (application 249). Nothing on the form is wrong, and nothing here should try
                    # to get round it -- a person pressing Submit is what it is waiting for.
                    raise NeedsHuman("The site's invisible bot check turned the submit down (every field is "
                                     "filled). Press Submit yourself in the open window — the form is ready — "
                                     "then click 'Mark applied' once it confirms.")
                raise NeedsHuman(f"The form keeps refusing this application and jobbot has run out of ways "
                                 f"to fix it: {summary}. Correct it in the open window, then click "
                                 f"Continue — what you type there is remembered for next time.")
            seen_rejections.add(signature)
            changed = repair_fields(ctx, fields)
            if expand_hidden_choice_groups(page):
                changed = list(changed) + ["opened a collapsed choice list"]
            if refill is not None:
                refill()
            if not changed and (refill is None or attempt > 1):
                raise NeedsHuman(f"The form rejected this application and jobbot could not work out what to "
                                 f"change: {summary}. Fix it in the open window, then click Continue — "
                                 f"what you type there is remembered for next time.")
            log.warning("form rejected (%s); %s", summary[:150],
                        ("repaired " + "; ".join(changed))[:200] if changed else "re-running the fill passes")
