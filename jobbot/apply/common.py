"""Shared Playwright helpers for adapters. All helpers are defensive: an absent optional field never raises."""
from __future__ import annotations

import base64
import logging
import random
import os
import pathlib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from jobbot.apply.base import AlreadyApplied, ApplyContext, ApplyError, NeedsHuman

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
CONFIRM_TEXTS = (
    "thank you for applying", "thanks for applying", "application has been submitted", "application submitted",
    "your application was submitted", "we have received your application", "we've received your application",
    "thank you for your application", "thank you for your interest", "application received",
    "successfully submitted", "your application has been received",
    # the shorter forms the newer boards use
    "thanks for your application", "you have applied", "you've applied", "application sent",
    "application complete", "we got your application", "we've got your application", "your application is in",
    "you're all set", "you are all set", "application was sent", "has been sent to",
)
FORM_GONE_GRACE_S = 4.0     # a form that vanished after Submit must stay gone this long to count as sent
CONFIRM_URL_HINTS = ("confirmation", "thanks", "thank-you", "thankyou", "submitted", "success", "applied")

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
        el.click(timeout=SHORT)
        _type_value(el, value)
        return True
    except Exception as e:
        log.debug("fill_if_empty failed: %s", e)
        return False


def upload_resume(page: Any, cv_path: str, file_input: Any | None = None) -> bool:
    """Set the CV on a file input if none is attached yet. Picks the first visible-or-hidden file input near 'Resume/CV'."""
    if not cv_path:
        return False
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
            else:
                continue
            if not _wants_cv(el) and attached:
                # nothing names a CV beside this one; a second file input here is "other documents"
                pass
        except Exception as e:
            log.debug("resume upload attempt failed: %s", e)
    if attached:
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


def dismiss_cookie_banner(page: Any) -> bool:
    """Close a cookie consent bar. These are fixed to the bottom of the page and sit over the submit button
    (Palantir's Lever board is one), so leaving one up can make the final click land on the banner instead.
    Refusal options are tried before acceptance."""
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
                btn.first.click(timeout=SHORT)
                page.wait_for_timeout(500)
                log.info("dismissed a cookie banner with %r", label)
                return True
            except Exception:
                continue
    return False


