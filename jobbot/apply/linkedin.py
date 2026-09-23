"""LinkedIn adapter — resolves a listing to the employer's real ATS, then hands off to that adapter.

LinkedIn is not an ATS: a listing is either "Easy Apply" (the form lives on LinkedIn, behind a login) or
"offsite" (the Apply button opens the employer's Greenhouse/Lever/Ashby/… page). The public job page does not
expose the offsite URL — jobspy's `job_url_direct` is always empty here because the `<code id="applyUrl">`
block it scrapes no longer exists, and the guest page only carries a sign-in redirect. The only reliable way
to get it is to click Apply in a signed-in browser and read where LinkedIn sends us.

So: click Apply, capture the destination, re-detect the ATS, and delegate. Anything we cannot drive (an
unsupported destination, a sign-in wall) raises NeedsHuman with a specific instruction rather than
dead-ending the application.

Easy Apply used to be on that list, and it was the most expensive entry on it: eleven applications across
five employers stopped with "complete it in the browser window" — more than any other single blocker in the
issues table. It is walked now, by EasyApplyWalker below.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from urllib.parse import urlparse

from jobbot.apply import common as c, navigator
from jobbot.apply.base import (Adapter, AlreadyApplied, ApplyContext, ApplyError, NeedsHuman, detect_ats,
                               get_adapter_for, register)
from jobbot.apply.generic import GenericFormAdapter

log = logging.getLogger(__name__)

NAV_TIMEOUT = 30000   # ms - explicit; the runner's 8s default is too tight for an ATS cold load
POPUP_TIMEOUT = 12000

# The visible Apply control is named by role; the elements carrying 'apply-link-*' tracking names are the
# sign-in modal's own dismiss X and "Join now" link, which must never be clicked.
# Signed in, the label carries a suffix ("Apply on company website", "Easy Apply to this job"); signed out
# it is the bare word. Anchor the start, let the rest run on — an exact match sees neither member label.
APPLY_NAME = re.compile(r"^\s*(easy\s+)?apply\b", re.I)
APPLY_FALLBACK = ("button#topbar-apply, button.apply-button, "
                  "[data-tracking-control-name*='apply']"
                  ":not([data-tracking-control-name*='dismiss'])"
                  ":not([data-tracking-control-name*='join-link'])")
SIGN_IN_WALL = "[data-tracking-control-name*='sign-in-modal'], a[href*='linkedin.com/login']"
# The signed-in UI carries no tracking attributes and hashes its class names per build, so neither can be
# selected on. aria-label is the one stable hook, and it also says which flow the job uses.
MEMBER_APPLY = "a[aria-label*='apply' i], button[aria-label*='apply' i]"
EASY_LABEL = re.compile(r"easy\s+apply", re.I)
CLOSED = ("no longer accepting applications", "not currently accepting applications",
          "no longer available")
SIGNED_OUT = "LinkedIn hides the employer's application link behind its sign-in wall. Sign in to LinkedIn in " \
             "the browser window, then click Continue — the session is saved, so this is a one-off."


@register
class LinkedInAdapter(Adapter):
    ats = "linkedin"
    needs_account = True

    def apply(self, ctx: ApplyContext) -> None:
        page = ctx.page
        c.require_open(page)

        # A resume re-enters apply() on whatever page the run stopped on. Once Apply has been followed that
        # page is the employer's form, half filled — re-resolving it would reload the page and throw away
        # every answer the user just came back to give. Hand straight to the ATS adapter for where we are.
        if not self._on_linkedin(page):
            ctx.step("Continuing on the employer's form")
            self._delegate(ctx, page.url, navigate=False)
            return

        ctx.step("Checking how this job accepts applications")

        # An Easy Apply modal that is already open is this run coming back to a half-filled application —
        # the user has just answered the question it paused on. Before anything else, because everything
        # below can navigate: the reload two lines down would throw the modal away along with every field
        # in it, and the run would start again from a blank step 1. That is what "it starts from the
        # beginning" looks like from the outside.
        if EasyApplyWalker.modal_open(page):
            EasyApplyWalker().apply(ctx)
            return

        # A sign-in the user performs in this window does not re-render the page behind it: the DOM stays
        # the signed-out one, Apply stays inside the inert modal, and a resume re-reads that same stale
        # page and raises the same NeedsHuman forever. One reload re-renders it under whatever session the
        # context now holds. Only when Apply is missing, so the signed-in first pass pays nothing.
        if self._apply_control(page) is None:
            self._reload(page, ctx)

        # A closed posting has no Apply button for the same reason a stale page has none, and telling the
        # user to "apply in the window" sends them hunting for a control that does not exist.
        if closed := self._closed(page):
            raise ApplyError(f"LinkedIn says this posting is closed ({closed}).")

        # Before either path goes looking for an Apply control: LinkedIn removes it once the application
        # is in, so "no Apply button" and "already applied" look identical from here (see applied_notice).
        if notice := applied_notice(page):
            raise AlreadyApplied(f"LinkedIn says this application is already in ({notice}).")

        if self._is_easy_apply(page):
            EasyApplyWalker().apply(ctx)
            return

        self._delegate(ctx, self._resolve_target(page, ctx), navigate=True)

    # ---------- helpers ----------
    @staticmethod
    def _on_linkedin(page) -> bool:
        try:
            return "linkedin.com" in (urlparse(page.url).netloc or "").lower()
        except Exception:
            return True     # unreadable URL: assume we never left, and resolve as usual

    def _delegate(self, ctx: ApplyContext, target: str, *, navigate: bool) -> None:
        """Hand the application to the adapter for `target`. `navigate` is False when the page is already
        there — a resume — so a half-filled form is never reloaded out from under the user."""
        ats = detect_ats(target)
        adapter = get_adapter_for(ats)
        if adapter is None:
            host = urlparse(target).netloc or "the employer's site"
            raise NeedsHuman(
                f"This job applies on {host}, which jobbot has no adapter for. "
                "Finish it in the browser window, then click Continue.")
        if navigate:
            ctx.step(f"Following LinkedIn through to {ats}")
            ctx.page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            ctx.page.wait_for_timeout(1000)
            # A shortened or redirecting Apply link only names its ATS once it has been followed, and the
            # detection above ran on the link as LinkedIn wrote it. "https://grnh.se/lv1el75lanz" is a
            # Greenhouse board; it read as "other", so the generic walker was sent to a form the Greenhouse
            # adapter knows by heart, and "could not find an application form" was filed four times
            # (application 123). Re-read where we actually landed and hand over to whoever owns it.
            landed = ctx.page.url or target
            settled = detect_ats(landed)
            if settled != "other" and settled != ats:
                log.info("linkedin: %s redirected to %s — handing to the %s adapter",
                         target, landed, settled)
                better = get_adapter_for(settled)
                if better is not None:
                    ats, target, adapter = settled, landed, better
                    ctx.step(f"Following LinkedIn through to {ats}")
        # Delegate on the same page so the runner's screenshots, pause and resume keep working.
        adapter.apply(replace(ctx, job={**ctx.job, "ats": ats, "url": target}))

    @classmethod
    def _is_easy_apply(cls, page) -> bool:
        """Signed in, the Apply control states its flow in its own label; signed out, only the tracking names
        do. Try the label first — the member UI has no tracking names to read."""
        label = cls._apply_label(page)
        if label:
            return bool(EASY_LABEL.search(label))
        return cls._is_easy_apply_guest(page)

    @staticmethod
    def _apply_label(page) -> str:
        """The member UI's Apply label, '' when there is none (guest page, or no Apply at all)."""
        try:
            el = page.locator(MEMBER_APPLY).first
            if el.count():
                return (el.get_attribute("aria-label") or "").strip()
        except Exception as e:  # noqa: BLE001
            log.debug("apply label lookup failed: %s", e)
        return ""

    @staticmethod
    def _closed(page) -> str:
        """The phrase LinkedIn used, '' when the posting is open."""
        try:
            text = (page.inner_text("body") or "").lower()
        except Exception as e:  # noqa: BLE001
            log.debug("closed check failed: %s", e)
            return ""
        return next((p for p in CLOSED if p in text), "")

    @staticmethod
    def _is_easy_apply_guest(page) -> bool:
        """LinkedIn marks its own on-site flow '…apply-link-simple_onsite' and offsite jobs
        '…apply-link-offsite' in tracking names. Offsite wins: those we can follow."""
        try:
            names = [n for n in page.eval_on_selector_all(
                "[data-tracking-control-name]",
                "els => els.map(e => e.getAttribute('data-tracking-control-name'))") if n]
        except Exception:
            return False
        if any("apply-link-offsite" in n for n in names):
            return False
        return any("apply-link-simple" in n or "_onsite" in n for n in names)

    @staticmethod
    def _apply_control(page):
        for locator in (page.get_by_role("button", name=APPLY_NAME),
                        page.get_by_role("link", name=APPLY_NAME),
                        page.locator(MEMBER_APPLY),
                        page.locator(APPLY_FALLBACK)):
            if c.visible(locator, c.SHORT):
                return locator.first
        return None

    @staticmethod
    def _reload(page, ctx: ApplyContext) -> None:
        """Re-render the job page under the context's current cookies. Failure is not fatal: the caller
        re-inspects the page either way and reports what it finds."""
        ctx.step("Reloading the page with the current session")
        try:
            page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            page.wait_for_timeout(1500)
        except Exception as e:  # noqa: BLE001
            log.debug("linkedin reload failed: %s", e)

    @staticmethod
    def _signed_out(page) -> bool:
        """Signed out, LinkedIn renders Apply inside an inert sign-in modal: it is in the DOM but absent from
        the accessibility tree and unclickable, so 'no button' here means 'not logged in', not 'no such job'.

        The session itself is the `li_at` cookie, so ask that first — modal markup is renamed often enough
        (the contextual 'Sign in to see who you already know' variant carries neither of SIGN_IN_WALL's
        selectors) that sniffing it alone reports a logged-out user as a missing Apply button."""
        try:
            if not any(ck.get("name") == "li_at" for ck in page.context.cookies()):
                return True
        except Exception as e:  # noqa: BLE001
            log.debug("cookie check failed: %s", e)
        try:
            return bool(page.locator(SIGN_IN_WALL).count())
        except Exception:
            return False

    def _resolve_target(self, page, ctx: ApplyContext) -> str:
        """Click Apply and return wherever LinkedIn sends us — a popup, or this tab leaving linkedin.com."""
        btn = self._apply_control(page)
        if btn is None:
            raise NeedsHuman(SIGNED_OUT if self._signed_out(page) else
                             "No Apply button found on this LinkedIn page. Apply in the browser window, "
                             "then click Continue.")
        ctx.step("Following the Apply button")
        try:
            with page.expect_popup(timeout=POPUP_TIMEOUT) as popup:
                btn.click(timeout=c.MEDIUM)
            tab = popup.value
            try:
                tab.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            except Exception:
                pass
            url = tab.url
            tab.close()
        except Exception:
            url = page.url  # no popup: LinkedIn either navigated this tab or showed its sign-in wall

        if not url or "linkedin.com" in (urlparse(url).netloc or ""):
            raise NeedsHuman(SIGNED_OUT)
        return url


