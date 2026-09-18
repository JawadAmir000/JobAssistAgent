"""Greenhouse job-boards adapter (job-boards.greenhouse.io/{slug}/jobs/{id} and legacy boards.greenhouse.io).

Idempotent: every field is checked for an existing value before it is filled, so `apply` can be re-run on the
same page after a NeedsHuman pause.
"""
from __future__ import annotations

import logging
import re

from jobbot.apply import common as c
from jobbot.apply.base import Adapter, ApplyContext, ApplyError, register

log = logging.getLogger(__name__)

IDENTITY_IDS = {"first_name", "last_name", "email", "phone", "resume", "cover_letter", "candidate-location",
                "resume_text", "cover_letter_text"}
SUBMIT_NAMES = ("Submit application", "Submit Application", "Submit",
                # after a security code is entered the button is relabelled ("resubmit your application")
                "Resubmit application", "Resubmit Application", "Resubmit", "Verify and submit", "Confirm")

# Any one of these visible means the form is open. Keyed on several markers, and on ':visible' rather than
# DOM order, because Greenhouse re-renders the identity block (phone widget, resume attach) while we fill it
# and a lone '#first_name' can briefly resolve to a detached or hidden node.
FORM_FIELD = "#application-form:visible, #first_name:visible, #email:visible"
EMBED_IFRAME = "iframe#grnhse_iframe, iframe[src*='greenhouse']"
# A Greenhouse-hosted application page — no iframe to look for once we are here.
ON_APPLICATION_URL = re.compile(r"greenhouse\.io/(embed/job_app|.+/jobs/\d+)", re.I)
EMBED_WAIT = 20000        # ms - SPA career pages inject the embed long after domcontentloaded
EMBED_NAV_TIMEOUT = 30000  # ms - explicit: page.set_default_timeout(8000) in the runner is too tight here