def detect_captcha(page: Any, raise_: bool = True) -> bool:
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
    if found and raise_:
        raise NeedsHuman(CAPTCHA_MSG)
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
                return deepAll('input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select')
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
                for (const b of deepAll('button, input[type=submit], input[type=button], [role=button]')) {
                    if (!vis(b)) continue;
                    if (want.has(norm(deepText(b) || b.value)) || want.has(norm(deepAttr(b, 'aria-label')))) return true;
                }
                return false; }""", list(names)))
    except Exception:  # noqa: BLE001
        return True     # unknown: assume the form is still there


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
    while time.time() < deadline:
        try:
            url = page.url.lower()
            if url != start_url.lower() and any(h in url for h in CONFIRM_URL_HINTS):
                return True
            body = clean(page.evaluate("() => (document.body && document.body.innerText) || ''")).lower()
            if any(t in body for t in CONFIRM_TEXTS):
                return True
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
        text = clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))
    except Exception:
        return ""
    if not text:
        return ""
    m = _SUBMIT_BLOCKED_RE.search(text)
    if not m:
        return ""
    # Report the sentence it appeared in, plus the next one, which usually carries the detail.
    start = max(0, text.rfind(".", 0, m.start()) + 1)
    chunk = text[start:m.end() + 220].strip()
    return re.split(r"(?<=\.)\s(?=[A-Z])", chunk)[0][:300] if chunk else ""


# Announcements that share the markup of an error without being one. Boards put upload confirmations and
# save notices in the same [role=alert] live region as their validation messages, so "…successfully
# uploaded" came back as a form error — and a step that had just done exactly what was asked was reported
# as stuck on it.
_NOT_AN_ERROR_RE = re.compile(r"success|uploaded|saved\b|complete[ds]?\b|thank you|no errors", re.I)


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
                const sel = "[aria-invalid=true], .field-error, .error, [class*='error' i], [role=alert]";
                for (const el of deepAll(sel)) {
                    if (!vis(el)) continue;
                    const t = clean(el.innerText || el.textContent);
                    if (t && t.length < 160) out.push(t);
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
    const CTRL = "input,select,textarea,[role=combobox],[contenteditable='true']";
    const fillable = e => {
        if (!e || !e.matches || !e.matches(CTRL)) return false;
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
    const labelFor = e => {
        if (e.labels && e.labels.length) { const t = clean(e.labels[0].innerText); if (t) return t; }
        const by = e.getAttribute('aria-labelledby');
        if (by) { const root = e.getRootNode();
            for (const id of by.split(/\\s+/)) {
                const nd = root.getElementById ? root.getElementById(id) : document.getElementById(id);
                if (nd) { const t = clean(nd.innerText); if (t) return t; } } }
        const al = e.getAttribute('aria-label'); if (al) return clean(al);
        let p = e.parentElement, hops = 0;
        while (p && hops++ < 5) {
            const lab = p.querySelector('label, legend, [class*="label" i]');
            if (lab) { const t = clean(lab.innerText); if (t) return t.slice(0, 120); }
            p = p.parentElement;
        }
        return clean(e.getAttribute('placeholder') || e.getAttribute('name') || e.getAttribute('id'));
    };
    const kindOf = e => {
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
    for (const el of deepAll(ERR_SEL)) {
        if (!vis(el)) continue;
        if (fillable(el)) continue;          // an input flagged invalid is picked up on its own below
        const txt = el.innerText || el.textContent;
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
        found = page.evaluate("() => {" + DEEP_JS + _FIELD_ERRORS_JS)
    except Exception as e:  # noqa: BLE001 - diagnosis must never become the failure
        log.debug("field_errors failed: %s", e)
        return []
    out: list[dict] = []
    for f in found or []:
        if not isinstance(f, dict):
            continue
        msg = clean(str(f.get("message") or ""))
        if not msg or _NOT_AN_ERROR_RE.search(msg):
            continue
        out.append({"id": f.get("id"), "label": clean(str(f.get("label") or "")),
                    "kind": str(f.get("kind") or ""), "value": clean(str(f.get("value") or "")),
                    "required": bool(f.get("required")), "message": msg})
    return out


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


def _repair_one(ctx: ApplyContext, el: Any, f: dict) -> bool:
    """Act on one field the form rejected. True when the control now holds something different.

    The return value is the whole point: it is what tells the caller whether pressing Submit again could
    possibly produce a different result. Never let a NeedsHuman out of here quietly — a field the form
    insists on and jobbot cannot answer is exactly the question the candidate should be asked, with the
    site's own complaint attached to it.
    """
    page = ctx.page
    label, kind, before, msg = f["label"], f["kind"], f["value"], f["message"]

    # Asked through ctx.answer rather than through _ask: _ask lets an unanswerable question go by when the
    # control looks optional, and a control the form has just named in a validation message is not optional
    # whatever its markup says. This is the other half of the C3 AI failure — the field-of-study box read as
    # optional, the question was skipped, and the form then refused the submit over the empty box.
    if kind == "checkbox":
        if before == "checked":
            return False
        # A consent gate the form will not go in without ("You need to agree to the terms and conditions").
        ans = ctx.answer(label, ["Yes", "No"], "checkbox")
        if not ans or not re.match(r"^\s*(?:y|true|agree|accept|i )", ans, re.I):
            return False
        return bool(tick(el))

    if kind in ("radio", "file"):
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
        return bool(choose_combobox(page, el, ans))

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
        log.info("repairing %r (%s, holds %r): %s",
                 f["label"][:60], f["kind"], f["value"][:40], f["message"][:90])
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
        txt = el.evaluate(_LABEL_JS)
        return clean(txt)
    except Exception:
        return ""


_LABEL_JS = """e => {
    const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
    const vis = n => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    // An empty label names nothing, so each of the searches below passes over one and keeps looking.
    for (const l of (e.labels || [])) { const t = clean(l.innerText || l.textContent); if (t) return t; }
    const al = e.getAttribute('aria-labelledby');
    const root = e.getRootNode();
    const byId = id => (root.getElementById ? root.getElementById(id) : document.getElementById(id));
    if (al) { const t = al.split(/\\s+/).map(id => { const n = byId(id); return n ? (n.innerText || n.textContent) : ''; }).join(' '); if (clean(t)) return clean(t); }
    const aria = e.getAttribute('aria-label'); if (aria) return clean(aria);
    const id = e.id; if (id) { for (const l of root.querySelectorAll(`label[for="${CSS.escape(id)}"]`)) { const t = clean(l.innerText || l.textContent); if (t) return t; } }
    const wrap = e.closest('label'); if (wrap) { const t = clean(wrap.innerText || wrap.textContent); if (t) return t; }
    const fs = e.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) { const t = clean(lg.innerText || lg.textContent); if (t) return t; } }
    const ph = e.getAttribute('placeholder'); if (ph) return clean(ph);
    let p = e.parentElement; for (let i = 0; i < 7 && p; i++, p = p.parentElement) {
        // Every candidate at this level, not just the first: an empty one is furniture, and stopping on it
        // hid the real label three levels up. The nearest one ABOVE the control wins, so a wrapper holding
        // two fields lends each its own label rather than the first in the markup (see get_label_for).
        let above = '', below = '';
        for (const l of p.querySelectorAll('label, legend, .label, [class*="label" i], h3, h4, h5')) {
            if (l.contains(e)) continue;
            const t = clean(l.innerText || l.textContent);
            if (!t) continue;
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
            const t = clean(k.innerText || k.textContent);
            if (t && t.length <= 200 && vis(k)) return t;
        }
    }
    return clean(e.name || '');
}"""


def same_value(a: str, b: str) -> str:
    """True when two field values are the same answer — case, spacing and a trailing slash aside."""
    norm = lambda s: clean(s).rstrip("/").lower()   # noqa: E731
    return norm(a) == norm(b)


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
    if (!lbl) return '';
    return (lbl.innerText || lbl.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
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
        # Last resort, and the only one that reaches a label rendered outside the input's ancestry:
        # activating the <label> in script toggles the control it names exactly as a click would.
        el.evaluate("e => { const l = (e.labels && e.labels[0]) || e.closest('label');"
                    "        if (l) l.click();"
                    "        if (!e.checked) e.click(); }")
    except Exception as e:  # noqa: BLE001
        log.debug("tick: scripted click refused (%s)", str(e)[:100])
    return is_checked_now(el)


def check_choice(container: Any, answer: str) -> bool:
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


def combobox_options(page: Any, combo: Any, limit: int = 60) -> list[str]:
    """Open a react-select / aria combobox and read its option texts, then close it.

    `limit` is a runaway guard, not a statement about how many options a control has. The default is sized
    for the yes/no and seniority lists that make up almost every dropdown on an application form, and a
    country list runs to ~240 — so a caller that must see every option has to raise it. Nothing does yet:
    the dial-code picker, the one control known to need the whole list, reads its rows through _dial_rows
    so it can click one without closing the list first.
    """
    opts: list[str] = []
    try:
        combo.click(timeout=SHORT)
        page.wait_for_timeout(400)
        listbox = page.locator("[role=listbox]:visible, [role=option]:visible")
        items = page.locator("[role=option]:visible")
        for i in range(min(items.count(), limit)):
            t = clean(items.nth(i).inner_text())
            if t:
                opts.append(t)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        _ = listbox
    except Exception:
        pass
    return opts


def choose_combobox(page: Any, combo: Any, answer: str) -> bool:
    """Click the combobox, type the answer, pick the matching option.

    Exact match first; else the only option left after typing; else the first option that starts with
    the answer. A blind Enter used to take whatever react-select had highlighted, which on a list that did
    not filter ("Yes" typed into a list of countries) was the wrong answer written with no error. Returns
    True only when the control reads back a value afterwards.
    """
    try:
        combo.click(timeout=SHORT)
        page.wait_for_timeout(200)
        try:
            combo.fill("", timeout=SHORT)
        except Exception:
            pass
        page.keyboard.type(answer, delay=20)
        page.wait_for_timeout(600)
        items = page.locator("[role=option]:visible")
        texts = [clean(items.nth(i).inner_text()) for i in range(min(items.count(), 60))]
        want = clean(answer).lower()
        pick = next((i for i, t in enumerate(texts) if t.lower() == want or same_option(t, answer)), None)
        if pick is None and len(texts) == 1 and want and want in texts[0].lower():
            pick = 0
        if pick is None:
            pick = next((i for i, t in enumerate(texts) if want and t.lower().startswith(want)), None)
        if pick is not None:
            items.nth(pick).click(timeout=SHORT)
        else:
            page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        if combobox_value(combo):
            return True
        # Some widgets swallow the typed text and only take a click on the opened list
        combo.click(timeout=SHORT)
        page.wait_for_timeout(300)
        items = page.locator("[role=option]:visible")
        for i in range(min(items.count(), 60)):
            text = clean(items.nth(i).inner_text())
            if text.lower() == want or same_option(text, answer):
                items.nth(i).click(timeout=SHORT)
                page.wait_for_timeout(300)
                break
        else:
            page.keyboard.press("Escape")
        return bool(combobox_value(combo))
    except Exception as e:
        log.debug("choose_combobox failed: %s", e)
        return False


def combobox_value(combo: Any) -> str:
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
    raise ApplyError("Submit button not found")


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
                return /\\b(country|dial(l?ing)?|area)\\s*code\\b/i.test(lbl);
            }"""))
    except Exception:  # noqa: BLE001
        return False