def applied_notice(page) -> str:
    """What LinkedIn shows in place of Apply once an application is in, '' when it shows nothing.

    Two different shapes, and only the first was known here. Easy Apply turns into a green **"Applied"**
    pill. An external application instead grows an **"Application status / Application submitted / 2 days
    ago"** card, and the Apply button simply goes away -- so `_resolve_target` found no control and raised
    "No Apply button found on this LinkedIn page. Apply in the browser window, then click Continue."
    That blocker was recorded four times (applications 120 and 126): every one of them asking the user to
    go and do by hand an application that had already been sent days earlier.

    Matched on an element's whole text rather than a substring, so the phrase inside a job description
    ("...once your application is submitted, we will...") cannot trip it.
    """
    try:
        return c.clean(page.evaluate(
            "() => {" + c.DEEP_JS + " const t = deepAll('button, span, div, h2, h3')"
            ".map(e => (e.innerText || '').trim())"
            ".find(t => /^applied( \\d+ .* ago)?$/i.test(t)"
            "        || /^application submitted$/i.test(t)); return t || ''; }"))
    except Exception:  # noqa: BLE001
        return ""


# ============================== Easy Apply ==============================
# LinkedIn's own application: a modal wizard drawn on top of the job page. Contact details, then the CV,
# then the employer's screening questions, then a review, then "Submit application".
#
# It is walked with the generic form walker rather than a hand-written one, because a step of Easy Apply is
# an ordinary set of labelled controls and everything that makes those fillable — the identity table, the
# answer cache, the resolver, the pause-and-resume — already exists in generic.py. Only what is genuinely
# LinkedIn's own is overridden below:
#
#   * its buttons are named by aria-label, not by the word printed on them. The button that says "Next"
#     has the accessible name "Continue to next step", and get_by_role matches the accessible name — so a
#     walker armed with the ordinary NEXT_NAMES sees no Next button anywhere on the page.
#   * everything is scoped to the modal. The page behind it carries LinkedIn's search box, the messaging
#     widget and a carousel of other jobs; a walker let loose on `body` would fill the site's furniture and
#     press a carousel arrow labelled "Next" for ever.
#   * the CV is already attached. LinkedIn keeps the last four CVs and pre-selects one, so uploading at
#     every step would fill that quota and then be refused.
#   * nothing may close the modal. Its X asks "discard this application?", and an application discarded
#     halfway is not a pause the user can come back to — so the walker only ever presses buttons it named.