@register
class GreenhouseAdapter(Adapter):
    ats = "greenhouse"
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
        if not c.upload_resume(page, ctx.cv_path):
            log.warning("greenhouse: no resume input found")

        ctx.step("Writing the cover letter")
        c.fill_cover_letter(ctx)

        ctx.step("Filling links & location")
        self._links_and_location(ctx)

        ctx.step("Answering screening questions")
        self._questions(ctx)

        c.detect_captcha(page)
        self._identity(ctx)   # idempotent re-pass: recover anything the page dropped since we filled it
        c.submit_and_confirm(ctx, SUBMIT_NAMES,
                             refill=lambda: (self._identity(ctx), self._links_and_location(ctx), self._questions(ctx)))
        ctx.step("Submitted")

    # ---------- phases ----------
    def _open_form(self, page) -> None:
        """Job page may show an 'Apply' button that scrolls to / opens the form; embeds may live in an iframe.

        Each way this can fail gets its own message. One generic "form not found" made unrelated breakages
        (closed window, career-page SPA that never rendered, slow embed) look identical and undebuggable.
        """
        c.require_open(page)
        # Two passes: clicking Apply on a company career page often navigates straight to the Greenhouse
        # application page, so the URL has to be re-checked after the click — not just before it.
        for clicked in (False, True):
            if c.visible(page.locator(FORM_FIELD), c.MEDIUM):
                return
            if ON_APPLICATION_URL.search(page.url or ""):
                # On a Greenhouse application page there is no iframe to hunt for and nothing safe to click.
                # apply() is re-run from the top after every pause and the form is often mid-re-render then,
                # so wait it out rather than reporting it missing.
                if c.visible(page.locator(FORM_FIELD), EMBED_WAIT):
                    return
                c.require_open(page)
                raise ApplyError(
                    f"On the Greenhouse application page but no form fields appeared within "
                    f"{EMBED_WAIT // 1000}s ({page.url})"[:400])
            if not clicked:
                self._click_apply(page)
        c.require_open(page)

        # Embedded board: the form lives in an iframe. Company career pages (mongodb.com, …) are SPAs that
        # inject it well after domcontentloaded, so wait for it rather than probing a fixed handful of times.
        try:
            page.wait_for_selector(EMBED_IFRAME, state="attached", timeout=EMBED_WAIT)
        except Exception:
            raise ApplyError(
                f"No Greenhouse form or embed iframe appeared within {EMBED_WAIT // 1000}s at {page.url} — "
                "the page probably never finished rendering; click Retry") from None

        src = page.locator(EMBED_IFRAME).first.get_attribute("src")
        if not src:
            raise ApplyError("Greenhouse embed iframe has no src to open")

        # Work on the embed as a top-level page so ordinary locators reach the fields.
        try:
            page.goto(src, wait_until="domcontentloaded", timeout=EMBED_NAV_TIMEOUT)
        except Exception as e:
            raise ApplyError(f"Greenhouse embed did not load in {EMBED_NAV_TIMEOUT // 1000}s: {e}"[:400]) from None
        page.wait_for_timeout(1000)

        if not c.visible(page.locator(FORM_FIELD), c.MEDIUM):
            c.require_open(page)
            raise ApplyError(f"Greenhouse embed loaded but shows no application form ({page.url})"[:400])

    @staticmethod
    def _click_apply(page) -> None:
        """Some boards hide the form behind an Apply button/link that scrolls to or reveals it."""
        for name in ("Apply", "Apply now", "Apply for this job", "Apply Now"):
            try:
                pattern = re.compile(rf"^\s*{name}\s*$", re.I)
                btn = page.get_by_role("button", name=pattern)
                if c.visible(btn, c.SHORT):
                    btn.first.click(timeout=c.MEDIUM)
                    return
                lnk = page.get_by_role("link", name=pattern)
                if c.visible(lnk, c.SHORT):
                    lnk.first.click(timeout=c.MEDIUM)
                    return
            except Exception:
                continue

    def _identity(self, ctx: ApplyContext) -> None:
        page = ctx.page
        c.wait_for_react(page, "#first_name, #email")   # new boards are React; legacy ones return at once
        c.fill_verified(page.locator("#first_name").first, ctx.fact("identity.first_name"))
        c.fill_verified(page.locator("#last_name").first, ctx.fact("identity.last_name"))
        c.fill_verified(page.locator("#email").first, ctx.fact("identity.email"))
        c.fill_verified(page.locator("#phone").first, ctx.fact("identity.phone"))

    def _links_and_location(self, ctx: ApplyContext) -> None:
        page = ctx.page
        links = (
            (r"linkedin", ctx.fact("identity.linkedin")),
            (r"github", ctx.fact("identity.github")),
            (r"portfolio|website|personal site", ctx.fact("identity.portfolio")),
        )
        for pattern, value in links:
            if not value:
                continue
            try:
                el = page.get_by_label(re.compile(pattern, re.I))
                if c.visible(el, c.SHORT):
                    c.fill_if_empty(el.first, value)
            except Exception:
                pass
        # Location autocomplete (#candidate-location on new boards; auto_complete_input on legacy)
        loc_val = ctx.fact("identity.location")
        if loc_val:
            for sel in ("#candidate-location", "input[id*='location' i][role=combobox]", "input[name*='location' i]",
                        "#job_application_location"):
                try:
                    el = page.locator(sel)
                    if not c.visible(el, c.SHORT):
                        continue
                    el = el.first
                    if c.current_value(el):
                        break
                    el.click(timeout=c.SHORT)
                    el.fill(loc_val, timeout=c.MEDIUM)
                    page.wait_for_timeout(1200)
                    opt = page.locator("[role=option]:visible, .pac-item:visible, [class*='autocomplete'] li:visible")
                    if c.visible(opt, 3000):
                        opt.first.click(timeout=c.SHORT)
                    else:
                        page.keyboard.press("ArrowDown")
                        page.keyboard.press("Enter")
                    break
                except Exception:
                    continue

    def _questions(self, ctx: ApplyContext) -> None:
        page = ctx.page
        handled_groups: set[str] = set()
        controls = page.locator(
            "form input:not([type=hidden]):not([type=submit]):not([type=button]), form textarea, form select, "
            "form [role=combobox]")
        n = controls.count()
        for i in range(n):
            el = controls.nth(i)
            try:
                if not c.is_visible_now(el):
                    continue
                eid = (el.get_attribute("id") or "")
                name = (el.get_attribute("name") or "")
                if eid in IDENTITY_IDS or name in IDENTITY_IDS:
                    continue
                tag = (el.evaluate("e => e.tagName") or "").lower()
                typ = (el.get_attribute("type") or "").lower()
                role = (el.get_attribute("role") or "").lower()
                if typ == "file":
                    continue
                if typ in ("radio", "checkbox"):
                    group = name or eid
                    if group in handled_groups:
                        continue
                    handled_groups.add(group)
                    container = el.locator("xpath=ancestor::fieldset[1]")
                    if container.count() == 0:
                        container = el.locator("xpath=ancestor::div[.//label][1]")
                    if container.count() == 0:
                        container = el.locator("xpath=..")
                    label = self._group_label(container, el)
                    if not label:
                        continue
                    opts = c.choice_options(container)
                    if typ == "checkbox" and len(opts) <= 1:
                        # single consent/acknowledgement checkbox
                        if el.is_checked():
                            continue
                        ans = ctx.answer(label, ["Yes", "No"], "checkbox")
                        if ans.lower().startswith("y"):
                            el.check(timeout=c.MEDIUM)
                        continue
                    c.answer_and_set(ctx, el, label, typ, opts, container)
                    continue
                label = c.strip_required(c.get_label_for(el))
                if not label or self._is_identity(label, eid):
                    continue
                if c.is_verification_control(el, label):
                    continue   # the emailed-code boxes; filled after submit, not asked about
                if role == "combobox" or "select__input" in (el.get_attribute("class") or ""):
                    # react-select for greenhouse custom questions: the input sits inside a div.select__control
                    c.answer_and_set(ctx, el, label, "combobox")
                elif tag == "select":
                    c.answer_and_set(ctx, el, label, "select")
                elif tag == "textarea":
                    c.answer_and_set(ctx, el, label, "textarea")
                else:
                    c.answer_and_set(ctx, el, label, "text")
            except c.NeedsHuman:
                raise
            except ApplyError:
                raise
            except Exception as e:
                log.warning("greenhouse: skipping control %d: %s", i, e)

    @staticmethod
    def _group_label(container, el) -> str:
        try:
            lg = container.locator("legend, label:not(:has(input)), .label, [class*='label' i]").first
            if lg.count():
                t = c.strip_required(c.clean(lg.inner_text()))
                if t:
                    return t
        except Exception:
            pass
        return c.strip_required(c.get_label_for(el))

    @staticmethod
    def _is_identity(label: str, eid: str) -> bool:
        l = label.lower()
        if eid and any(eid.startswith(p) for p in ("first_name", "last_name", "email", "phone")):
            return True
        return l in ("first name", "last name", "email", "email address", "phone", "phone number", "resume/cv",
                     "resume", "cover letter", "location (city)", "location", "linkedin profile", "linkedin",
                     "website", "github", "portfolio", "github profile", "portfolio url", "website url")