def dial_in(text: str) -> str:
    """The dial code written into `text` ("Bangladesh (+880)" -> "880", "+ 880" -> "880"); '' when none.

    One place knows how a form writes a dial code, because three of them used to guess at it separately.
    """
    m = re.search(r"\+\s*(\d{1,4})", text or "")
    return m.group(1) if m else ""


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
        return page.evaluate("() => {" + DEEP_JS + """
            const sel = %r;
            for (const el of deepAll(sel)) {
                const shown = el.tagName === 'SELECT'
                    ? ((el.selectedOptions && el.selectedOptions[0]) ? el.selectedOptions[0].textContent : '')
                    : (el.innerText || el.value || '');
                const m = (shown || '').match(/\\+\\s*(\\d{1,4})/);
                if (m) return m[1];
            }
            return ''; }""" % _DIAL_SHOWN_SEL) or ""
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
                        if dial_in(clean(rows.nth(i).inner_text())) == code), None)
            if hit is not None:
                rows.nth(hit).click(timeout=SHORT)
                page.wait_for_timeout(400)
                return True
            page.keyboard.press("Escape")
        except Exception as e:  # noqa: BLE001
            log.debug("_click_dial_option(%s) failed: %s", typed, e)
    return False


def set_dial_code(page: Any, phone: str) -> str:
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
                texts = [clean(rows.nth(i).inner_text()) for i in range(min(rows.count(), DIAL_OPTIONS))]
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
    return ""


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
        return ""
    if re.sub(r"\D", "", phone or "").startswith(held):
        return held     # already resting on our country — the Bangladeshi tenant of a Bangladeshi employer
    return set_dial_code(page, phone) or select_dial_country(page, country, phone) or held


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
            text = clean(row.inner_text())
            if pattern.search(text) and (not want or ("+" + want[:3] in text.replace(" ", "")) or "+" not in text):
                row.click(timeout=SHORT)
                page.wait_for_timeout(400)
                dial = dial_code_on_page(page)
                log.info("dial-code picker switched to %r -> +%s", country, dial or "?")
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
        best, score = "", 0
        for o in options:
            n = normalize_question(o)
            if head and head not in n:
                continue
            sc = fuzz.token_set_ratio(n, want)
            if sc > score:
                best, score = o, sc
        if score >= 80:
            return best
    except Exception:  # noqa: BLE001
        pass
    return ""