EASY_DIALOG = "div[role=dialog]"
# Which dialog is the application. LinkedIn draws others on the same page — the sign-in modal a signed-out
# visitor gets, a "save this search" prompt, a cookie notice — and every one of them is role=dialog with
# fields in it, so "a dialog is open" is not a test at all: it would have walked a sign-in panel as though
# it were an application and typed the candidate's details into somebody's login form.
#
# Matched on what only the application has: LinkedIn's own header id, its class, or one of the wizard
# buttons. The button test asks whether the control exists, not whether it is enabled — Next is disabled
# until the step is filled, which is exactly when the walker most needs to know it is still in the modal.
EASY_MODAL_SEL = ", ".join((
    "div[role=dialog][aria-labelledby*='jobs-apply' i]",
    "div[role=dialog][class*='easy-apply' i]",
    "div[role=dialog]:has([aria-label='Continue to next step' i])",
    "div[role=dialog]:has([aria-label='Review your application' i])",
    "div[role=dialog]:has([aria-label='Submit application' i])",
))
# Both spellings of every wizard control: the accessible name LinkedIn gives it, and the word it prints,
# for a build (or a test page) that labels the button the plain way.
EASY_SUBMIT_NAMES = ("Submit application", "Submit Application")
EASY_NEXT_NAMES = ("Continue to next step", "Review your application", "Next", "Review", "Continue")
EASY_MAX_STEPS = 12          # contact, CV, one step per question group, review. Six is a long one.
EASY_STEP_SETTLE_MS = 1500   # the modal swaps its contents in place, with an animation
EASY_MODAL_WAIT_MS = 12000
EASY_SENT_RE = re.compile(r"application\s+was\s+sent|your\s+application\s+(?:has\s+been\s+)?sent"
                          r"|application\s+sent\b", re.I)
