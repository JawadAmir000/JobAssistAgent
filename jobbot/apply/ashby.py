"""Ashby adapter (jobs.ashbyhq.com/{slug}/{id}/application).

Ashby's application page is a React app with NO <form> element — selectors prefixed `form …` never match
anything, which is how the previous version of this adapter failed on its first step. The real structure:

    .ashby-application-form-field-entry[data-field-path=…]      one block per field
        .ashby-application-form-question-title[for=<id>]        its label, linked to the control
    system fields have stable ids: #_systemfield_name, #_systemfield_email, #_systemfield_location,
        #_systemfield_resume  (the FIRST input[type=file] on the page is "Autofill from resume", which is
        given the CV first so Ashby fills what it can, and the CV itself is attached to #_systemfield_resume)
    custom questions have UUID ids; Yes/No ones are a pair of button[data-option=yes|no] with aria-pressed,
        not radios; the location field is an autocomplete whose suggestions are [role=option] rows.

Idempotent: every control is checked for an existing value before it is touched, so apply() can be re-run on
the same page after a NeedsHuman pause.
"""
from __future__ import annotations

import logging
import re

from jobbot.apply import common as c
from jobbot.apply.base import Adapter, ApplyContext, ApplyError, NeedsHuman, register

log = logging.getLogger(__name__)

SUBMIT_NAMES = ("Submit Application", "Submit application", "Submit")

FIELD_ENTRY = ".ashby-application-form-field-entry"
QUESTION_TITLE = ".ashby-application-form-question-title"
FORM_READY = f"#_systemfield_name, #_systemfield_email, {FIELD_ENTRY}"   # any visible == the form is open
RESUME_INPUT = "#_systemfield_resume"
LOCATION_ENTRY = f"{FIELD_ENTRY}[data-field-path='_systemfield_location']"
ON_APPLICATION_URL = re.compile(r"ashbyhq\.com/[^/]+/[^/?#]+/application", re.I)
NAV_TIMEOUT = 30000   # ms - explicit; the runner's 8s default is too tight for a cold load
FORM_WAIT = 20000     # ms - the form can be mid-render when apply() is re-run after a pause
SYSTEM_FIELDS_HANDLED = {"_systemfield_name", "_systemfield_email", "_systemfield_resume", "_systemfield_location"}
# Identity-type labels are filled from facts in _identity; skip them in _questions only when they hold a value,
# so an empty required one still reaches the resolver (which asks the user for contact details, never guesses).
IDENTITY_LABEL_RE = re.compile(
    r"^(full name|name|first name|last name|e-?mail( address)?|phone( number)?|linkedin( profile| url)?|"
    r"github( profile| url)?|portfolio( url)?|website|personal site|resume|cv|resume/cv|cover letter|"
    r"current (company|employer)|company)$", re.I)