_OTHER_RE = re.compile(r"^(other|others|not listed|n/?a)$", re.I)


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
_ONE_TIME_PASSWORD_RE = re.compile(r"one[\s-]*time|verification|security|confirmation|\botp\b|2fa", re.I)


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


def _ask(ctx: ApplyContext, el: Any, label: str, options: list[str] | None, kind: str,
         required_el: Any = None):
    """The resolver's answer, or None when there is no answer and the form does not need one."""
    try:
        return ctx.answer(label, options, kind)
    except NeedsHuman:
        if is_required(required_el if required_el is not None else el):
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
            const star = t => /\*|\brequired\b|\bobligatoire\b|\bpflichtfeld\b|\bobligatorio\b/i.test(t || '');
            // the control's own label first: Greenhouse and most React boards put the * there, and the
            // nearest <div> around a react-select input is a wrapper with no label in it at all
            for (const l of (e.labels ? Array.from(e.labels) : [])) if (star(l.innerText)) return true;
            const by = e.getAttribute('aria-labelledby');
            const root = e.getRootNode();
            if (by) for (const id of by.split(/\s+/)) {
                const n = root.getElementById ? root.getElementById(id) : document.getElementById(id);
                if (n && star(n.innerText)) return true; }
            const wrap = e.closest("[data-automation-id^='formField'], [class*='field-wrapper' i], [class*='form-field' i], .field, fieldset, li, div");
            const lab = wrap ? wrap.querySelector('label, legend, [class*="label" i]') : null;
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