# The CV step, once a CV is on it. LinkedIn draws the chosen one as a card with the file name and a
# "Change" control; there is no checked radio and no file input holding a value to read.
EASY_CV_CARD_RE = re.compile(r"\.(?:pdf|docx?)\b", re.I)


class EasyApplyWalker(GenericFormAdapter):
    """The Easy Apply modal, walked step by step. Reached only through LinkedInAdapter, never registered:
    detect_ats resolves a linkedin.com URL to the adapter above, which decides between this and an offsite
    hand-off."""

    ats = "linkedin-easy-apply"
    # Narrowest first, like every other walker. The <form> inside the modal on the builds that have one,
    # else the modal itself — never wider, whatever the page behind it holds.
    SCOPES = (f"{EASY_DIALOG} form", EASY_DIALOG)
    scope = EASY_DIALOG
    fallback_scope = EASY_DIALOG
    submit_names = EASY_SUBMIT_NAMES
    next_names = EASY_NEXT_NAMES
    submit_fallback = None      # never guess a submit by its shape inside somebody else's application UI
    max_pages = EASY_MAX_STEPS

    def apply(self, ctx: ApplyContext) -> None:
        page = ctx.page
        c.require_open(page)
        self._open_modal(ctx)

        for step_no in range(1, self.max_pages + 1):
            c.require_open(page)
            c.detect_captcha(page)
            if not self.modal_open(page):
                # The modal is gone. Either the application went in while we were looking away (LinkedIn
                # closes it on submit) or something dismissed it; the page says which.
                if self._confirmed(page):
                    ctx.step("Submitted")
                    return
                raise NeedsHuman("The Easy Apply window closed before the application was sent. "
                                 "Open it again in the browser window, then click Continue.")
            dialog = self._dialog(page)
            self.scope = self._detect_scope(page) or self.fallback_scope
            ctx.step(f"Easy Apply — {self._heading(page) or f'step {step_no}'}")
            self._fill_page(ctx)

            if self._button(dialog, self.submit_names) is not None:
                c.submit_and_confirm(ctx, self.submit_names,
                                     click=lambda: self._press(dialog, self.submit_names),
                                     refill=lambda: (self._identity(ctx), self._questions(ctx)))
                self._dismiss_confirmation(page)
                ctx.step("Submitted")
                return

            signature = self._signature(page)
            if not self._press(dialog, self.next_names):
                raise self._no_way_on(dialog)
            page.wait_for_timeout(EASY_STEP_SETTLE_MS)
            if self._confirmed(page):
                ctx.step("Submitted")
                return
            if self.modal_open(page) and self._signature(page) == signature:
                errors = c.form_errors(dialog)
                if not errors:
                    raise NeedsHuman(
                        "LinkedIn would not move past this step of the Easy Apply form and gave no reason. "
                        "Finish it in the browser window, then click Continue — what you type there is "
                        "remembered.")
                # One more pass fills whatever it says is missing (idempotent, so nothing already answered
                # is touched), then the loop tries the step again.
                log.warning("easy apply: step %d rejected (%s); refilling once", step_no, errors[:3])
                self._fill_page(ctx)
                if not self._press(dialog, self.next_names):
                    raise self._no_way_on(dialog)
                page.wait_for_timeout(EASY_STEP_SETTLE_MS)
                if self.modal_open(page) and self._signature(page) == signature:
                    raise NeedsHuman(
                        "LinkedIn keeps refusing this step: " + "; ".join(c.form_errors(dialog)[:3])[:260] +
                        " Finish it in the browser window, then click Continue.")
        raise ApplyError(f"Walked {self.max_pages} steps of the Easy Apply form without reaching Submit")

    # ---------- the modal ----------
    @classmethod
    def modal_open(cls, page) -> bool:
        """True when the Easy Apply modal — not one of LinkedIn's other dialogs — is on screen.

        Named without the leading underscore because LinkedInAdapter asks it before deciding what this job
        is: a run resuming into a modal that is already open must not go looking for an Apply button to
        press again, which on Dayforce restarted the whole application.
        """
        try:
            found = page.locator(EASY_MODAL_SEL)
            return bool(found.count()) and c.is_visible_now(found.last)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _dialog(page):
        """The modal as a locator, so every button press is scoped inside it. LinkedIn stacks dialogs (a
        cookie notice can still be in the DOM behind the application); the newest match is ours."""
        return page.locator(EASY_MODAL_SEL).last

    def _open_modal(self, ctx: ApplyContext) -> None:
        page = ctx.page
        if self.modal_open(page):
            ctx.step("Continuing the Easy Apply form")
            return
        self._refuse_if_applied(page)
        btn = LinkedInAdapter._apply_control(page)
        if btn is None:
            if LinkedInAdapter._signed_out(page):
                raise NeedsHuman(SIGNED_OUT)
            # Signed in and still no Apply control: LinkedIn renames and re-lays-out this card often, and
            # "No Apply button found on this LinkedIn page" is the second most common blocker in the
            # issues table. Let the model name the control before the page is handed back.
            if navigator.press_next(ctx, "start the application for this job (the Easy Apply button)"):
                if self.modal_open(page):
                    return
            raise NeedsHuman(
                "No Easy Apply button on this LinkedIn page. Apply in the browser window, then click "
                "Continue.")
        ctx.step("Opening the Easy Apply form")
        try:
            btn.click(timeout=c.MEDIUM)
        except Exception as e:  # noqa: BLE001
            raise NeedsHuman(f"LinkedIn's Easy Apply button would not open ({str(e)[:80]}). Apply in the "
                             "browser window, then click Continue.") from e
        deadline_ms = EASY_MODAL_WAIT_MS
        while deadline_ms > 0:
            if self.modal_open(page):
                return
            page.wait_for_timeout(250)
            deadline_ms -= 250
        c.detect_captcha(page)
        raise NeedsHuman("LinkedIn did not open the Easy Apply form. Apply in the browser window, then "
                         "click Continue.")

    @staticmethod
    def _refuse_if_applied(page) -> None:
        """See applied_notice: a run that is already in must not come back as a failure with a Retry
        button, because the retry can only ever fetch the same notice back."""
        if notice := applied_notice(page):
            raise AlreadyApplied(f"LinkedIn says this application is already in ({notice}).")

    # ---------- overrides ----------
    @classmethod
    def _detect_scope(cls, page) -> str | None:
        """Inside the modal, one control is a step. The generic three-field rule exists to tell an
        application form from a page's search box and newsletter signup; the modal has already made that
        distinction, and a step that holds a single salary box would otherwise fall through to the
        fallback scope and be walked as though it were empty."""
        for scope in cls.SCOPES:
            if cls._application_controls(page, scope) >= 1:
                return scope
        return None

    def _cv_already_attached(self, page) -> bool:
        """True when the CV step already shows a file. See the note at the top of this section."""
        try:
            return bool(EASY_CV_CARD_RE.search(c.clean(self._dialog(page).inner_text(timeout=c.SHORT))))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _confirmed(page) -> bool:
        """LinkedIn's own "your application was sent" panel. Deliberately not the generic test, which also
        counts a URL that merely contains "applied" — every LinkedIn job URL can carry that in a tracking
        parameter, and calling an unsent application submitted is the one mistake with no way back."""
        try:
            return bool(EASY_SENT_RE.search(c.clean(page.evaluate(
                "() => (document.body && document.body.innerText) || ''"))))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _signature(page) -> str:
        """Which step of the wizard this is: the modal's heading, its progress and the labels on it.

        The generic signature is the page URL, the first heading and a control count, and inside a modal
        all three are wrong: the URL never changes, the first heading on the page is the job title, and two
        different steps with the same number of questions would read as the same step — which is how a
        wizard that was walking perfectly would report "the page did not move on".
        """
        try:
            return page.evaluate("(sel) => {" + c.DEEP_JS + r"""
                const modal = deepAll(sel).filter(d => d.getBoundingClientRect().width > 0).pop();
                if (!modal) return '';
                const txt = el => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
                // every heading, not the first: the modal's own title ("Apply to Acme") is the first
                // one and is the same on every step, while the step's own name sits below it.
                const head = deepIn(modal, 'h1, h2, h3, h4').map(txt).join('/');
                const progress = deepIn(modal, 'progress').map(p => p.value).join(',');
                const labels = deepIn(modal, 'label, legend').map(txt).join('|').slice(0, 300);
                return head + '|' + progress + '|' + labels;
            }""", EASY_DIALOG)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _heading(page) -> str:
        """The step's own title ("Contact info", "Additional questions"), for the progress line."""
        try:
            return c.clean(page.locator(f"{EASY_DIALOG} h1, {EASY_DIALOG} h2, {EASY_DIALOG} h3").last
                           .inner_text(timeout=c.SHORT))[:60]
        except Exception:  # noqa: BLE001
            return ""

    def _no_way_on(self, dialog) -> Exception:
        """Why there was no Next to press. A disabled Next means the step is still missing something, and
        saying which beats "found neither a Submit nor a Next button"."""
        errors = c.form_errors(dialog)
        if errors:
            return NeedsHuman("LinkedIn will not move on until this is answered: " +
                              "; ".join(errors[:3])[:240] +
                              " Finish it in the browser window, then click Continue.")
        return NeedsHuman("jobbot filled this Easy Apply step but LinkedIn offers no Next or Submit on it. "
                          "Finish it in the browser window, then click Continue — whatever you type there "
                          "is remembered.")

    def _dismiss_confirmation(self, page) -> None:
        """Close the "your application was sent" panel, so the window is left clean for the next run.

        Only ever after the submit is confirmed: the same X mid-application asks whether to discard it.
        """
        try:
            self._press(self._dialog(page), ("Done", "Dismiss", "Close"))
        except Exception as e:  # noqa: BLE001 - cosmetic; the application is already in
            log.debug("easy apply: confirmation panel would not close: %s", e)
