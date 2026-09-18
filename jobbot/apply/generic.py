"""Fallback adapter for plain HTML application forms.

Covers the long tail that never justified an adapter of its own — Zoho Recruit, Workable, Recruitee,
Teamtailor, JazzHR, BambooHR, SmartRecruiters, PageUp, iCIMS, SuccessFactors, and most company career
pages. Nothing here is vendor-specific: fields are found by their visible label, which is the one thing all
of them have, and the filling, uploading, cover letter, captcha and submit-and-confirm machinery is the
shared code in common.py.

Three things make the long tail long, and each has a place here:

  * The form is rarely on the job page. It is behind an "Apply" / "I'm interested" / "Postuler" control,
    often inside an iframe (iCIMS), and pressing it may navigate, reveal the form in place, or swap the
    iframe's content. `_open_form` presses the opener in whichever frame holds it and, when the form is in
    a frame, opens that frame as the page so ordinary locators reach it.
  * The form is rarely one page. SmartRecruiters, iCIMS and SuccessFactors walk the candidate through
    "Next" pages — details, then questions, then review — and a walker that fills the first page and looks
    for Submit reports "no submit button" on a form that was one Next away. `apply` fills a page, presses
    Submit if there is one, else Next, and repeats until the site confirms.
  * The form is not always in English. A Quebec posting on SmartRecruiters asks for "Prénom", "Nom" and
    "Courriel"; the identity table knows the common French, German, Spanish, Portuguese, Dutch and Italian
    labels, so those are filled from facts.yaml instead of being asked about.

Where a board offers to fill itself from the CV it is taken up on that first, and a signup it puts in front
of the form is completed with the password from credentials.py rather than stopping to ask for one.

It stays careful, because it runs on pages nobody has inspected: it proceeds only when it can see a real
application form, and a field whose label it cannot map is asked about like any other screening question,
so an unknown form degrades into questions rather than into a wrong answer.

Idempotent like every other adapter: existing values are left alone, so `apply` can be re-run on the same
page after a pause, and a resume lands on whichever page of the wizard the run stopped on.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from urllib.parse import urlparse

from jobbot.apply import common as c
from jobbot.apply.base import Adapter, ApplyContext, ApplyError, NeedsHuman, register

log = logging.getLogger(__name__)

FORM_WAIT = 15000     # ms - career-page SPAs inject the form well after domcontentloaded
NAV_TIMEOUT = 30000
STEP_SETTLE_MS = 3000  # a Next click renders the following page in its own time
MAX_PAGES = 8         # wizards run to five or six pages; more than this is a loop

# The control that sends the application. Exact accessible-name matches only: a substring match on
# "Postuler" would press SmartRecruiters' "Postuler via Indeed", which opens someone else's login.
SUBMIT_NAMES = ("Submit application", "Submit Application", "Submit your application", "Submit my application",
                "Send application", "Send my application",
                # Gem offers two: "Apply without saving" sends the application as it is; "Apply and save"
                # also opens a Gem profile for the candidate. The plain one first — nothing else is wanted.
                "Apply without saving", "Apply and save",
                "Submit", "Apply now", "Apply", "Apply for this job", "Apply for this position", "Apply to this job",
                "Send", "Finish", "Review and submit", "Confirm and submit", "Submit and continue",
                # fr / de / es / pt / it / nl
                "Postuler", "Envoyer", "Soumettre", "Envoyer ma candidature", "Absenden", "Bewerbung absenden",
                "Jetzt bewerben", "Enviar", "Enviar solicitud", "Invia", "Invia candidatura", "Verzenden", "Solliciteer")
# When no exact name matches, a submit-typed control whose name STARTS with the verb still sends the form:
# "Apply and save", "Submit & Continue", "Send my details". Bounded to what a submit says, and never one that
# names a third party or a later time ("Apply with LinkedIn", "Save for later", "Autofill from resume").
SUBMIT_FALLBACK_RE = re.compile(r"^\s*(?:submit|apply|send)\b", re.I)
SUBMIT_FALLBACK_EXCLUDE_RE = re.compile(
    r"\b(?:with|via|using|through|later|draft|autofill|auto-fill|another|other|search|filter|feedback|alert|"
    r"referral|refer|share|save\s+(?:for|job|this))\b|\bwithout\s+(?!saving\b)", re.I)
# The control that moves a wizard to its next page. Never a submit: a form that stops on one of these has
# more to fill, and confirmation is checked after every press in case the last page is labelled this way.
NEXT_NAMES = ("Next", "Continue", "Save and continue", "Save & continue", "Save and Continue", "Proceed",
              "Next step", "Next page", "Suivant", "Continuer", "Étape suivante", "Weiter", "Siguiente",
              "Continuar", "Avanti", "Volgende", "Próximo", "Prosseguir")
# A page that is plainly the site's own confirmation, or its refusal. Checked after every Next.
FALLBACK_SUBMIT_NAMES = SUBMIT_NAMES

# What makes a form an application form. Counting visible controls beats naming one: Zoho Recruit hides its
# file input behind a styled button and names every field `rec-form_842019000000063542`, so a marker keyed on
# a visible file or email input sees nothing on a form that is plainly there. A search box or a newsletter
# signup does not reach three fields; an application always does.
FORM_FIELDS = ("form input:not([type=hidden]):not([type=submit]):not([type=button]):visible, "
               "form textarea:visible, form select:visible")
FORM_FILE = "form input[type=file]"
MIN_FIELDS = 3

# What opens the form when the page shows a teaser instead. Matched on the accessible name, in the
# languages the boards actually use, and NOT when the name goes on to say "with LinkedIn" / "via Indeed":
# those are third-party logins, and pressing one hands an employer's board a login it was never meant to
# have (or, on a headless run, a popup nobody can complete).
OPEN_NAMES = re.compile(
    r"^\s*(?:apply|apply\s+now|apply\s+online|apply\s+for\s+this\s+(?:job|position|role|opening)(?:\s+online)?"
    r"|apply\s+to\s+this\s+(?:job|position|role)|start\s+(?:your\s+|my\s+)?application|begin\s+application"
    r"|i'?\s*m\s+interested|je\s+suis\s+int[ée]ress[ée]e?(?:\(e\))?|postuler(?:\s+maintenant)?|candidater"
    r"|(?:jetzt\s+)?bewerben|solicitar(?:\s+empleo)?|aplicar|candidatar(?:-se)?|candidatura|solliciteer)"
    r"(?!\s*(?:with|via|using|through|by|avec|mit|con|com|met)\b)", re.I)
THIRD_PARTY_RE = re.compile(r"linkedin|indeed|google|facebook|apple|seek\b|xing|microsoft|dropbox", re.I)

# Sites that cannot be driven: they gate the application behind a national identity login or a hardware
# token. Stopping with the reason beats "could not find an application form", which sent the user hunting
# for a form that needs their Singpass.
MANUAL_HOSTS: tuple[tuple[str, str], ...] = (
    ("mycareersfuture.gov.sg", "MyCareersFuture applications go through a Singpass login, which only you can do. "
                               "Apply in the browser window, then click Continue."),
    ("jobsdb.com", "This board asks for its own account login before the form. Sign in (or apply) in the "
                   "browser window, then click Continue."),
)

# label -> facts.yaml key. First match wins, so the specific names come before the bare "name", and
# "current company" before the "company" that a plain employer field would also match. Non-English labels
# sit beside the English ones they mean: a Quebec posting asks for "Prénom", a German one for "Vorname".
IDENTITY: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(first|given|fore)\s*name\b|\bpr[ée]nom|\bvorname\b|\bnombre\b(?!\s+complet)|\bvoornaam\b"
                r"|\bnome\b(?!\s+complet)", re.I), "identity.first_name"),
    (re.compile(r"\b(last|family|sur)\s*name\b|\bnom\s+de\s+famille\b|^\s*nom\s*[*:]?\s*$|\bnachname\b|\bfamilienname\b"
                r"|\bapellidos?\b|\bachternaam\b|\bcognome\b|\bsobrenome\b", re.I), "identity.last_name"),
    (re.compile(r"\bmiddle\s*name\b", re.I), ""),                       # skip: nothing truthful to put here
    # Name fields for a script we do not write in. Workday asks for both ("Bengali Given Name(s)" next to
    # "Given Name(s) - Western Script"), and the Latin name belongs in exactly one of them.
    (re.compile(r"\b(?:bengali|bangla|chinese|japanese|kanji|katakana|hiragana|hangul|korean|cyrillic|"
                r"russian|arabic|hebrew|thai|greek|devanagari|local|native)\b(?=.*\bname\b)", re.I), ""),
    # Social handles we do not have. Asking the model for a Facebook URL invites an invented one.
    (re.compile(r"\b(?:twitter|facebook|instagram|xing|stack\s*overflow|behance|dribbble|medium|tiktok|youtube)\b"
                r"|^\s*x\s*\(", re.I), ""),
    (re.compile(r"\b(full|your|candidate)\s*name\b|^\s*name\s*$|\bnom\s+complet\b|\bnombre\s+completo\b"
                r"|\bnome\s+completo\b|\bvollst[äa]ndiger\s+name\b", re.I), "identity.full_name"),
    (re.compile(r"e-?mail|courriel|correo|adresse\s+[ée]lectronique|\bposta\s+elettronica\b", re.I), "identity.email"),
    (re.compile(r"\b(phone|mobile|telephone|cell)\b|t[ée]l[ée]phone|\btelefon|\btel[ée]fono\b|\btelefoon\b|\bportable\b"
                r"|\bhandy\b|\bcelular\b", re.I), "identity.phone"),
    (re.compile(r"linked\s*in", re.I), "identity.linkedin"),
    (re.compile(r"git\s*hub", re.I), "identity.github"),
    (re.compile(r"portfolio|personal\s*(web)?site|\bwebsite\b|\bweb\s*page\b|\bsite\s+web\b|\bwebseite\b"
                r"|\bsitio\s+web\b|\bsito\s+web\b", re.I), "identity.portfolio"),
    (re.compile(r"current\s*(employer|company|organi[sz]ation)|\bemployeur\s+actuel\b|\baktueller\s+arbeitgeber\b",
                re.I), "work.current_company"),
    (re.compile(r"current\s*(job\s*)?title|current\s*(role|position)|\bposte\s+actuel\b", re.I), "work.current_title"),
    # Before the location rule: "Country" wants "Bangladesh", not "Dhaka, Bangladesh". Anchored so that a
    # "Country phone code" field is not answered with a country name.
    (re.compile(r"^\s*country(?:\s*/\s*region)?\s*$|\bcountry\s+of\s+(?:residence|citizenship)\b|^\s*pays\s*[*:]?\s*$"
                r"|^\s*land\s*[*:]?\s*$|^\s*pa[ií]s\s*[*:]?\s*$|^\s*paese\s*[*:]?\s*$", re.I), "identity.country"),
    (re.compile(r"\b(city|town|location|address|where are you based|country)\b|\bville\b|\bstadt\b|\bort\b"
                r"|\bciudad\b|\bcidade\b|\bcitt[àa]\b|\bwoonplaats\b|\badresse\b|\bdirecci[óo]n\b", re.I),
     "identity.location"),
)
LOCATION_KEYS = {"identity.location", "identity.country"}
# Facts whose value is a URL. A form validator judges these on their shape, so they are written in the
# shape validators accept and re-tried in another shape when one is refused (see common.url_variants).
URL_KEYS = {"identity.linkedin", "identity.github", "identity.portfolio"}


# Shared by the control counter and the gateway test: a form that is plainly the site's own furniture
# rather than the application. Deliberately short — search boxes and newsletter signups only. Sign-in forms
# are NOT listed, because several boards really do gate an application behind one and excluding them would
# lock the walker out of the flow it exists to walk.
_JUNK_FORM_JS = r"""
    const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const junkName = /newsletter|subscribe|unsubscribe|mailing[-_ ]?list/i;
    const isJunkForm = f => {
        if (!f) return false;
        if (f.getAttribute('role') === 'search') return true;
        if (junkName.test([f.id, f.getAttribute('name'), f.getAttribute('action'), f.className]
                .filter(Boolean).join(' '))) return true;
        const t = [...f.querySelectorAll('input,textarea,select')]
            .filter(e => !['hidden', 'submit', 'button'].includes((e.type || '').toLowerCase()));
        return t.length > 0 && t.every(e => (e.type || '').toLowerCase() === 'search');
    };
