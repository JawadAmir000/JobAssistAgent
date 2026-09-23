"""Lever adapter (jobs.lever.co/{slug}/{id}/apply). Idempotent: skips fields that already carry a value."""
from __future__ import annotations

import logging
import re

from jobbot.apply import common as c
from jobbot.apply.base import Adapter, ApplyContext, ApplyError, register

log = logging.getLogger(__name__)

SUBMIT_NAMES = ("Submit application", "Submit Application", "Submit",
                "Resubmit application", "Resubmit", "Verify and submit", "Confirm")

FORM_FIELD = "input[name='name']"          # presence of this == the apply form is open
ON_APPLY_URL = re.compile(r"lever\.co/.+/apply", re.I)
NAV_TIMEOUT = 30000   # ms - explicit: the runner's 8s default is too tight for a cold Lever page
FORM_WAIT = 20000     # ms - the form can be mid-render when apply() is re-run after a pause
STANDARD_NAMES = {"name", "email", "phone", "org", "urls[LinkedIn]", "urls[GitHub]", "urls[Portfolio]",
                  "urls[Twitter]", "urls[Other]", "resume", "comments", "consent", "eeo[gender]", "eeo[race]",
                  "eeo[veteran]", "eeo[disability]"}


@register
class LeverAdapter(Adapter):
    ats = "lever"
    needs_account = False

    def apply(self, ctx: ApplyContext) -> None:
        page = ctx.page
        ctx.step("Opening application form")
        self._open_form(page)
        c.dismiss_cookie_banner(page)
        c.detect_captcha(page)

        ctx.step("Filling identity")
        self._identity(ctx)

        ctx.step("Uploading CV")
        resume_input = page.locator("input[type=file][name='resume'], #resume-upload-input, input[type=file]").first
        if not c.upload_resume(page, ctx.cv_path, resume_input):
            log.warning("lever: resume input not found")

        ctx.step("Writing the cover letter")
        c.fill_cover_letter(ctx)

        ctx.step("Filling location")
        self._location(ctx)

        ctx.step("Answering screening questions")
        self._cards(ctx)
        self._eeo(ctx)

        c.detect_captcha(page)
        self._identity(ctx)   # idempotent re-pass before submit
        c.submit_and_confirm(ctx, SUBMIT_NAMES,
                             refill=lambda: (self._identity(ctx), self._location(ctx), self._cards(ctx), self._eeo(ctx)))
        ctx.step("Submitted")

    # ---------- phases ----------
    def _identity(self, ctx: ApplyContext) -> None:
        page, f = ctx.page, ctx.fact
        c.wait_for_react(page, "input[name='name']")
        c.fill_verified(page.locator("input[name='name']").first,
                        f("identity.full_name") or f"{f('identity.first_name')} {f('identity.last_name')}".strip())
        c.fill_verified(page.locator("input[name='email']").first, f("identity.email"))
        c.fill_phone(page.locator("input[name='phone']").first, f("identity.phone"),
                     page=page, country=f("identity.country"))
        c.fill_verified(page.locator("input[name='org']").first, f("work.current_company"))
        c.fill_verified(page.locator("input[name='urls[LinkedIn]']").first, f("identity.linkedin"))
        c.fill_verified(page.locator("input[name='urls[GitHub]']").first, f("identity.github"))
        c.fill_verified(page.locator("input[name='urls[Portfolio]']").first, f("identity.portfolio"))

    def _open_form(self, page) -> None:
        """Navigate to the /apply page and confirm the form is there.

        Each failure gets its own message. A single "form not found" covered a closed window, a slow
        navigation and a captcha overlay alike, none of which is a missing form.
        """
        c.require_open(page)
        if c.visible(page.locator(FORM_FIELD), c.MEDIUM):
            return

        url = (page.url or "").split("?")[0].rstrip("/")
        if not url.endswith("/apply"):
            try:
                page.goto(url + "/apply", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                page.wait_for_timeout(800)
            except Exception as e:
                raise ApplyError(f"Lever apply page did not load in {NAV_TIMEOUT // 1000}s: {e}"[:400]) from None
            if c.visible(page.locator(FORM_FIELD), c.MEDIUM):
                return

        for name in ("Apply for this job", "Apply", "Apply now"):
            try:
                lnk = page.get_by_role("link", name=re.compile(rf"^\s*{name}\s*$", re.I))
                if c.visible(lnk, c.SHORT):
                    lnk.first.click(timeout=c.MEDIUM)
                    break
            except Exception:
                continue

        if c.visible(page.locator(FORM_FIELD), c.MEDIUM):
            return
        c.require_open(page)
        # A captcha overlay can hide the form; apply() is also re-run from the top on every resume, so this
        # is the common case after a pause — report the captcha rather than claiming the form vanished.
        c.detect_captcha(page)
        if ON_APPLY_URL.search(page.url or "") and c.visible(page.locator(FORM_FIELD), FORM_WAIT):
            return
        raise ApplyError(f"Lever application form not found at {page.url}"[:400])

    def _location(self, ctx: ApplyContext) -> None:
        page = ctx.page
        loc = ctx.fact("identity.location")
        if not loc:
            return
        try:
            el = page.locator("input[name='location'], #location-input").first
            if not c.is_visible_now(el) or c.current_value(el):
                return
            el.click(timeout=c.SHORT)
            el.fill(loc, timeout=c.MEDIUM)
            page.wait_for_timeout(1200)
            opt = page.locator(".dropdown-location .dropdown-results div:visible, [class*='location'] [class*='result']:visible, [role=option]:visible")
            if c.visible(opt, 3000):
                opt.first.click(timeout=c.SHORT)
        except Exception as e:
            log.debug("lever location: %s", e)

    def _cards(self, ctx: ApplyContext) -> None:
        """Custom questions live in .application-question / li.application-question blocks with a label and a control."""
        page = ctx.page
        blocks = page.locator(".application-question, .application-additional .application-field, ul.application-question-set li")
        n = blocks.count()
        handled: set[str] = set()
        for i in range(n):
            block = blocks.nth(i)
            try:
                if not c.is_visible_now(block):
                    continue
                label = self._block_label(block)
                if not label:
                    continue
                key = label.lower()
                if key in handled:
                    continue
                handled.add(key)
                # checkbox / radio group?
                choices = block.locator("input[type=radio], input[type=checkbox]")
                if choices.count():
                    typ = (choices.first.get_attribute("type") or "radio").lower()
                    opts = c.choice_options(block)
                    if typ == "checkbox" and len(opts) <= 1:
                        if choices.first.is_checked():
                            continue
                        ans = ctx.answer(label, ["Yes", "No"], "checkbox")
                        if ans.lower().startswith("y"):
                            choices.first.check(timeout=c.MEDIUM)
                        continue
                    c.answer_and_set(ctx, choices.first, label, typ, opts, block)
                    continue
                sel = block.locator("select")
                if sel.count() and c.is_visible_now(sel.first):
                    c.answer_and_set(ctx, sel.first, label, "select")
                    continue
                ta = block.locator("textarea")
                if ta.count() and c.is_visible_now(ta.first):
                    c.answer_and_set(ctx, ta.first, label, "textarea")
                    continue
                inp = block.locator("input:not([type=hidden]):not([type=file])")
                if inp.count() and c.is_visible_now(inp.first):
                    name = inp.first.get_attribute("name") or ""
                    if name in STANDARD_NAMES:
                        continue
                    c.answer_and_set(ctx, inp.first, label, "text")
            except (c.NeedsHuman, ApplyError):
                raise
            except Exception as e:
                log.warning("lever: skipping card %d: %s", i, e)

    def _eeo(self, ctx: ApplyContext) -> None:
        """Lever EEO block: selects named eeo[gender] etc. Resolver picks a 'decline' option or raises NeedsHuman."""
        page = ctx.page
        sels = page.locator("select[name^='eeo[']")
        for i in range(sels.count()):
            sel = sels.nth(i)
            try:
                if not c.is_visible_now(sel):
                    continue
                label = c.strip_required(c.get_label_for(sel)) or (sel.get_attribute("name") or "").replace("eeo[", "").rstrip("]").title()
                c.answer_and_set(ctx, sel, label, "select")
            except (c.NeedsHuman, ApplyError):
                raise
            except Exception as e:
                log.warning("lever: eeo select %d: %s", i, e)

    @staticmethod
    def _block_label(block) -> str:
        for sel in (".application-label", "label:not(:has(input))", "legend", ".text", "div:first-child"):
            try:
                l = block.locator(sel).first
                if l.count():
                    t = c.strip_required(c.clean(l.inner_text()))
                    if t:
                        return t
            except Exception:
                continue
        return ""
