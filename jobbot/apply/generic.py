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

A fourth thing turned out to be just as long a tail: the form is not shown at all until there is an account.
That gate is not an application form and must never be walked as one, so it is handled in account.py and
only once the search below has looked for a form and found none — see _open_form.

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
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from jobbot.apply import account, common as c, navigator
from jobbot.apply.base import Adapter, ApplyContext, ApplyError, NeedsHuman, register

log = logging.getLogger(__name__)

FORM_WAIT = 15000     # ms - career-page SPAs inject the form well after domcontentloaded
NAV_TIMEOUT = 30000
STEP_SETTLE_MS = 3000  # a Next click renders the following page in its own time
PARSE_SETTLE_MS = 2000  # a board that reads the CV rewrites the fields from it a moment after the upload
OPENER_SETTLE_MS = 7000  # LG creates its Dayforce popup several seconds after the click returns
OPENER_POLL_MS = 250
MAX_PAGES = 8         # wizards run to five or six pages; more than this is a loop
# How far back to read the mailbox for a one-time code when we do not know when it was sent (a run
# resumed after a pause). Short on purpose: a code from an earlier attempt at the same job is still
# in the inbox and typing it gets 'Incorrect security code'.
VERIFY_LOOKBACK_S = 900


def _flow_key(url: str) -> str:
    """A page URL as it is compared across a pause, to tell "the window is where the walk left it" from
    "the window has moved on". The fragment is dropped: a wizard that routes its steps through the hash is
    still the same window on the same application, and the trailing slash is noise."""
    return (url or "").split("#", 1)[0].rstrip("/")

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
# "Send New Code" starts with "Send", and on Oracle's identity step that was the only control the fallback
# could see: application 129 pressed it, which re-sent the code, started the resend cooldown and left the
# run waiting for a confirmation page that a code step never shows. A control that sends a code is never
# the control that sends the application.
SUBMIT_FALLBACK_EXCLUDE_RE = re.compile(
    r"\b(?:with|via|using|through|later|draft|autofill|auto-fill|another|other|search|filter|feedback|alert|"
    r"referral|refer|share|code|re-?send|save\s+(?:for|job|this))\b|\bwithout\s+(?!saving\b)", re.I)
# The control that moves a wizard to its next page. Never a submit: a form that stops on one of these has
# more to fill, and confirmation is checked after every press in case the last page is labelled this way.
NEXT_NAMES = ("Next", "Continue", "Save and continue", "Save & continue", "Save and Continue", "Proceed",
              "Next step", "Next page", "Suivant", "Continuer", "Étape suivante", "Weiter", "Siguiente",
              "Continuar", "Avanti", "Volgende", "Próximo", "Prosseguir",
              # What a code step calls its Next once the code has been typed in (Oracle Recruiting).
              # Listed here rather than left to the navigator so the step costs no model call.
              "Verify", "Verify code", "Verify email")
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
# A control holding a phone number's country code, not a place and not the number. `None` rather than '':
# the phone block in _identity drives this control off its own option list, and where it cannot, the
# resolver being asked about it in _questions is the last fallback there is — skipping it outright would
# leave a required Oracle control empty and the step unable to move.
#
# Without this, "Country code" fell past the anchored country rule into the location one on the strength of
# the bare word "country", which offered "Dhaka, Bangladesh" as the answer to a dial-code picker; and
# "Country Phone Code", Workday's own wording, matched the phone rule and was answered with the full
# international number. Only a text input ever reached either — _text_controls walks input and textarea —
# which is why this was a latent bug rather than the cause of application 135.
_DIAL_LABEL_RE = re.compile(r"\bcountry\s*(?:phone\s*)?code\b|\bphone\s*country\s*code\b"
                            r"|\b(?:dial(?:l?ing)?|area)\s*code\b|\bindicatif\b|\bvorwahl\b", re.I)
# "Confirm your email", "Re-enter email address". These mirror the box above them, so they are filled from
# whatever that box actually holds rather than from facts.yaml — see _identity. Only ever consulted for a
# label that already maps to an identity field, so an "I confirm that…" consent never reaches it.
CONFIRM_LABEL_RE = re.compile(
    r"\b(?:confirm|confirmation|re-?enter|re-?type|repeat|verify)\b"
    r"|\bconfirmer\b|\bbest[äa]tig|\bwiederholen\b|\bconfirmar\b|\bconferma\b|\bbevestig", re.I)
