"""The sign-in / create-account gate an employer puts in front of its application form.

Enterprise career sites increasingly will not show the application to anyone who is not signed in. SAP
SuccessFactors, iCIMS, Taleo and a good part of the long tail all open on "Career Opportunities: Sign In" —
an email box, a password box, and a "Create an account" link — and the walker in generic.py is built, on
purpose, not to treat that as an application form: a careers page with a search box, a newsletter signup and
a login panel reaches three controls, and filling and submitting the login panel is the one failure mode
that docstring calls worth being paranoid about.

So the gate is walked here instead, as its own thing, and only after generic.py has looked for a form and
found none. That ordering is the whole safety argument: a page that looks like an application is still
walked as an application, and nothing arrives here unless there was no form to walk.

Regression this exists for (application 105, Deloitte on SuccessFactors): LinkedIn handed the run to
jobs.deloitte.com.au, "Apply now »" led to career10.successfactors.com, and the gate there has exactly two
visible controls — username and password — against a three-control rule, under a button named "Sign In"
that is neither a Next nor a Submit. Scope detection returned nothing, the walker pressed Apply twice more
and gave up, and the same blocker was reported seven times on one job with a "Create an account" link on
screen the whole time.

The second shape (application 108, an Oracle role on dcjobs.asia) is a modal, and it is why everything here
is anchored to the password boxes rather than to the page: its tab strip carries tabs named "Sign In" and
"Create Account", word for word the buttons that send the two views, and the page header says "Sign in" too.
Reading the whole page for a name said "create" while the sign-in view was showing, and pressing the first
match pressed the tab — which flips the view and submits nothing. Three attempts, no error text to read, and
the run stopped on a form it had filled and never sent. The gate's own button is the one BELOW the boxes;
everything with the same name above them is chrome.

Contract:
    at_gate(page)        -> '' | 'sign_in' | 'create'   # what this page is showing, '' when not a gate
    pass_gate(ctx, fill) -> bool                        # True when there was a gate and we are past it

`fill` is the caller's own pass over the fields of the current page. The split is deliberate and is what
keeps this module small: it knows about gates — which view to be on, which password fits, the terms dialog,
what the site says when it refuses — and knows nothing about labels or facts.yaml, which the walker that
called it already does well.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from jobbot import credentials
from jobbot.apply import common as c
from jobbot.apply.base import ApplyContext, NeedsHuman

log = logging.getLogger(__name__)

# Create, then sign in, then one more move after whatever the site said about the first two. A fourth is not
# worth having: by then the site has refused the same credentials twice and is saying something this module
# does not understand, which is the user's cue, not another round's.
ATTEMPTS = 3
SETTLE_MS = 2500        # a gate answers a submit with a full page render, in its own time

# What the button that ends a gate is called. The two lists are kept apart because which one is on the page
# is how the gate says which of its two views it is showing.
SIGN_IN_NAMES = ("Sign In", "Sign in", "Log In", "Log in", "Login", "Signin", "Sign me in", "Submit",
                 "Se connecter", "Connexion", "Anmelden", "Einloggen", "Iniciar sesión", "Acceder",
                 "Accedi", "Inloggen", "Entrar")
CREATE_NAMES = ("Create Account", "Create an Account", "Create account", "Create an account",
                "Create my account", "Create Profile", "Create profile", "Create your account",
                "Register", "Sign Up", "Sign up", "Join now", "Registrieren", "Konto erstellen",
                "Créer un compte", "S'inscrire", "Crear cuenta", "Registrarse", "Registrati",
                "Registreren", "Criar conta")
GATE_NAMES = CREATE_NAMES + SIGN_IN_NAMES

# The link that switches a gate between its two views. Matched on the link's own words: on most of these
# sites both views are the same URL with a different query parameter, so the href says nothing useful.
TO_CREATE_RE = re.compile(
    r"create\s+(?:an?\s+|a\s+new\s+|my\s+|your\s+)?(?:account|profile|log\s*in|login)"
    r"|^\s*register\b|register\s+(?:now|here|as)|sign\s*up|new\s+user|not\s+a\s+registered\s+user"
    r"|cr[ée]er\s+un\s+compte|s'inscrire|registrieren|konto\s+erstellen"
    r"|crear\s+(?:una\s+)?cuenta|registrarse|registrati|registreren", re.I)
TO_SIGN_IN_RE = re.compile(
    r"(?:already|please)\s+sign\s+in|sign\s+in\s+(?:here|instead)|^\s*(?:please\s+)?sign\s+in\s*$"
    r"|^\s*log\s*in\s*$|existing\s+user|already\s+(?:have|a\s+registered|registered)"
    r"|d[ée]j[àa]\s+inscrit|bereits\s+registriert", re.I)
# Never pressed: these hand the employer's board a login it was never meant to have, and on a headless run
# they open a popup nobody can complete. Mirrors generic.THIRD_PARTY_RE.
THIRD_PARTY_RE = re.compile(r"linkedin|indeed|google|facebook|apple|seek\b|xing|microsoft|dropbox|sso"
                            r"|single\s+sign|okta|azure|saml", re.I)

# What a gate says when this address already has an account here. The password is reused across employers,
# so an account on it is almost always one an earlier application created.
ACCOUNT_EXISTS = re.compile(
    r"already\s+(?:been\s+)?(?:registered|regist|in\s+use|exists?|taken)"
    r"|already\s+ha(?:ve|s)\s+an?\s+(?:account|profile)"
    r"|(?:e-?mail|user\s*name|username)\s*(?:address)?\s*(?:is\s+)?already"
    r"|an?\s+account\s+(?:with|for)\s+th(?:is|at)\s+(?:e-?mail|address|user)", re.I)
# ...and when it will not accept the credentials it was given.
SIGN_IN_FAILED = re.compile(
    r"(?:invalid|incorrect|wrong|unrecognis?zed)\s+(?:user\s*name|username|e-?mail|login|password|credentials)"
    r"|(?:user\s*name|username|e-?mail|login)\s+(?:or|and|/)\s+password\s+(?:do(?:es)?\s+not|did\s+not|don'?t|is|are)"
    r"|(?:login|log\s*in|sign[\s-]?in)\s+(?:failed|was\s+unsuccessful|attempt\s+failed)"
    r"|we\s+(?:do\s+not|don'?t)\s+recognis?ze|no\s+account\s+(?:was\s+)?found", re.I)
# A password rule the generated one broke. Read from the form's error messages only, never from the page
# text: these signup pages print their password rules beside the box at all times, so "Password must be at
# least 8 characters long" is on screen on a page where nothing has gone wrong at all.
PASSWORD_REJECTED = re.compile(r"password", re.I)
# A gate that will not let the new account in until a link in a mail has been opened.
NEEDS_MAIL = re.compile(
    r"(?:verification|confirmation|activation)\s+(?:e-?mail|link|message|code)"
    r"|(?:verify|confirm|activate)\s+your\s+(?:e-?mail|account|registration)"
    r"|we(?:'ve|\s+have)\s+sent\s+(?:you\s+)?an?\s+e-?mail|check\s+your\s+(?:e-?mail|inbox)", re.I)

# The "Terms of Use" row a signup makes required. On SuccessFactors it is a link that opens a dialog rather
# than a checkbox, and the dialog refuses to open until the rest of the form validates — which is why the
# terms are accepted after the fields are filled, not before, and tried again on the next attempt when the
# first click found the form still incomplete.
TERMS_VERB_RE = re.compile(r"\b(?:accept|agree|acknowledge|consent)\b", re.I)
TERMS_SUBJECT_RE = re.compile(r"privacy|terms|conditions|statement|policy|data\s+protection|agreement", re.I)
ACCEPT_NAMES = ("Accept", "I Accept", "I accept", "Accept and continue", "Accept and Continue", "I Agree",
                "I agree", "Agree", "I acknowledge", "Confirm", "OK", "Ok", "Yes", "Continue",
                "Akzeptieren", "Ich stimme zu", "Accepter", "J'accepte", "Aceptar", "Acepto", "Accetto",
                "Accepteren")


# Everything both entry points below need, in one place because they have to agree: if the test that reads
# which view a gate is showing and the one that picks the button to press disagree, the module reads one
# view and submits the other view's form.
_GATE_PRELUDE = c.DEEP_JS + r"""
    const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const nameOf = el => [deepText(el), el.value, deepAttr(el, 'aria-label')].map(norm).filter(Boolean);
    const live = el => el.disabled !== true && el.getAttribute('aria-disabled') !== 'true';
    // A tab is a way of choosing a view, never the control that sends one.
    const isTab = el => el.getAttribute('role') === 'tab' || !!deepClosest(el, '[role=tablist]');

    // The gate's own button is the one BELOW the credential boxes. What is above them is chrome: the site
    // header's "Sign in", and — on a modal gate — the "Sign In | Create Account" tab strip, whose tabs are
    // word for word the same as the button that sends the form. Taking the first match in the document
    // therefore takes the tab, and pressing a tab flips the view and submits nothing at all.
    function gateButtons(names) {
        const passwords = deepAll('input[type=password]').filter(vis);
        const want = new Set(names.create.concat(names.signIn).map(norm));
        // Buttons only, never links: the sign-in view carries a "Create an account" LINK, and counting it
        // would report the view we want to move to rather than the one we are looking at.
        // Enabled or not: these buttons are commonly disabled until the boxes above them are filled, and
        // this is the test for which control the gate's button IS, not for whether it can be pressed yet.
        const all = deepAll('button, input[type=submit], input[type=button], [role=button]')
            .filter(el => vis(el) && !isTab(el) && nameOf(el).some(t => want.has(t)));
        const last = passwords[passwords.length - 1];
        const below = last ? all.filter(
            el => last.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) : [];
        // Falling back to the whole page keeps the gates that put their button above the boxes working;
        // it is the old behaviour, now only reached when the anchored test finds nothing.
        return {passwords: passwords, buttons: below.length ? below : all};
    }
