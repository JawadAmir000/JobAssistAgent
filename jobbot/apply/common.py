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
            """() => {
                const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                    return r.width > 30 && r.height > 30 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
                const hit = s => /recaptcha|hcaptcha|turnstile|arkose|funcaptcha|geetest|datadome|captcha-delivery|perimeterx|px-captcha|awswaf|challenges\.cloudflare/i.test(s || '');
                for (const f of document.querySelectorAll('iframe')) {
                    if ((hit(f.src) || hit(f.title) || hit(f.className)) && vis(f)) {
                        // The invisible reCAPTCHA badge is not blocking. Keyed on the frame's own URL
                        // (mirrors _PASSIVE_FRAME_RE) and on Google's badge container, never on page prose.
                        // A size=normal|compact anchor (the tick-box) or a visible bframe (the image
                        // challenge) does not match either test, falls through, and is reported.
                        if (/recaptcha\/(api2|enterprise)\/anchor\?[^#]*\bsize=invisible\b/i.test(f.src)
                            || f.closest('.grecaptcha-badge')) continue;
                        return true;
                    }
                }
                for (const d of document.querySelectorAll('.h-captcha, .cf-turnstile, .g-recaptcha:not([data-size=invisible])')) {
                    if (vis(d)) return true;
                }
                // Image challenges that name themselves in prose rather than in a known class or src.
                // Their own visibility floor: the widget test wants a box bigger than 30x30, but this text
                // is often a single-line <label> about 20px tall, which that floor rejects.
                const visText = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                    return r.width > 20 && r.height > 8 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
                const prompts = /click on the shape|select all (images|squares)|pick the (odd|different)|verify you are (a )?human|i'?m not a robot|solve the (puzzle|challenge)|type (the |below )?image text|enter the (captcha|characters|text) (shown|above|below|in the image)|captcha code|slide (right|the slider) to|drag the (piece|slider)|verification required|secure your access/i;
                for (const el of document.querySelectorAll('div,section,form,p,h1,h2,h3,span,label,legend')) {
                    const t = (el.innerText || '').trim();
                    if (t && t.length < 200 && prompts.test(t) && visText(el)) return true;
                }
                // ...and the ones that only say it in the box itself: Zoho Recruit's challenge names
                // itself in a placeholder, which no scan of element text can ever see.
                for (const el of document.querySelectorAll('input[placeholder], input[aria-label]')) {
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
            """() => { const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                return [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select')]
                    .filter(vis).length; }"""))
    except Exception:  # noqa: BLE001
        return -1


def _button_named_visible(page: Any, names: tuple[str, ...]) -> bool:
    """True when a visible button carries one of `names` — the form's own Submit/Next is still on screen."""
    if not names:
        return False
    try:
        return bool(page.evaluate(
            """(names) => { const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const want = new Set(names.map(norm));
                for (const b of document.querySelectorAll('button, input[type=submit], input[type=button], [role=button]')) {
                    if (!vis(b)) continue;
                    if (want.has(norm(b.innerText || b.value)) || want.has(norm(b.getAttribute('aria-label')))) return true;
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
            blocked = submit_blocked_message(page)
            if blocked:
                # The site refused outright (application quota, duplicate, closed posting). Refilling and
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
# matches. Deliberately reCAPTCHA-only: hCaptcha's invisible checkbox frame renders 0x0 and is already under
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
            box = fr.frame_element().bounding_box()
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
            """() => {
                const vis = el => { const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
                for (const el of document.querySelectorAll("button, input[type=submit]")) {
                    if (!vis(el)) continue;
                    const busy = el.disabled || el.getAttribute('aria-busy') === 'true'
                              || el.getAttribute('aria-disabled') === 'true';
                    if (!busy) continue;
                    const t = ((el.innerText || el.value || '') + ' ' + (el.getAttribute('aria-label') || ''))
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
            """() => {
                const vis = el => { const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
                const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
                const out = [];

                // (1) messages the page rendered itself
                const sel = "[aria-invalid=true], .field-error, .error, [class*='error' i], [role=alert]";
                for (const el of document.querySelectorAll(sel)) {
                    if (!vis(el)) continue;
                    const t = clean(el.innerText || el.textContent);
                    if (t && t.length < 160) out.push(t);
                }

                // (2) the browser's own constraint validation, which renders outside the DOM
                const labelFor = e => {
                    if (e.labels && e.labels.length) return clean(e.labels[0].innerText);
                    const al = e.getAttribute('aria-label'); if (al) return clean(al);
                    const wrap = e.closest('li,div,fieldset,label');
                    if (wrap) { const t = clean(wrap.innerText); if (t) return t.slice(0, 80); }
                    return clean(e.getAttribute('name') || e.getAttribute('id'));
                };
                for (const e of document.querySelectorAll('input, select, textarea')) {
                    if (e.disabled || e.type === 'hidden') continue;
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
    if (e.labels && e.labels.length) return clean(e.labels[0].innerText || e.labels[0].textContent);
    const al = e.getAttribute('aria-labelledby');
    if (al) { const t = al.split(/\\s+/).map(id => { const n = document.getElementById(id); return n ? (n.innerText || n.textContent) : ''; }).join(' '); if (clean(t)) return clean(t); }
    const aria = e.getAttribute('aria-label'); if (aria) return clean(aria);
    const id = e.id; if (id) { const l = document.querySelector(`label[for="${CSS.escape(id)}"]`); if (l) return clean(l.innerText || l.textContent); }
    const wrap = e.closest('label'); if (wrap) return clean(wrap.innerText || wrap.textContent);
    const fs = e.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) return clean(lg.innerText || lg.textContent); }
    const ph = e.getAttribute('placeholder'); if (ph) return clean(ph);
    let p = e.parentElement; for (let i = 0; i < 7 && p; i++, p = p.parentElement) {
        const l = p.querySelector('label, legend, .label, [class*="label" i], h3, h4, h5');
        if (l && !l.contains(e)) return clean(l.innerText || l.textContent);
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


def choose_select(el: Any, answer: str, options: list[str]) -> bool:
    """select_option by visible label, falling back to a case-insensitive match."""
    try:
        cur = clean(el.evaluate("e => e.selectedIndex > 0 ? e.options[e.selectedIndex].textContent : ''"))
        if cur:
            return True
        for o in options:
            if o.lower() == answer.lower():
                el.select_option(label=o, timeout=MEDIUM)
                return True
        el.select_option(label=answer, timeout=MEDIUM)
        return True
    except Exception as e:
        log.debug("choose_select failed: %s", e)
        return False


def check_choice(container: Any, answer: str) -> bool:
    """Tick the radio/checkbox inside `container` whose label equals `answer` (case-insensitive)."""
    try:
        inputs = container.locator("input[type=radio], input[type=checkbox]")
        n = inputs.count()
        for i in range(n):
            inp = inputs.nth(i)
            label = get_label_for(inp)
            if label.lower() == answer.lower() or clean(inp.get_attribute("value") or "").lower() == answer.lower():
                if not inp.is_checked():
                    try:
                        inp.check(timeout=MEDIUM)
                    except Exception:
                        inp.evaluate("e => { e.click(); }")
                return True
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


def combobox_options(page: Any, combo: Any) -> list[str]:
    """Open a react-select / aria combobox and read its option texts, then close it."""
    opts: list[str] = []
    try:
        combo.click(timeout=SHORT)
        page.wait_for_timeout(400)
        listbox = page.locator("[role=listbox]:visible, [role=option]:visible")
        items = page.locator("[role=option]:visible")
        for i in range(min(items.count(), 60)):
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
        pick = next((i for i, t in enumerate(texts) if t.lower() == want), None)
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
            if clean(items.nth(i).inner_text()).lower() == want:
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
    """Current value shown by a combobox (react-select single-value or the input text).

    react-select renders the chosen value as a sibling of the input's own wrapper:

        div.select__control > div.select__value-container > [div.select__single-value] + div.select__input-container > input

    The old lookup took the nearest ancestor whose class contained "select" — the input-container — and
    searched inside it, where the value never is. Every answered Greenhouse dropdown therefore read back as
    empty: the walker re-asked it on each pass, and one the user had chosen by hand in the window was
    asked about again after Continue. Walk up to the value container (or the control) instead.
    """
    try:
        v = current_value(combo)
        if v:
            return v
        return clean(combo.evaluate(
            """e => {
                const c = e.closest('[class*="value-container"]') || e.closest('[class*="control"]')
                    || e.closest('[class*="select"]') || e.parentElement;
                const sv = c && c.querySelector('[class*="single-value"], [class*="singleValue"], [class*="multi-value"], [class*="multiValue"]');
                if (sv && sv.textContent.trim()) return sv.textContent;
                // aria-style comboboxes name their choice on the control itself
                const t = e.getAttribute('aria-valuetext') || e.getAttribute('data-value') || '';
                return t;
            }"""))
    except Exception:
        return ""


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
def dial_code_on_page(page: Any) -> str:
    """The country dial code a separate control on this form already holds ("Bangladesh (+880)" -> "880").

    Only Workday-style forms split the number in two. When they do, the international number facts.yaml
    carries is not a valid answer for the Phone Number box beside it: Workday rejects "+8801XXXXXXXXX" with
    "Enter a valid format for Phone Number" and stops the step.
    """
    try:
        return page.evaluate("""() => {
            const sel = "[data-automation-id='selectedItem'], [data-automation-id*='countryPhoneCode' i],"
                      + " [class*='country' i] option:checked, select option:checked,"
                      + " button[aria-label*='country' i], button[aria-label*='pays' i], button[aria-label*='dial' i],"
                      + " button[aria-label*='indicatif' i], [class*='dial' i], [class*='country-code' i], [class*='countrycode' i]";
            for (const el of document.querySelectorAll(sel)) {
                const m = ((el.innerText || el.value || '')).match(/\\+\\s*(\\d{1,4})/);
                if (m) return m[1];
            }
            return ''; }""") or ""
    except Exception:  # noqa: BLE001
        return ""


_DIAL_PICKER = ("button[aria-label*='country' i], button[aria-label*='pays' i], button[aria-label*='dial' i], "
                "button[aria-label*='indicatif' i], [class*='country-code' i] button, [class*='countrycode' i] button, "
                "[class*='dial' i] button, .iti__selected-flag, .iti__selected-country")


def select_dial_country(page: Any, country: str, phone: str = "") -> str:
    """Switch a form's separate country-code picker to `country`, returning the dial code it then shows.

    SmartRecruiters (and the intl-tel-input widget many career sites use) defaults the picker to the job's
    country: a Montreal posting shows "+1", and a Bangladeshi number typed beside it is rejected as invalid.
    Best effort: a picker this cannot drive is left as it is and '' is returned, and the caller sends the
    number in full.
    """
    if not country:
        return ""
    want = re.sub(r"[^\d]", "", phone or "")[:4]
    try:
        picker = page.locator(_DIAL_PICKER).first
        if not is_visible_now(picker):
            return ""
        picker.click(timeout=SHORT)
        page.wait_for_timeout(500)
        # Searchable pickers take typing; the plain list is scanned for the country's name
        try:
            page.keyboard.type(country, delay=20)
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass
        rows = page.locator("[role=option]:visible, li:visible, [role=menuitem]:visible")
        n = min(rows.count(), 400)
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
    r"|leave\s+(?:this|it)\s+(?:field\s+)?(?:blank|empty)", re.I)


def is_honeypot(el: Any, label: str = "") -> bool:
    """True for a control no human would fill, whatever its label says it wants."""
    if label and _HONEYPOT_RE.search(label):
        return True
    try:
        if (el.get_attribute("aria-hidden") or "").lower() == "true":
            return True
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
        password = credentials.account_password()
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
            if (by) for (const id of by.split(/\s+/)) { const n = document.getElementById(id); if (n && star(n.innerText)) return true; }
            const wrap = e.closest("[data-automation-id^='formField'], [class*='field-wrapper' i], [class*='form-field' i], .field, fieldset, li, div");
            const lab = wrap ? wrap.querySelector('label, legend, [class*="label" i]') : null;
            if (lab) return star(lab.innerText);
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
            ctx.seen(existing, label)
            return
        if kind == "textarea" and COVER_LABEL_RE.search(label or ""):
            letter = cover_letter_text(ctx)
            if letter:
                fill_if_empty(el, letter)
                return
        ans = _ask(ctx, el, label, None, kind)
        if ans is not None:
            fill_if_empty(el, ans)
    elif kind == "select":
        opts = options or select_options(el)
        existing = clean(el.evaluate("e => e.selectedIndex > 0 ? e.options[e.selectedIndex].textContent : ''"))
        if existing:
            ctx.seen(existing, label)
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
            ctx.seen(existing, label)
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
            ctx.seen(checked_choice_label(cont), label)
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
    """Put the letter in whatever the form offers: a textarea, or a file input that wants a document."""
    page = ctx.page
    text = cover_letter_text(ctx)
    if not text:
        return False
    # A textarea is the better target: it keeps the letter readable in the ATS rather than as an attachment.
    for sel in ("textarea[name*='cover' i]", "textarea[id*='cover' i]", "#cover_letter_text",
                "textarea[aria-label*='cover' i]", "textarea[placeholder*='cover' i]"):
        try:
            ta = page.locator(sel).first
            if is_visible_now(ta):
                if current_value(ta):
                    return True     # already filled; apply() is re-run after every pause
                ta.fill(text, timeout=MEDIUM)
                log.info("cover letter written into %s", sel)
                return True
        except Exception:
            continue
    return upload_cover_letter(ctx, text)


def upload_cover_letter(ctx: ApplyContext, text: str) -> bool:
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
    r"|enter the (?:\d+[- ])?(?:character |digit )?code"
    r"|security code"
    r"|confirm (?:that )?you'?re (?:a )?human"
    r"|check your (?:email|inbox) for (?:a|the|your) code", re.I)
_VERIFY_LEN_RE = re.compile(r"(\d+)[-\s]*(?:character|digit)", re.I)
_VERIFY_TO_RE = re.compile(r"sent to\s+([^\s,;]+@[^\s,;.]+\.[A-Za-z]{2,})", re.I)
VERIFY_INPUTS = ("input[name*='security' i], input[name*='verification' i], input[name*='confirmation' i], "
                 "input[id*='security' i], input[id*='verification' i], input[autocomplete='one-time-code'], "
                 "input[inputmode='numeric'][maxlength='1'], input[maxlength='1']")


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
    try:
        boxes = page.locator(VERIFY_INPUTS)
        if not boxes.count() or not is_visible_now(boxes.first):
            return None
    except Exception:
        return None
    m = _VERIFY_LEN_RE.search(text)
    length = int(m.group(1)) if m and 3 <= int(m.group(1)) <= 12 else 0
    if not length:
        try:
            length = max(1, page.locator("input[maxlength='1']:visible").count()) or 8
        except Exception:
            length = 8
    to = ""
    m = _VERIFY_TO_RE.search(text)
    if m:
        to = m.group(1).rstrip(".")
    return {"length": length, "to": to}


def fill_verification_code(page: Any, code: str) -> bool:
    """Type `code` into the form, whether it is one input or one box per character."""
    code = (code or "").strip()
    if not code:
        return False
    try:
        singles = page.locator("input[maxlength='1']:visible")
        n = singles.count()
        if n >= len(code):
            for i, ch in enumerate(code):
                box = singles.nth(i)
                box.click(timeout=SHORT)
                box.fill("", timeout=SHORT)      # clear first: a retry must not append to what is there
                box.fill(ch, timeout=SHORT)
            page.wait_for_timeout(300)
            return True
        one = page.locator(VERIFY_INPUTS).first
        if is_visible_now(one):
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


def submit_and_confirm(ctx: ApplyContext, names: tuple[str, ...], refill: Callable[[], None] | None = None,
                       click: Callable[[], None] | None = None) -> None:
    """Click submit and wait for confirmation. If the form bounces with validation errors, run `refill` (the
    adapter's idempotent fill passes) and submit once more before giving up — a field the app dropped after we
    filled it is recovered instead of reported.

    `click` is for boards whose submit control cannot be pressed by its name. Workday is the one: its real
    buttons are aria-hidden behind a transparent div that carries the accessible name and swallows the click,
    so the default clicker would press something inert and then wait out the confirmation timeout.
    """
    page = ctx.page
    click = click or (lambda: click_submit(page, names))
    submitted_at = datetime.now(timezone.utc)
    for attempt in (1, 2, 3):
        # A resume can land straight back on the code step (apply() re-runs from the top), so deal with it
        # before clicking anything: submitting again without the code just reprints the same prompt.
        prompt = verification_prompt(page)
        if prompt:
            handle_verification(ctx, prompt, submitted_at - timedelta(seconds=VERIFY_CLOCK_SKEW_S))
            ctx.step("Submitting with the verification code")
        else:
            ctx.step("Submitting" if attempt == 1 else "Form bounced — refilling and submitting again")
            submitted_at = datetime.now(timezone.utc)
        if _human_typing():
            time.sleep(random.uniform(1.2, 2.6))    # a person looks the form over before sending it
        click()
        ctx.step("Waiting for confirmation")
        try:
            wait_for_confirmation(page, names=names)
            return
        except VerificationRequired:
            if attempt == 3:
                raise ApplyError("The form kept asking for a verification code")
            continue    # round the loop: the top of it fills the code and submits again
        except ApplyError as e:
            if attempt >= 2 or refill is None or not str(e).startswith("Form rejected"):
                raise
            log.warning("form rejected on first submit (%s); refilling and retrying", e)
            refill()