# Facts whose value is a URL. A form validator judges these on their shape, so they are written in the
# shape validators accept and re-tried in another shape when one is refused (see common.url_variants).
URL_KEYS = {"identity.linkedin", "identity.github", "identity.portfolio"}


# Shared by the control counter and the gateway test: a form that is plainly the site's own furniture
# rather than the application. Sign-in forms are NOT listed, because several boards really do gate an
# application behind one and excluding them would lock the walker out of the flow it exists to walk.
#
# `junkText` is the expensive half of the lesson (application 125, NAB). A NAB job page carries no
# application form at all — Apply leaves the site — but it does carry two forms that look exactly like one,
# each asking first name, last name and email:
#
#     "Refer someone to this job"   submit button: "Apply now for this job"
#     "Job Alert — Finalize your job alert by selecting criteria from the dropdowns below"   submit: "Send"
#
# Neither names itself in an id, a class or an action, so junkName missed both, and the walker filled the
# job-alert form and failed on its Categories dropdown. That failure was the lucky outcome: had the
# dropdown been drivable, jobbot would have signed the user up for job alerts, called it a submitted
# application and moved on. Note the referral form's button — a submit named "Apply now for this job" on a
# form that applies for nothing — which is why this is keyed on the form's own prose and never on the name
# of the button that sends it.
#
# The file-input guard keeps a real application safe: an application that takes a CV is never furniture,
# whatever an opt-in line inside it happens to say ("email me similar jobs" is a common consent box).
_JUNK_FORM_JS = r"""
    const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const junkName = /newsletter|subscribe|unsubscribe|mailing[-_ ]?list/i;
    // What only a real application asks. An opt-in line inside one ("also email me similar jobs") must not
    // cost the whole form -- application C in the regression set is exactly that shape.
    const applyMarker = /\bresumes?\b|\bcv\b|\bcurriculum vitae\b|\bcover letter\b|\bwork authoriz|\bauthoris?z?(?:ed|ation)\s+to\s+work\b|\blegally\s+(?:authoris|authoriz|entitled|eligible)|\bright to work\b|\bvisa\b|\bsponsorship\b|\bnotice period\b|\bsalary expectation|\bexpected salary\b|\byears of experience\b|\brequire sponsorship\b/i;
    const junkText = /\bjob alerts?\b|\bcreate (?:a |an )?(?:job )?alert\b|\balert me\b|\bemail me (?:similar |new )?jobs\b|\brefer (?:someone|a friend|this job|somebody)\b|\btell a friend\b|\btalent (?:community|network|pool)\b|\bjoin our talent\b|\bstay (?:connected|in touch)\b|\badd to favou?rites\b|\bsave this job\b/i;
    const isJunkForm = f => {
        if (!f) return false;
        if (f.getAttribute('role') === 'search') return true;
        if (junkName.test([f.id, f.getAttribute('name'), f.getAttribute('action'), f.className]
                .filter(Boolean).join(' '))) return true;
        const t = deepIn(f, 'input,textarea,select')
            .filter(e => !['hidden', 'submit', 'button'].includes((e.type || '').toLowerCase()));
        if (t.length > 0 && t.every(e => (e.type || '').toLowerCase() === 'search')) return true;
        // Names itself in its own prose. Guarded twice over, because a false positive here is worse than
        // the bug it fixes -- it makes jobbot refuse a real application: never for a form that takes a CV,
        // and never for one carrying a marker no alert or referral form ever has. "apply for this job" is
        // deliberately NOT such a marker: NAB's referral button says exactly that.
        const text = (f.innerText || '').replace(/\s+/g, ' ');
        if (!deepIn(f, 'input[type=file]').length && junkText.test(text) && !applyMarker.test(text))
            return true;
        return false;
    };
"""

# The scope is applied as composed-tree containment rather than as a CSS descendant combinator, because a
# control inside a component's shadow root has no `body`, `main` or `#content` ancestor within its own root
# however plainly it sits inside one — see common.DEEP_JS for the board that made this necessary.
_COUNT_CONTROLS_JS = "(scope) => {" + c.DEEP_JS + _JUNK_FORM_JS + r"""
    const sel = ['input:not([type=hidden]):not([type=submit]):not([type=button])',
                 'textarea', 'select', '[role=combobox]'].join(', ');
    const roots = scope === 'body' ? [document.body].filter(Boolean) : deepAll(scope);
    if (!roots.length) return 0;
    let n = 0;
    for (const el of deepAll(sel)) {
        if (!vis(el) || isJunkForm(deepClosest(el, 'form'))) continue;
        if (!roots.some(r => deepContains(r, el))) continue;
        n++;
    }
    return n;
}"""