def answer_and_set(ctx: ApplyContext, el: Any, label: str, kind: str, options: list[str] | None = None,
                   container: Any | None = None) -> None:
    """Ask the resolver for `label` and write the answer into the control according to `kind`."""
    page = ctx.page
    if is_verification_control(el, label):
        log.debug("skipping verification control %r; it is filled after submit", label)
        return
    if kind == "text" or kind == "textarea":
        existing = current_value(el)
        if existing:
            ctx.seen(existing, label, kind=kind, default=text_is_default(el))
            return
        if kind == "textarea" and COVER_LABEL_RE.search(label or ""):
            letter = cover_letter_text(ctx)
            if letter:
                fill_if_empty(el, letter)
                return
        if kind == "text" and is_number_box(el):
            # The control decides the shape of the answer: "Negotiable" is a fine answer to a salary box and
            # no answer at all to a salary *number* box. The resolver turns what it knows into a number where
            # it honestly can ("None" notice period -> 0 weeks) and asks the user where it cannot.
            kind = "number"
            clear_bad_input(el)     # a text answer from an earlier pass, still blocking the submit
        ans = _ask(ctx, el, label, None, kind)
        if ans is None:
            return
        if has_suggestions(el):
            # A box that only accepts what its own list offers. Typing the true answer at it is what fails:
            # Greenhouse refused "Computer Science & Engineering" with "Please select a school, degree, and
            # field of study from the suggestions" (application 145, C3 AI).
            fill_from_suggestions(page, el, ans, _answer_alternatives(ctx, label))
            return
        if fill_if_empty(el, ans) or kind == "number":
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
    elif kind == "select":
        opts = options or select_options(el)
        existing = clean(el.evaluate("e => e.selectedIndex > 0 ? e.options[e.selectedIndex].textContent : ''"))
        if existing:
            ctx.seen(existing, label, kind=kind, options=opts, default=select_is_default(el))
            return
        if len(opts) == 1:
            # "— Make a Selection — / Continue" (iCIMS's consent gate): one real choice is not a question,
            # and asking the user to pick the only option there is stops a run for nothing.
            log.info("select %r offers one option; taking %r", label, opts[0])
            if not choose_select(el, opts[0], opts):
                raise ApplyError(f"Could not select {opts[0]!r} for {label!r}")
            return
        ans = _ask(ctx, el, label, opts, kind)
        if ans is None:
            return
        if not choose_select(el, ans, opts):
            raise ApplyError(f"Could not select {ans!r} for {label!r}")
    elif kind == "combobox":
        existing = combobox_value(el)
        if existing:
            ctx.seen(existing, label, kind=kind, placeholder=combobox_is_placeholder(el))
            return
        opts = options if options is not None else combobox_options(page, el)
        ans = _ask(ctx, el, label, opts or None, kind)
        if ans is None:
            return
        if not choose_combobox(page, el, ans):
            raise ApplyError(f"Could not choose {ans!r} for {label!r}")
    elif kind in ("radio", "checkbox"):
        cont = container if container is not None else el
        if choice_checked(cont):
            ctx.seen(checked_choice_label(cont), label, kind=kind, options=options or choice_options(cont),
                     default=choice_is_default(cont))
            return
        opts = options or choice_options(cont)
        ans = _ask(ctx, el, label, opts, kind, cont)
        if ans is None:
            return
        if not check_choice(cont, ans):
            raise ApplyError(f"Could not tick {ans!r} for {label!r}")
    elif kind == "file":
        return  # the CV goes through upload_resume; the cover letter through fill_cover_letter


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
    fill_if_empty(el, value)
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
            for ch, box in zip(code, boxes):
                box.click(timeout=SHORT)
                box.fill("", timeout=SHORT)      # clear first: a retry must not append to what is there
                box.fill(ch, timeout=SHORT)
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


