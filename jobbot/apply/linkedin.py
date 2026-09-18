"""LinkedIn adapter — resolves a listing to the employer's real ATS, then hands off to that adapter.

LinkedIn is not an ATS: a listing is either "Easy Apply" (the form lives on LinkedIn, behind a login) or
"offsite" (the Apply button opens the employer's Greenhouse/Lever/Ashby/… page). The public job page does not
expose the offsite URL — jobspy's `job_url_direct` is always empty here because the `<code id="applyUrl">`
block it scrapes no longer exists, and the guest page only carries a sign-in redirect. The only reliable way
to get it is to click Apply in a signed-in browser and read where LinkedIn sends us.

So: click Apply, capture the destination, re-detect the ATS, and delegate. Anything we cannot drive (Easy
Apply, an unsupported destination, a sign-in wall) raises NeedsHuman with a specific instruction rather than
dead-ending the application.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from urllib.parse import urlparse

from jobbot.apply import common as c
from jobbot.apply.base import (Adapter, ApplyContext, ApplyError, NeedsHuman, detect_ats,
                               get_adapter_for, register)

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

        if self._is_easy_apply(page):
            raise NeedsHuman(
                "LinkedIn Easy Apply — the form is on LinkedIn and there is no external application to fill. "
                "Complete it in the browser window, then click Continue.")

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