"""

_AT_GATE_JS = "(names) => {" + _GATE_PRELUDE + r"""
    const g = gateButtons(names);
    if (!g.passwords.length) return '';
    // Every application form on these boards asks for a CV somewhere on it, and no gate ever does. Without
    // this test a one-page application that sets a password on the way through would be dragged in here.
    if (deepAll('input[type=file]').some(vis)) return '';
    if (!g.buttons.length) return '';
    const create = new Set(names.create.map(norm));
    // Which view this is, read off the button that sends it — and two password boxes are a password and
    // its confirmation, which only a signup has.
    const creating = g.buttons.some(el => nameOf(el).some(t => create.has(t)));
    return (creating || g.passwords.length > 1) ? 'create' : 'sign_in';
}"""

_GATE_BUTTON_JS = "(names) => {" + _GATE_PRELUDE + r"""
    const buttons = gateButtons(names).buttons;
    // In the caller's order, not the document's: the names are listed most specific first. An enabled
    // match wins over a disabled one of the same name; a disabled one is still returned when it is all
    // there is, so the caller can say the gate is holding its own button shut rather than say nothing.
    let fallback = null;
    for (const wanted of names.want.map(norm)) {
        for (const el of buttons) {
            if (!nameOf(el).some(t => t === wanted)) continue;
            if (live(el)) return el;
            fallback = fallback || el;
        }
    }
    return fallback;
}"""


def _names(want: tuple[str, ...] = ()) -> dict:
    return {"create": list(CREATE_NAMES), "signIn": list(SIGN_IN_NAMES), "want": list(want)}


def at_gate(page: Any) -> str:
    """'sign_in', 'create', or '' when this page is not an account gate.

    Three things have to hold at once, and each rules out a different thing that is not a gate. A visible
    password box says credentials are being asked for. A button named for signing in or registering says the
    asking IS this page, rather than an application that happens to set a password along the way. And no
    visible file input, because an application wants a CV and a gate never does.

    Which of the two views it is, is read off the button under the credential boxes and nothing else. A
    modal gate keeps both names on screen at all times — its tab strip says "Sign In | Create Account" —
    so a page-wide look for a create-shaped name answers "create" while the sign-in view is what is showing.
    """
    try:
        return str(page.evaluate(_AT_GATE_JS, _names()) or "")
    except Exception as e:  # noqa: BLE001 - a page we cannot ask is not one we should guess about
        log.debug("account: gate test: %s", e)
        return ""


def _gate_button(page: Any, names: tuple[str, ...]) -> Any:
    """The control that sends this gate, or None.

    Not c.named_button, which takes the first control in the document whose name matches — right for a form
    with one button of a given name, wrong for a gate. Regression (application 108, an Oracle role on
    dcjobs.asia): the modal's tab strip carries a tab named "Create Account" and its form carries a button
    named "Create Account", the tab comes first, and every attempt pressed the tab. The view flipped, nothing
    was submitted, no error was printed — so the gate "refused without saying why" three times and the run
    stopped on a filled-in form nobody had sent.
    """
    try:
        handle = page.evaluate_handle(_GATE_BUTTON_JS, _names(names))
        el = handle.as_element() if handle is not None else None
        if el is not None:
            return el
    except Exception as e:  # noqa: BLE001 - fall back rather than lose the attempt
        log.debug("account: gate button: %s", e)
        return c.named_button(page, names)
    return None


def pass_gate(ctx: ApplyContext, fill: Callable[[], None]) -> bool:
    """Get past an employer's account gate. True when there was one and the page is now through it.

    Creates the account before trying to sign into it. That is the order workday.py settled on and it is
    right for the same reason: jobbot has usually never been to this employer, and a sign-in with a password
    no account holds is a failed login on the user's own address, repeated once per run until something
    locks it. A signup against an address that does already have an account is answered with "already
    registered", which costs nothing and says exactly what to do next.
    """
    page = ctx.page
    if not at_gate(page):
        return False
    email = ctx.fact("identity.email")
    if not email:
        raise NeedsHuman("This employer wants an account before it will show the application form, and "
                         "facts.yaml has no email address to create one with.")

    creating = True
    for attempt in range(ATTEMPTS):
        c.require_open(page)
        kind = at_gate(page)
        if not kind:
            log.info("account: through the gate after %d attempt(s), now on %s", attempt, (page.url or "")[:100])
            ctx.step("Signed in — opening the application")
            return True
        kind = _switch_view(page, "create" if creating else "sign_in") or kind
        ctx.step("Creating your account with this employer" if kind == "create" else "Signing in")
        _submit_credentials(ctx, email, kind == "create", fill)
        page = ctx.page

        if not at_gate(page):
            log.info("account: %s succeeded on %s", "signup" if kind == "create" else "sign-in",
                     (page.url or "")[:100])
            ctx.step("Signed in — opening the application")
            return True
        creating = _read_refusal(ctx, page, email, creating)

    raise NeedsHuman(
        f"jobbot could not get past this employer's sign-in. It tried both creating an account for {email} "
        f"and signing in with the password it manages ({credentials.location_hint(c.password_max_length(page))}). "
        "Finish it in the browser window — sign in, or create the account by hand — then click Continue.")


def _read_refusal(ctx: ApplyContext, page: Any, email: str, creating: bool) -> bool:
    """What the gate said about the attempt just made, and therefore what to try next. Returns the next
    `creating`. Raises when the site is saying something no further attempt can answer."""
    errors = c.form_errors(page)
    joined = " ; ".join(errors)
    text = _text(page)

    if NEEDS_MAIL.search(joined) or NEEDS_MAIL.search(text):
        raise NeedsHuman(
            f"This employer emailed a confirmation link to {email} and will not let the new account in until "
            "it has been opened. Click the link in that mail, then click Continue.")
    if ACCOUNT_EXISTS.search(joined) or ACCOUNT_EXISTS.search(text):
        log.info("account: %s already has an account here; signing in instead", email)
        return False
    if SIGN_IN_FAILED.search(joined) or SIGN_IN_FAILED.search(text):
        if creating:
            return True     # a signup that failed a login check is still a signup; try it once more
        cap = c.password_max_length(page)
        raise NeedsHuman(
            f"There is an account at this employer for {email}, and it does not take the password jobbot "
            f"manages. Two ways on, both in the browser window that is open: sign in by hand, or use "
            f"'Forgot your password?' and set it to the one in {credentials.location_hint(cap)} — that "
            "second one means jobbot gets in by itself here from now on. Then click Continue.")
    # Only now, and only from the form's own error messages: the rules printed beside the box say
    # "Password must be…" on a page where nothing has gone wrong.
    rejected = [e for e in errors if PASSWORD_REJECTED.search(e)]
    if rejected and creating:
        cap = c.password_max_length(page)
        raise NeedsHuman(
            f"This employer's signup refused the password jobbot generated — it says: {rejected[0][:160]}. "
            f"Set one it accepts in the browser window, save it in Settings -> Secrets as "
            f"{credentials.SHORT_SECRET_NAME if cap else credentials.SECRET_NAME}, then click Continue.")
    if errors:
        # Stay on the view we are on. A validation message means this form was incomplete, not that it was
        # the wrong form — "Terms of Use is required" is what a signup says on the round where the dialog
        # would not open yet, and answering it by switching to the sign-in view abandons a signup that was
        # one idempotent refill from going through.
        log.info("account: the gate says %s", joined[:300])
        ctx.step("The signup was rejected — filling in what it asked for")
        return creating
    # Nothing said at all. Some gates answer a submit they did not accept by re-rendering an empty card
    # (workday.py saw Salesforce do exactly this), so the other route is the only move left.
    log.info("account: the gate refused without saying why; trying the %s route",
             "sign-in" if creating else "signup")
    return not creating


def _switch_view(page: Any, want: str) -> str:
    """Put the gate on the view we mean to use. Returns what it is showing afterwards.

    A no-op when it is already there, which matters more than it sounds: the "Create an account" link on a
    create page is usually still in the markup, and clicking it every time round would reload a form that
    was already filled.
    """
    showing = at_gate(page)
    if not showing or showing == want:
        return showing
    pattern = TO_CREATE_RE if want == "create" else TO_SIGN_IN_RE
    for role in ("link", "button"):
        try:
            found = page.get_by_role(role, name=pattern)
            for i in range(min(found.count(), 6)):
                el = found.nth(i)
                if not c.is_visible_now(el):
                    continue
                name = c.clean(el.inner_text() or el.get_attribute("aria-label") or "")
                if THIRD_PARTY_RE.search(name):
                    continue
                log.info("account: switching to the %s view with %r", want, name[:60])
                el.click(timeout=c.MEDIUM)
                page.wait_for_timeout(SETTLE_MS)
                _settle(page)
                return at_gate(page)
        except Exception as e:  # noqa: BLE001 - a link that will not click is not the end of the attempt
            log.debug("account: switching to %s via %s: %s", want, role, e)
    log.info("account: no %s link on this gate; using the view it is showing (%s)", want, showing)
    return showing


def _submit_credentials(ctx: ApplyContext, email: str, creating: bool, fill: Callable[[], None]) -> None:
    """Fill the gate and press its button. The order is the one the forms demand, not a tidy one."""
    page = ctx.page
    c.dismiss_cookie_banner(page)
    username, passwords = _credential_boxes(page)
    if username is not None:
        c.fill_if_empty(username, email)

    if creating:
        # The rest of a signup — names, phone, country, the retyped email — is an ordinary form, and the
        # caller already knows how to fill one from facts.yaml. Before the passwords, because a bounced
        # form may have kept them and this pass only writes into empty controls either way.
        fill()

    cap = c.password_max_length(page)
    password = credentials.account_password(cap)
    if passwords:
        c.fill_account_password(page, password)

    if creating:
        _accept_terms(page)
    # Last, and on both views: a challenge on a signup is a real stop, and pressing the button first would
    # spend the attempt and clear the form the user is about to be asked to finish.
    c.detect_captcha(page)
    c.detect_bot_block(page)

    names = CREATE_NAMES if creating else SIGN_IN_NAMES
    button = _gate_button(page, names)
    if button is None:
        log.info("account: no %s button on this gate", "create" if creating else "sign-in")
        return
    if not c.is_enabled_now(button):
        # The gate is holding its own button shut, which is how a modal says a box it wants is still empty.
        # Clicking anyway spends the five-second actionability wait and raises; saying so leaves the field
        # values in place for the next attempt, which fills whatever this pass missed.
        log.info("account: the %s button is still disabled — the gate wants more than was filled",
                 "create" if creating else "sign-in")
        return
    try:
        button.scroll_into_view_if_needed(timeout=c.SHORT)
    except Exception:  # noqa: BLE001
        pass
    button.click(timeout=c.MEDIUM)
    page.wait_for_timeout(SETTLE_MS)
    _settle(page)


def _credential_boxes(page: Any) -> tuple[Any, list]:
    """(the box the email goes in, the password boxes) for the gate's own form.

    Found through the password box rather than by looking for something labelled "Email": these gates label
    that box "Email Address", "User Name", "Login" or nothing at all, and the one thing they agree on is
    that it sits in the same form as the password. Returns (None, []) when the page has no password box,
    which at_gate has already ruled out by the time this is called.
    """
    try:
        boxes = page.locator("input[type=password]")
        first = next((boxes.nth(i) for i in range(min(boxes.count(), 6)) if c.is_visible_now(boxes.nth(i))), None)
    except Exception as e:  # noqa: BLE001
        log.debug("account: reading password boxes: %s", e)
        return None, []
    if first is None:
        return None, []
    scope = page
    try:
        form = first.locator("xpath=ancestor::form[1]")
        if form.count():
            scope = form.first
    except Exception:  # noqa: BLE001 - a gate with no <form> element is filled against the page
        pass
    try:
        text_boxes = scope.locator("input:not([type=password]):not([type=hidden]):not([type=submit])"
                                   ":not([type=button]):not([type=checkbox]):not([type=radio])"
                                   ":not([type=file]):not([type=search])")
        visible = [text_boxes.nth(i) for i in range(min(text_boxes.count(), 12))
                   if c.is_visible_now(text_boxes.nth(i))]
    except Exception as e:  # noqa: BLE001
        log.debug("account: reading the username box: %s", e)
        visible = []
    # Exactly one is the sign-in view's email box. More than one is the create view, where the email, its
    # retype and the names are all text boxes and the caller's own fill pass knows which is which.
    return (visible[0] if len(visible) == 1 else None), [first]


def _accept_terms(page: Any) -> bool:
    """Accept the data-privacy / terms row a signup makes required. True when something was accepted.

    Two shapes, both here because both are common: a checkbox beside the words, and a link that opens a
    dialog with its own Accept button. SuccessFactors uses the second, and validates the whole form before
    it will open the dialog — so a first click that finds the form incomplete does nothing, and the attempt
    after it (by which time the fields are filled) is the one that works. Never fatal: a gate whose terms
    cannot be found says so through its own validation, which is a better message than any guess here.
    """
    accepted = _tick_consent_boxes(page)
    try:
        links = page.get_by_role("link", name=TERMS_VERB_RE)
        for i in range(min(links.count(), 6)):
            el = links.nth(i)
            if not c.is_visible_now(el):
                continue
            name = c.clean(el.inner_text() or el.get_attribute("aria-label") or "")
            if not TERMS_SUBJECT_RE.search(name) or THIRD_PARTY_RE.search(name):
                continue
            # A link with a real href of its own is the footer's privacy page, not this form's accept
            # control; opening it navigates away from a filled signup.
            href = (el.get_attribute("href") or "").strip()
            if href and not href.startswith("#") and "javascript" not in href.lower():
                continue
            log.info("account: opening the terms with %r", name[:60])
            el.click(timeout=c.MEDIUM)
            page.wait_for_timeout(1200)
            accepted = _accept_in_dialog(page) or accepted
            break
    except Exception as e:  # noqa: BLE001
        log.debug("account: terms link: %s", e)
    return accepted


def _tick_consent_boxes(page: Any) -> bool:
    """Tick every unticked checkbox whose words are a consent. Consent is given, not asked about: a signup
    that will not proceed without it is not offering a choice the user has any reason to decline here."""
    ticked = False
    try:
        boxes = page.locator("input[type=checkbox]")
        for i in range(min(boxes.count(), 12)):
            el = boxes.nth(i)
            if not c.is_visible_now(el) or el.is_checked():
                continue
            words = c.clean(c.get_label_for(el) or c.label_context(el))
            if not TERMS_SUBJECT_RE.search(words) and not TERMS_VERB_RE.search(words):
                continue
            try:
                el.check(timeout=c.SHORT)
            except Exception:  # noqa: BLE001 - a styled box whose input is covered by its own label
                el.evaluate("e => e.click()")
            log.info("account: ticked %r", words[:80])
            ticked = True
    except Exception as e:  # noqa: BLE001
        log.debug("account: consent boxes: %s", e)
    return ticked


def _accept_in_dialog(page: Any) -> bool:
    """Press Accept in the terms dialog that has just opened. False when no dialog appeared — which is what
    SuccessFactors does when the form behind it does not yet validate."""
    button = c.named_button(page, ACCEPT_NAMES)
    if button is None:
        log.info("account: the terms link opened no dialog to accept (the form may not validate yet)")
        return False
    try:
        button.click(timeout=c.MEDIUM)
        page.wait_for_timeout(800)
        log.info("account: accepted the terms")
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("account: accepting the terms: %s", e)
        return False


def _settle(page: Any) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=c.MEDIUM)
    except Exception:  # noqa: BLE001 - a page that never settles is read as it stands
        pass


def _text(page: Any) -> str:
    try:
        return c.clean(page.evaluate("() => (document.body && document.body.innerText) || ''"))[:6000]
    except Exception:  # noqa: BLE001
        return ""