def handle_verification(ctx: ApplyContext, prompt: dict, submitted_at) -> None:
    """Satisfy the emailed-code step: read the code from the mailbox, or ask the user for it once.

    Raises NeedsHuman when the code cannot be read, so the application parks with the browser open instead of
    failing — the form is filled and one code away from being submitted.
    """
    from jobbot import mail

    length, to = prompt.get("length") or 8, prompt.get("to") or ""
    ctx.step("Waiting for the verification code by email")
    code = mail.fetch_code(submitted_at, length=length,
                           hints=(ctx.job.get("company", ""), ctx.job.get("ats", "")))
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
    if not fill_verification_code(ctx.page, code):
        raise ApplyError("Could not type the verification code into the form")
    ctx.step("Verification code entered")


def confirmation_showing(page: Any) -> bool:
    """True when the page is, right now, telling the candidate the application went in.

    The cheap half of wait_for_confirmation, for callers that only need to know what is on screen.
    """
    try:
        url = (page.url or "").lower()
        if any(h in url for h in CONFIRM_URL_HINTS):
            return True
        body = clean(page.evaluate("() => (document.body && document.body.innerText) || ''")).lower()
        return any(t in body for t in CONFIRM_TEXTS)
    except Exception:  # noqa: BLE001
        return False


def _guard_uncertain_submit(ctx: ApplyContext, page: Any) -> bool:
    """Stand between a form jobbot already sent once and a second press of its Submit button.

    A submit that produced no confirmation and no complaint may well have gone through — the form-gone
    grace period in wait_for_confirmation exists because boards word their thank-you pages in a hundred
    ways. Sending it again would put a duplicate application in front of a real employer, so from here on
    that decision is the candidate's. Returns True when the application turns out to be in already.
    """
    if not ctx.extra.get("submit_uncertain"):
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
            if message.startswith("Submit not confirmed"):
                ctx.extra["submit_uncertain"] = {"url": (getattr(page, "url", "") or "")[:300]}
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
                raise NeedsHuman(f"The form keeps refusing this application and jobbot has run out of ways "
                                 f"to fix it: {summary}. Correct it in the open window, then click "
                                 f"Continue — what you type there is remembered for next time.")
            seen_rejections.add(signature)
            changed = repair_fields(ctx, fields)
            if refill is not None:
                refill()
            if not changed and (refill is None or attempt > 1):
                raise NeedsHuman(f"The form rejected this application and jobbot could not work out what to "
                                 f"change: {summary}. Fix it in the open window, then click Continue — "
                                 f"what you type there is remembered for next time.")
            log.warning("form rejected (%s); %s", summary[:150],
                        ("repaired " + "; ".join(changed))[:200] if changed else "re-running the fill passes")