"""

_COUNT_CONTROLS_JS = "(scope) => {" + _JUNK_FORM_JS + r"""
    const sel = ['input:not([type=hidden]):not([type=submit]):not([type=button])',
                 'textarea', 'select', '[role=combobox]'].map(s => scope + ' ' + s).join(', ');
    let n = 0;
    for (const el of document.querySelectorAll(sel)) {
        if (!vis(el) || isJunkForm(el.closest('form'))) continue;
        n++;
    }
    return n;
}"""

_GATEWAY_FORM_JS = "(names) => {" + _JUNK_FORM_JS + r"""
    const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    // Each source is matched on its own. Joining them was a bug of its own making: PageUp's control is
    // <button aria-label="Next" value="Next"><span>Next</span></button>, the ordinary accessible shape,
    // and innerText + ' ' + aria-label made "Next Next", which is exactly none of the names.
    const named = b => {
        const parts = [b.innerText, b.value, b.getAttribute('aria-label'), b.getAttribute('title')]
            .map(s => (s || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
        return parts.some(t => names.some(n => new RegExp('^\\s*' + esc(n) + '\\s*$', 'i').test(t)));
    };
    for (const f of document.querySelectorAll('form')) {
        if (!vis(f) || isJunkForm(f)) continue;
        const typeable = [...f.querySelectorAll('input, textarea')].filter(e => vis(e) &&
            !['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search', 'password']
                .includes((e.type || '').toLowerCase()));
        if (!typeable.length) continue;
        if ([...f.querySelectorAll('button, input[type=submit]')].filter(vis).some(named)) return true;
    }
    return false;
}"""


class GenericFormAdapter(Adapter):
    """One walker for every plain-form board. Subclasses differ only in the `ats` name they register under."""

    ats = "generic"
    needs_account = False
    # Where an application's controls live, narrowest first. `form` keeps the walker safe on a plain HTML
    # board — a page's search box and newsletter signup sit outside it — and the wider ones are what make a
    # formless React board fillable at all.
    SCOPES = ("form", "main", "[role=main]", "#content", "body")
    # CSS root the field walkers search under. `form` is right for a plain HTML board and is what makes the
    # walker safe there: a page's search box and newsletter signup are outside it. Workday reuses these
    # walkers for its steps and sets its own root, because a Workday step has no <form> element at all —
    # scoped to `form`, every walk found nothing and the step went in empty.
    scope = "form"

    def apply(self, ctx: ApplyContext) -> None:
        page = ctx.page
        c.require_open(page)
        self._refuse_known_manual(page)

        # Cookie banner first: it is an overlay, so on boards that gate the form behind an "Apply" /
        # "I'm interested" button (Zoho Recruit does) it swallows the click and the form never appears —
        # which reads as "no application form on this page" when the form was one dismissal away.
        c.dismiss_cookie_banner(page)

        ctx.step("Looking for the application form")
        self._open_form(ctx)
        page = ctx.page

        for page_no in range(1, MAX_PAGES + 1):
            c.require_open(page)
            c.dismiss_cookie_banner(page)
            c.detect_captcha(page)
            c.detect_bot_block(page)
            # The form may live in an iframe (iCIMS keeps its whole flow in one, and frame-busts any attempt
            # to open it on its own), so every page of the wizard is found afresh and filled where it is.
            root = self._form_root(page)
            fctx = replace(ctx, page=root)
            self.scope = self._detect_scope(root) or "body"
            log.info("generic: page %d — walking the controls under %r on %s", page_no, self.scope, root.url[:80])
            c.detect_captcha(root)
            self._fill_page(fctx)
            c.detect_captcha(root)

            if self._button(root, SUBMIT_NAMES, fallback=SUBMIT_FALLBACK_RE) is not None:
                ctx.step("Submitting")
                self._identity(fctx)     # idempotent re-pass: recover anything the page dropped while we filled it
                c.submit_and_confirm(fctx, SUBMIT_NAMES,
                                     click=lambda: self._press(root, SUBMIT_NAMES, fallback=SUBMIT_FALLBACK_RE),
                                     refill=lambda: (self._identity(fctx), self._questions(fctx)))
                ctx.step("Submitted")
                return

            signature = self._signature(root)
            if not self._press(root, NEXT_NAMES):
                raise ApplyError(
                    "jobbot filled this page but found neither a Submit nor a Next button; the site's "
                    "application flow is not currently automated.")
            ctx.step(f"Moving to page {page_no + 1}")
            page.wait_for_timeout(STEP_SETTLE_MS)
            c.require_open(page)
            root = self._form_root(page)
            if self._confirmed(root) or self._confirmed(page):
                ctx.step("Submitted")
                return
            c.detect_captcha(page)
            c.detect_captcha(root)
            blocked = c.submit_blocked_message(root)
            if blocked:
                raise ApplyError(blocked[:400])
            if self._signature(root) == signature:
                errors = c.form_errors(root)
                if errors:
                    # The page bounced. One more pass fills whatever it says is missing (idempotent, so
                    # nothing already filled is touched); if it bounces again, say what it rejected.
                    log.warning("generic: page %d rejected (%s); refilling once", page_no, errors[:3])
                    fctx = replace(ctx, page=root)
                    self._fill_page(fctx)
                    self._press(root, NEXT_NAMES)
                    page.wait_for_timeout(STEP_SETTLE_MS)
                    root = self._form_root(page)
                    if self._signature(root) == signature and c.form_errors(root):
                        raise ApplyError("Form rejected: " + "; ".join(c.form_errors(root)[:5]))
                else:
                    raise ApplyError(
                        "jobbot pressed Next but the page did not move on, and the site shows no validation "
                        "error; the application's next step is not currently automated.")
        raise ApplyError(f"Walked {MAX_PAGES} pages of this form without reaching a submit button")

    @classmethod
    def _form_root(cls, page):
        """The page itself when its own document holds the form, else a view over the child frame that does."""
        try:
            if cls._detect_scope(page) is not None:
                return page
            for frame in list(page.frames)[1:]:
                try:
                    if frame.locator(cls._controls_selector("body", extra=True, visible=True)).count() >= MIN_FIELDS:
                        return c.FrameView(frame)
                except Exception:  # noqa: BLE001 - a frame that detached mid-scan
                    continue
        except Exception as e:  # noqa: BLE001
            log.debug("generic: form root: %s", e)
        return page

    # ---------- phases ----------
    def _fill_page(self, ctx: ApplyContext) -> None:
        """Fill everything on the current page, in the order a person would."""
        page = ctx.page
        # Before anything is typed: if the board can fill the form from the CV itself, let it. Everything
        # below is idempotent and only writes into empty controls, so the pass after it corrects nothing the
        # CV already answered and fills what the parse missed.
        if c.autofill_from_resume(page, ctx.cv_path):
            ctx.step("Autofilled from your CV")

        ctx.step("Filling your details")
        self._identity(ctx)
        # A signup the board puts in front of the form: the password is jobbot's own (see credentials.py),
        # not a question for the user, so it is filled here rather than reaching _questions.
        if c.fill_account_password(page):
            ctx.step("Setting up the account")

        ctx.step("Uploading CV")
        if not c.upload_resume(page, ctx.cv_path):
            log.info("generic: no file input found on %s", page.url[:80])

        ctx.step("Writing the cover letter")
        c.fill_cover_letter(ctx)

        ctx.step("Answering the form")
        self._questions(ctx)

    def _open_form(self, ctx: ApplyContext) -> None:
        """Make the application form visible, or stop and say the page is not one.

        Career pages come in three shapes: the form is already there, an Apply button reveals or navigates to
        it, or the whole thing is in an iframe — and the Apply button may itself be inside that iframe. Try
        each, more than once, then insist on seeing a real form: submitting a page we never recognised is
        the one failure mode worth being paranoid about.
        """
        page = ctx.page
        self._refuse_redirect_home(ctx)
        # A challenge page in place of the job (DataDome's slider, a Cloudflare turnstile) has no form and
        # no Apply control; without this it read as "could not find an application form".
        c.detect_captcha(page)
        c.detect_bot_block(page)
        if self._form_visible(page, c.SHORT) or self._form_in_frame(page):
            return
        if self._adopt_existing_application_page(ctx):
            page = ctx.page
            c.dismiss_cookie_banner(page)
            c.detect_captcha(page)
            c.detect_bot_block(page)
            if self._form_visible(page, c.SHORT) or self._form_in_frame(page):
                return

        for _ in range(3):
            clicked = self._click_opener(ctx)
            if clicked:
                page = ctx.page
                c.require_open(page)
                self._refuse_known_manual(page)
                c.detect_captcha(page)
                c.detect_bot_block(page)
                if self._form_visible(page) or self._form_in_frame(page):
                    return
            if self._enter_iframe(ctx):
                return
            if not clicked:
                break

        c.require_open(page)
        raise ApplyError(
            f"jobbot could not find an application form after automatic navigation at {page.url}"[:400])

    def _adopt_existing_application_page(self, ctx: ApplyContext) -> bool:
        """Reuse a popup left open by an earlier attempt before pressing Apply again."""
        current = ctx.page
        try:
            pages = list(current.context.pages)
        except Exception:
            return False
        for candidate in reversed(pages):
            try:
                if candidate is current or candidate.is_closed() or not (candidate.url or "").startswith("http"):
                    continue
                if self._has_opener(candidate) or self._form_visible(candidate, c.SHORT):
                    ctx.switch_page(candidate)
                    return True
            except Exception:  # a popup may close or navigate while it is inspected
                continue
        return False

    @staticmethod
    def _has_opener(page) -> bool:
        """True when this page has a visible, non-third-party application opener."""
        try:
            frames = list(page.frames)
        except Exception:
            frames = [page]
        for frame in frames:
            for role in ("button", "link"):
                try:
                    found = frame.get_by_role(role, name=OPEN_NAMES)
                    for i in range(min(found.count(), 6)):
                        el = found.nth(i)
                        if not c.is_visible_now(el):
                            continue
                        name = c.clean(el.inner_text() or el.get_attribute("aria-label") or "")
                        if not THIRD_PARTY_RE.search(name):
                            return True
                except Exception:
                    continue
        return False

    def _click_opener(self, ctx: ApplyContext) -> bool:
        """Press the Apply / I'm interested control, in whichever frame holds it. True when one was pressed."""
        page = ctx.page
        try:
            frames = list(page.frames)
        except Exception:  # noqa: BLE001
            frames = [page]
        for frame in frames:
            for role in ("button", "link"):
                try:
                    found = frame.get_by_role(role, name=OPEN_NAMES)
                    n = min(found.count(), 6)
                except Exception:  # noqa: BLE001 - a frame that detached mid-scan
                    continue
                for i in range(n):
                    el = found.nth(i)
                    if not c.is_visible_now(el):
                        continue
                    try:
                        name = c.clean(el.inner_text() or el.get_attribute("aria-label") or "")
                    except Exception:  # noqa: BLE001
                        name = ""
                    if THIRD_PARTY_RE.search(name):
                        continue
                    try:
                        try:
                            pages_before = tuple(page.context.pages)
                        except Exception:  # a frame/page double without a browser context
                            pages_before = ()
                        ctx.step(f"Pressing '{name[:40] or 'Apply'}'")
                        el.click(timeout=c.MEDIUM)
                        page.wait_for_timeout(2500)
                        if pages_before:
                            opened = [p for p in page.context.pages
                                      if p not in pages_before and not p.is_closed()]
                            if opened:
                                popup = opened[-1]
                                try:
                                    popup.wait_for_load_state("domcontentloaded", timeout=c.MEDIUM)
                                except Exception:
                                    pass
                                ctx.switch_page(popup)
                        return True
                    except Exception as e:  # noqa: BLE001 - a dead opener is not a reason to give up on the page
                        log.debug("generic: apply control did not click: %s", e)
        return False

    @classmethod
    def _form_visible(cls, page, timeout: int = FORM_WAIT) -> bool:
        """True when a real application form is on screen, <form> element or not.

        `timeout` is short on the first look (the form is usually behind an Apply click, and waiting the full
        budget before pressing it just stalls every such board) and generous afterwards, when an SPA may
        still be rendering what the click asked for.
        """
        deadline = time.monotonic() + max(timeout, 0) / 1000
        try:
            page.wait_for_selector(cls._controls_selector("body", extra=True) + ", form",
                                   state="attached", timeout=timeout)
        except Exception:
            return False
        while True:
            try:
                if cls._detect_scope(page) is not None:
                    return True
                # A CV upload plus something to type in is still an application. The file input alone is
                # not: boards keep one in the page before the form opens, so it must have company.
                fields = page.locator(FORM_FIELDS).count()
                if page.locator(FORM_FILE).count() and fields >= 1:
                    return True
            except Exception as e:  # noqa: BLE001
                log.debug("generic: counting form fields: %s", e)
                return False
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return False
            try:
                page.wait_for_timeout(min(250, remaining_ms))
            except Exception:
                return False

    @classmethod
    def _form_in_frame(cls, page, settle_ms: int = 2500) -> bool:
        """True when a child frame of this page holds the form. Frames render after the page does, so a
        blank first look is given a moment before it counts."""
        for _ in range(2):
            try:
                if not isinstance(cls._form_root(page), c.FrameView):
                    page.wait_for_timeout(settle_ms)
                    continue
                return True
            except Exception:  # noqa: BLE001
                return False
        return False

    @classmethod
    def _enter_iframe(cls, ctx: ApplyContext) -> bool:
        """Some boards embed the form from another host, or (iCIMS) keep the whole candidate flow inside a
        frame of their own site. Open that frame as the top-level page so ordinary locators reach the
        fields, exactly as the Greenhouse adapter does with its embed.

        The frame is chosen by what it holds — the one with the controls, else the one with an Apply
        control — rather than by its src, which on a page with a consent widget and a chat bubble is the
        wrong iframe more often than not.
        """
        page = ctx.page
        target = ""
        try:
            host = (urlparse(page.url or "").netloc or "").lower()
            for frame in list(page.frames)[1:]:
                url = frame.url or ""
                if not url.startswith("http") or (urlparse(url).netloc or "").lower() == host:
                    continue        # a same-site frame is filled in place (see _form_root), never hopped to
                try:
                    if frame.locator(cls._controls_selector("body", extra=True, visible=True)).count() >= MIN_FIELDS:
                        target = url
                        break
                    if any(frame.get_by_role(r, name=OPEN_NAMES).count() for r in ("button", "link")):
                        target = target or url
                except Exception:  # noqa: BLE001
                    continue
            if not target:
                frames = page.locator("iframe[src*='apply'], iframe[src*='job'], iframe[src*='career']")
                if frames.count():
                    target = frames.first.get_attribute("src") or ""
            if not target or target.split("#")[0] == (page.url or "").split("#")[0]:
                return False
            ctx.step("Opening the embedded form")
            page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            page.wait_for_timeout(1500)
        except Exception as e:  # noqa: BLE001
            log.debug("generic: iframe hop failed: %s", e)
            return False
        if cls._form_visible(page):
            return True
        # The frame held the Apply control, not the form: press it here, where the form it opens is ours.
        return bool(cls._click_opener(cls(), ctx) and cls._form_visible(page))

    def _identity(self, ctx: ApplyContext) -> None:
        """Fill the fields facts.yaml can answer, by label. Everything else is left to _questions."""
        page = ctx.page
        raw_phone = ctx.fact("identity.phone")
        ours = re.sub(r"\D", "", raw_phone)
        dial = c.dial_code_on_page(page)    # set only on forms that hold the country code separately
        if dial and ours and not ours.startswith(dial):
            # The picker defaulted to the job's country (+1 on a Montreal posting). Switch it to ours; a
            # picker that will not switch gets the number in full, which most of them re-derive from.
            dial = c.select_dial_country(page, ctx.fact("identity.country"), raw_phone) or dial
        errors = c.form_errors(page)       # set only on a re-pass after the form bounced
        for el, label in self._text_controls(page, self.scope):
            key = self._identity_key(label)
            if not key:
                continue
            value = ctx.fact(key)
            current = c.current_value(el)
            if key in URL_KEYS and value:
                self._url_field(ctx, el, label, key, value, current, errors)
                continue
            if current and value and not c.same_value(current, value) and not c.error_for_field(errors, label):
                # The user typed something else into this field in the window. It is their correction, not
                # ours to overwrite, and facts.yaml is where the next application will read it from.
                ctx.seen(current, label)
                continue
            if key == "identity.phone":
                if dial and ours.startswith(dial):
                    value = c.national_phone(raw_phone, dial)
                log.info("phone field %r: form holds the dial code %r, writing %r (was %r)",
                         label, dial or "-", value, current)
                if value != current and current in (raw_phone, c.national_phone(raw_phone, dial or "")):
                    # A re-run on a number the form already rejected: leaving the filled control alone
                    # would replay the same rejection for ever.
                    c.fill_if_empty(el, value, clear=True)
                    continue
            elif value and current and current != value and current.lower() == value.lower():
                # The CV parse filled it in the CV's own capitals ("JAWAD"), which Workday flags as
                # miscapitalised. Same name, so this is a rewrite of case only — never of content.
                c.fill_if_empty(el, value, clear=True)
                continue
            if value and not current:
                c.fill_verified(el, value)
                if key in LOCATION_KEYS:
                    self._settle_suggestions(page, value)

    def _url_field(self, ctx: ApplyContext, el, label: str, key: str, value: str, current: str,
                   errors: list[str]) -> None:
        """Fill a LinkedIn / GitHub / portfolio box, and answer a rejection by changing the URL's shape.

        The failure this exists for (application 100, Cevo on Gem): facts.yaml held
        "https://linkedin.com/in/jawad-amir", Gem's validator wants the www host, and the refill pass wrote
        the identical string back — so the form bounced twice on a URL that was correct but misshapen, and
        the run ended on "Form rejected" with nothing learned.
        """
        variants = c.url_variants(value)
        tried: dict = ctx.extra.setdefault("url_tries", {})
        rejected = c.error_for_field(errors, label)
        if not current:
            tried[key] = 0
            c.fill_verified(el, variants[0])
            return
        if not rejected:
            if not c.same_value(current, value) and current not in variants:
                ctx.seen(current, label)    # the user's own correction: learned back into facts.yaml
            return
        nxt = tried.get(key, 0) + 1
        if nxt >= len(variants):
            log.info("%r: the form rejected every shape of %r", label, value)
            return
        tried[key] = nxt
        log.info("%r rejected (%s); writing %r instead of %r", label, rejected[:80], variants[nxt], current)
        c.fill_if_empty(el, variants[nxt], clear=True)

    @staticmethod
    def _settle_suggestions(page, value: str) -> None:
        """A location box is usually an autocomplete: pick the suggestion that names what was typed, so
        the form holds a chosen place rather than free text it will reject on submit."""
        try:
            page.wait_for_timeout(1200)
            rows = page.locator("[role=option]:visible, [role=listbox] li:visible, .pac-item:visible, "
                                "[class*='suggestion' i]:visible li, [class*='autocomplete' i] li:visible")
            n = min(rows.count(), 12)
            if not n:
                return
            words = [w for w in re.split(r"[\s,]+", value.lower()) if len(w) > 2]
            for i in range(n):
                text = c.clean(rows.nth(i).inner_text()).lower()
                if any(w in text for w in words):
                    rows.nth(i).click(timeout=c.SHORT)
                    page.wait_for_timeout(300)
                    return
            page.keyboard.press("Escape")
        except Exception as e:  # noqa: BLE001
            log.debug("generic: suggestion list: %s", e)

    def _questions(self, ctx: ApplyContext) -> None:
        """Every remaining control, asked about by its label. Mirrors the Greenhouse walker; the difference is
        that identity fields are recognised by label rather than by this-vendor's ids."""
        page = ctx.page
        handled_groups: set[str] = set()
        controls = page.locator(self._controls_selector(self.scope, extra=True))
        for i in range(controls.count()):
            el = controls.nth(i)
            try:
                if not c.is_visible_now(el):
                    continue
                typ = (el.get_attribute("type") or "").lower()
                if typ == "file":
                    continue        # the CV goes through upload_resume, the letter through fill_cover_letter
                if typ == "password":
                    continue        # account credential, filled by fill_account_password — never asked

                if typ in ("radio", "checkbox"):
                    self._choice(ctx, el, typ, handled_groups)
                    continue

                label = c.strip_required(c.get_label_for(el))
                tag = (el.evaluate("e => e.tagName") or "").lower()
                role = (el.get_attribute("role") or "").lower()
                if not label and tag != "select":
                    continue
                key = self._identity_key(label)
                # `is not None` on purpose: _identity_key returns '' for a field to leave alone (a middle
                # name), and truthiness would let that fall through and be asked about.
                if key == "":
                    continue        # deliberately skipped, whatever kind of control it is
                if key is not None and tag not in ("select",) and role != "combobox":
                    continue        # a text field _identity already filled from facts
                # An identity field that is a dropdown — Workday's required Country is one — falls through
                # to the resolver, which answers it from facts.yaml without asking. Skipping it here (the
                # old behaviour) left a required control empty, and the step would not move on.
                if c.is_verification_control(el, label):
                    continue        # emailed-code boxes are filled after submit, never asked about
                if c.is_honeypot(el, label):
                    continue        # a bot trap: filling it is how a finished application gets binned
                if c.is_prompt_control(el):
                    continue        # a Workday prompt; workday.py picks from its list, typing does nothing

                if role == "combobox":
                    c.answer_and_set(ctx, el, label, "combobox")
                elif tag == "select":
                    c.answer_and_set(ctx, el, label or "Select", "select")
                elif tag == "textarea":
                    c.answer_and_set(ctx, el, label, "textarea")
                else:
                    c.answer_and_set(ctx, el, label, "text")
            except (NeedsHuman, c.ApplyError):
                raise
            except Exception as e:  # noqa: BLE001 - one odd control must not lose the whole application
                log.warning("generic: skipping control %d: %s", i, e)

    def _choice(self, ctx: ApplyContext, el, typ: str, handled_groups: set[str]) -> None:
        """Radio groups and checkboxes, asked once per group rather than once per option."""
        group = (el.get_attribute("name") or "") or (el.get_attribute("id") or "")
        if group and group in handled_groups:
            return
        if group:
            handled_groups.add(group)

        container = el.locator("xpath=ancestor::fieldset[1]")
        if container.count() == 0:
            container = el.locator("xpath=ancestor::div[.//label][1]")
        if container.count() == 0:
            container = el.locator("xpath=..")

        label = c.strip_required(self._group_label(container, el))
        if not label:
            return
        opts = c.choice_options(container)
        if typ == "checkbox" and len(opts) <= 1:
            # A lone consent/acknowledgement box: a yes/no, not a pick-one.
            if el.is_checked():
                ctx.seen("Yes", label)
                return
            if ctx.answer(label, ["Yes", "No"], "checkbox").lower().startswith("y"):
                try:
                    el.check(timeout=c.MEDIUM)
                except Exception:  # noqa: BLE001 - a styled box whose input is covered by its label
                    el.evaluate("e => e.click()")
            return
        c.answer_and_set(ctx, el, label, typ, opts, container)

    # ---------- wizard controls ----------
    @staticmethod
    def _button(page, names: tuple[str, ...], fallback: re.Pattern | None = None):
        """The first visible control whose accessible name is exactly one of `names`, or None.

        With `fallback`, a submit-typed control whose name matches that pattern (and none of the exclusions)
        is taken when no exact name is on the page — the shape of the phrase is known even when the words
        are new. Buttons of type=submit are looked at before the rest, because a form's own sender is one.
        """
        for name in names:
            pattern = re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
            for finder in (lambda: page.get_by_role("button", name=pattern),
                           lambda: page.locator(f"input[type=submit][value='{name}' i], input[type=button][value='{name}' i]"),
                           lambda: page.get_by_role("link", name=pattern)):
                try:
                    loc = finder()
                    for i in range(min(loc.count(), 4)):
                        el = loc.nth(i)
                        if c.is_visible_now(el) and el.is_enabled():
                            return el
                except Exception:  # noqa: BLE001
                    continue
        if fallback is None:
            return None
        for sel in ("button[type=submit], input[type=submit]", "button, [role=button]"):
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 24)):
                    el = loc.nth(i)
                    if not c.is_visible_now(el) or not el.is_enabled():
                        continue
                    text = c.clean(el.inner_text() or el.get_attribute("value") or el.get_attribute("aria-label") or "")
                    if (fallback.search(text) and not SUBMIT_FALLBACK_EXCLUDE_RE.search(text)
                            and not THIRD_PARTY_RE.search(text) and len(text) <= 40):
                        log.info("generic: no exact submit name on the page; pressing %r", text)
                        return el
            except Exception:  # noqa: BLE001
                continue
        return None

    def _press(self, page, names: tuple[str, ...], fallback: re.Pattern | None = None) -> bool:
        el = self._button(page, names, fallback=fallback)
        if el is None:
            return False
        try:
            el.scroll_into_view_if_needed(timeout=c.SHORT)
        except Exception:  # noqa: BLE001
            pass
        el.click(timeout=c.MEDIUM)
        return True

    @staticmethod
    def _signature(page) -> str:
        """What page of the wizard this is: URL, heading and control count. Unchanged after a Next means
        the site refused to move on."""
        try:
            return page.evaluate(
                "() => location.href.split('#')[0] + '|' + "
                "((document.querySelector('h1,h2,legend') || {}).innerText || '').trim().slice(0, 80) + '|' + "
                "document.querySelectorAll('input:not([type=hidden]),select,textarea').length")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _confirmed(page) -> bool:
        try:
            url = (page.url or "").lower()
            if any(h in url for h in c.CONFIRM_URL_HINTS):
                return True
            body = c.clean(page.evaluate("() => (document.body && document.body.innerText) || ''")).lower()
            return any(t in body for t in c.CONFIRM_TEXTS)
        except Exception:  # noqa: BLE001
            return False

    # ---------- refusals with a reason ----------
    @staticmethod
    def _refuse_known_manual(page) -> None:
        host = (urlparse(page.url or "").netloc or "").lower()
        for needle, why in MANUAL_HOSTS:
            if needle in host:
                raise NeedsHuman(why)

    @staticmethod
    def _refuse_redirect_home(ctx: ApplyContext) -> None:
        """A job link that lands on the site's front page is a posting that has been taken down. The front
        page has a search box and a newsletter form, and walking those as an application produced
        "Submit button not found" on a job that no longer existed."""
        try:
            job_path = urlparse(ctx.job.get("url") or "").path.strip("/")
            now = urlparse(ctx.page.url or "")
        except Exception:  # noqa: BLE001
            return
        if job_path and not now.path.strip("/") and not now.query:
            raise ApplyError("The job link redirected to the site's home page — the posting has probably been "
                             "closed or moved. Check it in the browser window.")

    # ---------- helpers ----------
    @staticmethod
    def _controls_selector(scope: str, extra: bool = False, visible: bool = False) -> str:
        """Every fillable control under `scope`; `extra` adds the selects and comboboxes _questions handles."""
        suffix = ":visible" if visible else ""
        parts = [f"{scope} input:not([type=hidden]):not([type=submit]):not([type=button]){suffix}",
                 f"{scope} textarea{suffix}"]
        if extra:
            parts += [f"{scope} select{suffix}", f"{scope} [role=combobox]{suffix}"]
        return ", ".join(parts)

    @classmethod
    def _application_controls(cls, page, scope: str) -> int:
        """Visible fillable controls under `scope`, ignoring the page's own search and newsletter boxes.

        Counting those was how a careers page with a job search, a mailing-list box and a sign-in panel
        reached three controls and was walked as though it were the application — the one failure mode the
        docstring on _open_form calls worth being paranoid about.
        """
        try:
            return int(page.evaluate(_COUNT_CONTROLS_JS, scope))
        except Exception as e:  # noqa: BLE001
            log.debug("generic: counting controls under %s: %s", scope, e)
            return 0

    @classmethod
    def _gateway_form(cls, page) -> bool:
        """True for a first step that gates the application rather than being the whole of it.

        PageUp opens an application with an email box, a privacy tick and Next — two controls, so the
        three-field rule read it as "not an application form" and asked the user to fill by hand a page
        jobbot could walk perfectly well. That blocker hit one job four times.

        Narrow on purpose: a real <form>, something to type in that is not a password, and a button whose
        accessible name is one this walker would press anyway. A search box has no such button, a newsletter
        signup says Subscribe and a sign-in panel says Sign in — none of the three gets through.
        """
        try:
            return bool(page.evaluate(_GATEWAY_FORM_JS, list(NEXT_NAMES + SUBMIT_NAMES)))
        except Exception as e:  # noqa: BLE001
            log.debug("generic: gateway-form test: %s", e)
            return False

    @classmethod
    def _detect_scope(cls, page) -> str | None:
        """The narrowest part of the page holding this application's controls, or None if there are none.

        Boards increasingly ship no <form> element at all: Workday, iCIMS, SuccessFactors and SmartRecruiters
        among them. Keyed on `form` alone the walker found nothing on pages whose form was plainly there, and
        "could not find an application form on this page" became the most repeated blocker in the issues
        table — five times across three different boards before it was read as one problem rather than three.

        Narrowest first, so a page that does have a <form> is still walked inside it and a stray search box
        in the header stays out of the application.
        """
        for scope in cls.SCOPES:
            if cls._application_controls(page, scope) >= MIN_FIELDS:
                return scope
        # Nothing reached three. A step that gates the application behind an email box and a Next button is
        # still an application form, and is walked inside its own <form> like any other.
        return "form" if cls._gateway_form(page) else None

    @staticmethod
    def _text_controls(page, scope: str = "form") -> list:
        """(control, label) for every visible text-like input on the form.

        Files, checkboxes and radios are left out: the CV goes through upload_resume and the choice controls
        are handled as groups in _questions, where the options are read from the container.
        """
        out: list = []
        controls = page.locator(GenericFormAdapter._controls_selector(scope))
        for i in range(controls.count()):
            el = controls.nth(i)
            try:
                if not c.is_visible_now(el):
                    continue
                if (el.get_attribute("type") or "").lower() in ("file", "checkbox", "radio", "submit",
                                                                "password"):
                    continue
                label = c.strip_required(c.get_label_for(el))
                if label and not c.is_honeypot(el, label) and not c.is_prompt_control(el):
                    out.append((el, label))
            except Exception as e:  # noqa: BLE001 - one odd control must not stop the pass
                log.debug("generic: reading control %d: %s", i, e)
        return out

    @staticmethod
    def _group_label(container, el) -> str:
        """The question a radio/checkbox group is asking: its legend, else the container's own text, else the
        control's label. Falls back rather than returning '' so the group is asked about, not skipped."""
        for probe in ("legend", "[class*='label' i]", "label"):
            try:
                found = container.locator(probe).first
                if found.count():
                    text = (found.inner_text() or "").strip()
                    if text:
                        return " ".join(text.split())[:300]
            except Exception:  # noqa: BLE001
                continue
        return c.get_label_for(el)

    @staticmethod
    def _identity_key(label: str) -> str | None:
        """The facts.yaml key this label wants, '' for one to skip, or None when it is not an identity field.

        '' and None are different on purpose: a middle name is a field we know to leave alone, while an
        unrecognised label is a question for the resolver.
        """
        text = " ".join((label or "").split())
        if not text:
            return None
        for pattern, key in IDENTITY:
            if pattern.search(text):
                return key
        return None


@register
class _Generic(GenericFormAdapter):
    """The catch-all: detect_ats falls back to this for any host without an adapter of its own."""
    ats = "generic"


# The same walker serves every plain-form board — they differ only in the hostname detect_ats matched, so
# each gets a thin subclass rather than a copy. Adding a board here is a one-line change plus a host pattern
# in base.detect_ats.
for _name in ("zoho", "workable", "recruitee", "teamtailor", "jazzhr", "bamboohr",
              "smartrecruiters", "pageup", "successfactors", "icims", "other"):
    register(type(f"{_name.title()}Adapter", (GenericFormAdapter,), {"ats": _name, "__doc__":
             f"{_name} runs an ordinary HTML form; the generic walker handles it."}))