@register
class AshbyAdapter(Adapter):
    ats = "ashby"
    needs_account = False

    def apply(self, ctx: ApplyContext) -> None:
        page = ctx.page
        ctx.step("Opening application form")
        self._open_form(page)
        c.dismiss_cookie_banner(page)
        c.detect_captcha(page)

        self._autofill(ctx)

        ctx.step("Filling identity")
        self._identity(ctx)

        ctx.step("Uploading CV")
        self._resume(ctx)

        ctx.step("Writing the cover letter")
        c.fill_cover_letter(ctx)

        ctx.step("Filling location")
        self._location(ctx)

        ctx.step("Answering screening questions")
        self._questions(ctx)

        c.detect_captcha(page)
        self._preflight(ctx)
        c.submit_and_confirm(ctx, SUBMIT_NAMES, refill=lambda: self._preflight(ctx))
        ctx.step("Submitted")

    # ---------- phases ----------
    def _open_form(self, page) -> None:
        """Get onto /application and confirm the field entries rendered. Each failure names its cause."""
        c.require_open(page)
        if c.visible(page.locator(FORM_READY), c.MEDIUM):
            return
        url = (page.url or "").split("?")[0].rstrip("/")
        if not ON_APPLICATION_URL.search(url):
            # The job page shows an "Application" tab; /application is the same view without the click.
            try:
                page.goto(url + "/application", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                page.wait_for_timeout(800)
            except Exception as e:
                raise ApplyError(f"Ashby application page did not load in {NAV_TIMEOUT // 1000}s: {e}"[:400]) from None
            if c.visible(page.locator(FORM_READY), c.MEDIUM):
                return
        c.require_open(page)
        c.detect_captcha(page)
        if c.visible(page.locator(FORM_READY), FORM_WAIT):   # re-run after a pause: give a re-render time
            return
        raise ApplyError(f"Ashby application form not found at {page.url}"[:400])

    def _identity(self, ctx: ApplyContext) -> None:
        page, f = ctx.page, ctx.fact
        # Filling before React hydrates leaves text the app's state never sees (rejected as empty on submit).
        c.wait_for_react(page, "#_systemfield_name, #_systemfield_email")
        full = f("identity.full_name") or f"{f('identity.first_name')} {f('identity.last_name')}".strip()
        c.fill_verified(page.locator("#_systemfield_name").first, full)
        c.fill_verified(page.locator("#_systemfield_email").first, f("identity.email"))
        self._fill_label(page, r"^(full )?name$", full)
        self._fill_label(page, r"^first name$", f("identity.first_name"))
        self._fill_label(page, r"^last name$", f("identity.last_name"))
        self._fill_label(page, r"^e-?mail( address)?$", f("identity.email"))
        self._fill_label(page, r"^phone( number)?$", f("identity.phone"), phone=True,
                         country=f("identity.country"))
        self._fill_label(page, r"linkedin", f("identity.linkedin"))
        self._fill_label(page, r"github", f("identity.github"))
        self._fill_label(page, r"portfolio|website|personal site", f("identity.portfolio"))
        self._fill_label(page, r"current (company|employer)|^company$|^organi[sz]ation$", f("work.current_company"))

    def _autofill(self, ctx: ApplyContext) -> None:
        """Hand the CV to Ashby's "Autofill from resume" dropzone before anything is typed by hand.

        It is the first file input on the page — the CV attachment is #_systemfield_resume — and it parses
        the CV into name, email, phone and the work history, which is the fastest way through a form whose
        custom questions would otherwise be asked one at a time. It only runs when there are two file inputs:
        with one, that input is the attachment and there is no autofill on offer.

        What it fills is checked, not trusted: _identity writes only into empty controls, so a value the
        parse got from the CV stays as the CV has it and everything it missed comes from facts.yaml.
        """
        page = ctx.page
        try:
            if page.locator("input[type=file]").count() < 2:
                return
        except Exception as e:  # noqa: BLE001
            log.debug("ashby: counting file inputs: %s", e)
            return
        ctx.step("Autofilling from your CV")
        if c.upload_resume(page, ctx.cv_path, page.locator("input[type=file]").first):
            page.wait_for_timeout(2500)      # Ashby parses the file, then re-renders the fields it filled

    def _resume(self, ctx: ApplyContext) -> None:
        page = ctx.page
        target = page.locator(RESUME_INPUT)
        if target.count() == 0:
            target = page.locator("input[type=file]").last   # never .first: that is the autofill dropzone
        if not c.upload_resume(page, ctx.cv_path, target.first):
            log.warning("ashby: resume input not found")

    def _location(self, ctx: ApplyContext) -> None:
        page = ctx.page
        loc = ctx.fact("identity.location")
        entry = page.locator(LOCATION_ENTRY)
        if not loc or entry.count() == 0:
            return
        inp = entry.locator("input").first
        try:
            if not c.is_visible_now(inp) or c.current_value(inp):
                return
            inp.click(timeout=c.SHORT)
            inp.press_sequentially(loc, delay=30)   # real keystrokes drive the autocomplete; fill() does not
            options = page.locator("[role=option]:visible")
            if not c.visible(options, 4000):
                return
            exact = options.filter(has_text=re.compile(rf"^\s*{re.escape(loc)}\s*$", re.I))
            (exact.first if exact.count() else options.first).click(timeout=c.MEDIUM)
            page.wait_for_timeout(400)
        except Exception as e:
            log.debug("ashby location: %s", e)

    def _questions(self, ctx: ApplyContext) -> None:
        entries = ctx.page.locator(FIELD_ENTRY)
        for i in range(entries.count()):
            entry = entries.nth(i)
            path = ""
            try:
                if not c.is_visible_now(entry):
                    continue
                path = entry.get_attribute("data-field-path") or ""
                if path in SYSTEM_FIELDS_HANDLED:
                    continue
                label = self._entry_label(entry)
                if not label:
                    continue
                if IDENTITY_LABEL_RE.match(label) and self._entry_has_value(entry):
                    continue
                if not self._answer_entry(ctx, entry, label):
                    log.debug("ashby: no recognised control under %r", label)
            except (c.NeedsHuman, ApplyError):
                raise
            except Exception as e:
                log.warning("ashby: skipping %r: %s", path or i, e)

    def _preflight(self, ctx: ApplyContext) -> None:
        """Idempotent re-pass right before submit: anything the app dropped since we filled it goes back in, and
        a required field we still could not fill is handed to the user rather than bounced by the form."""
        self._identity(ctx)
        self._location(ctx)
        self._questions(ctx)
        missing = self._required_missing(ctx.page)
        if missing:
            raise NeedsHuman(f"Required fields still empty: {', '.join(missing[:5])}. Fill them in the browser "
                             "window, then click Continue.")

    @staticmethod
    def _required_missing(page) -> list[str]:
        try:
            return page.evaluate("""() => [...document.querySelectorAll('.ashby-application-form-field-entry')]
                .filter(en => {
                    const lab = en.querySelector('.ashby-application-form-question-title'); if (!lab) return false;
                    const req = /required/i.test(lab.className) || /\*\s*$/.test(lab.innerText)
                             || !!en.querySelector('[aria-required=true],[required]');
                    if (!req) return false;
                    const has = [...en.querySelectorAll('input:not([type=hidden]),textarea,select')].some(x =>
                            (x.type === 'checkbox' || x.type === 'radio') ? x.checked
                          : x.type === 'file' ? (x.files && x.files.length) : !!x.value)
                        || !!en.querySelector('button[aria-pressed=true],button[aria-checked=true]')
                        || /\.(pdf|docx?|rtf|txt)\b/i.test(en.innerText);
                    return !has; })
                .map(en => en.querySelector('.ashby-application-form-question-title').innerText.trim().replace(/\*$/, '').trim())""") or []
        except Exception:
            return []

    # ---------- per-field ----------
    def _answer_entry(self, ctx: ApplyContext, entry, label: str) -> bool:
        yes_no = entry.locator("button[data-option]")
        if yes_no.count():
            self._button_group(ctx, entry, yes_no, label)
            return True
        sel = entry.locator("select")
        if sel.count():
            c.answer_and_set(ctx, sel.first, label, "select")
            return True
        ta = entry.locator("textarea")
        if ta.count() and c.is_visible_now(ta.first):
            c.answer_and_set(ctx, ta.first, label, "textarea")
            return True
        combo = entry.locator("[role=combobox], input[aria-autocomplete]")
        if combo.count() and c.is_visible_now(combo.first):
            c.answer_and_set(ctx, combo.first, label, "combobox")
            return True
        choices = entry.locator("input[type=radio], input[type=checkbox]")
        if choices.count():
            typ = (choices.first.get_attribute("type") or "radio").lower()
            opts = c.choice_options(entry)
            if typ == "checkbox" and len(opts) <= 1:
                if choices.first.is_checked():
                    return True
                if ctx.answer(label, ["Yes", "No"], "checkbox").lower().startswith("y"):
                    try:
                        choices.first.check(timeout=c.MEDIUM)
                    except Exception:
                        choices.first.evaluate("e => e.click()")
                return True
            c.answer_and_set(ctx, choices.first, label, typ, opts, entry)
            return True
        inp = entry.locator("input:not([type=hidden]):not([type=file])")
        if inp.count() and c.is_visible_now(inp.first):
            c.answer_and_set(ctx, inp.first, label, "text")
            return True
        return False

    def _button_group(self, ctx: ApplyContext, entry, buttons, label: str) -> None:
        opts = []
        for j in range(buttons.count()):
            b = buttons.nth(j)
            t = c.clean(b.inner_text()) or c.clean(b.get_attribute("data-option") or "")
            if t:
                opts.append(t)
        if not opts:
            return
        for j in range(buttons.count()):
            b = buttons.nth(j)
            if (b.get_attribute("aria-pressed") or "").lower() == "true" or \
                    (b.get_attribute("aria-checked") or "").lower() == "true":
                return  # already answered on an earlier pass
        ans = ctx.answer(label, opts, "radio")
        for j in range(buttons.count()):
            b = buttons.nth(j)
            if c.clean(b.inner_text()).lower() == ans.lower() or \
                    (b.get_attribute("data-option") or "").lower() == ans.lower():
                b.click(timeout=c.MEDIUM)
                ctx.page.wait_for_timeout(200)
                return
        raise ApplyError(f"Could not pick {ans!r} for {label!r}")

    @staticmethod
    def _entry_label(entry) -> str:
        for sel in (QUESTION_TITLE, "label", "legend"):
            try:
                node = entry.locator(sel).first
                if node.count():
                    text = c.strip_required(c.clean(node.inner_text()))
                    if text:
                        return text
            except Exception:
                continue
        return ""

    @staticmethod
    def _entry_has_value(entry) -> bool:
        try:
            return bool(entry.evaluate(
                """e => [...e.querySelectorAll('input:not([type=hidden]),textarea,select')].some(x =>
                        x.type === 'checkbox' || x.type === 'radio' ? x.checked : x.type === 'file' ? (x.files && x.files.length) : !!x.value)
                     || !!e.querySelector('button[aria-pressed=true],button[aria-checked=true]')"""))
        except Exception:
            return False

    @staticmethod
    def _fill_label(page, pattern: str, value: str, *, phone: bool = False, country: str = "") -> None:
        if not value:
            return
        try:
            el = page.get_by_label(re.compile(pattern, re.I))
            if c.visible(el, c.SHORT):
                # A phone box may be resting on its widget's country prefix, which reads back as filled
                # and would make fill_verified leave the number untyped — see common.fill_phone. Passing
                # the page with it also splits the number against a country-code control held separately,
                # which Ashby does not draw today and the next board to reach here might.
                if phone:
                    c.fill_phone(el.first, value, page=page, country=country)
                else:
                    c.fill_verified(el.first, value)
        except Exception:
            pass