_GATEWAY_FORM_JS = "(names) => {" + c.DEEP_JS + _JUNK_FORM_JS + r"""
    const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    // Each source is matched on its own. Joining them was a bug of its own making: PageUp's control is
    // <button aria-label="Next" value="Next"><span>Next</span></button>, the ordinary accessible shape,
    // and innerText + ' ' + aria-label made "Next Next", which is exactly none of the names.
    const named = b => {
        const parts = [b.innerText, b.value, b.getAttribute('aria-label'), b.getAttribute('title')]
            .map(s => (s || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
        return parts.some(t => names.some(n => new RegExp('^\\s*' + esc(n) + '\\s*$', 'i').test(t)));
    };
    for (const f of deepAll('form')) {
        if (!vis(f) || isJunkForm(f)) continue;
        const typeable = deepIn(f, 'input, textarea').filter(e => vis(e) &&
            !['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search', 'password']
                .includes((e.type || '').toLowerCase()));
        if (!typeable.length) continue;
        if (deepIn(f, 'button, input[type=submit]').filter(vis).some(named)) return true;
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
    # Where `scope` falls back to when nothing narrower holds the controls. `body` is right for a page whose
    # whole document is the application; a walker running inside somebody else's app (LinkedIn's Easy Apply
    # modal sits on a page with a nav bar, a search box and a chat widget) sets its own, because walking
    # that page's body would fill the site's furniture.
    fallback_scope = "body"
    # The wizard's vocabulary. Class attributes rather than module constants so a board whose buttons are
    # worded its own way — LinkedIn labels its Next "Continue to next step" — is a subclass that overrides
    # two tuples, not a second copy of this file.
    submit_names: tuple[str, ...] = SUBMIT_NAMES
    next_names: tuple[str, ...] = NEXT_NAMES
    submit_fallback: re.Pattern | None = SUBMIT_FALLBACK_RE
    max_pages = MAX_PAGES

    def apply(self, ctx: ApplyContext) -> None:
        page = ctx.page
        c.require_open(page)
        # Signed on the context, which outlives this call: the runner offers a failed vendor adapter one
        # retry with this walker, and a walk that has already happened must not be offered again.
        ctx.extra["generic_walked"] = True
        self._refuse_known_manual(page)

        # Cookie banner first: it is an overlay, so on boards that gate the form behind an "Apply" /
        # "I'm interested" button (Zoho Recruit does) it swallows the click and the form never appears —
        # which reads as "no application form on this page" when the form was one dismissal away.
        c.dismiss_cookie_banner(page)

        if self._already_in_flow(ctx):
            ctx.step("Continuing where it left off")
        else:
            ctx.step("Looking for the application form")
            self._open_form(ctx)
        page = ctx.page

        for page_no in range(1, self.max_pages + 1):
            c.require_open(page)
            c.dismiss_cookie_banner(page)
            c.detect_captcha(page)
            c.detect_bot_block(page)
            # The form may live in an iframe (iCIMS keeps its whole flow in one, and frame-busts any attempt
            # to open it on its own), so every page of the wizard is found afresh and filled where it is.
            root = self._form_root(page)
            fctx = replace(ctx, page=root)
            # Before the step is walked, for the same reason as in _open_form: a signup that appears
            # between two steps looks exactly like one more page of the form.
            if self._pass_account_gate(fctx):
                continue
            self.scope = self._detect_scope(root) or self.fallback_scope
            # Where the walk has got to, for a resume that re-enters apply() from the top. Recorded per
            # wizard page and on ctx.extra, the one thing that outlives a pause — the adapter instance
            # does not (a reload builds a new one) and nor does any local here.
            ctx.extra["flow_url"] = _flow_key(page.url)
            log.info("generic: page %d — walking the controls under %r on %s", page_no, self.scope, root.url[:80])
            c.detect_captcha(root)
            self._fill_page(fctx)
            c.detect_captcha(root)

            if self._button(root, self.submit_names, fallback=self.submit_fallback) is not None:
                ctx.step("Submitting")
                ctx.extra["advanced_at"] = datetime.now(timezone.utc)
                self._identity(fctx)     # idempotent re-pass: recover anything the page dropped while we filled it
                c.submit_and_confirm(fctx, self.submit_names,
                                     click=lambda: self._press(root, self.submit_names,
                                                               fallback=self.submit_fallback),
                                     refill=lambda: (self._identity(fctx), self._questions(fctx)))
                ctx.step("Submitted")
                return

            signature = self._signature(root)
            pages_before = self._context_pages(page)
            # Taken before the click: a board that mails a code does it the instant the step is accepted,
            # and a window that opens after the mail arrived would never find it.
            ctx.extra["advanced_at"] = datetime.now(timezone.utc)
            if not self._press(root, self.next_names):
                # Some boards only ask for the account partway through, so a step with no Next may be a
                # sign-in rather than a dead end. Getting through it puts the next step on screen.
                if self._pass_account_gate(fctx):
                    continue
                # Nothing on this step is worded like a Next or a Submit. Ask the model which control it
                # is, from the ones actually on the page — see navigator.py.
                pages_before = self._context_pages(page)
                if navigator.press_next(fctx, "move this job application to its next step, or send it"):
                    self._adopt_page_opened_since(ctx, pages_before)
                    page = ctx.page
                    root = self._form_root(page)
                    if self._confirmed(root) or self._confirmed(page):
                        ctx.step("Submitted")   # the control it pressed was this board's send button
                        return
                    continue
                raise ApplyError(
                    "jobbot filled this page but found neither a Submit nor a Next button; the site's "
                    "application flow is not currently automated.")
            ctx.step(f"Moving to page {page_no + 1}")
            page.wait_for_timeout(STEP_SETTLE_MS)
            self._adopt_page_opened_since(ctx, pages_before)
            page = ctx.page
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
                    # The page bounced. Read which controls it is complaining about, fix those, then run
                    # the broad pass for anything the step dropped while it was being filled. Pressing
                    # Next again is only worth doing once something has actually changed: the same
                    # complaint twice means the repair achieved nothing and a third press would too.
                    fields = c.field_errors(root)
                    summary = c.rejection_summary(fields, errors)
                    rejected = c.rejection_signature(fields, errors)
                    log.warning("generic: page %d rejected (%s)", page_no, summary[:160])
                    fctx = replace(ctx, page=root)
                    changed = c.repair_fields(fctx, fields)
                    self._fill_page(fctx)
                    if not changed and rejected == ctx.extra.get("last_rejection"):
                        raise NeedsHuman(
                            f"This step keeps refusing what jobbot put in it, and jobbot has run out of "
                            f"ways to fix it: {summary[:300]}. Correct it in the open window, then click "
                            f"Continue — what you type there is remembered for next time.")
                    ctx.extra["last_rejection"] = rejected
                    if changed:
                        log.info("generic: repaired %d field(s): %s", len(changed), "; ".join(changed)[:200])
                    pages_before = self._context_pages(page)
                    ctx.extra["advanced_at"] = datetime.now(timezone.utc)
                    self._press(root, self.next_names)
                    page.wait_for_timeout(STEP_SETTLE_MS)
                    self._adopt_page_opened_since(ctx, pages_before)
                    page = ctx.page
                    root = self._form_root(page)
                    if self._confirmed(root) or self._confirmed(page):
                        ctx.step("Submitted")
                        return
                    if self._signature(root) == signature and c.form_errors(root) and not changed:
                        raise NeedsHuman(
                            f"This step will not accept what jobbot filled in: "
                            f"{c.rejection_summary(c.field_errors(root), c.form_errors(root))[:300]}. "
                            f"Fix it in the open window, then click Continue — what you type there is "
                            f"remembered for next time.")
                else:
                    raise ApplyError(
                        "jobbot pressed Next but the page did not move on, and the site shows no validation "
                        "error; the application's next step is not currently automated.")
        raise ApplyError(f"Walked {self.max_pages} pages of this form without reaching a submit button")

    def _already_in_flow(self, ctx: ApplyContext) -> bool:
        """True when this run already walked a page of the form in this window and it is still on screen.

        Regression (application 122, LG Electronics on Dayforce): apply() is re-entered from the top on
        every resume — that is how an answer, or a fix, lands on a window that has been kept open — and
        _open_form's job is to get from a job posting to the form. Run again on a window that is already
        *inside* the application, its opener search is not the no-op it is on a single-page board: it found
        the board's Apply button and pressed it, and Dayforce answered by starting the application over.
        The questionnaire the user had just answered a salary question for was gone, the run was back on a
        blank page 1, and the CV, the cover letter and every filled field were typed in again from scratch.
        That is what "it starts from the beginning" looks like from the outside.

        Keyed on where the window is, not on what is drawn on it: a form that is open is not always
        recognisable as one. Dayforce's questionnaire step holds a single salary box, too few controls for
        _form_visible, which is exactly why _open_form went looking for an opener to press in the first
        place. The URL of the last page walked does not have that blind spot.

        Still guarded on the page having something fillable left: the window being where we left it says
        nothing if the tab has since been navigated to an error page or emptied by a session timeout, and
        walking on from one of those would fill nothing and press nothing. Falling through to _open_form
        there is the old behaviour, which at worst asks the board for the form again.
        """
        want = ctx.extra.get("flow_url")
        if not want:
            return False        # nothing walked yet in this window: this is a first run, not a resume
        try:
            if _flow_key(ctx.page.url) != want:
                return False    # the window moved on while it waited; find the form the usual way
            root = self._form_root(ctx.page)
            return self._application_controls(root, self._detect_scope(root) or self.fallback_scope) > 0
        except Exception as e:  # noqa: BLE001 - a window that cannot be measured is one to re-open
            log.debug("generic: resume-in-flow test: %s", e)
            return False

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
        # A step that is nothing but an emailed one-time code. submit_and_confirm already handles the same
        # thing after a submit; this is the mid-wizard case, which Oracle Recruiting puts before the form
        # has even opened -- "apply with your email" mails a code and waits. Without this the walk finds
        # nothing it recognises to fill, presses Next, and bounces on the same step until it gives up.
        if self._verification(ctx):
            return
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
        if self._cv_already_attached(page):
            log.info("generic: a CV is already attached to this step; not uploading another")
        elif c.upload_resume(page, ctx.cv_path):
            # The upload is not the end of it on a board that reads the CV. SmartRecruiters parses it about
            # a second later and writes what it found back over the personal-information fields: on the
            # Deloitte NZ one-click form it replaced the email that had just been typed and blanked
            # "Confirm your email" beside it, leaving a required field empty that nothing would have looked
            # at again before Next. The identity pass only writes into empty controls, so running it once
            # more after the parse costs a pass on every other board and rescues the form on this one.
            page.wait_for_timeout(PARSE_SETTLE_MS)
            self._identity(ctx)
        else:
            log.info("generic: no file input found on %s", page.url[:80])

        ctx.step("Writing the cover letter")
        c.fill_cover_letter(ctx)

        ctx.step("Answering the form")
        self._questions(ctx)

    def _verification(self, ctx: ApplyContext) -> bool:
        """True when this step was an emailed code and it has now been typed in.

        The code is read out of the mailbox; only when none arrives is the user asked for it, and that ask
        parks the application with the window open rather than failing it (common.handle_verification).
        """
        prompt = c.verification_prompt(ctx.page)
        if not prompt:
            return False
        sent_at = ctx.extra.get("advanced_at") or (datetime.now(timezone.utc)
                                                  - timedelta(seconds=VERIFY_LOOKBACK_S))
        log.info("generic: this step is an emailed code (%s characters)", prompt.get("length"))
        c.handle_verification(ctx, prompt, sent_at - timedelta(seconds=c.VERIFY_CLOCK_SKEW_S))
        return True

    def _cv_already_attached(self, page) -> bool:
        """True when this step already carries the CV and a second upload would be wrong rather than
        merely wasteful. False here, because on an ordinary form a file input that holds a file is skipped
        by upload_resume itself. LinkedIn overrides it: Easy Apply keeps the candidate's last four CVs and
        pre-selects one, so uploading on every step fills that quota and the step then refuses the file."""
        return False

    def _pass_account_gate(self, ctx: ApplyContext) -> bool:
        """Sign into — or create — the account an employer put in front of its application form.

        account.py owns the gate; this supplies the one thing it deliberately does not know, which is how to
        fill ordinary fields from facts.yaml. A signup asks for names, a phone and a country like any other
        form, and the walker above already answers those.
        """
        def fill() -> None:
            self.scope = self._detect_scope(ctx.page) or "form"
            self._identity(ctx)
            self._questions(ctx)

        return account.pass_gate(ctx, fill)

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
        # The gate before any form test, not after it. Trying the form first worked for a sign-in page —
        # two controls is plainly not an application — but a create-account page has nine, which reads as a
        # form on every test there is. The walker duly filled it, hunted for a CV upload a signup does not
        # have, wrote a cover letter into it, and rewrote the phone number it found there back into
        # facts.yaml as though the user had corrected it.
        if self._pass_account_gate(ctx):
            page = ctx.page
            c.require_open(page)
        if self._form_visible(page, c.SHORT) or self._form_in_frame(page):
            return
        if self._adopt_existing_application_page(ctx):
            page = ctx.page
            c.dismiss_cookie_banner(page)
            c.detect_captcha(page)
            c.detect_bot_block(page)
            has_opener = self._has_opener(page)
            if self._form_visible(page, c.SHORT if has_opener else FORM_WAIT):
                return
            if not has_opener and self._form_in_frame(page):
                return

        for _ in range(3):
            clicked = self._click_opener(ctx)
            if clicked:
                page = ctx.page
                c.require_open(page)
                self._refuse_known_manual(page)
                c.detect_captcha(page)
                c.detect_bot_block(page)
                if self._pass_account_gate(ctx):
                    page = ctx.page
                    c.require_open(page)
                has_opener = self._has_opener(page)
                if self._form_visible(page, c.SHORT if has_opener else FORM_WAIT):
                    return
                if not has_opener and self._form_in_frame(page):
                    return
            # No form here. Before giving up on the page, see whether it is an employer's sign-in: several
            # boards show the application only to an account, and the gate has too few controls to read as
            # a form (by design — see account.py).
            if self._pass_account_gate(ctx):
                page = ctx.page
                c.require_open(page)
                if self._form_visible(page, FORM_WAIT) or self._form_in_frame(page):
                    return
                continue        # signed in, but landed on a profile or a job list: press Apply from here
            entered_iframe = self._enter_iframe(ctx)
            page = ctx.page
            if entered_iframe:
                return
            if not clicked:
                break

        c.require_open(page)
        if self._model_opens_the_form(ctx):
            return
        c.require_open(ctx.page)
        raise ApplyError(
            f"jobbot could not find an application form after automatic navigation at {ctx.page.url}"[:400])

    def _model_opens_the_form(self, ctx: ApplyContext) -> bool:
        """Ask the model to press whatever opens the application here, and say whether a form appeared.

        The page has already been searched for every opener phrase this walker knows, in six languages, in
        every frame. Reaching here means the button is worded in a way nobody has written down yet —
        "Start your journey", "Register your interest" — which is a wording problem, not a hard page, and
        the model is much better at wording than a list is. Two presses at most: an opener sometimes puts
        an interstitial in front of the form, and the budget in navigator.py caps the application overall.
        """
        for _ in range(2):
            pages_before = self._context_pages(ctx.page)
            if not navigator.press_next(ctx, "open this employer's application form for this job"):
                return False
            # The press may have opened the application in a second tab, as an employer's own site does
            # when it hands off to its ATS. Follow it there, exactly as the named openers do.
            self._adopt_page_opened_since(ctx, pages_before)
            page = ctx.page
            c.require_open(page)
            c.dismiss_cookie_banner(page)
            c.detect_captcha(page)
            c.detect_bot_block(page)
            if self._pass_account_gate(ctx):
                page = ctx.page
            if self._form_visible(page, FORM_WAIT) or self._form_in_frame(page):
                log.info("generic: the model's press opened the form on %s", (page.url or "")[:100])
                return True
        return False

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
                # Every context belongs to one application. Its newest non-posting HTTP page is therefore
                # the application handoff even while it still shows only a loader.
                self._switch_context_page(ctx, candidate)
                return True
            except Exception:  # a popup may close or navigate while it is inspected
                continue
        return False

    @staticmethod
    def _context_pages(page) -> tuple:
        try:
            return tuple(page.context.pages)
        except Exception:
            return ()

    @classmethod
    def _adopt_page_opened_since(cls, ctx: ApplyContext, pages_before: tuple) -> bool:
        """Follow a wizard step that opens in another page instead of navigating in place."""
        if not pages_before:
            return False
        try:
            opened = [page for page in ctx.page.context.pages
                      if page not in pages_before and not page.is_closed()]
        except Exception:
            return False
        if not opened:
            return False
        cls._switch_context_page(ctx, opened[-1])
        return True

    @staticmethod
    def _switch_context_page(ctx: ApplyContext, page) -> None:
        """Switch pages even for a parked context created before ApplyContext gained switch_page."""
        switch = getattr(ctx, "switch_page", None)
        if callable(switch):
            switch(page)
            return
        ctx.page = page
        changed = getattr(ctx, "on_page_change", None)
        if callable(changed):
            changed(page)

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
                        pages_before = self._context_pages(page)
                        url_before = page.url
                        ctx.step(f"Pressing '{name[:40] or 'Apply'}'")
                        el.click(timeout=c.MEDIUM)
                        elapsed = 0
                        while elapsed < OPENER_SETTLE_MS:
                            wait_ms = min(OPENER_POLL_MS, OPENER_SETTLE_MS - elapsed)
                            page.wait_for_timeout(wait_ms)
                            elapsed += wait_ms
                            if self._adopt_page_opened_since(ctx, pages_before):
                                try:
                                    ctx.page.wait_for_load_state("domcontentloaded", timeout=c.MEDIUM)
                                except Exception:
                                    pass
                                return True
                            if page.url != url_before or self._detect_scope(page) is not None:
                                return True
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
        return bool(cls._click_opener(cls(), ctx) and cls._form_visible(ctx.page))

    def _identity(self, ctx: ApplyContext) -> None:
        """Fill the fields facts.yaml can answer, by label. Everything else is left to _questions."""
        page = ctx.page
        raw_phone = ctx.fact("identity.phone")
        ours = re.sub(r"\D", "", raw_phone)
        # The country code is settled before the controls are read, because switching it re-renders the
        # number box beside it. A picker that will not switch gets the number in full, which most widgets
        # re-derive the code from anyway.
        dial = c.dial_code_for_phone(page, raw_phone, ctx.fact("identity.country"))
        # Worked out once, here, and written by every branch below. The number used to be derived separately
        # in three of them, and application 135 (Presight, Oracle) is what that cost: the tenant's picker
        # rested on +971, the dial-code-only branch wrote the whole "+8801771614053" and returned before
        # reaching the trim twenty lines further down, and the form answered "Enter a valid number." on all
        # three passes before the run failed. Which branch fires can no longer decide what gets written.
        phone_value = c.national_phone(raw_phone, dial) if dial and ours.startswith(dial) else raw_phone
        if dial and ours and not ours.startswith(dial):
            # Say so plainly: this is the one failure the user can fix in the window in two seconds, and
            # three identical silent passes is what it looked like before.
            log.warning("the form's country-code control is stuck on +%s and this number is +%s — sending "
                        "it whole; the form may refuse it", dial, ours[:4])
        errors = c.form_errors(page)       # set only on a re-pass after the form bounced
        controls = self._text_controls(page, self.scope)
        # What the page itself already holds for each identity field, read before anything here is typed.
        # A "Confirm your email" box is filled from this rather than from facts.yaml, because its job is to
        # agree with the box it confirms: where a board's CV parser has rewritten that box (SmartRecruiters
        # does, moments after the upload), a confirmation taken from facts.yaml disagrees with the field
        # beside it and the form bounces on every pass with "emails do not match".
        on_page: dict[str, str] = {}
        for el, label in controls:
            key = self._identity_key(label)
            if key and not CONFIRM_LABEL_RE.search(label):
                on_page.setdefault(key, c.current_value(el))
        for el, label in controls:
            key = self._identity_key(label)
            if not key:
                continue
            value = ctx.fact(key)
            if CONFIRM_LABEL_RE.search(label):
                value = on_page.get(key) or value
            current = c.current_value(el)
            if key in URL_KEYS and value:
                self._url_field(ctx, el, label, key, value, current, errors)
                continue
            if key == "identity.phone":
                if c.is_dial_control(el):
                    # The country-code half of a composite phone widget, which Oracle labels "Phone Number"
                    # exactly like the number box beside it. It takes a dial code, not a number, and
                    # set_dial_code has already dealt with it — typing "+8801771614053" in here is what
                    # applications 135-138 did, and the widget answered by resetting itself to +971.
                    continue
                value = phone_value     # the national part wherever the form holds the code itself
                if current and c.same_phone(current, raw_phone):
                    continue    # the same number in another shape — a form (or an employer profile) that
                                # keeps the dial code apart shows only the national part. Not a correction,
                                # and learning it back is what stripped +880 out of facts.yaml.
                if current and c.dial_code_only(current):
                    # The mirror image: a widget that seeds its own country prefix and nothing else. The box
                    # reads back as filled, so without this it is taken for the candidate's own answer, left
                    # in place, and reported to `seen` as a correction — which is how "+880" became the phone
                    # number in facts.yaml (application 129).
                    #
                    # What goes in is the national part, not the international one: the code the box is
                    # showing is the form's own, so what it is asking for is the rest of the number. Writing
                    # the whole thing over a control already saying +971 is what application 135 did.
                    log.info("phone field %r holds only the dial code %r — writing %r over it",
                             label, current, value)
                    c.fill_if_empty(el, value, clear=True)
                    continue
            if current and value and not c.same_value(current, value) and not c.error_for_field(errors, label):
                # The user typed something else into this field in the window. It is their correction, not
                # ours to overwrite, and facts.yaml is where the next application will read it from.
                ctx.seen(current, label, kind="text")
                continue
            if key == "identity.phone":
                log.info("phone field %r: form holds the dial code %r, writing %r (was %r)",
                         label, dial or "-", value, current)
                if value != current and current in (raw_phone, phone_value):
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
                if key == "identity.phone":
                    # Said out loud because the number is assembled from two places — facts.yaml and
                    # whatever code the form is holding — and when it comes out wrong the log is the only
                    # record of which half was to blame. Six runs against Oracle were spent inferring this
                    # line from the form's own error message.
                    log.info("phone field %r: writing %r (dial code %s held separately)",
                             label, value, "+" + dial if dial else "none")
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
                ctx.seen(current, label, kind="text")   # their own correction: learned into facts.yaml
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
                typ = (el.get_attribute("type") or "").lower()
                if not c.is_visible_now(el) and not (typ in ("radio", "checkbox") and c.drawn_by_label(el)):
                    # A checkbox the page hides and draws with a styled label is invisible to Playwright
                    # and still required -- see common.drawn_by_label. Anything else off screen is
                    # deliberately not ours to fill.
                    continue
                if typ == "file":
                    continue        # the CV goes through upload_resume, the letter through fill_cover_letter
                if typ in ("radio", "checkbox"):
                    self._choice(ctx, el, typ, handled_groups)
                    continue

                label = c.strip_required(c.get_label_for(el))
                if c.is_password_control(el, label):
                    continue        # account credential, filled by fill_account_password — never asked
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

        opts = c.choice_options(container)
        lone = typ == "checkbox" and len(opts) <= 1
        # A single checkbox is its own question; only a radio or checkbox GROUP has one written above it.
        # Reading the container for a lone box is how Deloitte's "Notification:" tick was reported under
        # the label "Email Address:" — the first label in the table it shares — and cached as an answer.
        label = (c.strip_required(c.get_label_for(el)) if lone else "") \
            or c.strip_required(self._group_label(container, el))
        if not label:
            return
        if lone:
            # A lone consent/acknowledgement box: a yes/no, not a pick-one.
            if el.is_checked():
                ctx.seen("Yes", label, kind="checkbox", options=["Yes", "No"],
                         default=c.choice_is_default(el))
                return
            if ctx.answer(label, ["Yes", "No"], "checkbox").lower().startswith("y"):
                if not c.tick(el):
                    # A required box that will not tick is fatal, and saying so beats pressing Next and
                    # reading the board's own complaint back: "You need to agree to the terms and
                    # conditions" names the symptom, this names the control jobbot could not work.
                    if c.is_required(el):
                        raise ApplyError(f"Could not tick {label!r}")
                    log.warning("generic: optional %r would not tick; leaving it", label[:60])
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
        the site refused to move on.

        Read through the shadow roots for the same reason the detection above is: on a board whose whole
        form is a web component the light DOM has no heading and no controls, so every step of the wizard
        signed itself identically and the first Next would have been reported as "the page did not move on".
        """
        try:
            return page.evaluate("() => {" + c.DEEP_JS + r"""
                let head = '';
                for (const el of deepAll('h1, h2, legend')) {
                    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                    if (t) { head = t.slice(0, 80); break; }
                }
                return location.href.split('#')[0] + '|' + head + '|' +
                       deepAll('input:not([type=hidden]), select, textarea').length;
            }""")
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
                # Keyed on the label too, not just the type: SuccessFactors' "Show" button turns its
                # password box into an ordinary text input, and a credential read out of one is a
                # credential — never a value to report, to cache or to write to facts.yaml.
                if c.is_password_control(el, label):
                    continue
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
        if _DIAL_LABEL_RE.search(text):
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
              "smartrecruiters", "pageup", "successfactors", "icims", "oracle", "other"):
    register(type(f"{_name.title()}Adapter", (GenericFormAdapter,), {"ats": _name, "__doc__":
             f"{_name} runs an ordinary HTML form; the generic walker handles it."}))
