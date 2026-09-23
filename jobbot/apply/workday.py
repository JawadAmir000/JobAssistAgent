"""Workday adapter (*.myworkdayjobs.com).

Workday is the biggest ATS jobbot was missing and the most awkward to drive, for one structural reason: every
employer runs its own tenant, so there is no "log into Workday" — there is an account per company, created
with an email and a password, sometimes confirmed by an emailed code. That is why sessions here stay per
company (the default) rather than shared the way LinkedIn's are: a Salesforce account is not a Cloudera one.

What makes it tractable is that Workday labels every control with a stable `data-automation-id`. Those names
survive releases, unlike the hashed class names LinkedIn's member UI ships, so the selectors below are the
durable part of this file.

The flow: accept the legal notice, press Apply, take the "Autofill with Resume" route when the tenant offers
it, create the account (or sign into the one a previous application created), then walk the multi-step form
(My Information, My Experience, Questions, Voluntary Disclosures, Review) pressing Next until the review page
offers Submit. Every step is checked before it is taken and skipped when the page is already past it, so a
re-run after a pause continues rather than starting over.

The account is made by jobbot, not asked about. The password comes from credentials.py — generated once and
reused at every employer, which is what makes signing back into last week's account possible — and a tenant
that already knows the email is signed into instead. Stopping to ask a person to invent a password per
employer was the single biggest source of pauses in a Workday run.

Honest limits, each of which stops and asks rather than guessing: an emailed confirmation code when no
mailbox is set up; an account that exists with a password jobbot does not know; and anything the page does
after Submit that does not look like a confirmation.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from jobbot import credentials, mail
from jobbot.apply import common as c
from jobbot.apply import navigator
from jobbot.apply.base import Adapter, ApplyContext, ApplyError, NeedsHuman, register

log = logging.getLogger(__name__)

NAV_TIMEOUT = 30000
STEP_WAIT = 2000          # ms - Workday re-renders the whole step between Next presses
MAX_STEPS = 12            # a real application is 5-6 steps; this is the runaway guard
RENDER_WAIT = 15000       # ms - how long a step gets to draw itself before we read what is on it
ACCOUNT_ATTEMPTS = 3      # create, sign in, and one more move once the page has said what it wants
MAX_VERIFICATIONS = 1     # one resend-and-follow per run; more than that is a tenant we are not getting into
# Where a step's controls live, narrowest first. applyFlowPage is Workday's own container for the application;
# "body" is the last resort so that a tenant that renames it still gets filled rather than silently skipped.
STEP_SCOPES = ("[data-automation-id='applyFlowPage']", "form", "body")
SETTLE_POLL = 600         # ms - between samples of the step's field count
MOVE_POLL = 1000          # ms - between checks that Save and Continue has taken
MOVE_SAMPLES = 20         # so a step gets ~20s to save before it is called stuck
# How far back to look for the verification mail. Workday sends it the moment the account is created, and
# the "verify your account" notice only appears on the sign-in that follows — so searching from the sign-in
# looks past the very mail it is waiting for. Machine and mail-server clocks disagree by a little, too.
MAIL_LOOKBACK_S = 21600   # 6h, for the mail sent by an earlier run's signup
MAIL_SKEW_S = 120         # clocks here and at the mail server disagree by a little
# The page offers this the moment it refuses an unverified account, and it is the reliable route: a fresh
# mail lands in seconds, where the original may be hours old, already opened, or sitting in another folder.
RESEND_TEXT = re.compile(r"resend account verification|request a verification email|resend verification", re.I)
SETTLE_SAMPLES = 10       # so a step has ~6s to stop changing before it is read

# Workday's "prompt": a search box whose value can only come from the popup list beside it, and whose list is
# a tree — "Job Board" opens onto Indeed, LinkedIn Jobs, Naukri. Typing into one filters nothing, so a value
# typed in looks right on screen and counts as empty when Save and Continue is pressed.
PROMPT_INPUT = "[data-automation-id='multiselectInputContainer'] input"
# Tried in order, not together: Workday's options match both, and one comma-joined selector offered every
# option to the resolver twice ("Job Board, Job Board, Recruiting Event, Recruiting Event, ...").
PROMPT_OPTIONS = ("[data-automation-id='promptOption']:visible", "[role=option]:visible")
# What a search-as-you-type prompt shows before anything has been typed into it. It is drawn with the same
# markup as a real option, so it was read as one and offered to the resolver as the only thing on the menu
# (application 121, "Type to Add Skills" -> "it offered: No Items.").
PROMPT_EMPTY_RE = re.compile(r"^(?:no items\.?|no results?(?: found)?\.?|no matches?(?: found)?\.?|"
                             r"start typing|type to search|loading\.{0,3})$", re.I)
SELECTED_ITEM = "[data-automation-id='selectedItem']"
PROMPT_LEVELS = 3         # how deep the option tree is walked before giving up on it
# Workday's other dropdown: a plain <button aria-haspopup="listbox"> reading "Select One", with the question
# on the formField wrapper around it. It is not an <input>, a <select> or a [role=combobox], so every
# label-driven walker looks straight past it — which left five required Application Questions empty and the
# step refusing to move, with no warning logged because nothing had been skipped: nothing had been seen.
SELECT_BUTTON = "button[aria-haspopup='listbox']"
PLACEHOLDER_RE = re.compile(r"^\s*(?:select one|select\.{0,3}|choose one|-{0,2}\s*select\s*-{0,2}|)\s*$", re.I)
# The My Experience dropzone. Workday draws "Drop files here or Select files" and creates the file input
# only once that button is pressed, so there is nothing for an ordinary upload to attach the CV to.
SELECT_FILES = ("select-files", "fileUploadButton", "attachmentButton")
SELECT_FILES_TEXT = re.compile(r"select files?|attach (?:a )?(?:file|resume|cv)|upload (?:a )?file", re.I)
DROPZONE = ("[data-automation-id='file-upload-drop-zone']", "[data-automation-id='fileUploadDropZone']",
            "[data-automation-id*='drop' i]")
DROPZONE_TEXT = re.compile(r"drop files?\s+here", re.I)
ATTACHED = "[data-automation-id='file-upload-item'], [data-automation-id='deleteFile'], " \
           "[data-automation-id='filename']"
PROMPT_WAIT = 1200        # ms - Workday renders the option list in a portal a beat after the click

# data-automation-id is Workday's own contract with its test suite, so these are the stable selectors.
AID = "[data-automation-id='{}']"
LEGAL_ACCEPT = ("legalNoticeAcceptButton", "cookiePolicyAcceptButton", "acceptCookiesButton")
# "Apply" on a fresh posting; "Continue Application" once a draft exists — which it does as soon as jobbot
# has been through here once, so without the second name a re-run reports the posting as closed.
APPLY = ("adventureButton", "continueApplicationButton", "resumeApplicationButton")
APPLY_TEXT = re.compile(r"^\s*(?:apply|continue application|resume application|finish application)\b", re.I)
# How to start the application, best first. "Autofill with Resume" hands Workday the CV and lets it fill the
# name, contacts and — the part that matters — the employment and education blocks, which are otherwise a
# dozen date fields per job. Whatever it fills is re-read by _fill_step afterwards: empty controls are filled
# from facts.yaml and anything still unanswered is asked about, so the parse is a head start, not a trusted
# source. "Use My Last Application" is deliberately absent: it copies an application jobbot cannot see.
APPLY_ROUTES = ("autofillWithResume", "applyManually")
AUTOFILL_ROUTE = "autofillWithResume"
SIGN_IN_EMAIL = ("email", "userName")
PASSWORD = "password"
VERIFY_PASSWORD = ("verifyPassword", "confirmPassword")
# Some tenants (Cloudera) ask how you want to sign in before showing any fields at all. Only the email route
# is ever taken: the SSO buttons next to it hand the employer's board a Google/LinkedIn login, which is the
# user's own account and an OAuth dance jobbot has no business driving.
EMAIL_AUTH = ("SignInWithEmailButton", "signInWithEmailButton", "emailSignInButton")
SSO_AUTH = ("GoogleSignInButton", "LinkedInSignInButton", "AppleSignInButton")   # documented, never clicked
CREATE_ACCOUNT_LINK = ("createAccountLink", "createAccountCheckbox")
SIGN_IN_LINK = ("signInLink",)
ACCOUNT_SUBMIT = ("createAccountSubmitButton", "signInSubmitButton")
# The transparent div Workday lays over its real buttons. It is inert — clicking it sends no request at all —
# and it is why every click here is forced past it (see _click_first). Never a click target of its own: there
# is one per button and the first on the page is rarely the one that was wanted.
CLICK_SHIELD = "click_filter"
NEXT = ("bottom-navigation-next-button", "pageFooterNextButton", "wd-CommandButton")
CONTINUE = ("continueButton", "bottom-navigation-next-button", "wd-CommandButton")
SUBMIT_NAMES = ("Submit", "Submit Application", "Review and Submit")

ACCOUNT_EXISTS = re.compile(r"account already exists|email address is already in use"
                            r"|an account with this email", re.I)
# Workday prints these in the page when the credentials it was given do not work. They separate "this account
# is not ours" — the one case a person really has to settle — from an ordinary validation complaint.
SIGN_IN_FAILED = re.compile(r"(?:incorrect|invalid|wrong) (?:email|user\s*name|password)"
                            r"|(?:email|password) (?:you entered )?(?:is|was) (?:not )?(?:in)?correct"
                            r"|could not (?:be )?sign(?:ed)? in"
                            r"|account (?:might be|may be|is|has been) locked", re.I)
PASSWORD_REJECTED = re.compile(r"password (?:does not|doesn'?t|must) \w+|password requirements", re.I)
# The gate behind every "wrong password" this adapter met: a Workday account cannot be signed into until the
# link Workday mails has been followed. It says so only on the sign-in that follows the signup, and says it
# in the same breath as offering to resend, which is why it read as a bad password for so long.
ACCOUNT_UNVERIFIED = re.compile(
    r"verify your account|account (?:is )?not (?:yet )?verified|before you sign in"
    r"|request a verification email|check your email to verify", re.I)


@register
class WorkdayAdapter(Adapter):
    ats = "workday"
    # Deliberately False: accounts are per tenant, so each company keeps its own session file. The flag
    # controls session sharing, and sharing one across employers would send Salesforce cookies to Cloudera.
    needs_account = False

    def apply(self, ctx: ApplyContext) -> None:
        page = ctx.page
        c.require_open(page)

        self._dismiss_legal(page)
        c.raise_if_already_applied(page)

        ctx.step("Starting the Workday application")
        self._start(ctx)
        self._account(ctx)
        self._walk_steps(ctx)

    # ---------- phases ----------
    def _dismiss_legal(self, page) -> None:
        """Workday's legal/cookie banner overlays the Apply button and swallows the click."""
        for aid in LEGAL_ACCEPT:
            if self._click_first(page, (aid,)):
                page.wait_for_timeout(800)
        c.dismiss_cookie_banner(page)

    def _start(self, ctx: ApplyContext) -> None:
        """Press Apply, then take the CV route through the menu Workday offers."""
        page = ctx.page
        if self._on_form(page) or self._on_account_page(page):
            return      # a resume landed us mid-application, or on the signup _account handles next

        if not self._press_apply(page):
            raise NeedsHuman(
                "No Apply or Continue Application button on this Workday page — the posting may be closed. "
                "Check the browser window, finish it there if it is open, then click Continue.")
        page.wait_for_timeout(STEP_WAIT)

        self._choose_route(ctx)

    def _press_apply(self, page) -> bool:
        """Start (or pick up) the application. Named on the accessible label when the automation id is one
        this tenant does not use."""
        return self._click_first(page, APPLY) or self._click_by_name(page, APPLY_TEXT)

    @staticmethod
    def _click_by_name(page, pattern, roles: tuple[str, ...] = ("link", "button")) -> bool:
        """Click a control by its accessible name, forced past the shield like every other click here."""
        for role in roles:
            try:
                control = page.get_by_role(role, name=pattern)
                if not c.visible(control, c.SHORT):
                    continue
            except Exception as e:  # noqa: BLE001
                log.debug("workday: %s by name: %s", role, e)
                continue
            for force in (False, True):
                try:
                    control.first.click(timeout=c.SHORT, force=force)
                    return True
                except Exception as e:  # noqa: BLE001
                    log.debug("workday: click %s force=%s: %s", role, force, str(e).splitlines()[0][:100])
        return False

    def _choose_route(self, ctx: ApplyContext) -> None:
        """Pick how the application starts: autofill from the CV where the tenant offers it, else manual.

        Order is the whole point — see APPLY_ROUTES. Tenants differ on when they ask for the file: some open
        a picker straight away, others create the account first and ask on the My Information step, which
        _fill_step covers. Both are handled by trying the upload here and shrugging if there is nothing yet.
        """
        page = ctx.page
        for aid in APPLY_ROUTES:
            if not self._click_first(page, (aid,)):
                continue        # this tenant does not offer that route
            page.wait_for_timeout(STEP_WAIT)
            if aid == AUTOFILL_ROUTE:
                ctx.step("Autofilling from your CV")
            self._wait_for_render(page)
            # Tenants that ask for the file straight away get it here; the ones that put Create Account
            # first (most of them) have no file input yet, and their autofill step is walked like any other.
            if aid == AUTOFILL_ROUTE and c.upload_resume(page, ctx.cv_path):
                page.wait_for_timeout(STEP_WAIT)     # Workday parses the CV before it will move on
                self._click_first(page, CONTINUE)
                page.wait_for_timeout(STEP_WAIT)
            return
        log.debug("workday: no apply-route menu on %s", page.url)

    def _account(self, ctx: ApplyContext) -> None:
        """Create this tenant's account (or sign into the one an earlier application created).

        Never asks for a password: it is generated once and kept in the keychain (credentials.py), so the
        account jobbot makes here is one it can come back to. Skipped when the form is already open.
        """
        page = ctx.page
        if self._on_form(page) or not self._on_account_page(page):
            return

        email = ctx.fact("identity.email")
        if not email:
            raise NeedsHuman("No email in facts.yaml, so jobbot cannot create the Workday account.")
        password = credentials.account_password()
        # Before the first attempt, not after it: this is the window the verification mail will land in.
        started = datetime.now(timezone.utc) - timedelta(seconds=MAIL_LOOKBACK_S)

        attempt, budget, just_verified, verifications = 0, ACCOUNT_ATTEMPTS, False, 0
        while attempt < budget:
            # Every time round, not once before the loop: Workday answers a submitted signup by bouncing
            # back to "Sign in with Google / with email", and an attempt that starts there has no fields to
            # fill at all. Two attempts were being spent typing into a screen that had none.
            self._take_email_route(page)
            if just_verified:
                # The account exists and has just been activated: sign in. Offering to create it again
                # spends the last attempt on a signup Workday is bound to refuse.
                self._to_sign_in(page)
                creating, just_verified = False, False
            else:
                creating = self._next_move(page, attempt)
            ctx.step("Creating your account with this employer" if creating else "Signing in")
            attempt += 1
            sent_at = self._submit_credentials(page, email, password, creating)
            self._verification_code(ctx, sent_at)
            if self._on_form(page) or not self._on_account_page(page):
                return
            # Judged per attempt, not once at the end: a rejected sign-in means this employer's account is
            # not one jobbot can open, and creating another for the same email would only fail as well.
            text = self._text(page)
            if ACCOUNT_UNVERIFIED.search(text):
                # Counted, because the round it buys is bought against a budget: a tenant that keeps saying
                # "unverified" after the link has been followed would otherwise extend the budget every time
                # round and loop for as long as the browser stayed open.
                if verifications < MAX_VERIFICATIONS and self._verify_account(ctx, started):
                    verifications += 1
                    budget, just_verified = budget + 2, True
                    continue
                configured, _why = mail.is_configured()
                how = ("jobbot could not find it in your mailbox" if configured else
                       "set JOBBOT_MAIL_PASSWORD in Settings -> Secrets and jobbot will open it itself")
                raise NeedsHuman(
                    f"Workday emailed a verification link to {email} and will not let the new account sign "
                    f"in until it is opened ({how}). Click the link in that mail, then click Continue.")
            if PASSWORD_REJECTED.search(text):
                raise NeedsHuman(
                    "This employer's Workday rejected the password jobbot generated. Set one it accepts in "
                    "the browser window, save it in Settings -> Secrets as "
                    f"{credentials.SECRET_NAME}, then click Continue.")
            if not creating and SIGN_IN_FAILED.search(text):
                raise NeedsHuman(
                    f"There is already an account at this employer for {email}, and it does not take the "
                    f"password jobbot manages. Two ways on, both in the browser window that is open: sign "
                    f"in by hand, or use 'Forgot your password?' and set it to the one in "
                    f"{credentials.location_hint()} — that second one means jobbot gets in by itself here "
                    "from now on. Then click Continue.")
        raise NeedsHuman(
            "Workday would not let jobbot past the sign-in for this employer. Finish signing in (or "
            "creating the account) in the browser window, then click Continue.")

    def _take_email_route(self, page) -> None:
        """Answer the "how do you want to sign in?" screen with "Sign in with email".

        A screen with no fields on it read as "not the account page and not the form", so the walker treated
        it as a step, filled nothing, found no Next button and stopped. The SSO buttons beside it are never
        pressed — see SSO_AUTH.
        """
        if self._on_password_page(page):
            return              # already past the chooser, or this tenant never had one
        if self._click_first(page, EMAIL_AUTH):
            page.wait_for_timeout(STEP_WAIT)
            self._wait_for_render(page)

    def _next_move(self, page, attempt: int) -> bool:
        """Whether this attempt should create the account (True) or sign into it (False).

        Read off the page every time round rather than decided once, because tenants disagree about what
        happens after a signup: some drop straight into the application, some say the email is already
        taken, and some — Salesforce is one — bounce back to an empty Sign In card with no message at all,
        which is what made a successful signup look like a failure. A sign-in the tenant rejected means the
        account was never created after all, so the next move there is to create it.
        """
        text = self._text(page)
        if ACCOUNT_EXISTS.search(text):
            # The password is reused across tenants, so an account on this email is almost certainly one
            # jobbot created on an earlier application here.
            self._to_sign_in(page)
            return False
        if SIGN_IN_FAILED.search(text):
            return self._to_create_account(page)
        if self._is_create_form(page):
            return True
        if attempt == 0:
            return self._to_create_account(page)    # an account of our own, before falling back to sign-in
        return False

    def _submit_credentials(self, page, email: str, password: str, creating: bool):
        """Fill the account form and press its button. Returns when it was sent, for the code mail lookup."""
        self._fill_first(page, SIGN_IN_EMAIL, email)
        self._fill_first(page, (PASSWORD,), password)
        if creating:
            self._fill_first(page, VERIFY_PASSWORD, password)
            self._tick_account_checkbox(page)
        # Belt and braces for tenants whose boxes carry no data-automation-id jobbot knows: any password box
        # still empty gets the same password, so "Verify New Password" can never be left behind.
        c.fill_account_password(page, password)
        sent_at = datetime.now(timezone.utc)
        self._click_first(page, ACCOUNT_SUBMIT)
        page.wait_for_timeout(STEP_WAIT)
        self._wait_for_render(page)
        # Logged, not raised on: a signup that did not take says so in the page, and without this line the
        # only record of why is a screenshot nobody reads until the run has already stopped.
        errors = c.form_errors(page)
        if errors:
            log.info("workday: after %s the page says %s", "create account" if creating else "sign in",
                     "; ".join(errors[:3])[:300])
        return sent_at

    def _verify_account(self, ctx: ApplyContext, sent_at: datetime) -> bool:
        """Follow the verification link Workday mailed, so the account it just created can be signed into.

        Workday will not let a new account sign in until the link in its "Verify your candidate account"
        mail has been opened — the single thing standing between a created account and a filled application.
        The mail carries a link, not a code, so fetch_code would never find anything in it.

        A fresh one is requested first. The original may have been sent by a run hours ago, or already
        opened, and "Resend Account Verification" is offered on the very page that refuses the sign-in.
        With no mailbox configured this returns False and the caller asks the user to open it themselves.
        """
        page = ctx.page
        since = sent_at
        if self._click_by_name(page, RESEND_TEXT):
            ctx.step("Asked Workday to resend the verification email")
            since = datetime.now(timezone.utc) - timedelta(seconds=MAIL_SKEW_S)
            page.wait_for_timeout(STEP_WAIT)
        ctx.step("Opening the verification link from your mailbox")
        link = mail.fetch_link(since, hints=("workday", (ctx.job.get("company") or "").lower()))
        if not link:
            return False
        try:
            page.goto(link, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            page.wait_for_timeout(STEP_WAIT)
            log.info("workday: followed the verification link")
            page.goto(ctx.job["url"], wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            page.wait_for_timeout(STEP_WAIT)
            self._dismiss_legal(page)
            self._start(ctx)
            self._wait_for_render(page)
        except Exception as e:  # noqa: BLE001
            log.warning("workday: verification link did not open: %s", e)
            return False
        return True

    def _verification_code(self, ctx: ApplyContext, sent_at: datetime) -> None:
        """Some tenants email a code before they let a new account through."""
        page = ctx.page
        prompt = c.verification_prompt(page)
        if prompt is None:
            return
        ctx.step("Reading the confirmation code from your mailbox")
        code = mail.fetch_code(sent_at, length=int(prompt.get("length") or 6), hints=("workday",))
        if not code:
            raise NeedsHuman(
                "Workday emailed a confirmation code. Set JOBBOT_MAIL_PASSWORD in Settings -> Secrets to let "
                "jobbot read it, or paste the code into the browser window and click Continue.")
        c.fill_verification_code(page, code)
        self._click_first(page, ACCOUNT_SUBMIT + NEXT)
        page.wait_for_timeout(STEP_WAIT)

    def _walk_steps(self, ctx: ApplyContext) -> None:
        """Fill the visible step, press Next, repeat until the review page offers Submit.

        Workday keeps one step on screen at a time, so this is the same fill-and-answer pass the other
        adapters make once, run per step. Progress is measured by the step heading changing: when Next stops
        moving us the form is either done or stuck, and both are reported rather than looped on.
        """
        page = ctx.page
        seen_headings: list[str] = []
        for _ in range(MAX_STEPS):
            self._wait_for_render(page)
            c.detect_captcha(page)
            # Not only at the top of apply(): a tenant that requires an account shows the public job page
            # first and only says "You've already applied for this job." once the sign-in is through. Read
            # before the check moved here, that notice was a step with nothing to fill and no Next button,
            # and the run asked the user to finish by hand an application that was a day old.
            c.raise_if_already_applied(page)
            if self._on_account_page(page):
                # Some tenants only ask for the account after the route is chosen, so the walk can start
                # here. _account either gets past it or stops and says why; it is never filled as a step.
                self._account(ctx)
                page.wait_for_timeout(STEP_WAIT)
                continue
            heading = self._heading(page)
            ctx.step(f"Workday: {heading or 'application form'}")

            self._fill_step(ctx)

            if self._submit_if_review(ctx):
                return

            if self._cv_required_here(page):
                raise NeedsHuman(
                    "This Workday step wants your CV attached and jobbot could not drop it on the upload "
                    "area. Attach it in the browser window, then click Continue.")

            before = self._state(page)
            if not self._click_first(page, NEXT):
                # The step may simply not have finished drawing — the Review page arrives as an empty panel
                # and fills in a beat later, which read as "this step has no Next button" on the very last
                # screen of a completed application. Give it another look before giving up on it.
                self._wait_for_render(page)
                if self._submit_if_review(ctx):
                    return
                if not self._click_first(page, NEXT):
                    # A tenant that renames its own buttons. The automation ids above are Workday's, but a
                    # tenant may ship its own footer, so let the model name the control from the ones on
                    # the page before handing a filled step back to the user (see navigator.py).
                    if not navigator.press_next(ctx, "move this Workday application to its next step"):
                        raise NeedsHuman(
                            f"jobbot filled the '{heading or 'current'}' step but found no Next button. "
                            "Continue in the browser window, then click Continue here.")

            if not self._wait_for_move(page, before):
                # Next did not move: Workday rendered a validation error in place. Before handing it back,
                # fill the step once more — the error names a field that was left empty, and a second pass
                # is how the generic walker recovers the same situation. Only once: a step that refuses
                # twice is refusing something jobbot cannot supply, and looping on it helps nobody.
                errors = c.form_errors(page)
                if errors:
                    log.warning("workday: '%s' was refused (%s); filling it again", heading, errors[:3])
                    ctx.step(f"Workday: {heading or 'step'} bounced — filling it again")
                    self._fill_step(ctx)
                    before = self._state(page)
                    self._click_first(page, NEXT)
                if not self._wait_for_move(page, before):
                    # Say which field it was — the message used to guess ("probably asking for something"),
                    # and the answer was sitting in the page the whole time, in the red text under the
                    # control that was left empty.
                    errors = c.form_errors(page) or errors
                    detail = (" It is asking for: " + "; ".join(errors[:3])[:240]) if errors else ""
                    raise NeedsHuman(
                        f"Workday would not move past '{heading or 'this step'}'.{detail} Fill it in the "
                        "browser window, then click Continue — what you type there is remembered for next "
                        "time.")
            seen_headings.append(heading)

        raise ApplyError(f"Workday application did not finish in {MAX_STEPS} steps ({seen_headings}).")

    def _state(self, page) -> str:
        """A fingerprint of the screen, for deciding whether a step moved on.

        Every heading inside the flow, not just the first: tenants disagree about which one names the step.
        Cloudera puts the job title above it, so the first heading is the same on all five steps and a walk
        keyed on it could not tell a completed save from a stuck one. Joining them all sidesteps the
        question — what matters is only that the string changes when the screen does.
        """
        try:
            headings = page.evaluate(
                """(scope) => { const root = document.querySelector(scope) || document.body;
                    return [...root.querySelectorAll('h1,h2,h3')].map(e => e.innerText || '').join(' | '); }""",
                STEP_SCOPES[0])
        except Exception as e:  # noqa: BLE001
            log.debug("workday: reading step headings: %s", e)
            headings = self._heading(page)
        return (page.url or "") + "|" + c.clean(headings)[:300]

    def _wait_for_move(self, page, before: str) -> bool:
        """Wait for Save and Continue to actually take.

        Workday saves the step on the server before it renders the next one: for several seconds the footer
        button sits disabled and the screen still shows the step just filled. Judging that after a flat two
        seconds called a slow save a validation error and stopped a run whose form was entirely correct —
        the screenshot showed a complete My Information and a greyed-out button mid-save.
        """
        for _ in range(MOVE_SAMPLES):
            page.wait_for_timeout(MOVE_POLL)
            if self._state(page) != before:
                return True
            if c.form_errors(page):
                return False        # it came back with something to fix; no point waiting out the clock
        return False

    def _fill_step(self, ctx: ApplyContext) -> None:
        """One step's worth of fields, reusing the generic walker: Workday's markup is ordinary inputs,
        selects and fieldsets once you are inside a step.

        The CV goes in first. Workday parses it and re-renders the whole step, so anything typed before the
        upload is thrown away — and the parse fills employment history and education, which is most of what
        these steps ask for. The passes after it only write into controls the parse left empty.
        """
        page = ctx.page
        walker = self._step_walker(page)
        if self._upload_cv(ctx):
            page.wait_for_timeout(STEP_WAIT)
            self._wait_for_fields(page)     # the parse re-renders the step it just filled
        # Prompts first, identity second. The country dial code lives in a prompt, and the phone number
        # beside it is only right once that code is on screen: filling the number first wrote the full
        # +8801XXXXXXXXX into a box whose form was already saying +880, and Workday rejected it.
        # The split itself now lives in common.dial_code_for_phone, which _identity calls; it skips prompts,
        # so it reads the code this step has already set rather than fighting _prompts for the control.
        self._prompts(ctx)
        walker._identity(ctx)
        c.fill_account_password(page)       # a tenant that asks for the password again mid-flow
        c.fill_cover_letter(ctx)
        walker._questions(ctx)              # the walker leaves prompts alone; _prompts owns them

    def _upload_cv(self, ctx: ApplyContext) -> bool:
        """Attach the CV to this step, through the dropzone when that is all there is.

        Workday's My Experience step renders a dropzone, not an input: the <input type=file> does not exist
        until "Select files" is pressed, which is why the ordinary upload found nothing to attach to and the
        step went on with no CV — the one attachment the whole application is for.
        """
        page = ctx.page
        if not ctx.cv_path:
            return False
        if c.upload_resume(page, ctx.cv_path):
            return True
        try:
            with page.expect_file_chooser(timeout=c.MEDIUM) as chooser:
                if not self._click_first(page, SELECT_FILES):
                    page.get_by_text(SELECT_FILES_TEXT).first.click(timeout=c.MEDIUM)
            chooser.value.set_files(ctx.cv_path)
            page.wait_for_timeout(STEP_WAIT)
            log.info("workday: attached the CV through the file chooser")
            return True
        except Exception as e:  # noqa: BLE001 - a step with no upload on it lands here too
            log.debug("workday: file chooser: %s", e)
        zone = self._dropzone(page)
        if zone is not None and c.drop_file(page, zone, ctx.cv_path):
            page.wait_for_timeout(STEP_WAIT)
            if self._cv_attached(page):
                log.info("workday: dropped the CV on the upload area")
                return True
        return False

    @staticmethod
    def _dropzone(page):
        """The upload area, by automation id or by the words it shows."""
        for selector in DROPZONE:
            try:
                zone = page.locator(selector)
                if zone.count() and c.is_visible_now(zone.first):
                    return zone.first
            except Exception as e:  # noqa: BLE001
                log.debug("workday: dropzone %s: %s", selector, e)
        try:
            zone = page.get_by_text(DROPZONE_TEXT)
            if zone.count():
                return zone.first
        except Exception as e:  # noqa: BLE001
            log.debug("workday: dropzone by text: %s", e)
        return None

    @staticmethod
    def _cv_attached(page) -> bool:
        try:
            return bool(page.locator(ATTACHED).count())
        except Exception:  # noqa: BLE001
            return False

    def _cv_required_here(self, page) -> bool:
        """True when this step is an upload step that still has nothing attached."""
        return self._dropzone(page) is not None and not self._cv_attached(page)

    def _prompts(self, ctx: ApplyContext) -> None:
        """Answer both of Workday's list controls on this step: the search prompts and the Select One
        dropdowns. They differ only in what counts as answered; the picking is the same popup either way."""
        page = ctx.page
        for selector, answered in ((PROMPT_INPUT, self._prompt_answered),
                                   (SELECT_BUTTON, self._select_answered)):
            controls = page.locator(selector)
            try:
                count = controls.count()
            except Exception as e:  # noqa: BLE001
                log.debug("workday: locating %s: %s", selector, e)
                continue
            for i in range(count):
                el = controls.nth(i)
                try:
                    if not c.is_visible_now(el):
                        continue
                    label = self._field_label(el)
                    if answered(el):
                        if not self._wrong_source(ctx, el, label):
                            continue
                        ctx.step(f"Correcting '{label}'")
                        self._clear_prompt(page, el)
                    if not label or c.is_honeypot(el, label):
                        continue
                    self._fill_prompt(ctx, el, label, answered)
                except (NeedsHuman, ApplyError):
                    raise
                except Exception as e:  # noqa: BLE001 - one awkward control must not lose the application
                    log.warning("workday: list control %d (%s): %s", i, selector, e)

    @staticmethod
    def _wrong_source(ctx: ApplyContext, el, label: str) -> bool:
        """True when a "how did you hear about us" control already holds something other than the source
        facts.yaml states.

        That answer is policy, not the employer's to default and not an earlier run's to bequeath: a draft
        left holding "YouTube" would be submitted saying the candidate found the job on YouTube. Only this
        one question is second-guessed; every other filled control is left exactly as it is.
        """
        from jobbot.apply.resolver import DEFAULT_JOB_SOURCE, is_source_question
        # The same default the resolver answers with, so the check and the answer cannot disagree.
        source = ctx.fact("preferences.job_source") or DEFAULT_JOB_SOURCE
        if not label or not source or not is_source_question(label):
            return False
        try:
            current = c.clean(el.inner_text() if hasattr(el, "inner_text") else "") or c.current_value(el)
        except Exception:  # noqa: BLE001
            return False
        holder = c.clean(WorkdayAdapter._selected_text(el)) or current
        if not holder:
            return False
        return source.lower().replace(" ", "") not in holder.lower().replace(" ", "")

    @staticmethod
    def _clear_prompt(page, el) -> None:
        """Empty a prompt so it can be answered again. Workday's chip says how: press delete in the box."""
        try:
            el.click(timeout=c.SHORT)
            page.keyboard.press("Delete")
            page.wait_for_timeout(600)
        except Exception as e:  # noqa: BLE001
            log.debug("workday: clearing a prompt: %s", e)

    @staticmethod
    def _field_label(el) -> str:
        """The question a control belongs to.

        Workday hangs the label on the formField wrapper rather than on the control, so asking the control
        for its own label returns the placeholder it is showing — "Select One", which is not a question and
        which the resolver can make nothing of.
        """
        try:
            text = el.evaluate(
                """e => { const f = e.closest("[data-automation-id^='formField']");
                    if (!f) return '';
                    const l = f.querySelector('label, legend, [class*="label" i]');
                    return l ? (l.innerText || '') : ''; }""")
        except Exception as e:  # noqa: BLE001
            log.debug("workday: field label: %s", e)
            text = ""
        return c.strip_required(c.clean(text)) or c.strip_required(c.get_label_for(el))

    @staticmethod
    def _selected_text(el) -> str:
        """What this control is showing as its answer, from the chip Workday renders beside it."""
        try:
            return el.evaluate(
                """e => { const f = e.closest("[data-automation-id^='formField']")
                    || e.closest("[data-automation-id='multiSelectContainer']") || e.parentElement;
                    const chip = f && f.querySelector("[data-automation-id='selectedItem']");
                    return chip ? (chip.innerText || '') : ''; }""") or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _select_answered(el) -> bool:
        """A Select One button shows the chosen value once there is one."""
        try:
            return not PLACEHOLDER_RE.match(c.clean(el.inner_text() or ""))
        except Exception:  # noqa: BLE001
            return False

    def _fill_prompt(self, ctx: ApplyContext, box, label: str, answered=None) -> None:
        """Pick a value for one prompt, a level of its tree at a time.

        The list is not searchable — typing "LinkedIn" into "How Did You Hear About Us?" leaves the same five
        categories on screen — so the answer is chosen from what is actually offered: the resolver picks
        "Job Board" from the categories, that opens the boards, and it picks "LinkedIn Jobs" from those. Each
        level is a real question with real options, which is also why the answer needs no special-casing:
        the source policy in resolver.py recognises both lists for what they are.
        """
        page = ctx.page
        answered = answered or self._prompt_answered
        options = self._open_prompt(page, box)
        if not options:
            # Nothing on the menu: this is the other kind of prompt, one that only lists what you have
            # typed a query for. See _search_prompt.
            options = self._search_prompt(ctx, page, box, label)
        seen: list[str] = []
        for _ in range(PROMPT_LEVELS):
            if not options:
                break
            texts = [text for text, _ in options]
            seen = texts
            answer = ctx.answer(label, texts, "select")
            match = self._match_option(options, answer)
            log.info("workday: %r offered %s -> %r%s", label, texts[:8], answer,
                     "" if match else " (no option matches)")
            if match is None:
                break
            match.click(timeout=c.MEDIUM)
            page.wait_for_timeout(PROMPT_WAIT)
            if answered(box):
                return
            options = self._prompt_options(page)    # the click opened the next level of the tree
        raise NeedsHuman(
            f"jobbot could not pick a value for '{label}' from this Workday list"
            + (f" (it offered: {', '.join(seen[:8])})" if seen else " (the list did not open)")
            + ". Choose one in the browser window, then click Continue — it is remembered for next time.",
            question=label, options=seen[:25], kind="select")

    def _search_prompt(self, ctx: ApplyContext, page, box, label: str) -> list:
        """Options for a prompt that lists nothing until a query is typed into it.

        The docstring on _fill_prompt says Workday's lists are not searchable, and for the tree-shaped ones
        ("How Did You Hear About Us?") that is true. Some are the opposite and say so in their own label:
        "Type to Add Skills" shows "No Items." until you type, so the walk opened it, read that placeholder
        as the only option on the menu, and stopped to ask the user (application 121, Southern Cross Health
        Insurance -- "it offered: No Items."). PROMPT_EMPTY_RE now keeps the placeholder out of the options;
        this puts something real in.

        The query is the answer itself -- the resolver knows the candidate's skills from facts.yaml -- and
        then its first word, because "Amazon Bedrock AgentCore" matches nothing in a list that holds
        "Amazon Web Services" while "Amazon" matches plenty.
        """
        try:
            query = c.clean(ctx.answer(label, None, "text"))
        except NeedsHuman:
            raise                       # the resolver wants the user; that is a real pause, not a miss
        except Exception as e:          # noqa: BLE001
            log.debug("workday: no query for %r: %s", label, e)
            return []
        if not query:
            return []
        tried: list[str] = []
        for term in (query, query.split()[0] if query.split() else ""):
            if not term or term in tried:
                continue
            tried.append(term)
            try:
                box.click(timeout=c.SHORT)
                box.fill("", timeout=c.SHORT)
                box.press_sequentially(term, delay=40, timeout=c.MEDIUM)
            except Exception as e:      # noqa: BLE001
                log.debug("workday: typing %r into %r: %s", term, label, e)
                continue
            page.wait_for_timeout(PROMPT_WAIT)
            options = self._prompt_options(page)
            log.info("workday: %r lists nothing until typed; %r offered %d option(s)",
                     label, term, len(options))
            if options:
                return options
        return []

    def _open_prompt(self, page, box) -> list:
        """Open the popup and return its options, giving the list a second chance to appear.

        Workday renders the list in a portal a beat after the click, and on a slow step the first look finds
        an empty page. Pressing the box again (or the down arrow, which is what its aria hint tells a
        keyboard user to do) is cheaper than treating a slow popup as an unanswerable question.
        """
        for attempt in (1, 2):
            try:
                box.click(timeout=c.MEDIUM)
            except Exception as e:  # noqa: BLE001
                log.debug("workday: opening prompt: %s", e)
            page.wait_for_timeout(PROMPT_WAIT)
            options = self._prompt_options(page)
            if options:
                return options
            if attempt == 1:
                try:
                    box.press("ArrowDown", timeout=c.SHORT)
                except Exception as e:  # noqa: BLE001
                    log.debug("workday: arrow-down on prompt: %s", e)
                page.wait_for_timeout(PROMPT_WAIT)
                options = self._prompt_options(page)
                if options:
                    return options
        return []

    @staticmethod
    def _match_option(options: list, answer: str):
        """The option the resolver meant. Exact first, then either way round: a list that offers "LinkedIn
        Jobs" is answering the same question as one that offers "LinkedIn"."""
        want = c.clean(answer).lower()
        if not want:
            return None
        for text, el in options:
            if c.clean(text).lower() == want:
                return el
        for text, el in options:
            low = c.clean(text).lower()
            if want in low or low in want:
                return el
        return None

    @staticmethod
    def _prompt_options(page) -> list:
        """(text, element) for the options on screen, minus the chips showing what is already chosen —
        those carry the same markup as the options, so counting them offered "Bangladesh (+880)" as an
        answer to "How did you hear about us"."""
        for selector in PROMPT_OPTIONS:
            out: list = []
            items = page.locator(selector)
            try:
                count = min(items.count(), 80)
            except Exception:  # noqa: BLE001
                continue
            for i in range(count):
                el = items.nth(i)
                try:
                    if el.evaluate("e => !!e.closest(\"[data-automation-id='selectedItemList']\")"):
                        continue
                    text = c.clean(el.inner_text())
                    if text and not PROMPT_EMPTY_RE.match(text):
                        out.append((text, el))
                except Exception as e:  # noqa: BLE001
                    log.debug("workday: reading option %d: %s", i, e)
            if out:
                return out
        return []

    @staticmethod
    def _prompt_answered(box) -> bool:
        """True once the prompt carries a chosen value. The input itself stays empty, so its own value says
        nothing: what counts is the chip Workday adds beside it."""
        try:
            return bool(box.evaluate(
                """e => { const f = e.closest("[data-automation-id^='formField']")
                    || e.closest("[data-automation-id='multiSelectContainer']") || e.parentElement;
                    return !!(f && f.querySelector("[data-automation-id='selectedItem']")); }"""))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _step_walker(page):
        """The generic walker, pointed at this step's controls.

        A Workday step has no <form> element: its fields sit in the applyFlowPage container. The walker's
        default `form` scope therefore matched nothing at all, and a step whose every field was left empty
        was pressed through to the next one, where Workday answered with "is required and must have a value".
        """
        from jobbot.apply.generic import GenericFormAdapter
        walker = GenericFormAdapter()
        for scope in STEP_SCOPES:
            try:
                if page.locator(GenericFormAdapter._controls_selector(scope, extra=True)).count():
                    walker.scope = scope
                    return walker
            except Exception as e:  # noqa: BLE001
                log.debug("workday: scope %s: %s", scope, e)
        walker.scope = STEP_SCOPES[-1]
        return walker

    def _submit_if_review(self, ctx: ApplyContext) -> bool:
        """True when this was the review page and the application went in."""
        page = ctx.page
        if not self._is_review(page):
            return False
        ctx.step("Submitting")
        c.submit_and_confirm(ctx, SUBMIT_NAMES, refill=lambda: self._fill_step(ctx),
                             click=lambda: self._click_submit(page))
        ctx.step("Submitted")
        return True

    def _is_review(self, page) -> bool:
        """The last step, by its name or by the fact that it offers Submit rather than Next.

        Both, because neither is reliable alone: tenants that put the job title where the step name should
        be leave the heading saying nothing about Review, and a step still rendering has no button yet.
        """
        if re.search(r"\breview\b", self._heading(page), re.I):
            return True
        for name in SUBMIT_NAMES:
            try:
                button = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I))
                if c.visible(button, c.SHORT):
                    return True
            except Exception as e:  # noqa: BLE001
                log.debug("workday: looking for %s: %s", name, e)
        return False

    def _click_submit(self, page) -> None:
        """Press Submit on the review page.

        Its button is the bottom-navigation one, shielded like everything else here, so the name-based
        clicker in common.py would press the inert overlay and then time out waiting for a confirmation that
        was never going to come.
        """
        if not self._click_first(page, NEXT):
            c.click_submit(page, SUBMIT_NAMES)

    # ---------- helpers ----------
    # What every Workday screen has one of: a credentials box, a form field, a file drop, or the Next button.
    RENDERED = ("input[type=password], [data-automation-id='bottom-navigation-next-button'], "
                "[data-automation-id*='formField'], input[type=file]")

    @classmethod
    def _wait_for_render(cls, page, timeout: int = RENDER_WAIT) -> None:
        """Block until the screen that was navigated to has actually drawn.

        Workday routes between steps client-side: the progress bar and the URL change at once, the panel
        arrives a second or two later. Reading the page in that gap sees nothing on it — which is how a run
        that had just chosen the autofill route reported "no Next button" while the Create Account step was
        still rendering, and how the same blank moment hid the password box from _on_account_page.
        """
        try:
            page.wait_for_selector(cls.RENDERED, state="visible", timeout=timeout)
        except Exception as e:  # noqa: BLE001 - a screen that never draws is judged by the caller, not here
            log.debug("workday: nothing rendered within %dms: %s", timeout, e)
            return
        cls._wait_for_fields(page)

    @staticmethod
    def _wait_for_fields(page) -> None:
        """Then wait for the step's own fields, and for their number to stop changing.

        Workday paints a step in two goes: the shell (progress bar, footer buttons) first, the fields a
        second or two later. Reading between the two sees a step with nothing on it — which is how a run
        filled nothing, pressed Save and Continue, and got back "is required and must have a value" on every
        field. A step that genuinely has no fields (Review) simply costs the settle window once.
        """
        from jobbot.apply.generic import GenericFormAdapter
        selector = (GenericFormAdapter._controls_selector(STEP_SCOPES[0], extra=True)
                    + ", input[type=password], " + ", ".join(DROPZONE))
        last = -1
        for _ in range(SETTLE_SAMPLES):     # counted, not clocked: the pacing is the page's own wait
            try:
                now = page.locator(selector).count()
            except Exception as e:  # noqa: BLE001
                log.debug("workday: counting step fields: %s", e)
                return
            if now and now == last:
                return
            last = now
            page.wait_for_timeout(SETTLE_POLL)

    @staticmethod
    def _text(page) -> str:
        try:
            return page.inner_text("body") or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _heading(page) -> str:
        """The step's own title.

        Scoped to the apply flow first: unscoped, every step on Cloudera's tenant came back as "Cloudera
        Careers" (the site banner), so each one looked identical to the last and the walk could not tell
        that pressing Save and Continue had moved anything.
        """
        flow = STEP_SCOPES[0]
        for sel in (AID.format("jobApplicationProgressBarActiveStep"),
                    f"{flow} h2", f"{flow} h1", "h1", "h2"):
            try:
                found = page.locator(sel)
                # the last heading, not the first: Cloudera puts the job title above the step's own name,
                # so "My Experience" is the second h2 and taking .first logged every step as the job title
                el = found.last if sel.startswith(flow) else found.first
                if el.count():
                    text = " ".join((el.inner_text() or "").split())
                    if text:
                        return text[:120]
            except Exception:  # noqa: BLE001
                continue
        return ""

    @staticmethod
    def _on_form(page) -> bool:
        """Inside the application itself, rather than on the posting or the sign-in page.

        The password check is not a nicety. Workday builds its Create Account page out of the same
        `formField` wrappers as the application, so without it this said "we are on the form" while the
        screen showed the signup — _account was skipped, and the walker went on to treat "Password" as a
        screening question and ask the user for it. A page with a password box is a credentials page.
        """
        if WorkdayAdapter._on_account_page(page):
            return False
        try:
            return bool(page.locator(AID.format("bottom-navigation-next-button")).count()) or \
                bool(page.locator("form input[type=file], [data-automation-id*='formField']").count())
        except Exception:  # noqa: BLE001
            return False

    @classmethod
    def _on_account_page(cls, page) -> bool:
        """A credentials screen: the email/password form, or the chooser that leads to it."""
        if cls._on_password_page(page):
            return True
        try:
            return any(page.locator(AID.format(aid)).count() for aid in EMAIL_AUTH)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _on_password_page(page) -> bool:
        try:
            return bool(page.locator("input[type=password]").count())
        except Exception:  # noqa: BLE001
            return False

    def _to_create_account(self, page) -> bool:
        """Switch to the create-account view when Workday opened on sign-in. True when creating.

        The confirmation box is the proof, not the click: the "Create Account" control is a link on some
        tenants and a tab on others, and reporting success on a click that changed nothing would fill a
        sign-in card as though it were a signup.
        """
        if self._is_create_form(page):
            return True
        if self._click_first(page, CREATE_ACCOUNT_LINK):
            page.wait_for_timeout(1200)
        return self._is_create_form(page)

    @staticmethod
    def _is_create_form(page) -> bool:
        """A signup rather than a sign-in: only the signup asks for the password twice."""
        for aid in VERIFY_PASSWORD:
            try:
                if page.locator(AID.format(aid)).count():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _to_sign_in(self, page) -> None:
        self._click_first(page, SIGN_IN_LINK)
        page.wait_for_timeout(1200)

    @staticmethod
    def _tick_account_checkbox(page) -> None:
        """The 'I agree to the Terms' box some tenants require before the account can be created."""
        try:
            boxes = page.locator("input[type=checkbox]:visible")
            for i in range(min(boxes.count(), 3)):
                box = boxes.nth(i)
                if not box.is_checked():
                    box.check(timeout=c.SHORT)
        except Exception as e:  # noqa: BLE001
            log.debug("workday: terms checkbox: %s", e)

    @staticmethod
    def _fill_first(page, aids: tuple[str, ...], value: str) -> bool:
        for aid in aids:
            el = page.locator(AID.format(aid))
            try:
                if el.count() and el.first.is_visible():
                    if c.fill_verified(el.first, value):
                        return True
            except Exception as e:  # noqa: BLE001
                log.debug("workday: fill %s: %s", aid, e)
        return False

    @staticmethod
    def _click_first(page, aids: tuple[str, ...]) -> bool:
        """Click the first of these controls that is on the page, forcing past Workday's click shield.

        Workday marks its real buttons `aria-hidden` and lays a transparent `click_filter` div over each one.
        Playwright refuses a click another element would intercept, so the polite click retries until it
        times out; and the shield does nothing when clicked — watching the network while pressing it shows no
        request leaving the browser at all. Only a forced click on the button itself posts the form.

        That one detail cost a whole Workday run: with the shield in the candidate list, "Create Account"
        pressed whichever shield happened to be first in the DOM, Workday quietly switched to the Sign In
        card, and the sign-in that followed failed against an account that had never been created.
        """
        for aid in aids:
            el = page.locator(AID.format(aid))
            try:
                if not el.count() or not el.first.is_visible():
                    continue
                el.first.scroll_into_view_if_needed(timeout=c.SHORT)
            except Exception as e:  # noqa: BLE001
                log.debug("workday: locating %s: %s", aid, e)
                continue
            for force in (False, True):     # the polite click first: not every control is shielded
                try:
                    el.first.click(timeout=c.SHORT, force=force)
                    log.debug("workday: clicked %s%s", aid, " (forced past the shield)" if force else "")
                    return True
                except Exception as e:  # noqa: BLE001
                    log.debug("workday: click %s (force=%s): %s", aid, force, str(e).splitlines()[0][:120])
        return False
